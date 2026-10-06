"""Descriptor-relative POSIX policy; no transport, execution, or network code."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import heapq
import os
import re
import stat
import unicodedata
from contextlib import contextmanager
from functools import wraps
from typing import Any, Iterator

from . import __version__

MAX_TEXT_BYTES = 1_048_576
MAX_DIRECTORY_ENTRIES = 256
MAX_PATH_BYTES = 4096
MAX_COMPONENTS = 128
_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _FLAGS

MESSAGES = {
    "INVALID_PATH": "Use a valid root-relative path or bounded argument.",
    "OUTSIDE_ROOT": "Absolute paths and parent traversal are refused.",
    "SYMLINK_REFUSED": "Symbolic links cannot be traversed.",
    "MOUNT_ESCAPE_REFUSED": "Crossing the root device is refused.",
    "UNSUPPORTED_FILE_TYPE": "The object is not a supported file or directory.",
    "NOT_FOUND": "The requested object or parent does not exist.",
    "ALREADY_EXISTS": "The create target already exists.",
    "WRITE_PROTECTED": "Startup policy denies writes at this path.",
    "STALE_CONTENT": "The expected SHA-256 does not match current content.",
    "PAYLOAD_TOO_LARGE": "The UTF-8 text exceeds the byte limit.",
    "INVALID_TEXT": "The payload is not valid UTF-8 text.",
    "BUSY": "The object is locked by a cooperating process.",
    "OUTCOME_UNKNOWN": "The object changed or a write outcome requires reconciliation; do not retry blindly.",
    "INTERNAL_ERROR": "The filesystem operation failed.",
}


class Refusal(Exception):
    """Public outcome code only, without host error/path details."""

    def __init__(self, outcome: str):
        super().__init__(outcome)
        self.outcome = outcome


def refusal(outcome: str) -> dict[str, Any]:
    return {"ok": False, "outcome": outcome, "message": MESSAGES[outcome]}


def _os_outcome(exc: OSError) -> str:
    return {
        errno.ENOENT: "NOT_FOUND",
        errno.EEXIST: "ALREADY_EXISTS",
        errno.ELOOP: "SYMLINK_REFUSED",
        errno.ENOTDIR: "UNSUPPORTED_FILE_TYPE",
        errno.EISDIR: "UNSUPPORTED_FILE_TYPE",
    }.get(exc.errno, "INTERNAL_ERROR")


def result_method(function):
    """Contain failures inside stable structured tool results."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return {"ok": True, "outcome": "OK", **function(*args, **kwargs)}
        except Refusal as exc:
            return refusal(exc.outcome)
        except OSError as exc:
            return refusal(_os_outcome(exc))
        except Exception:
            return refusal("INTERNAL_ERROR")

    return wrapped


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _snapshot(info: os.stat_result) -> tuple[int, ...]:
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _policy_component(name: str) -> str:
    # Conservative matching also protects case/Unicode aliases on macOS.
    return unicodedata.normalize("NFD", name).casefold()


