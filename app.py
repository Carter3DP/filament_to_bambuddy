#!/usr/bin/env python3
"""
filament_to_bambuddy — app.py

A small mobile web app: scan a third-party filament box barcode with your phone,
look up the product (Open Filament Database + a learning per-barcode cache),
review the auto-filled details, and add the spool to your Bambuddy inventory.

Run:
    pip install flask requests
    export BAMBUDDY_URL="https://your-bambuddy"
    export BAMBUDDY_API_KEY="..."
    python app.py        # then open the printed URL on your phone

Camera scanning needs a SECURE origin (https:// or localhost) — see the README.

Copyright (C) 2026 Victor Manuel (hibikipr)
SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sys

if sys.version_info < (3, 10):  # noqa: UP036 - guards direct `python app.py` runs on an old system Python, independent of the Docker image's pinned version
    sys.exit(f"❌ Python 3.10+ required (this is {sys.version.split()[0]}).")

import json
import logging
import os
import subprocess
import threading
from pathlib import Path

import requests
from flask import Flask, jsonify, make_response, render_template, request

import i18n

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config (env vars) ─────────────────────────────────────────────────────────

BAMBUDDY_URL = os.getenv("BAMBUDDY_URL", "http://localhost:8000").rstrip("/")
BAMBUDDY_API_KEY = os.getenv("BAMBUDDY_API_KEY", "")

DEFAULT_LABEL_WEIGHT = int(os.getenv("DEFAULT_LABEL_WEIGHT", "1000"))
CACHE_FILE = Path(os.getenv("BARCODE_CACHE_FILE", "barcode_cache.json"))
SPOOL_BARCODE_FILE = Path(os.getenv("SPOOL_BARCODE_FILE", "spool_barcodes.json"))

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8088"))

# Fields Bambuddy's POST /inventory/spools accepts (SpoolCreate). We whitelist
# so the form can't send anything the API rejects.
ALLOWED_SPOOL_FIELDS = {
    "material", "subtype", "color_name", "rgba", "extra_colors", "effect_type",
    "brand", "label_weight", "core_weight", "nozzle_temp_min", "nozzle_temp_max",
    "note", "cost_per_kg", "category", "storage_location", "data_origin",
}

# GTIN-8/12/13/14 are the only standard checksummed lengths. The floor below
# the max (rather than an exact 8/12/13/14 match) accounts for leading zeros
# already stripped from the raw input — see _classify_code. Mirrors Bambuddy's
# `classify_code`/`_gtin_checksum_valid` in `backend/app/schemas/spool.py`.
_MIN_GTIN_LENGTH = 7
_MAX_GTIN_LENGTH = 14


def _gtin_checksum_valid(digits: str) -> bool:
    payload, check = digits[:-1], int(digits[-1])
    total = 0
    for i, ch in enumerate(reversed(payload)):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return (10 - (total % 10)) % 10 == check


def _classify_code(raw: str) -> tuple[str, str]:
    """Canonicalize `raw` and classify it as ("gtin", digits) or ("sku", stripped-upper).

    Classifies the canonicalized (zero-stripped) form rather than the raw
    input, then re-validates the checksum after re-padding to a full
    GTIN-14 — the checksum is invariant to leading-zero padding (weights are
    assigned right-to-left from the check digit, so a leading zero always
    lands in a weight-agnostic position and contributes 0 to the sum no
    matter how many precede it). Without this, a barcode already missing its
    leading zero(s) (e.g. typed from a receipt showing the short form, or a
    UPC-A with fewer digits than the exact 8/12/13/14-length gate this used
    to require) fails the length check and gets classified as a SKU instead
    of a GTIN — sending the external lookup to the wrong index.
    """
    import ofd

    canonical = ofd._canon(raw)
    if _MIN_GTIN_LENGTH <= len(canonical) <= _MAX_GTIN_LENGTH and _gtin_checksum_valid(
        canonical.zfill(_MAX_GTIN_LENGTH)
    ):
        return canonical, "gtin"
    return (raw or "").strip().upper(), "sku"


def _external_all_codes(code: str, kind: str) -> tuple | None:
    """Cross-reference OFD and SpoolmanDB-Community for `code`, merging both hits.

    Returns (fields, source, all_codes, title) where `source` is whichever
    database resolved first, `fields` prefers that source's values but fills
    any gaps (e.g. missing nozzle temps) from the other, and `all_codes` is
    the union of every sibling code (other package-size GTINs, the refill
    GTIN, the SKU/article number) discovered across both databases. If only
    one database resolves `code` directly, its sibling codes are also probed
    against the *other* database to recover cross-referenced fields/codes.
    Mirrors Bambuddy's `_external_all_codes` in `backend/app/api/routes/inventory.py`.
    """
    import ofd
    import spoolmandb_community

    def _ofd_lookup(c, k):
        return ofd.lookup(c) if k == "gtin" else ofd.lookup_article(c)

    def _smdb_lookup(c, k):
        return spoolmandb_community.lookup(c) if k == "gtin" else spoolmandb_community.lookup_sku(c)

    try:
        ofd_hit = _ofd_lookup(code, kind)
    except Exception:
        ofd_hit = None
    try:
        smdb_hit = _smdb_lookup(code, kind)
    except Exception:
        smdb_hit = None

    if not ofd_hit and not smdb_hit:
        return None

    fields: dict = {}
    all_codes: list = []
    source = None
    title = None

    def _merge(hit, src_name):
        nonlocal source, title
        hit_fields, hit_codes = hit
        for key, value in hit_fields.items():
            if key == "_title":
                continue
            if value is not None and fields.get(key) is None:
                fields[key] = value
        if title is None and hit_fields.get("_title"):
            title = hit_fields["_title"]
        for entry in hit_codes:
            if not any(existing["code"] == entry["code"] for existing in all_codes):
                all_codes.append(entry)
        if source is None:
            source = src_name

    if ofd_hit:
        _merge(ofd_hit, "ofd")
    if smdb_hit:
        _merge(smdb_hit, "spoolmandb-community")

    tried = {code}
    # list(...) is required, not redundant: _merge() below appends newly
    # cross-referenced codes to all_codes, so iterating a live view of it
    # here would pick up entries added mid-loop instead of just the
    # original siblings.
    for entry in list(all_codes):  # noqa: PERF101
        if ofd_hit and smdb_hit:
            break
        sibling_code = entry["code"]
        if sibling_code in tried:
            continue
        tried.add(sibling_code)
        if not ofd_hit:
            try:
                probe = _ofd_lookup(sibling_code, entry["kind"])
            except Exception:
                probe = None
            if probe:
                _merge(probe, "ofd")
                ofd_hit = probe
        if not smdb_hit:
            try:
                probe = _smdb_lookup(sibling_code, entry["kind"])
            except Exception:
                probe = None
            if probe:
                _merge(probe, "spoolmandb-community")
                smdb_hit = probe

    return fields, source, all_codes, title


def _get_version() -> str:
    """Resolve the running app's version for display in the GUI footer.

    Priority: APP_VERSION env var (baked in at Docker build time from the
    release git tag, see Dockerfile + docker-publish.yml) > local `git
    describe` (useful when running straight from a git checkout) > "dev".
    """
    env_version = os.getenv("APP_VERSION")
    if env_version:
        return env_version
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=3,
            check=False,  # returncode is checked explicitly below, not raised on
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        # Expected/benign when not running from a git checkout (e.g. inside
        # the Docker image, which doesn't copy .git) - debug, not a warning.
        log.debug("git describe unavailable", exc_info=True)
    return "dev"


APP_VERSION = _get_version()

app = Flask(__name__)

_spool_barcode_lock = threading.Lock()


# ── Barcode cache (learns from your confirmed entries) ────────────────────────

def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


# ── Reusable-spool barcode map ───────────────────────────────────────────────

def load_spool_barcodes() -> dict[str, int]:
    """Load the local reusable-barcode -> Bambuddy spool-id map."""
    if not SPOOL_BARCODE_FILE.exists():
        return {}
    try:
        data = json.loads(SPOOL_BARCODE_FILE.read_text())
        if not isinstance(data, dict):
            return {}
        return {
            str(barcode): int(spool_id)
            for barcode, spool_id in data.items()
            if str(barcode).strip()
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        log.warning("could not read reusable-spool barcode map", exc_info=True)
        return {}


def save_spool_barcodes(mapping: dict[str, int]):
    """Atomically persist the reusable-spool barcode map."""
    SPOOL_BARCODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SPOOL_BARCODE_FILE.with_name(f".{SPOOL_BARCODE_FILE.name}.tmp")
    tmp.write_text(json.dumps(mapping, indent=2, sort_keys=True) + "\n")
    tmp.replace(SPOOL_BARCODE_FILE)


def _bambuddy_headers(*, json_body: bool = False) -> dict[str, str]:
    headers = {"X-API-Key": BAMBUDDY_API_KEY, "Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _bambuddy_error(response: requests.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = body.get("detail") or body.get("error")
            if detail:
                return str(detail)
    except (ValueError, TypeError):
        pass
    return response.text[:200] or f"HTTP {response.status_code}"


def _spool_summary(spool: dict | None) -> dict | None:
    if not spool:
        return None
    label_weight = float(spool.get("label_weight") or 0)
    weight_used = float(spool.get("weight_used") or 0)
    remaining = max(0.0, label_weight - weight_used)
    return {
        "id": spool.get("id"),
        "material": spool.get("material") or "",
        "brand": spool.get("brand") or "",
        "color_name": spool.get("color_name") or "",
        "label_weight": label_weight,
        "weight_used": weight_used,
        "remaining_weight": remaining,
        "empty": remaining <= 0,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

LANG_COOKIE_NAME = "lang"
LANG_COOKIE_MAX_AGE = 365 * 24 * 3600


def _resolve_request_locale() -> str:
    """?lang= query override > lang cookie > browser Accept-Language > default.

    See i18n.resolve_locale for the actual precedence/matching logic — this
    just pulls the three candidate values out of the current Flask request.
    """
    return i18n.resolve_locale(
        request.headers.get("Accept-Language"),
        request.args.get("lang"),
        request.cookies.get(LANG_COOKIE_NAME),
    )


@app.get("/")
def index():
    lang = _resolve_request_locale()
    translator = i18n.Translator(lang)
    resp = make_response(
        render_template(
            "index.html",
            bambuddy_url=BAMBUDDY_URL,
            version=APP_VERSION,
            lang=lang,
            supported_langs=i18n.SUPPORTED_LANGS,
            t=translator.t,
            translations=i18n.TRANSLATIONS.get(lang, i18n.TRANSLATIONS[i18n.DEFAULT_LANG]),
            translations_en=i18n.TRANSLATIONS[i18n.DEFAULT_LANG],
        )
    )
    # Remember an explicit ?lang= choice so it sticks on the next visit
    # (e.g. after adding the PWA to the home screen, where there's no address
    # bar to re-type ?lang= into).
    if request.args.get("lang") in i18n.TRANSLATIONS:
        resp.set_cookie(LANG_COOKIE_NAME, lang, max_age=LANG_COOKIE_MAX_AGE, samesite="Lax")
    return resp


@app.get("/sw.js")
def service_worker():
    # Served from the root so its scope covers the whole site (a SW under
    # /static would only control /static/*).
    resp = app.send_static_file("sw.js")
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/manifest.webmanifest")
def manifest():
    resp = app.send_static_file("manifest.webmanifest")
    resp.headers["Content-Type"] = "application/manifest+json"
    return resp


# Dropdown option lists (mirroring Bambuddy's spool edit form).
MATERIAL_OPTIONS = ["PLA", "PETG", "PCTG", "ABS", "ASA", "TPU", "TPE", "PC", "PA",
                    "Nylon", "PVA", "HIPS", "PP", "PET", "PLA-CF", "PETG-CF", "PA-CF"]
SUBTYPE_OPTIONS = ["Basic", "Matte", "Silk", "Silk+", "Plus", "Tough", "HF",
                   "High Speed", "CF", "GF", "Galaxy", "Glow", "Marble", "Metal",
                   "Rainbow", "Sparkle", "Wood", "Translucent", "Transparent",
                   "Clear", "Gradient", "Dual Color", "Tri Color", "Carbon Fiber"]
WEIGHT_OPTIONS = [250, 500, 750, 1000, 2000, 3000]
COLOR_OPTIONS = ["Black", "White", "Gray", "Silver", "Red", "Orange", "Yellow",
                 "Green", "Blue", "Navy", "Cyan", "Teal", "Purple", "Pink",
                 "Magenta", "Brown", "Beige", "Gold", "Bronze", "Copper",
                 "Natural", "Clear", "Transparent"]
DEFAULT_BRANDS = ["Bambu Lab", "Polymaker", "eSUN", "Overture", "SUNLU", "Inland",
                  "Hatchbox", "Prusament", "Creality", "Generic"]


def _bambuddy_locations() -> list[str]:
    """Storage-location names from Bambuddy (best-effort; empty on failure)."""
    if not BAMBUDDY_API_KEY:
        return []
    try:
        r = requests.get(f"{BAMBUDDY_URL}/api/v1/inventory/locations",
                         headers={"X-API-Key": BAMBUDDY_API_KEY, "Accept": "application/json"},
                         timeout=10)
        if r.ok:
            return [loc.get("name") for loc in r.json() if loc.get("name")]
    except Exception:
        log.debug("Bambuddy location fetch failed (best-effort)", exc_info=True)
    return []


@app.get("/api/options")
def options():
    """Dropdown data for the form (materials, subtypes, weights, colours, brands, locations)."""
    import ofd
    import spoolmandb_community
    try:
        brands = sorted(
            set(DEFAULT_BRANDS) | set(ofd.get_brands()) | set(spoolmandb_community.get_brands()),
            key=str.lower,
        )
    except Exception:
        brands = DEFAULT_BRANDS
    try:
        materials = sorted(
            set(MATERIAL_OPTIONS) | {m["material"] for m in spoolmandb_community.get_materials() if m.get("material")},
            key=str.lower,
        )
    except Exception:
        materials = MATERIAL_OPTIONS
    # Note: SpoolmanDB-Community color names are deliberately NOT merged into
    # COLOR_OPTIONS — that list is a short generic color-family dropdown
    # (~23 entries), while SpoolmanDB's colors are thousands of specific
    # product names on a different axis (already covered by the free-text
    # color_name field, not a constrained dropdown).
    return jsonify(materials=materials, subtypes=SUBTYPE_OPTIONS,
                   weights=WEIGHT_OPTIONS, colors=COLOR_OPTIONS, brands=brands,
                   locations=_bambuddy_locations())


@app.delete("/api/cache")
def clear_cache():
    """Forget ALL remembered per-barcode lookups."""
    n = len(load_cache())
    save_cache({})
    log.info("cache cleared: %d entries removed", n)
    return jsonify(ok=True, cleared=n)


@app.delete("/api/cache/<barcode>")
def forget_barcode(barcode):
    """Forget the remembered entry for one barcode."""
    cache = load_cache()
    existed = barcode in cache
    if existed:
        del cache[barcode]
        save_cache(cache)
        log.info("cache forget: %s", barcode)
    return jsonify(ok=True, removed=existed)


@app.post("/api/ofd/refresh")
def ofd_refresh():
    """Force a re-download/rebuild of the OFD + SpoolmanDB-Community indexes.

    Kept at this URL (not renamed) so the existing "Refresh DB" button in
    templates/index.html doesn't need a second endpoint/button — one refresh
    action updates both community databases.
    """
    import ofd
    import spoolmandb_community
    log.info("OFD + SpoolmanDB-Community refresh started")

    # Each refresh is independently guarded — an OFD network blip must not
    # take down the whole endpoint (previously it returned 502 immediately
    # and never even attempted the SpoolmanDB-Community refresh) or discard
    # a SpoolmanDB-Community refresh that already succeeded.
    try:
        gtin_idx = ofd.get_gtin_index(force=True)
        article_idx = ofd.get_article_index()
        brands = len(ofd.get_brands())
        ofd_codes = len(gtin_idx) + len(article_idx)
        log.info(
            "OFD refresh done: %d GTINs, %d article numbers, %d brands",
            len(gtin_idx), len(article_idx), brands,
        )
    except Exception as e:
        log.error("OFD refresh failed: %s", e)
        ofd_codes = 0
        brands = 0

    try:
        smdb_gtin_idx = spoolmandb_community.get_gtin_index(force=True)
        smdb_sku_idx = spoolmandb_community.get_sku_index()
        smdb_codes = len(smdb_gtin_idx) + len(smdb_sku_idx)
        log.info("SpoolmanDB-Community refresh done: %d codes", smdb_codes)
    except Exception as e:
        log.error("SpoolmanDB-Community refresh failed: %s", e)
        smdb_codes = 0

    return jsonify(
        ok=True,
        barcodes=ofd_codes,
        brands=brands,
        spoolmandb_community_barcodes=smdb_codes,
    )


@app.get("/api/spool-barcodes")
def spool_barcodes():
    """Return active Bambuddy spools alongside their reusable barcode mapping."""
    if not BAMBUDDY_API_KEY:
        return jsonify(ok=False, error="BAMBUDDY_API_KEY not set on the server"), 400
    try:
        response = requests.get(
            f"{BAMBUDDY_URL}/api/v1/inventory/spools",
            headers=_bambuddy_headers(),
            params={"include_archived": "false"},
            timeout=15,
        )
    except requests.RequestException as exc:
        return jsonify(ok=False, error=str(exc)), 502
    if not response.ok:
        return jsonify(ok=False, error=_bambuddy_error(response)), response.status_code

    raw_spools = response.json()
    if not isinstance(raw_spools, list):
        return jsonify(ok=False, error="Unexpected Bambuddy spool response"), 502
    spools = [_spool_summary(spool) for spool in raw_spools]
    mapping = load_spool_barcodes()
    active_ids = {spool["id"] for spool in spools}
    # Stale keys are not silently removed here: they may become valid again if
    # Bambuddy was temporarily restored from a backup. The UI marks them by
    # omission because only active tracked spools are editable.
    return jsonify(ok=True, spools=spools,
                   mappings=[{"barcode": code, "spool_id": sid}
                             for code, sid in mapping.items() if sid in active_ids])


@app.put("/api/spool-barcodes")
def set_spool_barcode():
    """Bind one unique reusable barcode to one active Bambuddy spool."""
    if not BAMBUDDY_API_KEY:
        return jsonify(ok=False, error="BAMBUDDY_API_KEY not set on the server"), 400
    body = request.get_json(force=True, silent=True) or {}
    barcode = str(body.get("barcode") or "").strip()
    try:
        spool_id = int(body.get("spool_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="A valid spool_id is required"), 400
    if not barcode or len(barcode) > 128:
        return jsonify(ok=False, error="Barcode must contain 1 to 128 characters"), 400

    try:
        response = requests.get(
            f"{BAMBUDDY_URL}/api/v1/inventory/spools/{spool_id}",
            headers=_bambuddy_headers(), timeout=15,
        )
    except requests.RequestException as exc:
        return jsonify(ok=False, error=str(exc)), 502
    if not response.ok:
        return jsonify(ok=False, error=_bambuddy_error(response)), response.status_code
    if (response.json() or {}).get("archived_at"):
        return jsonify(ok=False, error="Cannot map a barcode to an archived spool"), 400

    with _spool_barcode_lock:
        mapping = load_spool_barcodes()
        owner = mapping.get(barcode)
        if owner is not None and owner != spool_id:
            return jsonify(ok=False, error=f"That barcode is already assigned to spool #{owner}"), 409
        replaced = [code for code, sid in mapping.items() if sid == spool_id and code != barcode]
        for code in replaced:
            del mapping[code]
        mapping[barcode] = spool_id
        save_spool_barcodes(mapping)
    return jsonify(ok=True, barcode=barcode, spool_id=spool_id, replaced=replaced)


@app.delete("/api/spool-barcodes/spool/<int:spool_id>")
def delete_spool_barcode(spool_id):
    """Remove every reusable barcode that points at a spool."""
    with _spool_barcode_lock:
        mapping = load_spool_barcodes()
        removed = [code for code, sid in mapping.items() if sid == spool_id]
        for code in removed:
            del mapping[code]
        if removed:
            save_spool_barcodes(mapping)
    return jsonify(ok=True, removed=removed)


@app.get("/api/printers")
def printers():
    """Proxy Bambuddy's printer list without exposing its API key to the browser."""
    if not BAMBUDDY_API_KEY:
        return jsonify(ok=False, error="BAMBUDDY_API_KEY not set on the server"), 400
    try:
        response = requests.get(
            f"{BAMBUDDY_URL}/api/v1/printers/",
            headers=_bambuddy_headers(), timeout=15,
        )
    except requests.RequestException as exc:
        return jsonify(ok=False, error=str(exc)), 502
    if not response.ok:
        return jsonify(ok=False, error=_bambuddy_error(response)), response.status_code
    return jsonify(ok=True, printers=[
        {"id": printer.get("id"), "name": printer.get("name") or f"Printer {printer.get('id')}"}
        for printer in response.json()
    ])


