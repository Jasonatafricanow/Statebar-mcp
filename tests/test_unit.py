"""Unit tests: Fast Overlay rules, semantic lifecycle, reconciler ruleset
(R1-R10/S1/S2), store idempotency, snapshot builder."""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from statebar_mcp.core.extractor.fast_overlay import FastOverlayExtractor  # noqa: E402
from statebar_mcp.core.lifecycle import lazy_expire, semantic_window  # noqa: E402
from statebar_mcp.core.models import (  # noqa: E402
    Certainty,
    Observation,
    Source,
    SourceType,
    State,
    StateStatus,
    TimeExpr,
)
from statebar_mcp.core.reconciler import Reconciler  # noqa: E402
from statebar_mcp.core.service import UserStateService  # noqa: E402
from statebar_mcp.core.snapshot import SnapshotBuilder  # noqa: E402
from statebar_mcp.core.store import SQLiteStore  # noqa: E402

LOCAL_TZ = datetime.now().astimezone().tzinfo


def local_dt(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=LOCAL_TZ)


class FixedClock:
    def __init__(self, start):
        self.now_dt = start

    def __call__(self):
        return self.now_dt

    def set(self, dt):
        self.now_dt = dt


def extract(text, now=None, source_type="conversation"):
    return FastOverlayExtractor().extract(
        "user-001", "evt-1", text, Source(type=source_type), now or datetime.now(timezone.utc)
    )


# ---------------------------------------------------------------------------
# Fast Overlay
# ---------------------------------------------------------------------------


class TestFastOverlay:
    @pytest.mark.parametrize("text", ["我刚睡醒", "我刚醒", "睡醒了", "刚起床", "刚醒过来"])
    def test_awake(self, text):
        obs = extract(text)
        assert any(o.type == "awake" for o in obs), text

    def test_awake_wins_over_embedded_sleep(self):
        obs = extract("我刚睡醒了")
        types = [o.type for o in obs]
        assert "awake" in types
        assert "sleep" not in types  # "睡了" inside "睡醒了" must not fire

    @pytest.mark.parametrize("text,value", [
        ("准备睡了", "preparing_sleep"),
        ("我要睡了", "preparing_sleep"),
        ("我去睡了", "preparing_sleep"),
        ("我睡了", "sleeping"),
    ])
    def test_sleep(self, text, value):
        obs = extract(text)
        assert any(o.type == "sleep" and o.value == value for o in obs), text

    def test_cancel(self):
        obs = extract("算了，不去了")
        assert any(o.type == "cancel" for o in obs)

    def test_symptom_active(self):
        obs = extract("胃有点疼")
        assert any(o.type == "symptom" and o.key == "stomach_pain" for o in obs)

    def test_symptom_resolved_with_body(self):
        obs = extract("胃不疼了")
        assert any(o.type == "resolve" and o.key == "stomach_pain" and o.value == "resolved" for o in obs)

    def test_resolve_improving_and_medication_cofire(self):
        obs = extract("好多了，吃了点药")
        assert any(o.type == "resolve" and o.value == "improving" for o in obs)
        assert any(o.type == "activity" and o.key == "medication" for o in obs)
        indexes = [o.observation_index for o in obs]
        assert indexes == sorted(indexes)

    def test_no_false_positive_on_plain_chat(self):
        assert extract("今天天气不错") == []
        assert extract("在忙什么呢") == []


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_afternoon_window(self):
        now = local_dt(2026, 8, 16, 8, 0)
        vf, vu, ru = semantic_window("afternoon", now, now)
        local_until = vu.astimezone(LOCAL_TZ)
        assert (local_until.hour, local_until.minute) == (17, 0)
        assert local_until.date() == now.date()

    def test_tonight_window(self):
        now = local_dt(2026, 8, 16, 8, 0)
        _, vu, _ = semantic_window("tonight", now, now)
        assert vu.astimezone(LOCAL_TZ).hour == 23

    def test_ttl_fallback(self):
        now = local_dt(2026, 8, 16, 8, 0)
        _, vu, _ = semantic_window("unspecified", now, now)
        assert vu == now + timedelta(hours=24)

    def test_lazy_expire(self):
        now = local_dt(2026, 8, 16, 8, 0)
        plan = State(
            subject_id="u", category="planning", key="calligraphy",
            status=StateStatus.TENTATIVE,
            valid_until=local_dt(2026, 8, 16, 17, 0),
            last_observed_at=now,
        )
        active = State(
            subject_id="u", category="sleep", key="awake",
            status=StateStatus.ACTIVE, valid_until=now + timedelta(hours=16),
            last_observed_at=now,
        )
        transitions, mutated = lazy_expire(
            [plan, active], local_dt(2026, 8, 16, 17, 1)
        )
        assert plan.status == StateStatus.EXPIRED
        assert active.status == StateStatus.ACTIVE
        assert len(transitions) == 1


