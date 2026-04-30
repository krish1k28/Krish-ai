#!/usr/bin/env python3
"""
ChatBot.py

Flask backend for the Krish AI frontend.

Features:
- Serves index.html and root-level static assets from the project root.
- If local index.html/avatar.png are missing, proxies them from a remote frontend (FRONTEND_REMOTE)
  and injects a small script that sets window.BACKEND_URL to the backend origin so the frontend will call this backend
  without editing the HTML file.
- Simple chat persistence in chats.json with a default "general" chat.
- Two message endpoints:
    - POST /api/chats/<chat_id>/messages  (store-only, plural)
    - POST /api/chats/<chat_id>/message   (backwards-compatible, calls upstream model and returns {"reply": "..."})
- Generic proxy endpoint /api/chat to forward arbitrary completion requests to the configured CHAT_URL.
- Improved error handling for upstream responses (including 401 Unauthorized).
- CORS support via ALLOWED_ORIGINS environment variable.
- Diagnostics endpoint at /diagnostics.

Usage:
- Put this file in your project root (next to index.html and avatar.png if you have them).
- Create a .env with at least OPENROUTER_API_KEY (or leave empty to test without model calls).
- Install dependencies: pip install flask requests python-dotenv flask-cors
- Run: python ChatBot.py
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
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
CHAT_URL = os.getenv("CHAT_URL", "https://openrouter.ai/api/v1/chat/completions").strip()

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


@app.route("/favicon.ico")
def favicon():
    """Serve favicon if present, else proxy remote or 404."""
    fav = APP_ROOT / "favicon.ico"
    if fav.exists():
        return send_from_directory(str(APP_ROOT), "favicon.ico")
    status, content, ctype = fetch_remote_bytes("favicon.ico")
    if status == 200:
        return Response(content, content_type=ctype)
    abort(404)


@app.route("/<path:filename>")
def root_static(filename):
    """
    Serve other root-level files (useful for robots.txt, styles.css, app.js).
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
    injection = f'<script>window.BACKEND_URL = {json.dumps(backend_origin)};</script>'

    # Insert injection before closing </head> if present, else before <body>, else prepend.
    lower = text.lower()
    if "</head>" in lower:
        idx = lower.rfind("</head>")
        new_text = text[:idx] + injection + text[idx:]
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
# Messages endpoints (plural) - stores messages only
# -------------------------
@app.route("/api/chats/<chat_id>/messages", methods=["GET"])
def api_get_messages(chat_id):
    data = load_chats()
    chat = find_chat(data, chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404
    return jsonify({"messages": chat.get("messages", [])})


@app.route("/api/chats/<chat_id>/messages", methods=["POST"])
def api_post_messages_store(chat_id):
    """
    Append a message to the chat (store only). This endpoint does not call the model.
    Expects JSON: { "role": "user"|"assistant", "content": "..." }
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
# Backwards-compatible singular message endpoint that also calls the model
# -------------------------
@app.route("/api/chats/<chat_id>/message", methods=["POST"])
def api_post_message(chat_id):
    """
    Accepts JSON:
      { "message": "<text>" } or { "content": "<text>" }
    Appends the user message locally, calls the upstream model, appends assistant reply, and returns {"reply": "..."}.
    """
    payload = request.get_json(force=True, silent=True) or {}
    user_message = (payload.get("message") or payload.get("content") or "").strip()
    if not user_message:
        return jsonify({"error": "Empty message"}), 400

    data = load_chats()
    chat = find_chat(data, chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404

    # Append user message locally first
    user_entry = {"role": "user", "content": user_message, "timestamp": now_ts()}
    chat.setdefault("messages", []).append(user_entry)
    save_chats(data)

    # If no API key configured, return a clear error
    if not OPENROUTER_API_KEY:
        logger.warning("Model call attempted but OPENROUTER_API_KEY is not set")
        return jsonify({"error": "Server not configured with OPENROUTER_API_KEY"}), 500

    # Call upstream model (OpenRouter)
    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        }
        body = {
            "model": "openrouter/free",
            "messages": [
                {"role": "system", "content": "You are Krish, an AI Coding Assistant developed by Devansh Nayak."},
            ],
            "max_tokens": 800,
            "temperature": 0.2
        }

        # include last up to 10 messages for context
        recent = chat.get("messages", [])[-10:]
        for m in recent:
            role = m.get("role", "user")
            content = m.get("content", "")
            body["messages"].append({"role": role, "content": content})

        resp = requests.post(CHAT_URL, headers=headers, json=body, timeout=30)

        # Handle common upstream HTTP errors explicitly
        if resp.status_code == 401:
            logger.warning("Upstream returned 401 Unauthorized. Check OPENROUTER_API_KEY.")
            return jsonify({"error": "Upstream unauthorized. Check OPENROUTER_API_KEY."}), 502
        if resp.status_code >= 400:
            # return upstream body for debugging but avoid leaking secrets
            logger.error("Upstream returned error %s: %s", resp.status_code, resp.text[:1000])
            return jsonify({"error": f"Upstream error {resp.status_code}", "details": resp.text[:1000]}), 502

        resp_json = resp.json()

        # Defensive parsing for OpenRouter response shapes
        assistant_text = ""
        if isinstance(resp_json, dict):
            choices = resp_json.get("choices") or []
            if choices:
                first = choices[0]
                message = first.get("message") or {}
                assistant_text = message.get("content") or first.get("text") or ""
        if not assistant_text:
            assistant_text = resp_json.get("text") or "Sorry, I couldn't get a response from the model."

        # Append assistant message and save
        assistant_entry = {"role": "assistant", "content": assistant_text, "timestamp": now_ts()}
        chat.setdefault("messages", []).append(assistant_entry)
        save_chats(data)

        return jsonify({"reply": assistant_text})
    except requests.exceptions.RequestException as e:
        logger.exception("Network error when calling upstream model")
        return jsonify({"error": f"Network error: {str(e)}"}), 502
    except Exception as e:
        logger.exception("Model call failed")
        return jsonify({"error": f"Server error: {str(e)}"}), 500


# -------------------------
# Proxy to upstream chat completion API (generic)
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
        logger.warning("api_chat called but OPENROUTER_API_KEY not set")
        return jsonify({"error": "Server not configured with OPENROUTER_API_KEY"}), 500

    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    try:
        resp = requests.post(CHAT_URL, headers=headers, json=payload, timeout=30)
        if resp.status_code == 401:
            logger.warning("Upstream returned 401 Unauthorized for /api/chat")
            return jsonify({"error": "Upstream unauthorized. Check OPENROUTER_API_KEY."}), 502
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
    """
    Diagnostics returns non-sensitive info about the server state.
    Note: OPENROUTER_API_KEY is not printed for security reasons.
    """
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
    os.environ.setdefault("BACKEND_ORIGIN", f"http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
