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
# import_recipe
#
# Async flow: import_recipe returns a future id, poll_async_result is polled
# until it attaches a result, then matches are saved via add_food. Fixtures are
# the real bodies captured from the app for "one hot dog\nketchup\nbun".
# Verified against the live API (food 81033679).
# ---------------------------------------------------------------------------

IMPORT_JOB_ID = "69326e05-6f0e-4227-bd13-7a21e0fee9d6"

IMPORT_STARTED = {
    "isError": False,
    "progress": 0,
    "messages": [],
    "id": IMPORT_JOB_ID,
    "message": "Preparing your recipe for import.",
    "userId": 6407976,
}

IMPORT_IN_PROGRESS = {
    "isError": False,
    "progress": 10,
    "messages": [],
    "id": IMPORT_JOB_ID,
    "message": "Searching our database for the ingredients...",
    "userId": 6407976,
}


def _import_entry(raw, description, food_id, measure_id, grams, top_k):
    return {
        "amountSearchString": "Optional[1.0]",
        "unitSearchString": "",
        "ingredient": {
            "measureId": measure_id,
            "translationId": 0,
            "foodId": food_id,
            "id": 0,
            "grams": grams,
            "value": grams,
        },
        "rawIngredientImport": raw,
        "description": description,
        "topKFoodIds": top_k,
        "servingSizeMatchFound": True,
    }


IMPORT_ENTRIES = [
    _import_entry(
        "one hot dog", "hot dog", 4572, 12695, 45, [4572, 449773, 459646, 461995]
    ),
    _import_entry("ketchup", "ketchup", 449782, 992859, 1, [449782, 11239521, 4387294]),
    _import_entry("bun", "bun", 456806, 1030841, 43, [456806, 461989, 33638819]),
]

IMPORT_COMPLETE = {
    "result": {
        "entries": IMPORT_ENTRIES,
        "recipe": {
            "name": "Classic Hot Dog Bun",
            "id": 0,
            "ingredients": [e["ingredient"] for e in IMPORT_ENTRIES],
        },
    },
    "isError": False,
    "progress": 100,
    "messages": [],
    "id": IMPORT_JOB_ID,
    "message": "Finishing Up",
    "userId": 6407976,
}

# Ingredient foods the saved recipe resolves against, keyed to the captured IDs.
IMPORT_INGREDIENT_FOODS = [
    {
        "id": 4572,
        "name": "Hot Dog",
        "measures": [{"id": 12695, "name": "g", "value": 1, "type": "Weight"}],
        "nutrients": [{"id": 208, "amount": 290.0}],
    },
    {
        "id": 449782,
        "name": "Ketchup",
        "measures": [{"id": 992859, "name": "g", "value": 1, "type": "Weight"}],
        "nutrients": [{"id": 208, "amount": 100.0}],
    },
    {
        "id": 456806,
        "name": "Bun",
        "measures": [{"id": 1030841, "name": "g", "value": 1, "type": "Weight"}],
        "nutrients": [{"id": 208, "amount": 280.0}],
    },
]


def _import_client(tmp_path: Path, monkeypatch, poll_bodies):
    """Client whose async-job endpoints replay `poll_bodies` in order.

    Routes by endpoint so the trailing add_food save is recorded separately
    from polling traffic. time.sleep is neutered to keep the loop instant.
    """
    monkeypatch.setattr("cronometer_api_mcp.client.time.sleep", lambda _s: None)

    client = CronometerClient(session_path=tmp_path / "session.json")
    client._user_id = 42
    client._token = "TOKEN"
    client.get_foods = lambda ids: IMPORT_INGREDIENT_FOODS  # type: ignore[method-assign]

    state = {"calls": [], "polls": 0, "add_food": []}
    remaining = list(poll_bodies)

    def fake_post(endpoint, json=None):
        state["calls"].append(endpoint)
        if endpoint == "/api/v2/add_food":
            state["add_food"].append(json)
            return FakeResp({"result": "SUCCESS", "id": 76533895})
        if endpoint in ("/api/v2/import_recipe", "/api/v2/poll_async_result"):
            if endpoint == "/api/v2/poll_async_result":
                state["polls"] += 1
            return FakeResp(remaining.pop(0) if remaining else poll_bodies[-1])
        raise AssertionError(f"unexpected endpoint {endpoint}")

    client._http.post = fake_post  # type: ignore[method-assign]
    return client, state


