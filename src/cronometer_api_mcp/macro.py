"""Macro ratio parsing, normalization and split validation."""

import math

# Profile prefs holding energy ratios, not necessarily totaling 100.
MACRO_SPLIT_PREFS = {
    "protein": "macroProtein",
    "carbs": "macroCarbs",
    "fat": "macroLipids",
}


def macro_ratios(prefs: dict) -> dict:
    return {
        name: float(prefs[key]) if prefs.get(key) is not None else None
        for name, key in MACRO_SPLIT_PREFS.items()
    }


def macro_percentages(ratios: dict) -> dict:
    unknown = dict.fromkeys(ratios)
    if any(v is None or not math.isfinite(v) or v < 0 for v in ratios.values()):
        return unknown
    total = sum(ratios.values())
    if not total or not math.isfinite(total):
        return unknown
    return {name: value / total * 100 for name, value in ratios.items()}


def validate_macro_split(split: dict) -> None:
    for name, value in split.items():
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError(f"{name} must be between 0 and 100, got {value}")
    if abs(sum(split.values()) - 100) > 0.01:
        raise ValueError(
            f"protein, carbs and fat must add up to 100, got {sum(split.values())}"
        )
