"""
ABS Diet Tracker — CLI wrapper
-------------------------------
Usage:
    python abs_tracker.py                           # uses default path
    python abs_tracker.py my_log.xlsx               # custom path
    python abs_tracker.py my_log.xlsx --hours 4     # custom look-back window
    python abs_tracker.py --test                    # run edge-case assertions only
"""

import sys
import argparse
from pathlib import Path

from core import parse_log, map_lookback, compute_lift_scores
from core.tests import run_assertions


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
