"""Inference Engine (V2 §7-§9).

Input : Observation + current Canonical State + Ontology + time
Output: TransitionIntent[] — never writes the database.

Phase 1 implements the interaction vertical slice (V2 §20):

    user interaction → strong evidence of awake
    awake INCOMPATIBLE sleeping
    → SUPERSEDE sleeping + ESTABLISH/UPDATE awake (confirmation only —
      NEVER claims a precise wake time, V2-T6).

Phase 2 (migration of the V1 business rules, V2 §22) adds the plan slice:

    plan / cancel / activity(plan completion) observations → PLAN_LIFECYCLE
    intents: ESTABLISH (tentative/planned), UPDATE (planned/cancelled/
    completed), SUPERSEDE+ESTABLISH (reschedule to a new window).

Every other observation returns NO intents; the Reconciler then falls back
to the V1 rule handlers (remaining rules stay alive during migration,
V2 §14/§18).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import List, Optional, Tuple

from . import lifecycle, ontology
from .models import (
    Certainty,
    IntentAction,
    Observation,
    ObservationType,
    SourceType,
    State,
    StateStatus,
    TransitionIntent,
    utc_now,
)

logger = logging.getLogger(__name__)

# Value marker for awake states established by interaction evidence (no
# wake-time claim). Explicit user language upgrades it back to "awake".
AWAKE_CONFIRMED = "awake_confirmed"

_PLAN_STATUSES = (StateStatus.TENTATIVE, StateStatus.PLANNED, StateStatus.PENDING)


class InferenceEngine:
    """Produces TransitionIntents from observations. Stateless and
    side-effect free (pure function over its inputs + injected clock)."""

    def __init__(self, now_fn=utc_now):
        self._now_fn = now_fn

    def _now(self):
        return self._now_fn()

    def infer(
        self, obs: Observation, states: List[State]
    ) -> Tuple[List[TransitionIntent], bool]:
        """Return (intents, handled). handled=True means this observation is
        owned by the inference path and the V1 rule handlers must be
        skipped even if the intents are empty or all blocked."""
        if obs.source.type == SourceType.ASSISTANT_QUESTION:
            # S1 (also enforced in Reconciler.apply before this gate):
            # assistant content never creates or changes user state.
            return [], False
        if ontology.is_interaction_observation(obs):
            return self._infer_interaction(obs, states), True
        if obs.type in (ObservationType.PLAN, ObservationType.CANCEL):
            return self._infer_plan(obs, states), True
        if obs.type in (ObservationType.SYMPTOM, ObservationType.RESOLVE):
            # owned in the next migration step; V1 handlers still run
            return [], False
        if obs.type == ObservationType.ACTIVITY:
            # partial ownership: only the plan-completion half. When no
            # active plan is targeted, V1 R4 keeps creating activity states.
            intents = self._infer_plan_completion(obs, states)
            if intents:
                return intents, True
            return [], False
        return [], False

    # -- phase 1 slice: interaction → awake -----------------------------------

    def _infer_interaction(self, obs: Observation, states: List[State]) -> List[TransitionIntent]:
        intents: List[TransitionIntent] = []
        evidence = [f"{obs.event_id}:{obs.observation_index}"]

        sleeping_states = [
            s for s in states
            if s.category == "sleep" and s.key == "sleeping"
            and s.status not in StateStatus.TERMINAL
        ]

        # Evidence precedence (V2 §10.3): behavioral inference is the
        # WEAKEST evidence. An explicit sleep claim at the same-or-later
        # semantic time (e.g. "我睡了" in this very event) wins — no awake
        # inference may contradict it. This also blocks delayed interactions
        # from superseding newer sleep (V2-T5).
        if any(s.last_observed_at >= obs.observed_at for s in sleeping_states):
            logger.debug(
                "interaction inference blocked: explicit sleep claim at >= "
                "%s owns the state", obs.observed_at.isoformat(),
            )
            return intents

        # INCOMPATIBLE(awake, sleeping): any active sleep state must go.
        for state in sleeping_states:
            intents.append(
                TransitionIntent(
                    action=IntentAction.SUPERSEDE,
                    reason="interactive_activity implies current wakefulness (V2 ontology)",
                    evidence=evidence,
                    target_state_id=state.state_id,
                    target_category=state.category,
                    target_key=state.key,
                )
            )

        # Awake: confirm presence without claiming a wake time (V2-T6).
        awake = next(
            (
                s for s in states
                if s.category == "sleep" and s.key == "awake"
                and s.status not in StateStatus.TERMINAL
            ),
            None,
        )
        if awake is None:
            intents.append(
                TransitionIntent(
                    action=IntentAction.ESTABLISH,
                    reason="interactive_activity implies current wakefulness (V2 ontology)",
                    evidence=evidence,
                    category="sleep",
                    key="awake",
                    value=AWAKE_CONFIRMED,
                    status=StateStatus.ACTIVE,
                    certainty=Certainty.OBSERVED,
                    snapshot_priority=4,
                )
            )
        else:
            intents.append(
                TransitionIntent(
                    action=IntentAction.UPDATE,
                    reason="re-confirmed awake by new interactive activity (V2)",
                    evidence=evidence,
                    target_state_id=awake.state_id,
                    target_category=awake.category,
                    target_key=awake.key,
                    # no semantic change — pure confirmation refresh (touch)
                    value="",
                    status="",
                    certainty="",
                )
            )
        return intents

    # -- phase 2 slice: plans (PLAN_LIFECYCLE, V1 R3/R5/R6 → ontology) --------

    def _plan_target(self, obs: Observation, states: List[State]) -> Optional[State]:
        plans = [
            s for s in states
            if s.category == "planning" and s.status in _PLAN_STATUSES
        ]
        if obs.key:
            for s in plans:
                if s.key == obs.key:
                    return s
            return None
        return max(plans, key=lambda s: s.updated_at) if plans else None

    @staticmethod
    def _latest_plan_for_key(states: List[State], key: str) -> Optional[State]:
        matches = [s for s in states if s.category == "planning" and s.key == key]
        if not matches:
            return None
        return max(matches, key=lambda s: s.updated_at)

    def _plan_establish(self, obs: Observation, evidence: List[str],
                        valid_until, relevant_until, reason: str) -> TransitionIntent:
        return TransitionIntent(
            action=IntentAction.ESTABLISH,
            reason=reason,
            evidence=evidence,
            category="planning",
            key=obs.key,
            value=obs.value or obs.key,
            status=(
                StateStatus.TENTATIVE
                if obs.certainty == Certainty.TENTATIVE
                else StateStatus.PLANNED
            ),
            certainty=obs.certainty,
            valid_from=obs.observed_at,
            valid_until=valid_until,
            relevant_until=relevant_until,
            snapshot_priority=3,
        )

    def _infer_plan(self, obs: Observation, states: List[State]) -> List[TransitionIntent]:
        intents: List[TransitionIntent] = []
        evidence = [f"{obs.event_id}:{obs.observation_index}"]

        if obs.type == ObservationType.CANCEL:
            plan = self._plan_target(obs, states)
            if plan is None:
                logger.info("R3: cancel with no active plan target; observation-only")
                return intents
            intents.append(
                TransitionIntent(
                    action=IntentAction.UPDATE,
                    reason="R3 plan cancelled (PLAN_LIFECYCLE)",
                    evidence=evidence,
                    target_state_id=plan.state_id,
                    target_category=plan.category,
                    target_key=plan.key,
                    status=StateStatus.CANCELLED,
                    relevant_until=self._now() + timedelta(
                        hours=lifecycle.CANCEL_RELEVANT_HOURS
                    ),
                    followup_relevant=False,
                )
            )
            return intents

        # plan create / confirm / reschedule (R5 / R6 semantics).
        existing = self._latest_plan_for_key(states, obs.key)
        if existing is not None and obs.observed_at < existing.last_observed_at:
            return intents  # stale: newer evidence already owns this key

        if existing is not None and existing.status in _PLAN_STATUSES:
            _, new_until, new_relevant = lifecycle.semantic_window(
                obs.time_expression, obs.observed_at, self._now()
            )
            if new_until != existing.valid_until:
                # R6 reschedule: a new window supersedes the old episode.
                # ESTABLISH must come FIRST so the reconciler's stale-
                # creation guard does not see the old row rewritten by this
                # same observation.
                intents.append(
                    self._plan_establish(
                        obs, evidence, new_until, new_relevant,
                        "R6 reschedule: new window (PLAN_LIFECYCLE)",
                    )
                )
                intents.append(
                    TransitionIntent(
                        action=IntentAction.SUPERSEDE,
                        reason="R6 reschedule: old window superseded (PLAN_LIFECYCLE)",
                        evidence=evidence,
                        target_state_id=existing.state_id,
                        target_category=existing.category,
                        target_key=existing.key,
                    )
                )
                return intents
            if (
                obs.certainty in (Certainty.PLANNED, Certainty.CONFIRMED)
                and existing.status == StateStatus.TENTATIVE
            ):
                # R5: tentative → planned (same window).
                intents.append(
                    TransitionIntent(
                        action=IntentAction.UPDATE,
                        reason="R5 tentative plan confirmed to planned (PLAN_LIFECYCLE)",
                        evidence=evidence,
                        target_state_id=existing.state_id,
                        target_category=existing.category,
                        target_key=existing.key,
                        status=StateStatus.PLANNED,
                        certainty=obs.certainty,
                    )
                )
                return intents
            # re-affirm without semantic change: no intents (pure touch)
        else:
            _, valid_until, relevant_until = lifecycle.semantic_window(
                obs.time_expression, obs.observed_at, self._now()
            )
            intents.append(
                self._plan_establish(
                    obs, evidence, valid_until, relevant_until,
                    "plan created (PLAN_LIFECYCLE)",
                )
            )

        # R9 (evidence precedence): explicit CONFIRMED language upgrades an
        # inferred/estimated state of the same key.
        if obs.certainty == Certainty.CONFIRMED:
            for s in states:
                if s.status in StateStatus.TERMINAL:
                    continue
                if s.certainty in (Certainty.INFERRED, Certainty.ESTIMATED) and s.key == obs.key:
                    intents.append(
                        TransitionIntent(
                            action=IntentAction.UPDATE,
                            reason="R9 explicit user description supersedes inference",
                            evidence=evidence,
                            target_state_id=s.state_id,
                            target_category=s.category,
                            target_key=s.key,
                            certainty=Certainty.CONFIRMED,
                            value=obs.value or s.value,
                        )
                    )
        return intents

    def _infer_plan_completion(
        self, obs: Observation, states: List[State]
    ) -> List[TransitionIntent]:
        """R4's plan half: an activity observation with an active plan
        target completes the plan (PLAN_LIFECYCLE → completed)."""
        plan = self._plan_target(obs, states)
        if plan is None:
            return []  # not owned — V1 R4 keeps creating activity states
        return [
            TransitionIntent(
                action=IntentAction.UPDATE,
                reason="R4 plan completed (PLAN_LIFECYCLE)",
                evidence=[f"{obs.event_id}:{obs.observation_index}"],
                target_state_id=plan.state_id,
                target_category=plan.category,
                target_key=plan.key,
                status=StateStatus.COMPLETED,
            )
        ]

    # -- phase 2 slice: symptoms (SYMPTOM_LIFECYCLE, V1 R7/R8) ----------------

    def _infer_symptom(self, obs: Observation, states: List[State]) -> List[TransitionIntent]:
        """Migrated in the next step; during migration the V1 handlers
        still own symptom observations."""
        return []