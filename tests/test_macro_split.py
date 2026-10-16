"""get_macro_split / set_macro_split: the macroProtein, macroCarbs and
macroLipids profile prefs, written with /api/v2/set_pref."""

from __future__ import annotations

import pytest
from test_client import make_cold_client

from cronometer_api_mcp.client import CronometerError

SET_OK = {"result": "SUCCESS", "messages": []}


def test_get_macro_split_reads_profile_prefs(tmp_path):
    profile = {
        "prefs": [
            {"macroLipids": 30.0},
            {"tz": 120},
            {"macroProtein": "27.0"},
            {"macroCarbs": "43.0"},
        ]
    }
    client, _ = make_cold_client(tmp_path, [profile])

    assert client.get_macro_split() == {"protein": 27.0, "carbs": 43.0, "fat": 30.0}


def test_get_macro_split_unset_share_is_none(tmp_path):
    client, _ = make_cold_client(tmp_path, [{"prefs": [{"macroProtein": "25"}]}])

    assert client.get_macro_split() == {"protein": 25.0, "carbs": None, "fat": None}


def test_set_macro_split_writes_all_three_prefs(tmp_path):
    client, state = make_cold_client(tmp_path, [SET_OK])

    result = client.set_macro_split(26, 44, 30)

    assert [(p["key"], p["val"]) for p in state["payloads"]] == [
        ("macroProtein", "26.0"),
        ("macroCarbs", "44.0"),
        ("macroLipids", "30.0"),
    ]
    assert all(p["config"] == {"call_version": 1} for p in state["payloads"])
    assert result == {"protein": 26, "carbs": 44, "fat": 30}


@pytest.mark.parametrize(
    ("split", "message"),
    [((30, 30, 30), "add up to 100"), ((-5, 70, 35), "between 0 and 100")],
)
def test_set_macro_split_validates_before_any_request(tmp_path, split, message):
    client, state = make_cold_client(tmp_path, [SET_OK])

    with pytest.raises(ValueError, match=message):
        client.set_macro_split(*split)
    assert state["payloads"] == []


def test_set_macro_split_raises_on_unexpected_response(tmp_path):
    client, _ = make_cold_client(tmp_path, [{"messages": []}])

    with pytest.raises(CronometerError, match="macroProtein"):
        client.set_macro_split(27, 43, 30)
