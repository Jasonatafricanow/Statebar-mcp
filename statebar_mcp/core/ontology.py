"""State Ontology (V2 §10) — stable relations between states.

The ontology describes WHAT state relationships hold, never what a specific
sentence means. Phase 1 freezes only the sleep/awake slice; the rest of the
relations are declared for the later migration phases (V2 §22).

Content so far:
- INCOMPATIBLE pairs (mutually exclusive states — strong evidence of one
  supersedes the other);
- evidence rules: which observations count as strong evidence for which
  state (interaction → awake);
- lifecycle definitions (declarative; phase 1 enforces sleep only).
"""

from __future__ import annotations

from typing import List, Tuple

from .models import Certainty, Observation, SourceType, State, StateStatus

# (category, key) pairs that cannot both be current.
# Phase 1: awake vs sleeping.
INCOMPATIBLE: List[Tuple[Tuple[str, str], Tuple[str, str]]] = [
    (("sleep", "awake"), ("sleep", "sleeping")),
]


def is_incompatible(a: Tuple[str, str], b: Tuple[str, str]) -> bool:
    return (a, b) in INCOMPATIBLE or (b, a) in INCOMPATIBLE


def incompatible_pairs_of(category: str, key: str) -> List[Tuple[str, str]]:
    out = []
    for a, b in INCOMPATIBLE:
        if a == (category, key):
            out.append(b)
        elif b == (category, key):
            out.append(a)
    return out


# ---------------------------------------------------------------------------
# Evidence rules (V2 §4/§6/§10.3)
# ---------------------------------------------------------------------------


def is_interaction_observation(obs: Observation) -> bool:
    """The user's real-time behavior itself: a user-authored interaction
    happened (message/voice/action). Determined by the SYSTEM, never by
    language analysis.

    The trusted source is required: only observations the system itself
    issued with ``source.type == interaction`` qualify as behavioral
    evidence. A language-extracted observation with the same shape but a
    different source (e.g. assistant_question, conversation) is NOT the
    user's behavior and must never establish user state."""
    return (
        obs.source.type == SourceType.INTERACTION
        and obs.type == "activity"
        and obs.category == "presence"
        and obs.key == "interactive_activity"
        and obs.certainty == Certainty.OBSERVED
    )


# ---------------------------------------------------------------------------
# Lifecycle definitions (V2 §10.2). Declarative; the Inference Engine
# consults them. Phase 1 enforces the sleep lifecycle via intents.
# ---------------------------------------------------------------------------

SLEEP_LIFECYCLE = (StateStatus.ACTIVE, StateStatus.SUPERSEDED)  # preparing→sleeping→awake(supersede)
PLAN_LIFECYCLE = (
    StateStatus.TENTATIVE, StateStatus.PLANNED, StateStatus.ACTIVE,
    StateStatus.COMPLETED, StateStatus.CANCELLED, StateStatus.EXPIRED,
    StateStatus.SUPERSEDED,  # reschedule: the old window episode is superseded
)
SYMPTOM_LIFECYCLE = (StateStatus.ACTIVE, StateStatus.IMPROVING, StateStatus.RESOLVED)


def validate_transition(state: State, to_status: str) -> bool:
    """Transition legality gate (V2 §13). Conservative: unknown transitions
    are allowed only if they appear in a declared lifecycle."""
    if state.status == to_status:
        return True
    if state.category == "sleep":
        return to_status in SLEEP_LIFECYCLE
    if state.category == "planning":
        return to_status in PLAN_LIFECYCLE
    if state.category == "health" and state.key != "medication":
        return to_status in SYMPTOM_LIFECYCLE
    return True  # conservative principle: don't block unknown relations
