"""Relative reads use a fresh account-local date and a stable per-call range."""

import asyncio
import json
from datetime import UTC, date, datetime
from unittest.mock import Mock, call

import pytest

from cronometer_api_mcp import server
from cronometer_api_mcp.client import CronometerClient


@pytest.fixture
def client(monkeypatch):
    client = Mock(spec=CronometerClient)
    client.today.return_value = date(2026, 1, 1)
    client.get_diary.return_value = {
        "summary": {"macros": {"energy": 2200}, "consumed": {"total": 1800}},
        "entries": [],
    }
    client.enrich_diary_servings.side_effect = lambda data: data
    client.get_consumed_nutrients.return_value = {
        "macros": {"energy": 1800, "protein": 100},
        "nutrients": [{"name": "Energy", "amount": 1800, "unit": "kcal"}],
    }
    client.get_nutrition_scores.return_value = {"Vitamins": 90}
    monkeypatch.setattr(server, "_client", client)
    return client


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "2026-01-01"),
        ("today", "2026-01-01"),
        (" YESTERDAY ", "2025-12-31"),
        ("1 day ago", "2025-12-31"),
        ("3 days ago", "2025-12-29"),
        ("0 days ago", "2026-01-01"),
        ("2024-02-29", "2024-02-29"),
    ],
)
@pytest.mark.parametrize(
    "tool,method,fields",
    [
        (
            server.get_food_log,
            "get_diary",
            {"diary", "energy_summary", "nutrition_summary"},
        ),
        (
            server.get_daily_nutrition,
            "get_consumed_nutrients",
            {"summary", "nutrients"},
        ),
        (server.get_nutrition_scores, "get_nutrition_scores", {"scores"}),
    ],
)
def test_single_day_compatibility(client, value, expected, tool, method, fields):
    result = json.loads(tool(date=value))

    assert result["status"] == "success"
    assert result["date"] == expected
    assert set(result) == {"status", "date"} | fields
    getattr(client, method).assert_called_once_with(date.fromisoformat(expected))
    if tool is server.get_food_log:
        client.get_consumed_nutrients.assert_called_once_with(
            date.fromisoformat(expected)
        )
        assert result["energy_summary"]["remaining_kcal"] == 400
        assert result["nutrition_summary"]["macros"]["energy"] == 1800
    if value == "2024-02-29":
        client.today.assert_not_called()


@pytest.mark.parametrize("tool", [server.get_food_log, server.get_daily_nutrition])
@pytest.mark.parametrize(
    "end,expected",
    [
        (None, ["2025-12-30", "2025-12-31", "2026-01-01"]),
        ("yesterday", ["2025-12-29", "2025-12-30", "2025-12-31"]),
        ("2024-03-01", ["2024-02-28", "2024-02-29", "2024-03-01"]),
    ],
)
def test_multi_day_range(client, tool, end, expected):
    result = json.loads(tool(date=end, days=3))

    assert result["status"] == "success"
    assert result["start_date"] == expected[0]
    assert result["end_date"] == expected[-1]
    assert [day["date"] for day in result["days"]] == expected
    calls = [call(date.fromisoformat(day)) for day in expected]
    assert client.get_consumed_nutrients.call_args_list == calls
    if tool is server.get_food_log:
        assert client.get_diary.call_args_list == calls
        assert client.enrich_diary_servings.call_count == 3


@pytest.mark.parametrize("tool", [server.get_food_log, server.get_daily_nutrition])
def test_midnight_during_read_and_fresh_date_on_next_call(client, tool):
    client.today.side_effect = [date(2026, 1, 1), date(2026, 1, 2)]

    first = json.loads(tool(days=3))
    second = json.loads(tool(days=3))

    assert first["end_date"] == "2026-01-01"
    assert second["end_date"] == "2026-01-02"
    assert client.today.call_count == 2
    expected = [date(2025, 12, 30), date(2025, 12, 31), date(2026, 1, 1)]
    assert client.get_consumed_nutrients.call_args_list[:3] == [
        call(d) for d in expected
    ]


def test_relative_read_uses_account_timezone(client, tmp_path, monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 1, tzinfo=UTC).astimezone(tz)

    monkeypatch.delenv("CRONOMETER_ACCOUNT_TZ", raising=False)
    monkeypatch.setattr("cronometer_api_mcp.client.datetime", FrozenDatetime)
    real_client = CronometerClient(session_path=tmp_path / "session.json")
    real_client._user_id = 123
    real_client._token = "TOKEN"
    real_client._timezone = "America/Los_Angeles"
    client.today.side_effect = real_client.today
    try:
        result = json.loads(server.get_daily_nutrition(days=2))
    finally:
        real_client._http.close()

    assert result["start_date"] == "2025-12-30"
    assert result["end_date"] == "2025-12-31"


@pytest.mark.parametrize("days", [0, -1, 32, 1.5, True, "3"])
@pytest.mark.parametrize("tool", [server.get_food_log, server.get_daily_nutrition])
def test_invalid_days_do_not_fetch(client, tool, days):
    result = json.loads(tool(days=days))

    assert result["status"] == "error"
    assert "integer between 1 and 31" in result["message"]
    assert not client.mock_calls


@pytest.mark.parametrize(
    "value", ["", "last week", "-1 days ago", "2026-02-30", "999999999999 days ago"]
)
@pytest.mark.parametrize(
    "tool",
    [server.get_food_log, server.get_daily_nutrition, server.get_nutrition_scores],
)
def test_invalid_dates_do_not_fetch(client, tool, value):
    result = json.loads(tool(date=value))

    assert result["status"] == "error"
    assert "YYYY-MM-DD" in result["message"]
    client.get_diary.assert_not_called()
    client.get_consumed_nutrients.assert_not_called()
    client.get_nutrition_scores.assert_not_called()


def test_range_underflow_does_not_fetch(client):
    result = json.loads(server.get_daily_nutrition(date="0001-01-01", days=2))

    assert result["status"] == "error"
    assert not client.mock_calls


def test_maximum_range(client):
    result = json.loads(server.get_daily_nutrition(days=31))

    assert result["start_date"] == "2025-12-02"
    assert result["end_date"] == "2026-01-01"
    assert len(result["days"]) == 31


def test_days_constraints_exposed_in_tool_schema():
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    for name in ("get_food_log", "get_daily_nutrition"):
        days = tools[name].input_schema["properties"]["days"]
        assert days["type"] == "integer"
        assert days["minimum"] == 1
        assert days["maximum"] == 31
        assert days["default"] == 1
