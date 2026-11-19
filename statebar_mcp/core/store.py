"""SQLite persistence for the User State Layer.

Four core tables (派单总纲 §13, v1.1-FIX2):

    ingested_events    event-level idempotency  UNIQUE(subject_id, event_id)
    observations       evidence                 UNIQUE(subject_id, event_id, observation_index)
    states             canonical current/recent state rows
    state_transitions  change history (S2: never destructive overwrite)

All timestamps are stored as ISO8601 UTC strings; the *semantic* time of an
observation is ``observed_at`` (caller-supplied), never DB inserted_at.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .sleep_episodes import SleepEpisode
from .models import (
    Observation,
    Source,
    State,
    StateStatus,
    StateTransition,
    dt_to_iso,
    parse_dt,
    utc_now,
)

logger = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS ingested_events (
    subject_id  TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ingested',
    created_at  TEXT NOT NULL,
    PRIMARY KEY (subject_id, event_id)
);

CREATE TABLE IF NOT EXISTS observations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id         TEXT NOT NULL,
    event_id           TEXT NOT NULL,
    observation_index  INTEGER NOT NULL DEFAULT 0,
    type               TEXT NOT NULL,
    category           TEXT NOT NULL DEFAULT '',
    key                TEXT NOT NULL DEFAULT '',
    value              TEXT NOT NULL DEFAULT '',
    source_type        TEXT NOT NULL DEFAULT 'conversation',
    source_platform    TEXT NOT NULL DEFAULT 'cli',
    source_session_id  TEXT NOT NULL DEFAULT '',
    source_message_id  TEXT NOT NULL DEFAULT '',
    observed_at        TEXT NOT NULL,
    time_expression    TEXT NOT NULL DEFAULT '',
    certainty          TEXT NOT NULL DEFAULT 'confirmed',
    confidence         REAL NOT NULL DEFAULT 1.0,
    raw_payload        TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    reconciled         INTEGER NOT NULL DEFAULT 0,
    UNIQUE (subject_id, event_id, observation_index)
);

CREATE INDEX IF NOT EXISTS idx_obs_subject_time
    ON observations (subject_id, observed_at);

CREATE TABLE IF NOT EXISTS states (
    state_id              TEXT PRIMARY KEY,
    subject_id            TEXT NOT NULL,
    category              TEXT NOT NULL DEFAULT '',
    key                   TEXT NOT NULL DEFAULT '',
    value                 TEXT NOT NULL DEFAULT '',
    status                TEXT NOT NULL DEFAULT 'created',
    certainty             TEXT NOT NULL DEFAULT 'confirmed',
    valid_from            TEXT NOT NULL,
    valid_until           TEXT NOT NULL,
    relevant_until        TEXT NOT NULL,
    followup_relevant     INTEGER NOT NULL DEFAULT 0,
    snapshot_priority     INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    last_observed_at      TEXT NOT NULL,
    last_observation_key  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_states_subject
    ON states (subject_id, category, key);

CREATE TABLE IF NOT EXISTS state_transitions (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    state_id               TEXT NOT NULL,
    subject_id             TEXT NOT NULL,
    from_status            TEXT NOT NULL,
    to_status              TEXT NOT NULL,
    reason                 TEXT NOT NULL DEFAULT '',
    source_observation_id  INTEGER,
    created_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transitions_state
    ON state_transitions (state_id, id);

-- Persistent "this observation was already applied" records (tombstones).
-- Unlike the per-state last_observation_key (which later observations
-- overwrite), this set is append-only: replaying an applied observation is
-- blocked at the reconciler entry point, regardless of what happened to
-- the states afterwards.
CREATE TABLE IF NOT EXISTS observation_applications (
    subject_id         TEXT NOT NULL,
    event_id           TEXT NOT NULL,
    observation_index  INTEGER NOT NULL,
    PRIMARY KEY (subject_id, event_id, observation_index)
);

-- Retrospective sleep episodes the user reported (see core/sleep_episodes.py).
-- Never deleted: a corrected report marks the older one superseded.
CREATE TABLE IF NOT EXISTS sleep_episodes (
    episode_id         TEXT PRIMARY KEY,
    subject_id         TEXT NOT NULL,
    event_id           TEXT NOT NULL,
    idx                INTEGER NOT NULL,
    wake_date          TEXT NOT NULL,
    start_at           TEXT,
    end_at             TEXT,
    duration_min       INTEGER,
    start_basis        TEXT,
    end_basis          TEXT,
    duration_basis     TEXT,
    observed_at        TEXT NOT NULL,
    source_text        TEXT NOT NULL DEFAULT '',
    source_message_id  TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'active',
    superseded_by      TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    offset_min         INTEGER NOT NULL DEFAULT 480
);
CREATE INDEX IF NOT EXISTS idx_sleep_episodes_day ON sleep_episodes (subject_id, wake_date);

-- One-way outbox to the health plugin: the latest daily total per wake date.
CREATE TABLE IF NOT EXISTS sleep_sync (
    subject_id       TEXT NOT NULL,
    wake_date        TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT NOT NULL DEFAULT '',
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (subject_id, wake_date)
);
"""


