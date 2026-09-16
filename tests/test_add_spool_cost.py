"""Tests for total filament cost handling in Add Filament."""

from unittest.mock import Mock, patch

import pytest

import app as app_module


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "BAMBUDDY_API_KEY", "test-key")
    app_module.app.testing = True
    return app_module.app.test_client()


def test_total_filament_cost_is_converted_to_cost_per_kg(client):
    bambuddy_response = Mock(status_code=201, text="")
    with patch("app.requests.post", return_value=bambuddy_response) as post:
        response = client.post("/api/spool", json={
            "quantity": 1,
            "fields": {
                "material": "PLA",
                "label_weight": "500",
                "filament_cost": "10.00",
                "cost_per_kg": "99.00",
            },
        })

    assert response.status_code == 200
    payload = post.call_args.kwargs["json"]
    assert payload["cost_per_kg"] == 20.0
    assert "filament_cost" not in payload


def test_negative_filament_cost_is_rejected(client):
    response = client.post("/api/spool", json={
        "fields": {
            "material": "PLA",
            "label_weight": "1000",
            "filament_cost": "-1",
        },
    })

    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_add_filament_page_contains_total_cost_input(client):
    response = client.get("/")

    assert response.status_code == 200
    assert b'id="filament_cost"' in response.data
    assert b"Filament cost" in response.data
