"""Deterministic validation for Persistent (LLM) extractor output.

The LLM proposes, the validator disposes. Rules enforced here (派单总纲 §8/§11):

- ``type`` / ``certainty`` must come from the frozen enums;
- ``source`` is ALWAYS taken from the observe request — the LLM can never
  forge provenance (this keeps S1 enforceable);
- ``key`` is normalized to a canonical slug;
- ``confidence`` is clamped to [0, 1];
- assistant-question sourced events can never yield canonical-state
  observations through the persistent path (S1 belt, reconciler is the
  suspenders).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional

from ..models import (
    Certainty,
    Observation,
    ObservationType,
    Source,
    SourceType,
    TimeExpr,
)

_SLUG_RE = re.compile(r"[^a-z0-9_]+")

# Chinese → canonical English keys (kept small on purpose; the LLM prompt
# already instructs English keys — this map is the safety net).
_KNOWN_KEYS = {
    "书法": "calligraphy",
    "写字": "calligraphy",
    "游泳": "swimming",
    "跑步": "running",
    "健身": "gym",
    "锻炼": "exercise",
    "吃饭": "meal",
    "午饭": "lunch",
    "晚饭": "dinner",
    "吃药": "medication",
    "胃": "stomach_pain",
    "胃疼": "stomach_pain",
    "头疼": "headache",
    "头痛": "headache",
    "睡觉": "sleep",
    "睡觉了": "sleep",
    "游泳了": "swimming",
    "工作": "work",
    "加班": "overtime_work",
    "开会": "meeting",
    "出差": "business_trip",
    "旅行": "travel",
    "看书": "reading",
    "写代码": "coding",
}

_VALID_TYPES = ObservationType.ALL
_VALID_CERTAINTIES = Certainty.ALL
_VALID_TIME = TimeExpr.ALL

# Current sleep/wake state is high-impact and easy to corrupt through keyword
# mention.  Admission therefore requires lexical evidence in the authoritative
# user message, not merely a model-proposed ontology key/value.
_SLEEP_ASSERTION_PATTERNS = [
    re.compile(
        r"(?:^|[，,。！？!?\\s])(?:我)?(?:准备|要|去)睡(?:觉)?了?"
        r"(?=$|[，,。！？!?\\s])"
    ),
    re.compile(
        r"(?:^|[，,。！？!?\\s])(?:我)?(?:已经)?睡(?:觉)?了"
        r"(?=$|[，,。！？!?\\s])"
    ),
]
_AWAKE_ASSERTION_PATTERNS = [
    re.compile(
        r"(?:^|[，,。！？!?\\s])(?:我)?(?:刚睡醒|刚醒(?:了|过来)?|睡醒了|刚起床|一觉醒来)"
        r"(?=$|[，,。！？!?\\s])"
    ),
    re.compile(
        r"(?:^|[，,。！？!?\\s])(?:我)?(?:现在)?醒着"
        r"(?=$|[，,。！？!?\\s])"
    ),
]


def _has_any(patterns, text: str) -> bool:
    return any(p.search(text) for p in patterns)


def _semantic_admission_ok(
    obs_type: str,
    category: str,
    key: str,
    value: str,
    source_text: str,
) -> bool:
    """Conservative truth gate for current sleep/wake observations.

    Mentioning a state name, questioning it, describing an app/history, or
    asking why a field exists is not evidence that the state is true.  For
    this high-impact domain we require an explicit current-state assertion in
    the authoritative user message.
    """
    sleep_domain = (
        category == "sleep"
        or obs_type in (ObservationType.SLEEP, ObservationType.AWAKE)
        or key in ("sleep", "sleeping", "awake")
    )
    if not sleep_domain:
        return True
    if not source_text:
        return False

    wants_awake = obs_type == ObservationType.AWAKE or key == "awake"
    wants_sleep = obs_type == ObservationType.SLEEP or key in ("sleep", "sleeping")

    if wants_awake:
        return _has_any(_AWAKE_ASSERTION_PATTERNS, source_text)
    if wants_sleep:
        return _has_any(_SLEEP_ASSERTION_PATTERNS, source_text)

    # Unknown sleep-domain state shapes are observation-only noise, not
    # canonical evidence.
    return False


def normalize_key(raw: Any, fallback_type: str, text: str) -> str:
    raw = str(raw or "").strip()
    if not raw:
        return ""
    if raw in _KNOWN_KEYS:
        return _KNOWN_KEYS[raw]
    slug = _SLUG_RE.sub("_", raw.lower()).strip("_")
    if slug and slug[0].isdigit():
        slug = "k_" + slug
    if not re.fullmatch(r"[a-z][a-z0-9_]*", slug or ""):
        # Non-ASCII or garbage: derive a stable slug from the raw text.
        digest = hashlib.md5(raw.encode("utf-8", errors="replace")).hexdigest()[:8]
        base = _SLUG_RE.sub("_", fallback_type or "state").strip("_") or "state"
        slug = f"{base}_{digest}"
    return slug


def validate_observation(
    raw: Dict[str, Any],
    subject_id: str,
    event_id: str,
    source: Source,
    observed_at,
    index: int,
    source_text: str = "",
) -> Optional[Observation]:
    if not isinstance(raw, dict):
        return None

    obs_type = str(raw.get("type", "")).strip().lower()
    if obs_type not in _VALID_TYPES:
        return None

    certainty_raw = raw.get("certainty")
    if certainty_raw is None:
        return None
    certainty = str(certainty_raw).strip().lower()
    if certainty not in _VALID_CERTAINTIES:
        return None

    time_expr = str(raw.get("time_expression", TimeExpr.UNSPECIFIED)).strip().lower()
    if time_expr not in _VALID_TIME:
        time_expr = TimeExpr.UNSPECIFIED

    # Never trust model-supplied raw_payload as evidence.  The caller owns the
    # authoritative user message; model text is only a fallback for direct
    # validator callers/tests that do not provide source_text.
    text = str(source_text or raw.get("raw_payload", "") or "")
    key = normalize_key(raw.get("key"), obs_type, text)
    category = str(raw.get("category", "") or "").strip()
    value = str(raw.get("value", "") or "").strip()

    if not _semantic_admission_ok(obs_type, category, key, value, text):
        return None

    try:
        confidence = float(raw.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    confidence = max(0.0, min(1.0, confidence))

    return Observation(
        subject_id=subject_id,
        event_id=event_id,
        observation_index=index,
        type=obs_type,
        category=category,
        key=key,
        value=value,
        certainty=certainty,
        time_expression=time_expr,
        source=source,  # provenance is caller-owned, never LLM-owned
        observed_at=observed_at,
        confidence=confidence,
        raw_payload=text,
    )


def validate_observations(
    payload: Any,
    subject_id: str,
    event_id: str,
    source: Source,
    observed_at,
    start_index: int = 0,
    source_text: str = "",
) -> List[Observation]:
    """Validate an LLM payload (list of dicts) into Observations."""
    if not isinstance(payload, list):
        return []
    out: List[Observation] = []
    for i, item in enumerate(payload):
        obs = validate_observation(
            item,
            subject_id,
            event_id,
            source,
            observed_at,
            start_index + i,
            source_text=source_text,
        )
        if obs is not None:
            out.append(obs)
    return out


EXTRACTOR_SYSTEM_PROMPT = """You are the User State extractor for an AI agent's short-term state layer.
Given ONE user message, output ONLY a JSON array of state observations. No prose.

