"""
Template-based fallback engine — deterministic analysis, prediction, and
combination detection using only lift scores and lookback data.

No external dependencies (no API, no ML). This is the default engine.
"""

from __future__ import annotations

from itertools import combinations

import pandas as pd


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
LIFT_STRONG = 1.5  # lift above this → "Strong suspect"
LIFT_MODERATE = 1.0  # lift above this → "Moderate suspect"
RISK_HIGH = 1.3  # weighted lift above this → HIGH risk
RISK_LOW = 0.8  # weighted lift below this → LOW risk


# ---------------------------------------------------------------------------
# generate_report
# ---------------------------------------------------------------------------
def generate_report(
    scores_all: pd.DataFrame,
    scores_by_period: dict[str, pd.DataFrame],
    bac_df: pd.DataFrame,
    med_periods: dict,
    summary: dict,
) -> dict:
    """
    Produce a structured analysis report from pre-computed lift scores.

    Returns a dict with keys: summary_text, top_suspects, safe_ingredients,
    medication_comparison, combinations, caveats.
    """
    # --- Summary text ---
    total = summary.get("total_readings", 0)
    episodes = summary.get("episodes", 0)
    date_min = summary.get("date_min", "?")
    date_max = summary.get("date_max", "?")
    bac_mean = summary.get("bac_mean", 0)
    bac_max = summary.get("bac_max", 0)
    unique_ing = summary.get("unique_ingredients", 0)

    summary_text = (
        f"{total} BAC readings from {date_min} to {date_max}. "
        f"{episodes} episodes (BAC > 0). "
        f"Mean BAC: {bac_mean:.2f}‰, Max: {bac_max:.2f}‰. "
        f"{unique_ing} unique ingredients tracked."
    )

    # --- Top suspects (lift > 1.0, sufficient observations) ---
    top_suspects = []
    safe_ingredients = []

    if scores_all is not None and not scores_all.empty:
        for _, row in scores_all.iterrows():
            if row.get("always_present") or row.get("low_confidence"):
                continue
            lift = row.get("lift")
            if lift is None:
                continue
            entry = {
                "ingredient": row["ingredient"],
                "lift": round(lift, 2),
                "n": int(row["n_present"]),
                "mean_bac_present": round(row.get("mean_bac_present", 0), 3),
            }
            if lift > LIFT_MODERATE:
                entry["assessment"] = (
                    "Strong suspect" if lift > LIFT_STRONG else "Moderate suspect"
                )
                top_suspects.append(entry)
            else:
                safe_ingredients.append(entry)

    top_suspects.sort(key=lambda x: x["lift"], reverse=True)
    safe_ingredients.sort(key=lambda x: x["lift"])

    # --- Medication period comparison ---
    medication_comparison = []
    for period, df in scores_by_period.items():
        period_bac = bac_df[bac_df["active_medications"] == period]
        mean_bac = (
            round(float(period_bac["promille"].mean()), 3)
            if not period_bac.empty
            else None
        )
        n_readings = len(period_bac)

        suspects_in_period = []
        if df is not None and not df.empty:
            high = df[
                (df["lift"] > LIFT_MODERATE)
                & ~df["low_confidence"]
                & ~df["always_present"]
            ]
            suspects_in_period = (
                high.sort_values("lift", ascending=False).head(3)["ingredient"].tolist()
            )

        medication_comparison.append(
            {
                "period": period,
                "mean_bac": mean_bac,
                "n_readings": n_readings,
                "top_3_suspects": (
                    suspects_in_period
                    if suspects_in_period
                    else ["(insufficient data)"]
                ),
            }
        )

    # Sort: no-medication first, then by mean_bac desc
    medication_comparison.sort(
        key=lambda x: (x["period"] != "none", -(x["mean_bac"] or 0))
    )

    # --- Caveats ---
    caveats = []
    if scores_all is not None and not scores_all.empty:
        low_conf_count = int(scores_all["low_confidence"].sum())
        if low_conf_count > 0:
            caveats.append(
                f"{low_conf_count} ingredients have low confidence (fewer than min-obs observations)."
            )
        always_count = int(scores_all["always_present"].sum())
        if always_count > 0:
            caveats.append(
                f"{always_count} ingredients appear in every reading — lift cannot be calculated."
            )

    if total < 100:
        caveats.append(
            f"Dataset is small ({total} readings). Patterns may change as more data is collected."
        )
    caveats.append(
        "Correlation ≠ causation — high-carb foods may co-occur with other unmeasured triggers."
    )

    return {
        "summary_text": summary_text,
        "top_suspects": top_suspects,
        "safe_ingredients": safe_ingredients,
        "medication_comparison": medication_comparison,
        "caveats": caveats,
    }


