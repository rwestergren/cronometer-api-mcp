"""add_note / update_note.

The app writes diary notes through /photoupload as a multipart form, with
the session token in a `sessionKey` field instead of the JSON auth block.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
from test_client import FakeResp, make_cold_client

from cronometer_api_mcp.client import CronometerError

DAY = date(1900, 1, 2)
NOTE_ID = 42080649


def capture_forms(
    client,
    body: dict | None = None,
    *,
    responses: list[httpx.Response] | None = None,
) -> list[dict]:
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
        if responses:
            resp = responses.pop(0)
            resp.request = httpx.Request("POST", client._http.base_url.join(endpoint))
            return resp
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


def test_update_note_keeps_day_order_and_photo(tmp_path):
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
    assert forms[0]["deleteExistingPhoto"] == "false"


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


AUTH_FAILURES = [
    (401, {}),
    (403, {}),
    (200, {"result": "FAIL", "error": "Token Authorization failed"}),
    (200, {"result": "FAILURE", "error": "Token Authorization failed"}),
]


@pytest.mark.parametrize("status,body", AUTH_FAILURES)
def test_add_note_refreshes_expired_session_and_rebuilds_form(tmp_path, status, body):
    client, state = make_cold_client(tmp_path, [{}])
    client._token = "STALE_TOKEN"
    forms = capture_forms(client, responses=[httpx.Response(status, json=body)])

    note = client.add_note("ate late", DAY, diary_group=3)

    assert note["noteId"] == NOTE_ID
    assert state["login"] == 1
    assert len(forms) == 2
    assert forms[0]["sessionKey"] == "STALE_TOKEN"
    assert forms[1] == {**forms[0], "sessionKey": "FRESH_TOKEN_1"}


@pytest.mark.parametrize("status,body", AUTH_FAILURES)
def test_save_note_stops_after_one_auth_retry(tmp_path, status, body):
    client, state = make_cold_client(tmp_path, [{}])
    client._token = "STALE_TOKEN"
    forms = capture_forms(
        client, responses=[httpx.Response(status, json=body) for _ in range(2)]
    )

    error = httpx.HTTPStatusError if status != 200 else CronometerError
    with pytest.raises(error):
        client.add_note("ate late", DAY, diary_group=3)

    assert state["login"] == 1
    assert len(forms) == 2


def test_update_note_preserves_photo_and_position_after_auth_retry(tmp_path):
    stored = {
        "type": "Note",
        "noteId": NOTE_ID,
        "text": "old",
        "day": "1900-01-02",
        "order": (2 << 16) | 4,
    }
    client, state = make_cold_client(tmp_path, [{"diary": [stored]}])
    client._token = "STALE_TOKEN"
    forms = capture_forms(client, responses=[httpx.Response(401)])

    note = client.update_note(NOTE_ID, "new", DAY)

    assert note["noteId"] == NOTE_ID
    assert state["login"] == 1
    assert len(forms) == 2
    assert forms[1] == {**forms[0], "sessionKey": "FRESH_TOKEN_1"}
    assert forms[1]["deleteExistingPhoto"] == "false"
    assert forms[1]["day"] == stored["day"]
    assert forms[1]["order"] == str(stored["order"])


@pytest.mark.parametrize("status", [200, 500])
def test_save_note_does_not_retry_non_auth_errors(tmp_path, status):
    client, state = make_cold_client(tmp_path, [{}])
    client._token = "TOKEN"
    forms = capture_forms(
        client, responses=[httpx.Response(status, text="Upload failed")]
    )

    error = httpx.HTTPStatusError if status != 200 else CronometerError
    with pytest.raises(error):
        client.add_note("ate late", DAY, diary_group=3)

    assert state["login"] == 0
    assert len(forms) == 1
