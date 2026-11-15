"""Current Snapshot builder (派单总纲 §6.2).

Every turn the snapshot is recomputed from scratch (never accumulates):
BASE = 5-8 most important states (fixed底座), plus 0-3 query-aware items.
Lazy expiration (R10) runs first, so expired plans are excluded on read.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Dict, List

from .lifecycle import SUBJECTIVE_LOOKBACK_HOURS, lazy_expire, subjective_date
from .models import Snapshot, SnapshotItem, State, StateStatus
from .store import SQLiteStore

BASE_CAP = 8
QUERY_CAP = 3
MAX_LINES = BASE_CAP + QUERY_CAP

# Display names for canonical keys (snapshot keeps English, mirrors §6.2).
_KEY_LABELS = {
    "awake": "awake",
    "sleeping": "sleeping",
    "stomach_pain": "stomach discomfort",
    "abdominal_pain": "abdominal discomfort",
    "headache": "headache",
    "sore_throat": "sore throat",
    "toothache": "toothache",
    "back_pain": "back pain",
    "leg_pain": "leg pain",
    "foot_pain": "foot pain",
    "hand_pain": "hand pain",
    "knee_pain": "knee pain",
    "eye_discomfort": "eye discomfort",
    "medication": "medication",
    "swimming": "swam",
    "running": "ran",
    "calligraphy": "calligraphy",
    "work": "work",
    "meal": "meal",
}

# Chinese query aliases → keys, so a Chinese query can surface states whose
# canonical keys are English slugs.
_QUERY_ALIASES: Dict[str, str] = {
    "书法": "calligraphy",
    "写字": "calligraphy",
    "游泳": "swimming",
    "胃": "stomach_pain",
    "肚子": "abdominal_pain",
    "头疼": "headache",
    "头痛": "headache",
    "睡": "sleep",
    "醒": "awake",
    "药": "medication",
    "吃饭": "meal",
    "饭": "meal",
    "运动": "exercise",
    "工作": "work",
}

# Terminal states that may briefly stay conversational (relevant_until).
_RECENT_TERMINAL = (StateStatus.COMPLETED, StateStatus.CANCELLED, StateStatus.RESOLVED)


def _hhmm(dt: datetime) -> str:
    local = dt.astimezone()
    return f"{local.hour:02d}:{local.minute:02d}"


def _window_note(state: State, today) -> str:
    """Human note for the semantic window of a plan, relative to the user's TODAY.

    ``today`` is the subjective date (it changes when the user sleeps, not at
    midnight). The window is anchored on the day the plan was mentioned, so the
    label must be computed against the current day: a plan for "tomorrow" ends
    at 23:59 of the next day and used to render as "tonight"; once that day
    arrives it is "today". "today" and "tonight" share the same window (until
    23:59), so a same-day end-of-day window renders as "today", true for both.
    """
    if state.valid_until is None:
        return ""
    local = state.valid_until.astimezone()
    days_ahead = (local.date() - today).days
    if days_ahead < 0:
        return ""
    part = {(17, 0): "afternoon", (12, 0): "morning", (19, 0): "evening"}.get((local.hour, local.minute), "")
    if local.hour == 23:
        part = "today"
    if not part:
        return ""  # a plain TTL window (e.g. "tonight" said past midnight) carries no day label
    if days_ahead == 0:
        return part
    day = "tomorrow" if days_ahead == 1 else local.strftime("%m-%d")
    return day if part == "today" else f"{day} {part}"


class SnapshotBuilder:
    def __init__(self, store: SQLiteStore, now_fn):
        self.store = store
        self._now_fn = now_fn

    def build(self, subject_id: str, query: str = "") -> Snapshot:
        now = self._now_fn()

        # R10 lazy expiration before reading
        states = self.store.get_states(subject_id)
        transitions, mutated = lazy_expire(states, now)
        for tr in transitions:
            self.store.record_transition(tr)
        for st in mutated:
            self.store.insert_state(st)

        # The user's day changes when they sleep, not at midnight.
        activity = self.store.get_activity_times(subject_id, now - timedelta(hours=2 * SUBJECTIVE_LOOKBACK_HOURS))

        def subjective(at: datetime):
            return subjective_date(at, activity, states)

        today = subjective(now)

        candidates: List[State] = [
            s
            for s in states
            if s.status not in StateStatus.TERMINAL
            or (
                s.status in _RECENT_TERMINAL
                and s.relevant_until is not None
                and s.relevant_until > now
            )
        ]

        def rank(state: State) -> tuple:
            return (
                1 if state.followup_relevant else 0,
                state.snapshot_priority,
                state.updated_at,
            )

        ranked = sorted(candidates, key=rank, reverse=True)
        base = ranked[:BASE_CAP]
        base_ids = {id(s) for s in base}
        base_items = [self._render(s, today, subjective) for s in base]

        query_items: List[SnapshotItem] = []
        if query:
            scored = [
                (self._query_score(query, s), s)
                for s in ranked
                if id(s) not in base_ids
            ]
            for score, state in sorted(scored, key=lambda p: -p[0]):
                if score <= 0 or len(query_items) >= QUERY_CAP:
                    break
                query_items.append(self._render(state, today, subjective))

        items = base_items + query_items
        calendar = now.astimezone().date()
        day_line = f"- user's day: {today.isoformat()}"
        if today != calendar:
            day_line += f" (calendar already {calendar.isoformat()}; not slept yet, still the same day for the user)"
        lines = ["CURRENT USER STATE", day_line]
        if not items:
            lines.append("(no current states)")
        for item in items[:MAX_LINES]:
            lines.append(item.render())

        return Snapshot(
            subject_id=subject_id,
            text="\n".join(lines),
            items=items,
            base_items=base_items,
            query_items=query_items,
            user_day=today.isoformat(),
            calendar_day=calendar.isoformat(),
        )

    # -- rendering ------------------------------------------------------------

    def _render(self, state: State, today, subjective) -> SnapshotItem:
        key, category, status = state.key, state.category, state.status

        if category == "sleep" and key == "awake":
            if state.value == "awake_confirmed":
                # V2-T6: interaction evidence confirms wakefulness but does
                # NOT claim a wake time — render the confirmation time only.
                text = f"awake confirmed at ~{_hhmm(state.last_observed_at)}"
            else:
                text = f"awake since ~{_hhmm(state.valid_from)}"
        elif category == "sleep" and key == "sleeping":
            text = "preparing to sleep" if state.value == "preparing_sleep" else "sleeping"
        elif key == "medication":
            text = "medication taken recently"
        elif category == "planning":
            label = _KEY_LABELS.get(key, key)
            window = _window_note(state, today)
            prefix = f"{window} " if window else ""
            text = f"{prefix}{label} plan: {status}"
        elif category == "activity":
            label = _KEY_LABELS.get(key, key)
            when = "today" if subjective(state.valid_from) == today else "recently"
            text = f"{label} {when}" if status == StateStatus.COMPLETED else f"{label}: {status}"
        elif category == "health":
            label = _KEY_LABELS.get(key, key)
            text = f"{label}: {status}"
        else:
            label = _KEY_LABELS.get(key, key)
            text = f"{label}: {status}" if status else label
        return SnapshotItem(text=text, category=category, key=key, value=state.value,
                            status=status, source_state_id=state.state_id)

    def _query_score(self, query: str, state: State) -> int:
        score = 0
        hay = f"{state.key} {state.category} {state.value}".lower()
        for alias, target in _QUERY_ALIASES.items():
            if alias in query:
                if target == state.key or target in hay:
                    score += 2
        for token in re.findall(r"[a-z0-9_]+", query.lower()):
            if len(token) >= 3 and token in hay:
                score += 1
        return score
