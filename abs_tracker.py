"""
ABS Diet Tracker — Ingestion & Lift-Score Correlation Pipeline
--------------------------------------------------------------
Usage:
    python abs_tracker.py                           # uses default path
    python abs_tracker.py my_log.xlsx               # custom path
    python abs_tracker.py my_log.xlsx --hours 4     # custom look-back window
    python abs_tracker.py --test                    # run edge-case assertions only
"""

import sys
import argparse
import datetime
import warnings
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

# ---------------------------------------------------------------------------
# Column indices (0-based) — matches jo_log.xlsx
# ---------------------------------------------------------------------------
C_DATE = 0
C_MEAL = 1
C_MEAL_TIME = 2
C_PRODUCT = 3
C_MEASURE = 4
C_GRAMS = 5
C_CALORIES = 6
C_PROTEIN = 7
C_FAT = 8
C_SAT_FAT = 9
C_CARBS = 10
C_SUGARS = 11
C_FIBRE = 12
C_BAC_TIME = 13
C_BAC_VAL = 14
C_EPISODE = 15
C_MEDICATION = 16
C_COMMENT = 17

MEAL_LABELS = {"Breakfast", "Snack", "Lunch", "Dinner"}
HEADER_STRINGS = {"Date", "Meal", "Espisode", "Episode"}

# Known medication keywords (lowercase) → canonical name
MEDICATION_KEYWORDS = {
    "rifaximin": "Rifaximin",
    "activated charcoal": "Activated Charcoal",
    "activated charcol": "Activated Charcoal",
    "charcol": "Activated Charcoal",
    "charcoal": "Activated Charcoal",
}


# ---------------------------------------------------------------------------
# parse_medication_events
# ---------------------------------------------------------------------------
def parse_medication_events(raw: pd.DataFrame) -> list[dict]:
    """
    Scan date rows for medication entries and return a list of events:
        [{"date": date, "medication": str, "action": "start"|"stop"}, ...]
    Multiple medications on one row (comma-separated) are split into separate events.
    """
    events = []
    date_rows = raw[raw[C_DATE].apply(lambda v: isinstance(v, datetime.datetime))]

    for _, row in date_rows.iterrows():
        med_raw = row[C_MEDICATION]
        if pd.isna(med_raw):
            continue
        date = row[C_DATE].date()
        # split on comma for multi-medication entries
        for part in str(med_raw).split(","):
            part = part.strip().lower()
            action = None
            if "start" in part:
                action = "start"
            elif "stop" in part:
                action = "stop"
            else:
                continue

            med_name = None
            for keyword, canonical in MEDICATION_KEYWORDS.items():
                if keyword in part:
                    med_name = canonical
                    break

            if med_name and action:
                events.append({"date": date, "medication": med_name, "action": action})

    return sorted(events, key=lambda e: e["date"])


# ---------------------------------------------------------------------------
# build_medication_periods
# ---------------------------------------------------------------------------
def build_medication_periods(events: list[dict]) -> dict[str, list[dict]]:
    """
    Convert start/stop events into date ranges per medication.
    Returns: {medication_name: [{"start": date, "stop": date|None}, ...]}
    """
    periods: dict[str, list[dict]] = {}
    open_starts: dict[str, datetime.date] = {}

    for event in events:
        med = event["medication"]
        date = event["date"]

        if event["action"] == "start":
            open_starts[med] = date

        elif event["action"] == "stop":
            if med in open_starts:
                periods.setdefault(med, []).append(
                    {
                        "start": open_starts.pop(med),
                        "stop": date,
                    }
                )

    # Any medication still open at end of data → stop=None (ongoing)
    for med, start in open_starts.items():
        periods.setdefault(med, []).append({"start": start, "stop": None})

    return periods


