#!/usr/bin/env python3
"""Flask GUI for the Yad2 vehicle monitor."""

import collections
import csv
import io
import json
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

import config
import monitor

app = Flask(__name__)

BASE_DIR = Path(__file__).parent
LOG_PATH = BASE_DIR / config.LOG_FILE
PROFILES_PATH = BASE_DIR / config.PROFILES_FILE

_monitor_thread: threading.Thread | None = None
_monitor_lock = threading.Lock()

DISABLED_PARAMS = {"manufacturer", "model", "subModel"}
BOOLEAN_PARAMS = {"priceOnly", "imgOnly", "ownerID"}
RANGE_PARAMS = {"year", "price", "km", "hand"}

SVG_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="12" fill="#6c8cff"/>'
    '<path d="M12 38c0-2 1-4 3-5l5-10c1-3 4-5 7-5h10c3 0 6 2 7 5l5 10c2 1 3 3 3 5v6'
    'c0 2-1 3-3 3h-2c-1 0-2-1-2-3h-20c0 2-1 3-2 3h-2c-2 0-3-1-3-3z" fill="#fff"/>'
    '<circle cx="20" cy="40" r="3" fill="#6c8cff"/>'
    '<circle cx="44" cy="40" r="3" fill="#6c8cff"/>'
    '</svg>'
)


def _is_monitor_running() -> bool:
    return _monitor_thread is not None and _monitor_thread.is_alive()


def _resolve_display_name(param: str, value: str) -> str:
    return config.DISPLAY_NAMES.get(param, {}).get(value, value)


def _load_profiles() -> dict:
    if not PROFILES_PATH.exists():
        return {}
    try:
        return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_profiles(profiles: dict) -> None:
    PROFILES_PATH.write_text(
        json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    return Response(SVG_FAVICON, mimetype="image/svg+xml")


@app.get("/api/params")
def get_params():
    params = dict(config.YAD2_PARAMS)

    display = {}
    for key in DISABLED_PARAMS:
        if key in params:
            display[key] = _resolve_display_name(key, params[key])

    return jsonify({
        "params": params,
        "display": display,
        "checkInterval": config.CHECK_INTERVAL_SECONDS,
        "autoStart": config.AUTO_START,
    })


@app.post("/api/params")
def set_params():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid JSON body"}), 400

    new_params = data.get("params", {})
    for key, value in new_params.items():
        if key in DISABLED_PARAMS:
            continue
        config.YAD2_PARAMS[key] = str(value)

    if "checkInterval" in data:
        try:
            config.CHECK_INTERVAL_SECONDS = max(5, int(data["checkInterval"]))
        except (ValueError, TypeError):
            pass

    if "autoStart" in data:
        config.AUTO_START = bool(data["autoStart"])

    return jsonify({"ok": True})


@app.post("/api/monitor/start")
def monitor_start():
    global _monitor_thread
    with _monitor_lock:
        if _is_monitor_running():
            return jsonify({"ok": False, "error": "already running"})

        _monitor_thread = threading.Thread(
            target=monitor.run_loop, daemon=True, name="yad2-monitor"
        )
        _monitor_thread.start()
    return jsonify({"ok": True})


@app.post("/api/monitor/stop")
def monitor_stop():
    global _monitor_thread
    with _monitor_lock:
        if not _is_monitor_running():
            return jsonify({"ok": False, "error": "not running"})
        monitor.shutdown_event.set()
    _monitor_thread.join(timeout=10)
    return jsonify({"ok": True})


@app.get("/api/monitor/status")
def monitor_status():
    state = monitor.get_state()
    state["running"] = _is_monitor_running()
    return jsonify(state)


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------

@app.get("/api/listings")
def get_listings():
    return jsonify({"listings": monitor.get_found_listings()})


@app.delete("/api/listings/<token>")
def dismiss_listing(token):
    removed = monitor.remove_found(token)
    return jsonify({"ok": removed})


@app.delete("/api/listings")
def clear_listings():
    monitor.clear_found()
    return jsonify({"ok": True})


@app.get("/api/listings/export")
def export_listings():
    items = monitor.get_found_listings()
    if not items:
        return Response("No data", status=204)

    fields = ["token", "manufacturer", "model", "sub_model", "price", "year", "km", "hand", "area", "link", "found_at"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        writer.writerow(item)

    output = buf.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=listings.csv"},
    )


# ---------------------------------------------------------------------------
# Seen (clear / reset)
# ---------------------------------------------------------------------------

@app.delete("/api/seen")
def clear_seen():
    monitor.clear_seen()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@app.get("/api/logs")
def get_logs():
    if not LOG_PATH.exists():
        return jsonify({"lines": []})

    try:
        tail = collections.deque(
            LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines(),
            maxlen=80,
        )
        return jsonify({"lines": list(tail)})
    except OSError:
        return jsonify({"lines": []})


@app.delete("/api/logs")
def clear_logs():
    try:
        LOG_PATH.write_text("", encoding="utf-8")
    except OSError:
        pass
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Profiles (Phase 6b)
# ---------------------------------------------------------------------------

@app.get("/api/profiles")
def list_profiles():
    return jsonify(_load_profiles())


@app.post("/api/profiles")
def save_profile():
    data = request.get_json(silent=True)
    if not data or "name" not in data:
        return jsonify({"error": "missing name"}), 400
    name = data["name"].strip()
    if not name:
        return jsonify({"error": "empty name"}), 400

    profiles = _load_profiles()
    profiles[name] = {
        "params": dict(config.YAD2_PARAMS),
        "checkInterval": config.CHECK_INTERVAL_SECONDS,
    }
    _save_profiles(profiles)
    return jsonify({"ok": True})


@app.post("/api/profiles/<name>/load")
def load_profile(name):
    profiles = _load_profiles()
    if name not in profiles:
        return jsonify({"error": "profile not found"}), 404
    profile = profiles[name]
    config.YAD2_PARAMS.update(profile.get("params", {}))
    if "checkInterval" in profile:
        config.CHECK_INTERVAL_SECONDS = max(5, int(profile["checkInterval"]))
    return jsonify({"ok": True})


@app.delete("/api/profiles/<name>")
def delete_profile(name):
    profiles = _load_profiles()
    if name not in profiles:
        return jsonify({"error": "profile not found"}), 404
    del profiles[name]
    _save_profiles(profiles)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=5001)