def test_import_recipe_parses_polls_and_saves(tmp_path, monkeypatch):
    """Happy path: start, poll past an in-progress tick, then save the match."""
    client, state = _import_client(
        tmp_path,
        monkeypatch,
        [IMPORT_STARTED, IMPORT_IN_PROGRESS, IMPORT_COMPLETE],
    )

    result = client.import_recipe("one hot dog\nketchup\nbun")

    # Name comes from the server when the caller doesn't supply one.
    assert result["recipe_name"] == "Classic Hot Dog Bun"
    assert result["food_id"] == 76533895
    assert result["ingredient_count"] == 3
    assert result["total_grams"] == 89.0  # 45 + 1 + 43
    assert result["unmatched"] == []

    started = state["calls"][0]
    assert started == "/api/v2/import_recipe"
    assert state["polls"] == 2

    # One save, carrying the parser's food/measure/gram triples.
    assert len(state["add_food"]) == 1
    ingredients = state["add_food"][0]["data"]["ingredients"]
    assert [(i["foodId"], i["measureId"], i["grams"]) for i in ingredients] == [
        (4572, 12695, 45.0),
        (449782, 992859, 1.0),
        (456806, 1030841, 43.0),
    ]

    # Matches are reported back for review, alternates included.
    assert result["ingredients"][0]["raw_text"] == "one hot dog"
    assert result["ingredients"][0]["description"] == "hot dog"
    assert result["ingredients"][0]["alternate_food_ids"] == [
        4572,
        449773,
        459646,
        461995,
    ]


def test_import_recipe_sends_expected_payloads(tmp_path, monkeypatch):
    """Request bodies match the captured app traffic."""
    client, _state = _import_client(
        tmp_path, monkeypatch, [IMPORT_STARTED, IMPORT_COMPLETE]
    )
    sent = []
    inner = client._http.post

    def recording_post(endpoint, json=None):
        sent.append((endpoint, json))
        return inner(endpoint, json=json)

    client._http.post = recording_post  # type: ignore[method-assign]

    client.import_recipe("one hot dog\nketchup\nbun")

    _, start_body = sent[0]
    assert start_body["url"] == ""
    assert start_body["ingredients"] == "one hot dog\nketchup\nbun"
    assert start_body["enable_async"] is True

    _, poll_body = sent[1]
    assert poll_body["futureId"] == IMPORT_JOB_ID
    assert poll_body["resultType"] == "recipe"


def test_import_recipe_save_false_skips_persistence(tmp_path, monkeypatch):
    """Parse-only mode returns matches without creating a food."""
    client, state = _import_client(
        tmp_path, monkeypatch, [IMPORT_STARTED, IMPORT_COMPLETE]
    )

    result = client.import_recipe("one hot dog\nketchup\nbun", save=False)

    assert state["add_food"] == []
    assert "food_id" not in result
    assert len(result["ingredients"]) == 3


def test_import_recipe_explicit_name_overrides_server_name(tmp_path, monkeypatch):
    client, state = _import_client(
        tmp_path, monkeypatch, [IMPORT_STARTED, IMPORT_COMPLETE]
    )

    result = client.import_recipe("one hot dog\nketchup\nbun", name="Ballpark Dog")

    assert result["recipe_name"] == "Ballpark Dog"
    assert state["add_food"][0]["data"]["name"] == "Ballpark Dog"


def test_import_recipe_result_on_first_response_skips_polling(tmp_path, monkeypatch):
    """A job that finishes immediately needs no poll round trip."""
    immediate = IMPORT_STARTED | {
        "progress": 100,
        "result": IMPORT_COMPLETE["result"],
    }
    client, state = _import_client(tmp_path, monkeypatch, [immediate])

    result = client.import_recipe("one hot dog")

    assert state["polls"] == 0
    assert result["food_id"] == 76533895


