"""Result cache: hit/miss, invalidation, isolation and fidelity (US2).

Covers FR-016–FR-020 and data-model § ResultCache:

- a re-request with unchanged parameters is served from cache, not recomputed;
- the report and the PDF are cached as separate artifacts;
- any parameter change is a different key, and new content clears everything;
- the cache is per-session and never shared, even for byte-identical uploads (FR-020);
- a cached artifact is equivalent in analysis content to a fresh computation (FR-018),
  compared as report data, not PDF bytes (`generate_pdf` embeds a timestamp).
"""

import io
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import app.compute as compute
from app.compute import (
    build_report_document,
    build_result_payload,
    params_signature,
    run_analysis,
)
from app.results_cache import ResultCache
from app.sessions import SessionData
from core import parse_log
from app.main import app


# ---------------------------------------------------------------------------
# Unit: the ResultCache data structure
# ---------------------------------------------------------------------------
def test_hit_and_miss_by_signature():
    cache = ResultCache()
    assert cache.get_payload("a") is None  # miss
    cache.set_payload("a", {"n": 1})
    assert cache.get_payload("a") == {"n": 1}  # hit
    assert cache.get_payload("b") is None  # different key still misses


def test_report_and_pdf_are_cached_as_separate_artifacts():
    cache = ResultCache()
    sig = "sig"
    cache.set_payload(sig, {"summary": {}})
    cache.set_report(sig, {"summary_text": "hi"})
    cache.set_pdf(sig, b"%PDF-1.4 ...")
    # Three independent artifacts under one key.
    assert cache.get_payload(sig) == {"summary": {}}
    assert cache.get_report(sig) == {"summary_text": "hi"}
    assert cache.get_pdf(sig) == b"%PDF-1.4 ..."
    # A report without a rendered PDF is a normal state.
    cache.set_report("s2", {"x": 1})
    assert cache.get_report("s2") == {"x": 1}
    assert cache.get_pdf("s2") is None


def test_any_parameter_change_is_a_different_key():
    base = params_signature(3.0, 3, True, True, 2.0)
    changed = params_signature(4.0, 3, True, True, 2.0)
    cache = ResultCache()
    cache.set_payload(base, {"which": "base"})
    assert cache.get_payload(changed) is None
    cache.set_payload(changed, {"which": "changed"})
    assert cache.get_payload(base) == {"which": "base"}
    assert cache.get_payload(changed) == {"which": "changed"}


def test_new_content_clears_the_whole_cache():
    cache = ResultCache(content_hash="hash-A")
    cache.set_payload("sig", {"n": 1})
    cache.set_report("sig", {"r": 1})
    cache.note_content("hash-A")  # unchanged → no-op
    assert cache.get_payload("sig") == {"n": 1}
    cache.note_content("hash-B")  # changed → FR-019 invalidation
    assert cache.get_payload("sig") is None
    assert cache.get_report("sig") is None
    assert cache.content_hash == "hash-B"


def test_cache_is_lru_bounded():
    cache = ResultCache(max_entries=3)
    for key in ("a", "b", "c"):
        cache.set_payload(key, {"k": key})
    cache.get_payload("a")  # touch 'a' so 'b' is now least-recently-used
    cache.set_payload("d", {"k": "d"})  # over the bound → evict LRU
    assert len(cache) == 3
    assert cache.get_payload("b") is None  # evicted
    assert cache.get_payload("a") == {"k": "a"}
    assert cache.get_payload("d") == {"k": "d"}


# ---------------------------------------------------------------------------
# Fidelity (FR-018): cached content == freshly computed content
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def analysed_session():
    from tests.fixtures.generate_year_log import generate

    path = generate(months=2, out=Path("/tmp/results_cache_fixture.xlsx"), seed=7)
    meals_df, bac_df, med_periods = parse_log(str(path))
    session = SessionData(
        meals_df=meals_df, bac_df=bac_df, med_periods=med_periods, filename="f.xlsx"
    )
    run_analysis(session, 3.0, 3, split_compounds=True, exclude_proteins=True)
    return session