# ---------------------------------------------------------------------------
# Reconciler ruleset
# ---------------------------------------------------------------------------


def make_rec(clock):
    store = SQLiteStore(":memory:")
    return Reconciler(store, now_fn=clock), store


_OBS_SEQ = 0


def obs(type_, key, value="", category="", certainty=Certainty.CONFIRMED,
        time_expr=TimeExpr.UNSPECIFIED, observed=None, source_type="conversation"):
    # each observation is its own (event_id, observation_index) pair — in
    # production the pair is UNIQUE, so the replay-idempotency key derived
    # from it must be unique per observation here too
    global _OBS_SEQ
    _OBS_SEQ += 1
    return Observation(
        subject_id="user-001", event_id=f"e{_OBS_SEQ}", type=type_,
        category=category or type_,
        key=key, value=value, certainty=certainty, time_expression=time_expr,
        source=Source(type=source_type),
        observed_at=observed or datetime.now(timezone.utc),
        raw_payload="x",
    )


class TestReconciler:
    def test_R1_awake_supersedes_sleep(self):
        clock = FixedClock(local_dt(2026, 8, 16, 4, 0).astimezone(timezone.utc))
        rec, store = make_rec(clock)
        rec.apply(obs("sleep", "sleeping", "sleeping", observed=clock()))
        clock.set(local_dt(2026, 8, 16, 6, 0).astimezone(timezone.utc))
        rec.apply(obs("awake", "awake", observed=clock()))
        sleeping = store.get_state("user-001", "sleep", "sleeping")
        awake = store.get_state("user-001", "sleep", "awake")
        assert sleeping.status == StateStatus.SUPERSEDED
        assert awake.status == StateStatus.ACTIVE

    def test_R2_sleep_keeps_wake_history(self):
        clock = FixedClock(local_dt(2026, 8, 16, 4, 0).astimezone(timezone.utc))
        rec, store = make_rec(clock)
        rec.apply(obs("awake", "awake", observed=clock()))
        original_valid_from = store.get_state("user-001", "sleep", "awake").valid_from
        clock.set(local_dt(2026, 8, 16, 23, 0).astimezone(timezone.utc))
        rec.apply(obs("sleep", "sleeping", "sleeping", observed=clock()))
        awake = store.get_state("user-001", "sleep", "awake")
        sleeping = store.get_state("user-001", "sleep", "sleeping")
        assert awake.status == StateStatus.SUPERSEDED
        assert awake.valid_from == original_valid_from  # history not rewritten
        assert sleeping.status == StateStatus.ACTIVE

    def test_R3_cancel_plan(self):
        clock = FixedClock(local_dt(2026, 8, 16, 8, 0).astimezone(timezone.utc))
        rec, store = make_rec(clock)
        rec.apply(obs("plan", "calligraphy", certainty=Certainty.TENTATIVE,
                      time_expr=TimeExpr.AFTERNOON, observed=clock()))
        clock.set(local_dt(2026, 8, 16, 10, 0).astimezone(timezone.utc))
        rec.apply(obs("cancel", "", observed=clock()))
        plan = store.get_state("user-001", "planning", "calligraphy")
        assert plan.status == StateStatus.CANCELLED

    def test_R4_plan_completed_by_activity(self):
        clock = FixedClock(local_dt(2026, 8, 16, 8, 0).astimezone(timezone.utc))
        rec, store = make_rec(clock)
        rec.apply(obs("plan", "swimming", certainty=Certainty.TENTATIVE,
                      time_expr=TimeExpr.TONIGHT, observed=clock()))
        clock.set(local_dt(2026, 8, 16, 18, 0).astimezone(timezone.utc))
        rec.apply(obs("activity", "swimming", "completed", observed=clock()))
        plan = store.get_state("user-001", "planning", "swimming")
        assert plan.status == StateStatus.COMPLETED

    def test_R4_activity_recent_state(self):
        clock = FixedClock(local_dt(2026, 8, 16, 18, 0).astimezone(timezone.utc))
        rec, store = make_rec(clock)
        rec.apply(obs("activity", "swimming", "completed", observed=clock()))
        state = store.get_state("user-001", "activity", "swimming")
        assert state.status == StateStatus.COMPLETED
        # relevant until end of the local day
        assert state.relevant_until.astimezone(LOCAL_TZ).hour == 23

    def test_R5_tentative_confirmed_to_planned(self):
        clock = FixedClock(local_dt(2026, 8, 16, 8, 0).astimezone(timezone.utc))
        rec, store = make_rec(clock)
        rec.apply(obs("plan", "calligraphy", certainty=Certainty.TENTATIVE,
                      time_expr=TimeExpr.AFTERNOON, observed=clock()))
        clock.set(local_dt(2026, 8, 16, 9, 0).astimezone(timezone.utc))
        rec.apply(obs("plan", "calligraphy", certainty=Certainty.PLANNED,
                      time_expr=TimeExpr.AFTERNOON, observed=clock()))
        plan = store.get_state("user-001", "planning", "calligraphy")
        assert plan.status == StateStatus.PLANNED
        assert plan.certainty == Certainty.PLANNED

    def test_R6_reschedule_new_window(self):
        clock = FixedClock(local_dt(2026, 8, 16, 8, 0).astimezone(timezone.utc))
        rec, store = make_rec(clock)
        rec.apply(obs("plan", "calligraphy", certainty=Certainty.TENTATIVE,
                      time_expr=TimeExpr.AFTERNOON, observed=clock()))
        clock.set(local_dt(2026, 8, 16, 16, 50).astimezone(timezone.utc))
        rec.apply(obs("plan", "calligraphy", certainty=Certainty.PLANNED,
                      time_expr=TimeExpr.TONIGHT, observed=clock()))
        # old window superseded, new tonight window effective
        plans = [s for s in store.get_states("user-001") if s.key == "calligraphy"]
        statuses = {s.status for s in plans}
        assert StateStatus.SUPERSEDED in statuses
        current = [s for s in plans if s.status == StateStatus.PLANNED]
        assert len(current) == 1
        assert current[0].valid_until.astimezone(LOCAL_TZ).hour == 23

    def test_R7_improving(self):
        clock = FixedClock(local_dt(2026, 8, 16, 9, 0).astimezone(timezone.utc))
        rec, store = make_rec(clock)
        rec.apply(obs("symptom", "stomach_pain", "active", category="health", observed=clock()))
        clock.set(local_dt(2026, 8, 16, 10, 0).astimezone(timezone.utc))
        rec.apply(obs("resolve", "", "improving", category="health", observed=clock()))
        state = store.get_state("user-001", "health", "stomach_pain")
        assert state.status == StateStatus.IMPROVING

    def test_R8_resolved(self):
        clock = FixedClock(local_dt(2026, 8, 16, 9, 0).astimezone(timezone.utc))
        rec, store = make_rec(clock)
        rec.apply(obs("symptom", "stomach_pain", "active", category="health", observed=clock()))
        clock.set(local_dt(2026, 8, 16, 12, 0).astimezone(timezone.utc))
        rec.apply(obs("resolve", "stomach_pain", "resolved", category="health", observed=clock()))
        state = store.get_state("user-001", "health", "stomach_pain")
        assert state.status == StateStatus.RESOLVED
        assert state.followup_relevant is False

    def test_R9_user_supersedes_inference(self):
        clock = FixedClock(local_dt(2026, 8, 16, 9, 0).astimezone(timezone.utc))
        rec, store = make_rec(clock)
        rec.apply(obs("plan", "calligraphy", certainty=Certainty.INFERRED,
                      time_expr=TimeExpr.AFTERNOON, observed=clock()))
        clock.set(local_dt(2026, 8, 16, 9, 30).astimezone(timezone.utc))
        rec.apply(obs("state", "calligraphy", "will go", certainty=Certainty.CONFIRMED,
                      observed=clock()))
        plan = store.get_state("user-001", "planning", "calligraphy")
        assert plan.certainty == Certainty.CONFIRMED
        assert plan.value == "will go"

    def test_S1_assistant_question_ignored(self):
        clock = FixedClock(local_dt(2026, 8, 16, 9, 0).astimezone(timezone.utc))
        rec, store = make_rec(clock)
        rec.apply(obs("awake", "awake", source_type=SourceType.ASSISTANT_QUESTION,
                      observed=clock()))
        assert store.counts()["states"] == 0

    def test_S2_transitions_preserved(self):
        clock = FixedClock(local_dt(2026, 8, 16, 8, 0).astimezone(timezone.utc))
        rec, store = make_rec(clock)
        rec.apply(obs("plan", "calligraphy", certainty=Certainty.TENTATIVE,
                      time_expr=TimeExpr.AFTERNOON, observed=clock()))
        clock.set(local_dt(2026, 8, 16, 9, 0).astimezone(timezone.utc))
        rec.apply(obs("plan", "calligraphy", certainty=Certainty.PLANNED,
                      time_expr=TimeExpr.AFTERNOON, observed=clock()))
        clock.set(local_dt(2026, 8, 16, 10, 0).astimezone(timezone.utc))
        rec.apply(obs("cancel", "", observed=clock()))
        plan = store.get_state("user-001", "planning", "calligraphy")
        transitions = store.get_transitions(plan.state_id)
        chain = [t.from_status for t in transitions] + [transitions[-1].to_status]
        assert chain == ["tentative", "tentative", "planned", "cancelled"]

    def test_conservative_unknown_type_no_mutation(self):
        clock = FixedClock(local_dt(2026, 8, 16, 9, 0).astimezone(timezone.utc))
        rec, store = make_rec(clock)
        before = store.counts()["states"]
        rec.apply(obs("state", "mystery_key", "whatever", observed=clock()))
        assert store.counts()["states"] == before


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TestStore:
    def test_event_idempotency(self):
        store = SQLiteStore(":memory:")
        now = datetime.now(timezone.utc)
        assert store.try_ingest_event("u", "e1", now) is True
        assert store.try_ingest_event("u", "e1", now) is False
        assert store.try_ingest_event("u", "e2", now) is True
        store.close()

    def test_observation_index_idempotency(self):
        store = SQLiteStore(":memory:")
        now = datetime.now(timezone.utc)
        a = obs("awake", "awake", observed=now)
        ids = store.insert_observations([a, a.clone(observation_index=1)])
        assert len(ids) == 2
        again = store.insert_observations([a])
        assert again == []  # duplicate ignored
        store.close()


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------


