"""
ABS Tracker — FastAPI Web Application
--------------------------------------
Run with:  uvicorn app.main:app --reload
"""

import io
import tempfile
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from core import parse_log, map_lookback, compute_lift_scores

app = FastAPI(title="ABS Diet Tracker", version="0.1.0")

# Serve static files (HTML/CSS/JS)
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# In-memory session store (single-user for now)
# ---------------------------------------------------------------------------
class SessionData:
    meals_df = None
    bac_df = None
    med_periods = None
    lookback_df = None
    scores_all = None
    scores_by_period = None
    hours = 3.0
    min_obs = 3
    split_compounds = True
    filename = None
    raw_bytes = None  # keep uploaded file for re-parse on toggle


session = SessionData()


class AnalysisParams(BaseModel):
    hours: float = 3.0
    min_obs: int = 3
    split_compounds: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _serialize_date(val):
    """Convert date/datetime/Timestamp to ISO string."""
    if val is None or (
        hasattr(val, "__class__") and val.__class__.__name__ == "NaTType"
    ):
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


def _run_analysis(hours: float, min_obs: int, split_compounds: bool = True):
    """Run lookback + lift scores on current session data.

    If split_compounds changed, re-parse from raw_bytes first.
    """
    # Re-parse if split_compounds toggle changed
    if split_compounds != session.split_compounds and session.raw_bytes is not None:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp.write(session.raw_bytes)
            tmp_path = tmp.name
        try:
            meals_df, bac_df, med_periods = parse_log(
                tmp_path, split_compounds=split_compounds
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        session.meals_df = meals_df
        session.bac_df = bac_df
        session.med_periods = med_periods
        session.split_compounds = split_compounds

    session.hours = hours
    session.min_obs = min_obs
    session.lookback_df = map_lookback(session.bac_df, session.meals_df, hours=hours)
    session.scores_all = compute_lift_scores(
        session.bac_df, session.lookback_df, min_observations=min_obs
    )
    periods_present = session.bac_df["active_medications"].unique()
    session.scores_by_period = {}
    for period in sorted(periods_present):
        s = compute_lift_scores(
            session.bac_df,
            session.lookback_df,
            min_observations=min_obs,
            period_filter=period,
        )
        if not s.empty:
            session.scores_by_period[period] = s


def _build_results_json() -> dict:
    """Build the full results payload for the frontend."""
    import pandas as pd

    bac_df = session.bac_df
    meals_df = session.meals_df

    # BAC readings
    bac_records = []
    if bac_df is not None and not bac_df.empty:
        for _, row in bac_df.iterrows():
            bac_records.append(
                {
                    "date": _serialize_date(row["date"]),
                    "bac_time": _serialize_date(row.get("bac_time")),
                    "bac_datetime": _serialize_date(row["bac_datetime"]),
                    "promille": row["promille"],
                    "episode": bool(row["episode"]),
                    "active_medications": row["active_medications"],
                    "comment": row.get("comment"),
                }
            )

    # Medication periods
    med_periods_out = {}
    if session.med_periods:
        for med, ranges in session.med_periods.items():
            med_periods_out[med] = [
                {
                    "start": _serialize_date(r["start"]),
                    "stop": _serialize_date(r["stop"]),
                }
                for r in ranges
            ]

    # Lift scores — overall
    scores_all = []
    if session.scores_all is not None and not session.scores_all.empty:
        scores_all = session.scores_all.where(
            pd.notna(session.scores_all), None
        ).to_dict("records")

    # Lift scores — per period
    scores_by_period = {}
    if session.scores_by_period:
        for period, df in session.scores_by_period.items():
            scores_by_period[period] = df.where(pd.notna(df), None).to_dict("records")

    # Summary stats
    summary = {}
    if bac_df is not None and not bac_df.empty:
        summary = {
            "total_readings": len(bac_df),
            "total_ingredients": len(meals_df) if meals_df is not None else 0,
            "date_min": _serialize_date(bac_df["date"].min()),
            "date_max": _serialize_date(bac_df["date"].max()),
            "bac_min": float(bac_df["promille"].min()),
            "bac_max": float(bac_df["promille"].max()),
            "bac_mean": round(float(bac_df["promille"].mean()), 4),
            "episodes": int(bac_df["episode"].sum()),
            "unique_ingredients": (
                int(meals_df["ingredient"].nunique()) if meals_df is not None else 0
            ),
            "lookback_pairs": (
                len(session.lookback_df) if session.lookback_df is not None else 0
            ),
        }

    return {
        "filename": session.filename,
        "hours": session.hours,
        "min_obs": session.min_obs,
        "split_compounds": session.split_compounds,
        "summary": summary,
        "bac_readings": bac_records,
        "medication_periods": med_periods_out,
        "lift_scores_overall": scores_all,
        "lift_scores_by_period": scores_by_period,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main page."""
    index_file = STATIC_DIR / "index.html"
    return HTMLResponse(content=index_file.read_text())


@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    hours: float = 3.0,
    min_obs: int = 3,
    split_compounds: bool = True,
):
    """Upload an Excel file, parse it, and run analysis."""
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Only .xlsx / .xls files are supported")

    contents = await file.read()

    # Keep raw bytes for re-parse when toggling split_compounds
    session.raw_bytes = contents
    session.split_compounds = split_compounds

    # Write to temp file so pandas can read it
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

    _run_analysis(hours, min_obs, split_compounds)
    return _build_results_json()


@app.get("/results")
async def get_results():
    """Return current analysis results."""
    if session.bac_df is None:
        raise HTTPException(404, "No data loaded — upload a file first")
    return _build_results_json()


@app.post("/results")
async def recompute(params: AnalysisParams):
    """Recompute analysis with new parameters (without re-uploading)."""
    if session.bac_df is None:
        raise HTTPException(404, "No data loaded — upload a file first")
    _run_analysis(params.hours, params.min_obs, params.split_compounds)
    return _build_results_json()
