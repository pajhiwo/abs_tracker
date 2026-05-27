"""
Per-user model training (Stage 8b).

Trains a personal LASSO model on the feature matrix produced by
`ml.features.extract_features`, with:

- naive baseline (predict user's mean BAC) for honest comparison
- ridge baseline (linear model without feature selection)
- leave-one-week-out temporal cross-validation
- bootstrap 95% confidence intervals on coefficients

Returns a JSON-serialisable dict suitable for direct embedding in the API
response and rendering in the UI.

Public API
----------
train_personal_model(features: dict, *, ...) -> dict | None
    `features` is the output of `extract_features`.
    Returns None if not enough data (< min_readings).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LassoCV, Ridge
from sklearn.preprocessing import StandardScaler


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
MIN_READINGS_DEFAULT = 80
PRELIMINARY_BELOW = 200  # 80..199 readings → mark model preliminary
BOOTSTRAP_N_DEFAULT = 200


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _mae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.mean(np.abs(np.asarray(y_true) - np.asarray(y_pred))))


def _week_key(ts: pd.Timestamp) -> str:
    iso = ts.isocalendar()
    return f"{int(iso.year)}-W{int(iso.week):02d}"


def _leave_one_week_out_mae(
    X: np.ndarray, y: np.ndarray, weeks: np.ndarray, model_factory
) -> float | None:
    """
    Temporal CV: each unique week is held out once.
    Returns mean fold MAE, or None if fewer than 2 weeks available.
    """
    unique_weeks = np.unique(weeks)
    if len(unique_weeks) < 2:
        return None

    fold_maes: list[float] = []
    for w in unique_weeks:
        test_mask = weeks == w
        train_mask = ~test_mask
        if train_mask.sum() < 5 or test_mask.sum() == 0:
            continue
        model = model_factory()
        try:
            model.fit(X[train_mask], y[train_mask])
            pred = model.predict(X[test_mask])
        except Exception:
            continue
        fold_maes.append(_mae(y[test_mask], pred))

    if not fold_maes:
        return None
    return float(np.mean(fold_maes))


def _lasso_factory(random_state: int = 0):
    def _make():
        return LassoCV(
            cv=3,
            alphas=20,
            max_iter=5000,
            random_state=random_state,
            n_jobs=None,
        )
    return _make


def _ridge_factory():
    def _make():
        return Ridge(alpha=1.0)
    return _make


def _naive_factory(mean_value: float):
    class _NaiveMean:
        def fit(self, X, y):
            self._m = float(np.mean(y))
            return self

        def predict(self, X):
            return np.full(len(X), self._m)

    def _make():
        return _NaiveMean()
    return _make


def _bootstrap_coef_ci(
    X: np.ndarray,
    y: np.ndarray,
    *,
    n_boot: int = BOOTSTRAP_N_DEFAULT,
    random_state: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Bootstrap 95% CIs for LASSO coefficients.
    Returns (low, high) arrays of length n_features.
    """
    rng = np.random.default_rng(random_state)
    n, p = X.shape
    coefs = np.zeros((n_boot, p))

    for b in range(n_boot):
        idx = rng.integers(0, n, size=n)
        Xb = X[idx]
        yb = y[idx]
        try:
            m = LassoCV(cv=3, alphas=10, max_iter=2000,
                        random_state=random_state + b)
            m.fit(Xb, yb)
            coefs[b] = m.coef_
        except Exception:
            coefs[b] = np.nan

    low = np.nanpercentile(coefs, 2.5, axis=0)
    high = np.nanpercentile(coefs, 97.5, axis=0)
    return low, high


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------
def _verdict(improvement_pct: float | None) -> str:
    if improvement_pct is None:
        return "insufficient_cv_folds"
    if improvement_pct < 5:
        return "no_signal"
    if improvement_pct < 15:
        return "modest"
    if improvement_pct < 30:
        return "meaningful"
    return "strong"


def _verdict_message(verdict: str) -> str:
    return {
        "insufficient_cv_folds": "Not enough weekly variation for cross-validation. Keep logging.",
        "no_signal": "Model does not outperform your average BAC. More data needed or food is not the main driver.",
        "modest": "Modest improvement. Effects are preliminary. Keep logging.",
        "meaningful": "Meaningful improvement. Significant effects are likely useful.",
        "strong": "Strong predictive power. Effects are stable enough to guide dietary experiments.",
    }[verdict]


