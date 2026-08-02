"""Timezone-correctness tests for diary stamping (issue #24).

Diary timestamps, auto meal-group, and the default "today" must be derived
from the account's timezone (resolved from the login response), not the host
process clock. These tests freeze the process at UTC and assert the client
still stamps entries in the account zone.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cronometer_api_mcp.client import CronometerClient


class FakeResp:
    def __init__(self, body, status: int = 200) -> None:
        self._body = body
        self.status_code = status

    def raise_for_status(self) -> None:  # pragma: no cover - trivial
        pass

    def json(self):
        return self._body


def _client(tmp_path: Path, timezone: str) -> CronometerClient:
    client = CronometerClient(session_path=tmp_path / "session.json")
    client._user_id = 123
    client._token = "TOKEN"
    client._timezone = timezone
    return client


def _capture_serving(client: CronometerClient) -> dict:
    """Stub the POST so add_serving returns a success body and record the
    serving payload the client sent."""
    captured: dict = {}

    def fake_post(endpoint, json=None):
        captured["endpoint"] = endpoint
        captured["payload"] = json
        return FakeResp({"result": "SUCCESS", "id": 999})

    client._http.post = fake_post  # type: ignore[method-assign]
    return captured


class FrozenDatetime(_dt.datetime):
    """datetime whose now() ignores the host clock and returns a fixed UTC
    instant, correctly converted into whatever tz is requested."""

    _fixed_utc = _dt.datetime(2026, 7, 27, 18, 1, 30, tzinfo=_dt.timezone.utc)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            # Naive now() in the *host* zone. Force UTC to simulate a UTC server.
            return cls._fixed_utc.replace(tzinfo=None)
        return cls._fixed_utc.astimezone(tz)


@pytest.fixture
def frozen_utc(monkeypatch):
    """Freeze wall-clock time to 2026-07-27 18:01:30 UTC (== 11:01 PDT)."""
    monkeypatch.setattr("cronometer_api_mcp.client.datetime", FrozenDatetime)


def test_stamps_in_account_timezone_not_host(tmp_path, frozen_utc):
    """Los Angeles account, UTC host: 18:01 UTC must record as 11:01 local,
    land on today's LA date, and auto-group as Lunch (not Dinner)."""
    client = _client(tmp_path, "America/Los_Angeles")
    captured = _capture_serving(client)

    client.add_serving(food_id=1, measure_id=0, grams=100.0)

    serving = captured["payload"]["serving"]
    assert serving["time"] == "11:1:30"
    assert serving["day"] == "2026-7-27"
    # 11:00 local -> Lunch (group 2); order == (2 << 16) | 1
    assert serving["order"] == (2 << 16) | 1
    # Matches the native app: no offset stored alongside the wall-clock time.
    assert serving["offset"] is None


def test_day_bucketing_respects_account_timezone(tmp_path, frozen_utc):
    """18:01 UTC is already 'tomorrow' in the host-UTC sense for a late-evening
    logger, but for LA it is still 2026-07-27 -- the entry must not slip a day."""
    client = _client(tmp_path, "America/Los_Angeles")
    captured = _capture_serving(client)

    client.add_serving(food_id=1, measure_id=0, grams=100.0)

    assert captured["payload"]["serving"]["day"] == "2026-7-27"
    assert client.today() == _dt.date(2026, 7, 27)


def test_explicit_day_is_unchanged(tmp_path, frozen_utc):
    """An explicitly supplied day is passed through verbatim (LLM connectors
    almost always supply it); timezone only affects the default path."""
    client = _client(tmp_path, "America/Los_Angeles")
    captured = _capture_serving(client)

    client.add_serving(food_id=1, measure_id=0, grams=100.0, day=_dt.date(2026, 1, 5))

    assert captured["payload"]["serving"]["day"] == "2026-1-5"


def test_explicit_diary_group_skips_auto(tmp_path, frozen_utc):
    """A caller-chosen meal group is never overridden by the hour heuristic."""
    client = _client(tmp_path, "America/Los_Angeles")
    captured = _capture_serving(client)

    client.add_serving(food_id=1, measure_id=0, grams=100.0, diary_group=3)

    assert captured["payload"]["serving"]["order"] == (3 << 16) | 1


def test_login_captures_account_timezone(tmp_path, monkeypatch):
    """login() stores the timezone from the response and persists it."""
    monkeypatch.setenv("CRONOMETER_USERNAME", "u@example.com")
    monkeypatch.setenv("CRONOMETER_PASSWORD", "pw")
    client = CronometerClient(session_path=tmp_path / "session.json")

    def fake_post(endpoint, json=None):
        return FakeResp(
            {
                "result": "SUCCESS",
                "id": 6407976,
                "sessionKey": "SESSIONKEY123",
                "timezone": "America/Los_Angeles",
            }
        )

    client._http.post = fake_post  # type: ignore[method-assign]
    client.login()

    assert client._timezone == "America/Los_Angeles"
    # Persisted to the session cache for warm starts.
    import json

    saved = json.loads((tmp_path / "session.json").read_text())
    assert saved["timezone"] == "America/Los_Angeles"


