"""Authoritative local task state for the single-worker pilot.

Leases use persistent UTC Unix seconds, never a process-local monotonic clock.
The injected clock and a persisted, nondecreasing time floor make expiry
deterministic in tests and prevent a backwards wall-clock adjustment from
reviving an expired lease. A substantial clock rollback can delay recovery
until UTC catches up; a state transition does not guess that time has elapsed.

An owner is a fresh UUID for each claim attempt, not a reusable worker name.
The store rejects reuse for the same task so an old process cannot regain
write access when a later attempt starts. Lease reclamation records evidence;
the runner must reconcile any recorded child process before reusing its files.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Callable, Iterator


class StateError(RuntimeError):
    """Base error for the task store."""


class ConflictError(StateError):
    """An identity or idempotency key was reused inconsistently."""


class LeaseLostError(StateError):
    """This owner no longer has a live lease for the task."""


class InvalidTransitionError(StateError):
    """The requested state transition is not allowed."""


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _json_object(value: dict, name: str) -> str:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")

    def check(item: object) -> None:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError(f"{name} must have string object keys")
            for child in item.values():
                check(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check(child)

    check(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain only finite JSON values") from exc


class StateStore:
    """SQLite task store; open a fresh connection per operation.

    Mutations run under BEGIN IMMEDIATE. A partial unique index additionally
    enforces at most one running task, including a task awaiting pause.
    A running pause is cooperative: it keeps the lease and global slot until
    the runner has stopped its child process and calls finish(..., 'paused').

    Returned task fields are id, dedupe_key, payload, status, phase, context,
    attempt, owner, lease_expires_at, pause_requested, reason, created_at,
    updated_at. All timestamps are UTC Unix seconds; null remains null.
    """

    FINISH_STATUSES = frozenset({"paused", "blocked", "completed", "failed"})
    RESUMABLE_STATUSES = frozenset({"paused", "blocked", "failed"})

    def __init__(self, db_path: str | Path,
                 clock: Callable[[], float] = time.time) -> None:
        if str(db_path) == ":memory:":
            raise ValueError("use a file-backed SQLite database for durable state")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1');
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('queued','running','paused','blocked','completed','failed')),
                    phase TEXT NOT NULL DEFAULT 'prepare',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
                    owner TEXT,
                    lease_expires_at REAL,
                    pause_requested INTEGER NOT NULL DEFAULT 0
                        CHECK(pause_requested IN (0,1)),
                    reason TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    CHECK((status = 'running' AND owner IS NOT NULL
                            AND lease_expires_at IS NOT NULL)
                        OR (status != 'running' AND owner IS NULL
                            AND lease_expires_at IS NULL))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_task
                    ON tasks ((1)) WHERE status = 'running';
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    kind TEXT NOT NULL,
                    owner TEXT,
                    at REAL NOT NULL,
                    data_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_events ON events(task_id, sequence);
            """)
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            if version != "1":
                raise StateError(f"unsupported state schema version: {version}")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                # Observing expiry must survive a rejected stale-owner write.
                # Otherwise rolling the UTC floor back with the mutation could
                # revive that lease after a wall-clock adjustment or restart.
                observed = connection.execute(
                    "SELECT value FROM metadata WHERE key='last_clock'"
                ).fetchone()
                connection.rollback()
                if observed is not None:
                    connection.execute(
                        "INSERT INTO metadata(key,value) VALUES ('last_clock',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=CASE "
                        "WHEN CAST(metadata.value AS REAL)<CAST(excluded.value AS REAL) "
                        "THEN excluded.value ELSE metadata.value END", (observed[0],)
                    )
                raise

    def _now(self, connection: sqlite3.Connection) -> float:
        observed = float(self.clock())
        if not math.isfinite(observed) or observed < 0:
            raise ValueError("clock must return finite nonnegative UTC Unix seconds")
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='last_clock'"
        ).fetchone()
        now = max(observed, float(row[0])) if row else observed
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES ('last_clock',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (repr(now),)
        )
        return now

    @staticmethod
    def _duration(lease_seconds: float) -> float:
        duration = float(lease_seconds)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        return duration

    @staticmethod
    def _task(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result["context"] = json.loads(result.pop("context_json"))
        result["pause_requested"] = bool(result["pause_requested"])
        return result

    @staticmethod
    def _row(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return row

    @staticmethod
    def _event(connection: sqlite3.Connection, task_id: str, kind: str,
               owner: str | None, now: float, data: dict | None = None) -> None:
        connection.execute(
            "INSERT INTO events(task_id,kind,owner,at,data_json) VALUES (?,?,?,?,?)",
            (task_id, kind, owner, now, _json_object(data or {}, "event data")),
        )

    def _owned(self, connection: sqlite3.Connection, task_id: str,
               owner: str, now: float) -> sqlite3.Row:
        _identifier(owner, "owner")
        row = self._row(connection, task_id)
        if (row["status"] != "running" or row["owner"] != owner
                or row["lease_expires_at"] <= now):
            raise LeaseLostError(f"owner {owner!r} has no live lease for {task_id!r}")
        return row

    def enqueue(self, task_id: str, payload: dict, dedupe_key: str) -> dict:
        """Create once, or return the original task for an identical trigger.

        A repeated dedupe key with the same payload returns the original id,
        even if the caller proposes another task id. The return adds created.
        Reusing either identity inconsistently raises ConflictError.
        """
        _identifier(task_id, "task_id")
        _identifier(dedupe_key, "dedupe_key")
        encoded = _json_object(payload, "payload")
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM tasks WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
            identity = connection.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if identity is not None and identity["dedupe_key"] != dedupe_key:
                raise ConflictError(f"task id {task_id!r} already has another dedupe key")
            if existing is not None:
                if existing["payload_json"] != encoded:
                    raise ConflictError(f"dedupe key {dedupe_key!r} has different payload")
                return {**self._task(existing), "created": False}
            now = self._now(connection)
            connection.execute(
                "INSERT INTO tasks(id,dedupe_key,payload_json,status,created_at,updated_at) "
                "VALUES (?,?,?,'queued',?,?)", (task_id, dedupe_key, encoded, now, now),
            )
            self._event(connection, task_id, "enqueued", None, now)
            return {**self._task(self._row(connection, task_id)), "created": True}

    def claim(self, owner: str, lease_seconds: float = 60) -> dict | None:
        """Claim queued work or recover the one expired running task.

        Recovery takes precedence over queued work, so an interrupted process
        is reconciled before another task runs. Returns recovered and
        previous_owner in addition to the persistent task fields.
        """
        _identifier(owner, "owner")
        duration = self._duration(lease_seconds)
        with self._transaction() as connection:
            now = self._now(connection)
            row = connection.execute(
                "SELECT * FROM tasks WHERE status='running'"
            ).fetchone()
            recovered = row is not None
            previous_owner = row["owner"] if row else None
            if row is not None and row["lease_expires_at"] > now:
                return None
            if row is None:
                # A lost controller can leave a live child after the task is
                # marked blocked. That uncertainty still occupies the global
                # execution slot. Only resuming that exact task may reconcile
                # it; unrelated queued work must not start beside the child.
                uncertain = next((item for item in connection.execute(
                    "SELECT * FROM tasks ORDER BY created_at,id"
                ) if json.loads(item["context_json"]).get("active_process")), None)
                if uncertain is not None:
                    if uncertain["status"] != "queued":
                        return None
                    row = uncertain
                else:
                    row = connection.execute(
                        "SELECT * FROM tasks WHERE status='queued' ORDER BY created_at,id LIMIT 1"
                    ).fetchone()
            if row is None:
                return None
            used = connection.execute(
                "SELECT 1 FROM events WHERE task_id=? AND owner=? "
                "AND kind IN ('claimed','lease_reclaimed') LIMIT 1", (row["id"], owner),
            ).fetchone()
            if used:
                raise ConflictError("each claim attempt for a task needs a fresh owner UUID")
            connection.execute(
                "UPDATE tasks SET status='running',owner=?,lease_expires_at=?,"
                "attempt=attempt+1,updated_at=? WHERE id=?",
                (owner, now + duration, now, row["id"]),
            )
            self._event(
                connection, row["id"], "lease_reclaimed" if recovered else "claimed",
                owner, now, {"previous_owner": previous_owner,
                             "previous_lease_expires_at": row["lease_expires_at"],
                             "attempt": row["attempt"] + 1},
            )
            return {**self._task(self._row(connection, row["id"])),
                    "recovered": recovered, "previous_owner": previous_owner}

    def heartbeat(self, task_id: str, owner: str,
                  lease_seconds: float = 60) -> dict:
        duration = self._duration(lease_seconds)
        with self._transaction() as connection:
            now = self._now(connection)
            row = self._owned(connection, task_id, owner, now)
            expiry = max(row["lease_expires_at"], now + duration)
            connection.execute(
                "UPDATE tasks SET lease_expires_at=?,updated_at=? WHERE id=?",
                (expiry, now, task_id),
            )
            self._event(connection, task_id, "heartbeat", owner, now,
                        {"lease_expires_at": expiry})
            return self._task(self._row(connection, task_id))

    def checkpoint(self, task_id: str, owner: str, phase: str, context: dict) -> dict:
        _identifier(phase, "phase")
        encoded = _json_object(context, "context")
        with self._transaction() as connection:
            now = self._now(connection)
            self._owned(connection, task_id, owner, now)
            connection.execute(
                "UPDATE tasks SET phase=?,context_json=?,updated_at=? WHERE id=?",
                (phase, encoded, now, task_id),
            )
            self._event(connection, task_id, "checkpoint", owner, now,
                        {"phase": phase, "context": json.loads(encoded)})
            return self._task(self._row(connection, task_id))

    def finish(self, task_id: str, owner: str, status: str,
               reason: str | None = None) -> dict:
        if status not in self.FINISH_STATUSES:
            raise InvalidTransitionError(f"finish cannot set status {status!r}")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("reason must be a string or null")
        with self._transaction() as connection:
            now = self._now(connection)
            self._owned(connection, task_id, owner, now)
            connection.execute(
                "UPDATE tasks SET status=?,reason=?,owner=NULL,lease_expires_at=NULL,"
                "pause_requested=0,updated_at=? WHERE id=?", (status, reason, now, task_id),
            )
            self._event(connection, task_id, "finished", owner, now,
                        {"status": status, "reason": reason})
            return self._task(self._row(connection, task_id))

    def pause(self, task_id: str) -> dict:
        """Request a running pause without releasing its global worker slot."""
        with self._transaction() as connection:
            now = self._now(connection)
            row = self._row(connection, task_id)
            if row["status"] == "completed":
                raise InvalidTransitionError("a completed task cannot be paused")
            if row["status"] == "paused" or row["pause_requested"]:
                return self._task(row)
            running = row["status"] == "running"
            connection.execute(
                "UPDATE tasks SET status=?,pause_requested=?,updated_at=? WHERE id=?",
                ("running" if running else "paused", int(running), now, task_id),
            )
            self._event(connection, task_id,
                        "pause_requested" if running else "paused", row["owner"], now,
                        {"previous_status": row["status"]})
            return self._task(self._row(connection, task_id))

    def resume(self, task_id: str) -> dict:
        """Explicitly retry paused, blocked, or failed work at its checkpoint."""
        with self._transaction() as connection:
            now = self._now(connection)
            row = self._row(connection, task_id)
            if row["status"] == "queued":
                return self._task(row)
            if row["status"] not in self.RESUMABLE_STATUSES:
                raise InvalidTransitionError(f"cannot resume a {row['status']} task")
            connection.execute(
                "UPDATE tasks SET status='queued',pause_requested=0,reason=NULL,"
                "updated_at=? WHERE id=?", (now, task_id),
            )
            self._event(connection, task_id, "resumed", None, now,
                        {"previous_status": row["status"], "previous_reason": row["reason"]})
            return self._task(self._row(connection, task_id))

    def renew_budget(self, task_id: str, confirmed_by: str, reason: str) -> dict:
        """Explicit human renewal; ordinary resume never resets a budget.

        This preserves phase, implementation, evidence and remote-operation
        intents. It does not approve a merge or make old evidence fresh. The
        next runner attempt must still apply its normal SHA/freshness gates.
        A running or unreconciled child cannot receive a new budget.
        """
        _identifier(confirmed_by, "confirmed_by")
        _identifier(reason, "reason")
        with self._transaction() as connection:
            now = self._now(connection)
            row = self._row(connection, task_id)
            if row["status"] not in {"queued", "paused", "blocked", "failed"}:
                raise InvalidTransitionError(f"cannot renew budget for a {row['status']} task")
            context = json.loads(row["context_json"])
            if context.get("active_process"):
                raise InvalidTransitionError("cannot renew budget before process reconciliation")
            keys = ("started_at", "deadline", "model_calls", "iterations",
                    "no_progress_count", "no_progress", "ci_poll_count")
            previous = {key: context.get(key) for key in keys}
            history = context.setdefault("budget_history", [])
            if not isinstance(history, list):
                raise InvalidTransitionError("budget_history must be a list")
            history.append({**previous, "confirmed_by": confirmed_by,
                            "reason": reason, "confirmed_at": now})
            for key in keys:
                context.pop(key, None)
            connection.execute(
                "UPDATE tasks SET context_json=?,updated_at=? WHERE id=?",
                (_json_object(context, "context"), now, task_id),
            )
            self._event(connection, task_id, "budget_renewed", None, now,
                        {"confirmed_by": confirmed_by, "reason": reason,
                         "previous_budget": previous})
            return self._task(self._row(connection, task_id))

    def get(self, task_id: str) -> dict:
        with self._connection() as connection:
            return self._task(self._row(connection, task_id))

    def list(self, status: str | None = None) -> list[dict]:
        with self._connection() as connection:
            if status is None:
                rows = connection.execute("SELECT * FROM tasks ORDER BY created_at,id")
            else:
                rows = connection.execute(
                    "SELECT * FROM tasks WHERE status=? ORDER BY created_at,id", (status,)
                )
            return [self._task(row) for row in rows]

    def events(self, task_id: str) -> list[dict]:
        with self._connection() as connection:
            self._row(connection, task_id)
            rows = connection.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY sequence", (task_id,)
            )
            result = []
            for row in rows:
                event = dict(row)
                event["data"] = json.loads(event.pop("data_json"))
                result.append(event)
            return result
