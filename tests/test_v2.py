"""V2 acceptance tests (Spec §21): the interaction → awake vertical slice.

V2-T1..T8. External Contract compatibility (V2-T8) is additionally proven by
the entire V1 suite (D1-D12, security, MCP stdio) staying green.
"""

from __future__ import annotations

import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from statebar_mcp.core import ontology  # noqa: E402
from statebar_mcp.core.extractor.persistent import MockExtractor  # noqa: E402
from statebar_mcp.core.models import (  # noqa: E402
    Certainty,
    IntentAction,
    Observation,
    ObserveRequest,
    Source,
    SourceType,
    State,
    StateStatus,
    TransitionIntent,
)
from statebar_mcp.core.service import UserStateService  # noqa: E402
from statebar_mcp.core.store import SQLiteStore  # noqa: E402
from statebar_mcp.transports.rest import create_server  # noqa: E402

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


def make_service(clock=None):
    store = SQLiteStore(":memory:")
    service = UserStateService(
        store,
        persistent_extractor=MockExtractor(),
        now_fn=clock or (lambda: datetime.now(timezone.utc)),
    )
    return service, store


def observe(service, subject_id, event_id, text, observed_at, source_type="conversation"):
    return service.observe(
        ObserveRequest(
            subject_id=subject_id,
            event_id=event_id,
            text=text,
            source=Source(type=source_type),
            observed_at=observed_at,
        )
    )


