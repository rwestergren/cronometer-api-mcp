"""update_custom_food / retire_custom_food.

Both ride on /api/v2/add_food, which upserts: re-sending a food object with
its existing id edits it in place, and flipping `retired` hides it from search
and the Custom Foods list (the app's own delete hard-deletes via an endpoint we
haven't found; retiring is the closest the known API offers).
"""

from __future__ import annotations

import pytest
from test_client import make_cold_client

from cronometer_api_mcp.client import CronometerError

FOOD_ID = 81460655
OK = {"result": "SUCCESS", "id": FOOD_ID}


def custom_food(**overrides) -> dict:
    """A get_food body shaped like the real one for a user-created food.

    Nutrients are per-100g; the default measure is a 50 g serving, so
    per-serving values are half of what is stored.
    """
    food = {
        "id": FOOD_ID,
        "name": "Test Food",
        "source": "Custom",
        "owner": 42,
        "retired": False,
        "category": 0,
        "labelType": "AMERICAN_2016",
        "foodTags": [],
        "barcodes": [],
        "properties": {},
        "meal": False,
        "defaultMeasureId": 900,
        "measures": [
            {
                "id": 900,
                "name": "serving",
                "amount": 1,
                "value": 50,
                "type": "Atomic",
                "hidden": False,
                "derivable": False,
                "derived": False,
            }
        ],
        "translations": [
            {"translationId": 1, "name": "Test Food", "languageCode": "en"}
        ],
        "nutrients": [
            {"id": 208, "amount": 200, "type": "PRIMARY"},
            {"id": 203, "amount": 20, "type": "PRIMARY"},
            {"id": 204, "amount": 10, "type": "PRIMARY"},
            {"id": 205, "amount": 40, "type": "PRIMARY"},
            {"id": 291, "amount": 4, "type": "PRIMARY"},
            {"id": 269, "amount": 0, "type": "PRIMARY"},
            {"id": 307, "amount": 0, "type": "PRIMARY"},
            {"id": 606, "amount": 0, "type": "PRIMARY"},
            {"id": -203, "amount": 20, "type": "PRIMARY"},
            {"id": -204, "amount": 10, "type": "PRIMARY"},
            {"id": -205, "amount": 40, "type": "PRIMARY"},
            {"id": -221, "amount": 0, "type": "PRIMARY"},
            {"id": -1205, "amount": 36, "type": "PRIMARY"},
            {"id": 601, "amount": 30, "type": "PRIMARY"},  # cholesterol (extra)
        ],
        # Server-side sync events that ride along on get_food; not food data.
        "messages": [
            {"id": 1, "type": "FOOD_CHANGED", "text": '{"id":1,"type":"ADD"}'}
        ],
    }
    food.update(overrides)
    return food


def nutrients_by_id(payload: dict) -> dict[int, float]:
    return {n["id"]: n["amount"] for n in payload["data"]["nutrients"]}


# ---------------------------------------------------------------------------
# update_custom_food
# ---------------------------------------------------------------------------


def test_update_custom_food_resends_food_with_same_id_and_new_name(tmp_path):
    """The fetched food goes back to add_food with its id intact, the new name
    applied to both the name and its translation, and the sync messages
    stripped."""
    client, state = make_cold_client(tmp_path, [custom_food(), OK])

    result = client.update_custom_food(FOOD_ID, name="Renamed")

    get_payload, add_payload = state["payloads"]
    assert get_payload["id"] == FOOD_ID
    data = add_payload["data"]
    assert data["id"] == FOOD_ID
    assert data["name"] == "Renamed"
    assert [t["name"] for t in data["translations"]] == ["Renamed"]
    assert data["retired"] is False
    assert "messages" not in data
    assert add_payload["config"] == {"call_version": 1}
    assert result == {"food_id": FOOD_ID, "name": "Renamed"}


def test_update_custom_food_scales_macros_by_default_serving(tmp_path):
    """Nutrient args are per serving; storage is per-100g, so a 50 g default
    measure doubles them. Nutrients not passed keep their stored values."""
    client, state = make_cold_client(tmp_path, [custom_food(), OK])

    client.update_custom_food(FOOD_ID, calories=150, sodium_mg=30)

    by_id = nutrients_by_id(state["payloads"][1])
    assert by_id[208] == 300.0  # 150 kcal per 50 g serving
    assert by_id[307] == 60.0
    assert by_id[203] == 20  # untouched
    assert by_id[204] == 10


