"""get_diary_groups: diary group (meal slot) names from the DGnn profile prefs."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from test_client import make_cold_client
from test_notes import capture_forms

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

BREAKFAST_FIRST = {
    "DG01": "Breakfast",
    "DG01ON": "true",
    "DG02": "Lunch",
    "DG02ON": "true",
    "DG03": "Dinner",
    "DG03ON": "true",
    "DG04": "Snacks",
    "DG04ON": "true",
    "DG05": "Uncategorized",
    "DG05ON": "true",
    "DG06": "Post workout",
    "DG06ON": "false",
    "DG07": "Group 7",
    "DG07ON": "false",
    "DG08": "Group 8",
    "DG08ON": "false",
}

WRITE_TOOLS = [
    ("add_food_entry", {"food_id": 1, "measure_id": 0, "grams": 100}),
    ("add_note", {"text": "late snack"}),
    ("add_exercise", {"activity_id": 1, "minutes": 30, "calories": 200}),
]


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


def test_blank_name_gets_fallback(tmp_path):
    client, _ = make_cold_client(tmp_path, [profile(DG02="Breakfast", DG03="")])

    groups = client.get_diary_groups()

    assert groups[1] == {
        "group": 1,
        "pref": "DG02",
        "name": "Breakfast",
        "enabled": True,
    }
    assert groups[2]["name"] == "Group 3"


def test_no_dg_prefs_uses_cronometer_defaults(tmp_path):
    client, _ = make_cold_client(tmp_path, [profile(weightUnit="Pounds")])

    groups = client.get_diary_groups()

    assert [g["name"] for g in groups] == [
        "Uncategorized",
        "Breakfast",
        "Lunch",
        "Dinner",
        "Snacks",
        "Group 6",
        "Group 7",
        "Group 8",
    ]
    assert [g["enabled"] for g in groups] == [
        True,
        True,
        True,
        True,
        True,
        False,
        False,
        False,
    ]
    assert [g["group"] for g in groups] == list(range(8))
    assert [g["pref"] for g in groups] == [f"DG{i:02}" for i in range(1, 9)]


def test_partial_prefs_override_names_and_visibility_independently(tmp_path):
    client, _ = make_cold_client(
        tmp_path, [profile(DG02ON="false", DG03="Brunch", DG06ON="true")]
    )

    groups = client.get_diary_groups()

    assert groups[1]["name"] == "Breakfast"
    assert groups[1]["enabled"] is False
    assert groups[2]["name"] == "Brunch"
    assert groups[2]["enabled"] is True
    assert groups[3]["name"] == "Dinner"
    assert groups[5]["name"] == "Group 6"
    assert groups[5]["enabled"] is True


@pytest.mark.parametrize("pref", ["DG06", "DG07", "DG08"])
def test_extra_groups_without_visibility_prefs_remain_disabled(tmp_path, pref):
    client, _ = make_cold_client(tmp_path, [profile(**{pref: "Extra meal"})])

    group = next(g for g in client.get_diary_groups() if g["pref"] == pref)

    assert group["enabled"] is False
    with pytest.raises(ValueError, match="disabled"):
        client.resolve_diary_group("Extra meal")


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


def test_resolve_auto_defers_selection_until_the_entry_time_is_known(tmp_path):
    client, state = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])

    assert client.resolve_diary_group("auto") == {"group": None, "name": "auto"}
    assert client.resolve_diary_group(" AUTO ")["group"] is None
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
    assert "Group 8" not in message
    assert "Exercise and Biometrics" in message


def test_resolve_rejects_a_disabled_group(tmp_path):
    client, _ = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])

    with pytest.raises(ValueError, match="disabled"):
        client.resolve_diary_group("group 8")


def test_resolve_accepts_group_zero_by_name(tmp_path):
    client, _ = make_cold_client(tmp_path, [profile(**CUSTOM_GROUPS)])

    assert client.resolve_diary_group("Exercise and Biometrics") == {
        "group": 0,
        "name": "Exercise and Biometrics",
    }


@pytest.mark.parametrize(
    "stored,requested",
    [
        ("Petit déjeuner", "  PETIT\tDÉJEUNER "),
        ("Dîner", "DI\u0302NER"),
        ("Straße", "STRASSE"),
        ("夕食", "夕 食"),
    ],
)
def test_resolve_localized_names_ignores_case_and_whitespace(
    tmp_path, stored, requested
):
    client, _ = make_cold_client(
        tmp_path, [profile(**{**BREAKFAST_FIRST, "DG01": stored})]
    )

    assert client.resolve_diary_group(requested) == {"group": 0, "name": stored}


def test_resolve_does_not_translate_or_strip_accents(tmp_path):
    client, _ = make_cold_client(
        tmp_path, [profile(**{**BREAKFAST_FIRST, "DG03": "Dîner"})]
    )

    for name in ("Dinner", "Diner"):
        with pytest.raises(ValueError, match="Invalid diary_group"):
            client.resolve_diary_group(name)


@pytest.mark.parametrize(
    "overrides",
    [
        {"DG02": " dinner ", "DG02ON": "false"},
        {"DG01": " DINNER ", "DG01ON": "false"},
        {"DG02": "Dinner", "DG02ON": "false", "DG08": "dinner"},
    ],
)
def test_resolve_ignores_disabled_groups_with_the_same_name(tmp_path, overrides):
    client, _ = make_cold_client(tmp_path, [profile(**{**CUSTOM_GROUPS, **overrides})])

    assert client.resolve_diary_group("dinner") == {"group": 4, "name": "Dinner"}


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,group", [("Breakfast", 1), ("Lunch", 2), ("Dinner", 3), ("Snacks", 4)]
)
def test_add_food_entry_uses_default_groups_when_prefs_are_unset(
    tmp_path, monkeypatch, name, group
):
    from cronometer_api_mcp import server

    client, state = make_cold_client(
        tmp_path, [profile(), {"result": "SUCCESS", "id": 7}]
    )
    monkeypatch.setattr(server, "_client", client)

    result = json.loads(
        server.add_food_entry(food_id=1, measure_id=0, grams=100, diary_group=name)
    )

    assert result["status"] == "success"
    assert result["diary_group"] == {"group": group, "name": name}
    assert state["payloads"][-1]["serving"]["order"] == (group << 16) | 1


@pytest.mark.parametrize("tool,kwargs", WRITE_TOOLS)
@pytest.mark.parametrize("pref,numbers", [("DG01", "0, 4"), ("DG03", "2, 4")])
def test_ambiguous_group_names_do_not_write(
    tmp_path, monkeypatch, tool, kwargs, pref, numbers
):
    from cronometer_api_mcp import server

    client, state = make_cold_client(
        tmp_path, [profile(**{**CUSTOM_GROUPS, pref: " DIN NER "})]
    )
    monkeypatch.setattr(server, "_client", client)
    forms = capture_forms(client)

    result = json.loads(getattr(server, tool)(**kwargs, diary_group="dinner"))

    assert result["status"] == "error"
    assert "Ambiguous diary_group 'dinner'" in result["message"]
    assert numbers in result["message"]
    assert state["post"] == 1
    assert forms == []


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


@pytest.mark.parametrize("tool,kwargs", WRITE_TOOLS)
@pytest.mark.parametrize(
    "prefs,name,group",
    [
        (BREAKFAST_FIRST, "Breakfast", 0),
        (BREAKFAST_FIRST, "Uncategorized", 4),
        ({}, "Uncategorized", 0),
        (CUSTOM_GROUPS, "Pre-bedtime", 5),
    ],
)
def test_named_groups_are_written_and_echoed(
    tmp_path, monkeypatch, tool, kwargs, prefs, name, group
):
    from cronometer_api_mcp import server

    account = profile(**{**prefs, "weightInKG": 80})
    responses = [account] * (2 if tool == "add_exercise" else 1)
    client, state = make_cold_client(
        tmp_path, [*responses, {"result": "SUCCESS", "id": 7}]
    )
    monkeypatch.setattr(server, "_client", client)
    forms = capture_forms(client)

    result = json.loads(getattr(server, tool)(**kwargs, diary_group=name))

    assert result["status"] == "success"
    assert result["diary_group"] == {"group": group, "name": name}
    if tool == "add_note":
        assert int(forms[0]["order"]) == (group << 16) | 1
    else:
        entry = "exercise" if tool == "add_exercise" else "serving"
        assert state["payloads"][-1][entry]["order"] == (group << 16) | 1


@pytest.mark.parametrize("tool,kwargs", WRITE_TOOLS)
@pytest.mark.parametrize(
    "prefs,groups",
    [
        (BREAKFAST_FIRST, (0, 1, 2, 3)),
        (CUSTOM_GROUPS, (1, 3, 4, 6)),
        ({}, (1, 2, 3, 4)),
    ],
    ids=["breakfast-first", "custom", "missing-prefs"],
)
@pytest.mark.parametrize(
    "hour,meal", [(3, 3), (4, 0), (9, 0), (10, 1), (13, 1), (14, 2), (20, 2), (21, 3)]
)
def test_auto_writes_to_account_groups_using_the_entry_time(
    tmp_path, monkeypatch, tool, kwargs, prefs, groups, hour, meal
):
    from cronometer_api_mcp import server

    account = profile(**{**prefs, "weightInKG": 80})
    responses = [account] * (2 if tool == "add_exercise" else 1)
    client, state = make_cold_client(
        tmp_path, [*responses, {"result": "SUCCESS", "id": 7}]
    )
    monkeypatch.setattr(server, "_client", client)
    forms = capture_forms(client)
    clock_hour = hour if tool == "add_exercise" else 0
    monkeypatch.setattr(
        client, "now", lambda: datetime(2026, 11, 13, clock_hour, 8, tzinfo=UTC)
    )
    at = {} if tool == "add_exercise" else {"time": f"{hour:02}:30"}

    result = json.loads(getattr(server, tool)(**kwargs, **at))

    assert result["status"] == "success"
    assert "diary_group" not in result
    if tool == "add_note":
        assert int(forms[0]["order"]) == (groups[meal] << 16) | 1
        assert forms[0]["time"] == f"{hour:02}:30:00"
    else:
        entry = "exercise" if tool == "add_exercise" else "serving"
        assert state["payloads"][-1][entry]["order"] == (groups[meal] << 16) | 1


@pytest.mark.parametrize("tool,kwargs", WRITE_TOOLS)
@pytest.mark.parametrize(
    "overrides",
    [
        {"DG04": "Evening snack"},
        {"DG04ON": "false"},
        {"DG05": " BREAK FAST "},
        {
            "DG01": "Petit déjeuner",
            "DG02": "Déjeuner",
            "DG03": "Dîner",
            "DG04": "Collations",
        },
        {f"DG{i:02}ON": "false" for i in range(1, 9)},
    ],
    ids=["missing", "disabled", "duplicate", "localized", "all-disabled"],
)
def test_auto_requires_all_four_unique_enabled_meals_before_writing(
    tmp_path, monkeypatch, tool, kwargs, overrides
):
    from cronometer_api_mcp import server

    client, state = make_cold_client(
        tmp_path, [profile(**{**BREAKFAST_FIRST, **overrides})]
    )
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(client, "now", lambda: datetime(2026, 11, 13, 8, tzinfo=UTC))
    forms = capture_forms(client)

    result = json.loads(getattr(server, tool)(**kwargs))

    assert result["status"] == "error"
    assert "Cannot resolve diary_group 'auto'" in result["message"]
    assert "Choose an explicit diary_group" in result["message"]
    for group in client._diary_groups:
        if group["enabled"]:
            assert group["name"] in result["message"]
    assert state["post"] == 1
    assert forms == []


def test_auto_reuses_the_group_cache(tmp_path):
    client, state = make_cold_client(tmp_path, [profile(**BREAKFAST_FIRST)])

    client.resolve_diary_group("Breakfast")
    assert client._auto_diary_group_for_hour(8) == 0
    assert client._auto_diary_group_for_hour(12) == 1
    assert state["post"] == 1


@pytest.mark.parametrize(
    "overrides", [{"DG04": "Other"}, {"DG04ON": "false"}, {"DG05": "Lunch"}]
)
def test_auto_refreshes_an_unusable_cached_layout_once(tmp_path, overrides):
    client, state = make_cold_client(
        tmp_path,
        [profile(**{**BREAKFAST_FIRST, **overrides}), profile(**BREAKFAST_FIRST)],
    )
    client.get_diary_groups()

    assert client._auto_diary_group_for_hour(8) == 0
    assert state["post"] == 2


def test_auto_stops_after_one_refresh_of_an_unusable_layout(tmp_path):
    client, state = make_cold_client(
        tmp_path, [profile(**{**BREAKFAST_FIRST, "DG04": "Other"})]
    )
    client.get_diary_groups()

    with pytest.raises(ValueError, match="Choose an explicit diary_group"):
        client._auto_diary_group_for_hour(8)
    assert state["post"] == 2


@pytest.mark.parametrize("tool,kwargs", WRITE_TOOLS)
def test_auto_profile_failure_does_not_write(tmp_path, monkeypatch, tool, kwargs):
    from cronometer_api_mcp import server

    client, state = make_cold_client(tmp_path, [{}])
    monkeypatch.setattr(server, "_client", client)
    forms = capture_forms(client)

    def unavailable():
        raise RuntimeError("profile unavailable")

    monkeypatch.setattr(client, "_get_profile_prefs", unavailable)

    result = json.loads(getattr(server, tool)(**kwargs))

    assert result["status"] == "error"
    assert "profile unavailable" in result["message"]
    assert state["post"] == 0
    assert forms == []


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

    def add_note(self, text, day, group, at=None):
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


# ------------------------------------------------------------------
# update_food_entry resolves the target group from the account layout
# ------------------------------------------------------------------


def test_update_food_entry_moves_to_a_named_custom_group(tmp_path, monkeypatch):
    from test_diary_edit import EDITED, diary, serving

    from cronometer_api_mcp import server

    client, state = make_cold_client(
        tmp_path, [profile(**CUSTOM_GROUPS), diary(serving()), EDITED]
    )
    monkeypatch.setattr(server, "_client", client)

    result = json.loads(
        server.update_food_entry(
            entry_id=str(serving()["servingId"]),
            date="1900-01-02",
            diary_group="Pre-bedtime",
        )
    )

    assert result["status"] == "success"
    assert result["diary_group"] == {"group": 5, "name": "Pre-bedtime"}
    assert state["payloads"][-1]["serving"]["order"] >> 16 == 5


def test_update_food_entry_refuses_auto_without_editing(tmp_path, monkeypatch):
    from cronometer_api_mcp import server

    client, state = make_cold_client(tmp_path, [])
    monkeypatch.setattr(server, "_client", client)

    result = json.loads(server.update_food_entry(entry_id="1", diary_group="auto"))

    assert result["status"] == "error"
    assert '"auto" cannot move an entry' in result["message"]
    assert state["payloads"] == []
