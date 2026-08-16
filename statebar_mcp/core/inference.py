"""Inference Engine (V2 §7-§9).

Input : Observation + current Canonical State + Ontology + time
Output: TransitionIntent[] — never writes the database.

Phase 1 implements exactly ONE vertical slice (V2 §20):

    user interaction → strong evidence of awake
    awake INCOMPATIBLE sleeping
    → SUPERSEDE sleeping + ESTABLISH/UPDATE awake (confirmation only —
      NEVER claims a precise wake time, V2-T6).

Every other observation returns NO intents; the Reconciler then falls back
to the V1 rule handlers (R1-R10 stay alive during migration, V2 §14/§18).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import List, Tuple

from . import lifecycle, ontology
from .models import (
    Certainty,
    IntentAction,
    Observation,
    State,
    StateStatus,
    TransitionIntent,
)

logger = logging.getLogger(__name__)

# Value marker for awake states established by interaction evidence (no
# wake-time claim). Explicit user language upgrades it back to "awake".
AWAKE_CONFIRMED = "awake_confirmed"


class InferenceEngine:
    """Produces TransitionIntents from observations. Stateless and
    side-effect free (pure function over its inputs)."""

    def infer(
        self, obs: Observation, states: List[State]
    ) -> Tuple[List[TransitionIntent], bool]:
        """Return (intents, handled). handled=True means this observation is
        owned by the inference path and the V1 rule handlers must be
        skipped even if the intents are empty or all blocked."""
        if ontology.is_interaction_observation(obs):
            return self._infer_interaction(obs, states), True
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
