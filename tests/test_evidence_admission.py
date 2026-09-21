"""Regression tests for semantic evidence admission.

These cases protect the boundary between mentioning a state and asserting that
the state is currently true.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from statebar_mcp.core.extractor.fast_overlay import FastOverlayExtractor
from statebar_mcp.core.extractor.persistent import _parse_llm_json
from statebar_mcp.core.extractor.validator import validate_observation
from statebar_mcp.core.models import ObserveRequest, Source


def _fast(text: str):
    return FastOverlayExtractor().extract(
        "u",
        "e",
        text,
        Source(type="conversation"),
        datetime.now(timezone.utc),
    )


@pytest.mark.parametrize(
    "text",
    [
        "我做过一个记录心情和睡眠的 App",
        "我状态里为什么还有 sleeping",
        "我没说我睡了",
        "为什么系统显示我睡了",
        "昨天只睡了三个小时",
        "他说我睡了",
    ],
)
def test_fast_overlay_does_not_turn_sleep_mentions_into_current_sleep(text):
    assert not any(o.type == "sleep" for o in _fast(text)), text


@pytest.mark.parametrize(
    "text,value",
    [
        ("我要睡了", "preparing_sleep"),
        ("准备睡了", "preparing_sleep"),
        ("我去睡了", "preparing_sleep"),
        ("我睡了", "sleeping"),
    ],
)
def test_fast_overlay_keeps_explicit_current_sleep_assertions(text, value):
    observations = _fast(text)
    assert any(o.type == "sleep" and o.value == value for o in observations), text


def test_persistent_cannot_forge_raw_payload_to_create_sleep():
    request = ObserveRequest(
        subject_id="u",
        event_id="e1",
        text="我做过一个记录心情和睡眠的 App",
        source=Source(type="conversation"),
        observed_at=datetime.now(timezone.utc),
    )
    model_output = json.dumps(
        [
            {
                "type": "sleep",
                "category": "sleep",
                "key": "sleeping",
                "value": "sleeping",
                "certainty": "confirmed",
                "time_expression": "now",
                "confidence": 1.0,
                # Deliberately forged by the model. Admission must use
                # request.text, not this field.
                "raw_payload": "我要睡了",
            }
        ],
        ensure_ascii=False,
    )
    assert _parse_llm_json(model_output, request) == []


def test_persistent_rejects_meta_sleep_status_observation():
    request = ObserveRequest(
        subject_id="u",
        event_id="e2",
        text="我状态里为什么还有 sleeping",
        source=Source(type="conversation"),
        observed_at=datetime.now(timezone.utc),
    )
    model_output = json.dumps(
        [
            {
                "type": "state",
                "category": "sleep",
                "key": "sleeping",
                "value": "incorrect_status",
                "certainty": "confirmed",
                "time_expression": "now",
                "confidence": 1.0,
                "raw_payload": request.text,
            }
        ],
        ensure_ascii=False,
    )
    assert _parse_llm_json(model_output, request) == []


def test_persistent_accepts_grounded_explicit_sleep_assertion():
    request = ObserveRequest(
        subject_id="u",
        event_id="e3",
        text="我要睡了",
        source=Source(type="conversation"),
        observed_at=datetime.now(timezone.utc),
    )
    model_output = json.dumps(
        [
            {
                "type": "sleep",
                "category": "sleep",
                "key": "sleeping",
                "value": "preparing_sleep",
                "certainty": "confirmed",
                "time_expression": "now",
                "confidence": 1.0,
                "raw_payload": request.text,
            }
        ],
        ensure_ascii=False,
    )
    observations = _parse_llm_json(model_output, request)
    assert len(observations) == 1
    assert observations[0].key == "sleeping"
    assert observations[0].raw_payload == request.text


def test_invalid_certainty_is_rejected_instead_of_promoted_to_confirmed():
    obs = validate_observation(
        {
            "type": "activity",
            "category": "activity",
            "key": "reading",
            "value": "completed",
            "certainty": "definitely",
            "time_expression": "today",
            "confidence": 0.9,
            "raw_payload": "今天看书了",
        },
        "u",
        "e4",
        Source(type="conversation"),
        datetime.now(timezone.utc),
        0,
        source_text="今天看书了",
    )
    assert obs is None