def _iso(dt: datetime) -> str:
    return dt_to_iso(dt)


class SQLiteStore:
    """Thread-safe wrapper around the four-table SQLite schema."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """In-place upgrades for databases created by older versions."""
        obs_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(observations)")}
        if "reconciled" not in obs_cols:
            self._conn.execute(
                "ALTER TABLE observations ADD COLUMN reconciled INTEGER NOT NULL DEFAULT 0"
            )
            # Only rows written by the pre-migration code path exist here;
            # that path reconciled inline, so they are all done.
            self._conn.execute("UPDATE observations SET reconciled=1")
        if "time_expression" not in obs_cols:
            # v0.1 dropped time_expression on persist; historical rows keep ''
            # (treated as unspecified), new rows carry the real expression.
            self._conn.execute(
                "ALTER TABLE observations ADD COLUMN time_expression TEXT NOT NULL DEFAULT ''"
            )
        sleep_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(sleep_episodes)")}
        if "offset_min" not in sleep_cols:
            self._conn.execute("ALTER TABLE sleep_episodes ADD COLUMN offset_min INTEGER NOT NULL DEFAULT 480")
        state_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(states)")}
        if "last_observation_key" not in state_cols:
            self._conn.execute(
                "ALTER TABLE states ADD COLUMN last_observation_key TEXT NOT NULL DEFAULT ''"
            )
        # Backfill the applied-observation tombstone table from existing
        # evidence: observations already marked reconciled were applied by
        # definition; so were the observations recorded as the last writer
        # of any state row.
        self._conn.execute(
            "INSERT OR IGNORE INTO observation_applications "
            "(subject_id, event_id, observation_index) "
            "SELECT subject_id, event_id, observation_index "
            "FROM observations WHERE reconciled=1"
        )
        for row in self._conn.execute(
            "SELECT DISTINCT subject_id, last_observation_key FROM states "
            "WHERE last_observation_key != ''"
        ).fetchall():
            key = row["last_observation_key"]
            if "::" in key:
                event_id, _, index = key.rpartition("::")
            else:
                event_id, _, index = key.rpartition(":")
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO observation_applications "
                    "(subject_id, event_id, observation_index) VALUES (?, ?, ?)",
                    (row["subject_id"], event_id, int(index)),
                )
            except (ValueError, sqlite3.Error):
                continue

    # -- transactions -----------------------------------------------------------

    def _commit_if_needed(self) -> None:
        """Commit only when NOT inside an explicit transaction() block."""
        if self._tx_depth == 0:
            self._conn.commit()

    @contextmanager
    def transaction(self):
        """One SQLite transaction: BEGIN IMMEDIATE ... COMMIT/ROLLBACK.

        Nested calls are supported (depth counter). All mutating store
        methods defer their commit while inside this block, so a group of
        state writes + transitions + reconciled marks + event status becomes
        atomic: a crash leaves either everything or nothing, and recovery
        replay can never double-apply."""
        with self._lock:
            self._tx_depth += 1
            try:
                if self._tx_depth == 1:
                    self._conn.execute("BEGIN IMMEDIATE")
                yield
                if self._tx_depth == 1:
                    self._conn.commit()
            except Exception:
                if self._tx_depth == 1:
                    self._conn.rollback()
                raise
            finally:
                self._tx_depth -= 1

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:  # pragma: no cover
                pass

    # -- events (event-level idempotency) -----------------------------------

    def try_ingest_event(self, subject_id: str, event_id: str, observed_at: datetime,
                         status: str = "pending") -> bool:
        """Insert the event (status 'pending' by default); returns False when
        it already exists. The event only moves to 'sync_committed' AFTER the
        synchronous path fully commits, so a crash leaves it retryable."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO ingested_events "
                "(subject_id, event_id, observed_at, status, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (subject_id, event_id, _iso(observed_at), status, _iso(utc_now())),
            )
            self._commit_if_needed()
            return cur.rowcount > 0

    def get_event(self, subject_id: str, event_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ingested_events WHERE subject_id=? AND event_id=?",
                (subject_id, event_id),
            ).fetchone()
            return dict(row) if row else None

    def set_event_status(self, subject_id: str, event_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE ingested_events SET status=? WHERE subject_id=? AND event_id=?",
                (status, subject_id, event_id),
            )
            self._commit_if_needed()

    # -- observations (observation-level idempotency) ------------------------

    def insert_observations(self, observations: List[Observation]) -> List[Observation]:
        """Insert evidence rows; duplicates are ignored. Returns the list of
        observations that were actually NEWLY inserted (the caller reconciles
        exactly those, so a retried event never double-records transitions)."""
        inserted: List[Observation] = []
        with self._lock:
            for obs in observations:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO observations "
                    "(subject_id, event_id, observation_index, type, category, key, value, "
                    " source_type, source_platform, source_session_id, source_message_id, "
                    " observed_at, time_expression, certainty, confidence, raw_payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        obs.subject_id,
                        obs.event_id,
                        obs.observation_index,
                        obs.type,
                        obs.category,
                        obs.key,
                        obs.value,
                        obs.source.type,
                        obs.source.platform,
                        obs.source.session_id,
                        obs.source.message_id,
                        _iso(obs.observed_at),
                        obs.time_expression,
                        obs.certainty,
                        obs.confidence,
                        obs.raw_payload,
                        _iso(utc_now()),
                    ),
                )
                # rowcount is the reliable signal for INSERT OR IGNORE:
                # 1 = inserted, 0 = duplicate ignored.
                if cur.rowcount > 0:
                    inserted.append(obs)
            self._commit_if_needed()
        return inserted

    def count_event_observations(self, subject_id: str, event_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM observations WHERE subject_id=? AND event_id=?",
                (subject_id, event_id),
            ).fetchone()
        return int(row[0])

    # -- reconcile tracking (crash recovery) -----------------------------------

    def is_observation_applied(self, subject_id: str, event_id: str,
                               observation_index: int) -> bool:
        """True when this observation was already reconciled (tombstone)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM observation_applications "
                "WHERE subject_id=? AND event_id=? AND observation_index=?",
                (subject_id, event_id, observation_index),
            ).fetchone()
        return row is not None

    def mark_observation_applied(self, subject_id: str, event_id: str,
                                 observation_index: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO observation_applications "
                "(subject_id, event_id, observation_index) VALUES (?, ?, ?)",
                (subject_id, event_id, observation_index),
            )
            self._commit_if_needed()

    def get_unreconciled_observations(
        self, subject_id: str, event_id: str
    ) -> List[Observation]:
        """Observations persisted but not yet reconciled — the crash window
        between insert and reconcile. Replayed on the next observe of the
        event, so no state is ever permanently lost."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM observations WHERE subject_id=? AND event_id=? "
                "AND reconciled=0 ORDER BY observed_at, observation_index",
                (subject_id, event_id),
            ).fetchall()
        return [self._obs_from_row(dict(r)) for r in rows]

    def mark_observations_reconciled(
        self, subject_id: str, event_id: str, observations: List[Observation]
    ) -> None:
        with self._lock:
            for obs in observations:
                self._conn.execute(
                    "UPDATE observations SET reconciled=1 "
                    "WHERE subject_id=? AND event_id=? AND observation_index=?",
                    (subject_id, event_id, obs.observation_index),
                )
            self._commit_if_needed()

    def get_observations(self, subject_id: str, limit: int = 200) -> List[Observation]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM observations WHERE subject_id=? ORDER BY observed_at DESC, id DESC LIMIT ?",
                (subject_id, limit),
            ).fetchall()
        return [self._obs_from_row(dict(r)) for r in rows]

    def get_observation_id(
        self, subject_id: str, event_id: str, observation_index: int
    ) -> Optional[int]:
        """Resolve the persisted observations.id for an evidence key
        (subject/event/index). Returns None when the row does not exist."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM observations "
                "WHERE subject_id=? AND event_id=? AND observation_index=?",
                (subject_id, event_id, observation_index),
            ).fetchone()
        return int(row["id"]) if row else None

    @staticmethod
    def _obs_from_row(row: Dict[str, Any]) -> Observation:
        return Observation(
            subject_id=row["subject_id"],
            event_id=row["event_id"],
            observation_index=row["observation_index"],
            type=row["type"],
            category=row["category"],
            key=row["key"],
            value=row["value"],
            certainty=row["certainty"],
            time_expression=row["time_expression"],
            source=Source(
                type=row["source_type"],
                platform=row["source_platform"],
                session_id=row["source_session_id"],
                message_id=row["source_message_id"],
            ),
            observed_at=parse_dt(row["observed_at"]),
            confidence=row["confidence"],
            raw_payload=row["raw_payload"],
        )

    def get_activity_times(self, subject_id: str, since: datetime) -> List[datetime]:
        """Semantic times of the subject's observations since ``since`` (any
        source except assistant questions), used to find rest gaps."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT observed_at FROM observations WHERE subject_id=? AND observed_at >= ? "
                "AND source_type != 'assistant_question' ORDER BY observed_at",
                (subject_id, _iso(since)),
            ).fetchall()
        return [parse_dt(r["observed_at"]) for r in rows]

    # -- states --------------------------------------------------------------

    def get_states(self, subject_id: str) -> List[State]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM states WHERE subject_id=? ORDER BY updated_at DESC",
                (subject_id,),
            ).fetchall()
        return [State.from_dict(dict(r)) for r in rows]

    def get_state(self, subject_id: str, category: str, key: str) -> Optional[State]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM states WHERE subject_id=? AND category=? AND key=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (subject_id, category, key),
            ).fetchone()
        return State.from_dict(dict(row)) if row else None

    def get_latest_state_by_key(self, subject_id: str, key: str) -> Optional[State]:
        """Latest state with this key across ALL categories and statuses."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM states WHERE subject_id=? AND key=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (subject_id, key),
            ).fetchone()
        return State.from_dict(dict(row)) if row else None

    def get_active_states(self, subject_id: str) -> List[State]:
        states = self.get_states(subject_id)
        return [s for s in states if s.status not in StateStatus.TERMINAL]

    def insert_state(self, state: State) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO states "
                "(state_id, subject_id, category, key, value, status, certainty, "
                " valid_from, valid_until, relevant_until, followup_relevant, "
                " snapshot_priority, created_at, updated_at, last_observed_at, "
                " last_observation_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    state.state_id,
                    state.subject_id,
                    state.category,
                    state.key,
                    state.value,
                    state.status,
                    state.certainty,
                    _iso(state.valid_from),
                    _iso(state.valid_until),
                    _iso(state.relevant_until),
                    int(state.followup_relevant),
                    state.snapshot_priority,
                    _iso(state.created_at),
                    _iso(state.updated_at),
                    _iso(state.last_observed_at),
                    state.last_observation_key,
                ),
            )
            self._commit_if_needed()

    def record_transition(self, transition: StateTransition) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO state_transitions "
                "(state_id, subject_id, from_status, to_status, reason, "
                " source_observation_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    transition.state_id,
                    transition.subject_id,
                    transition.from_status,
                    transition.to_status,
                    transition.reason,
                    transition.source_observation_id,
                    _iso(transition.created_at),
                ),
            )
            self._commit_if_needed()

    def get_transitions(self, state_id: str) -> List[StateTransition]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM state_transitions WHERE state_id=? ORDER BY id",
                (state_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            out.append(
                StateTransition(
                    state_id=d["state_id"],
                    subject_id=d["subject_id"],
                    from_status=d["from_status"],
                    to_status=d["to_status"],
                    reason=d["reason"],
                    source_observation_id=d["source_observation_id"],
                    created_at=parse_dt(d["created_at"]),
                )
            )
        return out

    def get_state_by_id(self, state_id: str) -> Optional[State]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM states WHERE state_id=?", (state_id,)
            ).fetchone()
        return State.from_dict(dict(row)) if row else None

    # -- diagnostics ----------------------------------------------------------

    def counts(self) -> Dict[str, int]:
        with self._lock:
            out = {}
            for table in ("ingested_events", "observations", "states", "state_transitions"):
                out[table] = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out

    # -- sleep episodes ---------------------------------------------------------

    def insert_sleep_episode(self, ep: SleepEpisode) -> bool:
        """Insert once per (event, index); returns False when it already exists."""
        now = _iso(utc_now())
        opt = lambda dt: _iso(dt) if dt else None  # noqa: E731
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO sleep_episodes (episode_id, subject_id, event_id, idx, wake_date, "
                "start_at, end_at, duration_min, start_basis, end_basis, duration_basis, observed_at, "
                "source_text, source_message_id, status, superseded_by, created_at, updated_at, offset_min) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ep.episode_id, ep.subject_id, ep.event_id, ep.index, ep.wake_date, opt(ep.start_at),
                 opt(ep.end_at), ep.duration_min, ep.start_basis, ep.end_basis, ep.duration_basis,
                 _iso(ep.observed_at), ep.source_text, ep.source_message_id, ep.status, ep.superseded_by,
                 now, now, ep.offset_min))
            self._commit_if_needed()
            return cur.rowcount == 1

    def get_sleep_episodes(self, subject_id: str, since_date: str = "", status: str = "") -> List[SleepEpisode]:
        sql = "SELECT * FROM sleep_episodes WHERE subject_id=? AND wake_date >= ?"
        args: List[Any] = [subject_id, since_date or ""]
        if status:
            sql += " AND status=?"
            args.append(status)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY wake_date, COALESCE(end_at, ''), observed_at", args).fetchall()
        opt = lambda v: parse_dt(v) if v else None  # noqa: E731
        return [SleepEpisode(
            subject_id=r["subject_id"], event_id=r["event_id"], index=r["idx"], wake_date=r["wake_date"],
            start_at=opt(r["start_at"]), end_at=opt(r["end_at"]), duration_min=r["duration_min"],
            start_basis=r["start_basis"], end_basis=r["end_basis"], duration_basis=r["duration_basis"],
            observed_at=parse_dt(r["observed_at"]), source_text=r["source_text"],
            source_message_id=r["source_message_id"], status=r["status"], superseded_by=r["superseded_by"],
            offset_min=r["offset_min"],
        ) for r in rows]

    def supersede_sleep_episode(self, episode_id: str, by: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sleep_episodes SET status='superseded', superseded_by=?, updated_at=? WHERE episode_id=?",
                (by, _iso(utc_now()), episode_id))
            self._commit_if_needed()

    def queue_sleep_sync(self, subject_id: str, wake_date: str, payload: Dict[str, Any]) -> bool:
        """Queue the day's total; a payload identical to what is already queued/sent is a no-op."""
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._lock:
            row = self._conn.execute("SELECT payload_json FROM sleep_sync WHERE subject_id=? AND wake_date=?",
                                     (subject_id, wake_date)).fetchone()
            if row and row["payload_json"] == body:
                return False
            self._conn.execute(
                "INSERT INTO sleep_sync (subject_id, wake_date, payload_json, status, attempts, last_error, updated_at) "
                "VALUES (?,?,?,'pending',0,'',?) ON CONFLICT(subject_id, wake_date) DO UPDATE SET "
                "payload_json=excluded.payload_json, status='pending', attempts=0, last_error='', updated_at=excluded.updated_at",
                (subject_id, wake_date, body, _iso(utc_now())))
            self._commit_if_needed()
            return True

    def get_sleep_sync(self, subject_id: str = "", pending_only: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
        sql, args = "SELECT * FROM sleep_sync WHERE 1=1", []
        if subject_id:
            sql += " AND subject_id=?"
            args.append(subject_id)
        if pending_only:
            sql += " AND status!='sent'"
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY wake_date LIMIT ?", args + [limit]).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload_json"])} for r in rows]

    def mark_sleep_sync(self, subject_id: str, wake_date: str, payload_json: str, ok: bool, error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sleep_sync SET status=?, attempts=attempts+1, last_error=?, updated_at=? "
                "WHERE subject_id=? AND wake_date=? AND payload_json=?",
                ("sent" if ok else "failed", error[:300], _iso(utc_now()), subject_id, wake_date, payload_json))
            self._commit_if_needed()

    def dump_json(self) -> str:
        with self._lock:
            payload = {}
            for table in ("ingested_events", "observations", "states", "state_transitions"):
                rows = self._conn.execute(f"SELECT * FROM {table}").fetchall()
                payload[table] = [dict(r) for r in rows]
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
