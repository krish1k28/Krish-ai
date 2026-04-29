#!/usr/bin/env python3
"""
ChatBot.py

Flask backend that:
- Serves index.html and root-level static assets from the project root.
- Provides simple chat persistence (chats.json).
- Proxies /api/chat (or /api/chats/<id>/message) to OpenRouter using OPENROUTER_API_KEY.
- Enables CORS so a static site (e.g., GitHub Pages) can call this backend.
- Includes basic logging and defensive error handling.

Place this file in the project root alongside index.html and avatar.png.
Install dependencies:
    pip install flask python-dotenv requests flask-cors
"""
import os
import json
import time
import logging
from pathlib import Path
from threading import Lock

import requests
from flask import Flask, request, jsonify, send_from_directory, abort
from dotenv import load_dotenv
from flask_cors import CORS

# Load environment variables from .env
load_dotenv()

# Basic logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("chatbot")

# Paths
APP_ROOT = Path(__file__).parent.resolve()
CHATS_FILE = APP_ROOT / "chats.json"

# Upstream chat API config
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
CHAT_URL = os.getenv("CHAT_URL", "https://openrouter.ai/api/v1/chat/completions")

# CORS config: set ALLOWED_ORIGINS to a comma-separated list (e.g., https://yourdomain.com,https://github.io)
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*")

# Thread-safe file access
file_lock = Lock()

# Create Flask app. static_folder is None because we serve root files explicitly.
app = Flask(__name__, static_folder=None)

# Enable CORS
CORS(app, origins=[o.strip() for o in ALLOWED_ORIGINS.split(",")] if ALLOWED_ORIGINS != "*" else "*")


# -------------------------
# Persistence helpers
# -------------------------
def _init_chats_file():
    """Create chats.json with a default 'general' chat if it doesn't exist."""
    if not CHATS_FILE.exists():
        default = {
            "chats": [
                {
                    "id": "general",
                    "name": "general",
                    "created_at": time.time(),
                    "metadata": {},
                    "messages": [
                        {
                            "role": "assistant",
                            "content": "Hi, I'm Krish! I can help with coding, debugging, and explanations.",
                            "timestamp": time.time()
                        }
                    ]
                }
            ]
        }
        with file_lock:
            CHATS_FILE.write_text(json.dumps(default, indent=2))
            logger.info("Created default chats.json")


def load_chats():
    """Load chats.json and return dict."""
    _init_chats_file()
    with file_lock:
        try:
            return json.loads(CHATS_FILE.read_text())
        except Exception:
            logger.exception("Failed to read chats.json, reinitializing")
            _init_chats_file()
            return json.loads(CHATS_FILE.read_text())


def save_chats(data):
    """Save chats dict to chats.json."""
    with file_lock:
        CHATS_FILE.write_text(json.dumps(data, indent=2))


# -------------------------
# Utility helpers
# -------------------------
def now_ts():
    return time.time()


def find_chat(data, chat_id):
    for c in data.get("chats", []):
        if c.get("id") == chat_id:
            return c
    return None


# -------------------------
# Static asset routes (serve from project root)
# -------------------------
@app.route("/avatar.png")
def avatar():
    """Serve avatar.png from project root."""
    avatar_path = APP_ROOT / "avatar.png"
    if not avatar_path.exists():
        abort(404)
    return send_from_directory(str(APP_ROOT), "avatar.png")


@app.route("/<path:filename>")
def root_static(filename):
    """
    Serve other root-level files (useful for robots.txt, favicon.ico, styles.css, app.js).
    Be cautious: this exposes files by name from the project root.
    """
    target = APP_ROOT / filename
    if not target.exists() or not target.is_file():
        abort(404)
    return send_from_directory(str(APP_ROOT), filename)


# -------------------------
# Frontend
# -------------------------
@app.route("/")
def index():
    """Serve index.html from project root."""
    index_path = APP_ROOT / "index.html"
    if not index_path.exists():
        logger.error("index.html not found in project root: %s", APP_ROOT)
        abort(404)
    return send_from_directory(str(APP_ROOT), "index.html")


# -------------------------
# Chat management endpoints
# -------------------------
@app.route("/api/chats", methods=["GET"])
def api_list_chats():
    data = load_chats()
    chats = [{"id": c["id"], "name": c["name"]} for c in data.get("chats", [])]
    return jsonify({"chats": chats})


@app.route("/api/chats/create", methods=["POST"])
def api_create_chat():
    payload = request.get_json(force=True, silent=True) or {}
    name = (payload.get("name") or "").strip()
    metadata = payload.get("metadata") or {}
    if not name:
        return jsonify({"error": "Chat name required"}), 400

    data = load_chats()
    new_id = f"chat_{int(time.time()*1000)}"
    new_chat = {
        "id": new_id,
        "name": name,
        "created_at": now_ts(),
        "metadata": metadata,
        "messages": [
            {
                "role": "assistant",
                "content": "New chat created. Hi, I'm Krish! I can help with coding, debugging, and explanations.",
                "timestamp": now_ts()
            }
        ]
    }
    data.setdefault("chats", []).insert(0, new_chat)
    save_chats(data)
    return jsonify({"chat": {"id": new_id, "name": name}})


