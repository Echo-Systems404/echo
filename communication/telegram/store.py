"""Telegram's private data, all in communication/telegram/data/ (git-ignored):

  token              the bot token (readable only by the user's account)
  settings.json      on/off, the paired account, and the model settings
  state.json         where polling left off, pushed reminders, last status
  current.json       the conversation in progress (survives a runner restart)
  conversations.jsonl  one line per finished conversation: its messages
"""

import json
import os
import time

import config


def data_dir():
    return config.COMMUNICATION_DIR / "telegram" / "data"


def _read(name, default):
    try:
        return json.loads((data_dir() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(name, data):
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    temporary = folder / f"{name}.tmp"
    temporary.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, folder / name)


# -- settings and token ------------------------------------------------------------

DEFAULTS = {"enabled": False, "user_id": None, "user_name": "", "bot_name": "",
            "driver": None, "model": None, "num_ctx": None, "idle_minutes": None}


def settings():
    saved = _read("settings.json", {})
    current = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}
    current["idle_minutes"] = current["idle_minutes"] or config.TELEGRAM_IDLE_MINUTES
    return current


def save_settings(**changes):
    _write("settings.json", {**settings(), **changes})


def token():
    try:
        return (data_dir() / "token").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def save_token(value):
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    path = folder / "token"
    path.write_text(value.strip(), encoding="utf-8")
    os.chmod(path, 0o600)


def ready():
    """Turned on, with a token and a paired account."""
    current = settings()
    return bool(current["enabled"] and current["user_id"] and token())


# -- polling state -----------------------------------------------------------------

def state():
    return {"offset": 0, "pushed": [], "status": "", "ignored": 0, **_read("state.json", {})}


def save_state(**changes):
    _write("state.json", {**state(), **changes})


# -- conversations -------------------------------------------------------------------

def current():
    return _read("current.json", None)


def save_current(conversation):
    _write("current.json", conversation)


def clear_current():
    try:
        (data_dir() / "current.json").unlink()
    except OSError:
        pass


def log_conversation(conversation, reason, now=None):
    """Append a finished conversation to conversations.jsonl."""
    if not conversation or not conversation.get("messages"):
        return
    entry = {**conversation, "ended": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now or time.time())),
             "reason": reason}
    entry.pop("last", None)
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "conversations.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    os.chmod(path, 0o600)


def conversations():
    try:
        lines = (data_dir() / "conversations.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return found


# -- Echo's recall tools --------------------------------------------------------------

def _first(conversation):
    for message in conversation.get("messages", []):
        if message["role"] == "user":
            text = " ".join(message["text"].split())
            return text if len(text) <= 80 else text[:77] + "..."
    return ""


def recall_list(query=""):
    """Past Telegram conversations, newest first; with a query, only those
    whose messages contain every word of it."""
    items = conversations()
    words = [w.lower() for w in str(query or "").split()]
    if words:
        items = [c for c in items
                 if all(w in " ".join(m["text"] for m in c.get("messages", [])).lower() for w in words)]
    if not items:
        return (f"No Telegram conversations match '{query}'." if words else "No Telegram conversations yet.")
    lines = [f"Telegram conversations ({len(items)}{' matching' if words else ''}), newest first:"]
    for c in reversed(items[-30:]):
        lines.append(f"- {c['id']}: {c.get('started', '?')} to {c.get('ended', '?')[11:16]}, "
                     f"{len(c.get('messages', []))} messages. Starts: \"{_first(c)}\"")
    if len(items) > 30:
        lines.append(f"({len(items) - 30} older not shown; search with a query to narrow it.)")
    return "\n".join(lines)


def recall_read(id=""):
    """One past Telegram conversation, as text."""
    items = conversations()
    if id in ("", "last", "latest") and items:
        found = items[-1]
    else:
        found = next((c for c in items if c.get("id") == id), None)
    if found is None:
        return f"Error: no Telegram conversation '{id}'. Use telegram_conversations to list them."
    name = config.USER_NAME
    lines = [f"Telegram conversation {found['id']}, {found.get('started', '?')} to {found.get('ended', '?')} "
             f"(ended: {found.get('reason', '?')}). Model: {found.get('model', '?')}."]
    for message in found.get("messages", []):
        who = name if message["role"] == "user" else config.AGENT_NAME
        lines.append(f"\n[{message.get('time', '')[11:16]}] {who}: {message['text']}")
    text = "\n".join(lines)
    limit = config.TELEGRAM_RECALL_MAX_CHARS
    return text if len(text) <= limit else text[:limit] + "\n... (the rest is cut; it was long)"
