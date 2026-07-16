# web/routers/settings.py
from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
import copy
import json
import logging

from web.shared_state import (
    SETTINGS_SCHEMA,
    get_env_path,
)
from transcodarr_core.database import (
    get_all_settings, get_setting, set_setting,
    get_encoding_presets, get_encoding_preset, create_encoding_preset,
    update_encoding_preset, delete_encoding_preset,
    restore_default_presets, get_auto_preset, save_auto_rules,
)
from dotenv import dotenv_values

router = APIRouter()


def _schema_with_hw_availability():
    """
    SETTINGS_SCHEMA annotated with the backends this node actually detected.

    Returns a copy — SETTINGS_SCHEMA is module-level shared state and must not be
    mutated per-request. Without this the picker lists every backend on every
    host, so choosing NVENC on an Intel box looks like it worked while silently
    falling back to software.
    """
    schema = copy.deepcopy(SETTINGS_SCHEMA)
    try:
        from transcodarr_core.ffmpeg.capabilities import detect_capabilities
        backends = {b["id"]: b for b in detect_capabilities()["backends"]}
    except Exception as e:
        logging.debug("[SETTINGS] capability annotation skipped: %s", e)
        return schema

    for section in schema.values():
        field = section.get("fields", {}).get("HW_BACKEND")
        if not field:
            continue
        for opt in field.get("options", []):
            b = backends.get(opt["value"])
            if not b or opt["value"] == "software":
                continue
            if b["available"]:
                codecs = ", ".join(b.get("codecs") or [])
                opt["label"] += f" — detected{f' ({codecs})' if codecs else ''}"
            else:
                opt["label"] += " — not detected on this host"
                opt["disabled"] = True
        break
    return schema


@router.get("/settings")
def api_get_settings(request: Request):
    """Return all settings with schema for UI rendering."""
    try:
        db_values = get_all_settings()
        s = request.app.state.settings

        result = {"schema": _schema_with_hw_availability(), "values": {},
                  "encoding_presets": [], "active_preset_id": None}

        for section_key, section in SETTINGS_SCHEMA.items():
            for field_key in section["fields"]:
                value = db_values.get(field_key)
                if value is None:
                    value = getattr(s, field_key, None)
                result["values"][field_key] = value if value is not None else ""

        try:
            result["encoding_presets"] = get_encoding_presets()
        except Exception:
            pass

        try:
            active_id = get_setting("ACTIVE_PRESET_ID")
            if active_id:
                result["active_preset_id"] = int(active_id)
        except Exception:
            pass

        return result
    except Exception as e:
        logging.exception("[SETTINGS] Failed to get settings")
        return JSONResponse({"error": str(e), "type": type(e).__name__}, status_code=500)


@router.post("/settings")
def api_save_settings(request: Request, data: dict = Body(default={})):
    """Save settings to database."""
    from transcodarr_core.config import DB_BACKED_SETTINGS

    updated = []
    errors = []

    valid_keys = set()
    for section in SETTINGS_SCHEMA.values():
        valid_keys.update(section["fields"].keys())

    for key, value in data.items():
        if key not in valid_keys:
            continue
        if key not in DB_BACKED_SETTINGS:
            continue

        try:
            if set_setting(key, str(value) if value is not None else ""):
                updated.append(key)
            else:
                errors.append({"key": key, "error": "Database write failed"})
        except Exception as e:
            errors.append({"key": key, "error": str(e)})

    # Live-reconfigure worker pool if worker counts changed
    worker_keys = {"MANUAL_WORKERS", "AUTO_WORKERS"}
    if worker_keys & set(updated):
        worker_pool = request.app.state.worker_pool
        if worker_pool:
            try:
                from transcodarr_core.config import get_setting as config_get_setting
                mw = int(config_get_setting("MANUAL_WORKERS", 0))
                aw = int(config_get_setting("AUTO_WORKERS", 2))
                worker_pool.reconfigure(mw, aw)
            except Exception as e:
                logging.warning("[SETTINGS] Failed to reconfigure worker pool: %s", e)

    return {
        "status": "ok" if not errors else "partial",
        "updated": updated,
        "errors": errors,
        "message": "Settings saved." if updated else "No changes made."
    }


@router.post("/settings/migrate-from-env")
def api_migrate_settings_from_env():
    """One-time migration: Copy runtime settings from .env to database."""
    from transcodarr_core.config import DB_BACKED_SETTINGS

    env_path = get_env_path()
    if not env_path.exists():
        return JSONResponse({"error": "No .env file found", "migrated": [], "skipped": []}, status_code=404)

    env_values = dotenv_values(env_path)
    migrated = []
    skipped = []
    errors = []

    existing_db = get_all_settings()

    for key in DB_BACKED_SETTINGS:
        env_val = env_values.get(key)
        if env_val is None:
            continue

        if key in existing_db and existing_db[key]:
            skipped.append({"key": key, "reason": "Already in database"})
            continue

        try:
            if set_setting(key, env_val):
                migrated.append(key)
                logging.info(f"[MIGRATE] Migrated {key} to database")
            else:
                errors.append({"key": key, "error": "Database write failed"})
        except Exception as e:
            errors.append({"key": key, "error": str(e)})

    return {
        "status": "ok" if not errors else "partial",
        "migrated": migrated,
        "skipped": skipped,
        "errors": errors,
        "message": f"Migrated {len(migrated)} settings from .env to database"
    }


