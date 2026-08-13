"""
Tests for core/analysis.py — lookback mapping and lift scores.
"""

import datetime
from collections import Counter
from pathlib import Path

import pandas as pd
import pytest

from core.analysis import map_lookback, compute_lift_scores
from core import parse_log


def _naive_map_lookback(bac_df, meals_df, hours=3):
    """The original nested-`iterrows` implementation, kept as the equivalence oracle.

    T019 replaced this with a vectorised join. Principle V requires proving the fast
    version produces the same result on real data before it is trusted, so the slow
    version lives on here as the reference.
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
                    records.append({
                        "bac_idx": bac_idx, "bac_datetime": bac_dt,
                        "promille": bac_row["promille"], "episode": bac_row["episode"],
                        "active_medications": bac_row["active_medications"],
                        "ingredient": meal_row["ingredient"],
                        "quantity_g": meal_row["quantity_g"], "meal": meal_row["meal"],
                        "meal_datetime": meal_dt, "hours_before": round(hours_before, 2),
                        "approximate": False,
                    })
            else:
                meal_date = meal_row["date"]
                if pd.notnull(meal_date) and (
                    pd.Timestamp(meal_date).date() >= window_start.date()
                    and pd.Timestamp(meal_date).date() <= bac_dt.date()
                ):
                    records.append({
                        "bac_idx": bac_idx, "bac_datetime": bac_dt,
                        "promille": bac_row["promille"], "episode": bac_row["episode"],
                        "active_medications": bac_row["active_medications"],
                        "ingredient": meal_row["ingredient"],
                        "quantity_g": meal_row["quantity_g"], "meal": meal_row["meal"],
                        "meal_datetime": None, "hours_before": None, "approximate": True,
                    })
    return pd.DataFrame(records)


def _canonical(df):
    """Order-independent multiset of rows with normalised values for comparison."""
    if df is None or df.empty:
        return Counter()
    rows = []
    for _, r in df.iterrows():
        mdt = r["meal_datetime"]
        hb = r["hours_before"]
        qty = r["quantity_g"]
        rows.append((
            int(r["bac_idx"]),
            str(r["ingredient"]),
            str(r["meal"]),
            bool(r["approximate"]),
            None if pd.isna(hb) else round(float(hb), 2),
            str(r["active_medications"]),
            round(float(r["promille"]), 6),
            bool(r["episode"]),
            None if pd.isna(qty) else round(float(qty), 6),
            None if pd.isna(mdt) else pd.Timestamp(mdt).isoformat(),
        ))
    return Counter(rows)


def _make_bac_df(readings: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(readings)
    df["date"] = pd.to_datetime(df["date"])
    df["bac_datetime"] = pd.to_datetime(df["bac_datetime"])
    return df


def _make_meals_df(meals: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(meals)
    df["date"] = pd.to_datetime(df["date"])
    if "meal_datetime" in df.columns:
        df["meal_datetime"] = pd.to_datetime(df["meal_datetime"])
    return df


# ---------------------------------------------------------------------------
# map_lookback
# ---------------------------------------------------------------------------
class TestMapLookback:
    def test_meal_within_window(self):
        bac = _make_bac_df([{
            "date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
            "promille": 1.5, "episode": False, "active_medications": "none",
        }])
        meals = _make_meals_df([{
            "date": "2025-04-01", "meal_datetime": "2025-04-01 10:00",
            "ingredient": "Rice", "quantity_g": 200, "meal": "Breakfast",
        }])
        result = map_lookback(bac, meals, hours=3)
        assert len(result) == 1
        assert result.iloc[0]["ingredient"] == "Rice"
        assert result.iloc[0]["approximate"] == False

    def test_meal_outside_window(self):
        bac = _make_bac_df([{
            "date": "2025-04-01", "bac_datetime": "2025-04-01 18:00",
            "promille": 0.5, "episode": False, "active_medications": "none",
        }])
        meals = _make_meals_df([{
            "date": "2025-04-01", "meal_datetime": "2025-04-01 08:00",
            "ingredient": "Rice", "quantity_g": 200, "meal": "Breakfast",
        }])
        result = map_lookback(bac, meals, hours=3)
        assert len(result) == 0

    def test_approximate_matching_no_meal_time(self):
        bac = _make_bac_df([{
            "date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
            "promille": 1.0, "episode": False, "active_medications": "none",
        }])
        meals = _make_meals_df([{
            "date": "2025-04-01", "meal_datetime": None,
            "ingredient": "Bread", "quantity_g": 100, "meal": "Lunch",
        }])
        result = map_lookback(bac, meals, hours=3)
        assert len(result) == 1
        assert result.iloc[0]["approximate"] == True

    def test_empty_meals(self):
        bac = _make_bac_df([{
            "date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
            "promille": 1.0, "episode": False, "active_medications": "none",
        }])
        result = map_lookback(bac, pd.DataFrame(), hours=3)
        assert result.empty

    def test_empty_bac(self):
        meals = _make_meals_df([{
            "date": "2025-04-01", "meal_datetime": "2025-04-01 10:00",
            "ingredient": "Rice", "quantity_g": 200, "meal": "Breakfast",
        }])
        result = map_lookback(pd.DataFrame(), meals, hours=3)
        assert result.empty

    def test_multiple_ingredients_in_window(self):
        bac = _make_bac_df([{
            "date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
            "promille": 2.0, "episode": True, "active_medications": "none",
        }])
        meals = _make_meals_df([
            {"date": "2025-04-01", "meal_datetime": "2025-04-01 10:00",
             "ingredient": "Rice", "quantity_g": 200, "meal": "Breakfast"},
            {"date": "2025-04-01", "meal_datetime": "2025-04-01 10:00",
             "ingredient": "Banana", "quantity_g": 120, "meal": "Breakfast"},
        ])
        result = map_lookback(bac, meals, hours=3)
        assert len(result) == 2
        assert set(result["ingredient"]) == {"Rice", "Banana"}

    def test_hours_before_calculation(self):
        bac = _make_bac_df([{
            "date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
            "promille": 1.0, "episode": False, "active_medications": "none",
        }])
        meals = _make_meals_df([{
            "date": "2025-04-01", "meal_datetime": "2025-04-01 10:30",
            "ingredient": "Rice", "quantity_g": 200, "meal": "Breakfast",
        }])
        result = map_lookback(bac, meals, hours=3)
        assert result.iloc[0]["hours_before"] == 1.5


# ---------------------------------------------------------------------------
# Vectorised map_lookback == naive reference (T019, Principle V)
# ---------------------------------------------------------------------------
class TestVectorisedEquivalence:
    def test_matches_reference_on_example_log(self):
        example = Path(__file__).resolve().parents[1] / "example" / "example_log.xlsx"
        if not example.exists():
            pytest.skip("example workbook not present")
        meals_df, bac_df, _ = parse_log(str(example))
        for hours in (1, 3, 6, 24):
            fast = map_lookback(bac_df, meals_df, hours=hours)
            slow = _naive_map_lookback(bac_df, meals_df, hours=hours)
            assert _canonical(fast) == _canonical(slow), f"mismatch at hours={hours}"

    def test_matches_reference_on_synthetic_fixture(self):
        from tests.fixtures.generate_year_log import generate

        path = generate(months=2, out=Path("/tmp/analysis_equiv.xlsx"), seed=99)
        meals_df, bac_df, _ = parse_log(str(path))
        for hours in (2, 3):
            fast = map_lookback(bac_df, meals_df, hours=hours)
            slow = _naive_map_lookback(bac_df, meals_df, hours=hours)
            assert _canonical(fast) == _canonical(slow)

    def test_matches_reference_with_untimed_meals(self):
        """Exercise the approximate date-level fallback path explicitly."""
        bac = _make_bac_df([
            {"date": "2025-04-02", "bac_datetime": "2025-04-02 09:00",
             "promille": 1.2, "episode": False, "active_medications": "none"},
            {"date": "2025-04-03", "bac_datetime": "2025-04-03 20:00",
             "promille": 2.4, "episode": True, "active_medications": "DrugA"},
        ])
        meals = _make_meals_df([
            # timed, within window of reading 1
            {"date": "2025-04-02", "meal_datetime": "2025-04-02 07:30",
             "ingredient": "Rice", "quantity_g": 200, "meal": "Breakfast"},
            # untimed on the same day as reading 1
            {"date": "2025-04-02", "meal_datetime": None,
             "ingredient": "Bread", "quantity_g": 80, "meal": "Snack"},
            # untimed on the day of reading 2
            {"date": "2025-04-03", "meal_datetime": None,
             "ingredient": "Pasta", "quantity_g": 150, "meal": "Dinner"},
            # untimed well before either reading's window
            {"date": "2025-03-01", "meal_datetime": None,
             "ingredient": "OldFood", "quantity_g": 50, "meal": "Lunch"},
        ])
        fast = map_lookback(bac, meals, hours=3)
        slow = _naive_map_lookback(bac, meals, hours=3)
        assert _canonical(fast) == _canonical(slow)
        # Sanity: OldFood must not appear (out of every window).
        assert "OldFood" not in set(fast["ingredient"])

    def test_vectorised_is_fast_at_scale(self):
        """The whole point: 12-month lookback finishes quickly, not in minutes."""
        import time
        from tests.fixtures.generate_year_log import generate

        path = generate(months=12, out=Path("/tmp/analysis_perf.xlsx"), seed=5)
        meals_df, bac_df, _ = parse_log(str(path))
        start = time.perf_counter()
        result = map_lookback(bac_df, meals_df, hours=3)
        elapsed = time.perf_counter() - start
        assert not result.empty
        # Un-vectorised this was ~436s (T004); a generous ceiling still proves the point.
        assert elapsed < 15.0, f"map_lookback too slow: {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# compute_lift_scores
# ---------------------------------------------------------------------------
class TestComputeLiftScores:
    def _setup(self):
        """Create BAC + lookback data with a clear suspect ingredient."""
        bac = _make_bac_df([
            {"date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
             "promille": 2.5, "episode": True, "active_medications": "none"},
            {"date": "2025-04-02", "bac_datetime": "2025-04-02 12:00",
             "promille": 0.3, "episode": False, "active_medications": "none"},
            {"date": "2025-04-03", "bac_datetime": "2025-04-03 12:00",
             "promille": 2.0, "episode": True, "active_medications": "none"},
            {"date": "2025-04-04", "bac_datetime": "2025-04-04 12:00",
             "promille": 0.2, "episode": False, "active_medications": "none"},
        ])
        # Rice present on high-BAC days (idx 0, 2), absent on low-BAC days
        lookback = pd.DataFrame([
            {"bac_idx": 0, "ingredient": "Rice", "active_medications": "none",
             "approximate": False},
            {"bac_idx": 2, "ingredient": "Rice", "active_medications": "none",
             "approximate": False},
            {"bac_idx": 0, "ingredient": "Water", "active_medications": "none",
             "approximate": False},
            {"bac_idx": 1, "ingredient": "Water", "active_medications": "none",
             "approximate": False},
            {"bac_idx": 2, "ingredient": "Water", "active_medications": "none",
             "approximate": False},
            {"bac_idx": 3, "ingredient": "Water", "active_medications": "none",
             "approximate": False},
        ])
        return bac, lookback

    def test_suspect_has_high_lift(self):
        bac, lookback = self._setup()
        scores = compute_lift_scores(bac, lookback, min_observations=1)
        rice = scores[scores["ingredient"] == "Rice"].iloc[0]
        # Rice present on 2.5 and 2.0 days → mean=2.25
        # Rice absent on 0.3 and 0.2 days → mean=0.25
        # Lift = 2.25/0.25 = 9.0
        assert rice["lift"] == 9.0
        assert rice["n_present"] == 2

    def test_always_present_ingredient(self):
        bac, lookback = self._setup()
        scores = compute_lift_scores(bac, lookback, min_observations=1)
        water = scores[scores["ingredient"] == "Water"].iloc[0]
        assert water["always_present"] == True
        # lift is None or NaN when always present
        assert water["lift"] is None or pd.isna(water["lift"])

    def test_low_confidence_flag(self):
        bac, lookback = self._setup()
        scores = compute_lift_scores(bac, lookback, min_observations=3)
        rice = scores[scores["ingredient"] == "Rice"].iloc[0]
        assert rice["low_confidence"] == True  # only 2 observations, need 3

    def test_empty_lookback(self):
        bac, _ = self._setup()
        scores = compute_lift_scores(bac, pd.DataFrame(), min_observations=1)
        assert scores.empty

    def test_period_filter(self):
        bac = _make_bac_df([
            {"date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
             "promille": 2.0, "episode": True, "active_medications": "DrugA"},
            {"date": "2025-04-02", "bac_datetime": "2025-04-02 12:00",
             "promille": 0.5, "episode": False, "active_medications": "none"},
        ])
        lookback = pd.DataFrame([
            {"bac_idx": 0, "ingredient": "Rice", "active_medications": "DrugA",
             "approximate": False},
            {"bac_idx": 1, "ingredient": "Rice", "active_medications": "none",
             "approximate": False},
        ])
        scores = compute_lift_scores(bac, lookback, min_observations=1, period_filter="DrugA")
        assert len(scores) == 1
        assert scores.iloc[0]["ingredient"] == "Rice"

    def test_sorted_by_lift_descending(self):
        bac, lookback = self._setup()
        scores = compute_lift_scores(bac, lookback, min_observations=1)
        lifts = [s for s in scores["lift"].tolist() if s is not None]
        assert lifts == sorted(lifts, reverse=True)
