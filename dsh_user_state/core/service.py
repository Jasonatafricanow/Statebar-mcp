"""UserStateService — the application-layer contract (派单总纲 §6/§8/§9).

The ONLY entry point for observing user state:

    observe(request) synchronous path:
      idempotency check (subject_id, event_id)
      → Fast Overlay (rules) → provisional Observations
      → Reconciler → commit → "accepted"

    observe(request) asynchronous path (background thread):
      Persistent LLM Extraction → deterministic Validator
      → Reconciler → commit

The asynchronous path is best-effort by design: when the extractor is
blocked/unavailable (D3) the synchronous path has already committed and the
snapshot stays correct.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional

from . import lifecycle
from .extractor.fast_overlay import FastOverlayExtractor
from .extractor.persistent import PersistentExtractor
from .lifecycle import lazy_expire
from .models import (
    ObserveRequest,
    Observation,
    Snapshot,
    State,
    StateStatus,
    utc_now,
)
from .reconciler import Reconciler
from .snapshot import SnapshotBuilder
from .store import SQLiteStore

logger = logging.getLogger(__name__)

__version__ = "0.1.0"


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
        self._threads: List[threading.Thread] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ observe

    def observe(self, request: ObserveRequest) -> Dict:
        """Application-layer observe. Returns a JSON-safe result dict."""
        subject_id, event_id = request.subject_id, request.event_id

        ingested = self.store.try_ingest_event(
            subject_id, event_id, request.observed_at
        )
        if not ingested:
            return {"status": "duplicate", "subject_id": subject_id, "event_id": event_id}

        # ---- synchronous path: Fast Overlay → Reconciler → commit
        sync_obs = self.fast_extractor.extract(
            subject_id, event_id, request.text, request.source, request.observed_at
        )
        self.store.insert_observations(sync_obs)
        sync_applied = self._reconcile_all(subject_id, sync_obs)
        self.store.set_event_status(subject_id, event_id, "sync_committed")

        result = {
            "status": "accepted",
            "subject_id": subject_id,
            "event_id": event_id,
            "sync_observations": [o.to_dict() for o in sync_obs],
            "sync_states_changed": [s.to_dict() for s in sync_applied],
            "async_scheduled": False,
        }

        # ---- asynchronous path: Persistent LLM extraction (best-effort)
        if self.persistent_extractor.available():
            result["async_scheduled"] = True
            self._schedule_async(request, start_index=len(sync_obs))
        return result

    def _schedule_async(self, request: ObserveRequest, start_index: int) -> None:
        def worker() -> None:
            if self._closed.is_set():
                return
            try:
                observations = self.persistent_extractor.extract(request)
                if observations:
                    for obs in observations:
                        obs.observation_index = start_index + obs.observation_index
                        obs.subject_id = request.subject_id
                        obs.event_id = request.event_id
                    self.store.insert_observations(observations)
                    self._reconcile_all(request.subject_id, observations)
                self.store.set_event_status(request.subject_id, request.event_id, "complete")
            except Exception:  # D3: async failure never breaks the sync path
                logger.exception("persistent extraction failed for event %s", request.event_id)

        thread = threading.Thread(
            target=worker, name=f"persistent-extract-{request.event_id}", daemon=True
        )
        with self._lock:
            if self._closed.is_set():
                return
            self._threads.append(thread)
        thread.start()

    def _reconcile_all(self, subject_id: str, observations: List[Observation]) -> List[State]:
        changed: List[State] = []
        # semantic ordering (D12): older observations first
        for obs in sorted(observations, key=lambda o: (o.observed_at, o.observation_index)):
            changed.extend(self.reconciler.apply(obs))
        return changed

    def reconcile(self, observation: Observation) -> List[State]:
        """Exposed for transports that already carry validated Observations."""
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
            "service": "dsh-user-state",
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
        self._closed.set()
        self.persistent_extractor.close()
        self.store.close()
