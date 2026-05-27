"""
Tests for ml/train.py — per-user model training.

Synthesises feature matrices directly (we don't need real Excel parsing here),
then verifies:
- insufficient-data path
- output schema
- LASSO recovers a planted signal
- naive baseline beats nothing on pure noise
- bootstrap CIs are returned when requested
- multi-week temporal CV runs
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.train import train_personal_model, MIN_READINGS_DEFAULT, PRELIMINARY_BELOW


def _synthetic_features(
    n: int,
    *,
    p_noise: int = 5,
    signal_strength: float = 0.5,
    seed: int = 0,
    start: str = "2025-01-01",
) -> dict:
    """
    Build a fake `features` dict spanning several weeks.

    Planted signal: y = 1.0 + signal_strength * x_signal + small noise.
    Adds `p_noise` irrelevant numeric features.
    """
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start=start, periods=n, freq="12h")
    x_signal = rng.integers(0, 2, size=n).astype(float)  # binary "ing_rice"
    noise_cols = {f"noise_{i}": rng.normal(0, 1, size=n) for i in range(p_noise)}

    X = pd.DataFrame({"ing_signal": x_signal, **noise_cols})
    y = pd.Series(1.0 + signal_strength * x_signal + rng.normal(0, 0.1, size=n))

    return {
        "X": X,
        "y": y,
        "dates": pd.Series(dates),
        "user_ids": pd.Series(["alice"] * n),
        "feature_names": list(X.columns),
        "dropped_ingredients": [],
    }


# ---------------------------------------------------------------------------
class TestInsufficientData:
    def test_returns_insufficient_when_too_few_readings(self):
        feats = _synthetic_features(n=10)
        out = train_personal_model(feats, min_readings=80, bootstrap=False)
        assert out["status"] == "insufficient_data"
        assert out["n_readings"] == 10
        assert out["needed"] == 80

    def test_empty_features(self):
        feats = {
            "X": pd.DataFrame(),
            "y": pd.Series(dtype=float),
            "dates": pd.Series(dtype="datetime64[ns]"),
            "user_ids": pd.Series(dtype=object),
            "feature_names": [],
            "dropped_ingredients": [],
        }
        out = train_personal_model(feats, min_readings=80, bootstrap=False)
        assert out["status"] == "insufficient_data"


# ---------------------------------------------------------------------------
class TestOutputSchema:
    def test_keys_present(self):
        feats = _synthetic_features(n=120, seed=1)
        out = train_personal_model(feats, min_readings=80, bootstrap=False)
        assert out["status"] == "ok"
        for k in (
            "n_readings", "preliminary", "baseline_mae", "ridge_mae", "lasso_mae",
            "improvement_pct", "verdict", "verdict_message", "effects",
            "n_selected_features", "intercept", "alpha",
        ):
            assert k in out

    def test_preliminary_flag(self):
        feats = _synthetic_features(n=120, seed=2)
        out = train_personal_model(feats, min_readings=80, bootstrap=False)
        assert out["preliminary"] is True  # 80 <= 120 < 200

        feats_big = _synthetic_features(n=PRELIMINARY_BELOW + 20, seed=3)
        out_big = train_personal_model(feats_big, min_readings=80, bootstrap=False)
        assert out_big["preliminary"] is False

    def test_effects_schema(self):
        feats = _synthetic_features(n=120, seed=4)
        out = train_personal_model(feats, min_readings=80, bootstrap=False)
        assert len(out["effects"]) == len(feats["feature_names"])
        for eff in out["effects"]:
            assert set(eff.keys()) >= {
                "feature", "coef", "coef_raw",
                "ci_low", "ci_high", "ci_low_raw", "ci_high_raw",
                "significant",
            }


# ---------------------------------------------------------------------------
class TestSignalRecovery:
    def test_lasso_selects_planted_signal(self):
        feats = _synthetic_features(
            n=300, p_noise=8, signal_strength=0.6, seed=42
        )
        out = train_personal_model(feats, min_readings=80, bootstrap=False)
        assert out["status"] == "ok"

        # Find the signal feature
        signal = next(e for e in out["effects"] if e["feature"] == "ing_signal")
        assert signal["coef"] != 0
        # Planted positive effect → coefficient should be positive
        assert signal["coef_raw"] > 0.3
        # And it should be the strongest effect by absolute value
        assert abs(signal["coef"]) == max(abs(e["coef"]) for e in out["effects"])

    def test_lasso_beats_naive_on_real_signal(self):
        feats = _synthetic_features(
            n=300, p_noise=8, signal_strength=0.6, seed=7
        )
        out = train_personal_model(feats, min_readings=80, bootstrap=False)
        assert out["lasso_mae"] is not None
        assert out["baseline_mae"] is not None
        assert out["lasso_mae"] < out["baseline_mae"]
        assert out["improvement_pct"] > 5


# ---------------------------------------------------------------------------
class TestNoSignal:
    def test_pure_noise_yields_low_improvement(self):
        feats = _synthetic_features(
            n=300, p_noise=8, signal_strength=0.0, seed=11
        )
        out = train_personal_model(feats, min_readings=80, bootstrap=False)
        # With no real signal, improvement should be small/negative
        assert out["improvement_pct"] is None or out["improvement_pct"] < 15
        assert out["verdict"] in ("no_signal", "modest", "insufficient_cv_folds")


# ---------------------------------------------------------------------------
class TestBootstrap:
    def test_ci_returned_when_enabled(self):
        feats = _synthetic_features(
            n=120, p_noise=4, signal_strength=0.6, seed=13
        )
        out = train_personal_model(
            feats, min_readings=80, bootstrap=True, n_bootstrap=30
        )
        assert out["status"] == "ok"
        for eff in out["effects"]:
            assert eff["ci_low"] is not None
            assert eff["ci_high"] is not None
            assert eff["ci_low"] <= eff["ci_high"]

    def test_significant_marked_when_ci_excludes_zero(self):
        feats = _synthetic_features(
            n=300, p_noise=4, signal_strength=0.8, seed=17
        )
        out = train_personal_model(
            feats, min_readings=80, bootstrap=True, n_bootstrap=50
        )
        signal = next(e for e in out["effects"] if e["feature"] == "ing_signal")
        assert signal["significant"] is True


# ---------------------------------------------------------------------------
class TestVerdict:
    def test_verdict_message_string(self):
        feats = _synthetic_features(n=120, seed=23)
        out = train_personal_model(feats, min_readings=80, bootstrap=False)
        assert isinstance(out["verdict_message"], str)
        assert len(out["verdict_message"]) > 0
