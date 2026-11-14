"""get_diary_groups: diary group (meal slot) names from the DGnn profile prefs."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from test_client import make_cold_client

from cronometer_api_mcp import client as client_module


def profile(**prefs) -> dict:
    """A profile response; prefs arrive as a list of one-key objects."""
    return {"result": "SUCCESS", "prefs": [{k: v} for k, v in prefs.items()]}


# An account that inserted "Post-workout" and "Pre-bedtime" into the defaults.
CUSTOM_GROUPS = {
    "DG01": "Exercise and Biometrics",
    "DG01ON": "true",
    "DG02": "Breakfast",
    "DG02ON": "true",
    "DG03": "Post-workout",
    "DG03ON": "true",
    "DG04": "Lunch",
    "DG04ON": "true",
    "DG05": "Dinner",
    "DG05ON": "true",
    "DG06": "Pre-bedtime",
    "DG06ON": "true",
    "DG07": "Snacks",
    "DG07ON": "true",
    "DG08": "Group 8",
    "DG08ON": "false",
    "DG_ON": "true",
    "weightUnit": "Pounds",
}


def test_group_index_is_pref_number_minus_one(tmp_path):
    client, _ = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])

    groups = client.get_diary_groups()

    by_name = {g["name"]: g for g in groups}
    assert by_name["Exercise and Biometrics"]["group"] == 0
    assert by_name["Breakfast"]["group"] == 1
    assert by_name["Lunch"]["group"] == 3
    assert by_name["Dinner"]["group"] == 4
    assert by_name["Snacks"]["group"] == 6
    assert by_name["Dinner"]["pref"] == "DG05"


def test_group_matches_the_order_of_a_logged_entry(tmp_path):
    """order >> 16 is the group: 196609 is Lunch on this account, not Dinner."""
    client, _ = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])

    names = {g["group"]: g["name"] for g in client.get_diary_groups()}

    assert names[196609 >> 16] == "Lunch"
    assert names[65537 >> 16] == "Breakfast"


def test_groups_are_sorted_and_only_dg_prefs_are_included(tmp_path):
    shuffled = dict(reversed(list(CUSTOM_GROUPS.items())))
    client, _ = make_cold_client(tmp_path, [profile(**shuffled)])

    groups = client.get_diary_groups()

    assert [g["group"] for g in groups] == list(range(8))
    # DGnnON twins, DG_ON and unrelated prefs are not groups.
    assert all(g["pref"].startswith("DG") and g["pref"][2:].isdigit() for g in groups)
    assert len(groups) == 8


def test_enabled_reads_the_on_twin(tmp_path):
    client, _ = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])

    enabled = {g["name"]: g["enabled"] for g in client.get_diary_groups()}

    assert enabled["Lunch"] is True
    assert enabled["Group 8"] is False


def test_missing_on_twin_defaults_to_enabled_and_blank_name_gets_fallback(tmp_path):
    client, _ = make_cold_client(tmp_path, [profile(DG02="Breakfast", DG03="")])

    groups = client.get_diary_groups()

    assert groups[0] == {
        "group": 1,
        "pref": "DG02",
        "name": "Breakfast",
        "enabled": True,
    }
    assert groups[1]["name"] == "Group 3"


def test_no_dg_prefs_returns_empty_list(tmp_path):
    client, _ = make_cold_client(tmp_path, [profile(weightUnit="Pounds")])

    assert client.get_diary_groups() == []


def test_get_diary_groups_tool(tmp_path, monkeypatch):
    from cronometer_api_mcp import server

    client, _ = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])
    monkeypatch.setattr(server, "_client", client)

    result = json.loads(server.get_diary_groups())

    assert result["status"] == "success"
    assert result["groups"][3] == {
        "group": 3,
        "pref": "DG04",
        "name": "Lunch",
        "enabled": True,
    }


# ---------------------------------------------------------------------------
# resolve_diary_group
# ---------------------------------------------------------------------------


def test_resolve_names_use_the_accounts_numbers(tmp_path):
    """On this account "dinner" is 4, not the hardcoded 3 (which is Lunch)."""
    client, _ = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])

    assert client.resolve_diary_group("dinner") == {"group": 4, "name": "Dinner"}
    assert client.resolve_diary_group("Lunch")["group"] == 3
    assert client.resolve_diary_group("  SNACKS ")["group"] == 6
    assert client.resolve_diary_group("post-workout")["group"] == 2
    assert client.resolve_diary_group("Pre-bedtime")["group"] == 5


def test_resolve_auto_is_zero_without_reading_the_profile(tmp_path):
    client, state = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])

    assert client.resolve_diary_group("auto") == {"group": 0, "name": "auto"}
    assert client.resolve_diary_group(" AUTO ")["group"] == 0
    assert state["post"] == 0


def test_resolve_reuses_the_cache(tmp_path):
    client, state = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])

    client.resolve_diary_group("dinner")
    client.resolve_diary_group("lunch")

    assert state["post"] == 1


def test_resolve_refreshes_once_on_a_miss(tmp_path):
    """A group renamed in the app after the cache filled is found on retry."""
    renamed = {**CUSTOM_GROUPS, "DG06": "Supper"}
    client, state = make_cold_client(
        tmp_path, [profile(**CUSTOM_GROUPS), profile(**renamed)]
    )

    assert client.resolve_diary_group("dinner")["group"] == 4  # fills the cache
    assert client.resolve_diary_group("supper")["group"] == 5  # miss, refetch
    assert state["post"] == 2


def test_resolve_unknown_name_on_a_cold_cache_fetches_only_once(tmp_path):
    client, state = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])

    with pytest.raises(ValueError):
        client.resolve_diary_group("brunch")

    assert state["post"] == 1


def test_resolve_expired_cache_is_read_again(tmp_path, monkeypatch):
    client, state = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])
    clock = [1000.0]
    monkeypatch.setattr(client_module.time, "monotonic", lambda: clock[0])

    client.resolve_diary_group("dinner")
    clock[0] += client_module._DIARY_GROUPS_TTL_SECONDS - 1
    client.resolve_diary_group("dinner")
    assert state["post"] == 1

    clock[0] += 2
    client.resolve_diary_group("dinner")
    assert state["post"] == 2


def test_resolve_unknown_name_lists_the_valid_ones(tmp_path):
    client, _ = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])

    with pytest.raises(ValueError) as exc:
        client.resolve_diary_group("brunch")

    message = str(exc.value)
    assert "Invalid diary_group 'brunch'" in message
    for name in ("auto", "Breakfast", "Post-workout", "Dinner", "Snacks"):
        assert name in message
    # Hidden and sentinel groups are not offered.
    assert "Group 8" not in message
    assert "Exercise and Biometrics" not in message


def test_resolve_rejects_a_disabled_group(tmp_path):
    client, _ = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])

    with pytest.raises(ValueError, match="disabled"):
        client.resolve_diary_group("group 8")


def test_resolve_rejects_group_zero_by_name(tmp_path):
    """0 is the auto sentinel, so group 0 can't be addressed by name."""
    client, _ = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])

    with pytest.raises(ValueError, match="auto"):
        client.resolve_diary_group("Exercise and Biometrics")


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


