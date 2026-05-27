"""
Tests for ml/features.py — multi-user-aware feature extraction.
"""

import datetime
import math

import pandas as pd
import pytest

from ml.features import extract_features, stack_user_features


def _bac(readings):
    df = pd.DataFrame(readings)
    df["date"] = pd.to_datetime(df["date"])
    df["bac_datetime"] = pd.to_datetime(df["bac_datetime"])
    return df


def _meals(rows):
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df["meal_datetime"] = pd.to_datetime(df["meal_datetime"])
    return df


# ---------------------------------------------------------------------------
class TestExtractFeaturesBasics:
    def test_empty_bac_returns_empty(self):
        out = extract_features(pd.DataFrame(), pd.DataFrame(), {})
        assert out["X"].empty
        assert out["y"].empty
        assert out["feature_names"] == []

    def test_no_meals_no_meds_produces_timing_only(self):
        bac = _bac([{
            "date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
            "promille": 1.5, "episode": False, "active_medications": "none",
        }])
        out = extract_features(pd.DataFrame(), bac, {})
        assert len(out["X"]) == 1
        cols = set(out["X"].columns)
        assert {"hour_sin", "hour_cos", "total_carbs_g", "n_ingredients"} <= cols
        # all nutritional totals are zero
        assert out["X"].iloc[0]["total_carbs_g"] == 0
        assert out["X"].iloc[0]["n_ingredients"] == 0

    def test_user_id_propagated(self):
        bac = _bac([{
            "date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
            "promille": 1.0, "episode": False, "active_medications": "none",
        }])
        out = extract_features(pd.DataFrame(), bac, {}, user_id="alice")
        assert all(out["user_ids"] == "alice")

    def test_target_matches_promille(self):
        bac = _bac([{
            "date": "2025-04-01", "bac_datetime": "2025-04-01 09:00",
            "promille": 2.3, "episode": True, "active_medications": "none",
        }])
        out = extract_features(pd.DataFrame(), bac, {})
        assert out["y"].iloc[0] == 2.3


# ---------------------------------------------------------------------------
class TestNutritionalAggregation:
    def test_carbs_and_sugars_summed_within_window(self):
        bac = _bac([{
            "date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
            "promille": 1.0, "episode": False, "active_medications": "none",
        }])
        meals = _meals([
            {"date": "2025-04-01", "meal_datetime": "2025-04-01 08:00",
             "ingredient": "Rice", "quantity_g": 100, "carbs_g": 30, "sugars_g": 1},
            {"date": "2025-04-01", "meal_datetime": "2025-04-01 10:00",
             "ingredient": "Banana", "quantity_g": 120, "carbs_g": 25, "sugars_g": 14},
        ])
        out = extract_features(meals, bac, {}, lookback_hours=24, min_ingredient_count=1)
        row = out["X"].iloc[0]
        assert row["total_carbs_g"] == 55
        assert row["total_sugars_g"] == 15
        assert row["total_quantity_g"] == 220
        assert row["n_ingredients"] == 2

    def test_meals_outside_window_ignored(self):
        bac = _bac([{
            "date": "2025-04-02", "bac_datetime": "2025-04-02 12:00",
            "promille": 1.0, "episode": False, "active_medications": "none",
        }])
        meals = _meals([
            # 30 h before — outside 24h window
            {"date": "2025-04-01", "meal_datetime": "2025-04-01 06:00",
             "ingredient": "Rice", "quantity_g": 100, "carbs_g": 30, "sugars_g": 1},
            # 2 h before — inside
            {"date": "2025-04-02", "meal_datetime": "2025-04-02 10:00",
             "ingredient": "Bread", "quantity_g": 80, "carbs_g": 40, "sugars_g": 2},
        ])
        out = extract_features(meals, bac, {}, lookback_hours=24, min_ingredient_count=1)
        row = out["X"].iloc[0]
        assert row["total_carbs_g"] == 40
        assert row["n_ingredients"] == 1


# ---------------------------------------------------------------------------
class TestTiming:
    def test_hours_since_last_meal(self):
        bac = _bac([{
            "date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
            "promille": 1.0, "episode": False, "active_medications": "none",
        }])
        meals = _meals([
            {"date": "2025-04-01", "meal_datetime": "2025-04-01 09:30",
             "ingredient": "Rice", "quantity_g": 100, "carbs_g": 30, "sugars_g": 1},
        ])
        out = extract_features(meals, bac, {}, lookback_hours=24, min_ingredient_count=1)
        assert out["X"].iloc[0]["hours_since_last_meal"] == pytest.approx(2.5)

    def test_hour_cyclic_features_range(self):
        bac = _bac([{
            "date": "2025-04-01", "bac_datetime": "2025-04-01 06:00",
            "promille": 1.0, "episode": False, "active_medications": "none",
        }])
        out = extract_features(pd.DataFrame(), bac, {})
        s = out["X"].iloc[0]["hour_sin"]
        c = out["X"].iloc[0]["hour_cos"]
        assert s == pytest.approx(math.sin(2 * math.pi * 6 / 24))
        assert c == pytest.approx(math.cos(2 * math.pi * 6 / 24))


