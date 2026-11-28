"""Native recent-food rankings and reference-serving round trips."""

import asyncio
import copy
import json

import httpx
import pytest

from cronometer_api_mcp import server
from cronometer_api_mcp.client import CronometerClient


def food_row(food_id, name, count, *, recipe=False, day="2026-11-28"):
    measure_id = food_id * 10
    return {
        "count": count,
        "food": {
            "id": food_id,
            "name": name,
            "source": "Custom",
            "retired": False,
            "defaultMeasureId": measure_id + 1,
            "measures": [
                {
                    "id": measure_id,
                    "name": "portion",
                    "type": "Recipe" if recipe else "Atomic",
                    "amount": 2,
                    "value": 0.5 if recipe else 30,
                },
                {"id": measure_id + 1, "name": "default", "value": 100},
            ],
            "nutrients": [{"id": 208, "amount": 150}],
        },
        "serving": {
            "foodId": food_id,
            "measureId": measure_id,
            "translationId": 0,
            "grams": 1.25 if recipe else 60,
            "day": day,
            "time": "12:30:00",
            "order": (7 << 16) | 3,
            "servingId": food_id * 100,
            "userId": 123,
        },
    }


@pytest.fixture
def native_client(tmp_path, monkeypatch):
    payload = {
        "servings": [
            food_row(1, "Plain yogurt", 5),
            food_row(2, "Vanilla yogurt", 9),
            food_row(3, "Recipe", 5, recipe=True),
            food_row(4, "Berry YOGURT", 1, day="2020-01-01"),
        ],
        "messages": [],
    }
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=payload)

    client = CronometerClient(session_path=tmp_path / "session.json")
    client._user_id = 123
    client._token = "test-token"
    client._http.close()
    with httpx.Client(
        base_url="https://mobile.cronometer.com",
        transport=httpx.MockTransport(respond),
    ) as http:
        client._http = http
        monkeypatch.setattr(server, "_client", client)
        yield client, calls, payload


@pytest.mark.parametrize(
    "method,expected",
    [("get_recent_foods", [4, 3, 2, 1]), ("get_frequent_foods", [2, 3, 1, 4])],
)
def test_native_ranking_uses_one_read_without_mutating_response(
    native_client, method, expected
):
    client, calls, payload = native_client
    original = copy.deepcopy(payload)

    rows = getattr(client, method)()

    assert [row["food"]["id"] for row in rows] == expected
    assert payload == original
    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert calls[0].url.path == "/api/v2/get_recent_foods"
    body = json.loads(calls[0].content)
    assert body["config"] == {"call_version": 1}
    assert body["auth"]["userId"] == 123
    assert set(body) == {"config", "auth", "lastSeen"}


@pytest.mark.parametrize("tool", [server.get_recent_foods, server.get_frequent_foods])
def test_filter_precedes_limit_and_preserves_recipe_reference(native_client, tool):
    _, calls, _ = native_client

    result = json.loads(tool(query=" rEcIpE ", limit=1))

    assert result["status"] == "success"
    assert result["available_count"] == 4
    assert result["count"] == 1
    assert result["foods"] == [
        {
            "food_id": 3,
            "name": "Recipe",
            "source": "Custom",
            "retired": False,
            "recent_count": 5,
            "reference_serving": {
                "measure_id": 30,
                "translation_id": 0,
                "grams": 1.25,
                "measure": {
                    "name": "portion",
                    "type": "Recipe",
                    "quantity": 2,
                    "value": 0.5,
                },
                "date": "2026-11-28",
                "time": "12:30:00",
                "diary_group_number": 7,
            },
        }
    ]
    assert len(calls) == 1


@pytest.mark.parametrize(
    "tool,expected",
    [(server.get_recent_foods, [4, 2]), (server.get_frequent_foods, [2, 1])],
)
def test_name_filter_keeps_ranking(native_client, tool, expected):
    result = json.loads(tool(query="YOGURT", limit=2))

    assert [food["food_id"] for food in result["foods"]] == expected


def test_unresolved_measure_and_group_zero_are_preserved(native_client):
    _, _, payload = native_client
    row = payload["servings"][-1]
    row["serving"]["measureId"] = 0
    row["serving"]["order"] = 0

    result = json.loads(server.get_recent_foods(limit=1))

    reference = result["foods"][0]["reference_serving"]
    assert reference["measure_id"] == 0
    assert reference["measure"] is None
    assert reference["diary_group_number"] == 0
    assert reference["grams"] == 60


@pytest.mark.parametrize("tool", [server.get_recent_foods, server.get_frequent_foods])
@pytest.mark.parametrize("limit", [0, 51, True, 1.5, "2"])
def test_invalid_limit_does_not_fetch(native_client, tool, limit):
    _, calls, _ = native_client

    result = json.loads(tool(limit=limit))

    assert result["status"] == "error"
    assert "limit must be an integer" in result["message"]
    assert calls == []


@pytest.mark.parametrize("tool", [server.get_recent_foods, server.get_frequent_foods])
def test_empty_list_and_no_matches(native_client, tool):
    _, _, payload = native_client
    result = json.loads(tool(query="absent"))
    assert result["foods"] == []
    assert result["count"] == 0
    assert result["available_count"] == 4

    payload["servings"] = []
    result = json.loads(tool())
    assert result["status"] == "success"
    assert result["foods"] == []
    assert result["count"] == result["available_count"] == 0


@pytest.mark.parametrize("tool", [server.get_recent_foods, server.get_frequent_foods])
def test_rate_limit_is_a_structured_error(native_client, monkeypatch, tool):
    client, _, _ = native_client

    def rate_limited(*args, **kwargs):
        request = httpx.Request(
            "POST", "https://mobile.cronometer.com/api/v2/get_recent_foods"
        )
        httpx.Response(429, request=request).raise_for_status()

    monkeypatch.setattr(client, "_request", rate_limited)

    result = json.loads(tool())

    assert result["status"] == "error"
    assert "Rate limit exceeded" in result["message"]


def test_tool_schemas_expose_local_limit_bounds():
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    for name in ("get_recent_foods", "get_frequent_foods"):
        limit = tools[name].input_schema["properties"]["limit"]
        assert limit["minimum"] == 1
        assert limit["maximum"] == 50
        assert limit["default"] == 50
