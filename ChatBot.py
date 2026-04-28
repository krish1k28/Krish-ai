import os
import json
import time
import requests
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
from pathlib import Path
from threading import Lock

# Load environment variables from .env
load_dotenv()

APP_ROOT = Path(__file__).parent.resolve()
CHATS_FILE = APP_ROOT / "chats.json"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

# Thread-safe file access
file_lock = Lock()

# Create Flask app (no templates folder for index)
app = Flask(__name__, static_folder="static")


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


def load_chats():
    """Load chats.json and return dict."""
    _init_chats_file()
    with file_lock:
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
# Static asset routes (served from project root)
# -------------------------
@app.route("/avatar.png")
def avatar():
    # Serve avatar.png from project root
    return send_from_directory(str(APP_ROOT), "avatar.png")


@app.route("/<path:filename>")
def root_static(filename):
    """
    Optional helper to serve other root-level static files if needed.
    Be careful: this will expose any file in the project root by path.
    Use only if you intend to serve specific files from root.
    """
    # Only allow a small whitelist if you want to be safer; here we serve requested filename.
    return send_from_directory(str(APP_ROOT), filename)


# -------------------------
# Frontend
# -------------------------
@app.route("/")
def index():
    # Serve index.html from project root
    return send_from_directory(str(APP_ROOT), "index.html")


# -------------------------
# Chat management endpoints
# -------------------------
@app.route("/api/chats", methods=["GET"])
def api_list_chats():
    data = load_chats()
    # return minimal chat list (id, name)
    chats = [{"id": c["id"], "name": c["name"]} for c in data.get("chats", [])]
    return jsonify({"chats": chats})


@app.route("/api/chats/create", methods=["POST"])
def api_create_chat():
    payload = request.get_json(force=True)
    name = (payload.get("name") or "").strip()
    metadata = payload.get("metadata") or {}
    if not name:
        return jsonify({"error": "Chat name required"}), 400

    data = load_chats()
    # generate simple unique id
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
    payload = request.get_json(force=True)
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


# -------------------------
# Example: proxying chat completions (kept as-is; implement as needed)
# -------------------------
@app.route("/api/chat", methods=["POST"])
def api_chat():
    """
    Example endpoint that proxies a request to OPENROUTER_API_KEY.
    Adjust payload handling and streaming as required by your frontend.
    """
    payload = request.get_json(force=True)
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    try:
        resp = requests.post(CHAT_URL, headers=headers, json=payload, timeout=30)
        return (resp.content, resp.status_code, resp.headers.items())
    except requests.RequestException as e:
        return jsonify({"error": "Upstream request failed", "details": str(e)}), 502


if __name__ == "__main__":
    # Run in debug mode for development; remove debug=True in production
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