# ---------------------------------------------------------------------------
class TestMedications:
    def test_on_med_flag_and_days(self):
        bac = _bac([{
            "date": "2025-04-10", "bac_datetime": "2025-04-10 12:00",
            "promille": 1.0, "episode": False, "active_medications": "Rifaximin",
        }])
        med_periods = {
            "Rifaximin": [{"start": datetime.date(2025, 4, 1), "stop": None}],
        }
        out = extract_features(pd.DataFrame(), bac, med_periods)
        row = out["X"].iloc[0]
        assert row["on_rifaximin"] == 1
        assert row["days_on_rifaximin"] == 9

    def test_med_inactive_outside_period(self):
        bac = _bac([{
            "date": "2025-05-01", "bac_datetime": "2025-05-01 12:00",
            "promille": 1.0, "episode": False, "active_medications": "none",
        }])
        med_periods = {
            "Rifaximin": [{"start": datetime.date(2025, 4, 1),
                           "stop": datetime.date(2025, 4, 14)}],
        }
        out = extract_features(pd.DataFrame(), bac, med_periods)
        row = out["X"].iloc[0]
        assert row["on_rifaximin"] == 0
        assert row["days_on_rifaximin"] == 0


# ---------------------------------------------------------------------------
class TestIngredientFiltering:
    def test_rare_ingredients_dropped(self):
        # Rice appears in 3 windows, Caviar in 1.  min_count=3 → Caviar dropped.
        readings = []
        meals = []
        for i, day in enumerate(["2025-04-01", "2025-04-02", "2025-04-03"]):
            readings.append({
                "date": day, "bac_datetime": f"{day} 12:00",
                "promille": 1.0 + i * 0.1,
                "episode": False, "active_medications": "none",
            })
            meals.append({
                "date": day, "meal_datetime": f"{day} 10:00",
                "ingredient": "Rice", "quantity_g": 100, "carbs_g": 30, "sugars_g": 1,
            })
        meals.append({
            "date": "2025-04-01", "meal_datetime": "2025-04-01 11:00",
            "ingredient": "Caviar", "quantity_g": 10, "carbs_g": 0, "sugars_g": 0,
        })

        out = extract_features(
            _meals(meals), _bac(readings), {},
            lookback_hours=24, min_ingredient_count=3,
        )
        cols = set(out["X"].columns)
        assert "ing_rice" in cols
        assert "ing_caviar" not in cols
        assert "caviar" in out["dropped_ingredients"]

    def test_canonicalisation_collapses_case(self):
        readings = [
            {"date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
             "promille": 1.0, "episode": False, "active_medications": "none"},
            {"date": "2025-04-02", "bac_datetime": "2025-04-02 12:00",
             "promille": 1.1, "episode": False, "active_medications": "none"},
        ]
        meals = [
            {"date": "2025-04-01", "meal_datetime": "2025-04-01 10:00",
             "ingredient": "RICE", "quantity_g": 100, "carbs_g": 30, "sugars_g": 1},
            {"date": "2025-04-02", "meal_datetime": "2025-04-02 10:00",
             "ingredient": "rice", "quantity_g": 100, "carbs_g": 30, "sugars_g": 1},
        ]
        out = extract_features(
            _meals(meals), _bac(readings), {},
            lookback_hours=24, min_ingredient_count=2,
        )
        assert "ing_rice" in out["X"].columns
        assert out["X"]["ing_rice"].sum() == 2


# ---------------------------------------------------------------------------
class TestMultiUserStacking:
    def test_stack_aligns_columns_and_fills_missing(self):
        bac_a = _bac([{
            "date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
            "promille": 1.0, "episode": False, "active_medications": "none",
        }])
        meals_a = _meals([
            {"date": "2025-04-01", "meal_datetime": "2025-04-01 10:00",
             "ingredient": "Rice", "quantity_g": 100, "carbs_g": 30, "sugars_g": 1},
        ])
        bac_b = _bac([{
            "date": "2025-04-02", "bac_datetime": "2025-04-02 12:00",
            "promille": 1.4, "episode": False, "active_medications": "none",
        }])
        meals_b = _meals([
            {"date": "2025-04-02", "meal_datetime": "2025-04-02 10:00",
             "ingredient": "Bread", "quantity_g": 80, "carbs_g": 40, "sugars_g": 2},
        ])
        a = extract_features(meals_a, bac_a, {}, user_id="alice", min_ingredient_count=1)
        b = extract_features(meals_b, bac_b, {}, user_id="bob", min_ingredient_count=1)
        combined = stack_user_features([a, b])

        assert len(combined["X"]) == 2
        assert "ing_rice" in combined["X"].columns
        assert "ing_bread" in combined["X"].columns
        assert list(combined["user_ids"]) == ["alice", "bob"]
        # alice has no bread, bob has no rice
        assert combined["X"].loc[0, "ing_bread"] == 0
        assert combined["X"].loc[1, "ing_rice"] == 0

    def test_stack_empty_inputs(self):
        out = stack_user_features([])
        assert out["X"].empty
