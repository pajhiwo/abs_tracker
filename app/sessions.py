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

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

SESSION_TTL = int(os.getenv("SESSION_TTL", "1800"))  # seconds
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "100"))


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
    exclude_proteins: bool = False
    episode_threshold: float = 2.0
    filename: str | None = None
    raw_bytes: bytes | None = None


class SessionStore(Protocol):
    def get(self, session_id: str) -> SessionData | None: ...
    def set(self, session_id: str, data: SessionData) -> None: ...
    def delete(self, session_id: str) -> None: ...


# ---------------------------------------------------------------------------
# In-Memory Store
# ---------------------------------------------------------------------------
class InMemoryStore:
    """Dict-based store with TTL expiry and capacity cap."""

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
        # Touch timestamp
        self._store[session_id] = (time.time(), data)
        return data

    def set(self, session_id: str, data: SessionData) -> None:
        self._cleanup()
        # Evict oldest if at capacity
        if session_id not in self._store and len(self._store) >= self._max:
            oldest_key = min(self._store, key=lambda k: self._store[k][0])
            del self._store[oldest_key]
        self._store[session_id] = (time.time(), data)

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
