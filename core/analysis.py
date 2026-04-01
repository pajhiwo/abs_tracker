"""
Analysis functions — look-back mapping and lift-score computation.
"""

import datetime

import pandas as pd


# ---------------------------------------------------------------------------
# map_lookback
# ---------------------------------------------------------------------------
def map_lookback(
    bac_df: pd.DataFrame,
    meals_df: pd.DataFrame,
    hours: float = 3,
) -> pd.DataFrame:
    """
    For each BAC reading, collect ingredients whose meal_datetime falls within
    [bac_datetime - hours, bac_datetime].
    Falls back to date-level matching (approximate=True) when meal_datetime is null.
    """
    if meals_df.empty or bac_df.empty:
        return pd.DataFrame()

    window = datetime.timedelta(hours=hours)
    records = []

    for bac_idx, bac_row in bac_df.iterrows():
        bac_dt = bac_row["bac_datetime"]
        if pd.isnull(bac_dt):
            continue
        window_start = bac_dt - window

        for _, meal_row in meals_df.iterrows():
            meal_dt = meal_row["meal_datetime"]

            if pd.notnull(meal_dt):
                if window_start <= meal_dt <= bac_dt:
                    hours_before = (bac_dt - meal_dt).total_seconds() / 3600
                    records.append(
                        {
                            "bac_idx": bac_idx,
                            "bac_datetime": bac_dt,
                            "promille": bac_row["promille"],
                            "episode": bac_row["episode"],
                            "active_medications": bac_row["active_medications"],
                            "ingredient": meal_row["ingredient"],
                            "quantity_g": meal_row["quantity_g"],
                            "meal": meal_row["meal"],
                            "meal_datetime": meal_dt,
                            "hours_before": round(hours_before, 2),
                            "approximate": False,
                        }
                    )
            else:
                meal_date = meal_row["date"]
                if pd.notnull(meal_date):
                    if (
                        pd.Timestamp(meal_date).date() >= window_start.date()
                        and pd.Timestamp(meal_date).date() <= bac_dt.date()
                    ):
                        records.append(
                            {
                                "bac_idx": bac_idx,
                                "bac_datetime": bac_dt,
                                "promille": bac_row["promille"],
                                "episode": bac_row["episode"],
                                "active_medications": bac_row["active_medications"],
                                "ingredient": meal_row["ingredient"],
                                "quantity_g": meal_row["quantity_g"],
                                "meal": meal_row["meal"],
                                "meal_datetime": None,
                                "hours_before": None,
                                "approximate": True,
                            }
                        )

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# compute_lift_scores
# ---------------------------------------------------------------------------
def compute_lift_scores(
    bac_df: pd.DataFrame,
    lookback_df: pd.DataFrame,
    min_observations: int = 3,
    period_filter: str | None = None,
) -> pd.DataFrame:
    """
    Compute lift score per ingredient.
    If period_filter is given (e.g. "none", "Rifaximin"), only BAC readings
    whose active_medications matches are used.
    """
    if lookback_df.empty:
        return pd.DataFrame()

    if period_filter is not None:
        bac_subset = bac_df[bac_df["active_medications"] == period_filter]
        lookback_subset = lookback_df[
            lookback_df["active_medications"] == period_filter
        ]
    else:
        bac_subset = bac_df
        lookback_subset = lookback_df

    if bac_subset.empty or lookback_subset.empty:
        return pd.DataFrame()

    presence = lookback_subset.groupby("ingredient")["bac_idx"].apply(set)
    approximate = lookback_subset.groupby("ingredient")["approximate"].any()
    results = []

    all_idx = set(bac_subset.index)

    for ingredient in presence.index:
        present_idx = presence[ingredient]
        absent_idx = all_idx - present_idx
        n_present = len(present_idx)
        n_absent = len(absent_idx)

        mean_bac_present = bac_subset.loc[list(present_idx), "promille"].mean()
        mean_bac_absent = (
            bac_subset.loc[list(absent_idx), "promille"].mean()
            if n_absent > 0
            else None
        )

        lift = (
            round(mean_bac_present / mean_bac_absent, 4)
            if mean_bac_absent and mean_bac_absent > 0
            else None
        )

        results.append(
            {
                "ingredient": ingredient,
                "n_present": n_present,
                "n_absent": n_absent,
                "mean_bac_present": round(mean_bac_present, 4),
                "mean_bac_absent": (
                    round(mean_bac_absent, 4) if mean_bac_absent is not None else None
                ),
                "lift": lift,
                "low_confidence": n_present < min_observations,
                "always_present": n_absent == 0,
                "has_approximate": bool(approximate.get(ingredient, False)),
            }
        )

    df = pd.DataFrame(results)
    if not df.empty:
        df = df.sort_values("lift", ascending=False, na_position="last").reset_index(
            drop=True
        )
    return df
