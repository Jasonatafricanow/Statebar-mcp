"""Reviewer's exact three-step repro, run against the CURRENT code.

1. legacy A (awake) applied but NOT marked reconciled (old-code crash);
2. same-timestamp B (sleep) applies normally, overwriting last_observation_key;
3. recovery replays A.
Expected (fixed): NO rollback / NO resurrection.
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from statebar_mcp.core.extractor.persistent import MockExtractor
from statebar_mcp.core.models import ObserveRequest, Source
from statebar_mcp.core.service import UserStateService
from statebar_mcp.core.store import SQLiteStore

store = SQLiteStore(":memory:")
service = UserStateService(store, persistent_extractor=MockExtractor())
t = datetime.now(timezone.utc)

req_a = ObserveRequest(subject_id="u", event_id="eA", text="我刚睡醒",
                      source=Source(type="conversation"), observed_at=t)
req_b = ObserveRequest(subject_id="u", event_id="eB", text="我睡了",
                      source=Source(type="conversation"), observed_at=t)

# 1. legacy A: ingest + persist observation + apply WITHOUT tombstone
store.try_ingest_event("u", "eA", t)
obs_a = service.fast_extractor.extract("u", "eA", req_a.text, req_a.source, t)
store.insert_observations(obs_a)
rec = service.reconciler
rec._apply_inner(obs_a[0], rec._active_states("u"))

# 2. B applies normally
service.observe(req_b)
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    if store.get_event("u", "eB")["status"] == "complete":
        break
    time.sleep(0.02)

def snapshot():
    return [(s.key, s.status, s.last_observation_key[-3:])
            for s in store.get_states("u")]

print("重放前:")
for line in snapshot():
    print(" ", line)

# 3. recovery replays A
service.observe(req_a)
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    if store.get_event("u", "eA")["status"] == "complete":
        break
    time.sleep(0.02)

print("重放后:")
for line in snapshot():
    print(" ", line)

awake_rows = [s for s in store.get_states("u") if s.key == "awake"]
sleeping = store.get_state("u", "sleep", "sleeping")
assert sleeping.status == "active", "sleeping was rolled back!"
assert len(awake_rows) == 1 and awake_rows[0].status == "superseded", \
    "awake was resurrected!"
assert store.is_observation_applied("u", "eA", 0), "tombstone missing"
print("PASS: no rollback, no resurrection, tombstone written")
service.close()
