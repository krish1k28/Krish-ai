import os
import json
import time
import requests
from flask import Flask, render_template, request, jsonify, send_from_directory
from dotenv import load_dotenv
from pathlib import Path
from threading import Lock

# Load environment variables from .env
load_dotenv()

APP_ROOT = Path(__file__).parent
CHATS_FILE = APP_ROOT / "chats.json"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

# Thread-safe file access
file_lock = Lock()

app = Flask(__name__, template_folder="templates")


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
# Static asset route
# -------------------------
@app.route("/avatar.png")
def avatar():
    return send_from_directory("templates", "avatar.png")


# -------------------------
# Frontend
# -------------------------
@app.route("/")
def index():
    return render_template("index.html")


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


@app.route("/api/chats/<chat_id>/message", methods=["POST"])
def api_post_message(chat_id):
    """
    Accepts JSON:
      { "message": "<text>" }
    Calls OpenRouter and appends both user and assistant messages to the chat.
    """
    payload = request.get_json(force=True)
    user_message = (payload.get("message") or "").strip()
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

    # Call OpenRouter
    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        }
        body = {
            "model": "openrouter/free",
            "messages": [
                {"role": "system", "content": "You are Krish, an AI Coding Assistant developed by Devansh Nayak."},
                # include recent chat context (last N messages) to keep responses coherent
            ],
            "max_tokens": 800,
            "temperature": 0.2
        }

        # include last up to 10 messages from this chat for context
        recent = chat.get("messages", [])[-10:]
        for m in recent:
            role = m.get("role", "user")
            content = m.get("content", "")
            body["messages"].append({"role": role, "content": content})

        resp = requests.post(CHAT_URL, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        resp_json = resp.json()

        # Defensive parsing for OpenRouter response shapes
        assistant_text = ""
        if isinstance(resp_json, dict):
            choices = resp_json.get("choices") or []
            if choices:
                first = choices[0]
                # common shape: choices[0].message.content
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
        # network error: keep user message saved, return error
        return jsonify({"error": f"Network error: {str(e)}"}), 502
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)}"}), 500


# -------------------------
# Optional: save tab metadata with a chat (client may call when creating a chat)
# -------------------------
@app.route("/api/chats/<chat_id>/metadata", methods=["POST"])
def api_save_metadata(chat_id):
    payload = request.get_json(force=True)
    metadata = payload.get("metadata", {})
    data = load_chats()
    chat = find_chat(data, chat_id)
    if not chat:
        return jsonify({"error": "Chat not found"}), 404
    chat["metadata"] = metadata
    save_chats(data)
    return jsonify({"ok": True})


# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    # Ensure chats file exists
    _init_chats_file()
    app.run(host="127.0.0.1", port=5000, debug=True)
