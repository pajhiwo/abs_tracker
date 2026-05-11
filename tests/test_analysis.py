"""
Tests for core/analysis.py — lookback mapping and lift scores.
"""

import datetime
import pandas as pd
import pytest

from core.analysis import map_lookback, compute_lift_scores


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
