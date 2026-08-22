"""Regression tests for CronometerClient auth-failure handling.

Covers issue #18: an expired session comes back as an HTTP 200 body of
{"result": "FAIL", ...} which the client previously failed to detect
(it only matched "FAILURE"), so a stale token was never invalidated and
the failure was surfaced to callers as a success.
"""

from __future__ import annotations

import threading
import time
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


# ---------------------------------------------------------------------------
# create_recipe
#
# Recipes post to the same /api/v2/add_food endpoint as custom foods; the
# `ingredients` array is what makes a food a recipe. Nutrients are aggregated
# from the ingredients' per-100g profiles and re-normalized to per-100g of the
# finished batch. Verified against the live API (food 79474948).
# ---------------------------------------------------------------------------

RECIPE_INGREDIENT_FOODS = [
    {
        "id": 1000,
        "name": "Sardines",
        "defaultMeasureId": 11,
        "measures": [
            {"id": 10, "name": "oz", "value": 28.3495231, "type": "Weight"},
            {"id": 11, "name": "g", "value": 1, "type": "Weight"},
        ],
        "translations": [{"translationId": 555, "name": "Sardines"}],
        # per-100g
        "nutrients": [{"id": 208, "amount": 200.0}, {"id": 203, "amount": 25.0}],
    },
    {
        "id": 2000,
        "name": "Marshmallow Creme",
        "defaultMeasureId": 21,
        "measures": [{"id": 21, "name": "g", "value": 1, "type": "Weight"}],
        # no translations -> translationId falls back to 0
        "nutrients": [{"id": 208, "amount": 300.0}, {"id": 205, "amount": 60.0}],
    },
]


def _recipe_client(tmp_path: Path, responses=None):
    client, state = make_cold_client(
        tmp_path, responses or [{"result": "SUCCESS", "id": 4242}]
    )
    client.get_foods = lambda ids: RECIPE_INGREDIENT_FOODS  # type: ignore[method-assign]
    return client, state


def test_create_recipe_payload_shape(tmp_path):
    """Ingredients, weight-based measures, and per-100g aggregate nutrients."""
    client, state = _recipe_client(tmp_path)

    result = client.create_recipe(
        "Test Recipe", ingredients=[(1000, 100.0), (2000, 50.0)]
    )

    assert result == {"food_id": 4242, "total_grams": 150.0, "ingredient_count": 2}
    data = state["payloads"][0]["data"]

    # A new food, with ingredients -- the marker that makes it a recipe.
    assert data["id"] == 0
    assert data["defaultMeasureId"] == 0
    assert data["properties"] == {"advancedServingSize": "false"}
    assert data["ingredients"] == [
        {
            "id": 0,
            "foodId": 1000,
            "measureId": 11,  # resolved to the 1-gram measure, not defaultMeasureId
            "translationId": 555,
            "grams": 100.0,
            "value": 100.0,
        },
        {
            "id": 0,
            "foodId": 2000,
            "measureId": 21,
            "translationId": 0,  # no translations on this food
            "grams": 50.0,
            "value": 50.0,
        },
    ]

    # Weight-based: every measure is type "Weight" (not "Recipe"), so the diary
    # treats logged amounts as real grams rather than a serving count.
    assert [m["type"] for m in data["measures"]] == ["Weight"] * 4
    by_name = {m["name"]: m["value"] for m in data["measures"]}
    assert by_name == {
        "Serving": 150.0,  # defaults to the full batch
        "g": 1.0,
        "oz": 28.3495231,
        "full recipe": 150.0,
    }
    # The serving measure leads, since the server makes the first one default.
    assert data["measures"][0]["name"] == "Serving"

    # Batch: 200*1.0 + 300*0.5 = 350 kcal over 150g -> 233.333333 per 100g.
    amounts = {n["id"]: n["amount"] for n in data["nutrients"]}
    assert amounts[208] == 233.333333
    assert amounts[203] == round(25.0 * 100 / 150, 6)  # only in the sardines
    assert amounts[205] == round(60.0 * 50 / 150, 6)  # only in the creme


def test_create_recipe_explicit_serving_and_comments(tmp_path):
    client, state = _recipe_client(tmp_path)

    client.create_recipe(
        "Test Recipe",
        ingredients=[(1000, 100.0), (2000, 50.0)],
        serving_name="bowl",
        serving_grams=75.0,
        comments="wildly inadvisable",
    )

    data = state["payloads"][0]["data"]
    assert data["measures"][0] == {
        "id": 0,
        "name": "bowl",
        "value": 75.0,
        "amount": 1.0,
        "type": "Weight",
    }
    # An explicit serving size must not change the batch weight.
    assert {m["name"]: m["value"] for m in data["measures"]}["full recipe"] == 150.0
    assert data["comments"] == "wildly inadvisable"


def test_create_recipe_measure_id_override(tmp_path):
    """A 3-tuple overrides the auto-resolved display measure."""
    client, state = _recipe_client(tmp_path)

    client.create_recipe("Test Recipe", ingredients=[(1000, 100.0, 10)])

    assert state["payloads"][0]["data"]["ingredients"][0]["measureId"] == 10


def test_create_recipe_rejects_empty_and_malformed(tmp_path):
    client, state = _recipe_client(tmp_path)

    with pytest.raises(ValueError):
        client.create_recipe("Test Recipe", ingredients=[])

    with pytest.raises(ValueError):
        client.create_recipe("Test Recipe", ingredients=[(1000,)])

    with pytest.raises(ValueError):
        client.create_recipe("Test Recipe", ingredients=[(1000, 0)])

    # Validation happens before any network call.
    assert state["post"] == 0


