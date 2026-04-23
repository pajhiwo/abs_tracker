"""
Excel log parser — reads the ABS diet log into clean DataFrames.
"""

import datetime
import warnings
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

# ---------------------------------------------------------------------------
# Column indices (0-based) — matches jo_log.xlsx
# ---------------------------------------------------------------------------
C_DATE = 0
C_MEAL = 1
C_MEAL_TIME = 2
C_PRODUCT = 3
C_MEASURE = 4
C_GRAMS = 5
C_CALORIES = 6
C_PROTEIN = 7
C_FAT = 8
C_SAT_FAT = 9
C_CARBS = 10
C_SUGARS = 11
C_FIBRE = 12
C_BAC_TIME = 13
C_BAC_VAL = 14
C_EPISODE = 15
C_MEDICATION = 16
C_COMMENT = 17

MEAL_LABELS = {"Breakfast", "Snack", "Lunch", "Dinner"}
HEADER_STRINGS = {"Date", "Meal", "Espisode", "Episode"}

# Known medication spelling corrections (lowercase) → canonical name.
# New medications are auto-detected from the Excel; this dict only normalises
# known misspellings / abbreviations.
MEDICATION_ALIASES = {
    "activated charcol": "Activated Charcoal",
    "charcol": "Activated Charcoal",
    "charcoal": "Activated Charcoal",
    "vancomicin": "Vancomycin",
}


# ---------------------------------------------------------------------------
# parse_medication_events
# ---------------------------------------------------------------------------
def parse_medication_events(raw: pd.DataFrame) -> list[dict]:
    """
    Scan date rows for medication entries and return a list of events:
        [{"date": date, "medication": str, "action": "start"|"stop"}, ...]
    Multiple medications on one row (comma-separated) are split into separate events.

    Medication names are auto-detected by stripping "start"/"stop" from the
    cell text, so new medications don't require code changes.
    """
    events = []
    date_rows = raw[raw[C_DATE].apply(lambda v: isinstance(v, datetime.datetime))]

    for _, row in date_rows.iterrows():
        med_raw = row[C_MEDICATION]
        if pd.isna(med_raw):
            continue
        date = row[C_DATE].date()
        for part in str(med_raw).split(","):
            part = part.strip()
            part_lower = part.lower()
            action = None
            if "start" in part_lower:
                action = "start"
            elif "stop" in part_lower:
                action = "stop"
            else:
                continue

            # Check aliases first for known misspellings
            med_name = None
            for alias, canonical in MEDICATION_ALIASES.items():
                if alias in part_lower:
                    med_name = canonical
                    break

            # Auto-detect: strip "start"/"stop" and use remaining text as name
            if med_name is None:
                name = part_lower.replace("start", "").replace("stop", "").strip()
                name = name.strip("-–— ")  # remove stray separators
                if name:
                    med_name = name.title()

            if med_name and action:
                events.append({"date": date, "medication": med_name, "action": action})

    return sorted(events, key=lambda e: e["date"])


# ---------------------------------------------------------------------------
# build_medication_periods
# ---------------------------------------------------------------------------
def build_medication_periods(events: list[dict]) -> dict[str, list[dict]]:
    """
    Convert start/stop events into date ranges per medication.
    Returns: {medication_name: [{"start": date, "stop": date|None}, ...]}
    """
    periods: dict[str, list[dict]] = {}
    open_starts: dict[str, datetime.date] = {}

    for event in events:
        med = event["medication"]
        date = event["date"]

        if event["action"] == "start":
            open_starts[med] = date

        elif event["action"] == "stop":
            if med in open_starts:
                periods.setdefault(med, []).append(
                    {
                        "start": open_starts.pop(med),
                        "stop": date,
                    }
                )

    # Any medication still open at end of data → stop=None (ongoing)
    for med, start in open_starts.items():
        periods.setdefault(med, []).append({"start": start, "stop": None})

    return periods


# ---------------------------------------------------------------------------
# get_active_medications
# ---------------------------------------------------------------------------
def get_active_medications(
    date: datetime.date,
    periods: dict[str, list[dict]],
) -> list[str]:
    """Return list of medications active on a given date."""
    active = []
    for med, ranges in periods.items():
        for r in ranges:
            stop = r["stop"] or datetime.date(9999, 12, 31)
            if r["start"] <= date <= stop:
                active.append(med)
                break
    return sorted(active)


