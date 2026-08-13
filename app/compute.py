"""Deterministic analysis compute, decoupled from the web layer (research R1).

This module owns the pure computation that used to live inline in `app/main.py`,
split into two stages so the request path can stage results and the result cache
can key on parameters:

- **Stage one** (`run_analysis` + `build_result_payload(..., include_ml=False)`):
  parsing-derived frames, lookback mapping, lift scores and the summary. No model
  is trained. Principle II: this deterministic core must stand on its own and must
  never depend on the optional ML block.
- **Stage two** (`compute_ml_block`): the optional personal LASSO model. A failure
  here is caught and reported in the payload's `ml` field, leaving every stage-one
  number intact.

`params_signature` produces a stable key over the five analysis parameters so the
result cache (US2) can recognise an unchanged re-request.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import pandas as pd

from core import compute_lift_scores, map_lookback, parse_log
from ml.features import extract_features
from ml.train import train_personal_model
from app.sessions import SessionData

# Reconciled default for `exclude_proteins`. The upload path and the UI checkbox
# both default to True (exclude proteins); `AnalysisParams` previously defaulted to
# False, so recompute silently re-included proteins. One source of truth now.
DEFAULT_EXCLUDE_PROTEINS = True

# Protein/meat keywords — very low in fermentable carbohydrates.
PROTEIN_KEYWORDS = {
    "chicken", "beef", "pork", "lamb", "turkey", "duck", "veal", "venison",
    "bison", "rabbit", "goat", "ham", "bacon", "sausage", "salmon", "tuna",
    "cod", "trout", "shrimp", "prawn", "crab", "lobster", "mackerel", "sardine",
    "herring", "tilapia", "halibut", "bass", "perch", "catfish", "anchovy",
    "squid", "octopus", "mussel", "clam", "oyster", "scallop", "fish", "egg",
    "eggs",
}


def _serialize_date(val):
    """Convert date/datetime/Timestamp to ISO string."""
    if val is None or (
        hasattr(val, "__class__") and val.__class__.__name__ == "NaTType"
    ):
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


def params_signature(
    hours: float,
    min_obs: int,
    split_compounds: bool,
    exclude_proteins: bool,
    episode_threshold: float,
) -> str:
    """Stable key over all five analysis parameters (resolved, not raw).

    Used by the result cache (US2, FR-016) to recognise an unchanged re-request.
    Values are normalised (floats to a fixed precision) so trivially different
    encodings of the same request collide on the same key.
    """
    payload = {
        "hours": round(float(hours), 6),
        "min_obs": int(min_obs),
        "split_compounds": bool(split_compounds),
        "exclude_proteins": bool(exclude_proteins),
        "episode_threshold": round(float(episode_threshold), 6),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def run_analysis(
    session: SessionData,
    hours: float,
    min_obs: int,
    split_compounds: bool = True,
    exclude_proteins: bool = DEFAULT_EXCLUDE_PROTEINS,
) -> None:
    """Stage one: lookback + lift scores on the session's parsed frames.

    Re-parses from `raw_bytes` only if the `split_compounds` toggle changed. Stores
    the derived frames back on the session. Trains no model.
    """
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
    if exclude_proteins and not lookback.empty:
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


def compute_ml_block(session: SessionData) -> dict | None:
    """Stage two: the optional personal model. Errors are returned, never raised.

    Principle II: this is the *optional* intelligence layer. Callers must be able to
    invoke it independently of the deterministic payload, and a failure here must not
    take the rest of the analysis down with it.
    """
    bac_df = session.bac_df
    if bac_df is None or bac_df.empty:
        return None
    try:
        feats = extract_features(
            session.meals_df if session.meals_df is not None else pd.DataFrame(),
            bac_df,
            session.med_periods or {},
            user_id=session.filename or "local",
            lookback_hours=float(session.hours) if session.hours else 24.0,
            min_ingredient_count=max(int(session.min_obs or 3), 3),
        )
        ml_block = train_personal_model(
            feats, min_readings=80, bootstrap=True, n_bootstrap=50
        )
        if ml_block is not None:
            ml_block["dropped_ingredients"] = feats.get("dropped_ingredients", [])
            ml_block["lookback_hours"] = (
                float(session.hours) if session.hours else 24.0
            )
        return ml_block
    except Exception as e:  # noqa: BLE001 — surfaced to the user, never propagated
        return {"status": "error", "message": str(e)}


def build_result_payload(session: SessionData, *, include_ml: bool = True) -> dict:
    """Build the frontend results payload.

    With `include_ml=False` this is the stage-one payload: everything the
    deterministic core produces, with `ml` set to `None`. The ML block is only
    trained when `include_ml=True`, so the summary and lift scores never depend on
    the model (Principle II; research R1).
    """
    bac_df = session.bac_df
    meals_df = session.meals_df

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

    scores_all = []
    if session.scores_all is not None and not session.scores_all.empty:
        scores_all = session.scores_all.where(
            pd.notna(session.scores_all), None
        ).to_dict("records")

    scores_by_period = {}
    if session.scores_by_period:
        for period, df in session.scores_by_period.items():
            scores_by_period[period] = df.where(pd.notna(df), None).to_dict("records")

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

    meal_carbs = []
    if meals_df is not None and not meals_df.empty and "carbs_g" in meals_df.columns:
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

    period_lifts = []
    if scores_all and session.scores_by_period and session.med_periods:
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
                        per_period.append(
                            {
                                "name": period_name,
                                "lift": round(float(lv), 2) if pd.notna(lv) else None,
                                "n": int(match.iloc[0]["n_present"]),
                            }
                        )
                    else:
                        per_period.append({"name": period_name, "lift": None, "n": 0})
                period_lifts.append({"ingredient": ing, "periods": per_period})

    ml_block = compute_ml_block(session) if include_ml else None

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
