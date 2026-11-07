"""get_macro_split / set_macro_split: the macroProtein, macroCarbs and
macroLipids profile prefs, written with /api/v2/set_pref."""

from __future__ import annotations

import json

import httpx
import pytest
from test_client import make_cold_client

from cronometer_api_mcp.client import CronometerError

SET_OK = {"result": "SUCCESS", "messages": []}
ORIGINAL_PREFS = {
    "macroIndex": "6",
    "macroProtein": "2.0",
    "macroCarbs": "4.0",
    "macroLipids": "4.0",
    "tz": 0,
}


def profile(prefs=None):
    prefs = ORIGINAL_PREFS if prefs is None else prefs
    return {"prefs": [{key: value} for key, value in prefs.items()]}


def make_macro_client(
    tmp_path,
    *,
    prefs=None,
    fail_writes=(),
    timeout_writes=(),
    fail_readback=False,
):
    client, _ = make_cold_client(tmp_path, [SET_OK])
    state = {
        "prefs": dict(ORIGINAL_PREFS if prefs is None else prefs),
        "writes": [],
        "reads": 0,
    }

    def post(endpoint, json):
        request = httpx.Request("POST", f"https://example.test{endpoint}")
        if endpoint == "/api/v2/get_profile":
            state["reads"] += 1
            if fail_readback and state["reads"] > 1:
                raise httpx.ReadTimeout("profile unavailable", request=request)
            return httpx.Response(200, json=profile(state["prefs"]), request=request)
        assert endpoint == "/api/v2/set_pref"
        state["writes"].append((json["key"], json["val"]))
        number = len(state["writes"])
        if number in fail_writes:
            return httpx.Response(429, request=request)
        state["prefs"][json["key"]] = json["val"]
        if number in timeout_writes:
            raise httpx.ReadTimeout("response lost after commit", request=request)
        return httpx.Response(200, json=SET_OK, request=request)

    client._http.post = post
    return client, state


def test_get_macro_split_reads_profile_prefs(tmp_path):
    profile = {
        "prefs": [
            {"macroLipids": 30.0},
            {"tz": 0},
            {"macroProtein": "30.0"},
            {"macroCarbs": "40.0"},
        ]
    }
    client, _ = make_cold_client(tmp_path, [profile])

    assert client.get_macro_split() == {"protein": 30.0, "carbs": 40.0, "fat": 30.0}


def test_get_macro_split_normalizes_ratios(tmp_path):
    client, _ = make_cold_client(tmp_path, [profile()])

    assert client.get_macro_split() == {"protein": 20.0, "carbs": 40.0, "fat": 40.0}


@pytest.mark.parametrize(
    "prefs",
    [
        {},
        {"macroProtein": "25"},
        {"macroProtein": "0", "macroCarbs": "0", "macroLipids": "0"},
    ],
)
def test_get_macro_split_unknown_total_returns_unknown_shares(tmp_path, prefs):
    client, _ = make_cold_client(tmp_path, [profile(prefs)])

    assert client.get_macro_split() == {"protein": None, "carbs": None, "fat": None}


def test_get_macro_split_preserves_zero_share(tmp_path):
    client, _ = make_cold_client(
        tmp_path,
        [profile({"macroProtein": "0", "macroCarbs": "1", "macroLipids": "1"})],
    )

    assert client.get_macro_split() == {"protein": 0.0, "carbs": 50.0, "fat": 50.0}


def test_set_macro_split_writes_all_three_prefs(tmp_path):
    client, state = make_cold_client(tmp_path, [profile(), SET_OK])

    result = client.set_macro_split(25, 45, 30)

    assert [(p["key"], p["val"]) for p in state["payloads"][1:]] == [
        ("macroProtein", "25.0"),
        ("macroCarbs", "45.0"),
        ("macroLipids", "30.0"),
    ]
    assert all(p["config"] == {"call_version": 1} for p in state["payloads"])
    assert result == {"protein": 25, "carbs": 45, "fat": 30}


@pytest.mark.parametrize("mode", [0, "0", 5, "5", None])
def test_set_macro_split_rejects_other_modes_before_writing(tmp_path, mode):
    prefs = {**ORIGINAL_PREFS, "macroIndex": mode}
    client, state = make_macro_client(tmp_path, prefs=prefs)

    with pytest.raises(CronometerError, match="requires Macro Ratios mode"):
        client.set_macro_split(30, 40, 30)

    assert state["writes"] == []
    assert state["prefs"] == prefs


