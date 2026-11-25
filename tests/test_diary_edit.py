"""update_serving: change a logged serving's amount, measure or meal group.

The serving is read back from get_diary and re-sent whole to
/api/v2/edit_serving. The meal group is the high 16 bits of `order`.
"""

from __future__ import annotations

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from test_client import SAMPLE_FOODS, make_cold_client
from test_custom_food_edit import stored_recipe

from cronometer_api_mcp.client import CronometerError

DAY = date(1900, 1, 2)
SERVING_ID = 5312202908


def serving(**overrides) -> dict:
    """A get_diary entry shaped like the real one."""
    entry = {
        "servingId": SERVING_ID,
        "type": "Serving",
        "foodId": 5442,
        "measureId": 14990,
        "translationId": 0,
        "grams": 100,
        "day": "1900-01-02",
        "time": "11:23:50",
        "offset": 0,
        "userId": 42,
        "order": (1 << 16) | 1,  # breakfast, first
    }
    entry.update(overrides)
    return entry


def diary(*entries) -> dict:
    return {"diary": list(entries), "summary": {}, "messages": []}


EDITED = {"messages": [], "id": SERVING_ID}
FOOD = {
    "id": 5442,
    "defaultMeasureId": 14990,
    "measures": [{"id": 14990, "name": "g", "type": "Weight", "value": 1}],
}


def test_update_serving_changes_grams_and_keeps_the_rest(tmp_path):
    client, state = make_cold_client(tmp_path, [diary(serving()), FOOD, EDITED])

    result = client.update_serving(SERVING_ID, DAY, grams=150)

    assert state["payloads"][1]["id"] == FOOD["id"]
    sent = state["payloads"][2]
    assert sent["config"] == {"call_version": 2}
    assert sent["serving"] == serving(grams=150)
    assert result == serving(grams=150)


def test_update_serving_moves_to_end_of_new_group(tmp_path):
    lunch = [
        serving(servingId=1, order=(2 << 16) | 1),
        serving(servingId=2, order=(2 << 16) | 4),
    ]
    client, state = make_cold_client(tmp_path, [diary(serving(), *lunch), EDITED])

    client.update_serving(str(SERVING_ID), DAY, diary_group=2)

    assert state["payloads"][1]["serving"]["order"] == (2 << 16) | 5


def test_update_serving_same_group_keeps_position(tmp_path):
    entry = serving(order=(1 << 16) | 3)
    client, state = make_cold_client(tmp_path, [diary(entry), EDITED])

    client.update_serving(SERVING_ID, DAY, diary_group=1, measure_id=7)

    sent = state["payloads"][1]["serving"]
    assert sent["order"] == (1 << 16) | 3
    assert sent["measureId"] == 7


def test_update_serving_unknown_id_raises_without_editing(tmp_path):
    client, state = make_cold_client(tmp_path, [diary(serving()), EDITED])

    with pytest.raises(CronometerError, match="No serving 999"):
        client.update_serving(999, DAY, grams=10)

    assert len(state["payloads"]) == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "needs grams"),
        ({"grams": 0}, "positive"),
        ({"diary_group": -1}, "0-65535"),
        ({"diary_group": 1 << 16}, "0-65535"),
    ],
)
def test_update_serving_validates_before_any_request(tmp_path, kwargs, message):
    client, state = make_cold_client(tmp_path, [diary(serving()), EDITED])

    with pytest.raises(ValueError, match=message):
        client.update_serving(SERVING_ID, DAY, **kwargs)

    assert state["payloads"] == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"grams": 100},
        {"grams": 100, "measure_id": 31},
        {"grams": 100, "diary_group": 5},
    ],
)
def test_update_serving_rejects_recipe_weights_without_editing(tmp_path, kwargs):
    entry = serving(foodId=300, measureId=30, grams=1.1)
    recipe = SAMPLE_FOODS[2]
    client, state = make_cold_client(tmp_path, [diary(entry), recipe, EDITED])

    with pytest.raises(CronometerError, match="serving-based recipe"):
        client.update_serving(SERVING_ID, DAY, **kwargs)

    assert len(state["payloads"]) == 2
    assert all("serving" not in p for p in state["payloads"])


def test_update_food_entry_reports_unsupported_recipe_weight(tmp_path, monkeypatch):
    from cronometer_api_mcp import server

    entry = serving(foodId=300, measureId=30, grams=1.1)
    client, state = make_cold_client(tmp_path, [diary(entry), SAMPLE_FOODS[2]])
    monkeypatch.setattr(server, "_client", client)

    result = json.loads(
        server.update_food_entry(str(SERVING_ID), DAY.isoformat(), grams=100)
    )

    assert result["status"] == "error"
    assert "serving-based recipe" in result["message"]
    assert "in Cronometer instead" in result["message"]
    assert len(state["payloads"]) == 2


def test_update_serving_changes_weight_based_recipe_amount(tmp_path):
    recipe = stored_recipe()
    entry = serving(foodId=recipe["id"], measureId=1)
    client, state = make_cold_client(tmp_path, [diary(entry), recipe, EDITED])

    result = client.update_serving(SERVING_ID, DAY, grams=150)

    assert result == {**entry, "grams": 150}
    assert state["payloads"][-1]["serving"] == result


@pytest.mark.parametrize(
    "kwargs",
    [{"measure_id": 31}, {"diary_group": 5}, {"measure_id": 31, "diary_group": 5}],
)
def test_update_serving_preserves_recipe_amount_on_measure_or_group_edit(
    tmp_path, kwargs
):
    entry = serving(foodId=300, measureId=30, grams=1.1)
    client, state = make_cold_client(tmp_path, [diary(entry), EDITED])

    result = client.update_serving(SERVING_ID, DAY, **kwargs)

    expected = dict(entry)
    if "measure_id" in kwargs:
        expected["measureId"] = kwargs["measure_id"]
    if "diary_group" in kwargs:
        expected["order"] = (kwargs["diary_group"] << 16) | 1
    assert result == expected
    assert state["payloads"][-1]["serving"] == expected
    assert len(state["payloads"]) == 2


def test_concurrent_serving_edits_preserve_both_changes(tmp_path, monkeypatch):
    client, _ = make_cold_client(tmp_path, [])
    stored = serving()
    state_lock = threading.Lock()
    first_fetched = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_fetched = threading.Event()
    reads = 0

    def request(endpoint, payload):
        nonlocal stored, reads
        if endpoint == "/api/v2/get_diary":
            with state_lock:
                snapshot = copy.deepcopy(stored)
                reads += 1
                first = reads == 1
            if first:
                first_fetched.set()
                assert release_first.wait(timeout=5)
            else:
                second_fetched.set()
            return diary(snapshot)
        if endpoint == "/api/v2/get_food":
            return FOOD
        assert endpoint == "/api/v2/edit_serving"
        with state_lock:
            stored = copy.deepcopy(payload["serving"])
        return EDITED

    def move():
        second_started.set()
        return client.update_serving(str(SERVING_ID), DAY, diary_group=5)

    monkeypatch.setattr(client, "_request", request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        amount = pool.submit(client.update_serving, SERVING_ID, DAY, grams=150)
        try:
            assert first_fetched.wait(timeout=5)
            moved = pool.submit(move)
            assert second_started.wait(timeout=5)
            fetched_before_save = second_fetched.wait(timeout=0.2)
        finally:
            release_first.set()
        amount.result(timeout=5)
        moved.result(timeout=5)

    assert not fetched_before_save
    assert stored == serving(grams=150, order=(5 << 16) | 1)
