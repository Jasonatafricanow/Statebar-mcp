"""Retrospective sleep episodes ("七点半睡的，十点半醒了").

Current sleep/wake STATE ("我要睡了", "我刚醒") stays in the state engine. This module
records what the user reports about a sleep that already happened: when they fell
asleep, when they woke, how long. statebar is the gate, not the model:

- the host proposes an episode; times marked ``stated`` must literally appear in the
  user's own words (hour and minute, Shanghai local time), a stated duration must match
  a duration the user said — the model can never turn "凌晨四点" into 04:20;
- what the user did not say may be filled in, but only marked ``inferred``: a wake
  time from the moment the user says they just woke, a sleep start from the last chat
  before a long silence;
- overlapping reports supersede each other: stated beats inferred, then the later
  message wins; nothing is deleted, superseded episodes stay as history;
- daily totals (by wake date) go one way to the health plugin through an outbox.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .models import dt_to_iso, parse_dt

LOCAL_TZ = timezone(timedelta(hours=8))  # Asia/Shanghai, no DST

STATED = "stated"
INFERRED = "inferred"
ACTIVE = "active"
SUPERSEDED = "superseded"

MIN_EPISODE = timedelta(minutes=10)
MAX_EPISODE = timedelta(hours=20)
MAX_LOOKBACK = timedelta(hours=48)  # a live report covers the last two days at most
GAP_FOR_INFERENCE = timedelta(minutes=90)
END_SLACK = timedelta(minutes=10)

# --- reading times and durations from the user's words -----------------------

_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "俩": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_NUM = r"[0-9]{1,2}|[零〇一二两俩三四五六七八九十]{1,3}"
_CLOCK_RE = re.compile(rf"({_NUM})\s*(?:点钟?|[:：])\s*(半|一刻|三刻|[0-9]{{1,2}}"
                       r"|[零〇一二三四五六七八九十]{1,3}(?=分)|[零〇][一二三四五六七八九]|[一二三四五]?十[一二三四五六七八九]?)?")
# Chinese minutes need 分, a 十 or a leading 零: "十点一起睡" is 10:00, not 10:01.
_DURATION_RE = re.compile(rf"({_NUM})\s*(?:个)?\s*(半)?\s*(?:个)?\s*(?:小时|钟头|h\b)")
_HALF_HOUR_RE = re.compile(r"(?<![0-9零〇一二两俩三四五六七八九十个])半(?:个)?(?:小时|钟头)")
_SLEEP_WORD_RE = re.compile(r"睡|醒|起床|起来")


def _num(text: str) -> Optional[int]:
    if text.isdigit():
        return int(text)
    if not text or any(ch not in _CN for ch in text):
        return None
    if text == "十":
        return 10
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN[head] if head else 1
        ones = _CN[tail] if tail else 0
        return tens * 10 + ones
    if len(text) == 1:
        return _CN[text]
    return None


def clock_mentions(text: str) -> set:
    """(hour % 12, minute) pairs the user mentioned, e.g. 七点半 -> (7, 30)."""
    out = set()
    for m in _CLOCK_RE.finditer(text or ""):
        hour = _num(m.group(1))
        if hour is None or hour > 24:
            continue
        tail = m.group(2) or ""
        if tail == "半":
            minute = 30
        elif tail == "一刻":
            minute = 15
        elif tail == "三刻":
            minute = 45
        elif tail:
            minute = _num(tail)
            if minute is None or minute > 59:
                continue
        else:
            minute = 0
        out.add((hour % 12, minute))
    return out


def duration_mentions(text: str) -> set:
    """Durations in minutes the user mentioned, e.g. 睡了俩小时 -> 120, 三个半小时 -> 210."""
    out = set()
    for m in _DURATION_RE.finditer(text or ""):
        hours = _num(m.group(1))
        if hours is None or hours > 24:
            continue
        out.add(hours * 60 + (30 if m.group(2) else 0))
    if _HALF_HOUR_RE.search(text or ""):
        out.add(30)
    return out


def _local(dt: datetime) -> datetime:
    return dt.astimezone(LOCAL_TZ)


def _clock_stated(dt: datetime, text: str) -> bool:
    loc = _local(dt)
    return (loc.hour % 12, loc.minute) in clock_mentions(text)


# --- the episode record --------------------------------------------------------


@dataclass
class SleepEpisode:
    subject_id: str
    event_id: str
    index: int
    wake_date: str  # YYYY-MM-DD (Shanghai) the sleep ends on
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    duration_min: Optional[int] = None
    start_basis: Optional[str] = None
    end_basis: Optional[str] = None
    duration_basis: Optional[str] = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source_text: str = ""
    source_message_id: str = ""
    status: str = ACTIVE
    superseded_by: str = ""
    offset_min: int = 480  # local clock the user reported in (+8 Beijing, +2 Maputo)

    @property
    def episode_id(self) -> str:
        return hashlib.sha1(f"{self.subject_id}|{self.event_id}|{self.index}".encode()).hexdigest()[:16]

    @property
    def basis(self) -> str:
        parts = [b for b in (self.start_basis, self.end_basis, self.duration_basis) if b]
        return STATED if parts and all(b == STATED for b in parts) else INFERRED

    @property
    def minutes(self) -> Optional[int]:
        if self.start_at and self.end_at:
            return int((self.end_at - self.start_at).total_seconds() // 60)
        return self.duration_min

    def to_dict(self) -> Dict[str, Any]:
        return {
            "episode_id": self.episode_id, "subject_id": self.subject_id, "event_id": self.event_id,
            "wake_date": self.wake_date,
            "start_at": dt_to_iso(self.start_at) if self.start_at else None,
            "end_at": dt_to_iso(self.end_at) if self.end_at else None,
            "start_local": self._clock(self.start_at), "end_local": self._clock(self.end_at),
            "utc_offset_min": self.offset_min,
            "minutes": self.minutes, "basis": self.basis,
            "start_basis": self.start_basis, "end_basis": self.end_basis, "duration_basis": self.duration_basis,
            "observed_at": dt_to_iso(self.observed_at), "source_text": self.source_text,
            "status": self.status, "superseded_by": self.superseded_by,
        }

    def _clock(self, dt: Optional[datetime]) -> Optional[str]:
        if dt is None:
            return None
        return dt.astimezone(timezone(timedelta(minutes=self.offset_min))).strftime("%m-%d %H:%M")

    def rank(self) -> Tuple[int, datetime]:
        return (1 if self.basis == STATED else 0, self.observed_at)

    def overlaps(self, other: "SleepEpisode") -> bool:
        if self.start_at and self.end_at and other.start_at and other.end_at:
            return self.start_at < other.end_at and other.start_at < self.end_at
        if self.end_at and other.end_at:
            return abs(self.end_at - other.end_at) < timedelta(minutes=45)
        # A duration-only report ("睡了俩小时") restates another sleep of that wake day only
        # when the lengths agree and it was said within 3 hours of that wake; otherwise it is
        # a separate sleep (a nap), never a replacement.
        if self.wake_date != other.wake_date:
            return False
        a, b = self.minutes, other.minutes
        if a is None or b is None or abs(a - b) > 30:
            return False
        timed = self if self.end_at else other if other.end_at else None
        if timed is None:  # two duration-only reports: the same sleep if said close together
            return abs(self.observed_at - other.observed_at) <= timedelta(hours=3)
        said = other if timed is self else self
        return timedelta(0) <= said.observed_at - timed.end_at <= timedelta(hours=3)


def _opt_dt(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        return parse_dt(value)
    except (TypeError, ValueError):
        raise ValueError(f"bad time {value!r}")


def validate_episode(raw: Dict[str, Any], *, subject_id: str, event_id: str, index: int,
                     text: str, observed_at: datetime, message_id: str = "",
                     activity_times: Optional[List[datetime]] = None,
                     infer: bool = True) -> Tuple[Optional[SleepEpisode], str]:
    """Return (episode, "") or (None, reason). ``text`` is the user's own message.
    ``infer=False`` (someone else's sleep): only what the user stated; the user's own chat
    silence or "醒了" says nothing about another person."""
    if not isinstance(raw, dict):
        return None, "not an object"
    if not text or not _SLEEP_WORD_RE.search(text):
        return None, "the user's words do not talk about sleeping or waking"
    try:
        start, end = _opt_dt(raw.get("start")), _opt_dt(raw.get("end"))
    except ValueError as exc:
        return None, str(exc)
    duration = raw.get("duration_min")
    if duration not in (None, ""):
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            return None, "duration_min must be an integer"
    else:
        duration = None

    ep = SleepEpisode(subject_id=subject_id, event_id=event_id, index=index, wake_date="",
                      observed_at=observed_at, source_text=text, source_message_id=message_id)

    # Stated parts must be literally in the user's words; anything else is inferred.
    if start is not None:
        ep.start_at, ep.start_basis = start, STATED if _clock_stated(start, text) else None
        if ep.start_basis is None:
            if str(raw.get("start_basis")) == STATED:
                return None, f"sleep start {_local(start):%H:%M} is not in the user's words"
            ep.start_basis = INFERRED
    if end is not None:
        ep.end_at, ep.end_basis = end, STATED if _clock_stated(end, text) else None
        if ep.end_basis is None:
            if str(raw.get("end_basis")) == STATED:
                return None, f"wake time {_local(end):%H:%M} is not in the user's words"
            ep.end_basis = INFERRED
    if duration is not None:
        if duration in duration_mentions(text):
            ep.duration_min, ep.duration_basis = duration, STATED
        elif start is None or end is None:
            return None, f"duration {duration} min is not in the user's words"
        # with both ends known a mismatching duration is simply dropped

    # "我刚醒" with no time: the wake is (about) now.
    # Not when the user named a clock the item left out ("十二点不到醒了" said at 03:19 is not a wake at 03:19).
    if infer and ep.end_at is None and ep.duration_min is None and re.search(r"醒|起床|起来", text) \
            and not clock_mentions(text):
        ep.end_at, ep.end_basis = observed_at, INFERRED
    if ep.end_at is None and ep.start_at is None and ep.duration_min is None:
        return None, "no sleep time, wake time or duration"

    # A missing start may come from the silence before the wake (marked inferred).
    if infer and ep.start_at is None and ep.duration_min is None and ep.end_at is not None and activity_times:
        before = [t for t in activity_times if t < ep.end_at - timedelta(minutes=1)]
        if before:
            last = max(before)
            if ep.end_at - last >= GAP_FOR_INFERENCE:
                ep.start_at, ep.start_basis = last, INFERRED

    # Plausibility (live reports cover the last two days).
    latest_ok = observed_at + END_SLACK
    for t in (ep.start_at, ep.end_at):
        if t is not None and (t > latest_ok or t < observed_at - MAX_LOOKBACK):
            return None, f"{_local(t):%m-%d %H:%M} is outside the last two days of this message"
    if ep.start_at and ep.end_at:
        span = ep.end_at - ep.start_at
        if span < MIN_EPISODE or span > MAX_EPISODE:
            return None, "sleep must last between 10 minutes and 20 hours"
    if ep.duration_min is not None and not 10 <= ep.duration_min <= 20 * 60:
        return None, "sleep must last between 10 minutes and 20 hours"

    if ep.end_at is not None:
        ep.wake_date = _local(ep.end_at).date().isoformat()
    else:
        wake_date = str(raw.get("wake_date") or "")
        try:
            wd = date.fromisoformat(wake_date)
        except ValueError:
            return None, "wake_date (YYYY-MM-DD) is required when the wake time is unknown"
        today = _local(observed_at).date()
        if not today - timedelta(days=2) <= wd <= today:
            return None, "wake_date must be within the last two days"
        ep.wake_date = wd.isoformat()
        if ep.start_at is not None and ep.duration_min is not None:
            ep.end_at = ep.start_at + timedelta(minutes=ep.duration_min)
            ep.end_basis = INFERRED
            ep.wake_date = _local(ep.end_at).date().isoformat()
    return ep, ""


def daily_payload(subject_id: str, wake_date: str, episodes: List[SleepEpisode]) -> Optional[Dict[str, Any]]:
    """What the health plugin receives for one day: total sleep of the active episodes
    that end on that day (night sleep and naps). None when no duration is known, or when
    any sleep of that day has an unknown length (an undercount would look like a real total)."""
    day = [e for e in episodes if e.status == ACTIVE and e.wake_date == wake_date]
    if not day or any(not e.minutes for e in day):
        return None
    known = day
    total = sum(e.minutes for e in known)
    ends = [e.end_at for e in known if e.end_at]
    noon = datetime.fromisoformat(wake_date + "T12:00:00").replace(tzinfo=LOCAL_TZ)
    measured = max(ends) if ends else noon
    if _local(measured).date().isoformat() != wake_date:  # e.g. a Maputo-clock wake late at night
        measured = noon
    body = {"sleepHours": round(total / 60, 2), "measuredAt": _local(measured).isoformat(timespec="seconds")}
    digest = hashlib.sha1(json.dumps([subject_id, wake_date, body], sort_keys=True).encode()).hexdigest()[:10]
    body["sourceEventId"] = f"statebar-sleep:{wake_date}:{digest}"
    return body


def replaces(new: SleepEpisode, old: SleepEpisode) -> bool:
    """For two reports of the same sleep: does the new one become the record?
    A bare duration never replaces a report with real times (it adds nothing);
    otherwise stated beats inferred, then the later message wins."""
    if new.end_at is None and old.start_at and old.end_at:
        return False
    return new.rank() >= old.rank()


_IMPORT_BASES = {"原话": STATED, "台账": STATED, "日记": STATED, "推理": INFERRED}


def history_episode(raw: Dict[str, Any], *, subject_id: str, event_id: str, index: int) -> SleepEpisode:
    """One reviewed history row -> episode. Shape and plausibility only; the evidence
    review was done against the original records and is cited in ``source``."""
    if not isinstance(raw, dict):
        raise ValueError("not an object")
    kind = str(raw.get("basis") or "")
    if kind not in _IMPORT_BASES:
        raise ValueError("basis must be 原话/台账/日记/推理")
    try:
        wd = date.fromisoformat(str(raw.get("wake_date") or ""))
    except ValueError:
        raise ValueError("wake_date (YYYY-MM-DD) is required") from None
    offset = int(raw.get("utc_offset_min", 480))
    if offset not in (120, 480):
        raise ValueError("utc_offset_min must be 120 (Maputo) or 480 (Beijing)")
    start, end = _opt_dt(raw.get("start")), _opt_dt(raw.get("end"))
    duration = raw.get("duration_min")
    duration = int(duration) if duration not in (None, "") else None
    if start and end and not MIN_EPISODE <= end - start <= MAX_EPISODE:
        raise ValueError("start/end must be 10 minutes to 20 hours apart")
    if duration is not None and not 10 <= duration <= 20 * 60:
        raise ValueError("duration_min must be 10..1200")
    if end is not None and end.astimezone(timezone(timedelta(minutes=offset))).date() != wd:
        raise ValueError("end is not on wake_date in the reported local clock")
    basis = _IMPORT_BASES[kind]
    approx = bool(raw.get("approx"))
    ep = SleepEpisode(subject_id=subject_id, event_id=event_id, index=index, wake_date=wd.isoformat(),
                      start_at=start, end_at=end, duration_min=duration,
                      start_basis=(basis if not approx else INFERRED) if start else None,
                      end_basis=(basis if not approx else INFERRED) if end else None,
                      duration_basis=basis if duration is not None else None,
                      observed_at=end or datetime.fromisoformat(wd.isoformat() + "T12:00:00").replace(
                          tzinfo=timezone(timedelta(minutes=offset))),
                      source_text=f"[{kind}] {str(raw.get('note') or '')[:300]}",
                      source_message_id=str(raw.get("source") or "")[:200], offset_min=offset)
    if ep.start_at and ep.end_at:
        ep.duration_min = None  # length comes from the clock times
    return ep
