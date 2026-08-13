"""
ABS Tracker — FastAPI Web Application
--------------------------------------
Run with:  uvicorn app.main:app --reload
"""

import re
import tempfile
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, Cookie, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from core import parse_log
from ai import generate_report, predict_risk, detect_combinations
from app import sessions
from app.sessions import SessionData, create_store, new_session_id
from app.compute import (
    DEFAULT_EXCLUDE_PROTEINS,
    build_result_payload,
    run_analysis,
)
from report.pdf_export import generate_pdf

app = FastAPI(title="ABS Diet Tracker", version="0.1.0")

# Serve static files (HTML/CSS/JS)
STATIC_DIR = Path(__file__).parent / "static"
# index.html lives outside STATIC_DIR deliberately: it is rendered per request to
# fill in the retention window, and serving it verbatim through the /static mount
# would publish a working upload page with no retention figure on it.
TEMPLATE_DIR = Path(__file__).parent / "templates"
EXAMPLE_DIR = Path(__file__).parent.parent / "example"
app.mount("/example", StaticFiles(directory=str(EXAMPLE_DIR)), name="example")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

store = create_store()


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------
def _get_or_create_session(
    response: Response, session_id: str | None
) -> tuple[str, SessionData]:
    """Return (session_id, SessionData), creating new if needed."""
    if session_id:
        data = store.get(session_id)
        if data is not None:
            return session_id, data
    # New session
    sid = new_session_id()
    data = SessionData()
    store.set(sid, data)
    response.set_cookie("session_id", sid, httponly=True, samesite="lax")
    return sid, data


class AnalysisParams(BaseModel):
    hours: float = 3.0
    min_obs: int = 3
    split_compounds: bool = True
    exclude_proteins: bool = DEFAULT_EXCLUDE_PROTEINS
    episode_threshold: float = 2.0


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


@app.post("/upload")
async def upload_file(
    response: Response,
    file: UploadFile = File(...),
    hours: float = 3.0,
    min_obs: int = 3,
    split_compounds: bool = True,
    exclude_proteins: bool = True,
    session_id: str | None = Cookie(default=None),
):
    """Upload an Excel file, parse it, and run analysis."""
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Only .xlsx / .xls files are supported")

    sid, session = _get_or_create_session(response, session_id)
    contents = await file.read()

    session.raw_bytes = contents
    session.split_compounds = split_compounds

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    try:
        meals_df, bac_df, med_periods = parse_log(
            tmp_path, split_compounds=split_compounds
        )
    except Exception as e:
        raise HTTPException(400, f"Failed to parse file: {e}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if bac_df.empty:
        raise HTTPException(400, "No BAC readings found in the file")

    session.meals_df = meals_df
    session.bac_df = bac_df
    session.med_periods = med_periods
    session.filename = file.filename

    run_analysis(session, hours, min_obs, split_compounds, exclude_proteins)
    store.set(sid, session)
    return build_result_payload(session)


@app.get("/results")
async def get_results(
    response: Response, session_id: str | None = Cookie(default=None)
):
    """Return current analysis results."""
    if not session_id:
        raise HTTPException(404, "No data loaded — upload a file first")
    session = store.get(session_id)
    if session is None or session.bac_df is None:
        raise HTTPException(404, "No data loaded — upload a file first")
    return build_result_payload(session)


@app.post("/results")
async def recompute(
    params: AnalysisParams,
    response: Response,
    session_id: str | None = Cookie(default=None),
):
    """Recompute analysis with new parameters (without re-uploading)."""
    if not session_id:
        raise HTTPException(404, "No data loaded — upload a file first")
    session = store.get(session_id)
    if session is None or session.bac_df is None:
        raise HTTPException(404, "No data loaded — upload a file first")
    session.episode_threshold = params.episode_threshold
    run_analysis(
        session,
        params.hours,
        params.min_obs,
        params.split_compounds,
        params.exclude_proteins,
    )
    store.set(session_id, session)
    return build_result_payload(session)


# ---------------------------------------------------------------------------
# Report & Prediction (template engine)
# ---------------------------------------------------------------------------
class PredictRequest(BaseModel):
    ingredients: list[str]


@app.get("/report")
async def get_report(response: Response, session_id: str | None = Cookie(default=None)):
    """Generate a template-based analysis report."""
    if not session_id:
        raise HTTPException(404, "No data loaded — upload a file first")
    session = store.get(session_id)
    if session is None or session.bac_df is None:
        raise HTTPException(404, "No data loaded — upload a file first")

    summary = build_result_payload(session, include_ml=False)["summary"]

    report = generate_report(
        session.scores_all,
        session.scores_by_period or {},
        session.bac_df,
        session.med_periods or {},
        summary,
    )
    report["combinations"] = detect_combinations(
        session.lookback_df, session.bac_df, min_cooccurrence=3
    )
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
    """Generate and return a PDF analysis report."""
    if not session_id:
        raise HTTPException(404, "No data loaded — upload a file first")
    session = store.get(session_id)
    if session is None or session.bac_df is None:
        raise HTTPException(404, "No data loaded — upload a file first")

    summary = build_result_payload(session, include_ml=False)["summary"]

    report_data = generate_report(
        session.scores_all,
        session.scores_by_period or {},
        session.bac_df,
        session.med_periods or {},
        summary,
    )
    report_data["combinations"] = detect_combinations(
        session.lookback_df, session.bac_df, min_cooccurrence=3
    )

    pdf_bytes = generate_pdf(report_data, summary)
    return Response(
        content=bytes(pdf_bytes),
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
