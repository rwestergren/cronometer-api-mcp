"""update_serving: change a logged serving's amount, measure or meal group.

The serving is read back from get_diary and re-sent whole to
/api/v2/edit_serving. The meal group is the high 16 bits of `order`.
"""

from __future__ import annotations

from datetime import date

import pytest
from test_client import make_cold_client

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


def test_update_serving_changes_grams_and_keeps_the_rest(tmp_path):
    client, state = make_cold_client(tmp_path, [diary(serving()), EDITED])

    result = client.update_serving(SERVING_ID, DAY, grams=150)

    sent = state["payloads"][1]
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
        ({"diary_group": 5}, "1-4"),
    ],
)
def test_update_serving_validates_before_any_request(tmp_path, kwargs, message):
    client, state = make_cold_client(tmp_path, [diary(serving()), EDITED])

    with pytest.raises(ValueError, match=message):
        client.update_serving(SERVING_ID, DAY, **kwargs)

    assert state["payloads"] == []
