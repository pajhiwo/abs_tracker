"""
Test multi-user session isolation.

Run with: python -m pytest tests/test_sessions.py -v
"""

import time

import pandas as pd
import pytest

from app.sessions import (
    InMemoryStore,
    SessionData,
    SessionStoreAtCapacity,
    new_session_id,
)


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


def test_live_session_is_never_evicted_to_admit_a_new_one():
    """At capacity, a NEW session is refused rather than evicting a live one (FR-021)."""
    store = InMemoryStore(ttl=60, max_sessions=3)

    sids = []
    for i in range(3):
        sid = new_session_id()
        s = SessionData()
        s.filename = f"file{i}.xlsx"
        store.set(sid, s)
        sids.append(sid)

    # A fourth new session must be refused, not swallow an existing one.
    with pytest.raises(SessionStoreAtCapacity):
        store.set(new_session_id(), SessionData())

    # Every pre-existing session is still intact — none was sacrificed.
    for i, sid in enumerate(sids):
        got = store.get(sid)
        assert got is not None
        assert got.filename == f"file{i}.xlsx"


def test_existing_session_updates_even_at_capacity():
    """Updating an already-present session never counts as a new admission."""
    store = InMemoryStore(ttl=60, max_sessions=2)
    a, b = new_session_id(), new_session_id()
    store.set(a, SessionData())
    store.set(b, SessionData())

    updated = store.get(a)
    updated.filename = "still-here.xlsx"
    store.set(a, updated)  # must not raise
    assert store.get(a).filename == "still-here.xlsx"


def test_capacity_frees_up_after_expiry():
    """Expiry — not eviction — is how capacity is reclaimed."""
    store = InMemoryStore(ttl=1, max_sessions=1)
    first = new_session_id()
    store.set(first, SessionData())
    with pytest.raises(SessionStoreAtCapacity):
        store.set(new_session_id(), SessionData())
    time.sleep(1.1)
    # Once the first has expired, the slot is available again.
    store.set(new_session_id(), SessionData())  # must not raise


def test_get_returns_a_copy_not_the_live_object():
    """Mutating a fetched session must not silently mutate stored state (R5 seam 1)."""
    store = InMemoryStore(ttl=60, max_sessions=10)
    sid = new_session_id()
    store.set(sid, SessionData(filename="orig.xlsx"))

    got = store.get(sid)
    got.filename = "mutated-without-set.xlsx"

    # Without an explicit set(), the store must be unchanged.
    assert store.get(sid).filename == "orig.xlsx"


def test_set_stores_a_copy_so_later_caller_mutation_does_not_leak():
    """The store must snapshot on set(), not alias the caller's object."""
    store = InMemoryStore(ttl=60, max_sessions=10)
    sid = new_session_id()
    data = SessionData(filename="orig.xlsx")
    store.set(sid, data)

    data.filename = "changed-after-set.xlsx"
    assert store.get(sid).filename == "orig.xlsx"


def test_copy_semantics_hold_for_dataframe_fields():
    """DataFrame fields are copied too, so a fetched frame is not the stored frame."""
    store = InMemoryStore(ttl=60, max_sessions=10)
    sid = new_session_id()
    df = pd.DataFrame({"promille": [1.0, 2.0]})
    store.set(sid, SessionData(bac_df=df))

    got = store.get(sid)
    got.bac_df.loc[0, "promille"] = 99.0

    assert store.get(sid).bac_df.loc[0, "promille"] == 1.0


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
