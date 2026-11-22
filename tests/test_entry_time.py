"""The optional `time` argument of add_food_entry and add_note."""

import datetime as _dt
import json
from unittest.mock import Mock

import pytest

from cronometer_api_mcp import server
from cronometer_api_mcp.client import CronometerClient


@pytest.fixture
def client(monkeypatch):
    c = Mock(spec=CronometerClient)
    c.resolve_diary_group.return_value = {"group": None, "name": "auto"}
    c.add_serving.return_value = {"id": 1}
    c.add_note.return_value = {"noteId": 2}
    monkeypatch.setattr(server, "_client", c)
    return c


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("00:00", _dt.time(0, 0)),
        ("08:30", _dt.time(8, 30)),
        (" 21:05:09 ", _dt.time(21, 5, 9)),
        ("23:59:59", _dt.time(23, 59, 59)),
    ],
)
def test_add_food_entry_passes_time_to_the_client(client, text, expected):
    out = json.loads(server.add_food_entry(1, 2, 100.0, time=text))
    assert out["status"] == "success"
    assert client.add_serving.call_args.kwargs["at"] == expected


def test_add_note_passes_time_to_the_client(client):
    out = json.loads(server.add_note("hi", time="12:30"))
    assert out["status"] == "success"
    assert client.add_note.call_args.kwargs["at"] == _dt.time(12, 30)


def test_time_defaults_to_now(client):
    server.add_food_entry(1, 2, 100.0)
    server.add_note("hi")
    assert client.add_serving.call_args.kwargs["at"] is None
    assert client.add_note.call_args.kwargs["at"] is None


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "25:00",
        "08:60",
        "08:30:60",
        "noon",
        "8",
        "08",
        "0830",
        "T08:30",
        "08:30Z",
        "08:30+02:00",
        "08:30-08:00",
        "08:30:00+02:00",
        "08:30:00.123",
    ],
)
def test_invalid_time_is_an_error_and_writes_nothing(client, bad):
    for out in (
        server.add_food_entry(1, 2, 100.0, time=bad),
        server.add_note("hi", time=bad),
    ):
        data = json.loads(out)
        assert data["status"] == "error"
        assert "Invalid time" in data["message"]
    client.add_serving.assert_not_called()
    client.add_note.assert_not_called()