def wait_complete(store, subject_id, event_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ev = store.get_event(subject_id, event_id)
        if ev and ev["status"] == "complete":
            return True
        time.sleep(0.02)
    return False


class TestV2InteractionSlice:
    def test_T1_sleeping_superseded_by_any_user_message(self):
        """sleeping active + arbitrary message (no wake words) → superseded."""
        service, store = make_service()
        t1 = datetime.now(timezone.utc)
        t2 = t1 + timedelta(minutes=30)
        try:
            observe(service, "u", "e1", "我睡了", t1)
            assert store.get_state("u", "sleep", "sleeping").status == "active"

            observe(service, "u", "e2", "背有点僵", t2)
            sleeping = store.get_state("u", "sleep", "sleeping")
            assert sleeping.status == StateStatus.SUPERSEDED
            awake = store.get_state("u", "sleep", "awake")
            assert awake is not None and awake.status == StateStatus.ACTIVE
        finally:
            service.close()

    def test_T2_interaction_observation_created(self):
        """The interaction itself is recorded as an observation — not a
        direct state mutation."""
        service, store = make_service()
        try:
            t = datetime.now(timezone.utc)
            observe(service, "u", "e1", "股票怎么回事", t)
            obs = store.get_observations("u")
            interactions = [
                o for o in obs
                if o.key == "interactive_activity" and o.category == "presence"
            ]
            assert len(interactions) == 1
            it = interactions[0]
            assert it.type == "activity"
            assert it.certainty == Certainty.OBSERVED
            assert it.confidence == 1.0
            assert it.source.type == SourceType.INTERACTION
        finally:
            service.close()

    def test_T3_transition_traceability(self):
        """The history must trace: interaction observation → awake inference
        → sleeping superseded."""
        service, store = make_service()
        t1 = datetime.now(timezone.utc)
        t2 = t1 + timedelta(minutes=10)
        try:
            observe(service, "u", "e1", "我睡了", t1)
            sleeping = store.get_state("u", "sleep", "sleeping")
            observe(service, "u", "e2", "哈哈", t2)
            transitions = store.get_transitions(sleeping.state_id)
            supersede = [t for t in transitions if t.to_status == StateStatus.SUPERSEDED]
            assert len(supersede) == 1
            assert "interactive_activity" in supersede[0].reason
            assert "wakefulness" in supersede[0].reason
            # the evidence observation exists and is reconciled
            interactions = [
                o for o in store.get_observations("u")
                if o.key == "interactive_activity" and o.event_id == "e2"
            ]
            assert interactions
            assert store.is_observation_applied(
                "u", "e2", interactions[0].observation_index
            ), "interaction observation tombstone missing"
        finally:
            service.close()

    def test_T4_assistant_question_produces_no_interaction(self):
        service, store = make_service()
        try:
            t = datetime.now(timezone.utc)
            observe(
                service, "u", "e1", "你还醒着吗？", t,
                source_type=SourceType.ASSISTANT_QUESTION,
            )
            obs = store.get_observations("u")
            assert not any(o.key == "interactive_activity" for o in obs)
            assert all(
                s.key != "awake" for s in store.get_states("u")
            )  # S1 still holds
        finally:
            service.close()

    def test_T5_delayed_message_does_not_supersede_newer_sleep(self):
        clock = FixedClock(local_dt(2026, 8, 16, 14, 0).astimezone(timezone.utc))
        service, store = make_service(clock=clock)
        try:
            observe(service, "u", "e1", "我睡了", clock())
            sleeping = store.get_state("u", "sleep", "sleeping")
            assert sleeping.status == "active"

            # a delayed user message, semantic time BEFORE the sleep state
            delayed = local_dt(2026, 8, 16, 13, 50).astimezone(timezone.utc)
            observe(service, "u", "e2", "背有点僵", delayed)

            assert store.get_state("u", "sleep", "sleeping").status == "active"
            awake = store.get_state("u", "sleep", "awake")
            assert awake is None or awake.status != StateStatus.ACTIVE, (
                "delayed interaction must not establish awake over newer sleep"
            )
        finally:
            service.close()

    def test_T6_no_wake_time_claim_from_interaction(self):
        """14:37 interaction must express 'awake confirmed at ~14:37', never
        'awake since ~14:37' (no invented wake time)."""
        clock = FixedClock(local_dt(2026, 8, 16, 14, 37).astimezone(timezone.utc))
        service, store = make_service(clock=clock)
        try:
            observe(service, "u", "e1", "背有点僵", clock())
            snap = service.get_snapshot("u")
            assert "awake confirmed at ~14:37" in snap.text
            assert "awake since" not in snap.text
            # an explicit wake claim later upgrades to a real "since"
            clock.set(local_dt(2026, 8, 16, 15, 0).astimezone(timezone.utc))
            observe(service, "u", "e2", "我刚睡醒", clock())
            snap2 = service.get_snapshot("u")
            assert "awake since ~15:00" in snap2.text
        finally:
            service.close()

    def test_T7_explicit_awake_still_works(self):
        service, store = make_service()
        try:
            t = datetime.now(timezone.utc)
            observe(service, "u", "e1", "我刚睡醒", t)
            awake = store.get_state("u", "sleep", "awake")
            assert awake is not None and awake.status == StateStatus.ACTIVE
            snap = service.get_snapshot("u")
            assert "awake since" in snap.text
        finally:
            service.close()

    def test_T8_external_contract_unchanged(self):
        """observe/snapshot over the frozen REST contract behave exactly as
        in V1 (V2 must not break the external protocol)."""
        service, store = make_service()
        srv = create_server(service, "127.0.0.1", 0)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            import json

            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/observe",
                data=json.dumps(
                    {"subject_id": "u", "event_id": "e1", "text": "我刚睡醒"}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            assert body["status"] == "accepted"
            assert "sync_observations" in body  # V1 response shape unchanged

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/snapshot?subject_id=u", timeout=5
            ) as resp:
                snap = json.loads(resp.read().decode("utf-8"))
            assert snap["text"].startswith("CURRENT USER STATE")
            assert "awake" in snap["text"]
        finally:
            srv.shutdown()
            srv.server_close()
            service.close()


class TestV2EvidencePrecedence:
    def test_explicit_sleep_beats_same_event_interaction(self):
        """The user says 我睡了 — the interaction evidence from the SAME
        event must NOT wake them back up (explicit language > behavior)."""
        service, store = make_service()
        try:
            t = datetime.now(timezone.utc)
            observe(service, "u", "e1", "我睡了", t)
            sleeping = store.get_state("u", "sleep", "sleeping")
            assert sleeping.status == "active", (
                "interaction evidence contradicted an explicit sleep claim"
            )
        finally:
            service.close()


class TestV2TrustBoundary:
    """P1: system-issued behavioral evidence must stay the ONLY source of
    behavioral inference. A language-extracted observation that merely LOOKS
    like the interaction shape must never create user state."""

    def test_assistant_sourced_interaction_lookalike_creates_no_awake(self):
        """The reviewer's repro: source=assistant_question with
        key=interactive_activity / certainty=observed used to establish
        'awake active' because inference ran before the assistant-only S1
        gate. It must be ignored at both layers (predicate + S1)."""
        service, store = make_service()
        try:
            t = datetime.now(timezone.utc)
            lookalike = Observation(
                subject_id="u",
                event_id="e1",
                type="activity",
                category="presence",
                key="interactive_activity",
                value="true",
                certainty=Certainty.OBSERVED,
                source=Source(type=SourceType.ASSISTANT_QUESTION),
                observed_at=t,
                confidence=1.0,
                raw_payload="你还醒着吗？",
            )
            changed = service.reconcile(lookalike)
            assert changed == []
            assert store.get_state("u", "sleep", "awake") is None, (
                "assistant-sourced look-alike established awake state"
            )
            assert store.counts()["states"] == 0
            # recorded as applied (no endless reprocessing), never as state
            assert store.is_observation_applied("u", "e1", 0)
        finally:
            service.close()

    def test_plain_conversation_lookalike_creates_no_awake(self):
        """Same shape but source=conversation (language-extracted, not
        system-issued) must also fail the interaction predicate."""
        service, store = make_service()
        try:
            t = datetime.now(timezone.utc)
            lookalike = Observation(
                subject_id="u",
                event_id="e1",
                type="activity",
                category="presence",
                key="interactive_activity",
                value="true",
                certainty=Certainty.OBSERVED,
                source=Source(type=SourceType.CONVERSATION),
                observed_at=t,
                confidence=1.0,
                raw_payload="背有点僵",
            )
            changed = service.reconcile(lookalike)
            # not owned by inference (wrong source) → falls back to V1 rules;
            # the sleep/awake boundary must stay untouched either way
            assert store.get_state("u", "sleep", "awake") is None
        finally:
            service.close()

    def test_S1_gate_covers_every_entry_before_inference(self):
        """An assistant-sourced AWAKE observation must stay observation-only
        even though V2 inference now runs ahead of the V1 rule handlers."""
        service, store = make_service()
        try:
            t = datetime.now(timezone.utc)
            assistant_awake = Observation(
                subject_id="u",
                event_id="e1",
                type="awake",
                category="sleep",
                key="awake",
                value="awake",
                source=Source(type=SourceType.ASSISTANT_QUESTION),
                observed_at=t,
                raw_payload="你今天是不是刚睡醒？",
            )
            changed = service.reconcile(assistant_awake)
            assert changed == []
            assert store.counts()["states"] == 0
            assert store.is_observation_applied("u", "e1", 0)
        finally:
            service.close()


class TestV2TransitionLegalityGate:
    """P2: ontology.validate_transition is wired into the intent execution
    path — an intent whose status hop is not in the declared lifecycle is
    rejected BEFORE any write."""

    def test_sleep_resolve_intent_rejected_by_gate(self):
        """The reviewer's injection: a RESOLVE intent against a sleep state.
        ontology.validate_transition returns False for sleep ACTIVE→RESOLVED
        (SLEEP_LIFECYCLE only allows ACTIVE/SUPERSEDED); the executor must
        refuse to change the status."""
        service, store = make_service()
        try:
            t = datetime.now(timezone.utc)
            observe(service, "u", "e1", "我睡了", t)
            sleeping = store.get_state("u", "sleep", "sleeping")
            assert sleeping.status == "active"
            assert ontology.validate_transition(
                sleeping, StateStatus.RESOLVED
            ) is False, "sleep→resolved must be illegal in the ontology"

            inject = Observation(
                subject_id="u", event_id="e2", type="resolve",
                category="health", key="", value="resolved",
                source=Source(type=SourceType.SYSTEM),
                observed_at=t, raw_payload="fault injection",
            )
            intent = TransitionIntent(
                action=IntentAction.RESOLVE,
                reason="injected illegal sleep RESOLVE",
                evidence=[],
                target_state_id=sleeping.state_id,
                target_category="sleep",
                target_key="sleeping",
            )
            rec = service.reconciler
            changed = rec._execute_intents(inject, rec._active_states("u"), [intent])
            assert changed == [], "illegal intent was executed"

            sleeping = store.get_state("u", "sleep", "sleeping")
            assert sleeping.status == "active", (
                "transition legality gate rejected the hop but the executor "
                "still changed the status"
            )
            assert len(store.get_transitions(sleeping.state_id)) == 1, (
                "rejected intent still wrote a transition row"
            )
        finally:
            service.close()

    def test_declared_lifecycles_pass_the_gate(self):
        """The gate must not block hops that the declared lifecycles allow
        (used by the migration steps): plan tentative→planned→cancelled and
        symptom active→improving→resolved."""
        plan = State(
            subject_id="u", category="planning", key="calligraphy",
            status=StateStatus.TENTATIVE,
        )
        assert ontology.validate_transition(plan, StateStatus.PLANNED)
        assert ontology.validate_transition(plan, StateStatus.CANCELLED)
        symptom = State(
            subject_id="u", category="health", key="stomach_pain",
            status=StateStatus.ACTIVE,
        )
        assert ontology.validate_transition(symptom, StateStatus.IMPROVING)
        assert ontology.validate_transition(symptom, StateStatus.RESOLVED)
        # sleep→superseded (the interaction slice) stays legal
        sleeping = State(
            subject_id="u", category="sleep", key="sleeping",
            status=StateStatus.ACTIVE,
        )
        assert ontology.validate_transition(sleeping, StateStatus.SUPERSEDED)
