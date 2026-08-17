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
        → sleeping superseded — including the persisted data lineage: the
        supersede transition row must point at the evidence observation's
        real row id (not just coexist with it)."""
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
            # P2 data lineage: the transition must point at the REAL
            # evidence row — traceable from state change back to evidence
            evidence_id = store.get_observation_id(
                "u", "e2", interactions[0].observation_index
            )
            assert evidence_id is not None
            assert supersede[0].source_observation_id == evidence_id, (
                "supersede transition lost its evidence lineage"
            )
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


class TestV2V1Coexistence:
    """P3: V2 inference and the V1 rule handlers must coexist during the
    migration — the direction V2 does NOT (yet) own stays V1's."""

    def test_reverse_awake_to_sleeping_still_v1(self):
        """awake→sleeping supersede stays under V1 R1/R2: explicit sleep
        language supersedes the current awake (history kept) and V2
        inference must not interfere."""
        service, store = make_service()
        try:
            t1 = datetime.now(timezone.utc)
            t2 = t1 + timedelta(minutes=30)
            observe(service, "u", "e1", "我刚睡醒", t1)
            awake = store.get_state("u", "sleep", "awake")
            assert awake is not None and awake.status == "active"

            observe(service, "u", "e2", "我睡了", t2)
            awake = store.get_state("u", "sleep", "awake")
            sleeping = store.get_state("u", "sleep", "sleeping")
            assert awake.status == StateStatus.SUPERSEDED
            assert sleeping is not None and sleeping.status == "active"
            # V1 ownership proof: the supersede reason is R2's, and the wake
            # history (valid_from) was NOT rewritten
            transitions = store.get_transitions(awake.state_id)
            supersede = [t for t in transitions if t.to_status == StateStatus.SUPERSEDED]
            assert len(supersede) == 1
            assert "R2" in supersede[0].reason, (
                "awake→sleeping supersede left V1 R2: %r" % supersede[0].reason
            )
            # the "我睡了" turn also carried an interaction observation — it
            # must NOT have woken the user back up
            assert store.get_state("u", "sleep", "sleeping").status == "active"
        finally:
            service.close()

    def test_sleeping_plus_interaction_same_second(self):
        """P3 mixed scenario: '睡了一半发消息' — an explicit sleep claim at
        second T and a message with an interaction observation at the SAME
        second T. Evidence precedence (explicit ≥ behavioral, same semantic
        time) must keep sleeping active and create no awake."""
        service, store = make_service()
        try:
            t = datetime.now(timezone.utc)
            observe(service, "u", "e1", "我睡了", t)
            sleeping = store.get_state("u", "sleep", "sleeping")
            assert sleeping.status == "active"

            # message at the exact same observed_at (different event)
            observe(service, "u", "e2", "背有点僵", t)

            sleeping = store.get_state("u", "sleep", "sleeping")
            assert sleeping.status == "active", (
                "same-second interaction superseded the explicit sleep claim"
            )
            awake = store.get_state("u", "sleep", "awake")
            assert awake is None, "same-second interaction established awake"
        finally:
            service.close()


