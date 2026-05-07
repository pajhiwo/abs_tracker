"""
Test multi-user session isolation.

Run with: python -m pytest tests/test_sessions.py -v
"""

import time
from app.sessions import InMemoryStore, SessionData, new_session_id


def test_separate_sessions_are_isolated():
    """Two users get independent session data."""
    store = InMemoryStore(ttl=60, max_sessions=10)

    sid1 = new_session_id()
    sid2 = new_session_id()

    s1 = SessionData()
    s1.filename = "user1.xlsx"
    s1.episode_threshold = 1.5

    s2 = SessionData()
    s2.filename = "user2.xlsx"
    s2.episode_threshold = 2.5

    store.set(sid1, s1)
    store.set(sid2, s2)

    # Retrieve and verify isolation
    got1 = store.get(sid1)
    got2 = store.get(sid2)

    assert got1.filename == "user1.xlsx"
    assert got1.episode_threshold == 1.5
    assert got2.filename == "user2.xlsx"
    assert got2.episode_threshold == 2.5

    # Mutating one doesn't affect the other
    got1.filename = "changed.xlsx"
    store.set(sid1, got1)

    got2_again = store.get(sid2)
    assert got2_again.filename == "user2.xlsx"


def test_session_ttl_expiry():
    """Expired sessions return None."""
    store = InMemoryStore(ttl=1, max_sessions=10)
    sid = new_session_id()
    store.set(sid, SessionData())

    assert store.get(sid) is not None
    time.sleep(1.1)
    assert store.get(sid) is None


def test_session_capacity_eviction():
    """Oldest session is evicted when at capacity."""
    store = InMemoryStore(ttl=60, max_sessions=3)

    sids = []
    for i in range(3):
        sid = new_session_id()
        s = SessionData()
        s.filename = f"file{i}.xlsx"
        store.set(sid, s)
        sids.append(sid)
        time.sleep(0.01)  # ensure distinct timestamps

    # All 3 should exist
    for sid in sids:
        assert store.get(sid) is not None

    # Adding a 4th should evict the oldest (sids[0] — but get() touched it above)
    # After the get() loop above, sids[0] was touched last in the loop first,
    # so let's touch sids[1] and sids[2] to make sids[0] oldest
    time.sleep(0.01)
    store.get(sids[1])
    time.sleep(0.01)
    store.get(sids[2])

    # Now sids[0] has the oldest timestamp
    new_sid = new_session_id()
    store.set(new_sid, SessionData())

    assert store.get(sids[0]) is None  # evicted
    assert store.get(sids[1]) is not None
    assert store.get(sids[2]) is not None
    assert store.get(new_sid) is not None


def test_nonexistent_session_returns_none():
    """Getting a session that doesn't exist returns None."""
    store = InMemoryStore()
    assert store.get("nonexistent-id") is None


def test_delete_session():
    """Deleted session is no longer retrievable."""
    store = InMemoryStore()
    sid = new_session_id()
    store.set(sid, SessionData())
    assert store.get(sid) is not None
    store.delete(sid)
    assert store.get(sid) is None
