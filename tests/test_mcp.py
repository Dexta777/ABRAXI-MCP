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


@pytest.mark.anyio
async def test_in_process_contract(tmp_path):
    (tmp_path / "protected").mkdir()
    (tmp_path / "protected/file").write_text("readable")
    with RootFilesystem(tmp_path, ("protected",)) as filesystem:
        with anyio.fail_after(15):
            async with Client(build_server(filesystem)) as client:
                assert client.protocol_version
                assert client.server_info.name == "ABRAXI-MCP"
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

                assert (await call("workspace_status"))["configured_root"] == str(tmp_path.resolve())
                assert (await call("list_directory"))["entries"][0]["path"] == "protected"
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
                missing = await client.call_tool("update_text_file", {"path": "file", "content": "bad"})
                assert missing.is_error and "Traceback" not in str(missing.content)
                extra = await client.call_tool("execute_shell", {"command": "anything"})
                assert extra.is_error
                assert (await call("read_text_file", path="file"))["content"] == "new"
                assert not (await client.list_resources()).resources
                assert not (await client.list_prompts()).prompts


@pytest.mark.anyio
async def test_bounded_stdio_process_smoke_and_reaping(tmp_path, monkeypatch):
    processes = []
    original = sdk_stdio._create_platform_compatible_process

    async def capture(**kwargs):
        process = await original(**kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(sdk_stdio, "_create_platform_compatible_process", capture)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "abraxi_mcp", "--root", str(tmp_path), "--write-denied-prefix", "protected"],
    )
    with anyio.fail_after(20):
        async with Client(parameters, read_timeout_seconds=5) as client:
            assert client.protocol_version
            assert {t.name for t in (await client.list_tools()).tools} == TOOLS
            status = (await client.call_tool("workspace_status", {})).structured_content
            assert status["configured_root"] == str(tmp_path.resolve())
            assert status["write_denied_prefixes"] == ["protected"]
            created = (await client.call_tool("create_text_file", {"path": "smoke", "content": "stdio"})).structured_content
            assert created["ok"]
            refused = (await client.call_tool("read_text_file", {"path": "/escape"})).structured_content
            assert refused["outcome"] == "OUTSIDE_ROOT"
            assert (await client.call_tool("read_text_file", {"path": "smoke"})).structured_content["content"] == "stdio"
    assert len(processes) == 1
    assert processes[0].returncode == 0
    with pytest.raises(ChildProcessError):
        os.waitpid(processes[0].pid, os.WNOHANG)