class TestV2MigrationPlans:
    """R3-R5 migration (PLAN_LIFECYCLE): plan observations are now owned by
    the Inference Engine; the V1 plan rule handlers are gone."""

    def _clock(self):
        return FixedClock(local_dt(2026, 8, 16, 8, 0).astimezone(timezone.utc))

    def _plan_obs(self, key, certainty=Certainty.TENTATIVE,
                  time_expr="afternoon", observed=None, event_id="e1"):
        from statebar_mcp.core.models import Observation

        return Observation(
            subject_id="u", event_id=event_id, type="plan",
            category="planning", key=key, value="", certainty=certainty,
            time_expression=time_expr, source=Source(type="conversation"),
            observed_at=observed or datetime.now(timezone.utc),
            raw_payload="下午可能去写书法",
        )

    def test_inference_owns_plan_observations(self):
        from statebar_mcp.core.inference import InferenceEngine

        clock = self._clock()
        engine = InferenceEngine(now_fn=clock)
        obs = self._plan_obs("calligraphy", observed=clock())
        intents, handled = engine.infer(obs, [])
        assert handled is True, "plan observation must be owned by inference"
        assert len(intents) == 1
        assert intents[0].action == "ESTABLISH"
        assert intents[0].category == "planning"
        assert intents[0].key == "calligraphy"
        assert intents[0].status == StateStatus.TENTATIVE

    def test_plan_lifecycle_chain_end_to_end(self):
        """tentative → planned → cancelled, all through inference intents
        with PLAN_LIFECYCLE-legal hops and persisted transitions."""
        clock = self._clock()
        service, store = make_service(clock=clock)
        try:
            rec = service.reconciler
            rec.apply(self._plan_obs("calligraphy", observed=clock(), event_id="e1"))
            plan = store.get_state("u", "planning", "calligraphy")
            assert plan.status == StateStatus.TENTATIVE

            clock.set(local_dt(2026, 8, 16, 9, 0).astimezone(timezone.utc))
            rec.apply(self._plan_obs(
                "calligraphy", certainty=Certainty.PLANNED,
                observed=clock(), event_id="e2",
            ))
            plan = store.get_state("u", "planning", "calligraphy")
            assert plan.status == StateStatus.PLANNED
            assert plan.certainty == Certainty.PLANNED

            clock.set(local_dt(2026, 8, 16, 10, 0).astimezone(timezone.utc))
            cancel = Observation(
                subject_id="u", event_id="e3", type="cancel",
                category="planning", key="", value="cancel",
                source=Source(type="conversation"), observed_at=clock(),
                raw_payload="算了，不去了",
            )
            rec.apply(cancel)
            plan = store.get_state("u", "planning", "calligraphy")
            assert plan.status == StateStatus.CANCELLED
            chain = [t.from_status for t in store.get_transitions(plan.state_id)] + \
                [store.get_transitions(plan.state_id)[-1].to_status]
            assert chain == ["tentative", "tentative", "planned", "cancelled"]
            # cancel keeps it briefly conversational (relevant window)
            assert plan.relevant_until > clock()
        finally:
            service.close()

    def test_plan_reschedule_new_episode_via_inference(self):
        """R6: a different semantic window supersedes the old plan episode
        and establishes a new one — both rows persist, old is superseded."""
        clock = self._clock()
        service, store = make_service(clock=clock)
        try:
            rec = service.reconciler
            rec.apply(self._plan_obs("calligraphy", observed=clock(), event_id="e1"))
            clock.set(local_dt(2026, 8, 16, 16, 50).astimezone(timezone.utc))
            rec.apply(self._plan_obs(
                "calligraphy", certainty=Certainty.PLANNED,
                time_expr="tonight", observed=clock(), event_id="e2",
            ))
            plans = [s for s in store.get_states("u") if s.key == "calligraphy"]
            statuses = {s.status for s in plans}
            assert StateStatus.SUPERSEDED in statuses
            current = [s for s in plans if s.status == StateStatus.PLANNED]
            assert len(current) == 1
            assert current[0].valid_until.astimezone(LOCAL_TZ).hour == 23
        finally:
            service.close()

    def test_plan_completion_by_activity_via_inference(self):
        """R4's plan half: an activity observation completes the active plan
        (PLAN_LIFECYCLE → completed)."""
        clock = self._clock()
        service, store = make_service(clock=clock)
        try:
            rec = service.reconciler
            rec.apply(self._plan_obs("swimming", observed=clock(), event_id="e1"))
            clock.set(local_dt(2026, 8, 16, 18, 0).astimezone(timezone.utc))
            activity = Observation(
                subject_id="u", event_id="e2", type="activity",
                category="activity", key="swimming", value="completed",
                source=Source(type="conversation"), observed_at=clock(),
                raw_payload="今天去游泳了",
            )
            rec.apply(activity)
            plan = store.get_state("u", "planning", "swimming")
            assert plan.status == StateStatus.COMPLETED
        finally:
            service.close()

    def test_v1_plan_rule_handlers_removed(self):
        """Migration acceptance: the V1 plan rule handlers are gone from the
        reconciler; only the still-unmigrated rules remain."""
        from statebar_mcp.core.reconciler import Reconciler, _RULE_HANDLERS

        for obs_type in ("plan", "cancel"):
            assert obs_type not in _RULE_HANDLERS, (
                f"V1 handler for {obs_type!r} still registered"
            )
        rec = Reconciler(SQLiteStore(":memory:"))
        for name in ("_r3_cancel", "_r5_confirm", "_r6_reschedule",
                     "_r_plan_new", "_create_plan_state", "_plans",
                     "_target_plan"):
            assert not hasattr(rec, name), f"V1 plan handler {name} still present"
