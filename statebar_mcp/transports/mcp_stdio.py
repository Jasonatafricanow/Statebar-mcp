"""MCP stdio transport (``statebar-mcp mcp``) — the default distribution.

Zero-dependency implementation of the MCP stdio transport: newline-delimited
JSON-RPC 2.0 over stdin/stdout. This is the protocol the MCP spec defines for
stdio, so official-SDK clients (Claude Desktop, Codex, the ``mcp`` Python
SDK) connect to it unchanged; the integration test verifies exactly that.

Exposed tools (frozen Contract): user_state.observe / snapshot /
context_candidates / get_state / health.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict, Optional

from ..core.service import UserStateService
from .contract import TOOLS, Contract, ContractError

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "statebar-mcp", "version": "0.1.0"}

_CAPABILITIES = {"tools": {"listChanged": False}}


def _make_response(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _make_error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def force_utf8_stdio() -> None:
    """The MCP stdio wire format is UTF-8 newline-delimited JSON-RPC. On
    Windows the standard streams default to the ANSI codepage (e.g. GBK), so
    ANY Chinese text would be corrupted at the protocol boundary unless we
    pin the encoding here. Must not depend on PYTHONUTF8/PYTHONIOENCODING."""
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):  # pragma: no cover
            # non-rewritable stream (e.g. replaced by tests); leave as-is
            pass


class MCPStdioServer:
    """Synchronous JSON-RPC loop over stdin/stdout (no external deps)."""

    def __init__(self, service: UserStateService, stdin=None, stdout=None, stderr=None):
        force_utf8_stdio()
        self.contract = Contract(service)
        self._stdin = stdin or sys.stdin
        self._stdout = stdout or sys.stdout
        self._stderr = stderr or sys.stderr

    def _log(self, message: str) -> None:
        # stdout is reserved for protocol frames; diagnostics go to stderr.
        print(f"[statebar-mcp mcp] {message}", file=self._stderr, flush=True)

    def handle_request(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return a response dict for requests, None for notifications."""
        method = message.get("method", "")
        request_id = message.get("id")
        params = message.get("params") or {}

        # Notifications carry no id and expect no response.
        if request_id is None:
            if method == "notifications/initialized":
                self._log("client initialized")
            elif method == "notifications/cancelled":
                pass
            return None

        try:
            if method == "initialize":
                result = {
                    "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                    "capabilities": _CAPABILITIES,
                    "serverInfo": SERVER_INFO,
                }
                return _make_response(request_id, result)
            if method == "ping":
                return _make_response(request_id, {})
            if method == "tools/list":
                return _make_response(request_id, {"tools": TOOLS})
            if method == "tools/call":
                return self._handle_tool_call(request_id, params)
            return _make_error(request_id, -32601, f"method not found: {method}")
        except Exception as exc:  # pragma: no cover
            logger.exception("mcp stdio request failed")
            return _make_error(request_id, -32603, str(exc))

    def _handle_tool_call(self, request_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            result = self.contract.call(name, arguments)
            text = json.dumps(result, ensure_ascii=False, default=str)
            return _make_response(
                request_id,
                {"content": [{"type": "text", "text": text}], "isError": False},
            )
        except ContractError as exc:
            text = json.dumps({"error": str(exc)}, ensure_ascii=False)
            return _make_response(
                request_id,
                {"content": [{"type": "text", "text": text}], "isError": True},
            )

    def run(self) -> None:
        self._log("stdio server ready (protocol " + PROTOCOL_VERSION + ")")
        for line in self._stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._log(f"ignoring non-JSON frame: {line[:80]!r}")
                continue
            response = self.handle_request(message)
            if response is not None:
                self._stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                self._stdout.flush()
        self._log("stdin closed; exiting")


def run_stdio(service: UserStateService) -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    MCPStdioServer(service).run()
