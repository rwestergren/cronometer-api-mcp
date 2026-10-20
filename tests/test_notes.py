"""add_note / update_note.

The app writes diary notes through /photoupload as a multipart form, with
the session token in a `sessionKey` field instead of the JSON auth block.
"""

from __future__ import annotations

from datetime import date

import pytest
from test_client import FakeResp, make_cold_client

from cronometer_api_mcp.client import CronometerError

DAY = date(1900, 1, 2)
NOTE_ID = 42080649


def capture_forms(client, body: dict | None = None) -> list[dict]:
    """Route POST /photoupload to a fake that records the parsed form."""
    forms: list[dict] = []
    real_post = client._http.post

    def fake_post(endpoint, json=None, content=None, headers=None):
        if endpoint != "/photoupload":
            return real_post(endpoint, json=json)
        boundary = headers["content-type"].split("boundary=")[1].encode()
        fields = {}
        for part in content.split(b"--" + boundary):
            if b'name="' not in part:
                continue
            head, _, value = part.partition(b"\r\n\r\n")
            name = head.split(b'name="')[1].split(b'"')[0].decode()
            fields[name] = value.rstrip(b"\r\n").decode()
        forms.append(fields)
        stored = body or {
            "noteId": int(fields["noteId"]) or NOTE_ID,
            "text": fields["noteText"],
            "day": fields["day"],
            "order": int(fields["order"]),
            "meta": {},
        }
        return FakeResp(stored)

    client._http.post = fake_post  # type: ignore[method-assign]
    return forms


def test_add_note_posts_multipart_form_with_session_key(tmp_path):
    client, _ = make_cold_client(tmp_path, [{}])
    forms = capture_forms(client)

    note = client.add_note("ate late", DAY, diary_group=3)

    assert forms == [
        {
            "action": "diaryImage",
            "sessionKey": "FRESH_TOKEN_1",
            "noteId": "0",
            "day": "1900-1-2",
            "order": str((3 << 16) | 1),
            "deleteExistingPhoto": "true",
            "noteText": "ate late",
        }
    ]
    assert note["noteId"] == NOTE_ID


def test_update_note_keeps_day_and_order(tmp_path):
    stored = {
        "type": "Note",
        "noteId": NOTE_ID,
        "text": "old",
        "day": "1900-01-02",
        "order": (2 << 16) | 4,
        "meta": {},
    }
    client, _ = make_cold_client(tmp_path, [{"diary": [stored]}])
    forms = capture_forms(client)

    client.update_note(NOTE_ID, "new", DAY)

    assert forms[0]["noteId"] == str(NOTE_ID)
    assert forms[0]["noteText"] == "new"
    assert forms[0]["day"] == "1900-01-02"
    assert forms[0]["order"] == str((2 << 16) | 4)


def test_update_note_unknown_id_raises(tmp_path):
    client, _ = make_cold_client(tmp_path, [{"diary": []}])
    forms = capture_forms(client)

    with pytest.raises(CronometerError, match="No note 1"):
        client.update_note(1, "x", DAY)
    assert forms == []


def test_save_note_raises_on_unexpected_response(tmp_path):
    client, _ = make_cold_client(tmp_path, [{}])
    capture_forms(client, body={"result": "FAIL"})

    with pytest.raises(CronometerError, match="Failed to save note"):
        client.add_note("x", DAY, diary_group=1)


def test_add_note_rejects_empty_text(tmp_path):
    client, _ = make_cold_client(tmp_path, [{}])
    forms = capture_forms(client)

    with pytest.raises(ValueError, match="empty"):
        client.add_note("  ", DAY, diary_group=1)
    assert forms == []
