"""Acceptance tests D1-D12 (派单总纲 §15).

Each test maps 1:1 to a frozen acceptance scenario. D1-D4 exercise the live
REST transport (as phrased in the spec); D5-D12 run at the service level with
an injectable clock and a deterministic mock persistent extractor, so the
semantic-window scenarios are exact instead of wall-clock dependent.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dsh_user_state.config import Config, ExtractorConfig  # noqa: E402
from dsh_user_state.core.extractor.persistent import (  # noqa: E402
    MockExtractor,
    PersistentExtractor,
)
from dsh_user_state.core.models import (  # noqa: E402
    Observation,
    ObserveRequest,
    Source,
    SourceType,
    StateStatus,
)
from dsh_user_state.core.service import UserStateService  # noqa: E402
from dsh_user_state.core.store import SQLiteStore  # noqa: E402
from dsh_user_state.transports.rest import create_server  # noqa: E402

LOCAL_TZ = datetime.now().astimezone().tzinfo


def local_dt(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=LOCAL_TZ)


class FixedClock:
    def __init__(self, start: datetime):
        self.now_dt = start
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self.now_dt

    def set(self, dt: datetime) -> None:
        with self._lock:
            self.now_dt = dt


class BlockedExtractor(PersistentExtractor):
    """D3: a persistent extractor that is 'configured' but always fails."""

    name = "blocked"

    def available(self) -> bool:
        return True

    def extract(self, request):
        raise RuntimeError("extractor backend unreachable")


def make_service(clock=None, extractor=None, db=":memory:"):
    store = SQLiteStore(db)
    service = UserStateService(
        store,
        persistent_extractor=extractor if extractor is not None else MockExtractor(),
        now_fn=clock or (lambda: datetime.now(timezone.utc)),
    )
    return service


def wait_event_complete(store, subject_id, event_id, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ev = store.get_event(subject_id, event_id)
        if ev and ev["status"] == "complete":
            return True
        time.sleep(0.02)
    return False


def observe(service, subject_id, event_id, text, observed_at, platform="cli", source_type="conversation"):
    req = ObserveRequest(
        subject_id=subject_id,
        event_id=event_id,
        text=text,
        source=Source(type=source_type, platform=platform),
        observed_at=observed_at,
    )
    return service.observe(req)


# ---------------------------------------------------------------------------
# D1-D4 : live REST transport
# ---------------------------------------------------------------------------


class TestRESTAcceptance:
    @pytest.fixture()
    def server(self):
        service = make_service()
        srv = create_server(service, "127.0.0.1", 0)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        yield service, port
        srv.shutdown()
        srv.server_close()
        service.close()

    def _post(self, port, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _get(self, port, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_D1_observe_accepted_fast_overlay(self, server):
        service, port = server
        now = datetime.now(timezone.utc)
        code, body = self._post(
            port,
            "/v1/observe",
            {
                "subject_id": "user-001",
                "event_id": "e1",
                "text": "我刚睡醒",
                "observed_at": now.isoformat(),
                "source": {"type": "conversation", "platform": "cli"},
            },
        )
        assert code == 200
        assert body["status"] == "accepted"
        types = [o["type"] for o in body["sync_observations"]]
        assert "awake" in types  # Fast Overlay fired synchronously

    def test_D2_snapshot_has_awake_immediately(self, server):
        service, port = server
        now = datetime.now(timezone.utc)
        self._post(
            port,
            "/v1/observe",
            {
                "subject_id": "user-001",
                "event_id": "e1",
                "text": "我刚睡醒",
                "observed_at": now.isoformat(),
            },
        )
        code, snap = self._get(port, "/v1/snapshot?subject_id=user-001")
        assert code == 200
        assert "awake" in snap["text"]  # no waiting on async LLM

    def test_D3_persistent_blocked_snapshot_still_correct(self):
        service = make_service(extractor=BlockedExtractor())
        srv = create_server(service, "127.0.0.1", 0)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            now = datetime.now(timezone.utc)
            self._post(
                port,
                "/v1/observe",
                {
                    "subject_id": "user-001",
                    "event_id": "e1",
                    "text": "我刚睡醒",
                    "observed_at": now.isoformat(),
                },
            )
            code, snap = self._get(port, "/v1/snapshot?subject_id=user-001")
            assert code == 200 and "awake" in snap["text"]
            code2, health = self._get(port, "/v1/health")
            assert code2 == 200 and health["status"] == "ok"
        finally:
            srv.shutdown()
            srv.server_close()
            service.close()

    def test_D4_event_idempotency(self, server):
        service, port = server
        now = datetime.now(timezone.utc)
        payload = {
            "subject_id": "user-001",
            "event_id": "e1",
            "text": "我刚睡醒",
            "observed_at": now.isoformat(),
        }
        code, first = self._post(port, "/v1/observe", payload)
        assert first["status"] == "accepted"
        before = service.store.counts()
        code, second = self._post(port, "/v1/observe", payload)
        assert second["status"] == "duplicate"
        after = service.store.counts()
        assert after == before  # no duplicate observation/state


# ---------------------------------------------------------------------------
# D5-D12 : service level with controlled clock
# ---------------------------------------------------------------------------


class TestServiceAcceptance:
    def test_D5_tentative_plan_window(self):
        clock = FixedClock(local_dt(2026, 8, 16, 8, 0).astimezone(timezone.utc))
        service = make_service(clock=clock)
        try:
            observe(service, "user-001", "e5", "下午可能去写书法",
                    clock().astimezone(LOCAL_TZ).astimezone(timezone.utc))
            assert wait_event_complete(service.store, "user-001", "e5")
            states = service.get_state("user-001", category="planning")
            assert len(states) == 1
            s = states[0]
            assert s["key"] == "calligraphy"
            assert s["certainty"] == "tentative"
            assert s["status"] == "tentative"
            # window = 13:00-17:00 local
            until = datetime.fromisoformat(s["valid_until"]).astimezone(LOCAL_TZ)
            assert (until.hour, until.minute) == (17, 0)
            assert until.date() == local_dt(2026, 8, 16, 8, 0).date()
        finally:
            service.close()

    def test_D6_cancel_plan(self):
        clock = FixedClock(local_dt(2026, 8, 16, 8, 0).astimezone(timezone.utc))
        service = make_service(clock=clock)
        try:
            observe(service, "user-001", "e5", "下午可能去写书法", clock())
            assert wait_event_complete(service.store, "user-001", "e5")
            res = observe(service, "user-001", "e6", "算了，不去了", clock())
            cancel_types = [o["type"] for o in res["sync_observations"]]
            assert "cancel" in cancel_types
            calligraphy = service.store.get_state("user-001", "planning", "calligraphy")
            assert calligraphy is not None and calligraphy.status == "cancelled"
            snap = service.get_snapshot("user-001")
            assert "calligraphy plan: cancelled" in snap.text
        finally:
            service.close()

    def test_D7_symptom_unresolved_followup(self):
        service = make_service()
        try:
            now = datetime.now(timezone.utc)
            observe(service, "user-001", "e7", "胃有点疼", now)
            states = service.get_state("user-001", category="health")
            stomach = [s for s in states if s["key"] == "stomach_pain"]
            assert stomach and stomach[0]["status"] == "active"
            assert stomach[0]["followup_relevant"] is True
            cands = service.get_context_candidates("user-001")
            assert any(c["key"] == "stomach_pain" for c in cands["unresolved"])
            assert any(c["key"] == "stomach_pain" for c in cands["followup"])
        finally:
            service.close()

    def test_D8_resolve_and_medication(self):
        clock = FixedClock(local_dt(2026, 8, 16, 9, 0).astimezone(timezone.utc))
        service = make_service(clock=clock)
        try:
            observe(service, "user-001", "e7", "胃有点疼", clock())
            res = observe(service, "user-001", "e8", "好多了，吃了点药", clock())
            types = [o["type"] for o in res["sync_observations"]]
            assert "resolve" in types and "activity" in types
            states = service.get_state("user-001", category="health")
            stomach = [s for s in states if s["key"] == "stomach_pain"]
            assert stomach[0]["status"] in ("improving", "resolved")
            medication = service.store.get_state("user-001", "health", "medication")
            assert medication is not None and medication.status == "completed"
            snap = service.get_snapshot("user-001")
            assert "medication taken recently" in snap.text
        finally:
            service.close()

    def test_D9_assistant_question_no_canonical_state(self):
        service = make_service()
        try:
            now = datetime.now(timezone.utc)
            observe(
                service, "user-001", "e9",
                "你今天是不是刚睡醒？", now,
                source_type=SourceType.ASSISTANT_QUESTION,
            )
            states = service.get_state("user-001")
            # S1: the overlay may have matched, but no canonical state exists
            assert all(s["key"] != "awake" for s in states)
            obs = service.store.get_observations("user-001")
            assert len(obs) >= 1  # evidence kept, state rejected
            assert obs[0].source.type == SourceType.ASSISTANT_QUESTION
        finally:
            service.close()

    def test_D10_lazy_expiration_after_window(self):
        start = local_dt(2026, 8, 16, 8, 0).astimezone(timezone.utc)
        clock = FixedClock(start)
        service = make_service(clock=clock)
        try:
            observe(service, "user-001", "e5", "下午可能去写书法", clock())
            assert wait_event_complete(service.store, "user-001", "e5")
            # 17:01 — the afternoon window (13:00-17:00) has passed
            clock.set(local_dt(2026, 8, 16, 17, 1).astimezone(timezone.utc))
            snap = service.get_snapshot("user-001")
            assert "calligraphy" not in snap.text  # excluded, not pending
            states = service.store.get_states("user-001")
            calligraphy = [s for s in states if s.key == "calligraphy"]
            assert calligraphy[0].status == StateStatus.EXPIRED
            cands = service.get_context_candidates("user-001")
            assert cands["planned"] == []
        finally:
            service.close()

    def test_D11_cross_platform_shared_scope(self):
        clock = FixedClock(local_dt(2026, 8, 16, 10, 0).astimezone(timezone.utc))
        service = make_service(clock=clock)
        try:
            observe(service, "user-001", "e11a", "胃有点疼", clock(), platform="weixin")
            observe(service, "user-001", "e11b", "在忙什么呢", clock(), platform="telegram")
            states = service.get_state("user-001", category="health")
            assert any(s["key"] == "stomach_pain" and s["status"] == "active" for s in states)
            snap = service.get_snapshot("user-001")
            assert "stomach discomfort" in snap.text
        finally:
            service.close()

    def test_D12_delayed_message_no_rollback(self):
        clock = FixedClock(local_dt(2026, 8, 16, 15, 3).astimezone(timezone.utc))
        service = make_service(clock=clock)
        try:
            # 15:03 Telegram: "我现在去吃饭" → activity meal completed
            service.reconcile(
                Observation(
                    subject_id="user-001",
                    event_id="e12a",
                    type="activity",
                    category="activity",
                    key="meal",
                    value="completed",
                    certainty="confirmed",
                    time_expression="now",
                    source=Source(type="conversation", platform="telegram"),
                    observed_at=clock(),
                    raw_payload="我现在去吃饭",
                )
            )
            meal = service.store.get_state("user-001", "activity", "meal")
            assert meal is not None and meal.status == "completed"

            # delayed WeChat message, semantic time 14:55, arrives at 15:30
            clock.set(local_dt(2026, 8, 16, 15, 30).astimezone(timezone.utc))
            service.reconcile(
                Observation(
                    subject_id="user-001",
                    event_id="e12b",
                    type="plan",
                    category="planning",
                    key="meal",
                    value="go eat",
                    certainty="tentative",
                    time_expression="afternoon",
                    source=Source(type="conversation", platform="weixin"),
                    observed_at=local_dt(2026, 8, 16, 14, 55).astimezone(timezone.utc),
                    raw_payload="我等下去吃饭",
                )
            )
            # state must NOT roll back to "还没吃" (no tentative meal plan)
            plans = service.get_state("user-001", category="planning")
            assert plans == []
            meal = service.store.get_state("user-001", "activity", "meal")
            assert meal.status == "completed"
        finally:
            service.close()

    def test_D12b_delayed_mutation_no_downgrade(self):
        clock = FixedClock(local_dt(2026, 8, 16, 4, 0).astimezone(timezone.utc))
        service = make_service(clock=clock)
        try:
            service.reconcile(
                Observation(
                    subject_id="user-001", event_id="a1", type="awake",
                    category="sleep", key="awake", value="awake",
                    source=Source(type="conversation"),
                    observed_at=local_dt(2026, 8, 16, 4, 0).astimezone(timezone.utc),
                    raw_payload="我刚睡醒",
                )
            )
            clock.set(local_dt(2026, 8, 16, 4, 30).astimezone(timezone.utc))
            service.reconcile(
                Observation(
                    subject_id="user-001", event_id="a2", type="sleep",
                    category="sleep", key="sleeping", value="sleeping",
                    source=Source(type="conversation"),
                    observed_at=local_dt(2026, 8, 16, 4, 30).astimezone(timezone.utc),
                    raw_payload="我睡了",
                )
            )
            sleeping = service.store.get_state("user-001", "sleep", "sleeping")
            assert sleeping.status == "active"
            # delayed duplicate "我刚睡醒" from 03:50 arrives late — must not
            # supersede the sleeping state written from newer evidence
            clock.set(local_dt(2026, 8, 16, 5, 0).astimezone(timezone.utc))
            service.reconcile(
                Observation(
                    subject_id="user-001", event_id="a3", type="awake",
                    category="sleep", key="awake", value="awake",
                    source=Source(type="conversation"),
                    observed_at=local_dt(2026, 8, 16, 3, 50).astimezone(timezone.utc),
                    raw_payload="我刚睡醒",
                )
            )
            sleeping = service.store.get_state("user-001", "sleep", "sleeping")
            assert sleeping.status == "active"  # no rollback
        finally:
            service.close()
