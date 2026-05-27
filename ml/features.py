"""
ML feature extraction (Stage 8a).

Builds a numeric feature matrix from parsed meals, BAC readings, and
medication periods. Designed multi-user-aware from day one: every row carries
a `user_id` even when only a single user exists.

Feature groups
--------------
- Nutritional totals over the lookback window:
    total_carbs_g, total_sugars_g, total_quantity_g, n_ingredients
- Timing:
    hours_since_last_meal, hour_sin, hour_cos
- Medication:
    on_<med>, days_on_<med>
- Ingredients (one-hot for frequent only):
    ing_<canonical_name>

Returns
-------
extract_features(...) -> dict with keys:
    X         : pd.DataFrame   feature matrix (numeric, no NaN)
    y         : pd.Series      target BAC (permille)
    dates     : pd.Series      bac_datetime per row
    user_ids  : pd.Series      user_id per row
    feature_names : list[str]
    dropped_ingredients : list[str]   below min_ingredient_count
"""

from __future__ import annotations

import datetime
import math
from typing import Iterable

import pandas as pd


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _canonical_ingredient(name: str) -> str:
    """
    Light placeholder canonicalisation. Will be replaced by
    `core/ingredient_normalizer.py` in Phase 2.
    """
    if name is None:
        return ""
    return str(name).strip().lower()


def _safe_col(name: str) -> str:
    """Make a safe column suffix from an arbitrary string."""
    out = []
    for ch in str(name).strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_"):
            out.append("_")
    cleaned = "".join(out).strip("_")
    return cleaned or "unknown"


def _days_active(date: datetime.date, med: str, periods: dict) -> int:
    """Days the medication has been active up to (and including) `date`."""
    for r in periods.get(med, []):
        stop = r["stop"] or datetime.date(9999, 12, 31)
        if r["start"] <= date <= stop:
            return (date - r["start"]).days
    return 0


