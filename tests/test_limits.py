"""US5 — very large and oversized logs are handled clearly (T048; FR-028–FR-030).

Two guards, tested here:

- an **oversized upload** is refused with ``413`` and the limit named, before it can
  consume capacity, and while it is refused other requests are unaffected (SC-011);
- a **within-size but over-complexity** log is refused with ``422 too_complex`` rather
  than being allowed to occupy a worker indefinitely (FR-030).

The middleware's streamed-abort path (dishonest/absent ``Content-Length``) is exercised
directly, since the test client always sends an honest length.
"""

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.limits as limits
from app.limits import LimitUploadSizeMiddleware
from app.main import app
from tests.fixtures.generate_year_log import generate


@pytest.fixture(scope="module")
def small_file(tmp_path_factory) -> bytes:
    out = tmp_path_factory.mktemp("limits") / "log.xlsx"
    generate(months=1, out=out, seed=5)
    return out.read_bytes()


def _upload(client: TestClient, data: bytes, name: str = "log.xlsx"):
    return client.post(
        "/upload?split_compounds=true&exclude_proteins=true",
        files={"file": (name, data, "application/vnd.openxmlformats")},
    )


# --------------------------------------------------------------- 413 (byte cap)
def test_oversized_upload_is_refused_with_the_limit_named(small_file):
    """SC-011 / FR-028: a file past the byte cap gets a 413 that states the limit, and
    the body never has to be a valid workbook — it is refused before parsing."""
    with TestClient(app) as client:
        junk = b"x" * (limits.MAX_UPLOAD_BYTES + 1024)
        resp = _upload(client, junk)
        assert resp.status_code == 413, resp.text
        body = resp.json()
        assert body["status"] == "too_large"
        assert body["limit_bytes"] == limits.MAX_UPLOAD_BYTES
        # The message names a concrete limit the person can act on.
        assert "limit" in body["message"].lower()


def test_a_rejected_upload_does_not_affect_other_requests(small_file):
    """SC-011 / FR-029: while an oversized upload is refused, an unrelated request still
    succeeds promptly — the rejection is isolated."""
    with TestClient(app) as client:
        junk = b"x" * (limits.MAX_UPLOAD_BYTES + 1024)
        assert _upload(client, junk).status_code == 413

        start = time.perf_counter()
        index = client.get("/")
        elapsed = time.perf_counter() - start
        assert index.status_code == 200
        assert elapsed < 2.0


def test_a_normal_upload_is_not_refused(small_file):
    """A within-cap, valid log is accepted (guard does not over-reject)."""
    with TestClient(app) as client:
        resp = _upload(client, small_file)
        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] in ("queued", "running", "partial")


# --------------------------------------------------------------- 422 (complexity)
def test_within_size_but_too_complex_log_is_refused(small_file, monkeypatch):
    """FR-030: a log inside the byte cap but past the complexity ceiling gets a clear
    422 too_complex, not an indefinite wait. Forced by lowering the ceiling."""
    monkeypatch.setattr(limits, "MAX_LOOKBACK_PAIRS", 1)
    with TestClient(app) as client:
        resp = _upload(client, small_file)
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["status"] == "too_complex"
        assert "too large" in body["message"].lower()


def test_complexity_metric_is_readings_times_meals():
    class _DF:
        def __init__(self, n):
            self._n = n

        def __len__(self):
            return self._n

    assert limits.estimate_lookback_pairs(_DF(2000), _DF(5000)) == 10_000_000
    # One year (~10.6M) is comfortably under the shipped 50M ceiling.
    assert not limits.exceeds_complexity(_DF(2011), _DF(5288))


# --------------------------------------------------------------- middleware unit
def test_middleware_aborts_a_dishonest_upload_without_content_length():
    """R9: with no Content-Length, the cap is enforced by counting streamed bytes and
    aborting once it is crossed — the whole oversized body is never buffered."""

    async def _dummy_app(scope, receive, send):  # pragma: no cover - must not run
        raise AssertionError("app should not be reached for an oversized upload")

    mw = LimitUploadSizeMiddleware(_dummy_app, max_upload_size=1000)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/upload",
        "headers": [],  # no content-length
    }

    # Stream three 600-byte chunks (1800 > 1000) with more_body set.
    chunks = [
        {"type": "http.request", "body": b"a" * 600, "more_body": True},
        {"type": "http.request", "body": b"b" * 600, "more_body": True},
        {"type": "http.request", "body": b"c" * 600, "more_body": False},
    ]
    it = iter(chunks)

    async def receive():
        return next(it)

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    asyncio.run(mw(scope, receive, send))

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413


def test_middleware_ignores_non_upload_paths():
    """A GET, or a POST to another path, must stream through untouched (FR-029)."""
    seen = {}

    async def _echo_app(scope, receive, send):
        seen["called"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = LimitUploadSizeMiddleware(_echo_app, max_upload_size=1)
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    asyncio.run(mw(scope, receive, send))
    assert seen.get("called") is True
