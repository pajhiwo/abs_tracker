"""Core analysis engine for ABS Diet Tracker."""

from core.parser import (
    parse_log,
    parse_medication_events,
    build_medication_periods,
    get_active_medications,
    EPISODE_THRESHOLD,
    C_DATE,
    C_MEAL,
    C_MEAL_TIME,
    C_PRODUCT,
    C_MEASURE,
    C_GRAMS,
    C_CALORIES,
    C_PROTEIN,
    C_FAT,
    C_SAT_FAT,
    C_CARBS,
    C_SUGARS,
    C_FIBRE,
    C_BAC_TIME,
    C_BAC_VAL,
    C_EPISODE,
    C_MEDICATION,
    C_COMMENT,
    MEAL_LABELS,
    HEADER_STRINGS,
    MEDICATION_ALIASES,
)
from core.analysis import map_lookback, compute_lift_scores

__all__ = [
    "parse_log",
    "parse_medication_events",
    "build_medication_periods",
    "get_active_medications",
    "map_lookback",
    "compute_lift_scores",
]