# ---------------------------------------------------------------------------
# main API
# ---------------------------------------------------------------------------
def train_personal_model(
    features: dict,
    *,
    min_readings: int = MIN_READINGS_DEFAULT,
    bootstrap: bool = True,
    n_bootstrap: int = BOOTSTRAP_N_DEFAULT,
    random_state: int = 0,
) -> dict | None:
    """
    Train a per-user LASSO model on the given feature dict.

    Returns a JSON-serialisable dict with keys:
        status              : "ok" | "insufficient_data"
        n_readings          : int
        preliminary         : bool   (True if 80..199 readings)
        baseline_mae        : float  (naive mean predictor)
        ridge_mae           : float | None
        lasso_mae           : float | None
        improvement_pct     : float | None   over naive baseline
        verdict             : str    (no_signal | modest | meaningful | strong | insufficient_cv_folds)
        verdict_message     : str
        effects             : list of {feature, coef, ci_low, ci_high, significant}
        n_selected_features : int
        intercept           : float
        alpha               : float
    """
    X_df: pd.DataFrame = features.get("X", pd.DataFrame())
    y_ser: pd.Series = features.get("y", pd.Series(dtype=float))
    dates: pd.Series = features.get("dates", pd.Series(dtype="datetime64[ns]"))
    feature_names: list[str] = list(features.get("feature_names", []))

    n = len(y_ser)
    if n < min_readings or X_df.empty:
        return {
            "status": "insufficient_data",
            "n_readings": n,
            "needed": min_readings,
            "verdict": "insufficient_data",
            "verdict_message": (
                f"Need at least {min_readings} BAC readings to train a personal "
                f"model (currently {n})."
            ),
        }

    preliminary = n < PRELIMINARY_BELOW

    # ---- prepare arrays
    X_raw = X_df.to_numpy(dtype=float, copy=True)
    y = y_ser.to_numpy(dtype=float, copy=True)
    weeks = np.array([_week_key(pd.Timestamp(t)) for t in dates])

    # standardise so coefficients are comparable across features
    scaler = StandardScaler()
    X = scaler.fit_transform(X_raw)
    # guard zero-variance columns (StandardScaler with_std=True handles this
    # by setting scale_=1 for zero-variance, leaving the column at 0)

    # ---- CV MAEs
    naive_mae = _leave_one_week_out_mae(
        X, y, weeks, _naive_factory(float(np.mean(y)))
    )
    ridge_mae = _leave_one_week_out_mae(X, y, weeks, _ridge_factory())
    lasso_mae = _leave_one_week_out_mae(X, y, weeks, _lasso_factory(random_state))

    if naive_mae is not None and lasso_mae is not None and naive_mae > 0:
        improvement_pct = (1.0 - lasso_mae / naive_mae) * 100.0
    else:
        improvement_pct = None

    # ---- fit final model on all data for coefficients
    final = LassoCV(cv=3, alphas=20, max_iter=5000, random_state=random_state)
    final.fit(X, y)
    coefs = final.coef_

    # ---- bootstrap CIs
    if bootstrap and len(coefs) > 0:
        ci_low, ci_high = _bootstrap_coef_ci(
            X, y, n_boot=n_bootstrap, random_state=random_state
        )
    else:
        ci_low = np.full_like(coefs, np.nan)
        ci_high = np.full_like(coefs, np.nan)

    # ---- de-standardise coefficients back to original feature units
    # std_coef refers to standardised X; to interpret on the original scale,
    # divide by scaler.scale_ (the column std).  We keep BOTH for the UI:
    # - `coef`  : standardised (comparable across features)
    # - `coef_raw`: per-unit-of-feature effect on BAC (permille per gram, etc.)
    scale = np.asarray(scaler.scale_, dtype=float).copy()
    scale[scale == 0] = 1.0
    coef_raw = coefs / scale
    ci_low_raw = ci_low / scale
    ci_high_raw = ci_high / scale

    effects = []
    for i, name in enumerate(feature_names):
        c = float(coefs[i])
        if c == 0 and (np.isnan(ci_low[i]) or (ci_low[i] <= 0 <= ci_high[i])):
            # uninteresting: zero effect and CI straddles zero
            significant = False
        else:
            significant = (
                not np.isnan(ci_low[i])
                and not np.isnan(ci_high[i])
                and (ci_low[i] > 0 or ci_high[i] < 0)
            )
        effects.append({
            "feature": name,
            "coef": c,                          # standardised
            "coef_raw": float(coef_raw[i]),     # per-unit
            "ci_low": float(ci_low[i]) if not np.isnan(ci_low[i]) else None,
            "ci_high": float(ci_high[i]) if not np.isnan(ci_high[i]) else None,
            "ci_low_raw": float(ci_low_raw[i]) if not np.isnan(ci_low_raw[i]) else None,
            "ci_high_raw": float(ci_high_raw[i]) if not np.isnan(ci_high_raw[i]) else None,
            "significant": bool(significant),
        })

    # sort effects by absolute standardised coefficient, descending
    effects.sort(key=lambda e: abs(e["coef"]), reverse=True)

    n_selected = int(np.sum(coefs != 0))
    verdict = _verdict(improvement_pct)

    return {
        "status": "ok",
        "n_readings": n,
        "preliminary": preliminary,
        "baseline_mae": naive_mae,
        "ridge_mae": ridge_mae,
        "lasso_mae": lasso_mae,
        "improvement_pct": improvement_pct,
        "verdict": verdict,
        "verdict_message": _verdict_message(verdict),
        "effects": effects,
        "n_selected_features": n_selected,
        "intercept": float(final.intercept_),
        "alpha": float(final.alpha_),
    }
