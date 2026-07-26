"""Regression tests for CronometerClient auth-failure handling.

Covers issue #18: an expired session comes back as an HTTP 200 body of
{"result": "FAIL", ...} which the client previously failed to detect
(it only matched "FAILURE"), so a stale token was never invalidated and
the failure was surfaced to callers as a success.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cronometer_api_mcp.client import CronometerClient, CronometerError

# Real captured response body from an expired session (see repo `flows`).
REAL_FAIL_BODY = {"result": "FAIL", "error": "Token Authorization failed"}
# Synthetic/defensive variant. Never observed in real traffic, but the
# client keeps handling it just in case some endpoint uses it.
SYNTHETIC_FAILURE_BODY = {"result": "FAILURE", "error": "synthetic"}


class FakeResp:
    def __init__(self, body, status: int = 200) -> None:
        self._body = body
        self.status_code = status

    def raise_for_status(self) -> None:  # pragma: no cover - trivial
        pass

    def json(self):
        return self._body


def make_client(tmp_path: Path, responses: list[dict]):
    """Build a client whose HTTP POST returns `responses` in order and whose
    login() just swaps in a fresh token without hitting the network.

    Returns (client, state) where state tracks login/post call counts.
    """
    client = CronometerClient(session_path=tmp_path / "session.json")
    client._user_id = 123
    client._token = "STALE_TOKEN"

    state = {"login": 0, "post": 0}

    def fake_login() -> None:
        state["login"] += 1
        client._user_id = 123
        client._token = f"FRESH_TOKEN_{state['login']}"

    def fake_post(endpoint, json=None):
        idx = state["post"]
        state["post"] += 1
        body = responses[min(idx, len(responses) - 1)]
        return FakeResp(body)

    client.login = fake_login  # type: ignore[method-assign]
    client._http.post = fake_post  # type: ignore[method-assign]
    return client, state


def test_fail_invalidates_token_logs_in_and_retries_once(tmp_path):
    """HTTP 200 + result:"FAIL" -> invalidate token, login, retry once, succeed."""
    client, state = make_client(
        tmp_path, [REAL_FAIL_BODY, {"result": "SUCCESS", "id": 999}]
    )

    result = client._request("/api/v2/get_diary", {})

    assert result == {"result": "SUCCESS", "id": 999}
    assert state["login"] == 1
    assert state["post"] == 2


def test_failure_variant_still_retries(tmp_path):
    """HTTP 200 + result:"FAILURE" retains the original retry behavior."""
    client, state = make_client(
        tmp_path, [SYNTHETIC_FAILURE_BODY, {"result": "SUCCESS", "id": 1}]
    )

    result = client._request("/api/v2/get_diary", {})

    assert result == {"result": "SUCCESS", "id": 1}
    assert state["login"] == 1
    assert state["post"] == 2


def test_second_failure_raises(tmp_path):
    """A second failed response raises CronometerError rather than returning it."""
    client, state = make_client(tmp_path, [REAL_FAIL_BODY, REAL_FAIL_BODY])

    with pytest.raises(CronometerError):
        client._request("/api/v2/get_diary", {})

    assert state["login"] == 1
    assert state["post"] == 2


def test_add_serving_does_not_return_success_wrapper_on_failure(tmp_path):
    """add_serving must not surface an API failure body as a success result."""
    client, state = make_client(tmp_path, [REAL_FAIL_BODY, REAL_FAIL_BODY])

    with pytest.raises(CronometerError):
        client.add_serving(food_id=1, measure_id=0, grams=100.0)

    assert state["post"] == 2


def test_stale_cache_file_is_removed_on_failure(tmp_path):
    """The stale cached session file is deleted when a FAIL is detected."""
    session_path = tmp_path / "session.json"
    session_path.write_text('{"username": "", "user_id": 123, "token": "STALE"}')

    client, _ = make_client(tmp_path, [REAL_FAIL_BODY, {"result": "SUCCESS", "id": 5}])
    # make_client overwrote session_path arg via same tmp_path
    assert client._session_path == session_path

    client._request("/api/v2/get_diary", {})

    # _invalidate_session() should have unlinked the stale file.
    assert not session_path.exists()
