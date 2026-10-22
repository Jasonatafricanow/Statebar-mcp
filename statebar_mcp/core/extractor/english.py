"""Small whole-message English rule pack; no substring or quoted-speech inference."""
import re


def statement(pattern: str) -> re.Pattern:
    # Deliberately require a complete first-person statement. Questions, quotes,
    # conditionals, past dates and extra clauses need the optional model path.
    return re.compile(r"\A\s*(?:" + pattern + r")[.!]?\s*\Z", re.IGNORECASE)


AWAKE = statement(r"I (?:just woke up|am awake(?: now)?)|I['’]m awake(?: now)?")
SLEEP = statement(r"I(?: am|['’]m) (?:going to (?:bed|sleep)|getting ready (?:for bed|to sleep))(?: now)?")
RULES = [
    (AWAKE, "awake", "sleep", "awake", "awake", "now"),
    (SLEEP, "sleep", "sleep", "sleeping", "preparing_sleep", "now"),
    (statement(r"I have a headache(?: now)?|My head hurts(?: now)?"),
     "symptom", "health", "headache", "active", "now"),
    (statement(r"My headache is gone|My head (?:does not|doesn't|doesn’t) hurt anymore"),
     "resolve", "health", "headache", "resolved", "now"),
    (statement(r"I (?:just took|have just taken) my (?:medicine|medication)"),
     "activity", "health", "medication", "taken", "today"),
]