@router.get("/auto-rules")
def api_get_auto_rules():
    """Return Auto preset rules and available presets for target dropdowns."""
    auto = get_auto_preset()
    rules_data = auto.get("auto_rules", {}) if auto else {}
    presets = get_encoding_presets()
    # Exclude Auto itself from target options
    target_presets = [{"id": p["id"], "name": p["name"]} for p in presets if not p.get("auto_rules")]
    return {
        "rules": rules_data.get("rules", []),
        "fallback_preset_id": rules_data.get("fallback_preset_id"),
        "target_presets": target_presets,
        "auto_preset_id": auto["id"] if auto else None,
    }


@router.post("/auto-rules")
def api_save_auto_rules(data: dict = Body(default={})):
    """Validate and save Auto preset rules."""
    rules = data.get("rules", [])
    fallback_id = data.get("fallback_preset_id")

    if not isinstance(rules, list):
        return JSONResponse({"error": "rules must be a list"}, status_code=400)

    # Validate each rule
    valid_preset_ids = {p["id"] for p in get_encoding_presets() if not p.get("auto_rules")}
    for i, rule in enumerate(rules):
        target_id = rule.get("target_preset_id")
        if not target_id or target_id not in valid_preset_ids:
            return JSONResponse({"error": f"Rule {i+1}: invalid target preset"}, status_code=400)
        conditions = rule.get("conditions", {})

    if fallback_id and fallback_id not in valid_preset_ids:
        return JSONResponse({"error": "Invalid fallback preset"}, status_code=400)

    rules_data = {"rules": rules, "fallback_preset_id": fallback_id}
    if save_auto_rules(rules_data):
        return {"status": "ok", "rules": rules, "fallback_preset_id": fallback_id}
    return JSONResponse({"error": "Failed to save rules"}, status_code=500)


@router.post("/presets/activate")
def api_activate_preset(data: dict = Body(default={})):
    """Set the active encoding preset. Also writes preset settings to DB for non-Auto presets."""
    preset_id = data.get("preset_id")
    if not preset_id:
        return JSONResponse({"error": "preset_id is required"}, status_code=400)

    preset = get_encoding_preset(preset_id)
    if not preset:
        return JSONResponse({"error": "Preset not found"}, status_code=404)

    set_setting("ACTIVE_PRESET_ID", str(preset_id))

    # For non-Auto presets, write their settings to individual DB settings
    if not preset.get("auto_rules") and preset.get("settings"):
        from transcodarr_core.config import DB_BACKED_SETTINGS
        for key, val in preset["settings"].items():
            if key in DB_BACKED_SETTINGS:
                set_setting(key, val)

    return {"status": "ok", "active_preset_id": preset_id, "preset_name": preset["name"]}


# ── Encoding Presets ────────────────────────────────────────────────────────

@router.get("/encoding-presets")
def api_get_presets():
    """List all encoding presets."""
    return {"presets": get_encoding_presets()}


@router.post("/encoding-presets")
def api_create_preset(data: dict = Body(default={})):
    """Create a custom encoding preset."""
    name = data.get("name", "").strip()
    settings = data.get("settings", {})
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    if not settings or not isinstance(settings, dict):
        return JSONResponse({"error": "settings dict is required"}, status_code=400)

    result = create_encoding_preset(name, settings)
    if result is None:
        return JSONResponse({"error": "Preset name already exists"}, status_code=409)
    return {"status": "created", "preset": result}


@router.put("/encoding-presets/{preset_id}")
def api_update_preset(preset_id: int, data: dict = Body(default={})):
    """Update a custom encoding preset. Cannot update built-in presets."""
    name = data.get("name", "").strip()
    settings = data.get("settings", {})
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    if not settings or not isinstance(settings, dict):
        return JSONResponse({"error": "settings dict is required"}, status_code=400)

    result = update_encoding_preset(preset_id, name, settings)
    if result is None:
        return JSONResponse({"error": "Preset not found or is a built-in preset"}, status_code=400)
    return {"status": "updated", "preset": result}


@router.delete("/encoding-presets/{preset_id}")
def api_delete_preset(preset_id: int):
    """Delete a custom encoding preset. Cannot delete built-in presets."""
    if delete_encoding_preset(preset_id):
        return {"status": "deleted"}
    return JSONResponse({"error": "Preset not found or is a built-in preset"}, status_code=400)


@router.post("/encoding-presets/restore")
def api_restore_presets():
    """Re-insert any missing built-in presets."""
    count = restore_default_presets()
    return {"status": "ok", "restored": count}
