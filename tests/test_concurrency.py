"""US1 — concurrent analyses that never block another person's page (T017).

The original defect: one analysis ran synchronously on the event loop and froze the
site for everyone. This suite proves the fixed behaviour end to end through the HTTP
layer: uploads return a job, the job runs off the event loop, each session gets only
its own result, and the page stays responsive while analyses run (SC-002, SC-007).
"""

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.fixtures.generate_year_log import generate


@pytest.fixture(scope="module")
def small_file(tmp_path_factory) -> bytes:
    out = tmp_path_factory.mktemp("conc") / "log.xlsx"
    generate(months=2, out=out, seed=3)
    return out.read_bytes()


@pytest.fixture(scope="module")
def other_file(tmp_path_factory) -> bytes:
    out = tmp_path_factory.mktemp("conc") / "other.xlsx"
    generate(months=3, out=out, seed=77)
    return out.read_bytes()


def _upload(client: TestClient, data: bytes, name: str):
    return client.post(
        "/upload?hours=3&min_obs=3&split_compounds=true&exclude_proteins=true",
        files={"file": (name, data, "application/vnd.openxmlformats")},
    )


def _poll_until_done(client: TestClient, job_id: str, timeout: float = 30.0) -> str:
    deadline = time.time() + timeout
    terminal = {"complete", "failed", "cancelled", "abandoned", "expired"}
    while time.time() < deadline:
        r = client.get(f"/jobs/{job_id}")
        assert r.status_code == 200, r.text
        status = r.json()["status"]
        if status in terminal:
            return status
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def _analyse(client: TestClient, data: bytes, name: str) -> dict:
    resp = _upload(client, data, name)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] in ("queued", "running", "partial")
    assert "job_id" in body
    status = _poll_until_done(client, body["job_id"])
    assert status == "complete", f"unexpected terminal state {status}"
    results = client.get("/results")
    assert results.status_code == 200, results.text
    return results.json()


def test_upload_returns_a_job_then_results(small_file):
    with TestClient(app) as client:
        payload = _analyse(client, small_file, "mylog.xlsx")
        assert payload["filename"] == "mylog.xlsx"
        assert payload["summary"]["total_readings"] > 0
        assert isinstance(payload["lift_scores_overall"], list)
        # ML block present after stage two.
        assert "ml" in payload


def test_get_results_404_before_anything_computed():
    with TestClient(app) as client:
        # No upload yet on this fresh session.
        r = client.get("/results")
        assert r.status_code == 404


def test_concurrent_analyses_each_get_their_own_result(small_file, other_file):
    """SC-007: two sessions running at once never see each other's data."""
    results: dict[str, dict] = {}
    errors: list[Exception] = []

    def run(key, data, name):
        try:
            with TestClient(app) as client:
                results[key] = _analyse(client, data, name)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=run, args=("a", small_file, "alice.xlsx"))
    t2 = threading.Thread(target=run, args=("b", other_file, "bob.xlsx"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, errors
    assert results["a"]["filename"] == "alice.xlsx"
    assert results["b"]["filename"] == "bob.xlsx"
    # The two logs are different sizes, so their summaries must differ — proving each
    # result was derived from its own upload, not shared.
    assert (
        results["a"]["summary"]["total_readings"]
        != results["b"]["summary"]["total_readings"]
    )


def test_site_stays_responsive_while_an_analysis_runs(small_file):
    """SC-002: a non-analysis request stays fast while an analysis is in flight."""
    with TestClient(app) as client:
        resp = _upload(client, small_file, "busy.xlsx")
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # While the job is being processed off the event loop, the landing page and
        # the status poll must both respond promptly.
        start = time.perf_counter()
        index = client.get("/")
        elapsed = time.perf_counter() - start
        assert index.status_code == 200
        assert elapsed < 2.0, f"index blocked for {elapsed:.2f}s during analysis"

        _poll_until_done(client, job_id)


def test_foreign_job_id_is_not_readable(small_file):
    """A job id from one session must 404 for another (FR-020)."""
    with TestClient(app) as owner:
        resp = _upload(owner, small_file, "owned.xlsx")
        job_id = resp.json()["job_id"]
        with TestClient(app) as stranger:
            # Stranger has no session cookie / a different one.
            r = stranger.get(f"/jobs/{job_id}")
            assert r.status_code == 404
        _poll_until_done(owner, job_id)
