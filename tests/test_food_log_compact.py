"""get_food_log detail="compact" and the food_id filter."""

import json
from datetime import date
from unittest.mock import Mock

import pytest

from cronometer_api_mcp import server
from cronometer_api_mcp.client import CronometerClient


def _nutrients(kcal):
    # A big profile, so size comparisons are meaningful.
    base = [
        {"id": 208, "name": "Energy", "amount": kcal, "unit": "kcal"},
        {"id": 203, "name": "Protein", "amount": 12.34, "unit": "g"},
        {"id": 205, "name": "Carbs", "amount": 45.0, "unit": "g"},
        {"id": 204, "name": "Fat", "amount": 6.66, "unit": "g"},
        {"id": 307, "name": "Sodium", "amount": 321.0, "unit": "mg"},
    ]
    return base + [
        {"id": 1000 + i, "name": f"Nutrient {i}", "amount": i, "unit": "mg"}
        for i in range(80)
    ]


def _diary():
    return {
        "summary": {"macros": {"energy": 2200}, "consumed": {"total": 1800}},
        "diary": [
            {
                "type": "Serving",
                "servingId": "s1",
                "foodId": 1,
                "measureId": 10,
                "grams": 100,
                "order": (1 << 16) | 1,
                "time": "8:00:00",
                "name": "Oats",
                "servings": 1.0,
                "nutrients": _nutrients(380),
            },
            {
                "type": "Serving",
                "servingId": "s2",
                "foodId": 2,
                "measureId": 20,
                "grams": 150,
                "order": (3 << 16) | 1,
                "time": "19:30:00",
                "name": "Pasta",
                "servings": 1.5,
                "nutrients": _nutrients(520),
            },
            {
                "type": "Serving",
                "servingId": "s3",
                "foodId": 3,
                "measureId": 30,
                "grams": None,
                "order": (4 << 16) | 2,
                "time": "15:00:00",
                "name": "Recipe",
                "nutrients": _nutrients(210),
            },
            {"type": "Exercise", "exerciseId": 7, "name": "Run", "order": 0},
        ],
    }


def _label(data):
    """Stand-in for label_diary_groups: names from the group number."""
    names = {1: "Breakfast", 3: "Dinner", 4: "Snacks"}
    for entry in data["diary"]:
        group = entry["order"] >> 16
        entry["diary_group"] = {"group": group, "name": names.get(group)}
    return data


@pytest.fixture
def client(monkeypatch):
    c = Mock(spec=CronometerClient)
    c.today.return_value = date(2026, 1, 1)
    c.get_diary.side_effect = lambda day: _diary()
    c.enrich_diary_servings.side_effect = lambda data: data
    c.label_diary_groups.side_effect = _label
    c.get_consumed_nutrients.return_value = {"macros": {"energy": 1800}}
    monkeypatch.setattr(server, "_client", c)
    return c


def _entries(**kw):
    return json.loads(server.get_food_log(date="2026-01-01", **kw))["diary"]["diary"]


def test_default_is_full(client):
    entries = _entries()
    assert len(entries) == 4
    assert len(entries[0]["nutrients"]) == 85


def test_compact_fields(client):
    entries = _entries(detail="compact")
    assert entries[0] == {
        "entry_id": "s1",
        "type": "Serving",
        "name": "Oats",
        "food_id": 1,
        "measure_id": 10,
        "grams": 100,
        "servings": 1.0,
        "time": "8:00:00",
        "order": (1 << 16) | 1,
        "diary_group": {"group": 1, "name": "Breakfast"},
        "kcal": 380.0,
        "protein_g": 12.3,
        "carbs_g": 45.0,
        "fat_g": 6.7,
        "sodium_mg": 321.0,
    }
    assert entries[2]["grams"] is None and entries[2]["servings"] is None
    assert entries[3]["kcal"] is None
    assert all("nutrients" not in e for e in entries)


def test_compact_entry_ids_cover_every_entry_type(client):
    diary = _diary()
    diary["diary"] += [
        {"type": "Note", "noteId": 42, "text": "ate late", "order": 1 << 16},
        {"type": "Biometric", "biometricId": 9, "order": 0},
    ]
    client.get_diary.side_effect = lambda day: diary
    entries = _entries(detail="compact")
    assert [e["entry_id"] for e in entries[3:]] == [7, 42, 9]
    assert entries[4]["text"] == "ate late"
    assert all("text" not in e for e in entries[:4] + entries[5:])


def test_compact_keeps_summaries_and_is_much_smaller(client):
    full = server.get_food_log(date="2026-01-01")
    compact = server.get_food_log(date="2026-01-01", detail="compact")
    assert len(compact) < len(full) / 4
    out = json.loads(compact)
    assert out["energy_summary"]["remaining_kcal"] == 400
    assert out["nutrition_summary"]["macros"]["energy"] == 1800


def test_compact_without_group_labels(client):
    """Group labels are best-effort: an unlabeled diary still compacts."""
    client.label_diary_groups.side_effect = lambda data: data
    entries = _entries(detail="compact")
    assert entries[0]["diary_group"] is None
    assert entries[0]["entry_id"] == "s1"


def test_food_id_filter(client):
    assert [e["food_id"] for e in _entries(food_id=2, detail="compact")] == [2]
    assert [e["servingId"] for e in _entries(food_id=1)] == ["s1"]
    assert _entries(food_id=99) == []


def test_food_id_filter_runs_before_enrichment(client):
    seen = []
    client.enrich_diary_servings.side_effect = lambda data: (
        seen.append([e["foodId"] for e in data["diary"]]) or data
    )
    _entries(food_id=2)
    assert seen == [[2]]


def test_invalid_detail_rejected(client):
    out = json.loads(server.get_food_log(date="2026-01-01", detail="tiny"))
    assert out["status"] == "error"
    client.get_diary.assert_not_called()


def test_multi_day_applies_compact_to_each_day(client):
    out = json.loads(server.get_food_log(date="2026-01-01", days=2, detail="compact"))
    assert [d["date"] for d in out["days"]] == ["2025-12-31", "2026-01-01"]
    for d in out["days"]:
        assert all("nutrients" not in e for e in d["diary"]["diary"])
