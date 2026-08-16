"""REST transport error-path and CLI smoke tests."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from statebar_mcp.core.service import UserStateService  # noqa: E402
from statebar_mcp.core.store import SQLiteStore  # noqa: E402
from statebar_mcp.transports.rest import create_server  # noqa: E402


@pytest.fixture()
def server():
    service = UserStateService(SQLiteStore(":memory:"))
    srv = create_server(service, "127.0.0.1", 0)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield port
    srv.shutdown()
    srv.server_close()
    service.close()


def _get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _post(port, path, payload, raw=False):
    data = payload if raw else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class TestRESTErrors:
    def test_404_unknown_path(self, server):
        code, body = _get(server, "/nope")
        assert code == 404

    def test_400_invalid_json(self, server):
        code, body = _post(server, "/v1/observe", b"{not json", raw=True)
        assert code == 400

    def test_400_missing_subject(self, server):
        code, body = _get(server, "/v1/snapshot")
        assert code == 400
        assert "subject_id" in body["error"]

    def test_observe_requires_event_fields(self, server):
        code, body = _post(server, "/v1/observe", {"subject_id": "u"})
        assert code in (400, 500)
        assert "error" in body


class TestCLI:
    def test_cli_health(self, tmp_path):
        import os

        env = {**os.environ, "PYTHONPATH": "", "DSH_USER_STATE_DB": str(tmp_path / "cli.db")}
        out = subprocess.run(
            [sys.executable, "-m", "statebar_mcp", "health"],
            capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=30,
        )
        assert out.returncode == 0, out.stderr
        payload = json.loads(out.stdout)
        assert payload["status"] == "ok"
        assert payload["service"] == "statebar-mcp"

    def test_cli_serve_and_observe(self, tmp_path):
        import os
        import socket
        import time

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = {
            **os.environ,
            "PYTHONPATH": "",
            "DSH_USER_STATE_DB": str(tmp_path / "serve.db"),
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "statebar_mcp", "serve", "--port", str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, cwd=str(ROOT),
        )
        try:
            deadline = time.monotonic() + 15
            ok = False
            while time.monotonic() < deadline:
                try:
                    _get(port, "/v1/health")
                    ok = True
                    break
                except Exception:
                    time.sleep(0.2)
            assert ok, "server did not come up"
            code, body = _post(
                port,
                "/v1/observe",
                {"subject_id": "cli-001", "event_id": "c1", "text": "我刚睡醒"},
            )
            assert code == 200 and body["status"] == "accepted"
            code, snap = _get(port, "/v1/snapshot?subject_id=cli-001")
            assert "awake" in snap["text"]
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
