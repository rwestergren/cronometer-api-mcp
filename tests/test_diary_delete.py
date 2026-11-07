"""delete_entries for every diary entry type.

Foods, exercises, notes and biometrics all go to the same v3 DELETE
/diary-entries, but notes and biometrics must be keyed by `id` there and
`meta` must be a JSON string.
"""

from __future__ import annotations

from datetime import date

from test_client import make_cold_client

DAY = date(1900, 1, 2)
SERVING_ID = 5312202908


def serving() -> dict:
    """A get_diary food entry shaped like the real one."""
    return {
        "servingId": SERVING_ID,
        "type": "Serving",
        "foodId": 5442,
        "measureId": 14990,
        "translationId": 0,
        "grams": 100,
        "day": "1900-01-02",
        "time": "11:23:50",
        "offset": 0,
        "userId": 42,
        "order": (1 << 16) | 1,
    }


def diary(*entries) -> dict:
    return {"diary": list(entries), "summary": {}, "messages": []}


def _capture_v3_delete(client) -> list[dict]:
    sent: list[dict] = []

    class NoContent:
        status_code = 204
        text = ""

    def fake_request(method, url, json=None, headers=None):
        sent.append({"method": method, "url": url, "json": json})
        return NoContent()

    client._http.request = fake_request  # type: ignore[method-assign]
    return sent


EXERCISE = {
    "type": "Exercise",
    "exerciseId": 881413372,
    "activityId": 1342,
    "activitySpecId": 0,
    "minutes": 30,
    "weight": 176.4,
    "calories": -123,
    "calorieOverride": True,
    "meta": {},
    "userId": 42,
    "day": "1900-01-02",
    "order": 65538,
}
NOTE = {
    "type": "Note",
    "noteId": 42080649,
    "text": "a note",
    "meta": {},
    "userId": 42,
    "day": "1900-01-02",
    "order": 65537,
}
BIOMETRIC = {
    "type": "Biometric",
    "biometricId": 1894305060,
    "metricId": 1,
    "unitId": 1,
    "amount": 99.9,
    "samplesVersion": 0,
    "meta": {},
    "userId": 42,
    "day": "1900-01-02",
    "order": 1,
}


def test_delete_entries_matches_every_entry_type_by_its_id(tmp_path):
    client, _ = make_cold_client(
        tmp_path, [diary(serving(), EXERCISE, NOTE, BIOMETRIC)]
    )
    sent = _capture_v3_delete(client)

    result = client.delete_entries(
        [str(SERVING_ID), "881413372", "42080649", "1894305060"], DAY
    )

    assert result["count"] == 4
    assert sent[0]["method"] == "DELETE"
    assert sent[0]["url"].endswith("/diary-entries")
    serving_out, exercise_out, note_out, bio_out = sent[0]["json"]["diaryEntries"]
    assert serving_out == serving()
    assert exercise_out["exerciseId"] == 881413372
    # v3 wants notes and biometrics keyed by `id`, and meta as a JSON string.
    assert note_out["id"] == 42080649 and "noteId" not in note_out
    assert bio_out["id"] == 1894305060 and "biometricId" not in bio_out
    assert {e["meta"] for e in (exercise_out, note_out, bio_out)} == {"{}"}


def test_delete_entries_leaves_unrequested_entries_alone(tmp_path):
    client, _ = make_cold_client(tmp_path, [diary(serving(), EXERCISE, NOTE)])
    sent = _capture_v3_delete(client)

    result = client.delete_entries(["42080649"], DAY)

    assert result == {"removed": ["42080649"], "count": 1}
    assert [e["type"] for e in sent[0]["json"]["diaryEntries"]] == ["Note"]