# ---------------------------------------------------------------------------
# predict_risk
# ---------------------------------------------------------------------------
def predict_risk(
    ingredients: list[str],
    scores_all: pd.DataFrame,
    lookback_df: pd.DataFrame | None = None,
    bac_df: pd.DataFrame | None = None,
) -> dict:
    """
    Predict BAC risk for a planned meal based on ingredient lift scores.

    Returns dict with: risk_level, weighted_lift, ingredient_details, reasoning.
    """
    if scores_all is None or scores_all.empty:
        return {
            "risk_level": "UNKNOWN",
            "weighted_lift": None,
            "ingredient_details": [],
            "reasoning": "No analysis data available. Upload a file first.",
        }

    # Build a quick lookup: ingredient (title-cased) → lift
    lift_lookup = {}
    for _, row in scores_all.iterrows():
        lift_lookup[row["ingredient"].lower()] = {
            "lift": row.get("lift"),
            "n": int(row["n_present"]),
            "low_confidence": bool(row.get("low_confidence", False)),
        }

    details = []
    known_lifts = []

    for ing in ingredients:
        ing_clean = ing.strip().title()
        ing_lower = ing_clean.lower()
        info = lift_lookup.get(ing_lower)
        if info and info["lift"] is not None:
            details.append(
                {
                    "ingredient": ing_clean,
                    "lift": round(info["lift"], 2),
                    "n": info["n"],
                    "known": True,
                    "low_confidence": info["low_confidence"],
                }
            )
            known_lifts.append(info["lift"])
        else:
            details.append(
                {
                    "ingredient": ing_clean,
                    "lift": None,
                    "n": 0,
                    "known": False,
                    "low_confidence": False,
                }
            )

    # Weighted average lift (equal weights for now — all known ingredients)
    if known_lifts:
        weighted_lift = round(sum(known_lifts) / len(known_lifts), 2)
    else:
        weighted_lift = None

    # Risk level
    if weighted_lift is None:
        risk_level = "UNKNOWN"
        reasoning = "None of these ingredients have been seen in your data yet."
    elif weighted_lift > RISK_HIGH:
        risk_level = "HIGH"
        top = max(details, key=lambda d: d["lift"] or 0)
        reasoning = (
            f"Weighted lift is {weighted_lift:.2f} (above {RISK_HIGH} threshold). "
            f"{top['ingredient']} (lift {top['lift']}) is the dominant risk factor."
        )
    elif weighted_lift > RISK_LOW:
        risk_level = "MEDIUM"
        reasoning = (
            f"Weighted lift is {weighted_lift:.2f} — moderate risk. "
            f"Some ingredients have elevated lift scores."
        )
    else:
        risk_level = "LOW"
        reasoning = (
            f"Weighted lift is {weighted_lift:.2f} (below {RISK_LOW} threshold). "
            f"These ingredients are generally associated with lower BAC."
        )

    # Sort details: highest lift first
    details.sort(key=lambda d: -(d["lift"] or 0))

    return {
        "risk_level": risk_level,
        "weighted_lift": weighted_lift,
        "ingredient_details": details,
        "reasoning": reasoning,
    }


# ---------------------------------------------------------------------------
# detect_combinations
# ---------------------------------------------------------------------------
def detect_combinations(
    lookback_df: pd.DataFrame,
    bac_df: pd.DataFrame,
    min_cooccurrence: int = 3,
) -> list[dict]:
    """
    Find ingredient pairs that co-occur in lookback windows and compute
    their "pair lift" (mean BAC when both present / overall mean BAC).

    Returns list of dicts sorted by pair_lift descending.
    """
    if lookback_df is None or lookback_df.empty or bac_df is None or bac_df.empty:
        return []

    overall_mean = bac_df["promille"].mean()
    if overall_mean <= 0:
        return []

    # Group ingredients by BAC reading
    grouped = lookback_df.groupby("bac_idx")["ingredient"].apply(set)

    # Count co-occurrences for each pair
    pair_readings: dict[tuple[str, str], list[int]] = {}
    for bac_idx, ingredients in grouped.items():
        ing_list = sorted(ingredients)
        for a, b in combinations(ing_list, 2):
            pair = (a, b)
            pair_readings.setdefault(pair, []).append(bac_idx)

    # Filter by min_cooccurrence and compute pair lift
    results = []
    for (a, b), bac_indices in pair_readings.items():
        if len(bac_indices) < min_cooccurrence:
            continue
        mean_bac_both = float(
            bac_df.loc[bac_df.index.isin(bac_indices), "promille"].mean()
        )
        pair_lift = round(mean_bac_both / overall_mean, 2) if overall_mean > 0 else None

        results.append(
            {
                "pair": [a, b],
                "count": len(bac_indices),
                "mean_bac": round(mean_bac_both, 3),
                "pair_lift": pair_lift,
            }
        )

    results.sort(key=lambda x: -(x["pair_lift"] or 0))
    return results
