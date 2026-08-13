"""US4 — data stays with its owner for the whole session, and is gone after (T042–T044).

Covers the privacy promise the project makes:

- SC-007 / FR-020: two people using the site at once never see each other's data
  through *any* endpoint.
- SC-008 / FR-021: across a long sequence of interactions, the owner is never asked to
  re-upload — their data stays available for the whole session.
- SC-009 / FR-022: after a session expires nothing of it is retrievable through the app.
- FR-015: expiring a session tears down any queued/running job it owns.
- SC-010 / FR-023: the full journey completes with no account of any kind.
"""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.jobs import InMemoryJobQueue, JobState
from app.main import app
from app.sessions import InMemoryStore, SessionData
from tests.fixtures.generate_year_log import generate


@pytest.fixture(scope="module")
def alice_file(tmp_path_factory) -> bytes:
    out = tmp_path_factory.mktemp("iso") / "alice.xlsx"
    generate(months=2, out=out, seed=11)
    return out.read_bytes()


@pytest.fixture(scope="module")
def bob_file(tmp_path_factory) -> bytes:
    out = tmp_path_factory.mktemp("iso") / "bob.xlsx"
    generate(months=4, out=out, seed=222)
    return out.read_bytes()


# --------------------------------------------------------------------------- helpers
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
    job_id = resp.json()["job_id"]
    assert _poll_until_done(client, job_id) == "complete"
    results = client.get("/results")
    assert results.status_code == 200, results.text
    return results.json()


# --------------------------------------------------------------------------- SC-007
def test_two_sessions_never_see_each_others_data_across_endpoints(alice_file, bob_file):
    """SC-007 / FR-020: with two live sessions, every read endpoint returns only the
    caller's own data, and never the other person's — even when hitting the same URLs."""
    with TestClient(app) as alice, TestClient(app) as bob:
        alice_payload = _analyse(alice, alice_file, "alice.xlsx")
        bob_payload = _analyse(bob, bob_file, "bob.xlsx")

        # Each /results reflects its own upload.
        assert alice.get("/results").json()["filename"] == "alice.xlsx"
        assert bob.get("/results").json()["filename"] == "bob.xlsx"

        # The logs differ in size, so their summaries must differ — proof the payloads
        # were not crossed.
        assert (
            alice_payload["summary"]["total_readings"]
            != bob_payload["summary"]["total_readings"]
        )

        # /report is per-session.
        a_report = alice.get("/report")
        b_report = bob.get("/report")
        assert a_report.status_code == 200 and b_report.status_code == 200
        assert a_report.json() != b_report.json()

        # /report/pdf bytes differ between the two people.
        a_pdf = alice.get("/report/pdf")
        b_pdf = bob.get("/report/pdf")
        assert a_pdf.status_code == 200 and b_pdf.status_code == 200
        assert a_pdf.content != b_pdf.content

        # /predict answers from the caller's own model, for both.
        for client in (alice, bob):
            pred = client.post("/predict", json={"ingredients": ["beer"]})
            assert pred.status_code == 200, pred.text

        # A stranger with no session sees nothing on any read endpoint.
        with TestClient(app) as stranger:
            for path in ("/results", "/report", "/report/pdf", "/ingredient/beer"):
                assert stranger.get(path).status_code == 404, path
            assert (
                stranger.post("/predict", json={"ingredients": ["beer"]}).status_code
                == 404
            )


# --------------------------------------------------------------------------- SC-010
def test_full_journey_completes_with_no_account(alice_file):
    """SC-010 / FR-023: upload → analyse → read report → download PDF → predict, all
    with nothing but an anonymous cookie. No login, token or identifying header."""
    with TestClient(app) as client:
        # No auth header is ever set on this client.
        payload = _analyse(client, alice_file, "anon.xlsx")
        assert payload["summary"]["total_readings"] > 0

        assert client.get("/report").status_code == 200
        assert client.get("/report/pdf").status_code == 200
        assert (
            client.post("/predict", json={"ingredients": ["beer"]}).status_code == 200
        )

        # The only thing tying requests together is the opaque session cookie.
        assert "session_id" in client.cookies
        assert set(client.cookies.keys()) == {"session_id"}


# --------------------------------------------------------------------------- SC-008
def test_owner_is_never_asked_to_reupload_across_a_long_session(alice_file):
    """SC-008 / FR-021: over a long run of varied interactions the upload stays
    available — no request degrades into 'upload a file first'."""
    with TestClient(app) as client:
        _analyse(client, alice_file, "long.xlsx")

        for i in range(30):
            assert client.get("/results").status_code == 200
            assert client.get("/report").status_code == 200
            # A pure recompute (same settings) must serve from cache, never 404.
            r = client.post(
                "/results",
                json={
                    "hours": 3,
                    "min_obs": 3,
                    "split_compounds": True,
                    "exclude_proteins": True,
                    "episode_threshold": 2.0,
                },
            )
            assert r.status_code == 200, f"iteration {i}: {r.status_code} {r.text}"


# --------------------------------------------------------------------------- SC-009
def test_nothing_is_retrievable_after_the_session_expires(alice_file):
    """SC-009 / FR-022: once the session has expired, every endpoint behaves as if the
    upload never happened. Forced by shrinking the live store's TTL."""
    original_ttl = main.store._ttl
    try:
        with TestClient(app) as client:
            _analyse(client, alice_file, "ephemeral.xlsx")
            assert client.get("/results").status_code == 200  # still alive

            # Expire the session: next access discards it.
            main.store._ttl = 0
            time.sleep(0.01)

            for path in ("/results", "/report", "/report/pdf", "/ingredient/beer"):
                assert client.get(path).status_code == 404, path
            assert (
                client.post("/predict", json={"ingredients": ["beer"]}).status_code
                == 404
            )
    finally:
        main.store._ttl = original_ttl


# --------------------------------------------------------------------------- FR-015
def test_session_expiry_expires_its_queued_and_running_jobs():
    """FR-015 / FR-022: when a session is discarded on expiry, the store's expiry hook
    tears down every job it owns so no place is held for data that is already gone.
    Exercised directly on the store+queue wiring used in `app/main.py`."""
    q = InMemoryJobQueue()
    store = InMemoryStore(ttl=0.05, on_expire=lambda sid: q.expire_session(sid))
    store.set("s1", SessionData())

    queued = q.enqueue("s1", "sig-a")
    running = q.enqueue("s1", "sig-b")
    # Drive one to running.
    for _ in range(2):
        c = q.claim(lease_seconds=60)
        if c is not None and c.job_id == running.job_id:
            break

    time.sleep(0.1)
    # Any access past the TTL discards the session and fires on_expire.
    assert store.get("s1") is None

    assert q.get(queued.job_id, "s1").state == JobState.EXPIRED
    assert q.get(running.job_id, "s1").state == JobState.EXPIRED
