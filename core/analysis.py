"""
Analysis functions — look-back mapping and lift-score computation.
"""

import datetime

import numpy as np
import pandas as pd

_LOOKBACK_COLUMNS = [
    "bac_idx",
    "bac_datetime",
    "promille",
    "episode",
    "active_medications",
    "ingredient",
    "quantity_g",
    "meal",
    "meal_datetime",
    "hours_before",
    "approximate",
]


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

    Vectorised (research R8a): the original was a nested `iterrows` — O(readings ×
    meals) in interpreted Python — which measured 436 seconds at twelve months (T004)
    and was the single request that froze the site. This version sorts meals once and
    uses `searchsorted` to slice the window per reading, so the cost is
    O(readings·log(meals) + matches). Equivalence against the un-vectorised output is
    pinned in `tests/test_analysis.py` (Principle V), since this shapes every
    downstream number.
    """
    if meals_df.empty or bac_df.empty:
        return pd.DataFrame()

    window = datetime.timedelta(hours=hours)

    # Only readings with a real timestamp participate (matches the original `continue`).
    bac = bac_df[bac_df["bac_datetime"].notnull()]
    if bac.empty:
        return pd.DataFrame()

    bac_idx = bac.index.to_numpy()
    bac_dt = bac["bac_datetime"].to_numpy()  # datetime64[ns]
    bac_promille = bac["promille"].to_numpy()
    bac_episode = bac["episode"].to_numpy()
    bac_meds = bac["active_medications"].to_numpy()
    window_ns = np.timedelta64(int(window.total_seconds() * 1_000_000_000), "ns")

    has_dt = meals_df["meal_datetime"].notnull()
    timed = meals_df[has_dt]
    untimed = meals_df[~has_dt]

    chunks: list[pd.DataFrame] = []

    # --- timed meals: exact window join via searchsorted on sorted meal_datetime ----
    if not timed.empty:
        timed = timed.sort_values("meal_datetime", kind="stable")
        md = timed["meal_datetime"].to_numpy()  # sorted datetime64[ns]
        m_ingredient = timed["ingredient"].to_numpy()
        m_qty = timed["quantity_g"].to_numpy()
        m_meal = timed["meal"].to_numpy()

        window_start = bac_dt - window_ns
        lo = np.searchsorted(md, window_start, side="left")
        hi = np.searchsorted(md, bac_dt, side="right")

        for i in range(len(bac_idx)):
            a, b = lo[i], hi[i]
            if b <= a:
                continue
            sl = slice(a, b)
            n = b - a
            meal_dts = md[sl]
            hours_before = np.round(
                (bac_dt[i] - meal_dts) / np.timedelta64(1, "s") / 3600.0, 2
            )
            chunks.append(
                pd.DataFrame(
                    {
                        "bac_idx": np.repeat(bac_idx[i], n),
                        "bac_datetime": np.repeat(bac_dt[i], n),
                        "promille": np.repeat(bac_promille[i], n),
                        "episode": np.repeat(bac_episode[i], n),
                        "active_medications": np.repeat(bac_meds[i], n),
                        "ingredient": m_ingredient[sl],
                        "quantity_g": m_qty[sl],
                        "meal": m_meal[sl],
                        "meal_datetime": meal_dts,
                        "hours_before": hours_before,
                        "approximate": np.repeat(False, n),
                    }
                )
            )

    # --- untimed meals: date-level fallback via searchsorted on sorted date ---------
    if not untimed.empty and untimed["date"].notnull().any():
        untimed = untimed[untimed["date"].notnull()].sort_values("date", kind="stable")
        # Compare on calendar date: normalise both sides to midnight.
        ud = untimed["date"].to_numpy().astype("datetime64[D]").astype("datetime64[ns]")
        u_ingredient = untimed["ingredient"].to_numpy()
        u_qty = untimed["quantity_g"].to_numpy()
        u_meal = untimed["meal"].to_numpy()

        win_start_day = (bac_dt - window_ns).astype("datetime64[D]").astype("datetime64[ns]")
        bac_day = bac_dt.astype("datetime64[D]").astype("datetime64[ns]")
        lo = np.searchsorted(ud, win_start_day, side="left")
        hi = np.searchsorted(ud, bac_day, side="right")

        for i in range(len(bac_idx)):
            a, b = lo[i], hi[i]
            if b <= a:
                continue
            sl = slice(a, b)
            n = b - a
            chunks.append(
                pd.DataFrame(
                    {
                        "bac_idx": np.repeat(bac_idx[i], n),
                        "bac_datetime": np.repeat(bac_dt[i], n),
                        "promille": np.repeat(bac_promille[i], n),
                        "episode": np.repeat(bac_episode[i], n),
                        "active_medications": np.repeat(bac_meds[i], n),
                        "ingredient": u_ingredient[sl],
                        "quantity_g": u_qty[sl],
                        "meal": u_meal[sl],
                        "meal_datetime": np.repeat(np.datetime64("NaT", "ns"), n),
                        "hours_before": np.repeat(np.nan, n),
                        "approximate": np.repeat(True, n),
                    }
                )
            )

    if not chunks:
        return pd.DataFrame()

    result = pd.concat(chunks, ignore_index=True)
    return result[_LOOKBACK_COLUMNS]


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