def test_import_recipe_server_error_raises(tmp_path, monkeypatch):
    """isError on the kickoff response fails loudly and saves nothing."""
    failed = {
        "isError": True,
        "progress": 0,
        "id": IMPORT_JOB_ID,
        "message": "Could not parse those ingredients.",
    }
    client, state = _import_client(tmp_path, monkeypatch, [failed])

    with pytest.raises(CronometerError, match="Could not parse those ingredients"):
        client.import_recipe("asdfgh")

    assert state["add_food"] == []


def test_import_recipe_poll_error_raises(tmp_path, monkeypatch):
    """isError surfacing mid-poll aborts the import."""
    failed = {"isError": True, "id": IMPORT_JOB_ID, "message": "Import failed"}
    client, state = _import_client(tmp_path, monkeypatch, [IMPORT_STARTED, failed])

    with pytest.raises(CronometerError, match="Import failed"):
        client.import_recipe("one hot dog")

    assert state["add_food"] == []


def test_import_recipe_times_out(tmp_path, monkeypatch):
    """A job that never completes raises rather than polling forever."""
    client, state = _import_client(
        tmp_path, monkeypatch, [IMPORT_STARTED, IMPORT_IN_PROGRESS]
    )

    # Advance the clock a minute per reading so the deadline trips quickly.
    ticks = iter(range(0, 10_000, 60))
    monkeypatch.setattr(
        "cronometer_api_mcp.client.time.monotonic", lambda: float(next(ticks))
    )

    with pytest.raises(CronometerError, match="did not finish within"):
        client.import_recipe("one hot dog", timeout=30.0)

    assert state["add_food"] == []


def test_import_recipe_finished_without_result_raises(tmp_path, monkeypatch):
    """100% progress with no payload is a server bug, not a silent success."""
    empty = {"isError": False, "progress": 100, "id": IMPORT_JOB_ID, "messages": []}
    client, state = _import_client(tmp_path, monkeypatch, [IMPORT_STARTED, empty])

    with pytest.raises(CronometerError, match="without a result"):
        client.import_recipe("one hot dog")

    assert state["add_food"] == []


def test_import_recipe_separates_unmatched_lines(tmp_path, monkeypatch):
    """Unresolvable lines are reported, not fed to create_recipe."""
    partial = {
        "isError": False,
        "progress": 100,
        "id": IMPORT_JOB_ID,
        "result": {
            "recipe": {"name": "Partial Recipe"},
            "entries": [
                IMPORT_ENTRIES[0],
                # No food matched.
                _import_entry("a pinch of unobtainium", "unobtainium", 0, 0, 0, []),
                # Matched a food but weighed nothing.
                _import_entry("air", "air", 999, 111, 0, [999]),
            ],
        },
    }
    client, state = _import_client(tmp_path, monkeypatch, [IMPORT_STARTED, partial])

    result = client.import_recipe("one hot dog\na pinch of unobtainium\nair")

    assert [i["raw_text"] for i in result["ingredients"]] == ["one hot dog"]
    assert [u["raw_text"] for u in result["unmatched"]] == [
        "a pinch of unobtainium",
        "air",
    ]
    # Only the usable line reaches the save.
    saved = state["add_food"][0]["data"]["ingredients"]
    assert [i["foodId"] for i in saved] == [4572]


def test_import_recipe_all_unmatched_raises(tmp_path, monkeypatch):
    """Nothing usable means no empty recipe gets created."""
    nothing = {
        "isError": False,
        "progress": 100,
        "id": IMPORT_JOB_ID,
        "result": {
            "recipe": {"name": "Empty"},
            "entries": [_import_entry("unobtainium", "unobtainium", 0, 0, 0, [])],
        },
    }
    client, state = _import_client(tmp_path, monkeypatch, [IMPORT_STARTED, nothing])

    with pytest.raises(CronometerError, match="no usable ingredients"):
        client.import_recipe("unobtainium")

    assert state["add_food"] == []


def test_import_recipe_rejects_blank_input(tmp_path, monkeypatch):
    """Validation happens before any network call."""
    client, state = _import_client(tmp_path, monkeypatch, [IMPORT_STARTED])

    with pytest.raises(ValueError):
        client.import_recipe("   \n  ")

    assert state["calls"] == []


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