def test_add_food_entry_writes_to_the_named_group_and_echoes_it(tmp_path, monkeypatch):
    from cronometer_api_mcp import server

    client, state = make_cold_client(
        tmp_path, [profile(**CUSTOM_GROUPS), {"result": "SUCCESS", "id": 7}]
    )
    monkeypatch.setattr(server, "_client", client)

    result = json.loads(
        server.add_food_entry(food_id=1, measure_id=0, grams=100, diary_group="dinner")
    )

    assert result["status"] == "success"
    assert result["diary_group"] == {"group": 4, "name": "Dinner"}
    serving = state["payloads"][-1]["serving"]
    assert serving["order"] == (4 << 16) | 1


def test_add_food_entry_auto_keeps_the_hour_based_numbers(tmp_path, monkeypatch):
    """auto is unchanged: hour -> 1-4, with no profile read and no echo."""
    from cronometer_api_mcp import server

    client, state = make_cold_client(tmp_path, [{"result": "SUCCESS", "id": 7}])
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(
        client, "now", lambda: datetime(2026, 11, 13, 19, 8, tzinfo=UTC)
    )

    result = json.loads(server.add_food_entry(food_id=1, measure_id=0, grams=100))

    assert result["status"] == "success"
    assert "diary_group" not in result
    assert state["post"] == 1  # just the add_serving call
    assert state["payloads"][-1]["serving"]["order"] == (3 << 16) | 1


def test_add_food_entry_unknown_group_returns_an_error(tmp_path, monkeypatch):
    from cronometer_api_mcp import server

    client, state = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])
    monkeypatch.setattr(server, "_client", client)

    result = json.loads(
        server.add_food_entry(food_id=1, measure_id=0, grams=100, diary_group="brunch")
    )

    assert result["status"] == "error"
    assert "Invalid diary_group 'brunch'" in result["message"]
    assert state["post"] == 1  # the profile read; nothing was written


