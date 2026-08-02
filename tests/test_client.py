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

# Real response body observed from an expired session.
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


# ---------------------------------------------------------------------------
# Cold-start auth ordering (issues #30 / #31)
#
# On a client that hasn't logged in yet, add_serving() must not embed a null
# userId: reading identity has to trigger login first, so the payload is
# correct on the first attempt and the retry never re-sends stale state.
# ---------------------------------------------------------------------------


def make_cold_client(tmp_path: Path, responses: list[dict]):
    """Like make_client but starts unauthenticated and records posted payloads."""
    client = CronometerClient(session_path=tmp_path / "session.json")
    client._user_id = None
    client._token = None

    state = {"login": 0, "post": 0, "payloads": []}

    def fake_login() -> None:
        state["login"] += 1
        client._user_id = 42
        client._token = f"FRESH_TOKEN_{state['login']}"

    def fake_post(endpoint, json=None):
        state["payloads"].append(json)
        idx = state["post"]
        state["post"] += 1
        body = responses[min(idx, len(responses) - 1)]
        return FakeResp(body)

    client.login = fake_login  # type: ignore[method-assign]
    client._http.post = fake_post  # type: ignore[method-assign]
    return client, state


def test_add_serving_cold_start_embeds_real_user_id(tmp_path):
    """First write on a cold client logs in once and sends the real userId."""
    client, state = make_cold_client(tmp_path, [{"result": "SUCCESS", "id": 7}])

    client.add_serving(food_id=1, measure_id=0, grams=100.0)

    assert state["login"] == 1
    assert state["post"] == 1  # no stale-payload double failure (#31)
    assert state["payloads"][0]["serving"]["userId"] == 42


def test_user_id_property_triggers_login(tmp_path):
    """Reading user_id on a cold client authenticates and returns the real id."""
    client, state = make_cold_client(tmp_path, [{"result": "SUCCESS"}])

    assert client.user_id == 42
    assert state["login"] == 1


# ---------------------------------------------------------------------------
# Diary enrichment (get_food_log food names / per-entry nutrients)
# ---------------------------------------------------------------------------


def _enrich_client(tmp_path: Path):
    client = CronometerClient(session_path=tmp_path / "session.json")
    client._user_id = 123
    client._token = "TOKEN"
    return client


SAMPLE_DIARY = {
    "diary": [
        {
            "type": "Serving",
            "foodId": 100,
            "measureId": 10,
            "grams": 200,
            "order": 65537,
        },
        {
            "type": "Serving",
            "foodId": 200,
            "measureId": 999,  # unknown measure -> falls back to default
            "grams": 50,
            "order": 65538,
        },
        {
            "type": "Serving",
            "foodId": 300,
            "measureId": 30,
            "grams": 1.1,  # Recipe: "grams" is a serving count, not grams
            "order": 65539,
        },
        {"type": "Exercise", "name": "Running", "order": 1},
    ]
}

SAMPLE_FOODS = [
    {
        "id": 100,
        "name": "Oats",
        "source": "USDA",
        "category": "Grains",
        "defaultMeasureId": 10,
        "measures": [{"id": 10, "name": "cup", "value": 100.0}],
        # per-100g
        "nutrients": [{"id": 208, "amount": 389.0}, {"id": 203, "amount": 16.9}],
    },
    {
        "id": 200,
        "name": "Milk",
        "source": "Custom",
        "defaultMeasureId": 20,
        "measures": [{"id": 20, "name": "glass", "value": 244.0}],
        "nutrients": [{"id": 208, "amount": 42.0}],
    },
    {
        "id": 300,
        "name": "Recipe Food",
        "source": "Custom",
        "defaultMeasureId": 30,
        "measures": [
            {"id": 30, "name": "serving", "value": 1, "amount": 1, "type": "Recipe"},
            {"id": 31, "name": "g", "value": 233.4, "amount": 1, "type": "Recipe"},
        ],
        # stored per one reference serving (not per-100g)
        "nutrients": [{"id": 208, "amount": 708.538}, {"id": 203, "amount": 56.03}],
    },
]

NUTRIENT_DEFS = {
    208: {"name": "Energy", "unit": "kcal", "category": "General"},
    203: {"name": "Protein", "unit": "g", "category": "Protein"},
}


def test_enrich_diary_merges_names_measures_and_scaled_nutrients(tmp_path):
    client = _enrich_client(tmp_path)
    client.get_foods = lambda ids: SAMPLE_FOODS  # type: ignore[method-assign]
    client.get_nutrient_definitions = lambda: NUTRIENT_DEFS  # type: ignore[method-assign]

    out = client.enrich_diary_servings(
        {"diary": [dict(e) for e in SAMPLE_DIARY["diary"]]}
    )
    entries = out["diary"]

    oats = entries[0]
    assert oats["name"] == "Oats"
    assert oats["source"] == "USDA"
    assert oats["category"] == "Grains"
    assert oats["measure"] == {
        "measure_id": 10,
        "name": "cup",
        "grams_per_unit": 100.0,
    }
    assert oats["servings"] == 2.0  # 200g / 100g per cup
    # per-100g energy 389 scaled to 200g -> 778
    energy = next(n for n in oats["nutrients"] if n["id"] == 208)
    assert energy["amount"] == 778.0
    assert energy["name"] == "Energy"
    assert energy["unit"] == "kcal"

    milk = entries[1]
    # unknown measureId 999 -> fell back to defaultMeasureId 20
    assert milk["measure"]["measure_id"] == 20
    assert milk["measure"]["name"] == "glass"
    # 42 kcal/100g scaled to 50g -> 21
    assert next(n for n in milk["nutrients"] if n["id"] == 208)["amount"] == 21.0

    recipe = entries[2]
    assert recipe["name"] == "Recipe Food"
    assert recipe["measure"]["measure_id"] == 30
    assert recipe["measure"]["name"] == "serving"
    # Recipe measure: nutrients are per-serving and "grams" is a serving count,
    # so 708.538 kcal/serving * 1.1 servings -> 779.39 (not grams/100)
    energy = next(n for n in recipe["nutrients"] if n["id"] == 208)
    assert energy["amount"] == 779.3918
    protein = next(n for n in recipe["nutrients"] if n["id"] == 203)
    assert protein["amount"] == 61.633

    # Non-Serving entry untouched
    assert entries[3] == {"type": "Exercise", "name": "Running", "order": 1}


def test_enrich_diary_is_best_effort_when_get_foods_fails(tmp_path):
    client = _enrich_client(tmp_path)

    def boom(ids):
        raise CronometerError("network down")

    client.get_foods = boom  # type: ignore[method-assign]

    original = {"diary": [dict(e) for e in SAMPLE_DIARY["diary"]]}
    out = client.enrich_diary_servings(
        {"diary": [dict(e) for e in SAMPLE_DIARY["diary"]]}
    )

    # Entries returned unchanged (no name/nutrients added).
    assert out["diary"] == original["diary"]


def test_enrich_diary_no_servings_skips_lookup(tmp_path):
    client = _enrich_client(tmp_path)
    calls = {"n": 0}

    def track(ids):
        calls["n"] += 1
        return []

    client.get_foods = track  # type: ignore[method-assign]

    out = client.enrich_diary_servings(
        {"diary": [{"type": "Exercise", "name": "Running"}]}
    )
    assert calls["n"] == 0
    assert out["diary"] == [{"type": "Exercise", "name": "Running"}]
