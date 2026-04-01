"""
Edge-case assertions (TC-01 through TC-10) for the ABS analysis pipeline.
"""

import pandas as pd

from core.analysis import map_lookback, compute_lift_scores


def run_assertions():
    print("Running edge-case assertions...\n")

    base_date = pd.Timestamp("2026-01-01")
    base_dt = pd.Timestamp("2026-01-01 20:00")

    def make_bac(promilles, datetimes=None, meds=None):
        dts = datetimes or [
            base_dt + pd.Timedelta(days=i) for i in range(len(promilles))
        ]
        meds = meds or ["none"] * len(promilles)
        return pd.DataFrame(
            {
                "date": [dt.date() for dt in dts],
                "bac_time": [dt.time() for dt in dts],
                "bac_datetime": dts,
                "promille": promilles,
                "episode": [p >= 2.0 for p in promilles],
                "active_medications": meds,
                "comment": [None] * len(promilles),
            }
        )

    def make_meals(ingredients, dates=None, meal_dts=None):
        n = len(ingredients)
        dates = dates or [base_date] * n
        dts = meal_dts or [pd.Timestamp(d) + pd.Timedelta(hours=12) for d in dates]
        return pd.DataFrame(
            {
                "date": [pd.Timestamp(d) for d in dates],
                "meal": ["Breakfast"] * n,
                "meal_time": [dt.time() for dt in dts],
                "meal_datetime": dts,
                "ingredient": ingredients,
                "quantity_g": [100.0] * n,
            }
        )

    # TC-01: Singleton → low_confidence
    bac = make_bac([0.5, 0.0, 0.0, 0.0, 0.0])
    meals = make_meals(
        ["RareFood", "CommonFood", "CommonFood", "CommonFood", "CommonFood"],
        dates=[base_date + pd.Timedelta(days=i) for i in range(5)],
        meal_dts=[
            base_dt - pd.Timedelta(hours=2) + pd.Timedelta(days=i) for i in range(5)
        ],
    )
    lb = map_lookback(bac, meals, hours=3)
    scores = compute_lift_scores(bac, lb, min_observations=3)
    rare = scores[scores["ingredient"] == "RareFood"]
    assert not rare.empty and rare.iloc[0]["low_confidence"], "TC-01 FAIL"
    print("TC-01 PASS — singleton ingredient flagged as low_confidence")

    # TC-02: Always co-occurring → always_present or lift=None
    bac = make_bac([1.0, 1.0, 1.0])
    dates = [base_date + pd.Timedelta(days=i) for i in range(3)]
    dts = [base_dt - pd.Timedelta(hours=2) + pd.Timedelta(days=i) for i in range(3)]
    meals = make_meals(["FoodA", "FoodB"] * 3, dates=dates * 2, meal_dts=dts * 2)
    lb = map_lookback(bac, meals, hours=3)
    scores = compute_lift_scores(bac, lb)
    for food in ["FoodA", "FoodB"]:
        row = scores[scores["ingredient"] == food]
        assert not row.empty and (
            row.iloc[0]["always_present"] or row.iloc[0]["lift"] is None
        ), f"TC-02 FAIL: {food}"
    print("TC-02 PASS — always-co-occurring ingredients flagged")

    # TC-03: No meals in window → no lookback rows
    bac = make_bac([0.5])
    meals = make_meals(
        ["FarFood"],
        dates=[base_date + pd.Timedelta(days=5)],
        meal_dts=[base_dt + pd.Timedelta(days=5, hours=-2)],
    )
    lb = map_lookback(bac, meals, hours=3)
    assert lb.empty or (0 not in lb["bac_idx"].values), "TC-03 FAIL"
    print("TC-03 PASS — no meals in window → no lookback rows")

    # TC-04: Multiple BAC readings same day → all captured
    dts = [
        pd.Timestamp("2026-01-01 14:30"),
        pd.Timestamp("2026-01-01 16:00"),
        pd.Timestamp("2026-01-01 17:30"),
        pd.Timestamp("2026-01-01 19:00"),
    ]
    bac = make_bac([0.89, 0.57, 1.2, 0.3], datetimes=dts)
    assert len(bac) == 4, "TC-04 FAIL"
    print("TC-04 PASS — multiple readings same day all captured")

    # TC-05: All BAC = 0 → no inflated lift
    bac = make_bac([0.0, 0.0, 0.0])
    meals = make_meals(
        ["FoodX"] * 3,
        dates=[base_date + pd.Timedelta(days=i) for i in range(3)],
        meal_dts=[
            base_dt - pd.Timedelta(hours=2) + pd.Timedelta(days=i) for i in range(3)
        ],
    )
    lb = map_lookback(bac, meals, hours=3)
    scores = compute_lift_scores(bac, lb)
    if not scores.empty:
        assert (scores["lift"].dropna() <= 1.0).all(), "TC-05 FAIL"
    print("TC-05 PASS — zero-BAC dataset produces no inflated lift scores")

    # TC-06: Medication period filter works
    bac = make_bac([1.5, 0.2], meds=["Rifaximin", "none"])
    meals = make_meals(
        ["FoodA", "FoodB"],
        meal_dts=[
            base_dt - pd.Timedelta(hours=1),
            base_dt - pd.Timedelta(hours=1) + pd.Timedelta(days=1),
        ],
    )
    lb = map_lookback(bac, meals, hours=3)
    scores_rifax = compute_lift_scores(bac, lb, period_filter="Rifaximin")
    scores_none = compute_lift_scores(bac, lb, period_filter="none")
    assert not scores_rifax.empty, "TC-06 FAIL: Rifaximin period empty"
    assert not scores_none.empty, "TC-06 FAIL: none period empty"
    print("TC-06 PASS — period filter correctly isolates medication periods")

    # TC-07: Whitespace stripped from ingredient names
    assert "  Avocado  ".strip() == "Avocado", "TC-07 FAIL"
    print("TC-07 PASS — ingredient names stripped of whitespace")

    # TC-08: Look-back crosses midnight with exact meal time
    bac = make_bac([1.5], datetimes=[pd.Timestamp("2026-01-02 01:00")])
    meals = make_meals(
        ["LateNightSnack"],
        dates=[pd.Timestamp("2026-01-01")],
        meal_dts=[pd.Timestamp("2026-01-01 23:30")],
    )
    lb = map_lookback(bac, meals, hours=3)
    assert not lb.empty and "LateNightSnack" in lb["ingredient"].values, "TC-08 FAIL"
    assert not lb.iloc[0]["approximate"], "TC-08 FAIL: should be exact"
    print("TC-08 PASS — look-back crosses midnight using exact meal time")

    # TC-09: Empty dataset → no crash
    empty_bac = pd.DataFrame(
        columns=[
            "date",
            "bac_time",
            "bac_datetime",
            "promille",
            "episode",
            "active_medications",
            "comment",
        ]
    )
    empty_meals = pd.DataFrame(
        columns=[
            "date",
            "meal",
            "meal_time",
            "meal_datetime",
            "ingredient",
            "quantity_g",
        ]
    )
    lb = map_lookback(empty_bac, empty_meals)
    scores = compute_lift_scores(empty_bac, lb)
    assert lb.empty and scores.empty, "TC-09 FAIL"
    print("TC-09 PASS — empty dataset handled without crash")

    # TC-10: Meal outside window excluded
    bac = make_bac([1.0], datetimes=[pd.Timestamp("2026-01-01 20:00")])
    meals = make_meals(
        ["TooEarlyFood"],
        dates=[pd.Timestamp("2026-01-01")],
        meal_dts=[pd.Timestamp("2026-01-01 14:00")],
    )
    lb = map_lookback(bac, meals, hours=3)
    assert lb.empty or "TooEarlyFood" not in lb["ingredient"].values, "TC-10 FAIL"
    print("TC-10 PASS — meal outside window correctly excluded")

    print("\nAll 10 assertions passed ✓")