class _StubClient:
    """Records the group number each write tool passes down."""

    def __init__(self):
        self.calls: list[tuple] = []

    def resolve_diary_group(self, name):
        return {"group": 4, "name": "Dinner"}

    def add_note(self, text, day, group):
        self.calls.append(("note", group))
        return {"noteId": 1}

    def add_exercise(self, activity_id, minutes, calories, day, group):
        self.calls.append(("exercise", group))
        return {"exerciseId": 2}


def test_add_note_and_add_exercise_use_the_resolved_group(monkeypatch):
    from cronometer_api_mcp import server

    stub = _StubClient()
    monkeypatch.setattr(server, "_client", stub)

    note = json.loads(server.add_note("late snack", diary_group="dinner"))
    exercise = json.loads(server.add_exercise(1, 30, 200, diary_group="dinner"))

    assert stub.calls == [("note", 4), ("exercise", 4)]
    assert note["diary_group"] == {"group": 4, "name": "Dinner"}
    assert exercise["diary_group"] == {"group": 4, "name": "Dinner"}


# ---------------------------------------------------------------------------
# label_diary_groups (get_food_log)
# ---------------------------------------------------------------------------


def diary_of(*entries: dict) -> dict:
    return {"diary": list(entries)}


def test_label_diary_groups_names_every_entry_type(tmp_path):
    client, _ = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])
    diary = diary_of(
        {"type": "Serving", "order": 65537},
        {"type": "Serving", "order": 196609},  # the order that looked like dinner
        {"type": "Serving", "order": 262145},
        {"type": "Note", "order": 327681},
        {"type": "Exercise", "order": 1},
        {"type": "Biometric", "order": 1},
    )

    out = client.label_diary_groups(diary)

    assert out is diary
    labels = [e["diary_group"] for e in diary["diary"]]
    assert labels == [
        {"group": 1, "name": "Breakfast"},
        {"group": 3, "name": "Lunch"},
        {"group": 4, "name": "Dinner"},
        {"group": 5, "name": "Pre-bedtime"},
        {"group": 0, "name": "Exercise and Biometrics"},
        {"group": 0, "name": "Exercise and Biometrics"},
    ]


def test_label_diary_groups_unknown_group_has_null_name(tmp_path):
    client, _ = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])
    diary = diary_of({"type": "Serving", "order": (12 << 16) | 1})

    client.label_diary_groups(diary)

    assert diary["diary"][0]["diary_group"] == {"group": 12, "name": None}


def test_label_diary_groups_names_a_disabled_group(tmp_path):
    client, _ = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])
    diary = diary_of({"type": "Serving", "order": (7 << 16) | 1})

    client.label_diary_groups(diary)

    assert diary["diary"][0]["diary_group"]["name"] == "Group 8"


def test_label_diary_groups_skips_entries_without_a_usable_order(tmp_path):
    client, _ = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])
    diary = diary_of(
        {"type": "Serving"},
        {"type": "Serving", "order": None},
        {"type": "Serving", "order": True},
        {"type": "Serving", "order": -5},
        {"type": "Serving", "order": 196609},
    )

    client.label_diary_groups(diary)

    labeled = ["diary_group" in e for e in diary["diary"]]
    assert labeled == [False, False, False, False, True]


def test_label_diary_groups_failure_leaves_the_diary_unlabeled(tmp_path, monkeypatch):
    client, _ = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])

    def boom():
        raise RuntimeError("profile down")

    monkeypatch.setattr(client, "get_diary_groups", boom)
    diary = diary_of({"type": "Serving", "order": 196609})

    out = client.label_diary_groups(diary)

    assert out is diary
    assert "diary_group" not in diary["diary"][0]


def test_label_diary_groups_reads_the_profile_only_when_needed(tmp_path):
    client, state = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])

    client.label_diary_groups(diary_of())
    client.label_diary_groups(diary_of({"type": "Serving"}))
    client.label_diary_groups({"no": "diary"})
    assert state["post"] == 0

    client.label_diary_groups(diary_of({"type": "Serving", "order": 196609}))
    client.label_diary_groups(diary_of({"type": "Serving", "order": 65537}))
    assert state["post"] == 1  # second call reuses the cache


def test_label_diary_groups_reuses_groups_cached_by_a_write(tmp_path):
    client, state = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])

    client.resolve_diary_group("dinner")
    diary = diary_of({"type": "Serving", "order": 262145})
    client.label_diary_groups(diary)

    assert state["post"] == 1
    assert diary["diary"][0]["diary_group"]["name"] == "Dinner"
