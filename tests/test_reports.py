"""Doses taken and workouts done (O40): the user's words, checked, then handed to the health plugin."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from statebar_mcp.core.extractor.persistent import PersistentExtractor  # noqa: E402
from statebar_mcp.core.family import parse_members  # noqa: E402
from statebar_mcp.core.health_sync import HealthSync  # noqa: E402
from statebar_mcp.core.service import UserStateService  # noqa: E402
from statebar_mcp.core.store import SQLiteStore  # noqa: E402
from test_sleep_episodes import SUBJECT, at, say  # noqa: E402

FAMILY = {"members": [{"subject_id": "kid", "name": "娃", "aliases": ["娃", "嘻嘻"]}]}


@pytest.fixture
def svc():
    posted = []
    sync = HealthSync(url="http://health.test/api/health-webhook", token="t", subject_id=SUBJECT, members={"kid": "kid"},
                      post=lambda url, token, body: posted.append((url, body)) or {"ok": True})
    service = UserStateService(SQLiteStore(":memory:"), persistent_extractor=PersistentExtractor(), health_sync=sync,
                               family_members=parse_members(FAMILY))
    yield service, posted
    service.close()


def test_a_dose_the_user_named_is_kept_and_sent_with_its_time(svc):
    service, posted = svc
    r = say(service, "d1", "嘻嘻发烧了，八点给她喂了美林5ml", at("2026-11-20T09:10"),
            [{"type": "dose", "who": "娃", "medication": "美林", "taken_at": at("2026-11-20T08:00").isoformat(), "amount": "5ml"}])
    assert r["reports_rejected"] == [] and r["reports"][0]["subject_id"] == "kid"
    url, body = posted[-1]
    assert url == "http://health.test/api/dose-webhook"
    assert body["medication"] == "美林" and body["memberId"] == "kid" and body["takenAt"].startswith("2026-11-20T00:00")


def test_just_now_is_the_message_time_and_a_replay_is_not_sent_twice(svc):
    service, posted = svc
    say(service, "d2", "刚吃了布洛芬", at("2026-11-20T09:00"), [{"type": "dose", "medication": "布洛芬"}])
    say(service, "d2", "刚吃了布洛芬", at("2026-11-20T09:00"), [{"type": "dose", "medication": "布洛芬"}])
    doses = [b for u, b in posted if u.endswith("dose-webhook")]
    assert len(doses) == 1 and "memberId" not in doses[0]


def test_unsaid_medicine_or_time_is_not_written(svc):
    service, posted = svc
    r = say(service, "d3", "吃了药", at("2026-11-20T09:00"),
            [{"type": "dose", "medication": "布洛芬"},
             {"type": "dose", "medication": "药", "taken_at": at("2026-11-20T07:00").isoformat()}])
    assert r["reports"] == [] and len(r["reports_rejected"]) == 2
    assert not [u for u, _ in posted if u.endswith("dose-webhook")]


def test_workout_length_and_day_must_be_said(svc):
    service, posted = svc
    r = say(service, "w1", "昨天游泳游了四十分钟", at("2026-11-20T09:00"),
            [{"type": "workout", "activity": "游泳", "duration_min": 40, "date": "2026-11-19"}])
    assert r["reports_rejected"] == []
    url, body = posted[-1]
    assert url.endswith("workout-webhook") and body["date"] == "2026-11-19" and body["durationMin"] == 40
    r = say(service, "w2", "去跑步了", at("2026-11-20T09:00"),
            [{"type": "workout", "activity": "跑步", "duration_min": 30},
             {"type": "workout", "activity": "跑步", "date": "2026-11-18"}])
    assert r["reports"] == [] and len(r["reports_rejected"]) == 2


def test_symptoms_the_user_said_go_to_the_timeline_once_each(svc):
    service, posted = svc
    say(service, "s1", "喉咙疼", at("2026-11-21T09:00"))
    say(service, "s1", "喉咙疼", at("2026-11-21T09:00"))  # replayed message
    say(service, "s2", "喉咙不疼了", at("2026-11-21T20:00"))
    sent = [b for u, b in posted if u.endswith("symptom-webhook")]
    assert [(b["symptom"], b["status"]) for b in sent] == [("喉咙疼", "active"), ("喉咙疼", "resolved")]
    assert sent[0]["episodeId"] == sent[1]["episodeId"] and sent[0]["at"].startswith("2026-11-21T01:00")
    assert "memberId" not in sent[0]  # the plugin's primary user
    assert len({b["sourceEventId"] for b in sent}) == 2
