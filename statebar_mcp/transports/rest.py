"""REST transport (``statebar-mcp serve``).

Stdlib-only HTTP server exposing the frozen Contract:

    POST /v1/observe             {"subject_id","event_id","text","observed_at","source"}
    GET  /v1/snapshot            ?subject_id=&query=
    GET  /v1/context-candidates  ?subject_id=
    GET  /v1/state               ?subject_id=&category=&key=
    GET  /v1/health

Security (fail-closed):

- Default bind is 127.0.0.1. The CLI REFUSES to start on a non-loopback
  address without an auth token (``DSH_USER_STATE_SERVE_TOKEN``).
- When a token is configured, EVERY endpoint — including /v1/health —
  requires ``Authorization: Bearer <token>`` (constant-time compare).
- Request bodies are capped (default 64 KiB → 413).
"""

from __future__ import annotations

import hmac
import json
import logging
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from ..core.service import UserStateService
from .contract import Contract, ContractError

logger = logging.getLogger(__name__)

DEFAULT_MAX_BODY_BYTES = 64 * 1024


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


class _Handler(BaseHTTPRequestHandler):
    server_version = "statebar-mcp/0.1"
    protocol_version = "HTTP/1.1"
    contract: Contract = None  # set by factory
    auth_token: str = ""        # set by factory; "" disables auth
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    # Socket-level read timeout for this connection. Bounds EVERY blocking
    # read (headers, bodies, drains) so slow/incomplete requests can never
    # pin a handler thread forever.
    request_timeout: float = 10.0
    # How long the reject paths may spend draining an (in)complete body
    # before responding. Keeps 401/413 latency bounded for slow-loris style
    # clients that declare Content-Length without sending anything.
    drain_timeout: float = 0.5

    def setup(self) -> None:
        super().setup()
        try:
            self.connection.settimeout(self.request_timeout)
        except OSError:  # pragma: no cover
            pass

    def log_message(self, fmt, *args):  # quieter
        logger.debug("http %s %s", self.address_string(), fmt % args)

    # -- helpers ----------------------------------------------------------------

    def _send_json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        """Bearer-token gate. No token configured = open (localhost mode)."""
        if not self.auth_token:
            return True
        header = self.headers.get("Authorization", "")
        scheme, _, provided = header.partition(" ")
        if scheme.lower() != "bearer" or not provided:
            return False
        return hmac.compare_digest(provided.strip(), self.auth_token)

    def _drain_request_body(self) -> None:
        """Bounded-time discard of any unread request body, so the socket can
        close cleanly (on Windows an unread-body close can RST and abort the
        client's read of our error response — WinError 10053) WITHOUT letting
        a client that declares Content-Length but sends nothing pin this
        thread forever: the drain gives up after ``drain_timeout`` seconds."""
        raw = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw)
        except ValueError:
            return
        if length <= 0:
            return
        remaining = min(length, self.max_body_bytes)
        deadline = time.monotonic() + self.drain_timeout
        sock = self.connection
        old_timeout = sock.gettimeout()
        try:
            # bound EACH read attempt to the drain budget: rfile.read(n)
            # blocks until n bytes or EOF, so the socket timeout is what
            # actually bounds a client that sends nothing.
            sock.settimeout(self.drain_timeout)
            while remaining > 0 and time.monotonic() < deadline:
                try:
                    chunk = self.rfile.read(min(65536, remaining))
                except OSError:
                    return
                if not chunk:
                    return
                remaining -= len(chunk)
        finally:
            try:
                sock.settimeout(old_timeout)
            except OSError:  # pragma: no cover
                pass

    def _reject_unauthorized(self) -> None:
        # Connection: close — the request body was never read; keep-alive
        # would try to parse it as the next request and abort the socket.
        self._drain_request_body()
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Bearer realm="statebar-mcp"')
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _reject_body_too_large(self) -> None:
        self._drain_request_body()
        body = json.dumps({"error": "request body too large"}).encode("utf-8")
        self.send_response(413)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _read_json_body(self) -> Dict[str, Any]:
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_length)
        except ValueError:
            self._send_json(400, {"error": "invalid Content-Length"})
            raise _BodyRejected
        if length < 0:
            self._send_json(400, {"error": "invalid Content-Length"})
            raise _BodyRejected
        if length > self.max_body_bytes:
            self._reject_body_too_large()
            raise _BodyRejected
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _query_params(self) -> Dict[str, str]:
        parsed = urllib.parse.urlparse(self.path)
        return {
            k: v[0] if isinstance(v, list) and v else v
            for k, v in urllib.parse.parse_qs(parsed.query).items()
        }

    # -- routes -------------------------------------------------------------------

    def do_GET(self) -> None:
        if not self._authorized():
            self._reject_unauthorized()
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = self._query_params()
        try:
            if path == "/v1/health":
                self._send_json(200, self.contract.health(params))
            elif path == "/v1/snapshot":
                self._send_json(200, self.contract.snapshot(params))
            elif path == "/v1/context-candidates":
                self._send_json(200, self.contract.context_candidates(params))
            elif path == "/v1/state":
                self._send_json(200, self.contract.get_state(params))
            else:
                self._send_json(404, {"error": f"not found: {path}"})
        except ContractError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover
            logger.exception("GET %s failed", path)
            self._send_json(500, {"error": str(exc)})

    def do_POST(self) -> None:
        if not self._authorized():
            self._reject_unauthorized()
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/v1/observe":
                body = self._read_json_body()
                self._send_json(200, self.contract.observe(body))
            else:
                self._send_json(404, {"error": f"not found: {path}"})
        except _BodyRejected:
            return  # response already sent
        except ContractError as exc:
            self._send_json(400, {"error": str(exc)})
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {"error": f"invalid JSON: {exc}"})
        except Exception as exc:  # pragma: no cover
            logger.exception("POST %s failed", path)
            self._send_json(500, {"error": str(exc)})


class _BodyRejected(Exception):
    """Internal: body was too large/invalid and a response was already sent."""


def create_server(
    service: UserStateService,
    host: str = "127.0.0.1",
    port: int = 8765,
    auth_token: str = "",
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> ThreadingHTTPServer:
    # Fail-closed invariant lives HERE, not just in the CLI: any library
    # caller binding a non-loopback address without a token must be refused.
    if not _is_loopback(host) and not auth_token:
        raise ValueError(
            f"refusing to bind non-loopback host {host!r} without an auth "
            "token: user state must not be exposed on the network "
            "unauthenticated (set auth_token, or bind 127.0.0.1)"
        )
    handler = type(
        "_ContractHandler",
        (_Handler,),
        {
            "contract": Contract(service),
            "auth_token": auth_token,
            "max_body_bytes": max_body_bytes,
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def run_server(
    service: UserStateService,
    host: str = "127.0.0.1",
    port: int = 8765,
    auth_token: str = "",
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> None:
    server = create_server(
        service, host, port, auth_token=auth_token, max_body_bytes=max_body_bytes
    )
    if auth_token:
        logger.info(
            "statebar-mcp REST serving on http://%s:%d (Bearer auth REQUIRED)", host, port
        )
    else:
        logger.info(
            "statebar-mcp REST serving on http://%s:%d (no auth — loopback only)", host, port
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
