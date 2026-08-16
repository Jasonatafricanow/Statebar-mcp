"""Data model for the User State Layer.

These are the frozen application-layer semantics of the project
(see 派单总纲 §11 / §13). Everything else in core/ operates on these
plain dataclasses; no transport code imports core internals.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Enumerations (kept as plain strings so the package stays dependency-free)
# ---------------------------------------------------------------------------


class ObservationType:
    PLAN = "plan"
    ACTIVITY = "activity"
    SYMPTOM = "symptom"
    SLEEP = "sleep"
    AWAKE = "awake"
    CANCEL = "cancel"
    RESOLVE = "resolve"
    STATE = "state"

    ALL = {PLAN, ACTIVITY, SYMPTOM, SLEEP, AWAKE, CANCEL, RESOLVE, STATE}


class SourceType:
    CONVERSATION = "conversation"
    DIARY = "diary"
    HEALTH = "health"
    ASSISTANT_QUESTION = "assistant_question"

    ALL = {CONVERSATION, DIARY, HEALTH, ASSISTANT_QUESTION}


class Certainty:
    CONFIRMED = "confirmed"
    PLANNED = "planned"
    TENTATIVE = "tentative"
    ESTIMATED = "estimated"
    INFERRED = "inferred"

    ALL = {CONFIRMED, PLANNED, TENTATIVE, ESTIMATED, INFERRED}


class StateStatus:
    CREATED = "created"
    TENTATIVE = "tentative"
    PLANNED = "planned"
    ACTIVE = "active"
    IMPROVING = "improving"
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"

    ALL = {
        CREATED,
        TENTATIVE,
        PLANNED,
        ACTIVE,
        IMPROVING,
        PENDING,
        COMPLETED,
        CANCELLED,
        RESOLVED,
        SUPERSEDED,
        EXPIRED,
    }

    # Statuses that still participate in the "current" view.
    CURRENT_LIKE = {CREATED, TENTATIVE, PLANNED, ACTIVE, IMPROVING, PENDING}
    TERMINAL = {COMPLETED, CANCELLED, RESOLVED, SUPERSEDED, EXPIRED}


class TimeExpr:
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"
    TONIGHT = "tonight"
    NOW = "now"
    TODAY = "today"
    TOMORROW = "tomorrow"
    UNSPECIFIED = "unspecified"

    ALL = {MORNING, AFTERNOON, EVENING, TONIGHT, NOW, TODAY, TOMORROW, UNSPECIFIED}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: Any) -> datetime:
    """Parse an ISO8601 string (with or without tz) into an aware datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # accept e.g. "2026-08-16 14:00:00"
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def dt_to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Source / Observation
# ---------------------------------------------------------------------------


@dataclass
class Source:
    type: str = SourceType.CONVERSATION
    platform: str = "cli"
    session_id: str = ""
    message_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "platform": self.platform,
            "session_id": self.session_id,
            "message_id": self.message_id,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Source":
        # 兼容字符串形式：source="diary" / "conversation" → type；dict 形式完整解析。
        # 修复：REST 契约文档写 source 为标量，但原实现只接受 dict → 500 (str.get)。
        if isinstance(data, str):
            return cls(type=data or SourceType.CONVERSATION)
        data = data or {}
        return cls(
            type=str(data.get("type") or SourceType.CONVERSATION),
            platform=str(data.get("platform") or "cli"),
            session_id=str(data.get("session_id") or ""),
            message_id=str(data.get("message_id") or ""),
        )


@dataclass
class Observation:
    subject_id: str
    event_id: str
    type: str
    category: str = ""
    key: str = ""
    value: str = ""
    certainty: str = Certainty.CONFIRMED
    time_expression: str = TimeExpr.UNSPECIFIED
    source: Source = field(default_factory=Source)
    observed_at: datetime = field(default_factory=utc_now)
    confidence: float = 1.0
    raw_payload: str = ""
    observation_index: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "event_id": self.event_id,
            "observation_index": self.observation_index,
            "type": self.type,
            "category": self.category,
            "key": self.key,
            "value": self.value,
            "certainty": self.certainty,
            "time_expression": self.time_expression,
            "source": self.source.to_dict(),
            "observed_at": dt_to_iso(self.observed_at),
            "confidence": self.confidence,
            "raw_payload": self.raw_payload,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Observation":
        return cls(
            subject_id=str(data["subject_id"]),
            event_id=str(data["event_id"]),
            observation_index=int(data.get("observation_index", 0)),
            type=str(data.get("type", "")),
            category=str(data.get("category", "")),
            key=str(data.get("key", "")),
            value=str(data.get("value", "")),
            certainty=str(data.get("certainty", Certainty.CONFIRMED)),
            time_expression=str(data.get("time_expression", TimeExpr.UNSPECIFIED)),
            source=Source.from_dict(data.get("source")),
            observed_at=parse_dt(data.get("observed_at", utc_now().isoformat())),
            confidence=float(data.get("confidence", 1.0)),
            raw_payload=str(data.get("raw_payload", "")),
        )

    def clone(self, **overrides: Any) -> "Observation":
        d = self.to_dict()
        d.update(overrides)
        return Observation.from_dict(d)


