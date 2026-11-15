"""Host-extracted observations: the host agent's own model supplies Observation JSON
instead of the built-in LLM extractor. statebar must still validate it against the
user's original words and reconcile it with the same evidence rules."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from statebar_mcp.core.extractor.persistent import PersistentExtractor  # noqa: E402
from statebar_mcp.core.models import ObserveRequest, Source, StateStatus  # noqa: E402
from statebar_mcp.core.service import UserStateService  # noqa: E402
from statebar_mcp.core.store import SQLiteStore  # noqa: E402
from statebar_mcp.transports.contract import Contract, ContractError  # noqa: E402


@pytest.fixture
def svc():
    store = SQLiteStore(":memory:")
    # Built-in LLM extractor disabled, as in the deployment that relies on the host.
    service = UserStateService(store, persistent_extractor=PersistentExtractor())
    yield service, store
    service.close()


def observe(service, event_id, text, at, observations=None):
    return service.observe(ObserveRequest(
        subject_id="u", event_id=event_id, text=text, source=Source(type="conversation"),
        observed_at=at, observations=observations))


PLAN = {"type": "plan", "category": "planning", "key": "calligraphy", "value": "go practice calligraphy",
        "certainty": "tentative", "time_expression": "afternoon", "confidence": 0.9, "raw_payload": "下午可能去写书法"}


def test_host_observation_is_validated_and_reconciled(svc):
    service, store = svc
    t = datetime.now(timezone.utc)
    observe(service, "e1", "下午可能去写书法", t)
    assert store.get_state("u", "planning", "calligraphy") is None  # fast rules alone don't see it

    result = observe(service, "e1", "下午可能去写书法", t, observations=[PLAN])
    assert result["status"] == "accepted" and result["event_id"] == "e1#host"
    assert result["host_rejected"] == 0
    state = store.get_state("u", "planning", "calligraphy")
    assert state is not None and state.certainty == "tentative"
    assert any(s["key"] == "calligraphy" for s in result["sync_states_changed"])


def test_host_call_is_idempotent(svc):
    service, store = svc
    t = datetime.now(timezone.utc)
    observe(service, "e1", "下午可能去写书法", t, observations=[PLAN])
    again = observe(service, "e1", "下午可能去写书法", t, observations=[PLAN])
    assert again["status"] == "duplicate"


def test_sleep_claim_without_user_assertion_is_rejected(svc):
    """The agent may not turn a mention or an inference into sleep evidence."""
    service, store = svc
    t = datetime.now(timezone.utc)
    claim = {"type": "sleep", "category": "sleep", "key": "sleeping", "value": "sleeping",
             "certainty": "confirmed", "time_expression": "now", "raw_payload": "睡眠记录里是凌晨四点"}
    result = observe(service, "e1", "你看下睡眠记录里写的是几点", t, observations=[claim])
    assert result["host_rejected"] == 1
    assert store.get_state("u", "sleep", "sleeping") is None


def test_late_host_analysis_cannot_revive_older_state(svc):
    """Host observations arrive after the reply, at the message's original time:
    a newer state must win (strict replay rules)."""
    service, store = svc
    t1 = datetime.now(timezone.utc)
    t2 = t1 + timedelta(hours=7)
    observe(service, "e1", "我睡了", t1)
    observe(service, "e2", "我刚睡醒", t2)
    sleep = {"type": "sleep", "category": "sleep", "key": "sleeping", "value": "sleeping",
             "certainty": "confirmed", "time_expression": "now", "raw_payload": "我睡了"}
    observe(service, "e1", "我睡了", t1, observations=[sleep])
    assert store.get_state("u", "sleep", "awake").status == StateStatus.ACTIVE
    assert store.get_state("u", "sleep", "sleeping").status != StateStatus.ACTIVE


def test_host_event_adds_no_interaction_evidence(svc):
    service, store = svc
    t = datetime.now(timezone.utc)
    result = observe(service, "e1", "下午可能去写书法", t, observations=[PLAN])
    assert all(o["category"] != "presence" for o in result["host_observations"])


def test_contract_rejects_malformed_observations(svc):
    service, _ = svc
    contract = Contract(service)
    base = {"subject_id": "u", "event_id": "e1", "text": "下午可能去写书法"}
    with pytest.raises(ContractError):
        contract.observe({**base, "observations": {"not": "a list"}})
    with pytest.raises(ContractError):
        contract.observe({**base, "text": "", "observations": [PLAN]})
    assert contract.observe(base)["status"] == "accepted"  # classic observe unchanged
