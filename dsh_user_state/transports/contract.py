"""The frozen application-layer Contract (派单总纲 §6).

Five application APIs; every transport maps to THE SAME semantics:

    observe(...)               user_state.observe            POST /v1/observe
    get_snapshot(...)          user_state.snapshot           GET  /v1/snapshot
    get_context_candidates()   user_state.context_candidates GET  /v1/context-candidates
    get_state(...)             user_state.get_state          GET  /v1/state
    healthcheck()              user_state.health             GET  /v1/health
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..core.models import ObserveRequest
from ..core.service import UserStateService

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "user_state.observe",
        "description": (
            "Record a user message as an observation of the user's state. "
            "Synchronous fast rules apply immediately; persistent LLM "
            "extraction follows in the background. Idempotent per event_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject_id": {"type": "string", "description": "Stable user id; state scope"},
                "event_id": {"type": "string", "description": "Unique event id for idempotency"},
                "text": {"type": "string", "description": "The user's message text"},
                "observed_at": {
                    "type": "string",
                    "description": "ISO8601 semantic time (caller-supplied), not server receipt time",
                },
                "source": {
                    "type": "object",
                    "description": "Provenance: type (conversation|diary|health|assistant_question), platform, session_id, message_id",
                    "properties": {
                        "type": {"type": "string"},
                        "platform": {"type": "string"},
                        "session_id": {"type": "string"},
                        "message_id": {"type": "string"},
                    },
                },
            },
            "required": ["subject_id", "event_id", "text"],
        },
    },
    {
        "name": "user_state.snapshot",
        "description": (
            "Build the current user-state snapshot (BASE + query-aware "
            "increment, 100-300 tokens). Expired states are lazily dropped."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject_id": {"type": "string"},
                "query": {"type": "string", "description": "Current user message for query-aware increment"},
            },
            "required": ["subject_id"],
        },
    },
    {
        "name": "user_state.context_candidates",
        "description": (
            "Buckets of conversation material: recent / unresolved / followup / "
            "planned / interesting. The service only supplies material; whether "
            "to be proactive is decided elsewhere."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"subject_id": {"type": "string"}},
            "required": ["subject_id"],
        },
    },
    {
        "name": "user_state.get_state",
        "description": "Read current (non-terminal) states, optionally filtered by category/key.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject_id": {"type": "string"},
                "category": {"type": "string"},
                "key": {"type": "string"},
            },
            "required": ["subject_id"],
        },
    },
    {
        "name": "user_state.health",
        "description": "Service healthcheck.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

TOOL_NAMES = [t["name"] for t in TOOLS]


class ContractError(Exception):
    pass


class Contract:
    """JSON in / JSON out mapping shared by all transports."""

    def __init__(self, service: UserStateService):
        self.service = service

    # -- typed handlers --------------------------------------------------------

    def observe(self, args: Dict[str, Any]) -> Dict[str, Any]:
        request = ObserveRequest.from_dict(args)
        return self.service.observe(request)

    def snapshot(self, args: Dict[str, Any]) -> Dict[str, Any]:
        subject_id = str(args.get("subject_id") or "")
        query = str(args.get("query") or "")
        if not subject_id:
            raise ContractError("subject_id is required")
        snap = self.service.get_snapshot(subject_id, query=query)
        return snap.to_dict()

    def context_candidates(self, args: Dict[str, Any]) -> Dict[str, Any]:
        subject_id = str(args.get("subject_id") or "")
        if not subject_id:
            raise ContractError("subject_id is required")
        return self.service.get_context_candidates(subject_id)

    def get_state(self, args: Dict[str, Any]) -> Dict[str, Any]:
        subject_id = str(args.get("subject_id") or "")
        if not subject_id:
            raise ContractError("subject_id is required")
        states = self.service.get_state(
            subject_id,
            category=str(args.get("category") or ""),
            key=str(args.get("key") or ""),
        )
        return {"subject_id": subject_id, "states": states}

    def health(self, args: Dict[str, Any] = None) -> Dict[str, Any]:
        return self.service.healthcheck()

    # -- generic dispatch --------------------------------------------------------

    def call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        handler = {
            "user_state.observe": self.observe,
            "user_state.snapshot": self.snapshot,
            "user_state.context_candidates": self.context_candidates,
            "user_state.get_state": self.get_state,
            "user_state.health": self.health,
        }.get(tool_name)
        if handler is None:
            raise ContractError(f"unknown tool: {tool_name}")
        try:
            return handler(arguments or {})
        except ContractError:
            raise
        except Exception as exc:  # transport-safe error surface
            raise ContractError(f"{tool_name} failed: {exc}") from exc