class TestSnapshot:
    def _service(self, clock):
        store = SQLiteStore(":memory:")
        return UserStateService(store, now_fn=clock), store

    def test_base_and_query_increment(self):
        clock = FixedClock(local_dt(2026, 8, 16, 9, 0).astimezone(timezone.utc))
        svc, store = self._service(clock)
        rec = svc.reconciler
        # fill BASE past its cap so the query increment has room to act
        for i in range(10):
            rec.apply(obs("plan", f"plan_{i}", certainty=Certainty.PLANNED,
                          time_expr=TimeExpr.TONIGHT, observed=clock()))
        rec.apply(obs("symptom", "stomach_pain", "active", category="health", observed=clock()))
        rec.apply(obs("activity", "swimming", "completed", observed=clock()))
        snap = svc.get_snapshot("user-001")
        assert len(snap.base_items) == 8  # fixed底座 5-8 cap
        assert "stomach discomfort: active" in snap.text
        # query-aware increment surfaces the right item (0-3 items)
        snap_q = svc.get_snapshot("user-001", query="还去游泳吗")
        assert "swam" in snap_q.text
        assert 0 < len(snap_q.query_items) <= 3
        assert len(snap_q.text.splitlines()) <= 1 + 8 + 3  # bounded, never bloats
        svc.close()

    def test_snapshot_excludes_expired(self):
        clock = FixedClock(local_dt(2026, 8, 16, 8, 0).astimezone(timezone.utc))
        svc, store = self._service(clock)
        svc.reconciler.apply(obs("plan", "calligraphy", certainty=Certainty.TENTATIVE,
                                 time_expr=TimeExpr.AFTERNOON, observed=clock()))
        clock.set(local_dt(2026, 8, 16, 18, 0).astimezone(timezone.utc))
        snap = svc.get_snapshot("user-001")
        assert "calligraphy" not in snap.text
        svc.close()

    def test_empty_snapshot(self):
        clock = FixedClock(local_dt(2026, 8, 16, 8, 0).astimezone(timezone.utc))
        svc, store = self._service(clock)
        snap = svc.get_snapshot("user-001")
        assert "(no current states)" in snap.text
        svc.close()