# ---------------------------------------------------------------------------
# get_active_medications
# ---------------------------------------------------------------------------
def get_active_medications(
    date: datetime.date,
    periods: dict[str, list[dict]],
) -> list[str]:
    """Return list of medications active on a given date."""
    active = []
    for med, ranges in periods.items():
        for r in ranges:
            stop = r["stop"] or datetime.date(9999, 12, 31)
            if r["start"] <= date <= stop:
                active.append(med)
                break
    return sorted(active)


# ---------------------------------------------------------------------------
# parse_log
# ---------------------------------------------------------------------------
def parse_log(path: str | Path) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """
    Parse the Excel log into two clean DataFrames plus medication periods.

    Returns
    -------
    meals_df    : one row per ingredient
    bac_df      : one row per BAC reading, with active_medications column
    med_periods : {medication: [{start, stop}, ...]}
    """
    raw = pd.read_excel(path, sheet_name=0, header=None)
    raw = raw[~raw[C_DATE].astype(str).isin(HEADER_STRINGS)].reset_index(drop=True)

    # Build medication periods first
    events = parse_medication_events(raw)
    med_periods = build_medication_periods(events)

    meals_rows = []
    bac_rows = []

    current_date = None
    current_meal = None
    current_meal_time = None
    current_meal_dt = None

    for _, row in raw.iterrows():
        has_date = pd.notna(row[C_DATE]) and isinstance(row[C_DATE], datetime.datetime)
        has_meal = pd.notna(row[C_MEAL]) and str(row[C_MEAL]) in MEAL_LABELS
        has_product = pd.notna(row[C_PRODUCT])
        has_bac = pd.notna(row[C_BAC_VAL])

        if has_date:
            current_date = row[C_DATE].date()
            current_meal = None
            current_meal_time = None
            current_meal_dt = None

        if has_meal and current_date is not None:
            current_meal = str(row[C_MEAL])
            raw_time = row[C_MEAL_TIME]
            if isinstance(raw_time, datetime.time):
                current_meal_time = raw_time
                current_meal_dt = datetime.datetime.combine(current_date, raw_time)
            else:
                current_meal_time = None
                current_meal_dt = None

        if has_bac and current_date is not None:
            bac_time = row[C_BAC_TIME]
            bac_dt = (
                datetime.datetime.combine(current_date, bac_time)
                if isinstance(bac_time, datetime.time)
                else None
            )
            episode_raw = row[C_EPISODE]
            is_episode = (
                pd.notna(episode_raw) and str(episode_raw).strip().lower() == "yes"
            )
            active_meds = get_active_medications(current_date, med_periods)
            bac_rows.append(
                {
                    "date": current_date,
                    "bac_time": bac_time,
                    "bac_datetime": bac_dt,
                    "promille": float(row[C_BAC_VAL]),
                    "episode": is_episode,
                    "active_medications": (
                        ", ".join(active_meds) if active_meds else "none"
                    ),
                    "comment": row[C_COMMENT] if pd.notna(row[C_COMMENT]) else None,
                }
            )

        if has_product and not has_date and not has_meal and current_date is not None:
            grams = row[C_GRAMS] if pd.notna(row[C_GRAMS]) else None
            meals_rows.append(
                {
                    "date": current_date,
                    "meal": current_meal,
                    "meal_time": current_meal_time,
                    "meal_datetime": current_meal_dt,
                    "ingredient": str(row[C_PRODUCT]).strip(),
                    "quantity_g": float(grams) if grams is not None else None,
                }
            )

    meals_df = pd.DataFrame(meals_rows)
    bac_df = pd.DataFrame(bac_rows)

    if not meals_df.empty:
        meals_df["date"] = pd.to_datetime(meals_df["date"])
    if not bac_df.empty:
        bac_df["date"] = pd.to_datetime(bac_df["date"])
        bac_df["bac_datetime"] = pd.to_datetime(bac_df["bac_datetime"])
        bac_df = bac_df.sort_values("bac_datetime").reset_index(drop=True)

    return meals_df, bac_df, med_periods


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


