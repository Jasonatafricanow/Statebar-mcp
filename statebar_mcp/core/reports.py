"""Doses taken and workouts done, as the user reported them (O40).

Like sleep, these are the user's real state: the host's model writes them in the <state> block, statebar
checks them against the user's own words, keeps them, and hands them one way to the health plugin
(nutrition-tracker), which computes "when can the next dose be taken" and the week's exercise.

  {"type":"dose","who"?,"medication":"美林","taken_at"?:"2026-11-20T08:00:00+08:00","amount"?:"5ml"}
  {"type":"workout","who"?,"activity":"游泳","duration_min"?:40,"date"?:"2026-11-19"}

Checks (the model must not put words in the user's mouth):
- the medicine / activity name appears in the user's message;
- a dose time is either the message time (said as just now) or a clock time the user said;
- a workout length is a duration the user said (minutes or hours);
- a workout day is the message day, or the day before when the user said 昨天.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from .sleep_episodes import LOCAL_TZ, _clock_stated, _num, _NUM, duration_mentions

KINDS = ("dose", "workout")
_MINUTES_RE = re.compile(rf"({_NUM}|[0-9]{{2,3}})\s*(?:分钟|min\b|分\b)")
_MAX_DOSE_AGE = timedelta(hours=36)


def minute_mentions(text: str) -> set:
    out = {m for m in duration_mentions(text)}
    for m in _MINUTES_RE.finditer(text or ""):
        n = _num(m.group(1)) if not m.group(1).isdigit() else int(m.group(1))
        if n and 0 < n <= 600:
            out.add(n)
    return out


def _said(name: str, text: str) -> bool:
    n = re.sub(r"\s+", "", name or "")
    return bool(n) and n in re.sub(r"\s+", "", text or "")


@dataclass
class Report:
    subject_id: str
    kind: str
    event_id: str
    index: int
    at: datetime
    payload: Dict[str, Any]
    source_text: str
    source_message_id: str

    @property
    def report_id(self) -> str:
        return hashlib.sha256(f"{self.subject_id}|{self.kind}|{self.event_id}|{self.index}".encode()).hexdigest()[:32]

    def to_dict(self) -> Dict[str, Any]:
        return {"report_id": self.report_id, "subject_id": self.subject_id, "kind": self.kind,
                "at": self.at.isoformat(), **self.payload}


def validate(raw: Dict[str, Any], *, subject_id: str, event_id: str, index: int, text: str,
             observed_at: datetime, message_id: str = "") -> Tuple[Optional[Report], str]:
    kind = raw.get("type")
    if kind == "dose":
        med = str(raw.get("medication") or "").strip()[:60]
        if not med:
            return None, "dose needs the medicine's name"
        if not _said(med, text):
            return None, f"medicine {med!r} is not in the user's words"
        at = observed_at
        if raw.get("taken_at"):
            try:
                at = datetime.fromisoformat(str(raw["taken_at"]))
            except ValueError:
                return None, "taken_at must be ISO time with offset"
            if at.tzinfo is None:
                return None, "taken_at needs a UTC offset"
            if not _clock_stated(at, text):
                return None, "dose time was not said by the user"
            if at > observed_at + timedelta(minutes=5) or observed_at - at > _MAX_DOSE_AGE:
                return None, "dose time must be within the last day and a half"
        payload = {"medication": med}
        if raw.get("amount"):
            payload["amount"] = str(raw["amount"])[:40]
        return Report(subject_id, kind, event_id, index, at, payload, text, message_id), ""
    if kind == "workout":
        activity = str(raw.get("activity") or "").strip()[:40]
        if not activity:
            return None, "workout needs the activity"
        if not _said(activity, text):
            return None, f"activity {activity!r} is not in the user's words"
        local = observed_at.astimezone(LOCAL_TZ).date()
        day = local
        if raw.get("date"):
            try:
                day = datetime.fromisoformat(str(raw["date"])).date()
            except ValueError:
                return None, "date must be YYYY-MM-DD"
            if day != local and not (day == local - timedelta(days=1) and "昨" in (text or "")):
                return None, "workout day must be today, or yesterday when the user said so"
        payload: Dict[str, Any] = {"activity": activity, "date": day.isoformat()}
        if raw.get("duration_min") is not None:
            try:
                minutes = int(raw["duration_min"])
            except (TypeError, ValueError):
                return None, "duration_min must be a number"
            if minutes not in minute_mentions(text):
                return None, "workout length was not said by the user"
            payload["duration_min"] = minutes
        return Report(subject_id, kind, event_id, index, observed_at, payload, text, message_id), ""
    return None, "unknown report type"
