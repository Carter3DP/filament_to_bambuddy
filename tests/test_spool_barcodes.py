"""Reusable-spool barcode mapping and assignment route tests."""

import json
import re
from unittest.mock import Mock, patch

import pytest

import app as app_module


def response(status=200, payload=None, text=""):
    result = Mock()
    result.status_code = status
    result.ok = 200 <= status < 300
    result.json.return_value = payload
    result.text = text
    return result


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "BAMBUDDY_API_KEY", "test-key")
    monkeypatch.setattr(app_module, "SPOOL_BARCODE_FILE", tmp_path / "spool_barcodes.json")
    monkeypatch.setattr(app_module, "PRINTER_BARCODE_FILE", tmp_path / "printer_barcodes.json")
    app_module.app.testing = True
    return app_module.app.test_client()


def spool(spool_id, *, label_weight=1000, weight_used=100, material="PLA"):
    return {
        "id": spool_id,
        "material": material,
        "brand": "Test",
        "color_name": "Blue",
        "label_weight": label_weight,
        "weight_used": weight_used,
        "archived_at": None,
    }


def test_sets_unique_barcode_and_replaces_previous_code_for_same_spool(client):
    with patch("app.requests.get", return_value=response(payload=spool(12))):
        first = client.put("/api/spool-barcodes", json={"barcode": "OLD", "spool_id": 12})
        second = client.put("/api/spool-barcodes", json={"barcode": "NEW", "spool_id": 12})

    assert first.status_code == 200
    assert second.get_json()["replaced"] == ["OLD"]
    assert json.loads(app_module.SPOOL_BARCODE_FILE.read_text()) == {"NEW": 12}


def test_rejects_barcode_already_owned_by_another_spool(client):
    app_module.save_spool_barcodes({"REUSE-1": 3})
    with patch("app.requests.get", return_value=response(payload=spool(4))):
        result = client.put("/api/spool-barcodes", json={"barcode": "REUSE-1", "spool_id": 4})

    assert result.status_code == 409
    assert "spool #3" in result.get_json()["error"]


def test_mapping_list_includes_remaining_filament_weight(client):
    with patch(
        "app.requests.get",
        return_value=response(payload=[spool(12, label_weight=1000, weight_used=275)]),
    ):
        result = client.get("/api/spool-barcodes")

    assert result.status_code == 200
    assert result.get_json()["spools"][0]["remaining_weight"] == 725


def test_current_assignments_are_normalized_sorted_and_include_remaining_weight(client):
    assignments = [
        {
            "id": 3, "spool_id": 12, "printer_id": 8, "printer_name": "Zulu",
            "ams_id": 255, "tray_id": 0, "spool": spool(12, weight_used=250),
        },
        {
            "id": 2, "spool_id": 11, "printer_id": 7, "printer_name": "Alpha",
            "ams_id": 0, "tray_id": 1, "ams_label": "Dry Box",
            "configured": True, "spool": spool(11, weight_used=400),
        },
    ]
    with patch("app.requests.get", return_value=response(payload=assignments)) as get:
        result = client.get("/api/assignments")

    assert result.status_code == 200
    data = result.get_json()["assignments"]
    assert [item["printer_name"] for item in data] == ["Alpha", "Zulu"]
    assert data[0]["slot_label"] == "Dry Box · Slot 2"
    assert data[0]["spool"]["remaining_weight"] == 600
    assert data[1]["slot_label"] == "External spool"
    assert get.call_args.args[0].endswith("/api/v1/inventory/assignments")


def test_current_assignments_propagate_bambuddy_errors(client):
    with patch("app.requests.get", return_value=response(status=503, payload={"detail": "Unavailable"})):
        result = client.get("/api/assignments")

    assert result.status_code == 503
    assert result.get_json()["error"] == "Unavailable"


def test_only_reusable_spool_scanners_enable_code_39(client):
    html = client.get("/").get_data(as_text=True)

    new_spool_formats = re.search(r"\breader\.possibleFormats\s*=\s*\[([^]]+)]", html)
    reusable_formats = re.search(r"accessoryReader\.possibleFormats\s*=\s*\[([^]]+)]", html)

    assert new_spool_formats
    assert reusable_formats
    assert "F.CODE_39" not in new_spool_formats.group(1)
    assert "F.CODE_39" in reusable_formats.group(1)


