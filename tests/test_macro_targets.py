"""set_macro_targets / get_fixed_macro_targets over /api/v2/set_pref."""

from __future__ import annotations

import pytest
from test_client import make_cold_client

from cronometer_api_mcp.client import CronometerError

OK = {"result": "SUCCESS", "messages": []}


def profile(**prefs) -> dict:
    base = {
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


def test_set_macro_targets_writes_prefs_and_rows_then_reads_back(tmp_path):
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
        [OK, OK, OK, after_prefs, targets(), OK, after_rows, after_prefs, after_rows],
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
    client, _ = make_cold_client(tmp_path, [OK, OK, profile()])
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
