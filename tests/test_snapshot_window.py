"""Plan labels and windows follow the user's day, which changes when they sleep,
not at midnight; snapshot labels are relative to that day, not to the window's end clock."""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from statebar_mcp.core import lifecycle  # noqa: E402
from statebar_mcp.core.extractor.persistent import PersistentExtractor  # noqa: E402
from statebar_mcp.core.models import ObserveRequest, Source  # noqa: E402
from statebar_mcp.core.service import UserStateService  # noqa: E402
from statebar_mcp.core.store import SQLiteStore  # noqa: E402

LOCAL_TZ = datetime.now().astimezone().tzinfo


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=LOCAL_TZ)


class World:
    def __init__(self):
        self.now = at(15, 12)
        self.service = UserStateService(SQLiteStore(":memory:"), persistent_extractor=PersistentExtractor(),
                                        now_fn=lambda: self.now)
        self.n = 0

    def say(self, when, text="嗯", plan=None):
        self.now = when
        self.n += 1
        request = dict(subject_id="u", event_id=f"e{self.n}", text=text, source=Source(type="conversation"),
                       observed_at=when)
        self.service.observe(ObserveRequest(**request))
        if plan:
            self.service.observe(ObserveRequest(**request, observations=[{
                "type": "plan", "category": "planning", "key": "hiking", "value": "go hiking",
                "certainty": "planned", "time_expression": plan, "raw_payload": text}]))

    def hiking(self, when):
        self.now = when
        return next((i.text for i in self.service.get_snapshot("u").items if "hiking" in i.text), None)

    def close(self):
        self.service.close()


def test_tomorrow_plan_is_not_rendered_as_tonight():
    w = World()
    try:
        w.say(at(15, 15), "明天去爬山", plan="tomorrow")
        assert w.hiking(at(15, 15, 5)).startswith("tomorrow ")
        # after a night's rest, the same plan is today's
        assert w.hiking(at(16, 9)).startswith("today ")
    finally:
        w.close()


def test_tomorrow_said_past_midnight_before_sleeping_means_after_the_sleep():
    w = World()
    try:
        w.say(at(15, 9))
        w.say(at(15, 23))
        w.say(at(16, 1), "明天去爬山", plan="tomorrow")
        # still the 15th for the user: the plan is "tomorrow", i.e. the 16th after sleeping
        assert w.hiking(at(16, 1, 5)).startswith("tomorrow ")
        assert w.hiking(at(16, 10)).startswith("today ")
        assert w.hiking(at(17, 10)) is None  # it was not the 17th
    finally:
        w.close()


def test_tonight_said_past_midnight_is_the_ongoing_night():
    w = World()
    try:
        w.say(at(15, 22))
        w.say(at(16, 1), "今晚还要去吃夜宵", plan="tonight")
        assert w.hiking(at(16, 1, 5)).startswith("today ") or w.hiking(at(16, 1, 5)).startswith("hiking")
    finally:
        w.close()


def test_subjective_date_rules():
    states = []
    # awake past midnight, no rest: still the previous day
    assert lifecycle.subjective_date(at(16, 1), [at(15, 9), at(15, 23), at(16, 1)], states) == date(2026, 11, 15)
    # a night's silence that ends in the morning starts a new day
    assert lifecycle.subjective_date(at(16, 9), [at(15, 23), at(16, 9)], states) == date(2026, 11, 16)
    # back home at 00:30 after five quiet hours: not a new day
    assert lifecycle.subjective_date(at(16, 0, 30), [at(15, 19, 30), at(16, 0, 30)], states) == date(2026, 11, 15)
    # no evidence: calendar date
    assert lifecycle.subjective_date(at(16, 1), [], states) == date(2026, 11, 16)


def test_explicit_sleep_and_wake_switch_the_day():
    w = World()
    try:
        w.say(at(15, 23))
        w.say(at(16, 2), "我要睡了")
        w.say(at(16, 4, 30), "我刚睡醒")  # explicit wake before 05:00 still counts
        reconciler = w.service.reconciler
        assert reconciler.subjective_date("u", at(16, 4, 35)) == date(2026, 11, 16)
        assert reconciler.subjective_date("u", at(16, 1, 30)) == date(2026, 11, 15)
    finally:
        w.close()
