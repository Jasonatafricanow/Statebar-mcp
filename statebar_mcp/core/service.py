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
import hashlib
import queue
import threading
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from .extractor.fast_overlay import SYMPTOM_LEXICON, FastOverlayExtractor
from .extractor.persistent import PersistentExtractor
from .extractor import validator
from . import sleep_episodes
from . import reports
from .health_sync import HealthSync
from . import family
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
        health_sync: Optional[HealthSync] = None,
        family_members: Optional[list] = None,
    ):
        self.store = store
        self.health_sync = health_sync or HealthSync.from_env()
        self.family = family.load_members() if family_members is None else family_members
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
        self._symptoms_queued = False

    # ------------------------------------------------------------------ observe

    def observe(self, request: ObserveRequest) -> Dict:
        """Application-layer observe. Returns a JSON-safe result dict."""
        if request.observations is not None:
            return self._observe_host(request)
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
        # "嘻嘻喉咙疼": the fast rules cannot tell whose body it is, so a message naming a family member
        # leaves symptoms and medicine to the host's state block, which says ``who``.
        if sync_obs and family.mentioned(request.text, self.family):
            sync_obs = [o for o in sync_obs if o.category != "health"]
            for i, o in enumerate(sync_obs):
                o.observation_index = i
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
        self._flush_symptoms()

        result = {
            "status": "accepted" if not resuming else "resumed",
            "subject_id": subject_id,
            "event_id": event_id,
            "sync_observations": [o.to_dict() for o in sync_obs],
            "sync_states_changed": [self._state_out(s) for s in sync_applied],
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

    def _state_out(self, state) -> Dict:
        """State dict for observe results, plus the user's day its start belongs to
        (it changes when they sleep, not at midnight), so downstream consumers
        can date it the way the user would."""
        out = state.to_dict()
        try:
            out["user_day"] = self.reconciler.subjective_date(state.subject_id, state.valid_from).isoformat()
        except Exception:  # never let a label break observe
            logger.debug("user_day unavailable for state %s", state.state_id)
        return out

    def _observe_host(self, request: ObserveRequest) -> Dict:
        """Host-extracted observations: the host agent's own model already read the
        user's message as part of its normal turn and proposes Observation JSON
        (same contract as the persistent extractor). Nothing is trusted as-is:
        every item goes through the deterministic validator against ``text`` (the
        user's original words — semantic admission for sleep/wake included), then
        the Reconciler, exactly like the async persistent path.

        Recorded as its own event ``<event_id>#host`` (idempotent; the user's
        message event keeps its fast-rule/interaction observations). Like a late
        re-analysis it runs at the message's ORIGINAL semantic time with strict
        replay rules, so it can never revive or downgrade a state that a
        same-or-later observation owns. No interaction evidence is added: this is
        an analysis of an earlier message, not a new user interaction.
        """
        subject_id = request.subject_id
        host_event = f"{request.event_id}#host"
        existing = self.store.get_event(subject_id, host_event)
        if existing and existing["status"] == "complete":
            return {"status": "duplicate", "subject_id": subject_id, "event_id": host_event}
        if existing is None:
            self.store.try_ingest_event(subject_id, host_event, request.observed_at)
        payload = request.observations if isinstance(request.observations, list) else []
        # Retrospective sleep reports are their own record (core/sleep_episodes.py).
        sleep_items = [x for x in payload if isinstance(x, dict) and x.get("type") == "sleep_episode"]
        # Doses taken and workouts done are their own records too (core/reports.py).
        report_items = [x for x in payload if isinstance(x, dict) and x.get("type") in reports.KINDS]
        payload = [x for x in payload if not (isinstance(x, dict) and x.get("type") in ("sleep_episode", *reports.KINDS))]
        # A family member's symptom ("who") is kept on that member's subject, by the same rule as their doses:
        # the user's words must name them. Only symptoms; other states stay the user's own.
        family_payload, family_rejected = {}, []
        own_payload = []
        for x in payload:
            who = x.get("who") if isinstance(x, dict) else None
            if str(who or "").strip() in family.SELF_WORDS:
                own_payload.append(x)
                continue
            member, why = family.resolve(who, request.text, self.family, subject_id)
            if member is None or str(x.get("category", "")).strip() != "health":
                family_rejected.append(why or "only a family member's symptom can name who")
                continue
            family_payload.setdefault(member, []).append(x)
        payload = own_payload
        observations = validator.validate_observations(
            payload,
            subject_id,
            host_event,
            request.source,
            request.observed_at,
            start_index=0,
            source_text=request.text,
        )
        family_obs = {m: validator.validate_observations(items, m, host_event, request.source, request.observed_at,
                                                         start_index=0, source_text=request.text)
                      for m, items in family_payload.items()}
        leftovers = self.store.get_unreconciled_observations(subject_id, host_event)
        replay_keys = {(o.event_id, o.observation_index) for o in leftovers}
        with self.store.transaction():
            self.store.insert_observations(observations)
            unreconciled = self.store.get_unreconciled_observations(subject_id, host_event)
            for o in unreconciled:
                o.is_replay = True
            applied = self._reconcile_all(subject_id, unreconciled, replay_keys)
            self.store.mark_observations_reconciled(subject_id, host_event, unreconciled)
            for member, obs_list in family_obs.items():
                if self.store.get_event(member, host_event) is None:
                    self.store.try_ingest_event(member, host_event, request.observed_at)
                self.store.insert_observations(obs_list)
                pending = self.store.get_unreconciled_observations(member, host_event)
                for o in pending:
                    o.is_replay = True
                applied += self._reconcile_all(member, pending)
                self.store.mark_observations_reconciled(member, host_event, pending)
                self.store.set_event_status(member, host_event, "complete")
            sleep_kept, sleep_rejected = self._record_sleep(request, host_event, sleep_items)
            reports_kept, reports_rejected = self._record_reports(request, host_event, report_items)
            self.store.set_event_status(subject_id, host_event, "complete")
        if any(self.health_sync.wants(k["subject_id"]) for k in sleep_kept):
            try:
                self.health_sync.flush(self.store)
            except Exception:  # delivery is best-effort; rows stay queued
                logger.exception("health sync flush failed")
        if any(self.health_sync.wants(k["subject_id"]) for k in reports_kept) or self._take_symptoms_flag():
            try:
                self.health_sync.flush_reports(self.store)
            except Exception:
                logger.exception("report sync flush failed")
        return {
            "status": "accepted" if existing is None else "resumed",
            "subject_id": subject_id,
            "event_id": host_event,
            "host_observations": [o.to_dict() for o in observations],
            "host_rejected": len(payload) - len(observations) + len(sleep_rejected) + len(reports_rejected)
            + len(family_rejected) + sum(len(family_payload[m]) - len(family_obs[m]) for m in family_obs),
            "family_observations": [o.to_dict() for obs_list in family_obs.values() for o in obs_list],
            "family_rejected": family_rejected,
            "sleep_episodes": sleep_kept,
            "sleep_rejected": sleep_rejected,
            "reports": reports_kept,
            "reports_rejected": reports_rejected,
            "sync_states_changed": [self._state_out(s) for s in applied],
            "async_scheduled": False,
        }

    # -- retrospective sleep ----------------------------------------------------

    def _record_sleep(self, request: ObserveRequest, event_id: str, items: List[Dict[str, Any]]):
        """Validate reported sleep episodes against the user's words, supersede overlapping
        older reports (stated beats inferred, then the later message), queue the daily
        totals for the health plugin. Runs inside the caller's transaction."""
        if not items:
            return [], []
        activity = self.store.get_activity_times(request.subject_id, request.observed_at - sleep_episodes.MAX_LOOKBACK)
        kept, rejected, touched = [], [], set()
        for i, raw in enumerate(items[:5]):
            # Whose sleep: the user's own unless the item names a family member the user mentioned.
            subject_id, why = family.resolve(raw.get("who") if isinstance(raw, dict) else None, request.text,
                                             self.family, request.subject_id)
            ep = None
            if subject_id is not None:
                own = subject_id == request.subject_id
                ep, why = sleep_episodes.validate_episode(
                    raw, subject_id=subject_id, event_id=event_id, index=i, text=request.text,
                    observed_at=request.observed_at, message_id=request.source.message_id,
                    activity_times=activity if own else None, infer=own)
            if ep is None:
                rejected.append(why)
                continue
            if not self.store.insert_sleep_episode(ep):
                continue  # same event replayed
            day = date.fromisoformat(ep.wake_date)
            near = self.store.get_sleep_episodes(subject_id, (day - timedelta(days=1)).isoformat(), status="active")
            for old in near:
                if old.episode_id == ep.episode_id or not ep.overlaps(old):
                    continue
                if sleep_episodes.replaces(ep, old):
                    self.store.supersede_sleep_episode(old.episode_id, ep.episode_id)
                    touched.add((subject_id, old.wake_date))
                else:
                    self.store.supersede_sleep_episode(ep.episode_id, old.episode_id)
                    ep.status, ep.superseded_by = sleep_episodes.SUPERSEDED, old.episode_id
                    break
            touched.add((subject_id, ep.wake_date))
            kept.append(ep.to_dict())
        for subject_id, wake_date in touched:
            if self.health_sync.wants(subject_id):
                body = sleep_episodes.daily_payload(
                    subject_id, wake_date, self.store.get_sleep_episodes(subject_id, wake_date, status="active"))
                if body is not None:
                    self.store.queue_sleep_sync(subject_id, wake_date, body)
        return kept, rejected

    def _record_reports(self, request: ObserveRequest, event_id: str, items: List[Dict[str, Any]]):
        """Doses and workouts: whose (family rules as for sleep), checked against the user's words, kept,
        and left pending for the health plugin. Runs inside the caller's transaction."""
        kept, rejected = [], []
        for i, raw in enumerate(items[:5]):
            subject_id, why = family.resolve(raw.get("who"), request.text, self.family, request.subject_id)
            report = None
            if subject_id is not None:
                report, why = reports.validate(raw, subject_id=subject_id, event_id=event_id, index=100 + i,
                                               text=request.text, observed_at=request.observed_at,
                                               message_id=request.source.message_id)
            if report is None:
                rejected.append(why)
                continue
            if self.store.insert_report(report):
                kept.append(report.to_dict())
        return kept, rejected

    def import_sleep_history(self, subject_id: str, import_id: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Import an already reviewed sleep history (each item names its source: 原话/台账/日记/推理).

        The review happened offline against the original records; statebar keeps each
        episode with its source reference and local clock, checks only shape and
        plausibility, never supersedes during an import, and then queues the days whose
        every sleep has a known length. Idempotent per (import_id, index); a later live
        report can supersede an imported episode by the usual rules."""
        if not import_id or not isinstance(items, list) or not items or len(items) > 2000:
            raise ValueError("import_id and 1..2000 items are required")
        inserted, rejected, days = 0, [], set()
        with self.store.transaction():
            for i, raw in enumerate(items):
                try:
                    ep = sleep_episodes.history_episode(raw, subject_id=subject_id, event_id=f"history:{import_id}", index=i)
                except ValueError as exc:
                    rejected.append({"index": i, "reason": str(exc)})
                    continue
                if self.store.insert_sleep_episode(ep):
                    inserted += 1
                    days.add(ep.wake_date)
            queued = 0
            if self.health_sync.wants(subject_id):
                for wake_date in sorted(days):
                    body = sleep_episodes.daily_payload(
                        subject_id, wake_date, self.store.get_sleep_episodes(subject_id, wake_date, status="active"))
                    if body is not None and self.store.queue_sleep_sync(subject_id, wake_date, body):
                        queued += 1
        sync = self.health_sync.flush(self.store, limit=1000) if queued else {"configured": self.health_sync.configured}
        return {"subject_id": subject_id, "import_id": import_id, "inserted": inserted, "rejected": rejected,
                "days": len(days), "days_queued_for_health": queued, "health_sync": sync}

    def get_sleep(self, subject_id: str, since: str = "") -> Dict[str, Any]:
        """Reported sleep episodes (active and superseded), daily totals and sync status."""
        episodes = self.store.get_sleep_episodes(subject_id, since)
        days = {}
        for ep in episodes:
            if ep.status == sleep_episodes.ACTIVE and ep.minutes:
                days[ep.wake_date] = days.get(ep.wake_date, 0) + ep.minutes
        return {
            "subject_id": subject_id,
            "episodes": [e.to_dict() for e in episodes],
            "daily_minutes": days,
            "health_sync": {"configured": self.health_sync.configured,
                            "subject_id": self.health_sync.subject_id,
                            "rows": [{k: r[k] for k in ("wake_date", "status", "attempts", "last_error", "payload")}
                                     for r in self.store.get_sleep_sync(subject_id)]},
        }

    # -- manual correction (audit console) ---------------------------------------

    def supersede_sleep(self, subject_id: str, episode_id: str, reason: str, actor: str = "") -> Dict[str, Any]:
        """Mark one episode as wrong (never deleted): superseded_by = ``manual:<reason>``."""
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("reason is required to supersede a sleep episode")
        return self._correct_sleep(subject_id, episode_id, "supersede", reason[:300], actor)

    def restore_sleep(self, subject_id: str, episode_id: str, reason: str = "", actor: str = "") -> Dict[str, Any]:
        """Bring a superseded episode back to active (status active, superseded_by cleared)."""
        return self._correct_sleep(subject_id, episode_id, "restore", (reason or "").strip()[:300], actor)

    def _correct_sleep(self, subject_id: str, episode_id: str, action: str, reason: str, actor: str) -> Dict[str, Any]:
        ep = self.store.get_sleep_episode(episode_id)
        if ep is None or ep.subject_id != subject_id:
            raise ValueError(f"sleep episode not found for {subject_id}: {episode_id}")
        target = sleep_episodes.SUPERSEDED if action == "supersede" else sleep_episodes.ACTIVE
        if ep.status == target:  # idempotent: no second audit row, nothing re-queued
            return {"changed": False, "episode": ep.to_dict(), "audit_id": None,
                    **self._day_after_correction(subject_id, ep.wake_date, requeue=False)}
        before = (ep.status, ep.superseded_by)
        with self.store.transaction():
            if action == "supersede":
                self.store.supersede_sleep_episode(episode_id, f"manual:{reason}")
            else:
                self.store.restore_sleep_episode(episode_id)
            after = self.store.get_sleep_episode(episode_id)
            audit_id = self.store.insert_sleep_audit({
                "subject_id": subject_id, "episode_id": episode_id, "wake_date": ep.wake_date, "action": action,
                "actor": (actor or "rest").strip()[:100], "reason": reason,
                "before_status": before[0], "before_superseded_by": before[1],
                "after_status": after.status, "after_superseded_by": after.superseded_by})
            day = self._day_after_correction(subject_id, ep.wake_date, requeue=True)
        if day["health_queued"]:
            try:
                self.health_sync.flush(self.store)
            except Exception:  # delivery is best-effort; the row stays queued
                logger.exception("health sync flush failed")
        out = {"changed": True, "episode": after.to_dict(), "audit_id": audit_id, **day}
        if action == "restore":
            # Restoring can bring back a sleep that a later report replaced: both would count.
            out["overlaps_active"] = [o.episode_id for o in self.store.get_sleep_episodes(
                subject_id, (date.fromisoformat(ep.wake_date) - timedelta(days=1)).isoformat(), status="active")
                if o.episode_id != episode_id and after.overlaps(o)]
        return out

    def _day_after_correction(self, subject_id: str, wake_date: str, requeue: bool) -> Dict[str, Any]:
        """The corrected day's active total, re-queued for the health plugin. When the day is left
        with no complete total there is nothing to send (the plugin has no 'clear a day')."""
        active = self.store.get_sleep_episodes(subject_id, wake_date, status="active")
        minutes = sum(e.minutes or 0 for e in active if e.wake_date == wake_date)
        body = sleep_episodes.daily_payload(subject_id, wake_date, active)
        queued = False
        if requeue and body is not None and self.health_sync.wants(subject_id):
            queued = self.store.queue_sleep_sync(subject_id, wake_date, body)
        note = "" if body is not None else "no complete total for this day; the health plugin keeps its last value"
        return {"wake_date": wake_date, "daily_minutes": minutes, "health_payload": body,
                "health_queued": queued, "health_note": note}

    def get_sleep_audit(self, subject_id: str, since: str = "", episode_id: str = "") -> Dict[str, Any]:
        return {"subject_id": subject_id,
                "audit": self.store.get_sleep_audit(subject_id, since_date=since, episode_id=episode_id)}

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
                # The async worker re-extracts an already-sync-processed
                # event at its ORIGINAL semantic time. Its fresh rows are
                # late analyses, not crash leftovers: re-tag them with the
                # strict same-or-later ownership rules (the transient
                # is_replay flag does not survive the store reload) so a
                # re-analysis can NEVER downgrade or revive a state that a
                # DIFFERENT observation at the same-or-later semantic time
                # owns — D8 acceptance flake regression (the re-extracted
                # symptom used to re-affirm ACTIVE over e8's IMPROVING).
                for o in unreconciled:
                    if (o.event_id, o.observation_index) not in replay_keys:
                        o.is_replay = True
                self._reconcile_all(subject_id, unreconciled, replay_keys)
                self.store.mark_observations_reconciled(subject_id, event_id, unreconciled)
                self.store.set_event_status(subject_id, event_id, "complete")
            self._flush_symptoms()
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
            applied = self.reconciler.apply(obs)
            for state in applied:
                self._queue_symptom(state, obs)
            changed.extend(applied)
        return changed

    # -- symptoms to the health plugin ------------------------------------------

    _SYMPTOM_SENT = (StateStatus.ACTIVE, StateStatus.IMPROVING, StateStatus.RESOLVED)

    def _queue_symptom(self, state: State, obs: Observation) -> None:
        """A symptom the user said appeared, eased or went away goes one way to the health plugin, which
        lays it beside the doses on a timeline. Only what the user said (active / improving / resolved);
        a symptom that merely timed out is not an event. Runs inside the caller's transaction; one row
        per (state, observation, status), so a replayed observation is not sent twice."""
        if state.category != "health" or state.status not in self._SYMPTOM_SENT:
            return
        if not self.health_sync.wants(state.subject_id):
            return
        parts = [p for p, k, _ in SYMPTOM_LEXICON if k == state.key]  # 喉咙 / 嗓子: the word the user used
        part = next((p for p in parts if p in (obs.raw_payload or "")), parts[0] if parts else "")
        label = (part + ("不舒服" if "discomfort" in state.key else "疼")) if part else "不舒服"
        index = int(hashlib.sha256(f"{obs.event_id}:{obs.observation_index}:{state.status}".encode()).hexdigest()[:12], 16)
        report = reports.Report(state.subject_id, "symptom", state.state_id, index, obs.observed_at,
                                {"symptom": label, "key": state.key, "status": state.status},
                                obs.raw_payload or "", obs.source.message_id)
        if self.store.insert_report(report):
            self._symptoms_queued = True

    def _take_symptoms_flag(self) -> bool:
        queued, self._symptoms_queued = self._symptoms_queued, False
        return queued

    def _flush_symptoms(self) -> None:
        if not self._take_symptoms_flag():
            return
        try:
            self.health_sync.flush_reports(self.store)
        except Exception:  # best-effort; rows stay pending for the next flush
            logger.exception("symptom sync flush failed")

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
