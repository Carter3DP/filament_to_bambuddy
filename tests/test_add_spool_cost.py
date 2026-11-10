"""Tests for total filament cost handling in Add Filament."""

from unittest.mock import Mock, patch

import pytest

import app as app_module


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "BAMBUDDY_API_KEY", "test-key")
    monkeypatch.setattr(app_module, "CACHE_FILE", tmp_path / "barcode_cache.json")
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


def test_successful_add_remembers_every_form_field(client):
    fields = {
        "material": "PETG",
        "brand": "Test Brand",
        "subtype": "Matte",
        "color_name": "Deep Blue",
        "rgba": "1234ABFF",
        "label_weight": "750",
        "storage_location": "Shelf A",
        "category": "Engineering",
        "nozzle_temp_min": "230",
        "nozzle_temp_max": "250",
        "filament_cost": "17.25",
        "cost_per_kg": "23.00",
        "note": "Dry before use",
        "paste_title": "Test Brand PETG Deep Blue 750g",
    }
    bambuddy_response = Mock(status_code=201, text="")
    with (
        patch("app.requests.post", return_value=bambuddy_response),
        patch("app._external_all_codes", return_value=None),
    ):
        response = client.post("/api/spool", json={
            "barcode": "TEST-SKU-123",
            "quantity": 3,
            "fields": fields,
        })

    assert response.status_code == 200
    remembered = app_module.load_cache()["TEST-SKU-123"]
    assert remembered == {**fields, "quantity": 3}

    lookup = client.get("/api/lookup?barcode=TEST-SKU-123")
    assert lookup.status_code == 200
    assert lookup.get_json()["source"] == "cache"
    assert lookup.get_json()["fields"] == remembered


def test_add_filament_page_restores_color_and_supports_overwrite_toggle(client):
    body = client.get("/").get_data(as_text=True)

    assert 'id="overwritePresets"' in body
    assert "const overwrite=$('overwritePresets').checked" in body
    assert "if(f.rgba!=null && (overwrite||!colorFieldSet))" in body
    assert "put('quantity',f.quantity)" in body
    assert "put('paste_title',f.paste_title)" in body
