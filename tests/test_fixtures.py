"""Smoke tests for the synthetic fixture generator (T003).

The measurement that gates the executor choice (R8a) rides on this generator, so
the generator itself must be trustworthy: it must parse through the real parser
and carry the structural quirks the plan says it should exercise.
"""

from pathlib import Path

import pytest

from core import parse_log
from tests.fixtures.generate_year_log import generate


@pytest.fixture(scope="module")
def small_log(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("fixtures") / "small.xlsx"
    return generate(months=2, out=out, seed=7)


def test_generator_output_parses(small_log):
    meals_df, bac_df, med_periods = parse_log(str(small_log))
    assert not meals_df.empty
    assert not bac_df.empty
    assert {"ingredient", "meal_datetime", "carbs_g"}.issubset(meals_df.columns)
    assert {"promille", "bac_datetime", "active_medications"}.issubset(bac_df.columns)


def test_generator_is_deterministic(tmp_path):
    a = parse_log(str(generate(2, tmp_path / "a.xlsx", seed=42)))
    b = parse_log(str(generate(2, tmp_path / "b.xlsx", seed=42)))
    assert len(a[0]) == len(b[0])
    assert len(a[1]) == len(b[1])


def test_carries_the_structural_quirks(small_log):
    meals_df, bac_df, med_periods = parse_log(str(small_log))

    # Compound dishes split into separate ingredients (suffix stripped, title-cased).
    split_parts = {"Lentil", "Kale", "Tomato", "Cucumber"}
    assert split_parts & set(meals_df["ingredient"].unique()), "no compound split present"

    # Protein ingredients present, so exclude_proteins has something to filter.
    proteins = {"chicken", "beef", "salmon", "eggs", "tuna", "pork", "turkey", "cod"}
    assert meals_df["ingredient"].str.lower().isin(proteins).any()

    # Partially filled nutrients: some ingredient rows lack carbs.
    assert meals_df["carbs_g"].isna().any(), "expected some blank nutrient cells"

    # Episodes present (readings >= 2.0 permille).
    assert bool(bac_df["episode"].any())

    # Medication periods parsed, so period-split analysis is exercised.
    assert med_periods, "expected medication periods"
    assert (bac_df["active_medications"] != "none").any()


def test_scale_grows_with_months(tmp_path):
    small = parse_log(str(generate(1, tmp_path / "1.xlsx", seed=1)))
    large = parse_log(str(generate(6, tmp_path / "6.xlsx", seed=1)))
    assert len(large[1]) > len(small[1]) * 3
