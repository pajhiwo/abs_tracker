"""Presence and abandonment (US3, T037; FR-012–FR-014; research R3).

A queued job at the front runs only if its owner has been seen within the grace
window; otherwise it is dropped as `abandoned` and its capacity goes to the next
person. A returning session can restart without re-uploading, because the parsed
frames are retained for the whole session.
"""

import io
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.executor import AnalysisExecutor
from app.jobs import InMemoryJobQueue, JobState
from app.sessions import InMemoryStore, SessionData
from app.main import app, _STATE_MESSAGES
from core import parse_log

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

DEFAULT_PARAMS = {
    "hours": 3.0,
    "min_obs": 3,
    "split_compounds": True,
    "exclude_proteins": True,
    "episode_threshold": 2.0,
}


@pytest.fixture(autouse=True)
def _isolate_module_state():
    import app.main as m

    if hasattr(m.queue, "_jobs"):
        m.queue._jobs.clear()
    if hasattr(m.store, "_store"):
        m.store._store.clear()
    yield


@pytest.fixture(scope="module")
def frames():
    from tests.fixtures.generate_year_log import generate

    path = generate(months=1, out=Path("/tmp/presence_fixture.xlsx"), seed=9)
    return parse_log(str(path))


def _session(frames) -> SessionData:
    meals_df, bac_df, med = frames
    return SessionData(
        meals_df=meals_df, bac_df=bac_df, med_periods=med, filename="f.xlsx"
    )


def _make_log_bytes(seed: int = 31) -> bytes:
    from tests.fixtures.generate_year_log import generate

    path = generate(months=1, out=Path(f"/tmp/presence_e2e_{seed}.xlsx"), seed=seed)
    return Path(path).read_bytes()


# ---------------------------------------------------------------------------
# Abandonment at the front of the queue (FR-013, SC-014)
# ---------------------------------------------------------------------------
def test_stale_job_is_abandoned_at_its_turn_not_run(frames):
    q = InMemoryJobQueue()
    store = InMemoryStore()
    store.set("s1", _session(frames))
    ex = AnalysisExecutor(q, store, max_concurrent=1, abandon_grace_seconds=300)

    job = q.enqueue("s1", "sig")
    # The owner has not polled for a long time — well past the grace window.
    q._jobs[job.job_id].last_seen_at = time.time() - 10_000

    ex.pump()

    assert q.get(job.job_id).state == JobState.ABANDONED
    # No capacity was spent: nothing was computed for the session.
    assert store.get("s1").results.get_payload("sig") is None


def test_a_present_job_runs_at_its_turn(frames):
    q = InMemoryJobQueue()
    store = InMemoryStore()
    store.set("s1", _session(frames))
    ex = AnalysisExecutor(q, store, max_concurrent=1, abandon_grace_seconds=300)

    job = q.enqueue("s1", "sig")  # last_seen_at = now → clearly present
    ex.pump()

    deadline = time.time() + 30
    while time.time() < deadline:
        state = q.get(job.job_id).state
        if state in (JobState.PARTIAL, JobState.COMPLETE):
            break
        assert state != JobState.ABANDONED, "a present job must not be abandoned"
        time.sleep(0.05)
    assert q.get(job.job_id).state in (JobState.PARTIAL, JobState.COMPLETE)


def test_the_next_person_gets_the_abandoned_slot(frames):
    q = InMemoryJobQueue()
    store = InMemoryStore()
    store.set("gone", _session(frames))
    store.set("here", _session(frames))
    ex = AnalysisExecutor(q, store, max_concurrent=1, abandon_grace_seconds=300)

    stale = q.enqueue("gone", "sig-gone")
    q._jobs[stale.job_id].last_seen_at = time.time() - 10_000
    fresh = q.enqueue("here", "sig-here")

    ex.pump()  # drops the stale front, then starts the next

    assert q.get(stale.job_id).state == JobState.ABANDONED
    deadline = time.time() + 30
    while time.time() < deadline:
        if q.get(fresh.job_id).state in (JobState.PARTIAL, JobState.COMPLETE):
            break
        time.sleep(0.05)
    assert q.get(fresh.job_id).state in (JobState.PARTIAL, JobState.COMPLETE)


# ---------------------------------------------------------------------------
# Restart without re-upload (FR-014)
# ---------------------------------------------------------------------------
def test_abandoned_message_invites_restart_without_reupload():
    msg = _STATE_MESSAGES[JobState.ABANDONED]
    assert "re-upload" in msg.lower()


def _poll_until_done(client: TestClient, job_id: str, timeout: float = 30.0) -> None:
    terminal = {"complete", "partial"}
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get(f"/jobs/{job_id}").json()["status"]
        if s in terminal:
            return
        if s in {"failed", "expired", "abandoned", "cancelled"}:
            raise AssertionError(f"job ended in {s}")
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


def test_returning_session_restarts_without_reuploading():
    data = _make_log_bytes()
    with TestClient(app) as c:
        up = c.post("/upload", files={"file": ("l.xlsx", io.BytesIO(data), XLSX)})
        _poll_until_done(c, up.json()["job_id"])

        # A fresh analysis with changed settings needs no re-upload: the parsed frames
        # are retained for the whole session, so /results alone re-runs it (FR-008/014).
        r = c.post("/results", json={**DEFAULT_PARAMS, "hours": 5.0})
        assert r.status_code in (200, 202)
        if r.status_code == 202:
            _poll_until_done(c, r.json()["job_id"])
        assert c.get("/results").status_code == 200
