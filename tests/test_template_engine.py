"""
Tests for ai/template_engine.py — report generation, risk prediction, combinations.
"""

import pandas as pd
import pytest

from ai.template_engine import generate_report, predict_risk, detect_combinations


def _make_scores_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _make_bac_df(readings: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(readings)
    df["date"] = pd.to_datetime(df["date"])
    df["bac_datetime"] = pd.to_datetime(df["bac_datetime"])
    return df


def _make_lookback_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# generate_report
# ---------------------------------------------------------------------------
class TestGenerateReport:
    def _default_data(self):
        scores = _make_scores_df([
            {"ingredient": "Rice", "lift": 2.5, "n_present": 10,
             "mean_bac_present": 1.2, "low_confidence": False, "always_present": False},
            {"ingredient": "Chicken", "lift": 0.5, "n_present": 8,
             "mean_bac_present": 0.3, "low_confidence": False, "always_present": False},
        ])
        bac = _make_bac_df([
            {"date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
             "promille": 1.5, "active_medications": "none"},
        ])
        summary = {
            "total_readings": 50,
            "episodes": 5,
            "date_min": "2025-01-01",
            "date_max": "2025-04-01",
            "bac_mean": 0.8,
            "bac_max": 2.5,
            "unique_ingredients": 30,
        }
        return scores, {}, bac, {}, summary

    def test_returns_expected_keys(self):
        scores, by_period, bac, med, summary = self._default_data()
        report = generate_report(scores, by_period, bac, med, summary)
        assert "summary_text" in report
        assert "top_suspects" in report
        assert "safe_ingredients" in report
        assert "medication_comparison" in report
        assert "caveats" in report

    def test_suspect_classified(self):
        scores, by_period, bac, med, summary = self._default_data()
        report = generate_report(scores, by_period, bac, med, summary)
        suspects = [s["ingredient"] for s in report["top_suspects"]]
        assert "Rice" in suspects

    def test_safe_classified(self):
        scores, by_period, bac, med, summary = self._default_data()
        report = generate_report(scores, by_period, bac, med, summary)
        safe = [s["ingredient"] for s in report["safe_ingredients"]]
        assert "Chicken" in safe

    def test_summary_text_contains_stats(self):
        scores, by_period, bac, med, summary = self._default_data()
        report = generate_report(scores, by_period, bac, med, summary)
        assert "50 BAC readings" in report["summary_text"]
        assert "Avg BAC" in report["summary_text"]

    def test_empty_scores(self):
        bac = _make_bac_df([
            {"date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
             "promille": 1.0, "active_medications": "none"},
        ])
        summary = {"total_readings": 1, "episodes": 0, "date_min": "2025-04-01",
                    "date_max": "2025-04-01", "bac_mean": 1.0, "bac_max": 1.0,
                    "unique_ingredients": 0}
        report = generate_report(pd.DataFrame(), {}, bac, {}, summary)
        assert report["top_suspects"] == []
        assert report["safe_ingredients"] == []


# ---------------------------------------------------------------------------
# predict_risk
# ---------------------------------------------------------------------------
class TestPredictRisk:
    def test_known_suspect(self):
        scores = _make_scores_df([
            {"ingredient": "Rice", "lift": 2.5, "n_present": 10,
             "mean_bac_present": 1.2, "mean_bac_absent": 0.5,
             "low_confidence": False, "always_present": False},
        ])
        lookback = _make_lookback_df([
            {"bac_idx": 0, "ingredient": "Rice", "approximate": False},
        ])
        bac = _make_bac_df([
            {"date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
             "promille": 2.0, "active_medications": "none"},
        ])
        result = predict_risk(["Rice"], scores, lookback, bac)
        assert "risk_level" in result
        assert result["risk_level"] in ("LOW", "MEDIUM", "HIGH")

    def test_unknown_ingredient(self):
        scores = _make_scores_df([
            {"ingredient": "Rice", "lift": 2.5, "n_present": 10,
             "mean_bac_present": 1.2, "mean_bac_absent": 0.5,
             "low_confidence": False, "always_present": False},
        ])
        bac = _make_bac_df([
            {"date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
             "promille": 1.0, "active_medications": "none"},
        ])
        result = predict_risk(["UnknownFruit"], scores, pd.DataFrame(), bac)
        assert "risk_level" in result


# ---------------------------------------------------------------------------
# detect_combinations
# ---------------------------------------------------------------------------
class TestDetectCombinations:
    def test_finds_pair(self):
        bac = _make_bac_df([
            {"date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
             "promille": 2.0, "active_medications": "none"},
            {"date": "2025-04-02", "bac_datetime": "2025-04-02 12:00",
             "promille": 2.5, "active_medications": "none"},
            {"date": "2025-04-03", "bac_datetime": "2025-04-03 12:00",
             "promille": 1.8, "active_medications": "none"},
        ])
        lookback = _make_lookback_df([
            {"bac_idx": 0, "ingredient": "Rice"},
            {"bac_idx": 0, "ingredient": "Banana"},
            {"bac_idx": 1, "ingredient": "Rice"},
            {"bac_idx": 1, "ingredient": "Banana"},
            {"bac_idx": 2, "ingredient": "Rice"},
            {"bac_idx": 2, "ingredient": "Banana"},
        ])
        combos = detect_combinations(lookback, bac, min_cooccurrence=3)
        assert len(combos) == 1
        assert set(combos[0]["pair"]) == {"Rice", "Banana"}

    def test_below_min_cooccurrence(self):
        bac = _make_bac_df([
            {"date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
             "promille": 2.0, "active_medications": "none"},
        ])
        lookback = _make_lookback_df([
            {"bac_idx": 0, "ingredient": "Rice"},
            {"bac_idx": 0, "ingredient": "Banana"},
        ])
        combos = detect_combinations(lookback, bac, min_cooccurrence=3)
        assert len(combos) == 0

    def test_empty_lookback(self):
        bac = _make_bac_df([
            {"date": "2025-04-01", "bac_datetime": "2025-04-01 12:00",
             "promille": 1.0, "active_medications": "none"},
        ])
        combos = detect_combinations(pd.DataFrame(), bac)
        assert combos == []
