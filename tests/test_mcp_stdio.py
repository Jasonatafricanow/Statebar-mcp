"""MCP stdio transport integration tests.

Two layers:
1. Raw JSON-RPC over the subprocess (protocol correctness).
2. Official ``mcp`` SDK client against OUR server (interop proof): a real
   ClientSession over stdio must be able to list and call the five tools.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def spawn_server(db_path):
    import os

    env = dict(
        os.environ,
        PYTHONPATH="",  # isolate from polluted host env
        DSH_USER_STATE_DB=str(db_path),
        DSH_USER_STATE_LLM_MOCK="1",
    )
    proc = subprocess.Popen(
        [PYTHON, "-m", "statebar_mcp", "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(ROOT),
    )
    return proc


def rpc_call(proc, method, params=None, timeout=10):
    import uuid

    rid = uuid.uuid4().hex
    frame = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
    proc.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("id") == rid:
            return msg
    raise TimeoutError(f"no response for {method}")


@pytest.fixture()
def proc(tmp_path):
    p = spawn_server(tmp_path / "test.db")
    try:
        result = rpc_call(p, "initialize", {"protocolVersion": "2024-11-05"})
        assert result["result"]["serverInfo"]["name"] == "statebar-mcp"
        yield p
    finally:
        p.stdin.close()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


class TestMCPStdioRaw:
    def test_ping(self, proc):
        msg = rpc_call(proc, "ping")
        assert msg["result"] == {}

    def test_tools_list(self, proc):
        msg = rpc_call(proc, "tools/list")
        names = [t["name"] for t in msg["result"]["tools"]]
        assert names == [
            "user_state.observe",
            "user_state.snapshot",
            "user_state.context_candidates",
            "user_state.get_state",
            "user_state.health",
        ]

    def test_observe_snapshot_roundtrip(self, proc):
        now = datetime.now(timezone.utc).isoformat()
        msg = rpc_call(
            proc,
            "tools/call",
            {
                "name": "user_state.observe",
                "arguments": {
                    "subject_id": "user-001",
                    "event_id": "m1",
                    "text": "我刚睡醒",
                    "observed_at": now,
                    "source": {"type": "conversation", "platform": "cli"},
                },
            },
        )
        assert msg["result"]["isError"] is False
        content = json.loads(msg["result"]["content"][0]["text"])
        assert content["status"] == "accepted"

        msg2 = rpc_call(
            proc,
            "tools/call",
            {"name": "user_state.snapshot", "arguments": {"subject_id": "user-001"}},
        )
        snap = json.loads(msg2["result"]["content"][0]["text"])
        assert "awake" in snap["text"]

    def test_unknown_tool_error(self, proc):
        msg = rpc_call(proc, "tools/call", {"name": "nope", "arguments": {}})
        assert msg["result"]["isError"] is True

    def test_unknown_method_error(self, proc):
        import uuid

        rid = uuid.uuid4().hex
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "bogus"}) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            msg = json.loads(proc.stdout.readline())
            if msg.get("id") == rid:
                assert msg["error"]["code"] == -32601
                return
        raise TimeoutError("no error response")


class TestMCPOfficialSDKInterop:
    @pytest.mark.skipif(
        __import__("importlib").util.find_spec("mcp") is None,
        reason="official mcp SDK not installed",
    )
    def test_sdk_client_session(self, tmp_path):
        import anyio

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def run():
            import os

            params = StdioServerParameters(
                command=PYTHON,
                args=["-m", "statebar_mcp", "mcp"],
                env={
                    **os.environ,
                    "PYTHONPATH": "",
                    "DSH_USER_STATE_DB": str(tmp_path / "sdk.db"),
                    "DSH_USER_STATE_LLM_MOCK": "1",
                },
                cwd=str(ROOT),
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = [t.name for t in tools.tools]
                    assert "user_state.observe" in names
                    assert "user_state.snapshot" in names

                    now = datetime.now(timezone.utc).isoformat()
                    result = await session.call_tool(
                        "user_state.observe",
                        {
                            "subject_id": "sdk-001",
                            "event_id": "s1",
                            "text": "我睡了",
                            "observed_at": now,
                        },
                    )
                    assert result.is_error is False
                    snap = await session.call_tool(
                        "user_state.snapshot", {"subject_id": "sdk-001"}
                    )
                    assert "sleeping" in snap.content[0].text

                    health = await session.call_tool("user_state.health", {})
                    assert '"status": "ok"' in health.content[0].text

        anyio.run(run)