def test_saves_and_resolves_printer_only_barcode(client):
    printers = [{"id": 7, "name": "Workshop X1C"}]
    with patch("app.requests.get", return_value=response(payload=printers)):
        saved = client.put("/api/printer-barcodes", json={
            "barcode": "PRINTER-7", "printer_id": 7,
        })

    resolved = client.get("/api/printer-barcodes/resolve?barcode=PRINTER-7")

    assert saved.status_code == 200
    assert resolved.get_json()["target"] == {"printer_id": 7}


def test_saves_fixed_printer_slot_and_defaults_assignment_to_it(client):
    printers = [{"id": 7, "name": "Workshop X1C"}]
    with patch("app.requests.get", return_value=response(payload=printers)):
        saved = client.put("/api/printer-barcodes", json={
            "barcode": "X1C-EXT", "printer_id": 7,
            "ams_id": 255, "tray_id": 0, "slot_label": "External spool",
        })
    assert saved.status_code == 200

    assignments = response(payload=[])
    assigned = response(payload={
        "id": 90, "spool_id": 22, "printer_id": 7, "ams_id": 255, "tray_id": 0,
    })
    with (
        patch("app.requests.get", return_value=assignments),
        patch("app.requests.post", return_value=assigned) as post,
    ):
        result = client.post("/api/spool-assignment", json={
            "mode": "printer", "barcode": "X1C-EXT", "spool_id": 22,
            # A configured slot is authoritative over stale client fields.
            "ams_id": 0, "tray_id": 3,
        })

    assert result.status_code == 200
    assert post.call_args.kwargs["json"] == {
        "spool_id": 22, "printer_id": 7, "ams_id": 255, "tray_id": 0,
    }


def test_printer_only_barcode_uses_slot_selected_during_assignment(client):
    app_module.save_printer_barcodes({"PRINTER-7": {"printer_id": 7}})
    with (
        patch("app.requests.get", return_value=response(payload=[])),
        patch("app.requests.post", return_value=response(payload={"id": 91})) as post,
    ):
        result = client.post("/api/spool-assignment", json={
            "mode": "printer", "barcode": "PRINTER-7", "spool_id": 22,
            "ams_id": 0, "tray_id": 2,
        })

    assert result.status_code == 200
    assert post.call_args.kwargs["json"] == {
        "spool_id": 22, "printer_id": 7, "ams_id": 0, "tray_id": 2,
    }


def test_already_assigned_spool_ids_are_compared_as_integers(client):
    app_module.save_spool_barcodes({"TARGET": 22})
    existing = {
        "spool_id": "22", "ams_id": "0", "tray_id": "2",
        "spool": spool(22, weight_used=250),
    }
    with (
        patch("app.requests.get", return_value=response(payload=[existing])),
        patch("app.requests.post") as post,
    ):
        result = client.post("/api/spool-assignment", json={
            "barcode": "TARGET", "printer_id": 7, "ams_id": 0, "tray_id": 2,
        })

    assert result.status_code == 200
    assert result.get_json()["already_assigned"] is True
    post.assert_not_called()


def test_success_refresh_keeps_replacement_prompt_closed(client):
    html = client.get("/assign-spool").get_data(as_text=True)

    assert (
        "await Promise.all([loadSlots(),loadMappings()]);\n"
        "    }\n"
        "    // loadSlots() sees the assignment that just succeeded as an occupied\n"
        in html
    )
    assert "$('existingChoice').classList.add('hidden');\n  }catch(e){" in html


def test_printer_barcode_mode_keeps_nonempty_replacement_confirmation(client):
    app_module.save_printer_barcodes({
        "X1C-EXT": {"printer_id": 7, "ams_id": 255, "tray_id": 0},
    })
    existing = {
        "spool_id": 11, "ams_id": 255, "tray_id": 0,
        "spool": spool(11, weight_used=250),
    }
    with (
        patch("app.requests.get", return_value=response(payload=[existing])),
        patch("app.requests.post") as post,
    ):
        result = client.post("/api/spool-assignment", json={
            "mode": "printer", "barcode": "X1C-EXT", "spool_id": 22,
        })

    assert result.status_code == 409
    assert result.get_json()["confirmation_required"] is True
    post.assert_not_called()


