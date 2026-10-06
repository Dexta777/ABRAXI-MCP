import errno
import fcntl
import hashlib
import os
import socket
import stat
from types import SimpleNamespace

import pytest

from abraxi_mcp.filesystem import (
    MAX_DIRECTORY_ENTRIES, MAX_TEXT_BYTES, Refusal, RootFilesystem,
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def fs(tmp_path):
    with RootFilesystem(tmp_path, ("protected/",)) as filesystem:
        yield filesystem


@pytest.mark.parametrize("path,outcome", [
    ("/absolute", "OUTSIDE_ROOT"), ("../escape", "OUTSIDE_ROOT"),
    ("dir/../escape", "OUTSIDE_ROOT"), ("a\x00b", "INVALID_PATH"),
    ("", "INVALID_PATH"), ("a//b", "INVALID_PATH"),
    ("./a", "INVALID_PATH"), ("a/", "INVALID_PATH"),
    ("\ud800", "INVALID_PATH"), ("a" * 4097, "INVALID_PATH"),
])
def test_path_boundary(fs, path, outcome):
    for operation in [fs.read_text_file, fs.sha256_file, fs.list_directory]:
        assert operation(path)["outcome"] == outcome
    assert fs.create_text_file(path, "x")["outcome"] == outcome
    assert fs.update_text_file(path, sha(b"x"), "y")["outcome"] == outcome


def test_explicit_root_establishment(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    for spelling in [link, str(link) + "/", str(link) + "/."]:
        with pytest.raises(Refusal, match="SYMLINK_REFUSED"):
            RootFilesystem(spelling)
    with pytest.raises(Refusal, match="NOT_FOUND"):
        RootFilesystem(tmp_path / "missing")
    file = tmp_path / "file"
    file.write_text("x")
    with pytest.raises(Refusal, match="UNSUPPORTED_FILE_TYPE"):
        RootFilesystem(file)


def test_intermediate_and_final_symlinks(fs, tmp_path):
    (tmp_path / "directory").mkdir()
    (tmp_path / "directory/file").write_text("unchanged")
    (tmp_path / "linkdir").symlink_to("directory", target_is_directory=True)
    (tmp_path / "linkfile").symlink_to("directory/file")
    for path in ["linkdir/file", "linkfile"]:
        assert fs.read_text_file(path)["outcome"] == "SYMLINK_REFUSED"
        assert fs.sha256_file(path)["outcome"] == "SYMLINK_REFUSED"
        assert fs.create_text_file(path, "changed")["outcome"] == "SYMLINK_REFUSED"
        assert fs.update_text_file(path, sha(b"unchanged"), "changed")["outcome"] == "SYMLINK_REFUSED"
    assert fs.list_directory("linkdir")["outcome"] == "SYMLINK_REFUSED"
    assert (tmp_path / "directory/file").read_text() == "unchanged"


@pytest.mark.parametrize("kind", ["fifo", "socket", "directory"])
def test_unsupported_types_are_nonblocking(fs, tmp_path, kind, monkeypatch):
    path = tmp_path / "special"
    sock = None
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "socket":
        sock = socket.socket(socket.AF_UNIX)
        # macOS has a short sockaddr_un pathname limit. Binding a relative
        # name in the same synthetic directory avoids that fixture constraint.
        monkeypatch.chdir(tmp_path)
        sock.bind("special")
    else:
        path.mkdir()
    try:
        assert fs.read_text_file("special")["outcome"] == "UNSUPPORTED_FILE_TYPE"
        assert fs.sha256_file("special")["outcome"] == "UNSUPPORTED_FILE_TYPE"
        assert fs.update_text_file("special", sha(b""), "new")["outcome"] == "UNSUPPORTED_FILE_TYPE"
    finally:
        if sock:
            sock.close()


def test_device_boundary_from_opened_descriptor(fs, tmp_path, monkeypatch):
    (tmp_path / "file").write_text("x")
    real_fstat = os.fstat

    def other_device(fd):
        original = real_fstat(fd)
        if stat.S_ISREG(original.st_mode):
            return SimpleNamespace(st_dev=original.st_dev + 1, st_mode=original.st_mode)
        return original

    monkeypatch.setattr(os, "fstat", other_device)
    assert fs.read_text_file("file")["outcome"] == "MOUNT_ESCAPE_REFUSED"
    assert fs.update_text_file("file", sha(b"x"), "y")["outcome"] == "MOUNT_ESCAPE_REFUSED"
    # Simulated identity change, not qualification against a real mount.
    assert (tmp_path / "file").read_bytes() == b"x"


def test_directory_device_boundary(fs, tmp_path, monkeypatch):
    (tmp_path / "child").mkdir()
    real_open = os.open
    real_fstat = os.fstat
    opened = set()

    def capture(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if path == "child":
            opened.add(fd)
        return fd

    def other_device(fd):
        info = real_fstat(fd)
        if fd in opened:
            return SimpleNamespace(st_dev=info.st_dev + 1, st_mode=info.st_mode)
        return info

    monkeypatch.setattr(os, "open", capture)
    monkeypatch.setattr(os, "fstat", other_device)
    assert fs.list_directory("child")["outcome"] == "MOUNT_ESCAPE_REFUSED"


def test_listing_is_one_level_sorted_and_bounded(fs, tmp_path):
    for name in ["z", "a", "m"]:
        (tmp_path / name).write_text(name)
    (tmp_path / "directory").mkdir()
    (tmp_path / "directory/hidden").write_text("nested")
    (tmp_path / "link").symlink_to("directory")
    result = fs.list_directory()
    assert result["ok"] and not result["truncated"]
    assert [e["path"] for e in result["entries"]] == ["a", "directory", "link", "m", "z"]
    assert next(e for e in result["entries"] if e["path"] == "link")["kind"] == "symlink"
    limited = fs.list_directory(limit=2)
    assert limited["truncated"] and [e["path"] for e in limited["entries"]] == ["a", "directory"]
    assert fs.list_directory("directory")["entries"][0]["path"] == "directory/hidden"


@pytest.mark.parametrize("limit", [0, -1, MAX_DIRECTORY_ENTRIES + 1, True, "2"])
def test_listing_invalid_limit(fs, limit):
    assert fs.list_directory(limit=limit)["outcome"] == "INVALID_PATH"


def test_utf8_read_and_binary_hash(fs, tmp_path):
    content = "αβ\r\nhello 🌍\n"
    data = content.encode()
    (tmp_path / "utf8").write_bytes(data)
    result = fs.read_text_file("utf8")
    assert result == {"ok": True, "outcome": "OK", "path": "utf8", "size": len(data),
                      "content": content, "sha256": sha(data)}
    (tmp_path / "binary").write_bytes(b"\xff\x00\x80")
    refused = fs.read_text_file("binary")
    assert refused["outcome"] == "INVALID_TEXT" and "content" not in refused
    hashed = fs.sha256_file("binary")
    assert hashed["ok"] and hashed["sha256"] == sha(b"\xff\x00\x80") and hashed["size"] == 3


def test_text_byte_bound(fs, tmp_path):
    (tmp_path / "large").write_bytes(b"x" * (MAX_TEXT_BYTES + 1))
    result = fs.read_text_file("large")
    assert result["outcome"] == "PAYLOAD_TOO_LARGE" and "content" not in result
    assert fs.sha256_file("large")["size"] == MAX_TEXT_BYTES + 1
    assert fs.create_text_file("new", "é" * MAX_TEXT_BYTES)["outcome"] == "PAYLOAD_TOO_LARGE"
    assert not (tmp_path / "new").exists()
    assert fs.create_text_file("new", "\ud800")["outcome"] == "INVALID_TEXT"
    assert fs.update_text_file("large", sha(b"x" * (MAX_TEXT_BYTES + 1)), "x" * (MAX_TEXT_BYTES + 1))["outcome"] == "PAYLOAD_TOO_LARGE"
    assert (tmp_path / "large").stat().st_size == MAX_TEXT_BYTES + 1


def test_create_exclusive_exact_mode_and_fsync(fs, tmp_path, monkeypatch):
    real_fsync = os.fsync
    flushed = []

    def record(fd):
        flushed.append(os.fstat(fd).st_mode)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", record)
    content = "new 🌍\r\n"
    result = fs.create_text_file("new", content)
    assert result["ok"] and result["sha256"] == sha(content.encode())
    assert (tmp_path / "new").read_bytes() == content.encode()
    assert stat.S_IMODE((tmp_path / "new").stat().st_mode) == 0o600
    assert any(stat.S_ISREG(mode) for mode in flushed)
    assert any(stat.S_ISDIR(mode) for mode in flushed)
    assert fs.create_text_file("new", "overwrite")["outcome"] == "ALREADY_EXISTS"
    assert (tmp_path / "new").read_bytes() == content.encode()
    assert fs.create_text_file("missing/file", "x")["outcome"] == "NOT_FOUND"
    assert not (tmp_path / "missing").exists()


def test_protected_prefix_readable_and_component_bound(fs, tmp_path):
    (tmp_path / "protected").mkdir()
    (tmp_path / "protected/file").write_bytes(b"old")
    assert fs.read_text_file("protected/file")["ok"]
    assert fs.sha256_file("protected/file")["ok"]
    assert fs.list_directory("protected")["ok"]
    assert fs.create_text_file("protected/new", "x")["outcome"] == "WRITE_PROTECTED"
    assert fs.update_text_file("protected/file", sha(b"old"), "x")["outcome"] == "WRITE_PROTECTED"
    assert fs.create_text_file("PROTECTED/new", "x")["outcome"] == "WRITE_PROTECTED"
    assert fs.create_text_file("protected-suffix", "x")["ok"]
    assert (tmp_path / "protected/file").read_bytes() == b"old"


def test_protection_is_configuration_not_universal(tmp_path):
    (tmp_path / "ABRAXI-MCP").mkdir()
    with RootFilesystem(tmp_path) as fs:
        assert fs.create_text_file("ABRAXI-MCP/new", "x")["ok"]
    with RootFilesystem(tmp_path, (".",)) as fs:
        assert fs.create_text_file("any", "x")["outcome"] == "WRITE_PROTECTED"


def test_unicode_prefix_alias_denied(tmp_path):
    with RootFilesystem(tmp_path, ("café",)) as fs:
        assert fs.create_text_file("cafe\u0301/new", "x")["outcome"] == "WRITE_PROTECTED"


def test_hardlink_alias_refused(fs, tmp_path):
    (tmp_path / "protected").mkdir()
    (tmp_path / "protected/file").write_bytes(b"old")
    os.link(tmp_path / "protected/file", tmp_path / "alias")
    assert fs.update_text_file("alias", sha(b"old"), "x")["outcome"] == "UNSUPPORTED_FILE_TYPE"
    assert fs.read_text_file("alias")["outcome"] == "UNSUPPORTED_FILE_TYPE"
    assert (tmp_path / "protected/file").read_bytes() == b"old"


def test_update_match_stale_and_truncate(fs, tmp_path):
    (tmp_path / "file").write_bytes(b"long original")
    old_mode = stat.S_IMODE((tmp_path / "file").stat().st_mode)
    stale = fs.update_text_file("file", sha(b"wrong"), "bad")
    assert stale["outcome"] == "STALE_CONTENT"
    assert (tmp_path / "file").read_bytes() == b"long original"
    updated = fs.update_text_file("file", sha(b"long original"), "é")
    assert updated["ok"] and updated["sha256"] == sha("é".encode()) and updated["size"] == 2
    assert (tmp_path / "file").read_bytes() == "é".encode()
    assert stat.S_IMODE((tmp_path / "file").stat().st_mode) == old_mode
    assert fs.update_text_file("missing", sha(b""), "x")["outcome"] == "NOT_FOUND"
    assert fs.update_text_file("file", "not-a-hash", "x")["outcome"] == "INVALID_PATH"
    assert fs.update_text_file("file", sha("é".encode()), "")["ok"]
    assert (tmp_path / "file").read_bytes() == b""


def test_opened_descriptor_kind_authoritative(fs, tmp_path, monkeypatch):
    (tmp_path / "file").write_bytes(b"old")
    real_fstat = os.fstat
    writes = []
    real_write = os.write

    def wrong_kind(fd):
        info = real_fstat(fd)
        if stat.S_ISREG(info.st_mode):
            return SimpleNamespace(st_dev=info.st_dev, st_mode=stat.S_IFIFO | 0o600, st_nlink=1)
        return info

    def count_write(*args):
        writes.append(1)
        return real_write(*args)

    monkeypatch.setattr(os, "fstat", wrong_kind)
    monkeypatch.setattr(os, "write", count_write)
    assert fs.update_text_file("file", sha(b"old"), "new")["outcome"] == "UNSUPPORTED_FILE_TYPE"
    assert not writes and (tmp_path / "file").read_bytes() == b"old"


def test_lock_contention_one_nonblocking_attempt(fs, tmp_path, monkeypatch):
    (tmp_path / "file").write_bytes(b"old")
    locked = os.open(tmp_path / "file", os.O_RDWR)
    real_flock = fcntl.flock
    attempts = []
    real_flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def record(fd, flags):
        attempts.append(flags)
        return real_flock(fd, flags)

    try:
        monkeypatch.setattr(fcntl, "flock", record)
        assert fs.update_text_file("file", sha(b"old"), "new")["outcome"] == "BUSY"
        assert attempts == [fcntl.LOCK_EX | fcntl.LOCK_NB]
        assert (tmp_path / "file").read_bytes() == b"old"
    finally:
        os.close(locked)


def test_hash_only_after_lock(fs, tmp_path, monkeypatch):
    (tmp_path / "file").write_bytes(b"old")
    original = fs._lock

    def change_before_lock(fd, *, write):
        os.pwrite(fd, b"new", 0)
        original(fd, write=write)

    monkeypatch.setattr(fs, "_lock", change_before_lock)
    assert fs.update_text_file("file", sha(b"old"), "bad")["outcome"] == "STALE_CONTENT"
    assert (tmp_path / "file").read_bytes() == b"new"


def test_replacement_after_write_unknown_no_retry(fs, tmp_path, monkeypatch):
    (tmp_path / "file").write_bytes(b"old")
    original = fs._write_all
    calls = []

    def replace(fd, payload):
        calls.append(fd)
        original(fd, payload)
        os.rename(tmp_path / "file", tmp_path / "retained")
        (tmp_path / "file").write_bytes(b"other actor")

    monkeypatch.setattr(fs, "_write_all", replace)
    assert fs.update_text_file("file", sha(b"old"), "updated")["outcome"] == "OUTCOME_UNKNOWN"
    assert len(calls) == 1
    assert (tmp_path / "retained").read_bytes() == b"updated"
    assert (tmp_path / "file").read_bytes() == b"other actor"


def test_directory_rename_unknown(fs, tmp_path, monkeypatch):
    (tmp_path / "parent").mkdir()
    (tmp_path / "parent/file").write_bytes(b"old")
    original = fs._write_all

    def rename_parent(fd, payload):
        original(fd, payload)
        os.rename(tmp_path / "parent", tmp_path / "renamed")
        (tmp_path / "parent").mkdir()
        (tmp_path / "parent/file").write_bytes(b"other actor")

    monkeypatch.setattr(fs, "_write_all", rename_parent)
    assert fs.update_text_file("parent/file", sha(b"old"), "new")["outcome"] == "OUTCOME_UNKNOWN"
    assert (tmp_path / "parent/file").read_bytes() == b"other actor"
    assert (tmp_path / "renamed/file").read_bytes() == b"new"


def test_create_replacement_unknown(fs, tmp_path, monkeypatch):
    original = fs._write_all

    def replace(fd, payload):
        original(fd, payload)
        os.rename(tmp_path / "file", tmp_path / "retained")
        (tmp_path / "file").write_bytes(b"other actor")

    monkeypatch.setattr(fs, "_write_all", replace)
    assert fs.create_text_file("file", "new")["outcome"] == "OUTCOME_UNKNOWN"
    assert (tmp_path / "file").read_bytes() == b"other actor"
    assert (tmp_path / "retained").read_bytes() == b"new"


def test_write_failure_unknown_preserves_partial(fs, tmp_path, monkeypatch):
    (tmp_path / "file").write_bytes(b"old")

    def failed(fd, payload):
        os.pwrite(fd, b"x", 0)
        raise OSError(errno.EIO, "private host information")

    monkeypatch.setattr(fs, "_write_all", failed)
    result = fs.update_text_file("file", sha(b"old"), "new")
    assert result["outcome"] == "OUTCOME_UNKNOWN"
    assert "private" not in str(result)
    assert (tmp_path / "file").read_bytes() == b"xld"


def test_internal_error_does_not_leak(fs, monkeypatch):
    def failed(*args, **kwargs):
        raise OSError(errno.EACCES, "private path and traceback")

    monkeypatch.setattr(os, "scandir", failed)
    result = fs.list_directory()
    assert result["outcome"] == "INTERNAL_ERROR"
    assert "private" not in str(result) and "Traceback" not in str(result)


def test_status(fs, tmp_path):
    result = fs.workspace_status()
    assert result["ok"] and result["server_version"] == "0.1.0"
    assert result["configured_root"] == str(tmp_path.resolve())
    assert result["root_device"] == tmp_path.stat().st_dev
    assert result["read_enabled"] and result["write_enabled"]
    assert result["write_denied_prefixes"] == ["protected"]
    assert result["max_text_bytes"] == MAX_TEXT_BYTES
