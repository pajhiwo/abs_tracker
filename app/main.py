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

from core import parse_log, map_lookback, compute_lift_scores
from ai import generate_report, predict_risk, detect_combinations
from app import sessions
from app.sessions import SessionData, create_store, new_session_id
from report.pdf_export import generate_pdf
from ml.features import extract_features
from ml.train import train_personal_model

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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Protein/meat keywords — very low in fermentable carbohydrates.
PROTEIN_KEYWORDS = {
    "chicken",
    "beef",
    "pork",
    "lamb",
    "turkey",
    "duck",
    "veal",
    "venison",
    "bison",
    "rabbit",
    "goat",
    "ham",
    "bacon",
    "sausage",
    "salmon",
    "tuna",
    "cod",
    "trout",
    "shrimp",
    "prawn",
    "crab",
    "lobster",
    "mackerel",
    "sardine",
    "herring",
    "tilapia",
    "halibut",
    "bass",
    "perch",
    "catfish",
    "anchovy",
    "squid",
    "octopus",
    "mussel",
    "clam",
    "oyster",
    "scallop",
    "fish",
    "egg",
    "eggs",
}


class AnalysisParams(BaseModel):
    hours: float = 3.0
    min_obs: int = 3
    split_compounds: bool = True
    exclude_proteins: bool = False
    episode_threshold: float = 2.0


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


def _run_analysis(
    session: SessionData,
    hours: float,
    min_obs: int,
    split_compounds: bool = True,
    exclude_proteins: bool = False,
):
    """Run lookback + lift scores on current session data.

    If split_compounds changed, re-parse from raw_bytes first.
    """
    # Re-parse if split_compounds toggle changed
    if split_compounds != session.split_compounds and session.raw_bytes is not None:
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
    session.exclude_proteins = exclude_proteins
    lookback = map_lookback(session.bac_df, session.meals_df, hours=hours)
    if exclude_proteins:
        mask = (
            lookback["ingredient"]
            .str.lower()
            .apply(lambda x: not any(kw in x for kw in PROTEIN_KEYWORDS))
        )
        lookback = lookback[mask]
    session.lookback_df = lookback
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


def _build_results_json(session: SessionData) -> dict:
    """Build the full results payload for the frontend."""
    import pandas as pd

    bac_df = session.bac_df
    meals_df = session.meals_df

    # BAC readings
    bac_records = []
    if bac_df is not None and not bac_df.empty:
        for idx, row in bac_df.iterrows():
            bac_records.append(
                {
                    "bac_idx": int(idx),
                    "date": _serialize_date(row["date"]),
                    "bac_time": _serialize_date(row.get("bac_time")),
                    "bac_datetime": _serialize_date(row["bac_datetime"]),
                    "promille": row["promille"],
                    "episode": bool(row["promille"] >= session.episode_threshold),
                    "active_medications": row["active_medications"],
                    "comment": (
                        row.get("comment") if pd.notna(row.get("comment")) else None
                    ),
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
            "episodes": int((bac_df["promille"] >= session.episode_threshold).sum()),
            "unique_ingredients": (
                int(meals_df["ingredient"].nunique()) if meals_df is not None else 0
            ),
            "lookback_pairs": (
                len(session.lookback_df) if session.lookback_df is not None else 0
            ),
        }

    # Lookback ingredients grouped by BAC reading index
    lookback_by_reading = {}
    if session.lookback_df is not None and not session.lookback_df.empty:
        for _, lrow in session.lookback_df.iterrows():
            bac_idx = int(lrow["bac_idx"])
            lookback_by_reading.setdefault(bac_idx, []).append(
                {
                    "ingredient": lrow["ingredient"],
                    "meal": lrow["meal"],
                    "hours_before": lrow["hours_before"],
                    "approximate": bool(lrow["approximate"]),
                }
            )

    # Carbs per meal for timeline chart (grouped by meal_datetime)
    meal_carbs = []
    if meals_df is not None and not meals_df.empty and "carbs_g" in meals_df.columns:
        # Group by meal_datetime (or date+meal if no time)
        group_col = "meal_datetime" if "meal_datetime" in meals_df.columns else "date"
        valid = meals_df[meals_df[group_col].notna()]
        if not valid.empty:
            grouped = (
                valid.groupby([group_col, "meal"])
                .agg(
                    carbs_g=("carbs_g", "sum"),
                    sugars_g=("sugars_g", "sum"),
                )
                .reset_index()
            )
            for _, row in grouped.iterrows():
                meal_carbs.append(
                    {
                        "datetime": _serialize_date(row[group_col]),
                        "meal": row["meal"],
                        "carbs_g": (
                            round(float(row["carbs_g"]), 1)
                            if pd.notna(row["carbs_g"])
                            else 0
                        ),
                        "sugars_g": (
                            round(float(row["sugars_g"]), 1)
                            if pd.notna(row["sugars_g"])
                            else 0
                        ),
                    }
                )

    # Period lift comparison — top 10 suspects across med periods
    period_lifts = []
    if (
        scores_all
        and session.scores_by_period
        and session.med_periods
    ):
        # Get top suspects from overall scores
        overall_df = session.scores_all
        if overall_df is not None and not overall_df.empty:
            suspects = (
                overall_df[
                    (overall_df["lift"] > 1.0)
                    & ~overall_df["low_confidence"]
                    & ~overall_df["always_present"]
                ]
                .sort_values("lift", ascending=False)
                .head(10)
            )
            for _, srow in suspects.iterrows():
                ing = srow["ingredient"]
                per_period = []
                for period_name, period_df in session.scores_by_period.items():
                    match = period_df[period_df["ingredient"] == ing]
                    if not match.empty:
                        lv = match.iloc[0]["lift"]
                        per_period.append({
                            "name": period_name,
                            "lift": round(float(lv), 2) if pd.notna(lv) else None,
                            "n": int(match.iloc[0]["n_present"]),
                        })
                    else:
                        per_period.append({"name": period_name, "lift": None, "n": 0})
                period_lifts.append({"ingredient": ing, "periods": per_period})

    # Personal ML model (Stage 8a/8b) — multi-user-aware
    ml_block: dict | None = None
    if bac_df is not None and not bac_df.empty:
        try:
            feats = extract_features(
                meals_df if meals_df is not None else pd.DataFrame(),
                bac_df,
                session.med_periods or {},
                user_id=session.filename or "local",
                lookback_hours=float(session.hours) if session.hours else 24.0,
                min_ingredient_count=max(int(session.min_obs or 3), 3),
            )
            ml_block = train_personal_model(
                feats,
                min_readings=80,
                bootstrap=True,
                n_bootstrap=50,
            )
            if ml_block is not None:
                ml_block["dropped_ingredients"] = feats.get("dropped_ingredients", [])
                ml_block["lookback_hours"] = float(session.hours) if session.hours else 24.0
        except Exception as e:
            ml_block = {"status": "error", "message": str(e)}

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
        "lookback_by_reading": lookback_by_reading,
        "meal_carbs": meal_carbs,
        "period_lifts": period_lifts,
        "ml": ml_block,
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

    _run_analysis(session, hours, min_obs, split_compounds, exclude_proteins)
    store.set(sid, session)
    return _build_results_json(session)


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
    return _build_results_json(session)


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
    _run_analysis(
        session,
        params.hours,
        params.min_obs,
        params.split_compounds,
        params.exclude_proteins,
    )
    store.set(session_id, session)
    return _build_results_json(session)


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

    summary = _build_results_json(session)["summary"]

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

    summary = _build_results_json(session)["summary"]

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
