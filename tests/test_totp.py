"""Tests for optional TOTP (two-factor) support at login.

Accounts with two-factor authentication enabled get `{"result": "FAIL",
"error": "TOTP_CODE_REQUIRED"}` from /api/v2/login unless the request carries
the current 6-digit code in `userCode`. When CRONOMETER_TOTP_SECRET is set the
client derives that code itself (RFC 6238, SHA-1, 30 s period, 6 digits).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cronometer_api_mcp.client import CronometerClient, CronometerError, _totp_code

# RFC 6238 Appendix B test vectors (SHA-1). The secret is the ASCII string
# "12345678901234567890"; the 6-digit codes are the last six digits of the
# 8-digit values listed in the RFC.
RFC6238_SECRET_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
RFC6238_VECTORS = [
    (59, "287082"),
    (1111111109, "081804"),
    (1234567890, "005924"),
    (2000000000, "279037"),
]

SUCCESS_BODY = {
    "result": "SUCCESS",
    "id": 123,
    "sessionKey": "TOKEN",
    "timezone": "UTC",
}
TOTP_REQUIRED_BODY = {"result": "FAIL", "error": "TOTP_CODE_REQUIRED"}


class FakeResp:
    def __init__(self, body) -> None:
        self._body = body
        self.status_code = 200

    def raise_for_status(self) -> None:  # pragma: no cover - trivial
        pass

    def json(self):
        return self._body


def make_client(tmp_path: Path, body: dict):
    """Client whose login POST is captured and answered with `body`."""
    client = CronometerClient(session_path=tmp_path / "session.json")
    captured: dict = {}

    def fake_post(endpoint, json=None):
        captured["endpoint"] = endpoint
        captured["payload"] = json
        return FakeResp(body)

    client._http.post = fake_post  # type: ignore[method-assign]
    return client, captured


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv("CRONOMETER_USERNAME", "user@example.com")
    monkeypatch.setenv("CRONOMETER_PASSWORD", "secret")
    monkeypatch.delenv("CRONOMETER_TOTP_SECRET", raising=False)


@pytest.mark.parametrize(("at", "expected"), RFC6238_VECTORS)
def test_totp_code_matches_rfc6238_sha1_vectors(at, expected):
    assert _totp_code(RFC6238_SECRET_B32, at) == expected


def test_totp_code_accepts_lowercase_and_spaced_secret():
    """Authenticator apps display the key grouped and sometimes lowercased."""
    spaced = "gezd gnbv gy3t qojq gezd gnbv gy3t qojq"
    assert _totp_code(spaced, 59) == "287082"


def test_login_sends_null_user_code_without_secret(credentials, tmp_path):
    client, captured = make_client(tmp_path, SUCCESS_BODY)

    client.login()

    assert captured["endpoint"] == "/api/v2/login"
    assert captured["payload"]["userCode"] is None


def test_login_sends_current_totp_code_when_secret_set(
    credentials, tmp_path, monkeypatch
):
    monkeypatch.setenv("CRONOMETER_TOTP_SECRET", RFC6238_SECRET_B32)
    client, captured = make_client(tmp_path, SUCCESS_BODY)

    client.login()

    code = captured["payload"]["userCode"]
    assert isinstance(code, str) and len(code) == 6 and code.isdigit()
    assert code == _totp_code(RFC6238_SECRET_B32)


def test_login_totp_required_without_secret_points_at_env_var(credentials, tmp_path):
    client, _ = make_client(tmp_path, TOTP_REQUIRED_BODY)

    with pytest.raises(CronometerError, match="CRONOMETER_TOTP_SECRET"):
        client.login()
