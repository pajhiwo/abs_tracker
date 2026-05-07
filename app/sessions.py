"""
Pluggable session store for ABS Tracker.

Supports:
- InMemoryStore (default, single-instance deployments)
- RedisStore (multi-instance / production, requires REDIS_URL env var)

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
# Redis Store
# ---------------------------------------------------------------------------
class RedisStore:
    """Redis-backed store using pickle serialization + SETEX for TTL."""

    def __init__(self, redis_url: str, ttl: int = SESSION_TTL):
        import redis
        import pickle  # noqa: F401

        self._r = redis.from_url(redis_url)
        self._ttl = ttl
        self._prefix = "abs:session:"

    def get(self, session_id: str) -> SessionData | None:
        import pickle

        raw = self._r.get(self._prefix + session_id)
        if raw is None:
            return None
        # Touch TTL
        self._r.expire(self._prefix + session_id, self._ttl)
        return pickle.loads(raw)

    def set(self, session_id: str, data: SessionData) -> None:
        import pickle

        self._r.setex(self._prefix + session_id, self._ttl, pickle.dumps(data))

    def delete(self, session_id: str) -> None:
        self._r.delete(self._prefix + session_id)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def create_store() -> InMemoryStore | RedisStore:
    """Auto-detect store backend from environment."""
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        return RedisStore(redis_url)
    return InMemoryStore()


def new_session_id() -> str:
    return uuid.uuid4().hex
