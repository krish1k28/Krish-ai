#!/usr/bin/env python3
"""
ChatBot.py

Flask backend that:
- Serves index.html and root-level static assets from the project root.
- If local index.html/avatar.png are missing, proxies them from a remote frontend (e.g., GitHub Pages)
  and injects a small script that sets window.BACKEND_URL to the backend origin so the frontend will call this backend
  without editing the HTML file in the repo.
- Provides simple chat persistence (chats.json).
- Proxies /api/chat to OpenRouter using OPENROUTER_API_KEY.
- Enables CORS so a static site (e.g., GitHub Pages) can call this backend.
Place this file in the project root alongside index.html and avatar.png (optional).
"""
import os
import json
import time
import logging
from pathlib import Path
from threading import Lock

import requests
from flask import Flask, request, jsonify, send_from_directory, abort, Response
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

# Remote frontend (used only if local index.html/avatar.png are missing)
FRONTEND_REMOTE = os.getenv("FRONTEND_REMOTE", "https://krish1k28.github.io/Krish-ai").rstrip("/")

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
# Helper: fetch remote frontend content (used only when local files missing)
# -------------------------
def fetch_remote_text(path="/"):
    """Fetch text content from FRONTEND_REMOTE + path. Returns (status_code, text, content_type)."""
    url = f"{FRONTEND_REMOTE.rstrip('/')}/{path.lstrip('/')}"
    try:
        r = requests.get(url, timeout=10)
        return r.status_code, r.text, r.headers.get("content-type", "text/html")
    except Exception as e:
        logger.exception("Failed to fetch remote frontend %s: %s", url, e)
        return 502, f"Error fetching remote frontend: {e}", "text/plain"


def fetch_remote_bytes(path="/"):
    """Fetch binary content from FRONTEND_REMOTE + path. Returns (status_code, bytes, content_type)."""
    url = f"{FRONTEND_REMOTE.rstrip('/')}/{path.lstrip('/')}"
    try:
        r = requests.get(url, timeout=10)
        return r.status_code, r.content, r.headers.get("content-type", "application/octet-stream")
    except Exception as e:
        logger.exception("Failed to fetch remote asset %s: %s", url, e)
        return 502, b"", "application/octet-stream"


# -------------------------
# Static asset routes (serve from project root or proxy from remote)
# -------------------------
@app.route("/avatar.png")
def avatar():
    """Serve avatar.png from project root or proxy from remote frontend if missing."""
    avatar_path = APP_ROOT / "avatar.png"
    if avatar_path.exists():
        return send_from_directory(str(APP_ROOT), "avatar.png")
    # proxy from remote
    status, content, ctype = fetch_remote_bytes("avatar.png")
    if status != 200:
        abort(404)
    return Response(content, content_type=ctype)


@app.route("/<path:filename>")
def root_static(filename):
    """
    Serve other root-level files (useful for robots.txt, favicon.ico, styles.css, app.js).
    If missing locally, attempt to proxy from FRONTEND_REMOTE.
    """
    target = APP_ROOT / filename
    if target.exists() and target.is_file():
        return send_from_directory(str(APP_ROOT), filename)

    # Try remote proxy
    status, content, ctype = fetch_remote_bytes(filename)
    if status == 200:
        return Response(content, content_type=ctype)
    abort(404)


# -------------------------
# Frontend: serve index.html from project root or proxy remote and inject BACKEND URL
# -------------------------
@app.route("/")
def index():
    """Serve index.html from project root. If missing, fetch from FRONTEND_REMOTE and inject backend URL script."""
    index_path = APP_ROOT / "index.html"
    if index_path.exists():
        return send_from_directory(str(APP_ROOT), "index.html")

    # Fetch remote index.html
    status, text, ctype = fetch_remote_text("/")
    if status != 200:
        logger.error("Failed to fetch remote index.html from %s (status %s)", FRONTEND_REMOTE, status)
        abort(502)

    # Inject a small script that sets window.BACKEND_URL to this server's origin.
    backend_origin = os.getenv("BACKEND_ORIGIN") or f"http://127.0.0.1:{os.getenv('PORT', '5000')}"
    # Use json.dumps to safely quote/escape the string for JS
    injection = f'<script>window.BACKEND_URL = {json.dumps(backend_origin)};</script>'

    # Insert injection before closing </head> if present, else before <body>, else prepend.
    lower = text.lower()
    if "</head>" in lower:
        idx = lower.rfind("</head>")
        # find the same index in original text by searching for the substring at that position
        # use lower to find position, then map to original
        pos = idx
        new_text = text[:pos] + injection + text[pos:]
    elif "<body" in lower:
        idx = lower.find("<body")
        body_close = text.find(">", idx)
        if body_close != -1:
            new_text = text[:body_close+1] + injection + text[body_close+1:]
        else:
            new_text = injection + text
    else:
        new_text = injection + text

    return Response(new_text, content_type=ctype)


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
    return jsonify({
        "app_root": str(APP_ROOT),
        "index_exists": (APP_ROOT / "index.html").exists(),
        "avatar_exists": (APP_ROOT / "avatar.png").exists(),
        "chats_file_exists": CHATS_FILE.exists(),
        "openrouter_configured": bool(OPENROUTER_API_KEY),
        "chat_url": CHAT_URL,
        "frontend_remote": FRONTEND_REMOTE
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
    _init_chats_file()
    logger.info("Starting ChatBot.py")
    logger.info("APP_ROOT: %s", APP_ROOT)
    logger.info("index.html exists: %s", (APP_ROOT / "index.html").exists())
    logger.info("avatar.png exists: %s", (APP_ROOT / "avatar.png").exists())
    logger.info("chats.json exists: %s", CHATS_FILE.exists())
    logger.info("openrouter configured: %s", bool(OPENROUTER_API_KEY))
    logger.info("frontend remote: %s", FRONTEND_REMOTE)
    port = int(os.getenv("PORT", 5000))
    # BACKEND_ORIGIN used for injection if needed; default to local dev origin
    os.environ.setdefault("BACKEND_ORIGIN", f"http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
