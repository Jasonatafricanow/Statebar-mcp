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

# Subjective day: the user's "today" changes when they sleep, not at midnight.
# Without an explicit sleep record, a silence of REST_GAP_HOURS that ends no
# earlier than WAKE_EARLIEST counts as a night's rest (a late return at 00:30
# after an evening out is not a new day).
REST_GAP_HOURS = 4.0
WAKE_EARLIEST = time(5, 0)
SUBJECTIVE_LOOKBACK_HOURS = 36.0


def subjective_date(at: datetime, activity_times: List[datetime], states: List[State], tz=None):
    """Local date the user's current day belongs to: the date on which their most
    recent rest ended. Rest = an explicit sleep followed by an awake state, or a
    long silence between interactions that ends in the morning. Past midnight
    without sleeping, this is still the previous date. Falls back to the
    calendar date when there is no evidence in the lookback window."""
    tz = tz or at.astimezone().tzinfo
    horizon = at - timedelta(hours=SUBJECTIVE_LOOKBACK_HOURS)
    ends: List[datetime] = []
    sleeps = sorted(s.valid_from for s in states if s.category == "sleep" and s.key == "sleeping")
    wakes = sorted(s.valid_from for s in states if s.category == "sleep" and s.key == "awake")
    for start in sleeps:
        woke = next((w for w in wakes if w > start), None)
        if woke is not None and woke <= at:
            ends.append(woke)
    times = sorted(t for t in activity_times if horizon <= t <= at)
    gap = timedelta(hours=REST_GAP_HOURS)
    for prev, cur in zip(times, times[1:] + [at]):
        if cur - prev >= gap and cur.astimezone(tz).time() >= WAKE_EARLIEST:
            ends.append(cur)
    recent = [e for e in ends if horizon <= e <= at]
    calendar = at.astimezone(tz).date()
    if recent:
        day = max(recent).astimezone(tz).date()
    elif times:
        # No rest since the earliest interaction we can see: still that day.
        day = times[0].astimezone(tz).date()
    else:
        return calendar
    # Never more than one day behind the calendar.
    return max(day, calendar - timedelta(days=1))


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
    day_anchor=None,
) -> Tuple[datetime, datetime, datetime]:
    """Return (valid_from, valid_until, relevant_until) in UTC.

    Windows are anchored on the LOCAL day of ``observed_at`` (the semantic
    time the caller passed), so a message observed in the morning about the
    afternoon resolves to that day's 13:00-17:00.

    ``day_anchor`` is the user's subjective date (see ``subjective_date``).
    When it is earlier than the calendar date — awake past midnight, not yet
    slept — "tomorrow" means the day after the coming sleep (the calendar
    date), and "today"/"tonight" mean the night still going on.
    """
    tz = _local_tz(now)
    local = observed_at.astimezone(tz)
    day_start, day_end = _day_bounds(observed_at)
    overnight = day_anchor is not None and day_anchor < local.date()

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
    if overnight and expr in (TimeExpr.TONIGHT, TimeExpr.TODAY):
        # Still the previous subjective day: the night that is going on now.
        valid_until = observed_at + timedelta(hours=TTL_NOW_HOURS)
        return observed_at, valid_until, valid_until
    if overnight and expr == TimeExpr.TOMORROW:
        # "Tomorrow" said before sleeping = the calendar day that has already begun.
        return observed_at, day_end, day_end
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
        # R10 fix (2026-08-18): ACTIVE/IMPROVING states were never expired because
        # this filter skipped them — hunger/headache/hangover symptoms stayed active
        # forever. TTL/semantic windows apply to every status with a valid_until.
        if state.status in (StateStatus.EXPIRED,):
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