@app.route("/api/chats/<chat_id>/rename", methods=["POST"])
def api_rename_chat(chat_id):
    payload = request.get_json(force=True, silent=True) or {}
    new_name = (payload.get("name") or "").strip()
    if not new_name:
        return jsonify({"error": "New name required"}), 400
    data = load_chats()
    chat = find_chat(data, chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404
    chat["name"] = new_name
    save_chats(data)
    return jsonify({"ok": True})


@app.route("/api/chats/<chat_id>/delete", methods=["POST"])
def api_delete_chat(chat_id):
    data = load_chats()
    chats = data.get("chats", [])
    if len(chats) <= 1:
        return jsonify({"error": "Cannot delete the last chat"}), 400
    new_list = [c for c in chats if c.get("id") != chat_id]
    if len(new_list) == len(chats):
        return jsonify({"error": "Chat not found"}), 404
    data["chats"] = new_list
    save_chats(data)
    return jsonify({"ok": True})


# -------------------------
# Messages endpoints
# -------------------------
@app.route("/api/chats/<chat_id>/messages", methods=["GET"])
def api_get_messages(chat_id):
    data = load_chats()
    chat = find_chat(data, chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404
    return jsonify({"messages": chat.get("messages", [])})


@app.route("/api/chats/<chat_id>/messages", methods=["POST"])
def api_post_message(chat_id):
    """
    Append a user message to the chat and optionally proxy to upstream chat API.
    The frontend can call /api/chat to get completions; this endpoint only stores messages.
    """
    payload = request.get_json(force=True, silent=True) or {}
    role = payload.get("role", "user")
    content = payload.get("content", "")
    if not content:
        return jsonify({"error": "Message content required"}), 400

    data = load_chats()
    chat = find_chat(data, chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404

    message = {"role": role, "content": content, "timestamp": now_ts()}
    chat.setdefault("messages", []).append(message)
    save_chats(data)
    return jsonify({"ok": True, "message": message})


# -------------------------
# Proxy to upstream chat completion API
# -------------------------
@app.route("/api/chat", methods=["POST"])
def api_chat():
    """
    Proxy request body to the configured CHAT_URL using OPENROUTER_API_KEY.
    This keeps the API key server-side and allows the static frontend to call this endpoint.
    """
    payload = request.get_json(force=True, silent=True)
    if payload is None:
        return jsonify({"error": "Invalid JSON payload"}), 400

    if not OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY not set")
        return jsonify({"error": "Server not configured with OPENROUTER_API_KEY"}), 500

    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    try:
        resp = requests.post(CHAT_URL, headers=headers, json=payload, timeout=30)
        # Forward status code and content
        return (resp.content, resp.status_code, resp.headers.items())
    except requests.RequestException as e:
        logger.exception("Upstream request failed")
        return jsonify({"error": "Upstream request failed", "details": str(e)}), 502


# -------------------------
# Health and diagnostics
# -------------------------
@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok", "time": now_ts()})


@app.route("/diagnostics", methods=["GET"])
def diagnostics():
    """Return minimal diagnostics useful for debugging deployments (no secrets)."""
    return jsonify({
        "app_root": str(APP_ROOT),
        "index_exists": (APP_ROOT / "index.html").exists(),
        "avatar_exists": (APP_ROOT / "avatar.png").exists(),
        "chats_file_exists": CHATS_FILE.exists(),
        "openrouter_configured": bool(OPENROUTER_API_KEY),
        "chat_url": CHAT_URL
    })


# -------------------------
# Error handlers
# -------------------------
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(e):
    logger.exception("Internal server error")
    return jsonify({"error": "Internal server error"}), 500


# -------------------------
# Entrypoint
# -------------------------
if __name__ == "__main__":
    # Ensure chats file exists
    _init_chats_file()

    # Print startup diagnostics
    logger.info("Starting ChatBot.py")
    logger.info("APP_ROOT: %s", APP_ROOT)
    logger.info("index.html exists: %s", (APP_ROOT / "index.html").exists())
    logger.info("avatar.png exists: %s", (APP_ROOT / "avatar.png").exists())
    logger.info("chats.json exists: %s", CHATS_FILE.exists())
    logger.info("openrouter configured: %s", bool(OPENROUTER_API_KEY))
    port = int(os.getenv("PORT", 5000))
    # In production, use a WSGI server (gunicorn) instead of Flask's dev server.
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