def test_set_macro_split_accepts_numeric_ratio_mode(tmp_path):
    client, state = make_macro_client(
        tmp_path, prefs={**ORIGINAL_PREFS, "macroIndex": 6}
    )

    assert client.set_macro_split(30, 40, 30) == {"protein": 30, "carbs": 40, "fat": 30}
    assert client.get_macro_split() == {"protein": 30.0, "carbs": 40.0, "fat": 30.0}
    assert state["prefs"]["macroIndex"] == 6


@pytest.mark.parametrize(
    ("split", "message"),
    [
        ((30, 30, 30), "add up to 100"),
        ((-5, 70, 35), "between 0 and 100"),
        ((float("nan"), 40, 30), "between 0 and 100"),
        ((float("inf"), 40, 30), "between 0 and 100"),
    ],
)
def test_set_macro_split_validates_before_any_request(tmp_path, split, message):
    client, state = make_cold_client(tmp_path, [SET_OK])

    with pytest.raises(ValueError, match=message):
        client.set_macro_split(*split)
    assert state["payloads"] == []


def test_set_macro_split_raises_on_unexpected_response(tmp_path):
    client, _ = make_cold_client(
        tmp_path, [profile(), {"messages": []}, SET_OK, profile()]
    )

    with pytest.raises(CronometerError, match="macroProtein"):
        client.set_macro_split(30, 40, 30)


@pytest.mark.parametrize("failed_write", [1, 2, 3])
def test_failed_write_restores_raw_ratios(tmp_path, failed_write):
    client, state = make_macro_client(tmp_path, fail_writes=(failed_write,))

    with pytest.raises(CronometerError, match="Previous macro split restored"):
        client.set_macro_split(30, 40, 30)

    assert state["prefs"] == ORIGINAL_PREFS
    assert state["reads"] == 2
    assert len(state["writes"]) == failed_write * 2
    assert client.get_macro_split() == {"protein": 20.0, "carbs": 40.0, "fat": 40.0}


@pytest.mark.parametrize("timed_out_write", [1, 2, 3])
def test_timeout_after_commit_restores_the_uncertain_write(tmp_path, timed_out_write):
    client, state = make_macro_client(tmp_path, timeout_writes=(timed_out_write,))

    with pytest.raises(CronometerError, match="Previous macro split restored"):
        client.set_macro_split(30, 40, 30)

    assert state["prefs"] == ORIGINAL_PREFS


def test_failed_restoration_reports_partial_state_through_server(tmp_path, monkeypatch):
    from cronometer_api_mcp import server

    client, state = make_macro_client(tmp_path, fail_writes=(3, 4))
    monkeypatch.setattr(server, "_client", client)

    result = json.loads(server.set_macro_split(30, 40, 30))

    assert result["status"] == "error"
    message = result["message"]
    assert "Partial macro update remains" in message
    assert "restoring macroProtein failed" in message
    assert 'Current raw ratios: {"protein": 30.0, "carbs": 4.0, "fat": 4.0}' in message
    assert "current split (%)" in message
    assert '"protein": 78.947' in message
    assert state["prefs"] == {**ORIGINAL_PREFS, "macroProtein": "30.0"}
    assert len(state["writes"]) == 6


def test_failed_readback_reports_unknown_state_through_server(tmp_path, monkeypatch):
    from cronometer_api_mcp import server

    client, _ = make_macro_client(tmp_path, fail_writes=(2, 3), fail_readback=True)
    monkeypatch.setattr(server, "_client", client)

    result = json.loads(server.set_macro_split(30, 40, 30))

    assert result["status"] == "error"
    assert "Current macro split is unknown; changes may remain" in result["message"]
    assert "readback failed" in result["message"]
    assert "restoring macroProtein failed" in result["message"]


def test_unset_original_preference_reports_incomplete_recovery(tmp_path):
    prefs = dict(ORIGINAL_PREFS)
    del prefs["macroProtein"]
    client, state = make_macro_client(tmp_path, prefs=prefs, fail_writes=(2,))

    with pytest.raises(CronometerError) as error:
        client.set_macro_split(30, 40, 30)

    assert "cannot restore unset macroProtein" in str(error.value)
    assert "Partial macro update remains" in str(error.value)
    assert state["prefs"]["macroProtein"] == "30.0"
    assert state["prefs"]["macroCarbs"] == "4.0"
