"""UserStateService — the application-layer contract (派单总纲 §6/§8/§9).

The ONLY entry point for observing user state:

    observe(request) synchronous path:
      idempotency check (subject_id, event_id)
      → Fast Overlay (rules) → provisional Observations
      → Reconciler → commit → "accepted"

    observe(request) asynchronous path (single background worker):
      Persistent LLM Extraction → deterministic Validator
      → Reconciler → commit

Event status state machine (recoverable, never swallows failures):

    pending        event row inserted, processing not committed yet
    sync_committed sync path committed (Fast Overlay + reconcile)
    complete       async path finished (successful extraction, possibly empty)
    failed         async extraction crashed → a later observe of the SAME
                   event_id resumes processing instead of being dropped

The asynchronous path is best-effort by design: when the extractor is
blocked/unavailable (D3) the synchronous path has already committed and the
snapshot stays correct.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Dict, List, Optional

from .extractor.fast_overlay import FastOverlayExtractor
from .extractor.persistent import PersistentExtractor
from .lifecycle import lazy_expire
from .models import (
    Certainty,
    ObserveRequest,
    Observation,
    ObservationType,
    Snapshot,
    Source,
    SourceType,
    State,
    StateStatus,
    utc_now,
)
from .reconciler import Reconciler
from .snapshot import SnapshotBuilder
from .store import SQLiteStore

logger = logging.getLogger(__name__)

__version__ = "0.1.0"

_SENTINEL = object()
_WORKER_JOIN_TIMEOUT_S = 5.0


class UserStateService:
    """Stable internal Python interface (transport layer calls this)."""

    def __init__(
        self,
        store: SQLiteStore,
        fast_extractor: Optional[FastOverlayExtractor] = None,
        persistent_extractor: Optional[PersistentExtractor] = None,
        now_fn=utc_now,
    ):
        self.store = store
        self.fast_extractor = fast_extractor or FastOverlayExtractor()
        self.persistent_extractor = persistent_extractor or PersistentExtractor()
        self._now_fn = now_fn
        self.reconciler = Reconciler(store, now_fn=now_fn)
        self.snapshot_builder = SnapshotBuilder(store, now_fn=now_fn)
        self._closed = threading.Event()
        # ONE bounded-lifecycle worker for the whole service (no per-event
        # thread growth): tasks queue up, the worker drains them, close()
        # joins it before closing the store.
        self._queue: "queue.Queue" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()
        # Events whose async extraction is queued or in-flight right now.
        self._in_flight: set = set()
        self._in_flight_lock = threading.Lock()

    # ------------------------------------------------------------------ observe

    def observe(self, request: ObserveRequest) -> Dict:
        """Application-layer observe. Returns a JSON-safe result dict."""
        subject_id, event_id = request.subject_id, request.event_id

        existing = self.store.get_event(subject_id, event_id)
        status = existing["status"] if existing else None

        if status == "complete":
            return {"status": "duplicate", "subject_id": subject_id, "event_id": event_id}

        if status is None:
            self.store.try_ingest_event(subject_id, event_id, request.observed_at)
            resuming = False
        else:
            # pending → previous sync path crashed before commit; failed →
            # previous async extraction crashed. Both are recoverable.
            resuming = True

        # ---- synchronous path: Fast Overlay → Reconciler → commit.
        # The event row stays 'pending' until this fully commits, so a crash
        # here can be retried instead of being swallowed as duplicate.
        # ONE SQLite transaction: observation insert + state/transition
        # writes + reconciled marks + applied tombstones + event status
        # commit atomically — recovery replay can never double-apply any
        # part of it.
        sync_obs = self.fast_extractor.extract(
            subject_id, event_id, request.text, request.source, request.observed_at
        )
        # V2 first-class signal: the user's real-time interaction itself is
        # evidence (observed, confidence 1.0), independent of the message's
        # language. Assistant questions never produce it (V2-T4), and diary
        # writes are not real-time interactions.
        if request.source.type == SourceType.CONVERSATION and sync_obs is not None:
            sync_obs = list(sync_obs) + [self._interaction_observation(request, len(sync_obs))]
        # leftovers = persisted-but-unreconciled rows from a crashed earlier
        # attempt; they are replayed with STRICT conflict rules (is_replay).
        leftovers = self.store.get_unreconciled_observations(subject_id, event_id)
        replay_keys = {(o.event_id, o.observation_index) for o in leftovers}
        with self.store.transaction():
            self.store.insert_observations(sync_obs)
            unreconciled = self.store.get_unreconciled_observations(subject_id, event_id)
            sync_applied = self._reconcile_all(subject_id, unreconciled, replay_keys)
            self.store.mark_observations_reconciled(subject_id, event_id, unreconciled)
            self.store.set_event_status(subject_id, event_id, "sync_committed")

        result = {
            "status": "accepted" if not resuming else "resumed",
            "subject_id": subject_id,
            "event_id": event_id,
            "sync_observations": [o.to_dict() for o in sync_obs],
            "sync_states_changed": [s.to_dict() for s in sync_applied],
            "async_scheduled": False,
        }
        if status in ("pending", "failed"):
            result["resumed_from"] = status

        # ---- asynchronous path: Persistent LLM extraction (best-effort).
        if self.persistent_extractor.available():
            if self._mark_in_flight(subject_id, event_id):
                if self._ensure_worker():
                    start_index = self.store.count_event_observations(subject_id, event_id)
                    self._queue.put((request, start_index))
                    result["async_scheduled"] = True
                else:
                    self._unmark_in_flight(subject_id, event_id)
        else:
            # nothing more will ever happen for this event
            self.store.set_event_status(subject_id, event_id, "complete")
        return result

    # -- worker (single thread, bounded lifecycle) -----------------------------

    def _interaction_observation(self, request: ObserveRequest, index: int) -> Observation:
        """V2 §4.1/§6: the user's real-time interaction is itself evidence —
        deterministic, no LLM, certainty=observed, confidence=1.0."""
        return Observation(
            subject_id=request.subject_id,
            event_id=request.event_id,
            observation_index=index,
            type=ObservationType.ACTIVITY,
            category="presence",
            key="interactive_activity",
            value="true",
            certainty=Certainty.OBSERVED,
            source=Source(
                type=SourceType.INTERACTION,
                platform=request.source.platform,
                session_id=request.source.session_id,
                message_id=request.source.message_id,
            ),
            observed_at=request.observed_at,
            confidence=1.0,
            raw_payload="",
        )

    def _mark_in_flight(self, subject_id: str, event_id: str) -> bool:
        key = (subject_id, event_id)
        with self._in_flight_lock:
            if key in self._in_flight:
                return False
            self._in_flight.add(key)
            return True

    def _unmark_in_flight(self, subject_id: str, event_id: str) -> None:
        with self._in_flight_lock:
            self._in_flight.discard((subject_id, event_id))

    def _ensure_worker(self) -> bool:
        with self._worker_lock:
            if self._closed.is_set():
                return False
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._worker_loop,
                    name="statebar-persistent-worker",
                    daemon=True,
                )
                self._worker.start()
            return True

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                return
            request, start_index = item
            if self._closed.is_set():
                # shutdown in progress: drop queued-but-unstarted work. Its
                # event stays sync_committed and will resume on a later
                # re-observe (in-flight set is cleared here).
                self._unmark_in_flight(request.subject_id, request.event_id)
                continue
            try:
                self._process_async(request, start_index)
            finally:
                self._unmark_in_flight(request.subject_id, request.event_id)

    def _process_async(self, request: ObserveRequest, start_index: int) -> None:
        subject_id, event_id = request.subject_id, request.event_id
        try:
            observations = self.persistent_extractor.extract(request)
            leftovers = self.store.get_unreconciled_observations(subject_id, event_id)
            replay_keys = {(o.event_id, o.observation_index) for o in leftovers}
            with self.store.transaction():
                if observations:
                    for obs in observations:
                        obs.observation_index = start_index + obs.observation_index
                        obs.subject_id = subject_id
                        obs.event_id = event_id
                    self.store.insert_observations(observations)
                # reconcile EVERY unreconciled observation of the event (new
                # ones plus leftovers from a previous crashed attempt), then
                # mark them reconciled — all in the SAME transaction as the
                # state/transition writes and the final event status.
                unreconciled = self.store.get_unreconciled_observations(subject_id, event_id)
                self._reconcile_all(subject_id, unreconciled, replay_keys)
                self.store.mark_observations_reconciled(subject_id, event_id, unreconciled)
                self.store.set_event_status(subject_id, event_id, "complete")
        except Exception:  # D3: async failure never breaks the sync path
            logger.exception("persistent extraction failed for event %s", event_id)
            try:
                self.store.set_event_status(subject_id, event_id, "failed")
            except Exception:  # store may already be closed during shutdown
                logger.debug("could not persist final event status for %s", event_id)

    def _reconcile_all(
        self,
        subject_id: str,
        observations: List[Observation],
        replay_keys: set = frozenset(),
    ) -> List[State]:
        changed: List[State] = []
        # semantic ordering (D12): older observations first
        for obs in sorted(observations, key=lambda o: (o.observed_at, o.observation_index)):
            if (obs.event_id, obs.observation_index) in replay_keys:
                obs.is_replay = True  # strict conflict rules for leftovers
            changed.extend(self.reconciler.apply(obs))
        return changed

    def reconcile(self, observation: Observation) -> List[State]:
        """Exposed for transports that already carry validated Observations.

        Wraps the reconciler in ONE store transaction: state rows, transition
        rows and the applied-observation tombstone commit atomically. When any
        write fails (e.g. the transition insert), the whole observation is
        rolled back — a retry re-applies it cleanly instead of permanently
        losing the transition (S2: state and history can never diverge).
        """
        with self.store.transaction():
            return self.reconciler.apply(observation)

    # ----------------------------------------------------------------- snapshot

    def build_snapshot(self, subject_id: str, query: str = "") -> Snapshot:
        return self.snapshot_builder.build(subject_id, query=query)

    def get_snapshot(self, subject_id: str, query: str = "") -> Snapshot:
        return self.build_snapshot(subject_id, query=query)

    def get_current_state(self, subject_id: str) -> List[State]:
        """Current (non-terminal) states after lazy expiration."""
        states = self.store.get_states(subject_id)
        transitions, mutated = lazy_expire(states, self._now_fn())
        for tr in transitions:
            self.store.record_transition(tr)
        for st in mutated:
            self.store.insert_state(st)
        return [s for s in states if s.status not in StateStatus.TERMINAL]

    # -------------------------------------------------------------------- state

    def get_state(
        self, subject_id: str, category: str = "", key: str = ""
    ) -> List[Dict]:
        states = self.get_current_state(subject_id)
        if category:
            states = [s for s in states if s.category == category]
        if key:
            states = [s for s in states if s.key == key]
        return [s.to_dict() for s in states]

    # -------------------------------------------------------- context candidates

    def get_context_candidates(self, subject_id: str) -> Dict:
        """Buckets for proactive-conversation material (心潮 Phase 3 consumes
        this; the service only supplies the material, never the decision)."""
        now = self._now_fn()
        states = self.store.get_states(subject_id)
        transitions, mutated = lazy_expire(states, now)
        for tr in transitions:
            self.store.record_transition(tr)
        for st in mutated:
            self.store.insert_state(st)

        recent, unresolved, planned, followup = [], [], [], []
        for s in states:
            if s.status in StateStatus.TERMINAL:
                if (
                    s.status in (StateStatus.COMPLETED, StateStatus.CANCELLED, StateStatus.RESOLVED)
                    and s.relevant_until is not None
                    and s.relevant_until > now
                ):
                    recent.append(s.to_dict())
                continue
            d = s.to_dict()
            if s.category == "planning" and s.status in (
                StateStatus.TENTATIVE, StateStatus.PLANNED, StateStatus.PENDING,
            ):
                planned.append(d)
            if s.followup_relevant:
                unresolved.append(d)
                followup.append(d)
            if s.category == "activity" and s.snapshot_priority >= 2:
                recent.append(d)
        return {
            "subject_id": subject_id,
            "recent": recent,
            "unresolved": unresolved,
            "followup": followup,
            "planned": planned,
            "interesting": recent,
        }

    # ------------------------------------------------------------------- health

    def healthcheck(self) -> Dict:
        return {
            "status": "ok",
            "service": "statebar-mcp",
            "version": __version__,
            "counts": self.store.counts(),
            "fast_extractor": self.fast_extractor.name,
            "persistent_extractor": {
                "name": self.persistent_extractor.name,
                "available": self.persistent_extractor.available(),
            },
        }

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        """Stop accepting work, skip queued-but-unstarted tasks, and make
        sure the worker has fully EXITED before the extractor/store are
        closed — an in-flight LLM request is bounded by the extractor's own
        timeout, so this join is bounded too and no background thread can
        ever write into a closed connection."""
        self._closed.set()
        try:
            self._queue.put_nowait(_SENTINEL)
        except queue.Full:  # pragma: no cover (unbounded queue)
            pass
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            grace = getattr(self.persistent_extractor, "timeout", _WORKER_JOIN_TIMEOUT_S) + 5.0
            worker.join(timeout=grace)
            if worker.is_alive():
                logger.warning(
                    "persistent worker still busy after %.1fs; waiting for the "
                    "in-flight extraction to finish (bounded by its own timeout)",
                    grace,
                )
                worker.join()
        self.persistent_extractor.close()
        self.store.close()