class RootFilesystem:
    """One retained root descriptor and immutable startup write-prefix policy.

    Callers own this object's lifetime; close only after all tool calls finish.
    Regular files with multiple hard links are refused to avoid outside aliases.
    """

    def __init__(self, root: str | os.PathLike[str], write_denied_prefixes: tuple[str, ...] = ()):
        self._fd = -1
        prefixes = []
        for prefix in write_denied_prefixes:
            # A single conventional trailing slash is allowed in configuration.
            parts = self._parts(prefix[:-1] if prefix.endswith("/") else prefix, directory=True)
            prefixes.append(parts)
        self._denied = tuple(prefixes)
        try:
            supplied = os.fspath(root)
            if not supplied or "\x00" in supplied or ".." in supplied.split("/"):
                raise Refusal("INVALID_PATH")
            path = os.path.abspath(supplied)
            initial = os.stat(path, follow_symlinks=False)
            if stat.S_ISLNK(initial.st_mode):
                raise Refusal("SYMLINK_REFUSED")
            if not stat.S_ISDIR(initial.st_mode):
                raise Refusal("UNSUPPORTED_FILE_TYPE")
            self._fd = os.open(path, _DIRECTORY_FLAGS)
            opened = os.fstat(self._fd)
            canonical = os.path.realpath(path)
            current = os.stat(path, follow_symlinks=False)
            canonical_info = os.stat(canonical, follow_symlinks=False)
            if not stat.S_ISDIR(opened.st_mode) or any(
                _identity(item) != _identity(opened) for item in (initial, current, canonical_info)
            ):
                raise Refusal("OUTCOME_UNKNOWN")
            self.root = canonical
            self.device = opened.st_dev
        except OSError as exc:
            self.close()
            raise Refusal(_os_outcome(exc)) from None
        except Exception:
            self.close()
            raise

    def __enter__(self) -> RootFilesystem:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    @staticmethod
    def _parts(path: str, *, directory: bool = False) -> tuple[str, ...]:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise Refusal("INVALID_PATH")
        if path.startswith("/") or ".." in path.split("/"):
            raise Refusal("OUTSIDE_ROOT")
        try:
            encoded = path.encode("utf-8")
        except UnicodeError:
            raise Refusal("INVALID_PATH") from None
        if len(encoded) > MAX_PATH_BYTES:
            raise Refusal("INVALID_PATH")
        if path == "." and directory:
            return ()
        parts = tuple(path.split("/"))
        if len(parts) > MAX_COMPONENTS or any(p in ("", ".") for p in parts):
            raise Refusal("INVALID_PATH")
        return parts

    def _check_device(self, info: os.stat_result) -> None:
        if info.st_dev != self.device:
            raise Refusal("MOUNT_ESCAPE_REFUSED")

    def _check_kind(self, info: os.stat_result, *, directory: bool) -> None:
        self._check_device(info)
        if stat.S_ISLNK(info.st_mode):
            raise Refusal("SYMLINK_REFUSED")
        supported = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not supported or (not directory and info.st_nlink != 1):
            raise Refusal("UNSUPPORTED_FILE_TYPE")

    def _open(self, parent: int, name: str, *, directory: bool, write: bool = False) -> int:
        # Preliminary check avoids knowingly opening a special object. The
        # authoritative type/device/identity check is repeated on the opened fd.
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        self._check_kind(before, directory=directory)
        flags = _DIRECTORY_FLAGS if directory else (os.O_RDWR if write else os.O_RDONLY) | _FLAGS
        fd = os.open(name, flags, dir_fd=parent)
        try:
            opened = os.fstat(fd)
            self._check_kind(opened, directory=directory)
            if _identity(before) != _identity(opened):
                raise Refusal("OUTCOME_UNKNOWN")
            return fd
        except Exception:
            os.close(fd)
            raise

    @contextmanager
    def _directories(self, parts: tuple[str, ...]) -> Iterator[tuple[int, list[tuple[int, str, int]]]]:
        root_fd = os.dup(self._fd)
        descriptors = [root_fd]
        chain = []
        try:
            self._check_kind(os.fstat(root_fd), directory=True)
            for name in parts:
                parent = descriptors[-1]
                child = self._open(parent, name, directory=True)
                descriptors.append(child)
                chain.append((parent, name, child))
            self._verify_chain(chain)
            yield descriptors[-1], chain
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    def _verify_chain(self, chain: list[tuple[int, str, int]]) -> None:
        try:
            for parent, name, child in chain:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                actual = os.fstat(child)
                if not stat.S_ISDIR(info.st_mode) or _identity(info) != _identity(actual):
                    raise Refusal("OUTCOME_UNKNOWN")
                self._check_device(actual)
        except OSError:
            raise Refusal("OUTCOME_UNKNOWN") from None

    def _verify_path(self, parent: int, name: str, fd: int, chain) -> None:
        self._verify_chain(chain)
        try:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            actual = os.fstat(fd)
            if not stat.S_ISREG(current.st_mode) or _identity(current) != _identity(actual):
                raise Refusal("OUTCOME_UNKNOWN")
            self._check_kind(actual, directory=False)
        except OSError:
            raise Refusal("OUTCOME_UNKNOWN") from None

    @staticmethod
    def _lock(fd: int, *, write: bool) -> None:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                raise Refusal("BUSY") from None
            raise

    def _write_allowed(self, parts: tuple[str, ...]) -> None:
        key = tuple(_policy_component(p) for p in parts)
        if any(key[:len(prefix)] == tuple(_policy_component(p) for p in prefix) for prefix in self._denied):
            raise Refusal("WRITE_PROTECTED")

    @staticmethod
    def _payload(content: str) -> bytes:
        if not isinstance(content, str):
            raise Refusal("INVALID_TEXT")
        if len(content) > MAX_TEXT_BYTES:
            raise Refusal("PAYLOAD_TOO_LARGE")
        try:
            payload = content.encode("utf-8")
        except UnicodeError:
            raise Refusal("INVALID_TEXT") from None
        if len(payload) > MAX_TEXT_BYTES:
            raise Refusal("PAYLOAD_TOO_LARGE")
        return payload

    def _bytes(self, fd: int, *, text: bool) -> tuple[int, str, bytes]:
        before = os.fstat(fd)
        self._check_kind(before, directory=False)
        if text and before.st_size > MAX_TEXT_BYTES:
            raise Refusal("PAYLOAD_TOO_LARGE")
        os.lseek(fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        chunks = []
        size = 0
        # At most the initial size plus one byte: a concurrently growing file
        # cannot make this request read indefinitely.
        remaining = before.st_size + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            remaining -= len(chunk)
            if text:
                if size > MAX_TEXT_BYTES:
                    raise Refusal("PAYLOAD_TOO_LARGE")
                chunks.append(chunk)
        if size != before.st_size or _snapshot(os.fstat(fd)) != _snapshot(before):
            raise Refusal("OUTCOME_UNKNOWN")
        return size, digest.hexdigest(), b"".join(chunks)

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        view = memoryview(payload)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("Write made no progress")
            view = view[count:]
        os.ftruncate(fd, len(payload))
        os.fsync(fd)

    @result_method
    def workspace_status(self) -> dict[str, Any]:
        self._check_kind(os.fstat(self._fd), directory=True)
        return {
            "server_version": __version__, "tool_surface_version": "bootstrap-001",
            "configured_root": self.root, "root_device": self.device,
            "read_enabled": True, "write_enabled": True,
            "write_denied_prefixes": ["/".join(p) or "." for p in self._denied],
            "max_text_bytes": MAX_TEXT_BYTES, "max_directory_entries": MAX_DIRECTORY_ENTRIES,
        }

    @result_method
    def list_directory(self, path: str = ".", limit: int = MAX_DIRECTORY_ENTRIES) -> dict[str, Any]:
        parts = self._parts(path, directory=True)
        if type(limit) is not int or not 1 <= limit <= MAX_DIRECTORY_ENTRIES:
            raise Refusal("INVALID_PATH")
        with self._directories(parts) as (fd, chain):
            # Keep only limit names in memory, even for very large directories.
            with os.scandir(fd) as iterator:
                names = heapq.nsmallest(limit + 1, (entry.name for entry in iterator))
            entries = []
            for name in names[:limit]:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                mode = info.st_mode
                kind = "symlink" if stat.S_ISLNK(mode) else "directory" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "unsupported"
                entries.append({"path": "/".join((*parts, name)), "kind": kind,
                                "same_device": info.st_dev == self.device})
            self._verify_chain(chain)
            return {"path": "/".join(parts) or ".", "entries": entries, "limit": limit,
                    "truncated": len(names) > limit}

    def _read(self, path: str, *, text: bool) -> dict[str, Any]:
        parts = self._parts(path)
        with self._directories(parts[:-1]) as (parent, chain):
            fd = self._open(parent, parts[-1], directory=False)
            try:
                self._lock(fd, write=False)
                size, digest, data = self._bytes(fd, text=text)
                self._verify_path(parent, parts[-1], fd, chain)
                result = {"path": path, "size": size, "sha256": digest}
                if text:
                    try:
                        result["content"] = data.decode("utf-8")
                    except UnicodeError:
                        raise Refusal("INVALID_TEXT") from None
                return result
            finally:
                os.close(fd)

    @result_method
    def read_text_file(self, path: str) -> dict[str, Any]:
        return self._read(path, text=True)

    @result_method
    def sha256_file(self, path: str) -> dict[str, Any]:
        return self._read(path, text=False)

    @result_method
    def create_text_file(self, path: str, content: str) -> dict[str, Any]:
        parts = self._parts(path)
        self._write_allowed(parts)
        payload = self._payload(content)
        with self._directories(parts[:-1]) as (parent, chain):
            try:
                existing = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISLNK(existing.st_mode):
                    raise Refusal("SYMLINK_REFUSED")
                raise Refusal("ALREADY_EXISTS")
            fd = os.open(parts[-1], os.O_RDWR | os.O_CREAT | os.O_EXCL | _FLAGS, 0o600, dir_fd=parent)
            try:
                # Any failure after exclusive creation leaves a possible partial
                # artifact. Never unlink a pathname another actor could own.
                self._check_kind(os.fstat(fd), directory=False)
                self._lock(fd, write=True)
                os.fchmod(fd, 0o600)
                self._write_all(fd, payload)
                os.fsync(parent)
                size, digest, _ = self._bytes(fd, text=False)
                self._verify_path(parent, parts[-1], fd, chain)
                if size != len(payload) or digest != hashlib.sha256(payload).hexdigest():
                    raise Refusal("OUTCOME_UNKNOWN")
                return {"path": path, "size": size, "sha256": digest}
            except Exception:
                raise Refusal("OUTCOME_UNKNOWN") from None
            finally:
                os.close(fd)

    @result_method
    def update_text_file(self, path: str, expected_sha256: str, content: str) -> dict[str, Any]:
        parts = self._parts(path)
        self._write_allowed(parts)
        payload = self._payload(content)
        if not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
            raise Refusal("INVALID_PATH")
        with self._directories(parts[:-1]) as (parent, chain):
            fd = self._open(parent, parts[-1], directory=False, write=True)
            writing = False
            try:
                self._lock(fd, write=True)
                _, current_hash, _ = self._bytes(fd, text=False)
                if current_hash != expected_sha256:
                    raise Refusal("STALE_CONTENT")
                self._verify_path(parent, parts[-1], fd, chain)
                writing = True
                self._write_all(fd, payload)
                size, digest, _ = self._bytes(fd, text=False)
                self._verify_path(parent, parts[-1], fd, chain)
                if size != len(payload) or digest != hashlib.sha256(payload).hexdigest():
                    raise Refusal("OUTCOME_UNKNOWN")
                return {"path": path, "size": size, "sha256": digest}
            except Exception:
                if writing:
                    raise Refusal("OUTCOME_UNKNOWN") from None
                raise
            finally:
                os.close(fd)
