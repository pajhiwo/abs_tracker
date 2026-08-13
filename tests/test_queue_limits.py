"""Queue caps, estimation, duplicate suppression and supersede (US3, T032).

Covers FR-005–FR-011 and data-model § AnalysisQueue:

- position is derived at read time, not stored;
- the wait estimate is recomputed and scales with position;
- a submission is refused when EITHER cap trips first (max_waiting / max_estimated_wait);
- the two `503` causes read differently (queue vs session saturation) and carry Retry-After;
- a duplicate `(session, signature)` returns the existing job;
- a new signature supersedes and cancels the session's prior job.
"""

import io
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.executor import AnalysisExecutor
from app.jobs import InMemoryJobQueue, JobState
from app.sessions import InMemoryStore, SessionStoreAtCapacity
from app.main import app

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

DEFAULT_PARAMS = {
    "hours": 3.0,
    "min_obs": 3,
    "split_compounds": True,
    "exclude_proteins": True,
    "episode_threshold": 2.0,
}


def _make_log_bytes(seed: int = 21) -> bytes:
    from tests.fixtures.generate_year_log import generate

    path = generate(months=1, out=Path(f"/tmp/ql_{seed}.xlsx"), seed=seed)
    return Path(path).read_bytes()


@pytest.fixture(autouse=True)
def _isolate_module_state():
    """Keep the shared in-memory queue/store from leaking between tests."""
    import app.main as m

    if hasattr(m.queue, "_jobs"):
        m.queue._jobs.clear()
    if hasattr(m.store, "_store"):
        m.store._store.clear()
    yield


def _exec(**kw) -> tuple[AnalysisExecutor, InMemoryJobQueue]:
    q = InMemoryJobQueue()
    ex = AnalysisExecutor(q, InMemoryStore(), max_concurrent=1, **kw)
    return ex, q


# ---------------------------------------------------------------------------
# Position + estimation (data-model AnalysisQueue; SC-012)
# ---------------------------------------------------------------------------
def test_position_is_derived_at_read_time():
    _, q = _exec()
    j1 = q.enqueue("s1", "a")
    j2 = q.enqueue("s2", "b")
    j3 = q.enqueue("s3", "c")
    assert q.position(j1.job_id) == 1
    assert q.position(j3.job_id) == 3
    # Completing the front shifts everyone up: position is computed, never stored.
    q.set_state(j1.job_id, JobState.COMPLETE)
    assert q.position(j2.job_id) == 1
    assert q.position(j3.job_id) == 2


def test_estimate_scales_with_position_and_uses_cold_start_prior():
    ex, _ = _exec(cold_start_prior_seconds=6.0)  # max_concurrent=1, no durations yet
    assert ex.estimate_wait_seconds(1) == 6
    assert ex.estimate_wait_seconds(3) == 18


def test_estimate_prefers_size_class_durations_when_available():
    ex, _ = _exec(cold_start_prior_seconds=100.0)
    # Record a couple of small-class durations; the estimate should follow them, not
    # the cold-start prior, once the class has history (T034).
    ex._record_duration("small", 2.0)
    ex._record_duration("small", 4.0)
    assert ex.estimate_wait_seconds(1, "small") == 3  # median(2,4)=3
    # A class with no history falls back to the overall median (also 3 here).
    assert ex.estimate_wait_seconds(1, "large") == 3


# ---------------------------------------------------------------------------
# The two caps (FR-010)
# ---------------------------------------------------------------------------
def test_refusal_when_max_waiting_trips_first():
    ex, q = _exec(max_waiting=2, max_estimated_wait=10_000, cold_start_prior_seconds=1.0)
    assert ex.can_accept()  # nothing waiting yet
    q.enqueue("s1", "a")
    q.enqueue("s2", "b")
    assert q.count_waiting() == 2
    assert not ex.can_accept()  # waiting cap reached, wait cap nowhere near


def test_refusal_when_estimated_wait_trips_first():
    ex, q = _exec(max_waiting=1000, max_estimated_wait=10, cold_start_prior_seconds=5.0)
    # max_concurrent=1, prior=5 → estimate(pos) = 5·pos.
    q.enqueue("s1", "a")  # waiting=1 → newcomer at pos 2 → 10 ≤ 10 → accept
    assert ex.can_accept()
    q.enqueue("s2", "b")  # waiting=2 → newcomer at pos 3 → 15 > 10 → refuse
    assert not ex.can_accept()  # wait cap trips while waiting cap (1000) is untouched


# ---------------------------------------------------------------------------
# At-capacity 503s (contracts; R10)
# ---------------------------------------------------------------------------
def test_queue_saturation_returns_503_with_retry_after(monkeypatch):
    monkeypatch.setattr("app.main.executor.can_accept", lambda size=None: False)
    data = _make_log_bytes()
    with TestClient(app) as c:
        r = c.post("/upload", files={"file": ("l.xlsx", io.BytesIO(data), XLSX)})
        assert r.status_code == 503
        assert r.headers["retry-after"]
        body = r.json()
        assert body["status"] == "at_capacity"
        assert "running" in body["message"].lower()  # queue wording, not session


def test_session_saturation_has_a_distinct_503(monkeypatch):
    def _full(*a, **k):
        raise SessionStoreAtCapacity("full")

    monkeypatch.setattr("app.main.store.set", _full)
    data = _make_log_bytes()
    with TestClient(app) as c:
        r = c.post("/upload", files={"file": ("l.xlsx", io.BytesIO(data), XLSX)})
        assert r.status_code == 503
        assert r.headers["retry-after"]
        assert "holding as many people" in r.json()["message"]


# ---------------------------------------------------------------------------
# Duplicate suppression + supersede (data-model "Settings changed mid-analysis")
# ---------------------------------------------------------------------------
def test_duplicate_submission_returns_the_existing_job(monkeypatch):
    # Freeze the pump so jobs stay queued and the dedup path is observable.
    monkeypatch.setattr("app.main.executor.pump", lambda: None)
    data = _make_log_bytes()
    with TestClient(app) as c:
        up = c.post("/upload", files={"file": ("l.xlsx", io.BytesIO(data), XLSX)})
        first = up.json()["job_id"]
        # Same settings as the upload defaults → same signature → same job.
        again = c.post("/results", json=DEFAULT_PARAMS)
        assert again.status_code == 202
        assert again.json()["job_id"] == first


def test_new_signature_supersedes_and_cancels_prior_job(monkeypatch):
    monkeypatch.setattr("app.main.executor.pump", lambda: None)
    data = _make_log_bytes()
    with TestClient(app) as c:
        up = c.post("/upload", files={"file": ("l.xlsx", io.BytesIO(data), XLSX)})
        a = up.json()["job_id"]
        changed = {**DEFAULT_PARAMS, "hours": 5.0}
        r = c.post("/results", json=changed)
        b = r.json()["job_id"]
        assert b != a
        # The earlier job is cancelled and its place released (FR-009 mechanics).
        assert c.get(f"/jobs/{a}").json()["status"] == "cancelled"
        assert c.get(f"/jobs/{b}").json()["status"] in ("queued", "running")
