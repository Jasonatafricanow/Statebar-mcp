"""Retrospective sleep episodes: the user's own report of a sleep that already happened.
Real phrases from the deployment (11-16..11-19) that statebar used to drop."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from statebar_mcp.core.extractor.persistent import PersistentExtractor  # noqa: E402
from statebar_mcp.core.health_sync import HealthSync  # noqa: E402
from statebar_mcp.core.models import ObserveRequest, Source  # noqa: E402
from statebar_mcp.core.service import UserStateService  # noqa: E402
from statebar_mcp.core.store import SQLiteStore  # noqa: E402

CN = timezone(timedelta(hours=8))
SUBJECT = "xiyue-user"


def at(text_time: str) -> datetime:
    return datetime.fromisoformat(text_time).replace(tzinfo=CN)


@pytest.fixture
def svc():
    posted = []
    sync = HealthSync(url="http://health.test/api/health-webhook", token="t", subject_id=SUBJECT,
                      post=lambda url, token, body: posted.append(body) or {"ok": True})
    service = UserStateService(SQLiteStore(":memory:"), persistent_extractor=PersistentExtractor(), health_sync=sync)
    yield service, posted
    service.close()


def say(service, event_id, text, when, items=None, subject=SUBJECT):
    """The user's message (fast path + interaction) and then the host's extraction of it."""
    req = dict(subject_id=subject, event_id=event_id, text=text, source=Source(type="conversation"), observed_at=when)
    service.observe(ObserveRequest(**req))
    if items is not None:
        return service.observe(ObserveRequest(**req, observations=items))
    return None


def ep(start=None, end=None, **kw):
    return {"type": "sleep_episode", "start": start and at(start).isoformat(), "end": end and at(end).isoformat(),
            "start_basis": "stated" if start else None, "end_basis": "stated" if end else None, **kw}


def test_stated_sleep_is_recorded_and_sent_to_health(svc):
    service, posted = svc
    r = say(service, "m1", "七点半睡的，十点半醒了", at("2026-11-18T11:54"), [ep("2026-11-18T07:30", "2026-11-18T10:30")])
    assert r["sleep_rejected"] == [] and len(r["sleep_episodes"]) == 1
    e = r["sleep_episodes"][0]
    assert (e["start_local"], e["end_local"], e["minutes"], e["basis"]) == ("11-18 07:30", "11-18 10:30", 180, "stated")
    assert posted == [{"sleepHours": 3.0, "measuredAt": "2026-11-18T10:30:00+08:00",
                       "sourceEventId": posted[0]["sourceEventId"], "source": "statebar"}]


def test_a_time_the_user_did_not_say_is_refused(svc):
    service, posted = svc
    # the 11-13 failure mode: "凌晨四点" written down as 04:20
    r = say(service, "m1", "凌晨四点才睡", at("2026-11-13T11:00"), [ep("2026-11-13T04:20", None, wake_date="2026-11-13")])
    assert r["sleep_episodes"] == [] and "not in the user's words" in r["sleep_rejected"][0]
    assert service.get_sleep(SUBJECT)["episodes"] == [] and posted == []


def test_mentions_and_questions_are_not_reports(svc):
    service, _ = svc
    r = say(service, "m1", "现在健康插件还是没有睡眠记录对不对", at("2026-11-19T06:53"),
            [ep("2026-11-18T22:00", "2026-11-19T00:50")])
    assert r["sleep_episodes"] == [] and r["sleep_rejected"]


def test_nap_and_night_sum_by_wake_day_and_overnight_crosses_midnight(svc):
    service, posted = svc
    say(service, "m1", "美美睡了俩小时，今天陪娃睡的", at("2026-11-18T16:30"),
        [{"type": "sleep_episode", "duration_min": 120, "wake_date": "2026-11-18"}])
    r = say(service, "m2", "十点陪娃一起睡了，十二点五十醒了", at("2026-11-19T01:26"),
            [ep("2026-11-18T22:00", "2026-11-19T00:50")])
    assert r["sleep_episodes"][0]["wake_date"] == "2026-11-19"
    assert service.get_sleep(SUBJECT)["daily_minutes"] == {"2026-11-18": 120, "2026-11-19": 170}
    assert [p["sleepHours"] for p in posted] == [2.0, 2.83]


def test_wake_only_is_inferred_from_the_silence_before_it(svc):
    service, _ = svc
    say(service, "m0", "晚安啦", at("2026-11-16T02:10"))
    r = say(service, "m1", "老婆我醒了", at("2026-11-16T09:05"), [{"type": "sleep_episode"}])
    e = r["sleep_episodes"][0]
    assert (e["start_local"], e["end_local"], e["basis"]) == ("11-16 02:10", "11-16 09:05", "inferred")
    assert (e["start_basis"], e["end_basis"]) == ("inferred", "inferred")


def test_a_stated_correction_supersedes_and_the_new_total_replaces_the_day(svc):
    service, posted = svc
    say(service, "m0", "晚安啦", at("2026-11-16T02:10"))
    say(service, "m1", "老婆我醒了", at("2026-11-16T09:05"), [{"type": "sleep_episode"}])
    r = say(service, "m2", "不对，我是三点睡的，九点醒的", at("2026-11-16T09:20"),
            [ep("2026-11-16T03:00", "2026-11-16T09:00")])
    sleep = service.get_sleep(SUBJECT)
    assert sorted(e["status"] for e in sleep["episodes"]) == ["active", "superseded"]
    assert sleep["daily_minutes"] == {"2026-11-16": 360}
    assert posted[-1]["sleepHours"] == 6.0 and posted[-1]["sourceEventId"] != posted[0]["sourceEventId"]
    # an inferred guess can never override what the user said
    r = say(service, "m3", "我又醒了", at("2026-11-16T09:40"), [{"type": "sleep_episode", "end": at("2026-11-16T09:00").isoformat()}])
    assert r["sleep_episodes"][0]["status"] == "superseded"
    assert service.get_sleep(SUBJECT)["daily_minutes"] == {"2026-11-16": 360}


def test_replay_and_unconfigured_sync_are_harmless(svc):
    service, posted = svc
    items = [ep("2026-11-18T07:30", "2026-11-18T10:30")]
    say(service, "m1", "七点半睡的，十点半醒了", at("2026-11-18T11:54"), items)
    again = say(service, "m1", "七点半睡的，十点半醒了", at("2026-11-18T11:54"), items)
    assert again["status"] == "duplicate" and len(posted) == 1
    other = UserStateService(SQLiteStore(":memory:"), persistent_extractor=PersistentExtractor(),
                             health_sync=HealthSync())
    say(other, "m1", "七点半睡的，十点半醒了", at("2026-11-18T11:54"), items)
    rows = other.get_sleep(SUBJECT)["health_sync"]
    assert rows["configured"] is False and rows["rows"][0]["status"] == "pending"
    other.close()


def test_an_explicit_wo_wakes_after_an_address_term_but_not_in_reported_speech(svc):
    from statebar_mcp.core.extractor import validator
    for text, ok in [("老婆我醒了", True), ("宝宝我已经醒了", True), ("我没说我醒了", False), ("你问我醒了吗", False)]:
        assert validator._semantic_admission_ok("awake", "sleep", "awake", "", text) is ok, text


def test_a_same_length_nap_is_not_mistaken_for_last_nights_sleep(svc):
    service, _ = svc
    say(service, "m1", "十点不到就睡了，但是十二点不到醒了", at("2026-11-18T00:20"),
        [ep("2026-11-17T22:00", "2026-11-18T00:00")])
    say(service, "m2", "就睡了俩小时", at("2026-11-18T00:40"),  # restating it right after waking
        [{"type": "sleep_episode", "duration_min": 120, "wake_date": "2026-11-18"}])
    assert service.get_sleep(SUBJECT)["daily_minutes"] == {"2026-11-18": 120}
    say(service, "m3", "美美睡了俩小时", at("2026-11-18T16:30"),  # an afternoon nap
        [{"type": "sleep_episode", "duration_min": 120, "wake_date": "2026-11-18"}])
    assert service.get_sleep(SUBJECT)["daily_minutes"] == {"2026-11-18": 240}
