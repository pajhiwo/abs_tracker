"""
ABS Tracker — FastAPI Web Application
--------------------------------------
Run with:  uvicorn app.main:app --reload
"""

import hashlib
import re
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, Cookie, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from core import parse_log
from ai import predict_risk
from app import sessions
from app.sessions import (
    SessionData,
    SessionStoreAtCapacity,
    create_store,
    new_session_id,
)
from app.compute import (
    DEFAULT_EXCLUDE_PROTEINS,
    params_signature,
)
from app.jobs import JobState, create_job_queue
from app.executor import AnalysisExecutor, size_class
from report.pdf_export import generate_pdf

store = create_store()
queue = create_job_queue()
executor = AnalysisExecutor(queue, store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup sweep (research R5a "Ephemeral filesystem"): a redeploy must not serve
    # stale positions. Reclaim jobs left running by a dead worker and expire jobs whose
    # session no longer exists.
    def _session_exists(sid: str) -> bool:
        return store.get(sid) is not None

    await run_in_threadpool(queue.sweep, _session_exists)
    yield
    # The executor is a process-lifetime singleton; its thread pool is reclaimed at
    # interpreter exit. We deliberately do not shut it down per app-context, so it
    # survives repeated app startups (e.g. across tests) rather than becoming unusable.


app = FastAPI(title="ABS Diet Tracker", version="0.1.0", lifespan=lifespan)

# Serve static files (HTML/CSS/JS)
STATIC_DIR = Path(__file__).parent / "static"
# index.html lives outside STATIC_DIR deliberately: it is rendered per request to
# fill in the retention window, and serving it verbatim through the /static mount
# would publish a working upload page with no retention figure on it.
TEMPLATE_DIR = Path(__file__).parent / "templates"
EXAMPLE_DIR = Path(__file__).parent.parent / "example"
app.mount("/example", StaticFiles(directory=str(EXAMPLE_DIR)), name="example")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------
def _get_or_create_session(
    response: Response, session_id: str | None
) -> tuple[str, SessionData]:
    """Return (session_id, SessionData), creating new if needed.

    Raises SessionStoreAtCapacity when a NEW session cannot be admitted (surfaced as
    a `503 at_capacity` for session saturation, distinct from queue saturation — R10).
    """
    if session_id:
        data = store.get(session_id)
        if data is not None:
            return session_id, data
    # New session
    sid = new_session_id()
    data = SessionData()
    store.set(sid, data)  # may raise SessionStoreAtCapacity
    response.set_cookie("session_id", sid, httponly=True, samesite="lax")
    return sid, data


class AnalysisParams(BaseModel):
    hours: float = 3.0
    min_obs: int = 3
    split_compounds: bool = True
    exclude_proteins: bool = DEFAULT_EXCLUDE_PROTEINS
    episode_threshold: float = 2.0


# ---------------------------------------------------------------------------
# Job-response helpers
# ---------------------------------------------------------------------------
def _session_signature(session: SessionData) -> str:
    """params_signature for the parameters currently resolved on the session."""
    return params_signature(
        session.hours,
        session.min_obs,
        session.split_compounds,
        session.exclude_proteins,
        session.episode_threshold,
    )


_STATE_MESSAGES = {
    JobState.FAILED: "Analysis failed. Please try again.",
    JobState.ABANDONED: "We paused your analysis while you were away. Restart it — no re-upload needed.",
    JobState.EXPIRED: "Your session expired. Please upload your file again.",
}


def _accepted_body(job, position: int | None, size: str | None = None) -> dict:
    """The `202 Accepted` body (contracts "Job accepted")."""
    body = {
        "status": job.state.value,
        "job_id": job.job_id,
        "poll_after_seconds": executor.poll_after_seconds,
    }
    if job.state == JobState.QUEUED and position is not None:
        body["position"] = position
        body["estimated_wait_seconds"] = executor.estimate_wait_seconds(position, size)
    return body


# The two 503 causes read differently (contracts "Other errors"; R10): a saturated
# queue is worth waiting out; session saturation means the app is holding all it can.
_QUEUE_FULL_MESSAGE = (
    "Too many analyses are running right now. Please try again in a few minutes."
)
_SESSIONS_FULL_MESSAGE = (
    "The application is holding as many people as it can right now. "
    "Please try again in a few minutes."
)
# Presence is refreshed at most this often, so polling does not become a per-request
# SQLite write at rung 2 (research R5b). Well under the grace window (minutes).
_PRESENCE_DEBOUNCE_SECONDS = 20.0


def _set_at_capacity(response: Response, message: str, retry_after: int = 180) -> dict:
    """Mark `response` as a 503 at-capacity and return its body.

    We mutate the injected response rather than returning a new one so any Set-Cookie
    already staged on it survives.
    """
    response.status_code = 503
    response.headers["Retry-After"] = str(retry_after)
    return {
        "status": "at_capacity",
        "message": message,
        "retry_after_seconds": retry_after,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
_RETENTION_WINDOW = re.compile(
    r'(<span class="retention-window">)[^<]*(</span>)', re.DOTALL
)


def _retention_window_text(ttl_seconds: int) -> str:
    """Human phrasing for the session lifetime, rounded away from zero.

    The disclosure frames this as an upper bound ("up to", "at most"), which is
    what the implementation can actually honour: capacity pressure can discard a
    session earlier, and rounding a sub-minute TTL up to one minute only ever
    overstates the bound, never the guarantee.
    """
    minutes = max(1, -(-ttl_seconds // 60))
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main page.

    The retention figures in the disclosure are substituted from the live
    SESSION_TTL so the stated retention cannot drift from the configured one
    (constitution Principle IV). This is the only route that serves the page, so
    there is no path on which the disclosure appears unrendered.
    """
    html = (TEMPLATE_DIR / "index.html").read_text()
    window = _retention_window_text(sessions.SESSION_TTL)
    return HTMLResponse(content=_RETENTION_WINDOW.sub(rf"\g<1>{window}\g<2>", html))


def _parse_bytes(data: bytes, split_compounds: bool):
    """Parse uploaded bytes via a temp file. Called off the event loop (R11)."""
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        return parse_log(tmp_path, split_compounds=split_compounds)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


async def _enqueue_and_accept(
    response: Response, sid: str, session: SessionData, sig: str
) -> dict:
    """Enqueue a job for `sig`, start the pump, and return the 202 body.

    - Duplicate `(session, signature)` returns the existing job untouched, so a
      double-click never consumes two places and is never refused for capacity.
    - A *different* signature supersedes the session's prior job: the earlier one is
      cancelled and its place released, so results correspond to the latest settings
      (data-model "Settings changed mid-analysis").
    - A genuinely new submission is subject to the FR-010 caps and may be refused with
      the queue-saturation `503`.

    Sets status on the injected `response` (not a new one) so a freshly-issued session
    cookie survives. Every queue call is offloaded off the event loop because `sqlite3`
    blocks it (R5a; T015).
    """
    size = size_class(session)
    prior_id = session.active_job_id
    prior = (
        await run_in_threadpool(queue.get, prior_id, sid) if prior_id else None
    )
    is_duplicate = (
        prior is not None and not prior.is_terminal and prior.params_signature == sig
    )

    if not is_duplicate and not await run_in_threadpool(executor.can_accept, size):
        return _set_at_capacity(response, _QUEUE_FULL_MESSAGE)

    job = await run_in_threadpool(queue.enqueue, sid, sig)
    if prior is not None and not prior.is_terminal and prior.job_id != job.job_id:
        # Supersede the earlier, now-outdated job and free its place (FR-009 mechanics).
        await run_in_threadpool(queue.cancel, prior.job_id, sid)
    session.active_job_id = job.job_id
    store.set(sid, session)  # existing session → update, never raises capacity
    await run_in_threadpool(executor.pump)
    fresh = await run_in_threadpool(queue.get, job.job_id, sid)
    position = await run_in_threadpool(queue.position, job.job_id)
    response.status_code = 202
    return _accepted_body(fresh or job, position, size)


@app.post("/upload")
async def upload_file(
    response: Response,
    file: UploadFile = File(...),
    hours: float = 3.0,
    min_obs: int = 3,
    split_compounds: bool = True,
    exclude_proteins: bool = DEFAULT_EXCLUDE_PROTEINS,
    session_id: str | None = Cookie(default=None),
):
    """Validate and parse an upload (off the event loop), then enqueue an analysis.

    Parsing happens before enqueueing (R11): a malformed workbook is rejected here
    with a clear message rather than failing inside a worker, and the job carries no
    uploaded bytes. Returns `202` with a job reference; the client polls `/jobs/{id}`.
    """
    if not file.filename or not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Only .xlsx / .xls files are supported")

    try:
        sid, session = _get_or_create_session(response, session_id)
    except SessionStoreAtCapacity:
        return _set_at_capacity(response, _SESSIONS_FULL_MESSAGE)

    contents = await file.read()
    content_hash = hashlib.sha256(contents).hexdigest()

    try:
        meals_df, bac_df, med_periods = await run_in_threadpool(
            _parse_bytes, contents, split_compounds
        )
    except Exception:
        raise HTTPException(
            400, "Could not read that file. Please upload a valid ABS log (.xlsx)."
        )

    if bac_df.empty:
        raise HTTPException(400, "No BAC readings found in the file")

    session.meals_df = meals_df
    session.bac_df = bac_df
    session.med_periods = med_periods
    session.filename = file.filename
    session.content_hash = content_hash
    # raw_bytes is retained for the session so the split_compounds toggle can re-parse.
    # Dropping it is deferred to T045 (US4/R6), which lifts compound splitting into a
    # pure transform and removes the need to keep the upload around.
    session.raw_bytes = contents
    session.hours = hours
    session.min_obs = min_obs
    session.split_compounds = split_compounds
    session.exclude_proteins = exclude_proteins
    # Fresh content invalidates the whole result cache (FR-019).
    session.results.note_content(content_hash)
    sig = _session_signature(session)
    return await _enqueue_and_accept(response, sid, session, sig)


@app.get("/results")
async def get_results(
    response: Response, session_id: str | None = Cookie(default=None)
):
    """Return the cached results for the session's current parameters (200), or 404."""
    if not session_id:
        raise HTTPException(404, "No data loaded — upload a file first")
    session = store.get(session_id)
    if session is None or session.bac_df is None:
        raise HTTPException(404, "No data loaded — upload a file first")
    cached = session.results.get_payload(_session_signature(session))
    if cached is None:
        raise HTTPException(404, "No results yet — the analysis is still running")
    return cached


@app.post("/results")
async def recompute(
    params: AnalysisParams,
    response: Response,
    session_id: str | None = Cookie(default=None),
):
    """Serve cached results for the requested parameters (200), or enqueue a job (202)."""
    if not session_id:
        raise HTTPException(404, "No data loaded — upload a file first")
    session = store.get(session_id)
    if session is None or session.bac_df is None:
        raise HTTPException(404, "No data loaded — upload a file first")

    # A split_compounds change means the parsed frames must be rebuilt from the upload
    # (done off the event loop). Everything else is a pure recompute over existing frames.
    if params.split_compounds != session.split_compounds and session.raw_bytes is not None:
        meals_df, bac_df, med_periods = await run_in_threadpool(
            _parse_bytes, session.raw_bytes, params.split_compounds
        )
        session.meals_df = meals_df
        session.bac_df = bac_df
        session.med_periods = med_periods

    session.hours = params.hours
    session.min_obs = params.min_obs
    session.split_compounds = params.split_compounds
    session.exclude_proteins = params.exclude_proteins
    session.episode_threshold = params.episode_threshold
    sig = _session_signature(session)

    cached = session.results.get_payload(sig)
    if cached is not None:
        store.set(session_id, session)  # persist the (possibly changed) params
        return cached

    store.set(session_id, session)
    return await _enqueue_and_accept(response, session_id, session, sig)


# ---------------------------------------------------------------------------
# Job status / cancellation
# ---------------------------------------------------------------------------
@app.get("/jobs/{job_id}")
async def job_status(job_id: str, session_id: str | None = Cookie(default=None)):
    """Job status, position and wait estimate. Doubles as the presence signal (R3)."""
    if not session_id:
        raise HTTPException(404, "Unknown job")
    job = await run_in_threadpool(queue.get, job_id, session_id)
    if job is None:
        # 404, never 403 — a 403 would confirm a foreign job exists (contracts).
        raise HTTPException(404, "Unknown job")
    # Presence (FR-013): refresh last_seen_at, but debounced so a busy poll loop does
    # not become one SQLite write per request (research R5b). Most polls are pure reads.
    if (time.time() - job.last_seen_at) >= _PRESENCE_DEBOUNCE_SECONDS:
        await run_in_threadpool(queue.touch, job_id, session_id)

    body: dict = {
        "status": job.state.value,
        "job_id": job.job_id,
        "poll_after_seconds": executor.poll_after_seconds,
    }
    if job.state == JobState.QUEUED:
        position = await run_in_threadpool(queue.position, job_id)
        if position is not None:
            session = store.get(session_id)
            size = size_class(session) if session is not None else None
            body["position"] = position
            body["estimated_wait_seconds"] = executor.estimate_wait_seconds(
                position, size
            )
    message = _STATE_MESSAGES.get(job.state)
    if message:
        body["message"] = message
    return body


@app.delete("/jobs/{job_id}")
async def cancel_job(job_id: str, session_id: str | None = Cookie(default=None)):
    """Cancel a queued job and release its place (FR-009), owner-only."""
    if not session_id:
        raise HTTPException(404, "Unknown job")
    cancelled = await run_in_threadpool(queue.cancel, job_id, session_id)
    if not cancelled:
        raise HTTPException(404, "Unknown job")
    return {"status": "cancelled", "job_id": job_id}


# ---------------------------------------------------------------------------
# Report & Prediction (template engine)
# ---------------------------------------------------------------------------
class PredictRequest(BaseModel):
    ingredients: list[str]


def _report_pending_body(response: Response, session: SessionData) -> dict:
    """`202` body for a report requested before stage one has cached it.

    The report is cached the moment stage one runs, so this is only hit while an
    analysis is still queued/running (contracts: "`202` only while no analysis exists
    yet"). Carries the active job id so the client can poll it.
    """
    response.status_code = 202
    return {
        "status": "pending",
        "job_id": session.active_job_id,
        "poll_after_seconds": executor.poll_after_seconds,
    }


@app.get("/report")
async def get_report(response: Response, session_id: str | None = Cookie(default=None)):
    """Serve the cached analysis report (200), or 202 while it is still being computed.

    The report is built and cached during stage one (research R1), so this never
    triggers analysis or training — it only reads the cache (FR-016, FR-017).
    """
    if not session_id:
        raise HTTPException(404, "No data loaded — upload a file first")
    session = store.get(session_id)
    if session is None or session.bac_df is None:
        raise HTTPException(404, "No data loaded — upload a file first")

    report = session.results.get_report(_session_signature(session))
    if report is None:
        return _report_pending_body(response, session)
    return report


@app.post("/predict")
async def predict_meal(
    req: PredictRequest,
    response: Response,
    session_id: str | None = Cookie(default=None),
):
    """Predict BAC risk for a planned meal."""
    if not session_id:
        raise HTTPException(404, "No data loaded — upload a file first")
    session = store.get(session_id)
    if session is None or session.bac_df is None:
        raise HTTPException(404, "No data loaded — upload a file first")
    if not req.ingredients:
        raise HTTPException(400, "Provide at least one ingredient")

    return predict_risk(
        req.ingredients,
        session.scores_all,
        session.lookback_df,
        session.bac_df,
    )


@app.get("/report/pdf")
async def report_pdf(
    response: Response, session_id: str | None = Cookie(default=None)
):
    """Serve the cached PDF (200), rendering it once on first request, or 202.

    The report and summary come from the stage-one cache; the PDF is rendered lazily
    from them the first time it is asked for, then cached so re-downloads replay the
    same bytes without recomputation (FR-016, FR-017).
    """
    if not session_id:
        raise HTTPException(404, "No data loaded — upload a file first")
    session = store.get(session_id)
    if session is None or session.bac_df is None:
        raise HTTPException(404, "No data loaded — upload a file first")

    sig = _session_signature(session)
    report_data = session.results.get_report(sig)
    payload = session.results.get_payload(sig)
    if report_data is None or payload is None:
        return _report_pending_body(response, session)

    pdf_bytes = session.results.get_pdf(sig)
    if pdf_bytes is None:
        pdf_bytes = bytes(
            await run_in_threadpool(generate_pdf, report_data, payload["summary"])
        )
        session.results.set_pdf(sig, pdf_bytes)
        store.set(session_id, session)  # persist the rendered PDF into the cache

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="abs_report.pdf"'},
    )


@app.get("/ingredient/{name}")
async def ingredient_detail(
    name: str,
    response: Response,
    session_id: str | None = Cookie(default=None),
):
    """Return BAC distribution split by ingredient presence."""
    import pandas as pd

    if not session_id:
        raise HTTPException(404, "No data loaded")
    session = store.get(session_id)
    if session is None or session.bac_df is None:
        raise HTTPException(404, "No data loaded")

    bac_df = session.bac_df
    lookback_df = session.lookback_df

    # Find BAC indices where this ingredient appears in the lookback
    with_indices = set()
    if lookback_df is not None and not lookback_df.empty:
        mask = lookback_df["ingredient"].str.lower() == name.lower()
        with_indices = set(lookback_df.loc[mask, "bac_idx"].astype(int))

    with_vals = []
    without_vals = []
    for idx, row in bac_df.iterrows():
        if int(idx) in with_indices:
            with_vals.append(float(row["promille"]))
        else:
            without_vals.append(float(row["promille"]))

    return {
        "ingredient": name,
        "with": with_vals,
        "without": without_vals,
        "with_count": len(with_vals),
        "without_count": len(without_vals),
        "with_mean": round(sum(with_vals) / len(with_vals), 3) if with_vals else 0,
        "without_mean": round(sum(without_vals) / len(without_vals), 3) if without_vals else 0,
    }
