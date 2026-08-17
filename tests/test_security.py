"""Security & recovery tests (post-review hardening):

- REST bearer auth (401/200, constant-time, incl. /v1/health)
- fail-closed serve: non-loopback bind without token is refused
- request body size cap (413)
- LLM extraction privacy gate: external backend only when explicitly enabled
- env bool parsing ("0" is False)
- event status state machine: failed events resume on re-observe
- worker lifecycle: close() joins the worker before closing the store
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from statebar_mcp.config import (  # noqa: E402
    Config,
    ExtractorConfig,
    ServeConfig,
    build_persistent_extractor,
    load_config,
)
from statebar_mcp.core.extractor.persistent import (  # noqa: E402
    MockExtractor,
    OpenAICompatExtractor,
    PersistentExtractor,
)
from statebar_mcp.core.models import (  # noqa: E402
    Observation,
    ObserveRequest,
    Source,
)
from statebar_mcp.core.service import UserStateService  # noqa: E402
from statebar_mcp.core.store import SQLiteStore  # noqa: E402
from statebar_mcp.transports.rest import create_server  # noqa: E402


def _make_server(auth_token="", max_body_bytes=64 * 1024):
    service = UserStateService(SQLiteStore(":memory:"), persistent_extractor=MockExtractor())
    srv = create_server(service, "127.0.0.1", 0, auth_token=auth_token,
                        max_body_bytes=max_body_bytes)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return service, srv, port, thread


def _get(port, path, token=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _post(port, path, payload, token=None, raw_bytes=None):
    data = raw_bytes if raw_bytes is not None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


class TestRESTAuth:
    def test_health_requires_token_when_configured(self):
        service, srv, port, thread = _make_server(auth_token="sekrit")
        try:
            code, _ = _get(port, "/v1/health")
            assert code == 401  # fail-closed: even health is protected
            code, body = _get(port, "/v1/health", token="wrong")
            assert code == 401
            code, body = _get(port, "/v1/health", token="sekrit")
            assert code == 200 and body["status"] == "ok"
        finally:
            srv.shutdown(); srv.server_close(); service.close()

    def test_observe_requires_token(self):
        service, srv, port, thread = _make_server(auth_token="sekrit")
        try:
            payload = {"subject_id": "u", "event_id": "e1", "text": "我刚睡醒"}
            code, _ = _post(port, "/v1/observe", payload)
            assert code == 401
            code, body = _post(port, "/v1/observe", payload, token="sekrit")
            assert code == 200 and body["status"] == "accepted"
        finally:
            srv.shutdown(); srv.server_close(); service.close()

    def test_no_token_localhost_open(self):
        service, srv, port, thread = _make_server(auth_token="")
        try:
            code, body = _get(port, "/v1/health")
            assert code == 200
        finally:
            srv.shutdown(); srv.server_close(); service.close()

    def test_body_too_large_413(self):
        service, srv, port, thread = _make_server(max_body_bytes=1024)
        try:
            payload = {"subject_id": "u", "event_id": "e1", "text": "x" * 2000}
            code, body = _post(port, "/v1/observe", payload)
            assert code == 413
        finally:
            srv.shutdown(); srv.server_close(); service.close()


class TestFailClosedCLI:
    def test_non_loopback_without_token_refused(self, tmp_path, monkeypatch):
        import os

        env = {**os.environ, "PYTHONPATH": "", "DSH_USER_STATE_DB": str(tmp_path / "x.db")}
        out = subprocess.run(
            [sys.executable, "-m", "statebar_mcp", "serve", "--host", "0.0.0.0", "--port", "0"],
            capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=30,
        )
        assert out.returncode == 2
        assert "refusing to bind" in (out.stderr + out.stdout)

    def test_library_level_create_server_fail_closed(self):
        """The fail-closed invariant is enforced by create_server itself —
        a library caller cannot bypass it."""
        service = UserStateService(SQLiteStore(":memory:"))
        try:
            with pytest.raises(ValueError, match="without an auth"):
                create_server(service, "0.0.0.0", 0, auth_token="")
            # with a token, non-loopback is allowed
            srv = create_server(service, "0.0.0.0", 0, auth_token="t")
            srv.server_close()
        finally:
            service.close()


class TestPrivacyGate:
    def test_llm_backend_requires_explicit_enable(self):
        cfg = Config(
            extractor=ExtractorConfig(
                enabled=False, base_url="https://example.invalid/v1", model="m"
            )
        )
        ext = build_persistent_extractor(cfg)
        assert not isinstance(ext, OpenAICompatExtractor)
        assert ext.available() is False

        cfg.extractor.enabled = True
        ext2 = build_persistent_extractor(cfg)
        assert isinstance(ext2, OpenAICompatExtractor)

    def test_mock_allowed_without_enable(self):
        cfg = Config(extractor=ExtractorConfig(use_mock=True, enabled=False))
        assert isinstance(build_persistent_extractor(cfg), MockExtractor)

    def test_env_bool_parsing(self, monkeypatch):
        monkeypatch.setenv("DSH_USER_STATE_LLM_MOCK", "0")
        cfg = load_config()
        assert cfg.extractor.use_mock is False  # "0" must NOT be truthy
        monkeypatch.setenv("DSH_USER_STATE_LLM_MOCK", "1")
        assert load_config().extractor.use_mock is True
        monkeypatch.setenv("DSH_USER_STATE_LLM_MOCK", "true")
        assert load_config().extractor.use_mock is True
        monkeypatch.delenv("DSH_USER_STATE_LLM_MOCK")
        monkeypatch.setenv("DSH_USER_STATE_LLM_ENABLED", "0")
        assert load_config().extractor.enabled is False
        monkeypatch.setenv("DSH_USER_STATE_LLM_ENABLED", "1")
        assert load_config().extractor.enabled is True


class FlakyThenGoodExtractor(PersistentExtractor):
    """Fails on the first call, then behaves like the mock (for retry tests)."""

    name = "flaky_then_good"

    def __init__(self):
        self.calls = 0
        self.inner = MockExtractor()

    def available(self):
        return True

    def extract(self, request):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("backend unreachable (first attempt)")
        return self.inner.extract(request)


class TestEventRecovery:
    def test_failed_event_resumes_on_reobserve(self):
        store = SQLiteStore(":memory:")
        ext = FlakyThenGoodExtractor()
        service = UserStateService(store, persistent_extractor=ext)
        try:
            now = datetime.now(timezone.utc)
            req = ObserveRequest(
                subject_id="u", event_id="e1", text="下午可能去写书法",
                source=Source(type="conversation"), observed_at=now,
            )
            r1 = service.observe(req)
            assert r1["status"] == "accepted"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if store.get_event("u", "e1")["status"] == "failed":
                    break
                time.sleep(0.02)
            assert store.get_event("u", "e1")["status"] == "failed"

            # re-observe the same event: must RESUME, not duplicate, and the
            # second extraction attempt must finally produce the plan.
            r2 = service.observe(req)
            assert r2["status"] == "resumed"
            assert r2["resumed_from"] == "failed"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if store.get_event("u", "e1")["status"] == "complete":
                    break
                time.sleep(0.02)
            assert store.get_event("u", "e1")["status"] == "complete"
            plan = store.get_state("u", "planning", "calligraphy")
            assert plan is not None and plan.status == "tentative"

            # a third observe is a true duplicate now
            assert service.observe(req)["status"] == "duplicate"
        finally:
            service.close()

    def test_pending_event_resumes(self):
        store = SQLiteStore(":memory:")
        service = UserStateService(store, persistent_extractor=MockExtractor())
        try:
            now = datetime.now(timezone.utc)
            req = ObserveRequest(
                subject_id="u", event_id="e1", text="我刚睡醒",
                source=Source(type="conversation"), observed_at=now,
            )
            # simulate a crash BETWEEN ingest and sync commit
            store.try_ingest_event("u", "e1", now)  # stays 'pending'
            r = service.observe(req)
            assert r["status"] == "resumed"
            assert r["resumed_from"] == "pending"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if store.get_event("u", "e1")["status"] == "complete":
                    break
                time.sleep(0.02)
            assert store.get_event("u", "e1")["status"] == "complete"
            assert store.get_state("u", "sleep", "awake").status == "active"
        finally:
            service.close()


class TestReconcileTransactionAtomicity:
    """P1: the public reconcile() entry wraps state + transition + tombstone
    in ONE store transaction. The reviewer's fault injection showed the old
    entry half-committing (state=1, transition=0, tombstone=true after retry
    → the transition was lost forever and S2 was broken)."""

    def test_transition_failure_rolls_back_everything_then_retry_recovers(self):
        store = SQLiteStore(":memory:")
        service = UserStateService(store, persistent_extractor=MockExtractor())
        now = datetime.now(timezone.utc)
        obs = Observation(
            subject_id="u",
            event_id="e1",
            observation_index=0,
            type="awake",
            category="sleep",
            key="awake",
            value="awake",
            source=Source(type="conversation"),
            observed_at=now,
            raw_payload="我刚睡醒",
        )
        orig_record_transition = store.record_transition

        def failing_record_transition(transition):
            raise RuntimeError("simulated transition write failure (disk full)")

        try:
            store.record_transition = failing_record_transition  # type: ignore[method-assign]
            with pytest.raises(RuntimeError, match="disk full"):
                service.reconcile(obs)

            # The FIRST failure must leave NOTHING behind: no half-committed
            # state, no tombstone that would make the retry skip this
            # observation.
            assert store.counts()["states"] == 0, "state half-committed"
            assert store.counts()["state_transitions"] == 0
            assert not store.is_observation_applied("u", "e1", 0), (
                "tombstone written although the observation was rolled back"
            )

            # The retry (writer healthy again) must recover completely:
            # state + transition + tombstone all present, exactly once.
            store.record_transition = orig_record_transition  # type: ignore[method-assign]
            service.reconcile(obs)
            awake = store.get_state("u", "sleep", "awake")
            assert awake is not None and awake.status == "active"
            transitions = store.get_transitions(awake.state_id)
            assert len(transitions) == 1, "retry lost or duplicated the transition"
            assert store.is_observation_applied("u", "e1", 0)
        finally:
            store.record_transition = orig_record_transition  # type: ignore[method-assign]
            service.close()

    def test_reconcile_supersede_failure_keeps_pair_consistent(self):
        """The same atomicity must hold for the supersede path: sleeping →
        superseded writes one state row AND one transition row; a transition
        failure must roll the superseded status back too."""
        store = SQLiteStore(":memory:")
        service = UserStateService(store, persistent_extractor=MockExtractor())
        now = datetime.now(timezone.utc)
        sleep_obs = Observation(
            subject_id="u", event_id="e1", type="sleep",
            category="sleep", key="sleeping", value="sleeping",
            source=Source(type="conversation"), observed_at=now,
            raw_payload="我睡了",
        )
        wake_obs = Observation(
            subject_id="u", event_id="e2", type="awake",
            category="sleep", key="awake", value="awake",
            source=Source(type="conversation"),
            observed_at=now + timedelta(minutes=1),
            raw_payload="我刚睡醒",
        )
        orig_record_transition = store.record_transition
        try:
            service.reconcile(sleep_obs)
            sleeping = store.get_state("u", "sleep", "sleeping")
            assert sleeping.status == "active"

            def failing_record_transition(transition):
                raise RuntimeError("simulated transition write failure")

            store.record_transition = failing_record_transition  # type: ignore[method-assign]
            with pytest.raises(RuntimeError):
                service.reconcile(wake_obs)

            # rollback: sleeping is still ACTIVE (its supersede was undone)
            sleeping = store.get_state("u", "sleep", "sleeping")
            assert sleeping.status == "active", "superseded status half-committed"
            assert store.get_state("u", "sleep", "awake") is None

            store.record_transition = orig_record_transition  # type: ignore[method-assign]
            service.reconcile(wake_obs)
            sleeping = store.get_state("u", "sleep", "sleeping")
            assert sleeping.status == "superseded"
            awake = store.get_state("u", "sleep", "awake")
            assert awake is not None and awake.status == "active"
            # create + supersede for sleeping, create for awake
            assert len(store.get_transitions(sleeping.state_id)) == 2
            assert len(store.get_transitions(awake.state_id)) == 1
        finally:
            store.record_transition = orig_record_transition  # type: ignore[method-assign]
            service.close()


class SlowExtractor(PersistentExtractor):
    name = "slow"

    def __init__(self, delay=1.0, started=None):
        self.delay = delay
        self.started = started  # optional threading.Event set when extract begins

    def available(self):
        return True

    def extract(self, request):
        if self.started is not None:
            self.started.set()
        time.sleep(self.delay)
        return []


class TestWorkerLifecycle:
    def test_close_completes_inflight_then_closes_store(self, tmp_path):
        """An in-flight task MUST finish before the store closes: close()
        waits for it (bounded by the task's own duration)."""
        started = threading.Event()
        store = SQLiteStore(str(tmp_path / "inflight.db"))
        service = UserStateService(store, persistent_extractor=SlowExtractor(1.0, started))
        now = datetime.now(timezone.utc)
        req = ObserveRequest(
            subject_id="u", event_id="e1", text="我刚睡醒",
            source=Source(type="conversation"), observed_at=now,
        )
        service.observe(req)
        assert started.wait(2.0), "worker never started the task"
        worker = service._worker
        t0 = time.monotonic()
        service.close()
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.9, "close() returned before the in-flight task finished"
        assert not worker.is_alive()
        check = SQLiteStore(str(tmp_path / "inflight.db"))
        try:
            assert check.get_event("u", "e1")["status"] == "complete"
        finally:
            check.close()

    def test_close_drops_unstarted_work_cleanly(self, tmp_path):
        """Queued-but-unstarted tasks are skipped at shutdown: no write races
        after close, event stays recoverable (sync_committed)."""
        started = threading.Event()
        store = SQLiteStore(str(tmp_path / "drop.db"))
        service = UserStateService(store, persistent_extractor=SlowExtractor(1.5, started))
        now = datetime.now(timezone.utc)
        e1 = ObserveRequest(subject_id="u", event_id="e1", text="我刚睡醒",
                            source=Source(type="conversation"), observed_at=now)
        e2 = ObserveRequest(subject_id="u", event_id="e2", text="我睡了",
                            source=Source(type="conversation"), observed_at=now)
        service.observe(e1)  # worker starts this (slow)
        assert started.wait(2.0)
        service.observe(e2)  # queued behind e1
        worker = service._worker
        service.close()  # e1 finishes in-flight; e2 skipped
        assert not worker.is_alive()
        check = SQLiteStore(str(tmp_path / "drop.db"))
        try:
            assert check.get_event("u", "e1")["status"] == "complete"
            # e2 was never processed asynchronously but remains resumable
            assert check.get_event("u", "e2")["status"] == "sync_committed"
        finally:
            check.close()

    def test_worker_not_spawned_without_extractor(self):
        store = SQLiteStore(":memory:")
        service = UserStateService(store, persistent_extractor=PersistentExtractor())
        try:
            service.observe(
                ObserveRequest(
                    subject_id="u", event_id="e1", text="我刚睡醒",
                    source=Source(type="conversation"),
                    observed_at=datetime.now(timezone.utc),
                )
            )
            assert service._worker is None  # no thread for a disabled path
            assert store.get_event("u", "e1")["status"] == "complete"
        finally:
            service.close()

    def test_crash_between_obs_insert_and_reconcile_recovers(self):
        """Crash window: observation persisted but NOT yet reconciled. The
        next observe of the event must replay it and build the state."""
        store = SQLiteStore(":memory:")
        service = UserStateService(store, persistent_extractor=MockExtractor())
        now = datetime.now(timezone.utc)
        req = ObserveRequest(
            subject_id="u", event_id="e1", text="我刚睡醒",
            source=Source(type="conversation"), observed_at=now,
        )
        try:
            # simulate: previous attempt ingested the event and persisted the
            # overlay observation, then crashed BEFORE reconciling it
            store.try_ingest_event("u", "e1", now)
            obs = service.fast_extractor.extract("u", "e1", req.text, req.source, now)
            store.insert_observations(obs)  # committed, but reconciled=0
            assert store.get_state("u", "sleep", "awake") is None

            r = service.observe(req)
            assert r["status"] == "resumed"
            # the unreconciled leftover was replayed → state now exists
            assert store.get_state("u", "sleep", "awake").status == "active"
            # and it was marked reconciled → no double transitions later
            assert store.get_unreconciled_observations("u", "e1") == []
        finally:
            service.close()

    def test_replay_after_reconcile_before_mark_is_idempotent(self):
        """The reviewer's minimal repro: legacy crash BETWEEN reconcile and
        the reconciled mark. Replaying the same observation must NOT produce
        duplicate transitions (1 → must stay 1)."""
        store = SQLiteStore(":memory:")
        service = UserStateService(store, persistent_extractor=MockExtractor())
        now = datetime.now(timezone.utc)
        req = ObserveRequest(
            subject_id="u", event_id="e1", text="我刚睡醒",
            source=Source(type="conversation"), observed_at=now,
        )
        try:
            # simulate a crash of the OLD code path: ingest → insert obs →
            # reconcile (state + transition committed) → crash before the
            # reconciled=1 mark
            store.try_ingest_event("u", "e1", now)
            obs = service.fast_extractor.extract("u", "e1", req.text, req.source, now)
            store.insert_observations(obs)
            service._reconcile_all("u", obs)  # commits state + transition
            awake = store.get_state("u", "sleep", "awake")
            assert awake is not None
            transitions_before = store.get_transitions(awake.state_id)
            assert len(transitions_before) == 1  # reviewer repro: exactly 1

            r = service.observe(req)  # recovery replay
            assert r["status"] == "resumed"

            # replay must be a no-op for state mutation
            transitions_after = store.get_transitions(awake.state_id)
            assert len(transitions_after) == 1, (
                "replay duplicated transitions: %d -> %d"
                % (len(transitions_before), len(transitions_after))
            )
            assert store.get_unreconciled_observations("u", "e1") == []
        finally:
            service.close()

    def test_legacy_replay_same_timestamp_no_resurrection(self):
        """Reviewer's three-step repro: legacy observation A (awake) applied
        but left unreconciled; a DIFFERENT same-timestamp observation B
        (sleep) later owns the states. Replaying A must NOT resurrect awake
        or supersede sleeping — no rollback of same-second evidence."""
        store = SQLiteStore(":memory:")
        service = UserStateService(store, persistent_extractor=MockExtractor())
        t = datetime.now(timezone.utc)
        req_a = ObserveRequest(
            subject_id="u", event_id="eA", text="我刚睡醒",
            source=Source(type="conversation"), observed_at=t,
        )
        req_b = ObserveRequest(
            subject_id="u", event_id="eB", text="我睡了",
            source=Source(type="conversation"), observed_at=t,
        )
        try:
            # legacy A: ingest + persist observation + apply states WITHOUT
            # the applied tombstone (old code path), crash before the
            # reconciled mark
            store.try_ingest_event("u", "eA", t)
            obs_a = service.fast_extractor.extract("u", "eA", req_a.text, req_a.source, t)
            store.insert_observations(obs_a)
            rec = service.reconciler
            rec._apply_inner(obs_a[0], rec._active_states("u"))
            awake = store.get_state("u", "sleep", "awake")
            assert awake is not None and awake.status == "active"

            # B arrives normally, same semantic timestamp
            r_b = service.observe(req_b)
            assert r_b["status"] == "accepted"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if store.get_event("u", "eB")["status"] == "complete":
                    break
                time.sleep(0.02)
            sleeping = store.get_state("u", "sleep", "sleeping")
            assert sleeping is not None and sleeping.status == "active"
            transitions_before = len(store.get_transitions(sleeping.state_id))

            # recovery replays A
            r_a = service.observe(req_a)
            assert r_a["status"] == "resumed"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if store.get_event("u", "eA")["status"] == "complete":
                    break
                time.sleep(0.02)

            # no resurrection, no rollback: sleeping stays active and owns
            # the states; awake stays superseded; no new state rows
            sleeping = store.get_state("u", "sleep", "sleeping")
            assert sleeping.status == "active"
            assert store.get_state("u", "sleep", "awake").status == "superseded"
            awake_rows = [s for s in store.get_states("u") if s.key == "awake"]
            assert len(awake_rows) == 1, "awake was resurrected as a new row"
            assert len(store.get_transitions(sleeping.state_id)) == transitions_before
            # and the replayed observation is now tombstoned + reconciled
            assert store.is_observation_applied("u", "eA", 0)
            assert store.get_unreconciled_observations("u", "eA") == []
        finally:
            service.close()

    def test_incomplete_body_401_is_bounded(self):
        """P2: a client declaring Content-Length but sending nothing must
        still receive the 401 within a bounded time — the reject-path drain
        must not block forever."""
        import socket as socket_mod

        service = UserStateService(SQLiteStore(":memory:"))
        srv = create_server(service, "127.0.0.1", 0, auth_token="sekrit")
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            sock = socket_mod.create_connection(("127.0.0.1", port), timeout=5)
            sock.settimeout(5)
            # declare a 64 KiB body, then send NOTHING
            sock.sendall(
                b"POST /v1/observe HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 65536\r\n"
                b"Connection: close\r\n\r\n"
            )
            t0 = time.monotonic()
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    data += sock.recv(4096)
            finally:
                sock.close()
            elapsed = time.monotonic() - t0
            assert b"401" in data.split(b"\r\n")[0], data[:80]
            assert elapsed < 3.0, f"401 took {elapsed:.2f}s (unbounded drain)"
        finally:
            srv.shutdown()
            srv.server_close()
            service.close()