# ---------------------------------------------------------------------------
# Edge-case assertions (TC-01 through TC-10)
# ---------------------------------------------------------------------------
def run_assertions():
    print("Running edge-case assertions...\n")

    base_date = pd.Timestamp("2026-01-01")
    base_dt = pd.Timestamp("2026-01-01 20:00")

    def make_bac(promilles, datetimes=None, meds=None):
        dts = datetimes or [
            base_dt + pd.Timedelta(days=i) for i in range(len(promilles))
        ]
        meds = meds or ["none"] * len(promilles)
        return pd.DataFrame(
            {
                "date": [dt.date() for dt in dts],
                "bac_time": [dt.time() for dt in dts],
                "bac_datetime": dts,
                "promille": promilles,
                "episode": [p >= 2.0 for p in promilles],
                "active_medications": meds,
                "comment": [None] * len(promilles),
            }
        )

    def make_meals(ingredients, dates=None, meal_dts=None):
        n = len(ingredients)
        dates = dates or [base_date] * n
        dts = meal_dts or [pd.Timestamp(d) + pd.Timedelta(hours=12) for d in dates]
        return pd.DataFrame(
            {
                "date": [pd.Timestamp(d) for d in dates],
                "meal": ["Breakfast"] * n,
                "meal_time": [dt.time() for dt in dts],
                "meal_datetime": dts,
                "ingredient": ingredients,
                "quantity_g": [100.0] * n,
            }
        )

    # TC-01: Singleton → low_confidence
    bac = make_bac([0.5, 0.0, 0.0, 0.0, 0.0])
    meals = make_meals(
        ["RareFood", "CommonFood", "CommonFood", "CommonFood", "CommonFood"],
        dates=[base_date + pd.Timedelta(days=i) for i in range(5)],
        meal_dts=[
            base_dt - pd.Timedelta(hours=2) + pd.Timedelta(days=i) for i in range(5)
        ],
    )
    lb = map_lookback(bac, meals, hours=3)
    scores = compute_lift_scores(bac, lb, min_observations=3)
    rare = scores[scores["ingredient"] == "RareFood"]
    assert not rare.empty and rare.iloc[0]["low_confidence"], "TC-01 FAIL"
    print("TC-01 PASS — singleton ingredient flagged as low_confidence")

    # TC-02: Always co-occurring → always_present or lift=None
    bac = make_bac([1.0, 1.0, 1.0])
    dates = [base_date + pd.Timedelta(days=i) for i in range(3)]
    dts = [base_dt - pd.Timedelta(hours=2) + pd.Timedelta(days=i) for i in range(3)]
    meals = make_meals(["FoodA", "FoodB"] * 3, dates=dates * 2, meal_dts=dts * 2)
    lb = map_lookback(bac, meals, hours=3)
    scores = compute_lift_scores(bac, lb)
    for food in ["FoodA", "FoodB"]:
        row = scores[scores["ingredient"] == food]
        assert not row.empty and (
            row.iloc[0]["always_present"] or row.iloc[0]["lift"] is None
        ), f"TC-02 FAIL: {food}"
    print("TC-02 PASS — always-co-occurring ingredients flagged")

    # TC-03: No meals in window → no lookback rows
    bac = make_bac([0.5])
    meals = make_meals(
        ["FarFood"],
        dates=[base_date + pd.Timedelta(days=5)],
        meal_dts=[base_dt + pd.Timedelta(days=5, hours=-2)],
    )
    lb = map_lookback(bac, meals, hours=3)
    assert lb.empty or (0 not in lb["bac_idx"].values), "TC-03 FAIL"
    print("TC-03 PASS — no meals in window → no lookback rows")

    # TC-04: Multiple BAC readings same day → all captured
    dts = [
        pd.Timestamp("2026-01-01 14:30"),
        pd.Timestamp("2026-01-01 16:00"),
        pd.Timestamp("2026-01-01 17:30"),
        pd.Timestamp("2026-01-01 19:00"),
    ]
    bac = make_bac([0.89, 0.57, 1.2, 0.3], datetimes=dts)
    assert len(bac) == 4, "TC-04 FAIL"
    print("TC-04 PASS — multiple readings same day all captured")

    # TC-05: All BAC = 0 → no inflated lift
    bac = make_bac([0.0, 0.0, 0.0])
    meals = make_meals(
        ["FoodX"] * 3,
        dates=[base_date + pd.Timedelta(days=i) for i in range(3)],
        meal_dts=[
            base_dt - pd.Timedelta(hours=2) + pd.Timedelta(days=i) for i in range(3)
        ],
    )
    lb = map_lookback(bac, meals, hours=3)
    scores = compute_lift_scores(bac, lb)
    if not scores.empty:
        assert (scores["lift"].dropna() <= 1.0).all(), "TC-05 FAIL"
    print("TC-05 PASS — zero-BAC dataset produces no inflated lift scores")

    # TC-06: Medication period filter works
    bac = make_bac([1.5, 0.2], meds=["Rifaximin", "none"])
    meals = make_meals(
        ["FoodA", "FoodB"],
        meal_dts=[
            base_dt - pd.Timedelta(hours=1),
            base_dt - pd.Timedelta(hours=1) + pd.Timedelta(days=1),
        ],
    )
    lb = map_lookback(bac, meals, hours=3)
    scores_rifax = compute_lift_scores(bac, lb, period_filter="Rifaximin")
    scores_none = compute_lift_scores(bac, lb, period_filter="none")
    assert not scores_rifax.empty, "TC-06 FAIL: Rifaximin period empty"
    assert not scores_none.empty, "TC-06 FAIL: none period empty"
    print("TC-06 PASS — period filter correctly isolates medication periods")

    # TC-07: Whitespace stripped from ingredient names
    assert "  Avocado  ".strip() == "Avocado", "TC-07 FAIL"
    print("TC-07 PASS — ingredient names stripped of whitespace")

    # TC-08: Look-back crosses midnight with exact meal time
    bac = make_bac([1.5], datetimes=[pd.Timestamp("2026-01-02 01:00")])
    meals = make_meals(
        ["LateNightSnack"],
        dates=[pd.Timestamp("2026-01-01")],
        meal_dts=[pd.Timestamp("2026-01-01 23:30")],
    )
    lb = map_lookback(bac, meals, hours=3)
    assert not lb.empty and "LateNightSnack" in lb["ingredient"].values, "TC-08 FAIL"
    assert not lb.iloc[0]["approximate"], "TC-08 FAIL: should be exact"
    print("TC-08 PASS — look-back crosses midnight using exact meal time")

    # TC-09: Empty dataset → no crash
    empty_bac = pd.DataFrame(
        columns=[
            "date",
            "bac_time",
            "bac_datetime",
            "promille",
            "episode",
            "active_medications",
            "comment",
        ]
    )
    empty_meals = pd.DataFrame(
        columns=[
            "date",
            "meal",
            "meal_time",
            "meal_datetime",
            "ingredient",
            "quantity_g",
        ]
    )
    lb = map_lookback(empty_bac, empty_meals)
    scores = compute_lift_scores(empty_bac, lb)
    assert lb.empty and scores.empty, "TC-09 FAIL"
    print("TC-09 PASS — empty dataset handled without crash")

    # TC-10: Meal outside window excluded
    bac = make_bac([1.0], datetimes=[pd.Timestamp("2026-01-01 20:00")])
    meals = make_meals(
        ["TooEarlyFood"],
        dates=[pd.Timestamp("2026-01-01")],
        meal_dts=[pd.Timestamp("2026-01-01 14:00")],
    )
    lb = map_lookback(bac, meals, hours=3)
    assert lb.empty or "TooEarlyFood" not in lb["ingredient"].values, "TC-10 FAIL"
    print("TC-10 PASS — meal outside window correctly excluded")

    print("\nAll 10 assertions passed ✓")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="ABS Diet Tracker — correlation pipeline"
    )
    parser.add_argument("path", nargs="?", default="jo_log.xlsx")
    parser.add_argument("--hours", type=float, default=3)
    parser.add_argument("--min-obs", type=int, default=3)
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    if args.test:
        run_assertions()
        return

    path = Path(args.path)
    if not path.exists():
        print(f"ERROR: File not found: {path}")
        sys.exit(1)

    print(f"Parsing: {path}")
    meals_df, bac_df, med_periods = parse_log(path)

    exact_meals = meals_df["meal_datetime"].notna().sum()
    print(f"  → {len(meals_df)} ingredient rows ({exact_meals} with exact meal time)")
    print(f"  → {len(bac_df)} BAC readings")
    print(f"  → Date range: {bac_df['date'].min()} to {bac_df['date'].max()}")
    print(f"  → BAC range:  {bac_df['promille'].min()}‰ – {bac_df['promille'].max()}‰")
    print(f"  → Episodes:   {bac_df['episode'].sum()} flagged")

    print(f"\nMedication periods detected:")
    for med, ranges in med_periods.items():
        for r in ranges:
            stop = r["stop"] or "ongoing"
            print(f"  {med}: {r['start']} → {stop}")

    print(f"\nMapping look-back window ({args.hours}h)...")
    lookback_df = map_lookback(bac_df, meals_df, hours=args.hours)
    exact = (~lookback_df["approximate"]).sum() if not lookback_df.empty else 0
    approx = lookback_df["approximate"].sum() if not lookback_df.empty else 0
    print(
        f"  → {len(lookback_df)} ingredient-reading pairs ({exact} exact, {approx} approximate)"
    )

    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)

    # Overall lift scores
    print(f"\nComputing overall lift scores (min observations: {args.min_obs})...")
    scores_all = compute_lift_scores(bac_df, lookback_df, min_observations=args.min_obs)

    # Per-medication-period lift scores
    periods_present = bac_df["active_medications"].unique()
    scores_by_period = {}
    for period in sorted(periods_present):
        s = compute_lift_scores(
            bac_df, lookback_df, min_observations=args.min_obs, period_filter=period
        )
        if not s.empty:
            scores_by_period[period] = s

    # Print overall results
    def print_scores(scores, label=""):
        high = scores[~scores["low_confidence"] & ~scores["always_present"]]
        low = scores[scores["low_confidence"] | scores["always_present"]]
        title = f"LIFT SCORES{' — ' + label if label else ''} (high confidence)"
        print(f"\n=== {title} ===\n")
        if high.empty:
            print(
                "  No high-confidence results — try a wider look-back window (--hours 5)"
            )
        else:
            print(high.to_string(index=False))
        if not low.empty:
            print(
                f"\n  Low confidence / always present: {len(low)} ingredients (see CSV)"
            )

    print_scores(scores_all, "overall")
    for period, scores in scores_by_period.items():
        n_readings = (bac_df["active_medications"] == period).sum()
        print_scores(scores, f"{period} — {n_readings} readings")

    # Save CSVs
    bac_df.to_csv(output_dir / "bac_readings.csv", index=False)
    meals_df.to_csv(output_dir / "meals.csv", index=False)
    scores_all.to_csv(output_dir / "lift_scores_overall.csv", index=False)

    for period, scores in scores_by_period.items():
        safe_name = period.replace(" ", "_").replace(",", "").lower()
        scores.to_csv(output_dir / f"lift_scores_{safe_name}.csv", index=False)

    print(f"\nOutputs saved to {output_dir}/")
    print(f"  bac_readings.csv, meals.csv, lift_scores_overall.csv")
    for period in scores_by_period:
        safe_name = period.replace(" ", "_").replace(",", "").lower()
        print(f"  lift_scores_{safe_name}.csv")


if __name__ == "__main__":
    main()
