"""Persistent (async) LLM structured extractor.

LLM-first for everything the Fast Overlay doesn't cover (plans, activities,
reschedules, ...). The extractor model is a *deployment config item*
(派单总纲 §8) — this module binds no concrete model. Two concrete backends:

- ``OpenAICompatExtractor`` : any OpenAI-compatible chat-completions endpoint
  (OpenAI, Gemini-OpenAI, vLLM, Ollama's /v1, ...), stdlib urllib only;
- ``MockExtractor``          : deterministic table for offline dev/tests.

Both implement ``extract(request) -> list[dict]`` and their output is always
pushed through the deterministic validator before reconciliation.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from ..models import Observation, ObserveRequest, Source
from . import validator
from .validator import EXTRACTOR_SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)


class PersistentExtractor:
    """Interface every persistent extractor implements."""

    name = "persistent"

    def available(self) -> bool:
        return False

    def extract(self, request: ObserveRequest) -> List[Observation]:
        return []

    def close(self) -> None:
        pass


class OpenAICompatExtractor(PersistentExtractor):
    """OpenAI-compatible chat completions backend (stdlib only)."""

    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "",
        timeout: float = 30.0,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature

    def available(self) -> bool:
        return bool(self.base_url and self.model)

    def extract(self, request: ObserveRequest) -> List[Observation]:
        if not self.available():
            return []
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": EXTRACTOR_SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(request.text)},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
            logger.warning("persistent extractor request failed: %s", exc)
            return []
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            logger.warning(
                "persistent extractor: unexpected response shape: %.300s", str(data)
            )
            return []
        logger.debug("persistent extractor raw content: %.500s", content)
        return _parse_llm_json(content, request)


def _parse_llm_json(content: str, request: ObserveRequest) -> List[Observation]:
    text = (content or "").strip()
    # tolerate markdown fences
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    # tolerate prose around a JSON array
    if not text.startswith("["):
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(
            "persistent extractor: invalid JSON from model: %.300s", text
        )
        return []
    observations = validator.validate_observations(
        payload,
        request.subject_id,
        request.event_id,
        request.source,
        request.observed_at,
        start_index=0,
    )
    if isinstance(payload, list) and payload and not observations:
        logger.warning(
            "persistent extractor: model payload validated to zero observations: %.300s",
            str(payload),
        )
    return observations


# ---------------------------------------------------------------------------
# Deterministic mock (offline development / tests / CI). NOT a production
# semantic engine — the prompt/schema of the real extractor stays the
# reference, this only makes the async path testable without a model.
# ---------------------------------------------------------------------------

_MOCK_TABLE: List[tuple[re.Pattern, List[Dict[str, Any]]]] = [
    (
        re.compile(r"下午可能去写书法"),
        [
            {
                "type": "plan",
                "category": "planning",
                "key": "calligraphy",
                "value": "go practice calligraphy",
                "certainty": "tentative",
                "time_expression": "afternoon",
                "confidence": 0.9,
            }
        ],
    ),
    (
        re.compile(r"下午(?:三点|3点)?去写书法"),
        [
            {
                "type": "plan",
                "category": "planning",
                "key": "calligraphy",
                "value": "go practice calligraphy",
                "certainty": "planned",
                "time_expression": "afternoon",
                "confidence": 0.95,
            }
        ],
    ),
    (
        re.compile(r"晚点去写书法|吃完饭以后去写|改到晚上写"),
        [
            {
                "type": "plan",
                "category": "planning",
                "key": "calligraphy",
                "value": "rescheduled to tonight",
                "certainty": "planned",
                "time_expression": "tonight",
                "confidence": 0.9,
            }
        ],
    ),
    (
        re.compile(r"今天(?:去)?游泳了"),
        [
            {
                "type": "activity",
                "category": "activity",
                "key": "swimming",
                "value": "completed",
                "certainty": "confirmed",
                "time_expression": "today",
                "confidence": 1.0,
            }
        ],
    ),
    (
        re.compile(r"现在胃疼|胃疼|胃有点疼|胃不舒服"),
        [
            {
                "type": "symptom",
                "category": "health",
                "key": "stomach_pain",
                "value": "active",
                "certainty": "confirmed",
                "time_expression": "now",
                "confidence": 1.0,
            }
        ],
    ),
]


class MockExtractor(PersistentExtractor):
    """Deterministic table-driven extractor for offline runs and tests."""

    name = "mock"

    def __init__(self):
        self.enabled = True

    def available(self) -> bool:
        return self.enabled

    def extract(self, request: ObserveRequest) -> List[Observation]:
        for pattern, observations in _MOCK_TABLE:
            if pattern.search(request.text):
                raw = [
                    {**obs, "raw_payload": request.text}
                    for obs in observations
                ]
                return validator.validate_observations(
                    raw,
                    request.subject_id,
                    request.event_id,
                    request.source,
                    request.observed_at,
                    start_index=0,
                )
        return []
