"""Fast Overlay extractor.

Rule-based, synchronous, deliberately SMALL (派单总纲 §8): only a handful of
high-confidence immediate states so the current turn never makes low-level
state mistakes (e.g. nagging about sleep right after "我刚睡醒").

Design:
- rules run in a fixed priority order;
- when a rule fires it MASKS its matched span, so later rules test the
  remaining text ("好多了，吃了点药" → resolve + medication both fire,
  while "睡醒了" is claimed by the awake rule before the sleep rule can
  match the embedded "睡了").
"""

from __future__ import annotations

import re
from typing import List, Optional

from ..models import (
    Certainty,
    Observation,
    ObservationType,
    Source,
    TimeExpr,
)

# Body-part → canonical key / display value.
SYMPTOM_LEXICON = [
    ("胃", "stomach_pain", "stomach discomfort"),
    ("肚子", "abdominal_pain", "abdominal discomfort"),
    ("头", "headache", "headache"),
    ("嗓子", "sore_throat", "sore throat"),
    ("喉咙", "sore_throat", "sore throat"),
    ("牙", "toothache", "toothache"),
    ("腰", "back_pain", "lower back pain"),
    ("背", "back_pain", "back pain"),
    ("腿", "leg_pain", "leg pain"),
    ("脚", "foot_pain", "foot pain"),
    ("手", "hand_pain", "hand pain"),
    ("膝盖", "knee_pain", "knee pain"),
    ("眼睛", "eye_discomfort", "eye discomfort"),
]

_BODY_ALT = "|".join(p for p, _, _ in SYMPTOM_LEXICON)

_AWAKE_PATTERNS = [
    re.compile(r"刚睡醒|刚醒(?:了|过来)?|睡醒了|刚起床|一觉醒来|睡了个好觉刚醒"),
]

_SLEEP_PATTERNS = [
    re.compile(r"准备睡(?:觉)?|要睡(?:觉)?了|去睡(?:觉)?了|困了.{0,6}睡"),
    re.compile(r"睡(?:觉)?了|躺下了|睡了"),
]

_CANCEL_PATTERNS = [
    re.compile(r"算了[，,。\s]*不去了|不去了|取消了|不写了|不游了|不练了|不看了|改天再(?:说|约)"),
]

_RESOLVE_WITH_BODY = re.compile(
    rf"({_BODY_ALT})(?:有点)?(?:不疼了|不痛了|好了|好多了|不难受了|没事了)"
)
_RESOLVE_GENERIC = re.compile(r"好多了|好很多了|不疼了|不痛了|舒服多了")

_SYMPTOM_ACTIVE = re.compile(
    rf"({_BODY_ALT})(?:有点|有些|很|一阵一阵|点)?(?:疼|痛|不舒服|难受|痛得)"
)

_MEDICATION = re.compile(r"吃(?:了点?|过|完)?药(?:了)?|刚(?:才)?吃药|服药了|刚吃完药")

_CONFIRM_PLAN = re.compile(r"确认|说好了|就这么定|一定去|肯定去")

_SLEEP_WAKE_WORDS = re.compile(r"睡醒|醒了|起床")


def _mask(text: str, span: tuple[int, int]) -> str:
    start, end = span
    return text[:start] + " " * (end - start) + text[end:]


def _find_symptom(body: str) -> tuple[str, str]:
    for part, key, value in SYMPTOM_LEXICON:
        if body.startswith(part):
            return key, value
    return "symptom", "discomfort"


class FastOverlayExtractor:
    """Extract high-confidence immediate states from a single user message."""

    name = "fast_overlay"

    def extract(
        self,
        subject_id: str,
        event_id: str,
        text: str,
        source: Source,
        observed_at,
    ) -> List[Observation]:
        if not text or not text.strip():
            return []
        remaining = text
        found: List[Observation] = []

        def add(
            obs_type: str,
            category: str,
            key: str,
            value: str,
            time_expr: str = TimeExpr.NOW,
            certainty: str = Certainty.CONFIRMED,
        ) -> None:
            found.append(
                Observation(
                    subject_id=subject_id,
                    event_id=event_id,
                    type=obs_type,
                    category=category,
                    key=key,
                    value=value,
                    certainty=certainty,
                    time_expression=time_expr,
                    source=source,
                    observed_at=observed_at,
                    confidence=1.0,
                    raw_payload=text,
                )
            )

        # 1. awake (must win over the embedded "睡了" in "睡醒了")
        for pat in _AWAKE_PATTERNS:
            m = pat.search(remaining)
            if m:
                add(ObservationType.AWAKE, "sleep", "awake", "awake")
                remaining = _mask(remaining, m.span())
                break

        # 2. sleep preparing / sleeping
        if _SLEEP_PATTERNS[0].search(remaining):
            m = _SLEEP_PATTERNS[0].search(remaining)
            add(ObservationType.SLEEP, "sleep", "sleeping", "preparing_sleep")
            remaining = _mask(remaining, m.span())
        elif _SLEEP_PATTERNS[1].search(remaining):
            m = _SLEEP_PATTERNS[1].search(remaining)
            add(ObservationType.SLEEP, "sleep", "sleeping", "sleeping")
            remaining = _mask(remaining, m.span())

        # 3. cancel(plan)
        for pat in _CANCEL_PATTERNS:
            m = pat.search(remaining)
            if m:
                add(ObservationType.CANCEL, "planning", "", "cancel")
                remaining = _mask(remaining, m.span())
                break

        # 4. symptom resolved with explicit body part
        while True:
            m = _RESOLVE_WITH_BODY.search(remaining)
            if not m:
                break
            key, value = _find_symptom(m.group(1))
            add(ObservationType.RESOLVE, "health", key, "resolved")
            remaining = _mask(remaining, m.span())

        # 5. symptom active with explicit body part
        while True:
            m = _SYMPTOM_ACTIVE.search(remaining)
            if not m:
                break
            key, value = _find_symptom(m.group(1))
            add(ObservationType.SYMPTOM, "health", key, "active")
            remaining = _mask(remaining, m.span())

        # 6. generic symptom resolution (no body part; reconciler targets the
        #    most recent active symptom — R8)
        m = _RESOLVE_GENERIC.search(remaining)
        if m:
            add(ObservationType.RESOLVE, "health", "", "improving")
            remaining = _mask(remaining, m.span())

        # 7. medication taken
        m = _MEDICATION.search(remaining)
        if m:
            add(
                ObservationType.ACTIVITY,
                "health",
                "medication",
                "taken",
                time_expr=TimeExpr.TODAY,
            )
            remaining = _mask(remaining, m.span())

        # index observations in rule order (0, 1, 2, ...)
        for i, obs in enumerate(found):
            obs.observation_index = i
        return found
