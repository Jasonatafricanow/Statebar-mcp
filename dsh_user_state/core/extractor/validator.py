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
) -> Optional[Observation]:
    if not isinstance(raw, dict):
        return None

    obs_type = str(raw.get("type", "")).strip().lower()
    if obs_type not in _VALID_TYPES:
        return None

    certainty = str(raw.get("certainty", "confirmed")).strip().lower()
    if certainty not in _VALID_CERTAINTIES:
        certainty = Certainty.CONFIRMED

    time_expr = str(raw.get("time_expression", TimeExpr.UNSPECIFIED)).strip().lower()
    if time_expr not in _VALID_TIME:
        time_expr = TimeExpr.UNSPECIFIED

    text = str(raw.get("raw_payload", "") or "")
    key = normalize_key(raw.get("key"), obs_type, text)

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
        category=str(raw.get("category", "") or "").strip(),
        key=key,
        value=str(raw.get("value", "") or "").strip(),
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
) -> List[Observation]:
    """Validate an LLM payload (list of dicts) into Observations."""
    if not isinstance(payload, list):
        return []
    out: List[Observation] = []
    for i, item in enumerate(payload):
        obs = validate_observation(item, subject_id, event_id, source, observed_at, start_index + i)
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

Examples:
Input: "下午可能去写书法"
Output: [{"type":"plan","category":"planning","key":"calligraphy","value":"go practice calligraphy","certainty":"tentative","time_expression":"afternoon","confidence":0.9,"raw_payload":"下午可能去写书法"}]

Input: "今天去游泳了，好累"
Output: [{"type":"activity","category":"activity","key":"swimming","value":"completed","certainty":"confirmed","time_expression":"today","confidence":1.0,"raw_payload":"今天去游泳了"}]

Input: "晚点去写书法，吃完饭以后"
Output: [{"type":"plan","category":"planning","key":"calligraphy","value":"rescheduled after dinner","certainty":"planned","time_expression":"tonight","confidence":0.9,"raw_payload":"晚点去写书法，吃完饭以后"}]
"""


def build_user_prompt(text: str) -> str:
    return f"User message: {text}\n\nOutput JSON array only:"
