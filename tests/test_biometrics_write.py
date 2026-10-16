"""add_biometric: record a reading through /api/v2/add_biometric."""

from __future__ import annotations

import math
from datetime import date

import pytest
from test_client import make_cold_client

from cronometer_api_mcp.client import CronometerError

ADDED = {"heightInCM": 180, "weightInKG": 80.0, "messages": [], "id": 1894305060}


def test_add_biometric_sends_new_reading(tmp_path):
    client, state = make_cold_client(tmp_path, [ADDED])

    result = client.add_biometric(1, 2, 165.5, date(1900, 1, 2))

    sent = state["payloads"][0]
    assert sent["config"] == {"call_version": 1}
    assert sent["biometric"] == {
        "biometricId": 0,
        "type": "Biometric",
        "metricId": 1,
        "unitId": 2,
        "amount": 165.5,
        "day": "1900-1-2",
        "order": 1,
        "meta": None,
        "externalId": None,
        "source": None,
        "userId": 42,
    }
    assert result["biometricId"] == 1894305060
    assert result["day"] == "1900-01-02"


@pytest.mark.parametrize("bad", [-1.0, math.nan, math.inf])
def test_add_biometric_rejects_bad_amounts(tmp_path, bad):
    client, state = make_cold_client(tmp_path, [ADDED])

    with pytest.raises(ValueError, match="non-negative"):
        client.add_biometric(1, 1, bad)
    assert state["payloads"] == []


def test_add_biometric_raises_without_id(tmp_path):
    client, _ = make_cold_client(tmp_path, [{"messages": []}])

    with pytest.raises(CronometerError, match="Failed to add biometric"):
        client.add_biometric(1, 1, 70.0)