Schema per observation:
{
  "type": "plan | activity | symptom | sleep | awake | cancel | resolve | state",
  "category": "planning | health | activity | sleep | ...",
  "key": "stable lowercase english slug, e.g. calligraphy / stomach_pain / swimming / medication",
  "value": "short english value, e.g. tentative plan content, 'completed', 'improving'",
  "certainty": "confirmed | planned | tentative | estimated | inferred",
  "time_expression": "morning | afternoon | evening | tonight | now | today | tomorrow | unspecified",
  "confidence": 0.0-1.0,
  "raw_payload": "the original sentence"
}

Rules (hard):
1. Only facts the USER stated. If the user merely answers a question without
   confirming the assistant's guess, do NOT create a state for that guess.
2. Distinguish plan vs idea strictly:
   "下午去写书法"    -> type plan, certainty planned
   "下午可能去写书法" -> type plan, certainty tentative
   "下午三点去写书法" -> type plan, certainty planned, value "3pm calligraphy"
3. "算了，不去了" -> type cancel. "今天去游泳了" -> type activity, key swimming, value completed.
4. "胃有点疼" -> type symptom, key stomach_pain, value active.
   "好多了" -> type resolve, key stomach_pain, value improving (only if a symptom exists;
   otherwise omit). "不疼了" -> value resolved.
5. "我刚睡醒" -> type awake, key awake. "我要睡了" -> type sleep, key sleeping, value preparing_sleep.
6. Every observation must carry the original sentence in raw_payload.
7. If nothing state-worthy, output [].
8. Mention is NOT assertion. A state word used as a topic, field name, quoted
   text, correction, negation, historical record, app feature, or question is
   not evidence that the state is currently true.
9. For sleep/awake, emit an observation only when the user explicitly states
   their own current transition/state (e.g. "我要睡了", "我刚睡醒"). Never infer
   current sleep from "睡眠记录", "为什么还有 sleeping", "我没说我睡了",
   "昨天睡了三个小时", or quoted/reported speech.

Examples:
Input: "下午可能去写书法"
Output: [{"type":"plan","category":"planning","key":"calligraphy","value":"go practice calligraphy","certainty":"tentative","time_expression":"afternoon","confidence":0.9,"raw_payload":"下午可能去写书法"}]

Input: "今天去游泳了，好累"
Output: [{"type":"activity","category":"activity","key":"swimming","value":"completed","certainty":"confirmed","time_expression":"today","confidence":1.0,"raw_payload":"今天去游泳了"}]

Input: "晚点去写书法，吃完饭以后"
Output: [{"type":"plan","category":"planning","key":"calligraphy","value":"rescheduled after dinner","certainty":"planned","time_expression":"tonight","confidence":0.9,"raw_payload":"晚点去写书法，吃完饭以后"}]


Input: "我做过一个记录心情和睡眠的 App"
Output: []

Input: "我状态里为什么还有 sleeping"
Output: []

Input: "我没说我睡了"
Output: []

Input: "昨天只睡了三个小时"
Output: []
"""


def build_user_prompt(text: str) -> str:
    return f"User message: {text}\n\nOutput JSON array only:"
