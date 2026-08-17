"""Semantic lifecycle (派单总纲 §3.4, §10-R10).

- Semantic windows beat fixed TTLs: "afternoon" is 13:00-17:00 local time.
- TTL only as a fallback.
- Lazy expiration: pending/tentative states whose window has passed are
  expired AT READ TIME (snapshot), never by cron.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import List, Tuple

from .models import State, StateStatus, StateTransition, TimeExpr

AFTERNOON = (time(13, 0), time(17, 0))
MORNING = (time(5, 0), time(12, 0))
EVENING = (time(17, 0), time(19, 0))
TONIGHT_START = time(18, 0)
DAY_END = time(23, 59, 59, 999999)

# Fallback TTLs (hours) — used only when no semantic window applies.
TTL_DEFAULT_HOURS = 24.0
TTL_NOW_HOURS = 12.0
TTL_AWAKE_HOURS = 16.0
TTL_SLEEP_HOURS = 12.0

# A cancelled plan stays briefly conversational (recently-cancelled bucket).
CANCEL_RELEVANT_HOURS = 6.0


def _local_tz(now: datetime) -> timezone:
    return now.astimezone().tzinfo or timezone.utc


def _day_bounds(anchor: datetime) -> Tuple[datetime, datetime]:
    """Start and end of the LOCAL calendar day containing ``anchor``."""
    local = anchor.astimezone()
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(microseconds=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def semantic_window(
    time_expression: str,
    observed_at: datetime,
    now: datetime,
    hours_ttl: float = TTL_DEFAULT_HOURS,
) -> Tuple[datetime, datetime, datetime]:
    """Return (valid_from, valid_until, relevant_until) in UTC.

    Windows are anchored on the LOCAL day of ``observed_at`` (the semantic
    time the caller passed), so a message observed in the morning about the
    afternoon resolves to that day's 13:00-17:00.
    """
    tz = _local_tz(now)
    local = observed_at.astimezone(tz)
    day_start, day_end = _day_bounds(observed_at)

    def at(t: time) -> datetime:
        return local.replace(
            hour=t.hour, minute=t.minute, second=t.second, microsecond=t.microsecond
        ).astimezone(timezone.utc)

    expr = (time_expression or TimeExpr.UNSPECIFIED).lower()

    if expr == TimeExpr.AFTERNOON:
        start, end = at(AFTERNOON[0]), at(AFTERNOON[1])
        return observed_at, end, end
    if expr == TimeExpr.MORNING:
        start, end = at(MORNING[0]), at(MORNING[1])
        return observed_at, end, end
    if expr == TimeExpr.EVENING:
        start, end = at(EVENING[0]), at(EVENING[1])
        return observed_at, end, end
    if expr == TimeExpr.TONIGHT:
        start, end = at(TONIGHT_START), day_end
        return observed_at, end, end
    if expr == TimeExpr.TODAY:
        return observed_at, day_end, day_end
    if expr == TimeExpr.TOMORROW:
        tomorrow_start = day_start + timedelta(days=1)
        tomorrow_end = tomorrow_start + timedelta(days=1) - timedelta(microseconds=1)
        return observed_at, tomorrow_end, tomorrow_end
    if expr == TimeExpr.NOW:
        valid_until = observed_at + timedelta(hours=hours_ttl)
        return observed_at, valid_until, valid_until
    # unspecified → pure TTL fallback
    valid_until = observed_at + timedelta(hours=hours_ttl)
    return observed_at, valid_until, valid_until


def relevant_until_for_activity(observed_at: datetime) -> datetime:
    """Completed activities stay conversational until the end of their day."""
    _, day_end = _day_bounds(observed_at)
    return day_end


def lazy_expire(states: List[State], now: datetime) -> Tuple[List[StateTransition], List[State]]:
    """R10: expire pending/tentative states whose semantic window has passed.

    Returns (transitions_to_record, mutated_states). The caller persists both
    and the snapshot builder then excludes expired states.
    """
    transitions: List[StateTransition] = []
    mutated: List[State] = []
    for state in states:
        if state.status not in (StateStatus.TENTATIVE, StateStatus.PLANNED, StateStatus.PENDING):
            continue
        if state.valid_until is not None and state.valid_until < now:
            transitions.append(
                StateTransition(
                    state_id=state.state_id,
                    subject_id=state.subject_id,
                    from_status=state.status,
                    to_status=StateStatus.EXPIRED,
                    reason="R10 semantic window ended (lazy expiration)",
                    created_at=now,
                )
            )
            state.status = StateStatus.EXPIRED
            state.updated_at = now
            mutated.append(state)
    return transitions, mutated
