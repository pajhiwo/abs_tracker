"""Analysis job queue (research R5, R5a; data-model AnalysisJob/AnalysisQueue).

The queue is what turns "one analysis freezes the site" into "analyses run a few at
a time and everyone else sees a position." Two backings implement one protocol:

- `InMemoryJobQueue` — for tests and single-process rung 1.
- `SqliteJobQueue` — WAL-mode SQLite with the R5a correctness conditions, so the same
  ordering / atomic-leased-claim / cancellation semantics hold *across processes*.
  This is what lets rung 2 add Uvicorn workers without changing the queue.

Jobs reference a session by id and carry **no uploaded bytes** — parsing happens before
enqueue (R11), so the queue can span sessions while holding no health data.

Thread-safety: the executor is a thread pool (T004 decision), and every queue call from
an `async` handler is offloaded to a worker thread (`sqlite3` blocks the event loop —
the very defect this feature removes). Both backings guard their state with a lock so
concurrent worker threads cannot corrupt it; SQLite additionally relies on its own file
locking plus `busy_timeout` for the cross-process case.
"""

from __future__ import annotations

import enum
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol


class JobState(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PARTIAL = "partial"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"
    EXPIRED = "expired"


#: States from which no further transition happens.
TERMINAL_STATES = frozenset(
    {
        JobState.COMPLETE,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.ABANDONED,
        JobState.EXPIRED,
    }
)
#: States that occupy a worker (not waiting, not finished).
ACTIVE_STATES = frozenset({JobState.RUNNING, JobState.PARTIAL})


class QueueBusy(Exception):
    """Raised when the SQLite backing stays locked past its retry budget.

    The handler surfaces this as a `503` with `Retry-After` rather than hanging the
    request (research R5a "bounded retry surfaced as 503").
    """


@dataclass
class AnalysisJob:
    """One queued or running unit of work (data-model AnalysisJob)."""

    job_id: str
    session_id: str
    params_signature: str
    state: JobState
    queued_at: float
    started_at: float | None = None
    last_seen_at: float = 0.0
    lease_expires_at: float | None = None
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


def new_job_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
class JobQueue(Protocol):
    def enqueue(self, session_id: str, params_signature: str) -> AnalysisJob: ...
    def get(self, job_id: str, session_id: str | None = None) -> AnalysisJob | None: ...
    def position(self, job_id: str) -> int | None: ...
    def claim(self, lease_seconds: float) -> AnalysisJob | None: ...
    def renew_lease(self, job_id: str, lease_seconds: float) -> None: ...
    def set_state(
        self, job_id: str, state: JobState, error: str | None = None
    ) -> AnalysisJob | None: ...
    def cancel(self, job_id: str, session_id: str) -> bool: ...
    def touch(self, job_id: str, session_id: str) -> AnalysisJob | None: ...
    def reclaim_expired_leases(self, now: float | None = None) -> list[str]: ...
    def count_waiting(self) -> int: ...
    def sweep(self, session_exists: Callable[[str], bool]) -> dict: ...


# ---------------------------------------------------------------------------
# In-memory backing
# ---------------------------------------------------------------------------
class InMemoryJobQueue:
    """Dict-backed queue for tests and single-process use."""

    def __init__(self) -> None:
        self._jobs: dict[str, AnalysisJob] = {}
        self._lock = threading.RLock()

    def enqueue(self, session_id: str, params_signature: str) -> AnalysisJob:
        with self._lock:
            # Duplicate suppression: a live job with the same (session, signature)
            # is returned rather than enqueuing a second (data-model; double-click).
            for job in self._jobs.values():
                if (
                    job.session_id == session_id
                    and job.params_signature == params_signature
                    and not job.is_terminal
                ):
                    return job
            now = time.time()
            job = AnalysisJob(
                job_id=new_job_id(),
                session_id=session_id,
                params_signature=params_signature,
                state=JobState.QUEUED,
                queued_at=now,
                last_seen_at=now,
            )
            self._jobs[job.job_id] = job
            return job

    def get(self, job_id: str, session_id: str | None = None) -> AnalysisJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if session_id is not None and job.session_id != session_id:
                return None  # FR-020 isolation — never leak across sessions
            return _copy_job(job)

    def _waiting_ordered(self) -> list[AnalysisJob]:
        waiting = [j for j in self._jobs.values() if j.state == JobState.QUEUED]
        return sorted(waiting, key=lambda j: j.queued_at)

    def position(self, job_id: str) -> int | None:
        with self._lock:
            for idx, job in enumerate(self._waiting_ordered(), start=1):
                if job.job_id == job_id:
                    return idx
            return None

    def claim(self, lease_seconds: float) -> AnalysisJob | None:
        with self._lock:
            waiting = self._waiting_ordered()
            if not waiting:
                return None
            job = waiting[0]
            now = time.time()
            job.state = JobState.RUNNING
            job.started_at = now
            job.lease_expires_at = now + lease_seconds
            return _copy_job(job)

    def renew_lease(self, job_id: str, lease_seconds: float) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and job.state in ACTIVE_STATES:
                job.lease_expires_at = time.time() + lease_seconds

    def set_state(
        self, job_id: str, state: JobState, error: str | None = None
    ) -> AnalysisJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job.state = state
            if error is not None:
                job.error = error
            if state in TERMINAL_STATES or state == JobState.QUEUED:
                job.lease_expires_at = None
            return _copy_job(job)

    def cancel(self, job_id: str, session_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.session_id != session_id:
                return False
            if job.is_terminal:
                return False
            job.state = JobState.CANCELLED
            job.lease_expires_at = None
            return True

    def touch(self, job_id: str, session_id: str) -> AnalysisJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.session_id != session_id:
                return None
            job.last_seen_at = time.time()
            return _copy_job(job)

    def reclaim_expired_leases(self, now: float | None = None) -> list[str]:
        now = now if now is not None else time.time()
        reclaimed = []
        with self._lock:
            for job in self._jobs.values():
                if (
                    job.state in ACTIVE_STATES
                    and job.lease_expires_at is not None
                    and job.lease_expires_at < now
                ):
                    job.state = JobState.QUEUED
                    job.started_at = None
                    job.lease_expires_at = None
                    reclaimed.append(job.job_id)
        return reclaimed

    def count_waiting(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.state == JobState.QUEUED)

    def sweep(self, session_exists: Callable[[str], bool]) -> dict:
        with self._lock:
            expired, reclaimed = 0, 0
            for job in self._jobs.values():
                if job.is_terminal:
                    continue
                if not session_exists(job.session_id):
                    job.state = JobState.EXPIRED
                    job.lease_expires_at = None
                    expired += 1
                elif job.state in ACTIVE_STATES:
                    # A job left running by a dead worker: requeue it.
                    job.state = JobState.QUEUED
                    job.started_at = None
                    job.lease_expires_at = None
                    reclaimed += 1
            return {"expired": expired, "reclaimed": reclaimed}


def _copy_job(job: AnalysisJob) -> AnalysisJob:
    return AnalysisJob(
        job_id=job.job_id,
        session_id=job.session_id,
        params_signature=job.params_signature,
        state=job.state,
        queued_at=job.queued_at,
        started_at=job.started_at,
        last_seen_at=job.last_seen_at,
        lease_expires_at=job.lease_expires_at,
        error=job.error,
    )


# ---------------------------------------------------------------------------
# SQLite backing
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id           TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    params_signature TEXT NOT NULL,
    state            TEXT NOT NULL,
    queued_at        REAL NOT NULL,
    started_at       REAL,
    last_seen_at     REAL NOT NULL,
    lease_expires_at REAL,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state_queued ON jobs (state, queued_at);
CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs (session_id);
"""

_COLS = (
    "job_id, session_id, params_signature, state, queued_at, "
    "started_at, last_seen_at, lease_expires_at, error"
)


class SqliteJobQueue:
    """SQLite-backed queue with the R5a correctness conditions.

    WAL + `synchronous=NORMAL` + non-zero `busy_timeout`; the atomic leased claim is a
    single `UPDATE ... RETURNING`; writes use `BEGIN IMMEDIATE`; `SQLITE_BUSY` past the
    timeout is surfaced as `QueueBusy` (→ `503`). Ordering, position, claim and cancel
    are all transactions, so they are correct across processes as well as threads.
    """

    def __init__(
        self,
        path: str = ":memory:",
        *,
        busy_timeout_ms: int = 5000,
        max_retries: int = 3,
    ) -> None:
        self._path = path
        self._max_retries = max_retries
        self._lock = threading.RLock()
        # One connection shared across worker threads; a Python-side lock serialises
        # access because a single sqlite3 connection is not safe for concurrent use.
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self._conn.executescript(_SCHEMA)

    # -- helpers ----------------------------------------------------------
    def _row_to_job(self, row: sqlite3.Row) -> AnalysisJob:
        return AnalysisJob(
            job_id=row["job_id"],
            session_id=row["session_id"],
            params_signature=row["params_signature"],
            state=JobState(row["state"]),
            queued_at=row["queued_at"],
            started_at=row["started_at"],
            last_seen_at=row["last_seen_at"],
            lease_expires_at=row["lease_expires_at"],
            error=row["error"],
        )

    def _write(self, fn):
        """Run a write transaction with BEGIN IMMEDIATE and a bounded busy retry."""
        last_exc: Exception | None = None
        for _ in range(self._max_retries):
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    result = fn()
                    self._conn.execute("COMMIT")
                    return result
                except sqlite3.OperationalError as exc:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass
                    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                        last_exc = exc
                        continue
                    raise
        raise QueueBusy(str(last_exc) if last_exc else "queue is busy")

    # -- protocol ---------------------------------------------------------
    def enqueue(self, session_id: str, params_signature: str) -> AnalysisJob:
        def _do() -> AnalysisJob:
            cur = self._conn.execute(
                f"SELECT {_COLS} FROM jobs WHERE session_id=? AND params_signature=? "
                "AND state NOT IN ('complete','failed','cancelled','abandoned','expired') "
                "ORDER BY queued_at ASC LIMIT 1",
                (session_id, params_signature),
            )
            row = cur.fetchone()
            if row is not None:
                return self._row_to_job(row)
            now = time.time()
            job = AnalysisJob(
                job_id=new_job_id(),
                session_id=session_id,
                params_signature=params_signature,
                state=JobState.QUEUED,
                queued_at=now,
                last_seen_at=now,
            )
            self._conn.execute(
                f"INSERT INTO jobs ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    job.job_id, job.session_id, job.params_signature, job.state.value,
                    job.queued_at, job.started_at, job.last_seen_at,
                    job.lease_expires_at, job.error,
                ),
            )
            return job

        return self._write(_do)

    def get(self, job_id: str, session_id: str | None = None) -> AnalysisJob | None:
        with self._lock:
            cur = self._conn.execute(
                f"SELECT {_COLS} FROM jobs WHERE job_id=?", (job_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        job = self._row_to_job(row)
        if session_id is not None and job.session_id != session_id:
            return None
        return job

    def position(self, job_id: str) -> int | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT job_id FROM jobs WHERE state='queued' ORDER BY queued_at ASC"
            )
            for idx, row in enumerate(cur.fetchall(), start=1):
                if row["job_id"] == job_id:
                    return idx
        return None

    def claim(self, lease_seconds: float) -> AnalysisJob | None:
        now = time.time()

        def _do() -> AnalysisJob | None:
            cur = self._conn.execute(
                f"UPDATE jobs SET state='running', started_at=?, lease_expires_at=? "
                "WHERE job_id = ("
                "  SELECT job_id FROM jobs WHERE state='queued' "
                "  ORDER BY queued_at ASC, rowid ASC LIMIT 1"
                f") RETURNING {_COLS}",
                (now, now + lease_seconds),
            )
            row = cur.fetchone()
            return self._row_to_job(row) if row is not None else None

        return self._write(_do)

    def renew_lease(self, job_id: str, lease_seconds: float) -> None:
        def _do() -> None:
            self._conn.execute(
                "UPDATE jobs SET lease_expires_at=? "
                "WHERE job_id=? AND state IN ('running','partial')",
                (time.time() + lease_seconds, job_id),
            )

        self._write(_do)

    def set_state(
        self, job_id: str, state: JobState, error: str | None = None
    ) -> AnalysisJob | None:
        clears_lease = state in TERMINAL_STATES or state == JobState.QUEUED

        def _do() -> AnalysisJob | None:
            if error is not None:
                self._conn.execute(
                    "UPDATE jobs SET state=?, error=?, "
                    "lease_expires_at=CASE WHEN ? THEN NULL ELSE lease_expires_at END "
                    "WHERE job_id=?",
                    (state.value, error, clears_lease, job_id),
                )
            else:
                self._conn.execute(
                    "UPDATE jobs SET state=?, "
                    "lease_expires_at=CASE WHEN ? THEN NULL ELSE lease_expires_at END "
                    "WHERE job_id=?",
                    (state.value, clears_lease, job_id),
                )
            cur = self._conn.execute(
                f"SELECT {_COLS} FROM jobs WHERE job_id=?", (job_id,)
            )
            row = cur.fetchone()
            return self._row_to_job(row) if row is not None else None

        return self._write(_do)

    def cancel(self, job_id: str, session_id: str) -> bool:
        def _do() -> bool:
            cur = self._conn.execute(
                "UPDATE jobs SET state='cancelled', lease_expires_at=NULL "
                "WHERE job_id=? AND session_id=? "
                "AND state NOT IN ('complete','failed','cancelled','abandoned','expired') "
                "RETURNING job_id",
                (job_id, session_id),
            )
            return cur.fetchone() is not None

        return self._write(_do)

    def touch(self, job_id: str, session_id: str) -> AnalysisJob | None:
        def _do() -> AnalysisJob | None:
            cur = self._conn.execute(
                f"UPDATE jobs SET last_seen_at=? WHERE job_id=? AND session_id=? "
                f"RETURNING {_COLS}",
                (time.time(), job_id, session_id),
            )
            row = cur.fetchone()
            return self._row_to_job(row) if row is not None else None

        return self._write(_do)

    def reclaim_expired_leases(self, now: float | None = None) -> list[str]:
        now = now if now is not None else time.time()

        def _do() -> list[str]:
            cur = self._conn.execute(
                "UPDATE jobs SET state='queued', started_at=NULL, lease_expires_at=NULL "
                "WHERE state IN ('running','partial') AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at < ? RETURNING job_id",
                (now,),
            )
            return [r["job_id"] for r in cur.fetchall()]

        return self._write(_do)

    def count_waiting(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE state='queued'")
            return int(cur.fetchone()["n"])

    def sweep(self, session_exists: Callable[[str], bool]) -> dict:
        def _do() -> dict:
            cur = self._conn.execute(
                "SELECT job_id, session_id, state FROM jobs "
                "WHERE state NOT IN ('complete','failed','cancelled','abandoned','expired')"
            )
            rows = cur.fetchall()
            expired, reclaimed = 0, 0
            for row in rows:
                if not session_exists(row["session_id"]):
                    self._conn.execute(
                        "UPDATE jobs SET state='expired', lease_expires_at=NULL "
                        "WHERE job_id=?",
                        (row["job_id"],),
                    )
                    expired += 1
                elif row["state"] in ("running", "partial"):
                    self._conn.execute(
                        "UPDATE jobs SET state='queued', started_at=NULL, "
                        "lease_expires_at=NULL WHERE job_id=?",
                        (row["job_id"],),
                    )
                    reclaimed += 1
            return {"expired": expired, "reclaimed": reclaimed}

        return self._write(_do)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def create_job_queue() -> JobQueue:
    """Build the job queue backing.

    Rung 1 uses the in-memory queue in a single process. The SQLite backing exists so
    rung 2 (multiple workers) is a configuration change, not a rewrite (research R5).
    """
    import os

    db = os.getenv("ABS_QUEUE_DB")
    if db:
        return SqliteJobQueue(db)
    return InMemoryJobQueue()