def test_create_recipe_missing_ingredient_food_raises(tmp_path):
    """An unresolvable food ID fails loudly rather than silently dropping it."""
    client, state = _recipe_client(tmp_path)

    with pytest.raises(CronometerError):
        client.create_recipe("Test Recipe", ingredients=[(1000, 100.0), (9999, 10.0)])

    assert state["post"] == 0


# ---------------------------------------------------------------------------
# create_custom_food: extra_nutrients
# ---------------------------------------------------------------------------


def test_create_custom_food_extra_nutrients_payload(tmp_path):
    """extra_nutrients are appended, normalized to per-100g like the macros."""
    client, state = make_cold_client(tmp_path, [{"result": "SUCCESS", "id": 555}])

    client.create_custom_food(
        "Test Food",
        calories=200,
        protein_g=10,
        fat_g=5,
        carbs_g=20,
        serving_grams=200.0,  # scale = 0.5
        extra_nutrients={601: 30.0, 430: 10.0},  # cholesterol, vitamin K
    )

    nutrients = state["payloads"][0]["data"]["nutrients"]
    by_id = {n["id"]: n["amount"] for n in nutrients}
    assert by_id[601] == 15.0  # 30 * 0.5
    assert by_id[430] == 5.0  # 10 * 0.5


def test_create_custom_food_extra_nutrients_overlap_raises(tmp_path):
    """Reusing an ID the named macro args already write is rejected, not
    silently duplicated or shadowed."""
    client, _ = make_client(tmp_path, [{"result": "SUCCESS", "id": 555}])

    with pytest.raises(ValueError):
        client.create_custom_food(
            "Test Food",
            calories=200,
            protein_g=10,
            fat_g=5,
            carbs_g=20,
            extra_nutrients={204: 5.0},  # fat -- already set by fat_g
        )


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


# ---------------------------------------------------------------------------
# Concurrent auth (MCP SDK 2.x)
#
# The SDK runs sync tool handlers on worker threads, so tool calls share one
# client concurrently where 1.x serialized them. Auth is a read-modify-write
# over (_user_id, _token, _timezone) plus a session-file write, so an unguarded
# burst logs in once per thread against a rate-limited endpoint (#3).
# ---------------------------------------------------------------------------

THREADS = 8


def test_concurrent_cold_start_logs_in_once(tmp_path):
    """A burst of first-calls on a cold client triggers exactly one login."""
    client = CronometerClient(session_path=tmp_path / "session.json")
    client._user_id = None
    client._token = None

    state = {"login": 0}
    start = threading.Barrier(THREADS)

    def fake_login() -> None:
        # Widen the race window: without the lock every thread has already
        # passed the `_token is None` check by the time the first one stores.
        state["login"] += 1
        time.sleep(0.05)
        client._user_id = 42
        client._token = "FRESH_TOKEN"

    client.login = fake_login  # type: ignore[method-assign]

    def worker() -> None:
        start.wait()
        client._ensure_auth()

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["login"] == 1
    assert client._token == "FRESH_TOKEN"


def test_concurrent_expired_token_logs_in_once(tmp_path):
    """Concurrent 401s on one stale token collapse into a single re-login."""
    client = CronometerClient(session_path=tmp_path / "session.json")
    client._user_id = 123
    client._token = "STALE_TOKEN"

    state = {"login": 0}
    lock = threading.Lock()
    start = threading.Barrier(THREADS)

    def fake_login() -> None:
        with lock:
            state["login"] += 1
            n = state["login"]
        time.sleep(0.05)
        client._user_id = 123
        client._token = f"FRESH_TOKEN_{n}"

    def fake_post(endpoint, json=None):
        # Reject the stale token, accept anything issued by a login.
        if json["auth"]["token"] == "STALE_TOKEN":
            return FakeResp(REAL_FAIL_BODY)
        return FakeResp({"result": "SUCCESS"})

    client.login = fake_login  # type: ignore[method-assign]
    client._http.post = fake_post  # type: ignore[method-assign]

    results = []

    def worker() -> None:
        start.wait()
        results.append(client._request("/api/v2/get_diary", {}))

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["login"] == 1
    assert results == [{"result": "SUCCESS"}] * THREADS


def test_reauthenticate_skips_login_when_token_already_refreshed(tmp_path):
    """A caller holding an already-replaced token must not force a new login."""
    client = CronometerClient(session_path=tmp_path / "session.json")
    client._user_id = 123
    client._token = "FRESH_TOKEN"

    state = {"login": 0}

    def fake_login() -> None:
        state["login"] += 1

    client.login = fake_login  # type: ignore[method-assign]

    client._reauthenticate("STALE_TOKEN")

    assert state["login"] == 0
    assert client._token == "FRESH_TOKEN"


def test_reauthenticate_logs_in_when_token_is_still_stale(tmp_path):
    """The thread that actually holds the current (stale) token does re-login."""
    client = CronometerClient(session_path=tmp_path / "session.json")
    client._user_id = 123
    client._token = "STALE_TOKEN"

    state = {"login": 0}

    def fake_login() -> None:
        state["login"] += 1
        client._token = "FRESH_TOKEN"

    client.login = fake_login  # type: ignore[method-assign]

    client._reauthenticate("STALE_TOKEN")

    assert state["login"] == 1
    assert client._token == "FRESH_TOKEN"