# ---------------------------------------------------------------------------
# Canonical State / Transition
# ---------------------------------------------------------------------------


@dataclass
class State:
    subject_id: str
    category: str
    key: str
    value: str = ""
    status: str = StateStatus.CREATED
    certainty: str = Certainty.CONFIRMED
    valid_from: datetime = field(default_factory=utc_now)
    valid_until: datetime = field(default_factory=utc_now)
    relevant_until: datetime = field(default_factory=utc_now)
    followup_relevant: bool = False
    snapshot_priority: int = 0
    state_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    # Semantic time of the observation that last changed this state.
    # Out-of-order guards compare against THIS (D12), never against
    # internal processing time — equal-second bursts must not be rejected.
    last_observed_at: datetime = field(default_factory=utc_now)
    # Identity of the observation that last changed this state
    # ("<event_id>:<observation_index>"). Replaying the SAME observation is
    # a no-op (crash-recovery idempotency) — never duplicate transitions.
    last_observation_key: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state_id": self.state_id,
            "subject_id": self.subject_id,
            "category": self.category,
            "key": self.key,
            "value": self.value,
            "status": self.status,
            "certainty": self.certainty,
            "valid_from": dt_to_iso(self.valid_from),
            "valid_until": dt_to_iso(self.valid_until),
            "relevant_until": dt_to_iso(self.relevant_until),
            "followup_relevant": self.followup_relevant,
            "snapshot_priority": self.snapshot_priority,
            "created_at": dt_to_iso(self.created_at),
            "updated_at": dt_to_iso(self.updated_at),
            "last_observed_at": dt_to_iso(self.last_observed_at),
            "last_observation_key": self.last_observation_key,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "State":
        return cls(
            state_id=str(data.get("state_id") or uuid.uuid4().hex),
            subject_id=str(data["subject_id"]),
            category=str(data.get("category", "")),
            key=str(data.get("key", "")),
            value=str(data.get("value", "")),
            status=str(data.get("status", StateStatus.CREATED)),
            certainty=str(data.get("certainty", Certainty.CONFIRMED)),
            valid_from=parse_dt(data.get("valid_from") or utc_now().isoformat()),
            valid_until=parse_dt(data.get("valid_until") or utc_now().isoformat()),
            relevant_until=parse_dt(data.get("relevant_until") or utc_now().isoformat()),
            followup_relevant=bool(data.get("followup_relevant", False)),
            snapshot_priority=int(data.get("snapshot_priority", 0)),
            created_at=parse_dt(data.get("created_at") or utc_now().isoformat()),
            updated_at=parse_dt(data.get("updated_at") or utc_now().isoformat()),
            last_observed_at=parse_dt(data.get("last_observed_at") or utc_now().isoformat()),
            last_observation_key=str(data.get("last_observation_key", "") or ""),
        )


@dataclass
class StateTransition:
    state_id: str
    subject_id: str
    from_status: str
    to_status: str
    reason: str
    source_observation_id: Optional[int] = None
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state_id": self.state_id,
            "subject_id": self.subject_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "reason": self.reason,
            "source_observation_id": self.source_observation_id,
            "created_at": dt_to_iso(self.created_at),
        }


# ---------------------------------------------------------------------------
# Request / snapshot payloads
# ---------------------------------------------------------------------------


@dataclass
class ObserveRequest:
    subject_id: str
    event_id: str
    text: str
    source: Source = field(default_factory=Source)
    observed_at: datetime = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ObserveRequest":
        return cls(
            subject_id=str(data["subject_id"]),
            event_id=str(data["event_id"]),
            text=str(data.get("text", "")),
            source=Source.from_dict(data.get("source")),
            observed_at=parse_dt(data.get("observed_at") or utc_now().isoformat()),
        )


@dataclass
class SnapshotItem:
    text: str
    category: str = ""
    key: str = ""
    value: str = ""
    status: str = ""
    source_state_id: str = ""

    def render(self) -> str:
        return f"- {self.text}"


@dataclass
class Snapshot:
    subject_id: str
    text: str
    items: List[SnapshotItem] = field(default_factory=list)
    base_items: List[SnapshotItem] = field(default_factory=list)
    query_items: List[SnapshotItem] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "text": self.text,
            "items": [
                {
                    "text": i.text,
                    "category": i.category,
                    "key": i.key,
                    "value": i.value,
                    "status": i.status,
                }
                for i in self.items
            ],
            "base_count": len(self.base_items),
            "query_count": len(self.query_items),
        }