def test_printer_slots_include_ams_and_external_assignments(client):
    status = {
        "connected": True,
        "ams": [{"id": 0, "tray": [{"id": 0}, {"id": 1}]}],
        "vt_tray": [{"id": 254}],
    }
    assignments = [{
        "spool_id": 11, "ams_id": 0, "tray_id": 1,
        "ams_label": "Dry Box", "spool": spool(11, weight_used=400),
    }]
    with patch("app.requests.get", side_effect=[response(payload=status), response(payload=assignments)]):
        result = client.get("/api/printers/7/slots")

    data = result.get_json()
    assert [(s["ams_id"], s["tray_id"]) for s in data["slots"]] == [(0, 0), (0, 1), (255, 0)]
    assert data["slots"][1]["label"] == "Dry Box · Slot 2"
    assigned = data["slots"][1]["assignment"]["spool"]
    assert assigned["remaining_weight"] == 600
    assert assigned["empty"] is False


def test_nonempty_existing_spool_requires_explicit_choice(client):
    app_module.save_spool_barcodes({"TARGET": 22})
    existing = {"spool_id": 11, "ams_id": 0, "tray_id": 2, "spool": spool(11, weight_used=250)}
    with patch("app.requests.get", return_value=response(payload=[existing])), patch("app.requests.post") as post:
        result = client.post("/api/spool-assignment", json={
            "barcode": "TARGET", "printer_id": 7, "ams_id": 0, "tray_id": 2,
        })

    assert result.status_code == 409
    assert result.get_json()["confirmation_required"] is True
    post.assert_not_called()


def test_keep_choice_assigns_without_deleting_old_spool(client):
    app_module.save_spool_barcodes({"TARGET": 22, "OLD": 11})
    existing = {"spool_id": 11, "ams_id": 0, "tray_id": 2, "spool": spool(11, weight_used=250)}
    assigned = {"id": 90, "spool_id": 22, "printer_id": 7, "ams_id": 0, "tray_id": 2}
    with (
        patch("app.requests.get", return_value=response(payload=[existing])),
        patch("app.requests.post", return_value=response(payload=assigned)) as post,
        patch("app.requests.delete") as delete,
    ):
        result = client.post("/api/spool-assignment", json={
            "barcode": "TARGET", "printer_id": 7, "ams_id": 0, "tray_id": 2,
            "delete_existing": False,
        })

    assert result.status_code == 200
    assert post.call_args.kwargs["json"] == {"spool_id": 22, "printer_id": 7, "ams_id": 0, "tray_id": 2}
    delete.assert_not_called()
    assert app_module.load_spool_barcodes() == {"TARGET": 22, "OLD": 11}


def test_empty_existing_spool_is_deleted_and_its_mapping_reset(client):
    app_module.save_spool_barcodes({"TARGET": 22, "EMPTY-SPOOL": 11})
    existing = {"spool_id": 11, "ams_id": 255, "tray_id": 0,
                "spool": spool(11, label_weight=1000, weight_used=1000)}
    assigned = {"id": 91, "spool_id": 22, "printer_id": 7, "ams_id": 255, "tray_id": 0}
    with (
        patch("app.requests.get", return_value=response(payload=[existing])),
        patch("app.requests.post", return_value=response(payload=assigned)),
        patch("app.requests.delete", return_value=response(payload={"status": "deleted"})) as delete,
    ):
        result = client.post("/api/spool-assignment", json={
            "barcode": "TARGET", "printer_id": 7, "ams_id": 255, "tray_id": 0,
        })

    assert result.get_json()["deleted_existing"] is True
    assert delete.call_args.args[0].endswith("/api/v1/inventory/spools/11")
    assert app_module.load_spool_barcodes() == {"TARGET": 22}
