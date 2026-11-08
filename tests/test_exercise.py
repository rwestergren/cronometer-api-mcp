"""get_activities / add_exercise."""

from __future__ import annotations

from datetime import date

import pytest
from test_client import make_cold_client

from cronometer_api_mcp.client import CronometerError

FOREST = {
    "activitiesMap": {
        "1342": {"id": 1342, "name": "Treadmill Walking", "calories": 0},
    },
    "groupsMap": {},
}
LEGACY = {"activities": [{"id": 12, "name": "Walking 3.0 mph", "cals": 1.5}]}
PROFILE = {"prefs": [{"tz": 0}, {"weightInKG": 80.0}]}
ADDED = {"messages": [], "id": 881403934}


def test_get_activities_merges_both_catalogs(tmp_path):
    client, _ = make_cold_client(tmp_path, [FOREST, LEGACY])

    assert client.get_activities() == [
        {"id": 1342, "name": "Treadmill Walking", "legacy": False},
        {"id": 12, "name": "Walking 3.0 mph", "legacy": True},
    ]


def test_add_exercise_sends_override_calories_and_profile_weight(tmp_path):
    client, state = make_cold_client(tmp_path, [PROFILE, ADDED])

    result = client.add_exercise(1342, 15, 40, date(1900, 1, 2), diary_group=2)

    sent = state["payloads"][1]
    assert sent["config"] == {"call_version": 1}
    ex = sent["exercise"]
    assert ex["activityId"] == 1342
    assert ex["minutes"] == 15
    assert ex["calories"] == -40
    assert ex["calorieOverride"] is True
    assert ex["weight"] == 176.4  # 80 kg in lb
    assert ex["day"] == "1900-1-2"
    assert ex["order"] == (2 << 16) | 1
    assert ex["exerciseId"] is None
    assert result == {
        "exerciseId": 881403934,
        "activityId": 1342,
        "minutes": 15,
        "calories": 40,
        "day": "1900-01-02",
    }


def test_add_exercise_needs_a_profile_weight(tmp_path):
    client, state = make_cold_client(tmp_path, [{"prefs": [{"tz": 0}]}, ADDED])

    with pytest.raises(CronometerError, match="no weight"):
        client.add_exercise(1342, 15, 40, diary_group=1)
    assert len(state["payloads"]) == 1


@pytest.mark.parametrize(
    ("minutes", "calories", "message"),
    [(0, 10, "minutes"), (10, -5, "calories"), (float("nan"), 10, "minutes")],
)
def test_add_exercise_validates_before_any_request(
    tmp_path, minutes, calories, message
):
    client, state = make_cold_client(tmp_path, [PROFILE, ADDED])

    with pytest.raises(ValueError, match=message):
        client.add_exercise(1342, minutes, calories, diary_group=1)
    assert state["payloads"] == []
