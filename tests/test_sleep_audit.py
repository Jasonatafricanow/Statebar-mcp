"""Manual correction of sleep episodes from the audit console: supersede / restore over REST,
append-only audit, and the corrected daily total going back to the health plugin."""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from statebar_mcp.core.health_sync import HealthSync  # noqa: E402
from statebar_mcp.core.service import UserStateService  # noqa: E402
from statebar_mcp.core.store import SQLiteStore  # noqa: E402
from statebar_mcp.transports.rest import create_server  # noqa: E402

CN = timezone(timedelta(hours=8))
SUBJECT = "xiyue-user"


def iso(s: str) -> str:
    return datetime.fromisoformat(s).replace(tzinfo=CN).isoformat()


@pytest.fixture
def api():
    posted = []
    sync = HealthSync(url="http://health.test/api/health-webhook", token="t", subject_id=SUBJECT,
                      post=lambda url, token, body: posted.append(body) or {"ok": True})
    service = UserStateService(SQLiteStore(":memory:"), health_sync=sync)
    # one day with a night sleep and a nap, one day with a single sleep
    service.import_sleep_history(SUBJECT, "seed", [
        {"wake_date": "2026-11-18", "start": iso("2026-11-18T01:00"), "end": iso("2026-11-18T07:00"), "basis": "原话"},
        {"wake_date": "2026-11-18", "start": iso("2026-11-18T14:00"), "end": iso("2026-11-18T15:00"), "basis": "推理"},
        {"wake_date": "2026-11-19", "start": iso("2026-11-19T00:00"), "end": iso("2026-11-19T08:00"), "basis": "台账"},
    ])
    posted.clear()
    srv = create_server(service, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def call(method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    yield call, service, posted
    srv.shutdown()
    srv.server_close()
    service.close()


def episode(service, wake_date, start_hhmm):
    return next(e for e in service.get_sleep(SUBJECT)["episodes"]
                if e["wake_date"] == wake_date and e["start_local"].endswith(start_hhmm))


def test_supersede_drops_the_day_total_and_restore_brings_it_back(api):
    call, service, posted = api
    nap = episode(service, "2026-11-18", "14:00")
    code, r = call("POST", "/v1/sleep/supersede", {"subject_id": SUBJECT, "episode_id": nap["episode_id"],
                                                    "reason": "那是躺着没睡着", "actor": "pi-web"})
    assert code == 200 and r["changed"] and r["episode"]["status"] == "superseded"
    assert r["episode"]["superseded_by"] == "manual:那是躺着没睡着"
    assert service.get_sleep(SUBJECT)["daily_minutes"]["2026-11-18"] == 360
    assert [p["sleepHours"] for p in posted] == [6.0]  # the corrected total reached the plugin

    code, r = call("POST", "/v1/sleep/restore", {"subject_id": SUBJECT, "episode_id": nap["episode_id"],
                                                  "reason": "确实睡着了", "actor": "pi-web"})
    assert code == 200 and r["changed"] and r["episode"]["status"] == "active"
    assert r["episode"]["superseded_by"] == "" and r["overlaps_active"] == []
    assert service.get_sleep(SUBJECT)["daily_minutes"]["2026-11-18"] == 420
    assert [p["sleepHours"] for p in posted] == [6.0, 7.0]


def test_every_change_is_audited_and_repeats_are_no_ops(api):
    call, service, posted = api
    nap = episode(service, "2026-11-18", "14:00")
    body = {"subject_id": SUBJECT, "episode_id": nap["episode_id"], "reason": "误记", "actor": "pi-web"}
    call("POST", "/v1/sleep/supersede", body)
    code, again = call("POST", "/v1/sleep/supersede", body)
    assert code == 200 and again["changed"] is False and again["audit_id"] is None
    call("POST", "/v1/sleep/restore", body)
    call("POST", "/v1/sleep/restore", body)
    assert len(posted) == 2

    code, a = call("GET", f"/v1/sleep/audit?subject_id={SUBJECT}")
    assert code == 200
    rows = [(x["action"], x["before_status"], x["after_status"], x["after_superseded_by"], x["actor"], x["reason"])
            for x in a["audit"]]
    assert rows == [("restore", "superseded", "active", "", "pi-web", "误记"),
                    ("supersede", "active", "superseded", "manual:误记", "pi-web", "误记")]
    assert all(x["at"] and x["wake_date"] == "2026-11-18" for x in a["audit"])


def test_the_audit_table_cannot_be_rewritten(api):
    _, service, _ = api
    nap = episode(service, "2026-11-18", "14:00")
    service.supersede_sleep(SUBJECT, nap["episode_id"], "误记")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        service.store._conn.execute("DELETE FROM sleep_audit")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        service.store._conn.execute("UPDATE sleep_audit SET reason='x'")


def test_a_day_left_empty_is_reported_not_sent_as_zero(api):
    call, service, posted = api
    only = episode(service, "2026-11-19", "00:00")
    code, r = call("POST", "/v1/sleep/supersede", {"subject_id": SUBJECT, "episode_id": only["episode_id"],
                                                    "reason": "重复导入"})
    assert code == 200 and r["changed"] and r["daily_minutes"] == 0
    assert r["health_queued"] is False and r["health_note"] and posted == []


def test_refusals(api):
    call, service, _ = api
    nap = episode(service, "2026-11-18", "14:00")
    code, r = call("POST", "/v1/sleep/supersede", {"subject_id": SUBJECT, "episode_id": nap["episode_id"], "reason": " "})
    assert code == 400 and "reason" in r["error"]
    code, _ = call("POST", "/v1/sleep/supersede", {"subject_id": "kid", "episode_id": nap["episode_id"], "reason": "x"})
    assert code == 400  # another subject's episode
    code, _ = call("POST", "/v1/sleep/restore", {"subject_id": SUBJECT, "episode_id": "nope"})
    assert code == 400
    assert service.get_sleep_audit(SUBJECT)["audit"] == []
