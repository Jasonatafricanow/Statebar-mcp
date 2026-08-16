"""REST transport (``dsh-user-state serve``).

Stdlib-only HTTP server exposing the frozen Contract:

    POST /v1/observe             {"subject_id","event_id","text","observed_at","source"}
    GET  /v1/snapshot            ?subject_id=&query=
    GET  /v1/context-candidates  ?subject_id=
    GET  /v1/state               ?subject_id=&category=&key=
    GET  /v1/health
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from ..core.service import UserStateService
from .contract import Contract, ContractError

logger = logging.getLogger(__name__)


class _Handler(BaseHTTPRequestHandler):
    server_version = "dsh-user-state/0.1"
    contract: Contract = None  # set by factory

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

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
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
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/v1/observe":
                body = self._read_json_body()
                self._send_json(200, self.contract.observe(body))
            else:
                self._send_json(404, {"error": f"not found: {path}"})
        except ContractError as exc:
            self._send_json(400, {"error": str(exc)})
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {"error": f"invalid JSON: {exc}"})
        except Exception as exc:  # pragma: no cover
            logger.exception("POST %s failed", path)
            self._send_json(500, {"error": str(exc)})


def create_server(
    service: UserStateService, host: str = "127.0.0.1", port: int = 8765
) -> ThreadingHTTPServer:
    handler = type(
        "_ContractHandler", (_Handler,), {"contract": Contract(service)}
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def run_server(service: UserStateService, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = create_server(service, host, port)
    logger.info("dsh-user-state REST serving on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
