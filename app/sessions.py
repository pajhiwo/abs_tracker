"""
Pluggable session store for ABS Tracker.

Currently one implementation: InMemoryStore. Access goes through the
SessionStore protocol so a session-scoped disk backend can be added for
multi-worker deployments without touching call sites.

Usage:
    store = create_store()
    session = store.get(session_id)
    store.set(session_id, session)
"""

import copy
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.results_cache import ResultCache

SESSION_TTL = int(os.getenv("SESSION_TTL", "1800"))  # seconds
# Capacity is a memory-derived backstop, NOT the normal reclamation path — that is
# TTL expiry (see InMemoryStore). T004 measured one analysis at ~29 MiB peak and
# <1 MB retained per session, so 500 sessions is comfortable headroom rather than a
# limit expected to bind in normal use. A live session is never evicted to admit a
# new one (FR-021); at capacity a new session is refused instead.
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "500"))


class SessionStoreAtCapacity(Exception):
    """Raised when a NEW session cannot be admitted because the store is full.

    The handler surfaces this as the R10 `503` "session saturation" response. It is
    never raised for updates to an already-present session.
    """


@dataclass
class SessionData:
    """Per-user session holding parsed data and analysis results."""

    meals_df: Any = None
    bac_df: Any = None
    med_periods: Any = None
    lookback_df: Any = None
    scores_all: Any = None
    scores_by_period: Any = None
    hours: float = 3.0
    min_obs: int = 3
    split_compounds: bool = True
    exclude_proteins: bool = True  # reconciled default (see app.compute)
    episode_threshold: float = 2.0
    filename: str | None = None
    raw_bytes: bytes | None = None
    # Content hash of the uploaded bytes — used to key/invalidate cached results
    # (FR-019). Set at upload time.
    content_hash: str | None = None
    # The job this session is currently waiting on, if any (data-model Session).
    active_job_id: str | None = None
    # Per-session LRU cache keyed by params_signature, holding the results payload,
    # the report document and the rendered PDF (data-model ResultCache; FR-016–FR-020).
    # The executor writes the stage-one payload + report, then updates the ML block in
    # stage two. Cleared on a new upload via `note_content` (FR-019).
    results: ResultCache = field(default_factory=ResultCache)


class SessionStore(Protocol):
    def get(self, session_id: str) -> SessionData | None: ...
    def set(self, session_id: str, data: SessionData) -> None: ...
    def delete(self, session_id: str) -> None: ...


# ---------------------------------------------------------------------------
# In-Memory Store
# ---------------------------------------------------------------------------
class InMemoryStore:
    """Dict-based store with TTL expiry and a capacity backstop.

    Copy semantics: ``get()`` returns a deep copy and ``set()`` stores a deep copy,
    so a caller mutating a fetched session never changes stored state without an
    explicit ``set()``. This matches how a future disk/multi-worker backing behaves
    (each ``get()`` deserialises a fresh object), so call sites do not have to change
    when the store does (research R5 seam 1; data-model "Store semantics"). Retained
    state is <1 MB per session (T004), so per-call copying is cheap at rung 1.
    """

    def __init__(self, ttl: int = SESSION_TTL, max_sessions: int = MAX_SESSIONS):
        self._store: dict[str, tuple[float, SessionData]] = {}
        self._ttl = ttl
        self._max = max_sessions

    def get(self, session_id: str) -> SessionData | None:
        self._cleanup()
        entry = self._store.get(session_id)
        if entry is None:
            return None
        ts, data = entry
        if time.time() - ts > self._ttl:
            del self._store[session_id]
            return None
        # Touch timestamp; hand back a copy so the caller cannot mutate stored state.
        self._store[session_id] = (time.time(), data)
        return copy.deepcopy(data)

    def set(self, session_id: str, data: SessionData) -> None:
        self._cleanup()
        # Refuse a NEW session at capacity rather than evicting a live one (FR-021).
        # Updating an existing session is always allowed. Expiry — not eviction — is
        # how capacity is reclaimed.
        if session_id not in self._store and len(self._store) >= self._max:
            raise SessionStoreAtCapacity(
                f"session store at capacity ({self._max} sessions)"
            )
        self._store[session_id] = (time.time(), copy.deepcopy(data))

    def delete(self, session_id: str) -> None:
        self._store.pop(session_id, None)

    def _cleanup(self) -> None:
        now = time.time()
        expired = [k for k, (ts, _) in self._store.items() if now - ts > self._ttl]
        for k in expired:
            del self._store[k]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def create_store() -> SessionStore:
    """Build the session store."""
    return InMemoryStore()


def new_session_id() -> str:
    return uuid.uuid4().hex