# ---------------------------------------------------------------------------
# main API
# ---------------------------------------------------------------------------
def extract_features(
    meals_df: pd.DataFrame,
    bac_df: pd.DataFrame,
    med_periods: dict | None = None,
    *,
    user_id: str = "local",
    lookback_hours: float = 24.0,
    min_ingredient_count: int = 5,
) -> dict:
    """
    Build per-reading feature rows.

    Parameters
    ----------
    meals_df : DataFrame with columns
        date, meal_datetime, ingredient, quantity_g, carbs_g, sugars_g
    bac_df : DataFrame with columns
        date, bac_datetime, promille, active_medications
    med_periods : dict[str, list[{start, stop}]]   (from build_medication_periods)
    user_id : tag to store on every row (multi-user ready)
    lookback_hours : window for aggregating meals before each BAC reading
    min_ingredient_count : keep only ingredients appearing in >= this many windows
    """
    med_periods = med_periods or {}

    empty = {
        "X": pd.DataFrame(),
        "y": pd.Series(dtype=float),
        "dates": pd.Series(dtype="datetime64[ns]"),
        "user_ids": pd.Series(dtype=object),
        "feature_names": [],
        "dropped_ingredients": [],
    }
    if bac_df is None or bac_df.empty:
        return empty

    meals_df = meals_df if meals_df is not None else pd.DataFrame()

    # normalise dtypes defensively
    bac_df = bac_df.copy()
    bac_df["bac_datetime"] = pd.to_datetime(bac_df["bac_datetime"])
    bac_df = bac_df.dropna(subset=["bac_datetime", "promille"]).reset_index(drop=True)

    if bac_df.empty:
        return empty

    if not meals_df.empty:
        meals_df = meals_df.copy()
        meals_df["meal_datetime"] = pd.to_datetime(
            meals_df.get("meal_datetime"), errors="coerce"
        )

    window = datetime.timedelta(hours=lookback_hours)

    # ------------------------------------------------------------------ rows
    rows: list[dict] = []
    ingredient_window_counts: dict[str, int] = {}

    all_meds = sorted(med_periods.keys())

    for _, bac in bac_df.iterrows():
        bac_dt: pd.Timestamp = bac["bac_datetime"]
        if pd.isnull(bac_dt):
            continue

        # meals strictly inside the lookback window
        if meals_df.empty:
            window_meals = meals_df
        else:
            mdt = meals_df["meal_datetime"]
            mask = mdt.notna() & (mdt <= bac_dt) & (mdt >= bac_dt - window)
            window_meals = meals_df[mask]

        # nutritional totals
        total_carbs = float(window_meals["carbs_g"].sum()) if not window_meals.empty else 0.0
        total_sugars = float(window_meals["sugars_g"].sum()) if not window_meals.empty else 0.0
        total_qty = float(window_meals["quantity_g"].sum()) if not window_meals.empty else 0.0
        n_ingredients = int(window_meals["ingredient"].notna().sum()) if not window_meals.empty else 0

        # timing
        if not window_meals.empty and window_meals["meal_datetime"].notna().any():
            last_meal = window_meals["meal_datetime"].max()
            hours_since = (bac_dt - last_meal).total_seconds() / 3600.0
        else:
            hours_since = float(lookback_hours)  # cap when unknown

        hour = bac_dt.hour + bac_dt.minute / 60.0
        hour_sin = math.sin(2 * math.pi * hour / 24.0)
        hour_cos = math.cos(2 * math.pi * hour / 24.0)

        # medications
        bac_date = bac_dt.date()
        med_features = {}
        for med in all_meds:
            active = 0
            for r in med_periods[med]:
                stop = r["stop"] or datetime.date(9999, 12, 31)
                if r["start"] <= bac_date <= stop:
                    active = 1
                    break
            med_features[f"on_{_safe_col(med)}"] = active
            med_features[f"days_on_{_safe_col(med)}"] = (
                _days_active(bac_date, med, med_periods) if active else 0
            )

        # ingredient indicators (canonical), counted per window once
        canon_present: set[str] = set()
        if not window_meals.empty:
            for raw in window_meals["ingredient"].dropna():
                canon_present.add(_canonical_ingredient(raw))
        canon_present.discard("")

        for ing in canon_present:
            ingredient_window_counts[ing] = ingredient_window_counts.get(ing, 0) + 1

        row = {
            "user_id": user_id,
            "bac_datetime": bac_dt,
            "target_promille": float(bac["promille"]),
            "total_carbs_g": total_carbs,
            "total_sugars_g": total_sugars,
            "total_quantity_g": total_qty,
            "n_ingredients": n_ingredients,
            "hours_since_last_meal": float(hours_since),
            "hour_sin": hour_sin,
            "hour_cos": hour_cos,
            **med_features,
            "_ingredients": canon_present,  # temporary, expanded below
        }
        rows.append(row)

    if not rows:
        return empty

    # ------------------------------------------------------------------ filter ingredients
    kept = sorted(
        ing for ing, c in ingredient_window_counts.items() if c >= min_ingredient_count
    )
    dropped = sorted(
        ing for ing, c in ingredient_window_counts.items() if c < min_ingredient_count
    )

    # ------------------------------------------------------------------ build DataFrame
    expanded: list[dict] = []
    for r in rows:
        present = r.pop("_ingredients")
        for ing in kept:
            r[f"ing_{_safe_col(ing)}"] = 1 if ing in present else 0
        expanded.append(r)

    df = pd.DataFrame(expanded)

    y = df["target_promille"].astype(float)
    dates = df["bac_datetime"]
    user_ids = df["user_id"]

    feature_cols = [
        c
        for c in df.columns
        if c not in ("user_id", "bac_datetime", "target_promille")
    ]
    X = df[feature_cols].astype(float).fillna(0.0)

    return {
        "X": X.reset_index(drop=True),
        "y": y.reset_index(drop=True),
        "dates": dates.reset_index(drop=True),
        "user_ids": user_ids.reset_index(drop=True),
        "feature_names": feature_cols,
        "dropped_ingredients": dropped,
    }


# ---------------------------------------------------------------------------
# convenience: stack multiple users (forward-looking helper)
# ---------------------------------------------------------------------------
def stack_user_features(parts: Iterable[dict]) -> dict:
    """
    Combine multiple `extract_features` outputs (one per user) into a single
    feature matrix. Missing columns are filled with 0. Useful once persistent
    multi-user data exists.
    """
    parts = [p for p in parts if not p["X"].empty]
    if not parts:
        return {
            "X": pd.DataFrame(),
            "y": pd.Series(dtype=float),
            "dates": pd.Series(dtype="datetime64[ns]"),
            "user_ids": pd.Series(dtype=object),
            "feature_names": [],
            "dropped_ingredients": [],
        }

    all_cols = sorted({c for p in parts for c in p["X"].columns})

    Xs = [p["X"].reindex(columns=all_cols, fill_value=0.0) for p in parts]
    X = pd.concat(Xs, axis=0, ignore_index=True)
    y = pd.concat([p["y"] for p in parts], axis=0, ignore_index=True)
    dates = pd.concat([p["dates"] for p in parts], axis=0, ignore_index=True)
    user_ids = pd.concat([p["user_ids"] for p in parts], axis=0, ignore_index=True)
    dropped = sorted({d for p in parts for d in p["dropped_ingredients"]})

    return {
        "X": X,
        "y": y,
        "dates": dates,
        "user_ids": user_ids,
        "feature_names": all_cols,
        "dropped_ingredients": dropped,
    }
