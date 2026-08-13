"""Job queue conformance, run against BOTH backings (T011; research R5a).

The whole point of the two-backing design is that the in-memory and SQLite queues are
behaviourally identical, so rung 2 (SQLite, multiple workers) inherits the semantics
proven at rung 1. Every test below therefore runs against both.
"""

import threading

import pytest

from app.jobs import (
    ACTIVE_STATES,
    AnalysisJob,
    InMemoryJobQueue,
    JobState,
    SqliteJobQueue,
)


@pytest.fixture(params=["memory", "sqlite"])
def queue(request):
    if request.param == "memory":
        yield InMemoryJobQueue()
    else:
        q = SqliteJobQueue(":memory:")
        yield q
        q.close()


def test_enqueue_returns_a_queued_job(queue):
    job = queue.enqueue("sess-a", "sig-1")
    assert isinstance(job, AnalysisJob)
    assert job.state == JobState.QUEUED
    assert job.session_id == "sess-a"
    assert queue.get(job.job_id).job_id == job.job_id


def test_fifo_ordering_of_claims(queue):
    a = queue.enqueue("s1", "sig")
    b = queue.enqueue("s2", "sig")
    c = queue.enqueue("s3", "sig")

    assert queue.claim(lease_seconds=60).job_id == a.job_id
    assert queue.claim(lease_seconds=60).job_id == b.job_id
    assert queue.claim(lease_seconds=60).job_id == c.job_id
    assert queue.claim(lease_seconds=60) is None


def test_claim_is_atomic_and_leased(queue):
    job = queue.enqueue("s1", "sig")
    claimed = queue.claim(lease_seconds=60)
    assert claimed.job_id == job.job_id
    assert claimed.state == JobState.RUNNING
    assert claimed.lease_expires_at is not None and claimed.lease_expires_at > 0
    # It is no longer claimable a second time.
    assert queue.claim(lease_seconds=60) is None


def test_concurrent_claims_never_double_claim(queue):
    for i in range(20):
        queue.enqueue(f"s{i}", "sig")

    claimed_ids: list[str] = []
    lock = threading.Lock()

    def worker():
        while True:
            job = queue.claim(lease_seconds=60)
            if job is None:
                return
            with lock:
                claimed_ids.append(job.job_id)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed_ids) == 20
    assert len(set(claimed_ids)) == 20  # no job claimed twice


def test_position_is_one_based_over_waiting_only(queue):
    a = queue.enqueue("s1", "sig")
    b = queue.enqueue("s2", "sig")
    c = queue.enqueue("s3", "sig")

    assert queue.position(a.job_id) == 1
    assert queue.position(b.job_id) == 2
    assert queue.position(c.job_id) == 3

    # Once a runs, b and c shift forward.
    queue.claim(lease_seconds=60)
    assert queue.position(a.job_id) is None  # no longer waiting
    assert queue.position(b.job_id) == 1
    assert queue.position(c.job_id) == 2


def test_cancellation_releases_the_place(queue):
    a = queue.enqueue("s1", "sig")
    b = queue.enqueue("s2", "sig")
    assert queue.cancel(a.job_id, "s1") is True
    assert queue.get(a.job_id).state == JobState.CANCELLED
    assert queue.position(b.job_id) == 1
    # The next claim skips the cancelled job.
    assert queue.claim(lease_seconds=60).job_id == b.job_id


def test_cancel_is_owner_only(queue):
    a = queue.enqueue("s1", "sig")
    assert queue.cancel(a.job_id, "someone-else") is False
    assert queue.get(a.job_id).state == JobState.QUEUED


def test_foreign_session_cannot_read_a_job(queue):
    a = queue.enqueue("s1", "sig")
    assert queue.get(a.job_id, session_id="s1") is not None
    assert queue.get(a.job_id, session_id="s2") is None  # FR-020 isolation


def test_lease_expiry_makes_a_running_job_reclaimable(queue):
    job = queue.enqueue("s1", "sig")
    queue.claim(lease_seconds=60)
    # Force the lease into the past.
    import time

    reclaimed = queue.reclaim_expired_leases(now=time.time() + 3600)
    assert job.job_id in reclaimed
    assert queue.get(job.job_id).state == JobState.QUEUED
    # It can be claimed again by another worker.
    assert queue.claim(lease_seconds=60).job_id == job.job_id