def test_login_sends_null_request_timezone(tmp_path, monkeypatch):
    """login() must send timezone:null in the request. A non-null value is a
    *write* that overwrites the account's server-side zone (issue #29), so the
    field must never be hardcoded again -- this is a regression guard."""
    monkeypatch.setenv("CRONOMETER_USERNAME", "u@example.com")
    monkeypatch.setenv("CRONOMETER_PASSWORD", "pw")
    monkeypatch.delenv("CRONOMETER_ACCOUNT_TZ", raising=False)
    client = CronometerClient(session_path=tmp_path / "session.json")

    captured: dict = {}

    def fake_post(endpoint, json=None):
        captured["payload"] = json
        return FakeResp(
            {
                "result": "SUCCESS",
                "id": 1,
                "sessionKey": "K",
                "timezone": "America/Los_Angeles",
            }
        )

    client._http.post = fake_post  # type: ignore[method-assign]
    client.login()

    assert "timezone" in captured["payload"]
    assert captured["payload"]["timezone"] is None
    # The response zone is trusted now that we no longer overwrite it.
    assert client._timezone == "America/Los_Angeles"


def test_env_override_wins_over_login_response(tmp_path, monkeypatch):
    """CRONOMETER_ACCOUNT_TZ is authoritative over the login response zone."""
    monkeypatch.setenv("CRONOMETER_USERNAME", "u@example.com")
    monkeypatch.setenv("CRONOMETER_PASSWORD", "pw")
    monkeypatch.setenv("CRONOMETER_ACCOUNT_TZ", "America/Los_Angeles")
    client = CronometerClient(session_path=tmp_path / "session.json")

    def fake_post(endpoint, json=None):
        return FakeResp(
            {
                "result": "SUCCESS",
                "id": 1,
                "sessionKey": "K",
                "timezone": "America/New_York",
            }
        )

    client._http.post = fake_post  # type: ignore[method-assign]
    client.login()

    assert client._timezone == "America/Los_Angeles"
    import json

    saved = json.loads((tmp_path / "session.json").read_text())
    assert saved["timezone"] == "America/Los_Angeles"


def test_env_override_wins_over_poisoned_cache(tmp_path, monkeypatch):
    """A session.json poisoned by an older build must not defeat an explicit
    CRONOMETER_ACCOUNT_TZ override on warm start (issue #29)."""
    monkeypatch.setenv("CRONOMETER_USERNAME", "")
    monkeypatch.setenv("CRONOMETER_ACCOUNT_TZ", "Europe/Berlin")
    import json

    (tmp_path / "session.json").write_text(
        json.dumps(
            {
                "username": "",
                "user_id": 123,
                "token": "TOKEN",
                "timezone": "America/New_York",
            }
        )
    )
    client = CronometerClient(session_path=tmp_path / "session.json")
    assert client._token == "TOKEN"
    assert client._timezone == "Europe/Berlin"


def test_invalid_env_override_is_ignored(tmp_path, monkeypatch):
    """A malformed CRONOMETER_ACCOUNT_TZ is ignored, falling through to the
    login response rather than hard-failing."""
    monkeypatch.setenv("CRONOMETER_USERNAME", "u@example.com")
    monkeypatch.setenv("CRONOMETER_PASSWORD", "pw")
    monkeypatch.setenv("CRONOMETER_ACCOUNT_TZ", "Not/AZone")
    client = CronometerClient(session_path=tmp_path / "session.json")

    def fake_post(endpoint, json=None):
        return FakeResp(
            {
                "result": "SUCCESS",
                "id": 1,
                "sessionKey": "K",
                "timezone": "America/Chicago",
            }
        )

    client._http.post = fake_post  # type: ignore[method-assign]
    client.login()

    assert client._timezone == "America/Chicago"


def test_env_override_stamps_entries(tmp_path, monkeypatch, frozen_utc):
    """End-to-end: with the override set, diary stamping uses the override zone
    even when the client's stored zone would otherwise be Eastern."""
    monkeypatch.setenv("CRONOMETER_ACCOUNT_TZ", "America/Los_Angeles")
    client = _client(tmp_path, "America/New_York")
    # Re-resolve as warm start would, honoring the override.
    client._timezone = client._resolve_timezone("America/New_York")
    captured = _capture_serving(client)

    client.add_serving(food_id=1, measure_id=0, grams=100.0)

    serving = captured["payload"]["serving"]
    # 18:01 UTC -> 11:01 PDT (Los Angeles), not 14:01 EDT.
    assert serving["time"] == "11:1:30"
    assert serving["day"] == "2026-7-27"


def test_warm_start_restores_timezone(tmp_path, monkeypatch):
    """A cache file with a timezone is restored without re-login."""
    monkeypatch.setenv("CRONOMETER_USERNAME", "")
    import json

    (tmp_path / "session.json").write_text(
        json.dumps(
            {
                "username": "",
                "user_id": 123,
                "token": "TOKEN",
                "timezone": "America/Chicago",
            }
        )
    )
    client = CronometerClient(session_path=tmp_path / "session.json")
    assert client._token == "TOKEN"
    assert client._timezone == "America/Chicago"


def test_pre_timezone_cache_is_rejected(tmp_path, monkeypatch):
    """A legacy cache lacking a timezone is treated as invalid so the next
    login refreshes the account zone."""
    monkeypatch.setenv("CRONOMETER_USERNAME", "")
    import json

    (tmp_path / "session.json").write_text(
        json.dumps({"username": "", "user_id": 123, "token": "TOKEN"})
    )
    client = CronometerClient(session_path=tmp_path / "session.json")
    assert client._token is None
    assert client._timezone is None


def test_unknown_timezone_falls_back(tmp_path):
    """An unresolvable zone name falls back rather than raising."""
    client = _client(tmp_path, "Not/AZone")
    assert client._tzinfo() == ZoneInfo("America/New_York")