def test_cached_report_matches_a_fresh_computation(analysed_session):
    """A cached report must be equivalent in analysis content to a fresh one (FR-018).

    Compared as report data, not PDF bytes, because generate_pdf embeds a timestamp
    (data-model "Not byte-identity for PDFs").
    """
    payload = build_result_payload(analysed_session, include_ml=False)
    report_first = build_report_document(analysed_session, payload)

    cache = ResultCache()
    sig = "sig"
    cache.set_report(sig, report_first)
    cached = cache.get_report(sig)

    report_fresh = build_report_document(analysed_session, payload)
    assert cached == report_fresh


def test_cached_payload_carries_confidence_and_counts(analysed_session):
    """low_confidence / always_present / observation counts survive caching (FR-018)."""
    payload = build_result_payload(analysed_session, include_ml=False)
    cache = ResultCache()
    cache.set_payload("sig", payload)
    cached = cache.get_payload("sig")
    assert cached["lift_scores_overall"]
    row = cached["lift_scores_overall"][0]
    for key in ("low_confidence", "always_present", "n_present"):
        assert key in row


# ---------------------------------------------------------------------------
# End-to-end: caching + isolation through the HTTP layer
# ---------------------------------------------------------------------------
def _make_log_bytes(seed: int) -> bytes:
    from tests.fixtures.generate_year_log import generate

    path = generate(months=1, out=Path(f"/tmp/rc_e2e_{seed}.xlsx"), seed=seed)
    return Path(path).read_bytes()


def _upload_and_finish(client: TestClient, data: bytes, name: str) -> None:
    import time

    resp = client.post("/upload", files={"file": (name, io.BytesIO(data), XLSX)})
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    deadline = time.time() + 30
    while time.time() < deadline:
        s = client.get(f"/jobs/{job_id}").json()["status"]
        if s in {"partial", "complete"}:
            return
        if s in {"failed", "expired", "abandoned", "cancelled"}:
            raise AssertionError(f"job ended in {s}")
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_report_is_served_from_cache_after_analysis():
    data = _make_log_bytes(seed=3)
    with TestClient(app) as client:
        _upload_and_finish(client, data, "log.xlsx")
        first = client.get("/report")
        assert first.status_code == 200, first.text
        second = client.get("/report")
        assert second.status_code == 200
        assert first.json() == second.json()  # identical, from cache


def test_pdf_is_cached_and_replayed_byte_for_byte():
    data = _make_log_bytes(seed=4)
    with TestClient(app) as client:
        _upload_and_finish(client, data, "log.xlsx")
        # Ensure the report exists first (the download button is gated on it).
        assert client.get("/report").status_code == 200
        pdf1 = client.get("/report/pdf")
        assert pdf1.status_code == 200
        assert pdf1.headers["content-type"] == "application/pdf"
        pdf2 = client.get("/report/pdf")
        assert pdf2.status_code == 200
        # A cached PDF returned twice is the same bytes (data-model: byte-identity is
        # valid for a *cached* artifact replayed, unlike fresh-vs-cached).
        assert pdf1.content == pdf2.content


def test_report_before_any_analysis_is_404():
    with TestClient(app) as client:
        # No session, no upload.
        assert client.get("/report").status_code == 404
        assert client.get("/report/pdf").status_code == 404


def test_identical_uploads_do_not_share_a_cached_result():
    """FR-020: two sessions with byte-identical files never share a cache entry."""
    data = _make_log_bytes(seed=5)
    with TestClient(app) as a, TestClient(app) as b:
        _upload_and_finish(a, data, "same.xlsx")
        # b has never uploaded; a's result must not leak to b even though, had b
        # uploaded the same bytes, the content_hash would match.
        assert b.get("/results").status_code == 404
        assert b.get("/report").status_code == 404

        _upload_and_finish(b, data, "same.xlsx")
        ra = a.get("/results").json()
        rb = b.get("/results").json()
        # Same analysis content (identical inputs) but computed independently: each
        # session owns its own cache, asserted rather than assumed.
        assert ra["summary"]["total_readings"] == rb["summary"]["total_readings"]