def _slot_label(ams_id: int, tray_id: int, ams_label: str | None = None) -> str:
    if ams_id == 255:
        return "External spool" if tray_id == 0 else f"External spool {tray_id + 1}"
    prefix = ams_label or (f"AMS {ams_id + 1}" if ams_id < 128 else f"AMS HT {ams_id - 127}")
    return f"{prefix} · Slot {tray_id + 1}"


@app.get("/api/printers/<int:printer_id>/slots")
def printer_slots(printer_id):
    """Return selectable AMS/external slots and any current assignments."""
    if not BAMBUDDY_API_KEY:
        return jsonify(ok=False, error="BAMBUDDY_API_KEY not set on the server"), 400
    try:
        status_response = requests.get(
            f"{BAMBUDDY_URL}/api/v1/printers/{printer_id}/status",
            headers=_bambuddy_headers(), timeout=15,
        )
        assignments_response = requests.get(
            f"{BAMBUDDY_URL}/api/v1/inventory/assignments",
            headers=_bambuddy_headers(), params={"printer_id": printer_id}, timeout=15,
        )
    except requests.RequestException as exc:
        return jsonify(ok=False, error=str(exc)), 502
    if not status_response.ok:
        return jsonify(ok=False, error=_bambuddy_error(status_response)), status_response.status_code
    if not assignments_response.ok:
        return jsonify(ok=False, error=_bambuddy_error(assignments_response)), assignments_response.status_code

    assignments = assignments_response.json() if isinstance(assignments_response.json(), list) else []
    assignment_map = {(a.get("ams_id"), a.get("tray_id")): a for a in assignments}
    status = status_response.json() or {}
    slots: dict[tuple[int, int], dict] = {}

    for unit in status.get("ams") or []:
        ams_id = int(unit.get("id", 0))
        for tray in unit.get("tray") or []:
            tray_id = int(tray.get("id", 0))
            slots[(ams_id, tray_id)] = {
                "ams_id": ams_id, "tray_id": tray_id,
                "label": _slot_label(ams_id, tray_id),
            }
    for tray in status.get("vt_tray") or []:
        global_id = int(tray.get("id", 254))
        tray_id = max(0, global_id - 254)
        slots[(255, tray_id)] = {
            "ams_id": 255, "tray_id": tray_id,
            "label": _slot_label(255, tray_id),
        }

    # Offline printers may not expose live slot data. Keep assigned slots
    # selectable so the user can still replace their database assignment.
    for key, assignment in assignment_map.items():
        if key not in slots:
            slots[key] = {
                "ams_id": key[0], "tray_id": key[1],
                "label": _slot_label(key[0], key[1], assignment.get("ams_label")),
            }

    for key, slot in slots.items():
        assignment = assignment_map.get(key)
        if assignment:
            slot["label"] = _slot_label(key[0], key[1], assignment.get("ams_label"))
            slot["assignment"] = {
                "spool_id": assignment.get("spool_id"),
                "spool": _spool_summary(assignment.get("spool")),
            }
    ordered = sorted(slots.values(), key=lambda item: (item["ams_id"] == 255, item["ams_id"], item["tray_id"]))
    return jsonify(ok=True, connected=bool(status.get("connected")), slots=ordered)


