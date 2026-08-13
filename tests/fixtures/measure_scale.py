"""Measure per-stage wall time and per-field retained size (research R8/R8a).

This is the evidence T004 needs before the executor is chosen. It answers the one
question the plan is blocked on: does the nested-`iterrows` `map_lookback` dominate,
or does LASSO training? The answer decides thread pool vs process pool, and whether
`map_lookback` is worth vectorising.

Run directly for a table:
    uv run python tests/fixtures/measure_scale.py --months 1 3 12 24

Or under cProfile to see where time actually goes at one size:
    uv run python -m cProfile -s cumtime tests/fixtures/measure_scale.py --months 12
"""

from __future__ import annotations

import argparse
import gc
import pickle
import sys
import time
import tracemalloc
from pathlib import Path

# Allow `python tests/fixtures/measure_scale.py` (quickstart form) by putting the
# repo root on the path; pytest already does this via pyproject's pythonpath.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core import parse_log, map_lookback, compute_lift_scores  # noqa: E402
from ai import generate_report, detect_combinations
from ml.features import extract_features
from ml.train import train_personal_model
from report.pdf_export import generate_pdf

from tests.fixtures.generate_year_log import generate


def _time(label: str, fn):
    """Run fn(), returning (result, elapsed_seconds)."""
    gc.collect()
    start = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - start


def _size_bytes(obj) -> int:
    if obj is None:
        return 0
    try:
        return len(pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL))
    except Exception:
        return -1


def measure_one(months: float, workdir: Path) -> dict:
    """Measure a single fixture size end to end."""
    path = generate(months, workdir / f"scale_{months}.xlsx")

    timings: dict[str, float] = {}
    tracemalloc.start()

    (meals_df, bac_df, med_periods), timings["parse"] = _time(
        "parse", lambda: parse_log(str(path))
    )
    lookback_df, timings["map_lookback"] = _time(
        "map_lookback", lambda: map_lookback(bac_df, meals_df, hours=3.0)
    )
    scores_all, timings["compute_lift_scores"] = _time(
        "compute_lift_scores",
        lambda: compute_lift_scores(bac_df, lookback_df, min_observations=3),
    )

    def _by_period():
        out = {}
        for period in sorted(bac_df["active_medications"].unique()):
            s = compute_lift_scores(
                bac_df, lookback_df, min_observations=3, period_filter=period
            )
            if not s.empty:
                out[period] = s
        return out

    scores_by_period, timings["lift_scores_by_period"] = _time(
        "lift_scores_by_period", _by_period
    )

    feats, timings["extract_features"] = _time(
        "extract_features",
        lambda: extract_features(
            meals_df, bac_df, med_periods or {},
            user_id="measure", lookback_hours=3.0, min_ingredient_count=3,
        ),
    )
    ml_block, timings["train_personal_model"] = _time(
        "train_personal_model",
        lambda: train_personal_model(feats, min_readings=80, bootstrap=True, n_bootstrap=50),
    )

    summary = {
        "total_readings": len(bac_df),
        "total_ingredients": len(meals_df),
        "unique_ingredients": int(meals_df["ingredient"].nunique()),
        "lookback_pairs": len(lookback_df),
        "episodes": int(bac_df["episode"].sum()),
    }
    report, timings["generate_report"] = _time(
        "generate_report",
        lambda: generate_report(scores_all, scores_by_period, bac_df, med_periods or {}, summary),
    )
    report["combinations"] = detect_combinations(lookback_df, bac_df, min_cooccurrence=3)
    _, timings["generate_pdf"] = _time(
        "generate_pdf", lambda: generate_pdf(report, summary)
    )

    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    sizes = {
        "meals_df": _size_bytes(meals_df),
        "bac_df": _size_bytes(bac_df),
        "lookback_df": _size_bytes(lookback_df),
        "scores_all": _size_bytes(scores_all),
        "scores_by_period": _size_bytes(scores_by_period),
    }

    return {
        "months": months,
        "rows": {"meals": len(meals_df), "readings": len(bac_df), "lookback_pairs": len(lookback_df)},
        "timings": timings,
        "sizes": sizes,
        "peak_python_mem": peak,
        "ml_readings": (ml_block or {}).get("n_readings"),
    }


def _fmt_bytes(n: int) -> str:
    if n < 0:
        return "n/a"
    if n < 1024:
        return f"{n}B"
    if n < 1048576:
        return f"{n / 1024:.1f}KiB"
    return f"{n / 1048576:.1f}MiB"


def _report(results: list[dict]) -> None:
    stages = [
        "parse", "map_lookback", "compute_lift_scores", "lift_scores_by_period",
        "extract_features", "train_personal_model", "generate_report", "generate_pdf",
    ]
    print("\n=== Wall time per stage (seconds) ===")
    header = "stage".ljust(24) + "".join(f"{r['months']}mo".rjust(12) for r in results)
    print(header)
    for stage in stages:
        line = stage.ljust(24)
        for r in results:
            line += f"{r['timings'].get(stage, 0):.3f}".rjust(12)
        print(line)
    print("total".ljust(24) + "".join(f"{sum(r['timings'].values()):.3f}".rjust(12) for r in results))

    print("\n=== Dominant stage per size ===")
    for r in results:
        dom = max(r["timings"], key=r["timings"].get)
        print(f"  {r['months']}mo ({r['rows']['readings']} readings x {r['rows']['meals']} meals): "
              f"{dom} = {r['timings'][dom]:.3f}s")

    print("\n=== Retained field sizes (pickled) ===")
    for r in results:
        print(f"  {r['months']}mo: " + ", ".join(
            f"{k}={_fmt_bytes(r['sizes'][k])}" for k in r["sizes"]
        ))
    print("\n=== Peak Python allocation per analysis ===")
    for r in results:
        print(f"  {r['months']}mo: {r['peak_python_mem'] / 1048576:.1f} MiB "
              f"(ml trained on {r['ml_readings']} readings)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Measure ABS analysis scaling.")
    ap.add_argument("--months", type=float, nargs="+", default=[1, 3, 12, 24])
    ap.add_argument("--workdir", type=Path, default=Path("/tmp/abs_measure"))
    args = ap.parse_args()
    args.workdir.mkdir(parents=True, exist_ok=True)

    results = []
    for m in args.months:
        print(f"measuring {m} months ...", flush=True)
        results.append(measure_one(m, args.workdir))
    _report(results)


if __name__ == "__main__":
    main()