def test_update_custom_food_refreshes_derived_fields(tmp_path):
    """The app mirrors protein/fat/carbs into -203/-204/-205 and keeps
    net carbs (-1205) = carbs - fiber; an edit must keep those consistent."""
    client, state = make_cold_client(tmp_path, [custom_food(), OK])

    client.update_custom_food(FOOD_ID, protein_g=15, carbs_g=10, fiber_g=4)

    by_id = nutrients_by_id(state["payloads"][1])
    assert by_id[203] == 30.0
    assert by_id[-203] == 30.0
    assert by_id[205] == 20.0
    assert by_id[-205] == 20.0
    assert by_id[291] == 8.0
    assert by_id[-1205] == 12.0  # 20 - 8
    assert by_id[-204] == 10  # fat untouched


def test_update_custom_food_serving_overrides_measure_and_scale(tmp_path):
    """serving_grams / serving_name edit the default measure, and nutrient
    args are interpreted against the new serving size."""
    client, state = make_cold_client(tmp_path, [custom_food(), OK])

    client.update_custom_food(
        FOOD_ID, serving_name="1 cup", serving_grams=200, calories=400
    )

    data = state["payloads"][1]["data"]
    (measure,) = data["measures"]
    assert measure["id"] == 900
    assert measure["name"] == "1 cup"
    assert measure["value"] == 200
    assert nutrients_by_id(state["payloads"][1])[208] == 200.0  # 400 per 200 g


def test_update_custom_food_extra_nutrients(tmp_path):
    """extra_nutrients are set (scaled) by id, replacing an existing entry or
    appending a new one, and may not overlap the named macro args."""
    client, state = make_cold_client(tmp_path, [custom_food(), OK])

    client.update_custom_food(FOOD_ID, extra_nutrients={601: 2.5, 430: 10.0})

    by_id = nutrients_by_id(state["payloads"][1])
    assert by_id[601] == 5.0  # cholesterol, replaced in place
    assert by_id[430] == 20.0  # vitamin K, appended
    ids = [n["id"] for n in state["payloads"][1]["data"]["nutrients"]]
    assert ids.count(601) == 1

    client2, _ = make_cold_client(tmp_path, [custom_food(), OK])
    with pytest.raises(ValueError):
        client2.update_custom_food(FOOD_ID, extra_nutrients={204: 1.0})


def test_update_custom_food_refuses_database_foods(tmp_path):
    """Only user-created foods may be edited; a database food is rejected
    before anything is sent to add_food."""
    client, state = make_cold_client(
        tmp_path, [custom_food(source="CRDB", owner=None), OK]
    )

    with pytest.raises(CronometerError, match="not a custom food"):
        client.update_custom_food(FOOD_ID, name="Renamed")

    assert len(state["payloads"]) == 1  # only the get_food


def test_update_custom_food_raises_when_server_returns_other_id(tmp_path):
    """add_food answering with a different id means it created a copy instead
    of editing in place; surface that rather than report success."""
    client, _ = make_cold_client(
        tmp_path, [custom_food(), {"result": "SUCCESS", "id": FOOD_ID + 1}]
    )

    with pytest.raises(CronometerError):
        client.update_custom_food(FOOD_ID, name="Renamed")


# ---------------------------------------------------------------------------
# retire_custom_food
# ---------------------------------------------------------------------------


def test_retire_custom_food_resends_food_with_retired_flag(tmp_path):
    client, state = make_cold_client(tmp_path, [custom_food(), OK])

    result = client.retire_custom_food(FOOD_ID)

    data = state["payloads"][1]["data"]
    assert data["id"] == FOOD_ID
    assert data["retired"] is True
    assert data["name"] == "Test Food"
    assert "messages" not in data
    assert result == {"food_id": FOOD_ID, "name": "Test Food", "retired": True}


def test_retire_custom_food_refuses_database_foods(tmp_path):
    client, state = make_cold_client(tmp_path, [custom_food(source="USDA"), OK])

    with pytest.raises(CronometerError, match="not a custom food"):
        client.retire_custom_food(FOOD_ID)

    assert len(state["payloads"]) == 1
