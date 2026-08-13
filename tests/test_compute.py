"""Deterministic core is standalone and staged (T008, T010; research R1, Principle II).

Two properties matter here:

1. The summary and lift scores are stage-one output and must be computable without
   training a model. The ML block is optional intelligence layered on top.
2. An ML failure must not take the deterministic payload down with it.

Plus a regression pin on the reconciled `exclude_proteins` default (T010).
"""

from pathlib import Path

import pytest

import app.compute as compute
from app.compute import (
    DEFAULT_EXCLUDE_PROTEINS,
    build_result_payload,
    params_signature,
    run_analysis,
)
from app.sessions import SessionData
from core import parse_log


@pytest.fixture(scope="module")
def analysed_session():
    """A session with a small real analysis run through stage one."""
    from tests.fixtures.generate_year_log import generate

    path = generate(months=2, out=Path("/tmp/compute_fixture.xlsx"), seed=11)
    meals_df, bac_df, med_periods = parse_log(str(path))
    session = SessionData(
        meals_df=meals_df, bac_df=bac_df, med_periods=med_periods, filename="f.xlsx"
    )
    run_analysis(session, hours=3.0, min_obs=3, split_compounds=True, exclude_proteins=True)
    return session


def test_summary_is_computed_without_the_ml_block(analysed_session, monkeypatch):
    """Stage one must not invoke training to produce the summary (Principle II)."""
    def _boom(*a, **k):
        raise AssertionError("ML must not be trained for the stage-one payload")

    monkeypatch.setattr(compute, "train_personal_model", _boom)
    monkeypatch.setattr(compute, "extract_features", _boom)

    payload = build_result_payload(analysed_session, include_ml=False)
    assert payload["ml"] is None
    assert payload["summary"]["total_readings"] > 0
    assert isinstance(payload["lift_scores_overall"], list)


def test_ml_failure_leaves_the_rest_of_the_payload_intact(analysed_session, monkeypatch):
    """A crash in stage two is reported, not propagated (research R1)."""
    def _explode(*a, **k):
        raise RuntimeError("simulated model blow-up")

    monkeypatch.setattr(compute, "extract_features", _explode)

    payload = build_result_payload(analysed_session, include_ml=True)
    # Deterministic content survives.
    assert payload["summary"]["total_readings"] > 0
    assert payload["lift_scores_overall"]
    # The failure is surfaced in the ml field rather than raised.
    assert payload["ml"]["status"] == "error"
    assert "simulated model blow-up" in payload["ml"]["message"]


def test_summary_and_lift_scores_are_present_after_stage_one(analysed_session):
    payload = build_result_payload(analysed_session, include_ml=False)
    s = payload["summary"]
    for key in ("total_readings", "episodes", "unique_ingredients", "lookback_pairs"):
        assert key in s
    assert payload["lift_scores_overall"], "expected some overall lift scores"


def test_exclude_proteins_default_is_reconciled_true():
    """Regression pin: upload path, UI checkbox and AnalysisParams all default True."""
    assert DEFAULT_EXCLUDE_PROTEINS is True
    assert SessionData().exclude_proteins is True
    from app.main import AnalysisParams

    assert AnalysisParams().exclude_proteins is True


def test_params_signature_is_stable_and_sensitive():
    """Same resolved params → same key; any change → different key (FR-016)."""
    base = params_signature(3.0, 3, True, True, 2.0)
    assert base == params_signature(3.0, 3, True, True, 2.0)
    # Float encoding differences must not matter.
    assert base == params_signature(3, 3, True, True, 2)
    # Each field is part of the key.
    assert base != params_signature(4.0, 3, True, True, 2.0)
    assert base != params_signature(3.0, 5, True, True, 2.0)
    assert base != params_signature(3.0, 3, False, True, 2.0)
    assert base != params_signature(3.0, 3, True, False, 2.0)
    assert base != params_signature(3.0, 3, True, True, 2.5)


def test_exclude_proteins_actually_filters(analysed_session):
    """With exclude_proteins on, protein ingredients are absent from lift scores."""
    included = SessionData(
        meals_df=analysed_session.meals_df,
        bac_df=analysed_session.bac_df,
        med_periods=analysed_session.med_periods,
    )
    run_analysis(included, 3.0, 3, split_compounds=True, exclude_proteins=False)
    excluded = SessionData(
        meals_df=analysed_session.meals_df,
        bac_df=analysed_session.bac_df,
        med_periods=analysed_session.med_periods,
    )
    run_analysis(excluded, 3.0, 3, split_compounds=True, exclude_proteins=True)

    def _ingredients(sess):
        if sess.scores_all is None or sess.scores_all.empty:
            return set()
        return set(sess.scores_all["ingredient"].str.lower())

    proteins = {"chicken", "beef", "salmon", "eggs", "tuna", "pork", "turkey", "cod"}
    assert _ingredients(included) & proteins, "fixture should contain proteins"
    assert not (_ingredients(excluded) & proteins), "proteins should be filtered out"
