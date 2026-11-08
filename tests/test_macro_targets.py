"""set_macro_targets / get_fixed_macro_targets over /api/v2/set_pref."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_client import FakeResp, make_cold_client

from cronometer_api_mcp.client import CronometerError

OK = {"result": "SUCCESS", "messages": []}


def profile(**prefs) -> dict:
    base = {
        "macroIndex": "0",
        "targets.fixed.protein": "151.0",
        "targets.fixed.protein.max": "151.0",
        "targets.fixed.fats": "143.0",
        "targets.fixed.fats.max": "143.0",
        "targets.fixed.net.carbs": "30.0",
        "targets.fixed.net.carbs.max": "30.0",
        "weightGoal": "-1.0",
        "weightUnit": "Pounds",
    }
    base.update(prefs)
    return {"result": "SUCCESS", "prefs": [{k: v} for k, v in base.items()]}


def targets(**rows) -> dict:
    base = {
        203: {"id": 203, "min": 151, "max": 151, "vis": True, "custom": True},
        204: {"id": 204, "min": 143, "max": 143, "vis": True, "custom": True},
        -1205: {"id": -1205, "min": 30, "max": 30, "vis": True, "custom": True},
        307: {"id": 307, "min": 1500, "max": 2300, "vis": True, "custom": False},
    }
    for nid, row in rows.items():
        base[int(nid)] = {**base.get(int(nid), {"id": int(nid), "vis": True}), **row}
    return {"result": "SUCCESS", "targets": list(base.values())}


def test_get_fixed_macro_targets_reads_prefs_and_rows(tmp_path):
    client, _ = make_cold_client(tmp_path, [profile(), targets()])
    out = client.get_fixed_macro_targets()
    assert out["protein_g"] == 151.0
    assert out["weight_goal_per_week"] == -1.0
    assert out["weight_unit"] == "Pounds"
    assert out["sodium_mg_min"] == 1500
    assert out["sodium_mg_max"] == 2300
    assert out["rows"]["fat_g"] == {"min": 143, "max": 143}


def test_get_macro_targets_includes_fixed_targets_and_split(tmp_path, monkeypatch):
    from cronometer_api_mcp import server

    schedules = {"schedules": []}
    templates = {"templates": []}
    prefs = profile(macroProtein="2.0", macroCarbs="4.0", macroLipids="4.0")
    client, _ = make_cold_client(
        tmp_path, [schedules, templates, prefs, targets(), prefs]
    )
    monkeypatch.setattr(server, "_client", client)

    result = json.loads(server.get_macro_targets())

    assert result["status"] == "success"
    assert result["fixed_targets"]["protein_g"] == 151
    assert result["macro_split"] == {"protein": 20, "carbs": 40, "fat": 40}
    assert result["schedules"] == schedules
    assert result["templates"] == templates


@pytest.mark.parametrize("mode", [0, "0"])
def test_set_macro_targets_writes_prefs_and_rows_then_reads_back(tmp_path, mode):
    after_prefs = profile(
        **{
            "targets.fixed.protein": "160.0",
            "targets.fixed.protein.max": "160.0",
            "weightGoal": "-1.5",
        }
    )
    after_rows = targets(**{"203": {"min": 160.0, "max": 160.0}})
    client, state = make_cold_client(
        tmp_path,
        [
            profile(macroIndex=mode),
            targets(),
            OK,
            OK,
            OK,
            after_prefs,
            OK,
            after_rows,
            after_prefs,
            after_rows,
        ],
    )

    result = client.set_macro_targets(protein_g=160, weight_goal_per_week=-1.5)

    sets = [(p["key"], p["val"]) for p in state["payloads"] if "key" in p]
    assert sets == [
        ("targets.fixed.protein", "160.0"),
        ("targets.fixed.protein.max", "160.0"),
        ("weightGoal", "-1.5"),
    ]
    edits = [p for p in state["payloads"] if "cus" in p]
    assert len(edits) == 1
    assert {k: edits[0][k] for k in ("id", "min", "max", "vis", "cus")} == {
        "id": 203,
        "min": 160.0,
        "max": 160.0,
        "vis": True,
        "cus": True,
    }
    assert result["protein_g"] == 160.0
    assert result["rows"]["protein_g"] == {"min": 160.0, "max": 160.0}


def test_set_sodium_only_touches_the_sodium_row(tmp_path):
    after_rows = targets(**{"307": {"min": 3000.0, "max": 5000.0, "custom": True}})
    client, state = make_cold_client(
        tmp_path, [targets(), OK, after_rows, profile(), after_rows]
    )

    result = client.set_macro_targets(sodium_mg_min=3000, sodium_mg_max=5000)

    assert not [p for p in state["payloads"] if "key" in p]  # no set_pref
    edits = [p for p in state["payloads"] if "cus" in p]
    assert [(e["id"], e["min"], e["max"], e["cus"]) for e in edits] == [
        (307, 3000.0, 5000.0, True)
    ]
    assert result["sodium_mg_min"] == 3000.0
    assert result["sodium_mg_max"] == 5000.0


def test_one_sided_sodium_keeps_the_other_bound(tmp_path):
    after_rows = targets(**{"307": {"max": 4000.0, "custom": True}})
    client, state = make_cold_client(
        tmp_path, [targets(), OK, after_rows, profile(), after_rows]
    )

    client.set_macro_targets(sodium_mg_max=4000)

    edit = next(p for p in state["payloads"] if "cus" in p)
    assert (edit["min"], edit["max"]) == (1500, 4000.0)


def test_set_macro_targets_raises_when_pref_does_not_stick(tmp_path):
    client, _ = make_cold_client(tmp_path, [profile(), targets(), OK, OK, profile()])
    with pytest.raises(CronometerError, match="did not read back"):
        client.set_macro_targets(fat_g=90)


def test_set_macro_targets_raises_when_row_does_not_stick(tmp_path):
    client, _ = make_cold_client(tmp_path, [targets(), OK, targets()])
    with pytest.raises(CronometerError, match="rows did not read back"):
        client.set_macro_targets(sodium_mg_min=3000, sodium_mg_max=5000)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"protein_g": -1},
        {"net_carbs_g": 5000},
        {"weight_goal_per_week": -3},
        {"sodium_mg_min": 6000, "sodium_mg_max": 5000},
        {"sodium_mg_max": 50000},
    ],
)
def test_set_macro_targets_rejects_bad_input_without_calls(tmp_path, kwargs):
    client, state = make_cold_client(tmp_path, [OK])
    with pytest.raises(CronometerError):
        client.set_macro_targets(**kwargs)
    assert state["payloads"] == []


@pytest.mark.parametrize("sodium", [{"sodium_mg_min": 3000}, {"sodium_mg_max": 1000}])
@pytest.mark.parametrize("other", [{"protein_g": 160}, {"weight_goal_per_week": -1.5}])
def test_invalid_merged_sodium_range_rejects_before_writes(tmp_path, sodium, other):
    responses = [profile(), targets()] if "protein_g" in other else [targets()]
    client, state = make_cold_client(tmp_path, responses)

    with pytest.raises(CronometerError, match="above max"):
        client.set_macro_targets(**sodium, **other)

    assert not any("key" in p or "cus" in p for p in state["payloads"])


@pytest.mark.parametrize("mode", [5, "5", 6, "6", None])
@pytest.mark.parametrize("macro", ["protein_g", "fat_g", "net_carbs_g"])
def test_gram_targets_require_fixed_mode_before_any_write(tmp_path, mode, macro):
    client, state = make_cold_client(tmp_path, [profile(macroIndex=mode)])

    with pytest.raises(CronometerError, match="require Fixed Targets mode"):
        client.set_macro_targets(**{macro: 100}, weight_goal_per_week=-1.5)

    assert not any("key" in p or "cus" in p for p in state["payloads"])


def test_weight_and_sodium_updates_allow_other_macro_modes(tmp_path):
    after_prefs = profile(macroIndex="6", weightGoal="-1.5")
    after_rows = targets(**{"307": {"max": 3000.0, "custom": True}})
    client, state = make_cold_client(
        tmp_path,
        [targets(), OK, after_prefs, OK, after_rows, after_prefs, after_rows],
    )

    result = client.set_macro_targets(weight_goal_per_week=-1.5, sodium_mg_max=3000)

    assert result["weight_goal_per_week"] == -1.5
    assert (result["sodium_mg_min"], result["sodium_mg_max"]) == (1500, 3000)
    assert [p["key"] for p in state["payloads"] if "key" in p] == ["weightGoal"]


@pytest.mark.parametrize("stall_at", [1, 3])
def test_concurrent_sodium_updates_preserve_both_bounds(tmp_path, stall_at):
    client, _ = make_cold_client(tmp_path, [OK])
    row = targets()["targets"][-1]
    stalled = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    reads = 0
    calls = []

    def fake_post(endpoint, json=None):
        nonlocal reads
        calls.append(endpoint)
        if endpoint == "/api/v2/get_targets":
            snapshot = dict(row)
            reads += 1
            # Hold either the initial snapshot or the final result readback.
            if reads == stall_at:
                stalled.set()
                assert release.wait(timeout=5)
            return FakeResp({"targets": [snapshot]})
        if endpoint == "/api/v2/edit_target":
            row.update({k: json[k] for k in ("id", "min", "max", "vis")})
            row["custom"] = json["cus"]
            return FakeResp(OK)
        assert endpoint == "/api/v2/get_profile"
        return FakeResp(profile())

    def update_max():
        second_started.set()
        return client.set_macro_targets(sodium_mg_max=3000)

    client._http.post = fake_post
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.set_macro_targets, sodium_mg_min=1000)
        try:
            assert stalled.wait(timeout=5)
            calls_before = list(calls)
            second = pool.submit(update_max)
            assert second_started.wait(timeout=5)
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
            assert calls == calls_before
        finally:
            release.set()
        assert first.result(timeout=5)["sodium_mg_min"] == 1000
        result = second.result(timeout=5)

    assert (row["min"], row["max"]) == (1000, 3000)
    assert (result["sodium_mg_min"], result["sodium_mg_max"]) == (1000, 3000)
