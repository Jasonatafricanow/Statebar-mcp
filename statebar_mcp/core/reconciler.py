"""State Reconciler — the frozen ruleset (派单总纲 §10).

Business rules R1-R10, system rules S1-S2, plus the conservative principle
and the out-of-order guard (semantic observed_at ordering, D12).

Every mutation is expressed as a State + StateTransition pair, so S2
(non-destructive history) holds by construction. Unknown relations are
recorded as observations only and never mutate unrelated canonical state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from . import lifecycle
from .models import (
    Certainty,
    Observation,
    ObservationType,
    SourceType,
    State,
    StateStatus,
    StateTransition,
    utc_now,
)
from .store import SQLiteStore

logger = logging.getLogger(__name__)

_PLAN_STATUSES = (StateStatus.TENTATIVE, StateStatus.PLANNED, StateStatus.PENDING)
_SYMPTOM_STATUSES = (StateStatus.ACTIVE, StateStatus.IMPROVING)

# statuses whose states may be found as "current" for a key
_NON_TERMINAL = tuple(
    s for s in StateStatus.ALL if s not in StateStatus.TERMINAL
)


class Reconciler:
    """Applies one validated Observation against the canonical state store."""

    def __init__(self, store: SQLiteStore, now_fn=utc_now):
        self.store = store
        self._now_fn = now_fn

    def _now(self) -> datetime:
        return self._now_fn()

    # -- helpers ------------------------------------------------------------

    def _active_states(self, subject_id: str) -> List[State]:
        return [
            s
            for s in self.store.get_states(subject_id)
            if s.status not in StateStatus.TERMINAL
        ]

    def _latest_for_key(self, states: List[State], category: str, key: str) -> Optional[State]:
        matches = [s for s in states if s.category == category and s.key == key]
        if not matches:
            return None
        return max(matches, key=lambda s: s.updated_at)

    def _latest_for_key_any_category(self, states: List[State], key: str) -> Optional[State]:
        """Latest state with this key across ALL categories (staleness guard)."""
        matches = [s for s in states if s.key == key]
        if not matches:
            return None
        return max(matches, key=lambda s: s.updated_at)

    @staticmethod
    def _obs_key(obs: Optional[Observation]) -> str:
        if obs is None:
            return ""
        return f"{obs.event_id}:{obs.observation_index}"

    def _is_stale_creation(self, obs: Observation, states: List[State], key: str) -> bool:
        """D12 + crash-recovery idempotency for creation paths:
        - a delayed message must never create a state that would roll back
          newer evidence for the same semantic key;
        - replaying the SAME observation (already applied) must not create a
          duplicate state. Consults the store (ALL statuses, including
          terminal), because a completed/cancelled state is exactly what
          must not be rolled back."""
        if not key:
            return False
        latest = self.store.get_latest_state_by_key(obs.subject_id, key)
        if latest is None:
            return False
        if latest.last_observation_key == self._obs_key(obs):
            return True  # this exact observation already applied
        if obs.is_replay and latest.last_observed_at >= obs.observed_at:
            # ambiguous legacy replay: a different observation at the same-or-
            # later time already owns this key — do not create/resurrect.
            return True
        if latest.last_observed_at > obs.observed_at:
            logger.debug(
                "stale creation skipped: obs observed_at %s older than state %s last_observed %s",
                obs.observed_at.isoformat(), latest.state_id, latest.last_observed_at.isoformat(),
            )
            return True
        return False

    def _create(self, obs: Observation, category: str, key: str, value: str,
                status: str, certainty: str, valid_from: datetime,
                valid_until: datetime, relevant_until: datetime,
                followup_relevant: bool, priority: int) -> State:
        now = self._now()
        return State(
            subject_id=obs.subject_id,
            category=category,
            key=key,
            value=value,
            status=status,
            certainty=certainty,
            valid_from=valid_from,
            valid_until=valid_until,
            relevant_until=relevant_until,
            followup_relevant=followup_relevant,
            snapshot_priority=priority,
            created_at=now,
            updated_at=now,
            last_observed_at=obs.observed_at,
            last_observation_key=self._obs_key(obs),
        )

    def _transition(self, state: State, to_status: str, reason: str,
                    obs: Optional[Observation]) -> StateTransition:
        return StateTransition(
            state_id=state.state_id,
            subject_id=state.subject_id,
            from_status=state.status,
            to_status=to_status,
            reason=reason,
            source_observation_id=None,
            created_at=self._now(),
        )

    def _apply(self, state: State, transition: StateTransition) -> None:
        """Persist a mutation: state row + transition row (S2)."""
        self.store.insert_state(state)
        self.store.record_transition(transition)

    def _mutate(
        self,
        state: State,
        obs: Observation,
        *,
        status: Optional[str] = None,
        value: Optional[str] = None,
        certainty: Optional[str] = None,
        valid_from: Optional[datetime] = None,
        valid_until: Optional[datetime] = None,
        relevant_until: Optional[datetime] = None,
        followup_relevant: Optional[bool] = None,
        reason: str,
    ) -> bool:
        """In-place update with transition history. Refuses when the
        observation is older than the state's last SEMANTIC change (D12
        guard; equal-second bursts are fine — T10). Refuses when this exact
        observation was already applied (crash-recovery replay idempotency:
        no duplicate transitions)."""
        obs_key = self._obs_key(obs)
        if obs_key and state.last_observation_key == obs_key:
            logger.debug(
                "observation %s already applied to state %s; replay skipped",
                obs_key, state.state_id,
            )
            return False
        if obs.is_replay and obs.observed_at <= state.last_observed_at:
            # ambiguous legacy replay: a DIFFERENT observation at the same-or-
            # later semantic time already owns this state — never roll back.
            logger.debug(
                "legacy replay %s blocked: state %s owned by a different "
                "observation at >= the same semantic time",
                obs_key, state.state_id,
            )
            return False
        if obs.observed_at < state.last_observed_at:
            logger.debug(
                "stale observation %s (observed %s < state last_observed %s); skipped",
                obs.type, obs.observed_at.isoformat(), state.last_observed_at.isoformat(),
            )
            return False
        transition = self._transition(state, status or state.status, reason, obs)
        if status is not None:
            state.status = status
        if value is not None:
            state.value = value
        if certainty is not None:
            state.certainty = certainty
        if valid_from is not None:
            state.valid_from = valid_from
        if valid_until is not None:
            state.valid_until = valid_until
        if relevant_until is not None:
            state.relevant_until = relevant_until
        if followup_relevant is not None:
            state.followup_relevant = followup_relevant
        state.updated_at = self._now()
        state.last_observed_at = obs.observed_at
        state.last_observation_key = obs_key
        self._apply(state, transition)
        return True

    def _supersede(self, state: State, obs: Optional[Observation], reason: str) -> bool:
        """Mark a state superseded. Refuses when the observation is older
        than the state's last semantic change (D12: no rollbacks) or when
        this exact observation was already applied (replay idempotency)."""
        obs_key = self._obs_key(obs)
        if obs_key and state.last_observation_key == obs_key:
            return False
        if obs.is_replay and obs_key and obs.observed_at <= state.last_observed_at:
            # ambiguous legacy replay: a DIFFERENT observation at the same-or-
            # later semantic time already owns this state — never roll back.
            return False
        if obs is not None and obs.observed_at < state.last_observed_at:
            logger.debug(
                "stale supersede skipped: obs %s < state last_observed %s",
                obs.observed_at.isoformat(), state.last_observed_at.isoformat(),
            )
            return False
        transition = self._transition(state, StateStatus.SUPERSEDED, reason, obs)
        state.status = StateStatus.SUPERSEDED
        state.updated_at = self._now()
        state.last_observed_at = obs.observed_at if obs is not None else state.last_observed_at
        state.last_observation_key = obs_key
        self._apply(state, transition)
        return True

    # -- entry point ----------------------------------------------------------

    def apply(self, obs: Observation) -> List[State]:
        """Reconcile one observation. Returns states created/updated.

        Idempotent per observation: an applied-record tombstone is checked
        BEFORE any rule runs and written AFTER the rules succeed (inside the
        caller's transaction when there is one), so replays are skipped no
        matter how the states changed afterwards."""
        if self.store.is_observation_applied(
            obs.subject_id, obs.event_id, obs.observation_index
        ):
            logger.debug(
                "observation %s:%s already applied; replay skipped",
                obs.event_id, obs.observation_index,
            )
            return []
        states = self._active_states(obs.subject_id)
        changed = self._apply_inner(obs, states)
        self.store.mark_observation_applied(
            obs.subject_id, obs.event_id, obs.observation_index
        )
        return changed

    def _replay_blocked_by_latest(self, obs: Observation, key: str) -> bool:
        """Replay-mode guard for creation branches: if ANY state with this
        key (any status) was already touched by a DIFFERENT observation at
        the same-or-later semantic time, the replayed observation must not
        create/resurrect a state (the ambiguous legacy-crash case)."""
        if not obs.is_replay:
            return False
        latest = self.store.get_latest_state_by_key(obs.subject_id, key)
        if latest is None:
            return False
        if latest.last_observation_key == self._obs_key(obs):
            return True  # exact same observation already applied
        return latest.last_observed_at >= obs.observed_at

    def _apply_inner(self, obs: Observation, states: List[State]) -> List[State]:
        changed: List[State] = []

        # S1: assistant-only observations can never create canonical state.
        if obs.source.type == SourceType.ASSISTANT_QUESTION:
            logger.info("S1: assistant-sourced observation %s ignored", obs.type)
            return changed

        handler = _RULE_HANDLERS.get(obs.type)
        if handler is not None:
            result = handler(self, obs, states)
            changed.extend(result)
        else:
            logger.info("unknown observation type %r: observation-only, no state mutation", obs.type)

        # R9: an explicit user statement supersedes an inferred/estimated one.
        if obs.certainty == Certainty.CONFIRMED and obs.key:
            changed.extend(self._apply_r9(obs, states))
        return changed

    # -- R1 / R2 : the sleep/awake pair ---------------------------------------

    def _r1_awake(self, obs: Observation, states: List[State]) -> List[State]:
        changed: List[State] = []
        # supersede any active sleeping / preparing_sleep state
        for s in states:
            if s.category == "sleep" and s.key == "sleeping":
                if not self._supersede(s, obs, "R1 awake supersedes sleep"):
                    return changed  # stale: newer evidence already moved on
                changed.append(s)
        awake = self._latest_for_key(states, "sleep", "awake")
        if awake is not None:
            if obs.observed_at >= awake.last_observed_at:
                # new wake event: refresh valid_from (keep original history)
                if self._mutate(
                    awake, obs,
                    status=StateStatus.ACTIVE,
                    valid_from=obs.observed_at,
                    reason="R1 awake re-affirmed",
                ):
                    changed.append(awake)
            return changed
        if self._replay_blocked_by_latest(obs, "awake"):
            return changed  # legacy replay must not resurrect a superseded wake
        valid_until = obs.observed_at + timedelta(hours=lifecycle.TTL_AWAKE_HOURS)
        new = self._create(
            obs, "sleep", "awake", "awake", StateStatus.ACTIVE, Certainty.CONFIRMED,
            obs.observed_at, valid_until, valid_until, False, 4,
        )
        self._apply(new, self._transition(new, new.status, "R1 awake created", obs))
        changed.append(new)
        return changed

    def _r2_sleep(self, obs: Observation, states: List[State]) -> List[State]:
        changed: List[State] = []
        # do NOT rewrite historical wake: only mark the current awake superseded.
        for s in states:
            if s.category == "sleep" and s.key == "awake":
                if not self._supersede(s, obs, "R2 sleep supersedes current awake (history kept)"):
                    return changed  # stale: newer evidence already moved on
                changed.append(s)
        sleeping = self._latest_for_key(states, "sleep", "sleeping")
        if sleeping is not None:
            if self._mutate(
                sleeping, obs,
                status=StateStatus.ACTIVE,
                value=obs.value or "sleeping",
                valid_until=obs.observed_at + timedelta(hours=lifecycle.TTL_SLEEP_HOURS),
                reason="R2 sleep state re-affirmed",
            ):
                changed.append(sleeping)
            return changed
        if self._replay_blocked_by_latest(obs, "sleeping"):
            return changed  # legacy replay must not resurrect a superseded sleep
        valid_until = obs.observed_at + timedelta(hours=lifecycle.TTL_SLEEP_HOURS)
        new = self._create(
            obs, "sleep", "sleeping", obs.value or "sleeping", StateStatus.ACTIVE,
            Certainty.CONFIRMED, obs.observed_at, valid_until, valid_until, False, 4,
        )
        self._apply(new, self._transition(new, new.status, "R2 sleep created (wake history untouched)", obs))
        changed.append(new)
        return changed

    # -- R3 / R5 / R6 : plans ---------------------------------------------------

    def _plans(self, states: List[State]) -> List[State]:
        return [s for s in states if s.category == "planning" and s.status in _PLAN_STATUSES]

    def _target_plan(self, obs: Observation, states: List[State]) -> Optional[State]:
        plans = self._plans(states)
        if obs.key:
            for s in plans:
                if s.key == obs.key:
                    return s
            return None
        return max(plans, key=lambda s: s.updated_at) if plans else None

    def _r3_cancel(self, obs: Observation, states: List[State]) -> List[State]:
        plan = self._target_plan(obs, states)
        if plan is None:
            logger.info("R3: cancel with no active plan target; observation-only")
            return []
        if self._mutate(
            plan, obs,
            status=StateStatus.CANCELLED,
            relevant_until=self._now() + timedelta(hours=6),
            followup_relevant=False,
            reason="R3 plan cancelled",
        ):
            return [plan]
        return []

    def _r5_confirm(self, obs: Observation, states: List[State]) -> List[State]:
        if obs.certainty not in (Certainty.PLANNED, Certainty.CONFIRMED):
            return []
        plan = self._target_plan(obs, states)
        if plan is None or plan.status != StateStatus.TENTATIVE:
            return []
        if self._mutate(
            plan, obs,
            status=StateStatus.PLANNED,
            certainty=obs.certainty,
            reason="R5 tentative plan confirmed to planned",
        ):
            return [plan]
        return []

    def _r6_reschedule(self, obs: Observation, states: List[State]) -> List[State]:
        plan = self._target_plan(obs, states)
        if plan is None:
            return []
        _, new_until, new_relevant = lifecycle.semantic_window(
            obs.time_expression, obs.observed_at, self._now()
        )
        if new_until != plan.valid_until:
            if not self._supersede(plan, obs, "R6 reschedule: old window superseded"):
                return []
            changed: List[State] = [plan]
            new = self._create_plan_state(obs, new_until, new_relevant)
            self._apply(new, self._transition(new, new.status, "R6 reschedule: new window", obs))
            changed.append(new)
            return changed
        return self._r5_confirm(obs, states)

    def _create_plan_state(self, obs: Observation, valid_until, relevant_until) -> State:
        status = StateStatus.TENTATIVE if obs.certainty == Certainty.TENTATIVE else StateStatus.PLANNED
        return self._create(
            obs, "planning", obs.key, obs.value or obs.key, status, obs.certainty,
            obs.observed_at, valid_until, relevant_until, False, 3,
        )

    def _r_plan_new(self, obs: Observation, states: List[State]) -> List[State]:
        """Create a new plan (R5/R6 only mutate existing ones)."""
        if self._is_stale_creation(obs, states, obs.key):
            return []
        existing = self._latest_for_key(states, "planning", obs.key)
        if existing is not None:
            # A plan for this key already exists (any status). Do not fork a
            # second one unless the new observation is newer (conservative
            # principle: prefer coexisting over speculative merging).
            if obs.observed_at < existing.last_observed_at:
                return []
            if existing.status in _PLAN_STATUSES:
                # R6 handles reschedules; R5 handles confirmations. If the
                # window is identical, just re-affirm.
                return self._r6_reschedule(obs, states)
            if existing.status in (StateStatus.COMPLETED, StateStatus.CANCELLED,
                                   StateStatus.EXPIRED, StateStatus.RESOLVED):
                # a new, distinct plan episode for the same key
                _, valid_until, relevant_until = lifecycle.semantic_window(
                    obs.time_expression, obs.observed_at, self._now()
                )
                new = self._create_plan_state(obs, valid_until, relevant_until)
                self._apply(new, self._transition(new, new.status, "plan created (new episode)", obs))
                return [new]
        _, valid_until, relevant_until = lifecycle.semantic_window(
            obs.time_expression, obs.observed_at, self._now()
        )
        new = self._create_plan_state(obs, valid_until, relevant_until)
        self._apply(new, self._transition(new, new.status, "plan created", obs))
        return [new]

    # -- R7 / R8 : symptoms ------------------------------------------------------

    def _active_symptoms(self, states: List[State]) -> List[State]:
        return [s for s in states if s.category == "health" and s.status in _SYMPTOM_STATUSES]

    def _target_symptom(self, obs: Observation, states: List[State]) -> Optional[State]:
        symptoms = self._active_symptoms(states)
        if obs.key:
            for s in symptoms:
                if s.key == obs.key:
                    return s
            return None
        return max(symptoms, key=lambda s: s.updated_at) if symptoms else None

    def _r_symptom_new(self, obs: Observation, states: List[State]) -> List[State]:
        if self._is_stale_creation(obs, states, obs.key):
            return []
        existing = self._latest_for_key(states, "health", obs.key)
        if existing is not None:
            if obs.observed_at < existing.last_observed_at:
                return []
            if existing.status in _SYMPTOM_STATUSES:
                if self._mutate(
                    existing, obs,
                    status=StateStatus.ACTIVE,
                    followup_relevant=True,
                    reason="symptom re-affirmed active",
                ):
                    return [existing]
                return []
            if existing.status in (StateStatus.RESOLVED, StateStatus.SUPERSEDED):
                # new episode
                valid_until = obs.observed_at + timedelta(hours=lifecycle.TTL_NOW_HOURS)
                new = self._create(
                    obs, "health", obs.key, "active", StateStatus.ACTIVE, Certainty.CONFIRMED,
                    obs.observed_at, valid_until, valid_until, True, 5,
                )
                self._apply(new, self._transition(new, new.status, "symptom new episode", obs))
                return [new]
        valid_until = obs.observed_at + timedelta(hours=lifecycle.TTL_NOW_HOURS)
        new = self._create(
            obs, "health", obs.key, "active", StateStatus.ACTIVE, Certainty.CONFIRMED,
            obs.observed_at, valid_until, valid_until, True, 5,
        )
        self._apply(new, self._transition(new, new.status, "symptom active", obs))
        return [new]

    def _r7_improving(self, obs: Observation, states: List[State]) -> List[State]:
        symptom = self._target_symptom(obs, states)
        if symptom is None:
            logger.info("R7: improving with no active symptom target; observation-only")
            return []
        if self._mutate(symptom, obs, status=StateStatus.IMPROVING, reason="R7 symptom improving"):
            return [symptom]
        return []

    def _r8_resolved(self, obs: Observation, states: List[State]) -> List[State]:
        symptom = self._target_symptom(obs, states)
        if symptom is None:
            logger.info("R8: resolved with no active symptom target; observation-only")
            return []
        if self._mutate(
            symptom, obs,
            status=StateStatus.RESOLVED,
            followup_relevant=False,
            reason="R8 symptom resolved",
        ):
            return [symptom]
        return []

    # -- R4 : activity completed ---------------------------------------------------

    def _r4_completed(self, obs: Observation, states: List[State]) -> List[State]:
        plan = self._target_plan(obs, states)
        if plan is not None:
            if self._mutate(plan, obs, status=StateStatus.COMPLETED, reason="R4 plan completed"):
                return [plan]
            return []
        existing = self._latest_for_key_any_category(states, obs.key)
        if existing is not None and existing.status not in StateStatus.TERMINAL:
            if obs.observed_at < existing.last_observed_at:
                return []
            if self._mutate(existing, obs, status=StateStatus.COMPLETED, reason="R4 activity re-completed"):
                return [existing]
            return []
        if self._is_stale_creation(obs, states, obs.key):
            return []
        relevant_until = lifecycle.relevant_until_for_activity(obs.observed_at)
        valid_until = obs.observed_at + timedelta(hours=lifecycle.TTL_DEFAULT_HOURS)
        category = obs.category or "activity"
        new = self._create(
            obs, category, obs.key, obs.value or "completed", StateStatus.COMPLETED,
            Certainty.CONFIRMED, obs.observed_at, valid_until, relevant_until, False, 2,
        )
        self._apply(new, self._transition(new, new.status, "R4 activity completed (recent)", obs))
        return [new]

    # -- R9 : explicit user description supersedes inference -------------------------

    def _apply_r9(self, obs: Observation, states: List[State]) -> List[State]:
        changed: List[State] = []
        for s in states:
            if s.status in StateStatus.TERMINAL:
                continue
            if s.certainty in (Certainty.INFERRED, Certainty.ESTIMATED) and s.key == obs.key:
                if obs.observed_at < s.last_observed_at:
                    continue
                if self._mutate(
                    s, obs,
                    certainty=Certainty.CONFIRMED,
                    value=obs.value or s.value,
                    reason="R9 explicit user description supersedes inference",
                ):
                    changed.append(s)
        return changed


def _resolve_handler(rec: Reconciler, obs: Observation, states: List[State]) -> List[State]:
    if obs.type == ObservationType.RESOLVE or (
        obs.type == ObservationType.SYMPTOM and obs.value in ("improving", "resolved")
    ):
        if obs.value == "resolved":
            return rec._r8_resolved(obs, states)
        return rec._r7_improving(obs, states)
    return rec._r_symptom_new(obs, states)


_RULE_HANDLERS: Dict[str, object] = {
    ObservationType.AWAKE: lambda rec, obs, states: rec._r1_awake(obs, states),
    ObservationType.SLEEP: lambda rec, obs, states: rec._r2_sleep(obs, states),
    ObservationType.CANCEL: lambda rec, obs, states: rec._r3_cancel(obs, states),
    ObservationType.PLAN: lambda rec, obs, states: rec._r_plan_new(obs, states),
    ObservationType.ACTIVITY: lambda rec, obs, states: rec._r4_completed(obs, states),
    ObservationType.SYMPTOM: _resolve_handler,
    ObservationType.RESOLVE: _resolve_handler,
    # ObservationType.STATE is intentionally unmapped: conservative principle.
}