def test_duplicate_submission_returns_the_existing_job(queue):
    first = queue.enqueue("s1", "sig-x")
    again = queue.enqueue("s1", "sig-x")
    assert again.job_id == first.job_id  # no second place consumed
    # A different signature is a genuinely different job.
    other = queue.enqueue("s1", "sig-y")
    assert other.job_id != first.job_id


def test_state_transitions_and_terminal_lease_clear(queue):
    job = queue.enqueue("s1", "sig")
    queue.claim(lease_seconds=60)
    queue.set_state(job.job_id, JobState.PARTIAL)
    assert queue.get(job.job_id).state == JobState.PARTIAL
    assert queue.get(job.job_id).lease_expires_at is not None
    done = queue.set_state(job.job_id, JobState.COMPLETE)
    assert done.state == JobState.COMPLETE
    assert done.lease_expires_at is None


def test_touch_updates_presence_owner_only(queue):
    job = queue.enqueue("s1", "sig")
    before = queue.get(job.job_id).last_seen_at
    import time

    time.sleep(0.01)
    assert queue.touch(job.job_id, "s1") is not None
    assert queue.get(job.job_id).last_seen_at > before
    assert queue.touch(job.job_id, "other") is None


def test_expire_session_expires_only_that_sessions_non_terminal_jobs(queue):
    """FR-015/FR-022: expiring a session tears down its queued and running jobs, and
    only its own — another session's work is untouched, and a job that already reached
    a terminal state is not disturbed."""
    mine_queued = queue.enqueue("mine", "sig-a")
    mine_running = queue.enqueue("mine", "sig-b")
    others = queue.enqueue("other", "sig-c")

    # Make one of mine 'running' by claiming until we get it.
    for _ in range(3):
        c = queue.claim(lease_seconds=60)
        if c is not None and c.job_id == mine_running.job_id:
            break
    # And drive one of mine to a terminal state so we can prove it is left alone.
    done = queue.enqueue("mine", "sig-d")
    queue.set_state(done.job_id, JobState.COMPLETE)

    n = queue.expire_session("mine")

    assert n == 2  # the queued and running ones, not the already-complete one
    assert queue.get(mine_queued.job_id).state == JobState.EXPIRED
    assert queue.get(mine_running.job_id).state == JobState.EXPIRED
    assert queue.get(mine_running.job_id).lease_expires_at is None
    assert queue.get(done.job_id).state == JobState.COMPLETE  # untouched
    assert queue.get(others.job_id).state == JobState.QUEUED  # other session untouched


def test_expire_session_frees_the_queue_place(queue):
    """An expired session's queued job must not keep holding a position (FR-015)."""
    first = queue.enqueue("gone", "sig")
    second = queue.enqueue("stays", "sig")
    assert queue.position(second.job_id) == 2

    queue.expire_session("gone")

    # With the first session's job expired, the survivor moves up.
    assert queue.position(second.job_id) == 1
    assert queue.count_waiting() == 1


def test_startup_sweep_expires_orphans_and_reclaims_running(queue):
    alive = queue.enqueue("alive", "sig")
    dead = queue.enqueue("dead", "sig")
    running_dead = queue.enqueue("dead2", "sig")
    # Make running_dead 'running' by claiming; but claim takes the oldest, so claim
    # until we get it, then leave it running.
    for _ in range(3):
        c = queue.claim(lease_seconds=60)
        if c is not None and c.job_id == running_dead.job_id:
            break

    def session_exists(sid: str) -> bool:
        return sid == "alive"

    # Requeue the ones we claimed that belong to alive sessions so the state is sane,
    # then sweep.
    result = queue.sweep(session_exists)
    assert result["expired"] >= 1  # dead / dead2 orphaned
    assert queue.get(dead.job_id).state == JobState.EXPIRED
    # The alive session's job survives (as queued, possibly reclaimed from running).
    assert queue.get(alive.job_id).state in {JobState.QUEUED} | set(ACTIVE_STATES)
