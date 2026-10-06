import hashlib
import os
import sys

import anyio
import pytest
from mcp import Client, StdioServerParameters
import mcp.client.stdio as sdk_stdio

from abraxi_mcp.__main__ import main
from abraxi_mcp.filesystem import RootFilesystem
from abraxi_mcp.server import build_server

TOOLS = {"workspace_status", "list_directory", "read_text_file", "sha256_file",
         "create_text_file", "update_text_file"}


def test_cli_requires_root_and_has_no_transport_option(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert not captured.out and "--root" in captured.err
    with pytest.raises(SystemExit) as exc:
        main(["--root", "unused", "--transport", "http"])
    assert exc.value.code == 2


def test_cli_refuses_invalid_root_on_stderr(tmp_path, capsys):
    assert main(["--root", str(tmp_path / "missing")]) == 2
    result = capsys.readouterr()
    assert not result.out and "NOT_FOUND" in result.err and "Traceback" not in result.err


def test_cli_help_exposes_read_prefix_without_transport(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--read-denied-prefix" in help_text and "--write-denied-prefix" in help_text
    assert "--transport" not in help_text


@pytest.mark.anyio
async def test_in_process_contract(tmp_path):
    (tmp_path / "protected").mkdir()
    (tmp_path / "protected/file").write_text("readable")
    (tmp_path / "sealed-file").write_text("synthetic")
    (tmp_path / "sealed-dir").mkdir()
    (tmp_path / "project").mkdir()
    (tmp_path / "project/hidden").write_text("synthetic")
    (tmp_path / "project/visible").write_text("public")
    prefixes = ("sealed-file", "sealed-dir", "project/hidden", "sealed-absent")
    with RootFilesystem(tmp_path, ("protected",), read_denied_prefixes=prefixes) as filesystem:
        with anyio.fail_after(15):
            async with Client(build_server(filesystem)) as client:
                assert client.protocol_version
                assert client.server_info.name == "ABRAXI-MCP"
                assert client.server_info.version == "0.2.0"
                listed = await client.list_tools()
                assert len(listed.tools) == 6 and {tool.name for tool in listed.tools} == TOOLS
                assert listed.next_cursor is None
                for tool in listed.tools:
                    assert tool.input_schema["type"] == "object"
                    assert tool.output_schema is not None
                update = next(t for t in listed.tools if t.name == "update_text_file")
                assert "expected_sha256" in update.input_schema["required"]

                async def call(name, **arguments):
                    result = await client.call_tool(name, arguments)
                    assert not result.is_error
                    assert isinstance(result.structured_content, dict)
                    assert "ok" in result.structured_content and "outcome" in result.structured_content
                    assert "Traceback" not in str(result.content)
                    return result.structured_content

                status = await call("workspace_status")
                assert status["configured_root"] == str(tmp_path.resolve())
                assert status["server_version"] == "0.2.0"
                assert status["read_denied_prefixes"] == list(prefixes)
                assert "protected" in [entry["path"] for entry in (await call("list_directory"))["entries"]]
                assert (await call("create_text_file", path="file", content="α"))["ok"]
                data = "α".encode()
                digest = hashlib.sha256(data).hexdigest()
                read = await call("read_text_file", path="file")
                assert read["content"] == "α" and read["size"] == len(data) and read["sha256"] == digest
                assert (await call("sha256_file", path="file"))["sha256"] == digest
                assert (await call("update_text_file", path="file", expected_sha256=digest, content="new"))["ok"]
                assert (await call("update_text_file", path="file", expected_sha256=digest, content="bad"))["outcome"] == "STALE_CONTENT"
                assert (await call("read_text_file", path="../escape"))["outcome"] == "OUTSIDE_ROOT"
                assert (await call("create_text_file", path="protected/new", content="bad"))["outcome"] == "WRITE_PROTECTED"
                assert (await call("read_text_file", path="protected/file"))["content"] == "readable"
                for path in ["sealed-file", "sealed-dir", "sealed-absent", "project/hidden"]:
                    for tool in ["read_text_file", "sha256_file", "list_directory"]:
                        assert (await call(tool, path=path))["outcome"] == "READ_PROTECTED"
                    assert (await call("create_text_file", path=path, content="bad"))["outcome"] == "WRITE_PROTECTED"
                    assert (await call("update_text_file", path=path, expected_sha256=digest, content="bad"))["outcome"] == "WRITE_PROTECTED"
                parent = await call("list_directory", path="project", limit=1)
                assert parent["entries"] == [{"path": "project/visible", "kind": "file", "same_device": True}]
                assert not parent["truncated"] and "hidden" not in str(parent)
                missing = await client.call_tool("update_text_file", {"path": "file", "content": "bad"})
                assert missing.is_error and "Traceback" not in str(missing.content)
                extra = await client.call_tool("execute_shell", {"command": "anything"})
                assert extra.is_error
                assert (await call("read_text_file", path="file"))["content"] == "new"
                assert not (await client.list_resources()).resources
                assert not (await client.list_prompts()).prompts


@pytest.mark.anyio
async def test_bounded_stdio_process_smoke_and_reaping(tmp_path, monkeypatch):
    (tmp_path / "sealed-file").write_text("synthetic")
    (tmp_path / "sealed-dir").mkdir()
    (tmp_path / "project").mkdir()
    (tmp_path / "project/hidden").write_text("synthetic")
    (tmp_path / "project/visible").write_text("public")
    processes = []
    original = sdk_stdio._create_platform_compatible_process

    async def capture(**kwargs):
        process = await original(**kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(sdk_stdio, "_create_platform_compatible_process", capture)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "abraxi_mcp", "--root", str(tmp_path), "--write-denied-prefix", "protected",
              "--read-denied-prefix", "sealed-file", "--read-denied-prefix", "sealed-dir",
              "--read-denied-prefix", "project/hidden"],
    )
    with anyio.fail_after(20):
        async with Client(parameters, read_timeout_seconds=5) as client:
            assert client.protocol_version
            listed = await client.list_tools()
            assert len(listed.tools) == 6 and {t.name for t in listed.tools} == TOOLS
            assert client.server_info.version == "0.2.0"
            status = (await client.call_tool("workspace_status", {})).structured_content
            assert status["configured_root"] == str(tmp_path.resolve())
            assert status["write_denied_prefixes"] == ["protected"]
            assert status["read_denied_prefixes"] == ["sealed-file", "sealed-dir", "project/hidden"]
            created = (await client.call_tool("create_text_file", {"path": "smoke", "content": "stdio"})).structured_content
            assert created["ok"]
            refused = (await client.call_tool("read_text_file", {"path": "/escape"})).structured_content
            assert refused["outcome"] == "OUTSIDE_ROOT"
            assert (await client.call_tool("read_text_file", {"path": "smoke"})).structured_content["content"] == "stdio"
            for path in ["sealed-file", "sealed-file/absent", "sealed-dir", "project/hidden"]:
                for tool in ["read_text_file", "sha256_file", "list_directory"]:
                    result = await client.call_tool(tool, {"path": path})
                    assert not result.is_error and result.structured_content["outcome"] == "READ_PROTECTED"
                assert (await client.call_tool("create_text_file", {"path": path, "content": "bad"})).structured_content["outcome"] == "WRITE_PROTECTED"
                assert (await client.call_tool("update_text_file", {"path": path, "expected_sha256": created["sha256"], "content": "bad"})).structured_content["outcome"] == "WRITE_PROTECTED"
            root_listing = (await client.call_tool("list_directory", {"path": "."})).structured_content
            assert [entry["path"] for entry in root_listing["entries"]] == ["project", "smoke"]
            parent = (await client.call_tool("list_directory", {"path": "project", "limit": 1})).structured_content
            assert parent["entries"] == [{"path": "project/visible", "kind": "file", "same_device": True}]
            assert not parent["truncated"] and "hidden" not in str(parent)
    assert len(processes) == 1
    assert processes[0].returncode == 0
    with pytest.raises(ChildProcessError):
        os.waitpid(processes[0].pid, os.WNOHANG)