# ---------------------------------------------------------------------------
# parse_log
# ---------------------------------------------------------------------------
def parse_log(
    path: str | Path,
    *,
    split_compounds: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """
    Parse the Excel log into two clean DataFrames plus medication periods.

    Parameters
    ----------
    path             : path to the .xlsx log file
    split_compounds  : if True, split ingredient names containing "&" into
                       separate rows (e.g. "A & B soup" → "A", "B")

    Returns
    -------
    meals_df    : one row per ingredient
    bac_df      : one row per BAC reading, with active_medications column
    med_periods : {medication: [{start, stop}, ...]}
    """
    raw = pd.read_excel(path, sheet_name=0, header=None)
    raw = raw[~raw[C_DATE].astype(str).isin(HEADER_STRINGS)].reset_index(drop=True)

    # Build medication periods first
    events = parse_medication_events(raw)
    med_periods = build_medication_periods(events)

    meals_rows = []
    bac_rows = []

    current_date = None
    current_meal = None
    current_meal_time = None
    current_meal_dt = None

    for _, row in raw.iterrows():
        has_date = pd.notna(row[C_DATE]) and isinstance(row[C_DATE], datetime.datetime)
        has_meal = pd.notna(row[C_MEAL]) and str(row[C_MEAL]) in MEAL_LABELS
        has_product = pd.notna(row[C_PRODUCT])
        has_bac = pd.notna(row[C_BAC_VAL])

        if has_date:
            current_date = row[C_DATE].date()
            current_meal = None
            current_meal_time = None
            current_meal_dt = None

        if has_meal and current_date is not None:
            current_meal = str(row[C_MEAL])
            raw_time = row[C_MEAL_TIME]
            if isinstance(raw_time, datetime.time):
                current_meal_time = raw_time
                current_meal_dt = datetime.datetime.combine(current_date, raw_time)
            else:
                current_meal_time = None
                current_meal_dt = None

        if has_bac and current_date is not None:
            bac_time = row[C_BAC_TIME]
            bac_dt = (
                datetime.datetime.combine(current_date, bac_time)
                if isinstance(bac_time, datetime.time)
                else None
            )
            episode_raw = row[C_EPISODE]
            is_episode = (
                pd.notna(episode_raw) and str(episode_raw).strip().lower() == "yes"
            )
            active_meds = get_active_medications(current_date, med_periods)
            bac_rows.append(
                {
                    "date": current_date,
                    "bac_time": bac_time,
                    "bac_datetime": bac_dt,
                    "promille": float(row[C_BAC_VAL]),
                    "episode": is_episode,
                    "active_medications": (
                        ", ".join(active_meds) if active_meds else "none"
                    ),
                    "comment": row[C_COMMENT] if pd.notna(row[C_COMMENT]) else None,
                }
            )

        if has_product and not has_date and not has_meal and current_date is not None:
            grams = row[C_GRAMS] if pd.notna(row[C_GRAMS]) else None
            raw_name = str(row[C_PRODUCT]).strip()

            # Split compound ingredients on "&" (e.g. "Zuccini & Chicken & Spinach soup")
            # The trailing word after the last ingredient (e.g. "soup") is stripped from each part
            if split_compounds and "&" in raw_name:
                parts = [p.strip() for p in raw_name.split("&")]
                # Check if the last part ends with a shared suffix like "soup"
                last_part = parts[-1]
                suffix = ""
                last_words = last_part.rsplit(None, 1)
                if len(last_words) == 2:
                    suffix = last_words[1].lower()
                    # Only treat as suffix if it's a known dish type
                    if suffix in ("soup", "stew", "salad", "bowl", "mix"):
                        parts[-1] = last_words[0]  # remove suffix from last part
                    else:
                        suffix = ""
                ingredient_names = [p.strip().title() for p in parts if p.strip()]
            else:
                ingredient_names = [raw_name]

            for ingredient in ingredient_names:
                meals_rows.append(
                    {
                        "date": current_date,
                        "meal": current_meal,
                        "meal_time": current_meal_time,
                        "meal_datetime": current_meal_dt,
                        "ingredient": ingredient,
                        "quantity_g": float(grams) if grams is not None else None,
                    }
                )

    meals_df = pd.DataFrame(meals_rows)
    bac_df = pd.DataFrame(bac_rows)

    if not meals_df.empty:
        meals_df["date"] = pd.to_datetime(meals_df["date"])
    if not bac_df.empty:
        bac_df["date"] = pd.to_datetime(bac_df["date"])
        bac_df["bac_datetime"] = pd.to_datetime(bac_df["bac_datetime"])
        bac_df = bac_df.sort_values("bac_datetime").reset_index(drop=True)

    return meals_df, bac_df, med_periods