@app.post("/api/spool-assignment")
def assign_spool_by_barcode():
    """Resolve a reusable barcode and assign its spool to a Bambuddy slot."""
    if not BAMBUDDY_API_KEY:
        return jsonify(ok=False, error="BAMBUDDY_API_KEY not set on the server"), 400
    body = request.get_json(force=True, silent=True) or {}
    barcode = str(body.get("barcode") or "").strip()
    try:
        printer_id = int(body.get("printer_id"))
        ams_id = int(body.get("ams_id"))
        tray_id = int(body.get("tray_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Printer and slot are required"), 400
    if not barcode:
        return jsonify(ok=False, error="Barcode is required"), 400

    mapping = load_spool_barcodes()
    spool_id = mapping.get(barcode)
    if spool_id is None:
        return jsonify(ok=False, error="This barcode is not linked to a tracked spool"), 404

    try:
        assignments_response = requests.get(
            f"{BAMBUDDY_URL}/api/v1/inventory/assignments",
            headers=_bambuddy_headers(), params={"printer_id": printer_id}, timeout=15,
        )
    except requests.RequestException as exc:
        return jsonify(ok=False, error=str(exc)), 502
    if not assignments_response.ok:
        return jsonify(ok=False, error=_bambuddy_error(assignments_response)), assignments_response.status_code
    assignments = assignments_response.json() if isinstance(assignments_response.json(), list) else []
    existing = next((a for a in assignments
                     if a.get("ams_id") == ams_id and a.get("tray_id") == tray_id), None)
    old_spool = _spool_summary(existing.get("spool")) if existing else None
    if existing and existing.get("spool_id") == spool_id:
        return jsonify(ok=True, already_assigned=True, spool_id=spool_id,
                       assignment=existing, deleted_existing=False)

    delete_choice = body.get("delete_existing")
    if old_spool and not old_spool["empty"] and delete_choice is None:
        return jsonify(ok=False, confirmation_required=True, existing_spool=old_spool), 409
    delete_old = bool(old_spool and (old_spool["empty"] or delete_choice is True))

    payload = {"spool_id": spool_id, "printer_id": printer_id,
               "ams_id": ams_id, "tray_id": tray_id}
    try:
        assignment_response = requests.post(
            f"{BAMBUDDY_URL}/api/v1/inventory/assignments",
            headers=_bambuddy_headers(json_body=True), json=payload, timeout=30,
        )
    except requests.RequestException as exc:
        return jsonify(ok=False, error=str(exc)), 502
    if not assignment_response.ok:
        return jsonify(ok=False, error=_bambuddy_error(assignment_response)), assignment_response.status_code

    deleted_existing = False
    warning = None
    if delete_old:
        old_spool_id = int(old_spool["id"])
        try:
            delete_response = requests.delete(
                f"{BAMBUDDY_URL}/api/v1/inventory/spools/{old_spool_id}",
                headers=_bambuddy_headers(), timeout=30,
            )
            if delete_response.ok:
                deleted_existing = True
                with _spool_barcode_lock:
                    latest = load_spool_barcodes()
                    for code in [code for code, sid in latest.items() if sid == old_spool_id]:
                        del latest[code]
                    save_spool_barcodes(latest)
            else:
                warning = f"Spool assigned, but old spool could not be deleted: {_bambuddy_error(delete_response)}"
        except requests.RequestException as exc:
            warning = f"Spool assigned, but old spool could not be deleted: {exc}"

    return jsonify(ok=True, spool_id=spool_id, assignment=assignment_response.json(),
                   deleted_existing=deleted_existing, warning=warning)


@app.get("/api/health")
def health():
    """Confirm Bambuddy is reachable + config present, with a specific reason."""
    if not BAMBUDDY_API_KEY:
        return jsonify(ok=False, bambuddy=BAMBUDDY_URL,
                       error="BAMBUDDY_API_KEY not set — start with ./run.sh"), 200
    headers = {"X-API-Key": BAMBUDDY_API_KEY, "Accept": "application/json"}
    # Probe the endpoint we actually use (inventory), so the key is checked
    # against the permission this app needs.
    try:
        r = requests.get(f"{BAMBUDDY_URL}/api/v1/inventory/spools",
                         headers=headers, params={"limit": 1}, timeout=10)
    except requests.exceptions.SSLError as e:
        return jsonify(ok=False, bambuddy=BAMBUDDY_URL, error=f"TLS error: {e}"), 200
    except requests.exceptions.ConnectionError:
        return jsonify(ok=False, bambuddy=BAMBUDDY_URL,
                       error=f"cannot connect to {BAMBUDDY_URL} (wrong URL, or not on the same network?)"), 200
    except Exception as e:
        return jsonify(ok=False, bambuddy=BAMBUDDY_URL, error=str(e)), 200

    if r.ok:
        return jsonify(ok=True, bambuddy=BAMBUDDY_URL, status=r.status_code)
    if r.status_code == 401:
        return jsonify(ok=False, level="error", bambuddy=BAMBUDDY_URL,
                       error="API key rejected (HTTP 401) — wrong or revoked key"), 200
    if r.status_code == 403:
        # Reachable + key valid, but the key lacks inventory permission. Not a
        # connectivity failure — surface as a fixable warning.
        return jsonify(ok=False, level="warn", bambuddy=BAMBUDDY_URL,
                       error=("connected, but this API key lacks inventory access. "
                              "In Bambuddy → Settings → API Keys, enable 'Manage Inventory' "
                              "(required to add spools) and 'Read Status', then update run.sh.")), 200
    return jsonify(ok=False, level="error", bambuddy=BAMBUDDY_URL,
                   error=f"HTTP {r.status_code}: {r.text[:160]}"), 200


@app.get("/api/lookup")
def lookup():
    """Resolve a barcode or SKU to filament fields: cache first, then a
    cross-referenced OFD/SpoolmanDB-Community lookup.

    A scanned/typed code is classified server-side as a GTIN (checksummed,
    standard length) or a manufacturer SKU/article number (e.g. a Code 128
    "inventory barcode" with no UPC/EAN counterpart) via `_classify_code`,
    then resolved through the matching path in each database. Whichever
    database hits first, its sibling codes (other package-size GTINs, the
    refill GTIN, the SKU) are cross-referenced against the *other* database
    to fill in any missing fields — see `_external_all_codes`.
    """
    barcode = (request.args.get("barcode") or "").strip()
    if not barcode:
        return jsonify(error="barcode required"), 400

    code, kind = _classify_code(barcode)

    # 1. Personal cache — your own confirmed entries win. Checked under both
    # the canonicalized code and the raw scanned/typed string, so cache
    # entries written before this rework (keyed by the un-canonicalized
    # value) still hit.
    cache = load_cache()
    cache_hit = cache.get(code)
    if cache_hit is None and code != barcode:
        cache_hit = cache.get(barcode)
    if cache_hit is not None:
        log.info("lookup %s: cache hit", barcode)
        return jsonify(barcode=code, source="cache", fields=cache_hit, title=None, linked_codes=[])

    # 2. Cross-referenced OFD + SpoolmanDB-Community lookup.
    external = _external_all_codes(code, kind)
    if external:
        fields, source, all_codes, title = external
        clean_fields = {k: v for k, v in fields.items() if not k.startswith("_")}
        clean_fields.setdefault("label_weight", DEFAULT_LABEL_WEIGHT)
        linked_codes = [c for c in all_codes if c["code"] != code]
        log.info("lookup %s: %s hit — %s", barcode, source, title or "")
        return jsonify(barcode=code, source=source, title=title, fields=clean_fields, linked_codes=linked_codes)

    # 3. Not found — fill in manually (and it'll be remembered).
    src = "amazon" if kind == "sku" else "none"
    log.info("lookup %s: not found (source=%s)", barcode, src)
    return jsonify(barcode=code, source=src,
                   fields={"label_weight": DEFAULT_LABEL_WEIGHT}, title=None, linked_codes=[])


def _extract_barcode(text: str) -> str | None:
    """Pull a barcode (UPC/EAN/GTIN) out of free text, e.g. label OCR.

    Prefers a labelled number ("EAN: 6938936716785"); falls back to any bare
    12–14 digit run. Returns digits only, or None.
    """
    import re
    m = re.search(r"(?:EAN|UPC|GTIN|BARCODE)\s*[:#]?\s*(\d[\d\s]{6,16}\d)", text, re.IGNORECASE)
    cand = re.sub(r"\D", "", m.group(1)) if m else None
    if not cand:
        m = re.search(r"(?<!\d)(\d{12,14})(?!\d)", text)
        cand = m.group(1) if m else None
    return cand if cand and 8 <= len(cand) <= 14 else None


@app.get("/api/parse")
def parse_endpoint():
    """Parse free text (pasted title or label OCR) into filament fields.

    Heuristically parses the text, and if a barcode is present in it, looks
    that barcode up (OFD, then SpoolmanDB-Community) — authoritative data
    overrides the guesses when found.
    """
    import ofd
    from filament_parse import parse_title
    title = (request.args.get("title") or "").strip()
    if not title:
        return jsonify(fields={})
    try:
        brands = ofd.get_brands()
    except Exception:
        brands = []
    fields = parse_title(title, extra_brands=brands)

    source, out_title = "parsed", title
    linked_codes: list = []
    barcode = _extract_barcode(title)
    resolved_code = barcode
    if barcode:
        code, kind = _classify_code(barcode)
        external = _external_all_codes(code, kind)
        if external:
            ext_fields, ext_source, all_codes, ext_title = external
            fields = {**fields, **{k: v for k, v in ext_fields.items() if not k.startswith("_") and v is not None}}
            source = ext_source
            out_title = ext_title or title
            resolved_code = code
            linked_codes = [c for c in all_codes if c["code"] != code]

    fields.setdefault("label_weight", DEFAULT_LABEL_WEIGHT)
    return jsonify(fields=fields, title=out_title, barcode=resolved_code, source=source, linked_codes=linked_codes)


@app.post("/api/spool")
def add_spool():
    """Create one or more spools in Bambuddy, then remember the details by barcode."""
    if not BAMBUDDY_API_KEY:
        return jsonify(ok=False, error="BAMBUDDY_API_KEY not set on the server"), 400

    body = request.get_json(force=True, silent=True) or {}
    barcode = (body.get("barcode") or "").strip()
    quantity = max(1, min(int(body.get("quantity") or 1), 50))
    fields = body.get("fields") or {}

    if not (fields.get("material") or "").strip():
        return jsonify(ok=False, error="Material is required"), 400

    # Build a clean Bambuddy payload from whitelisted, typed fields.
    payload: dict = {}
    for key in ALLOWED_SPOOL_FIELDS:
        if key not in fields or fields[key] in (None, ""):
            continue
        val = fields[key]
        if key in ("label_weight", "core_weight", "nozzle_temp_min", "nozzle_temp_max"):
            try:
                val = int(val)
            except (TypeError, ValueError):
                continue
        elif key == "cost_per_kg":
            try:
                val = float(val)
            except (TypeError, ValueError):
                continue
        payload[key] = val
    payload.setdefault("label_weight", DEFAULT_LABEL_WEIGHT)
    payload["data_origin"] = "barcode-scan"

    headers = {"X-API-Key": BAMBUDDY_API_KEY, "Content-Type": "application/json",
               "Accept": "application/json"}
    created, errors = 0, []
    for _ in range(quantity):
        try:
            r = requests.post(f"{BAMBUDDY_URL}/api/v1/inventory/spools",
                              json=payload, headers=headers, timeout=30)
            if r.status_code in (200, 201):
                created += 1
            else:
                errors.append(f"{r.status_code}: {r.text[:200]}")
                break
        except Exception as e:
            errors.append(str(e))
            break

    # Remember the confirmed details for this barcode (learning cache), plus
    # every cross-referenced sibling code — so a later scan of ANY of them
    # (another package-size GTIN, the refill GTIN, the manufacturer SKU)
    # resolves instantly as "remembered" instead of re-querying OFD/SpoolmanDB.
    if created and barcode:
        cache = load_cache()
        remembered = {k: v for k, v in fields.items()
                      if k in ALLOWED_SPOOL_FIELDS or k in ("diameter_mm",)}
        code, kind = _classify_code(barcode)
        cache[code] = remembered
        try:
            external = _external_all_codes(code, kind)
        except Exception:
            external = None
        if external:
            _, _, all_codes, _ = external
            for entry in all_codes:
                if entry["code"] != code:
                    cache[entry["code"]] = remembered
        save_cache(cache)

    ok = created == quantity
    material = payload.get("material", "?")
    brand = payload.get("brand", "")
    label = f"{brand} {material}".strip() if brand else material
    if ok:
        log.info("spool added: %dx %s (barcode=%s)", created, label, barcode or "none")
    else:
        log.warning("spool add partial: %d/%d %s (barcode=%s) errors=%s",
                    created, quantity, label, barcode or "none", errors)
    return jsonify(ok=ok, created=created, requested=quantity,
                   errors=errors, bambuddy=BAMBUDDY_URL), (200 if created else 502)


if __name__ == "__main__":
    print(f"  Bambuddy: {BAMBUDDY_URL}  (API key {'set' if BAMBUDDY_API_KEY else 'NOT set'})")
    print(f"  Open on your phone:  http://<this-computer-ip>:{PORT}/")
    print("  ⚠️  Camera scanning needs https:// or localhost — see README.\n")
    app.run(host=HOST, port=PORT, debug=False)
