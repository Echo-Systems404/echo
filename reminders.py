"""Reminders: things an agent should bring up the next time the user talks to Echo.

One JSON line each in reminders/reminders.jsonl, inside the harness, so only
the harness writes the file; agents use the remind tool. Tasks get it too:
a reminder is outgoing mail, not memory, so task memory stays read-only.

When chat starts, unseen reminders go into the system prompt (urgent ones are
brought up first). They're marked seen after Echo's first reply. Urgent ones
also show a macOS notification when they're written.
"""

import fcntl
import json
import os
import subprocess
import time
from contextlib import contextmanager

import config

NORMAL_LISTED = 10  # normal reminders listed in the prompt; the rest are counted


def _path():
    return config.REMINDERS_DIR / "reminders.jsonl"


@contextmanager
def _locked():
    """The runner and a chat window can both write reminders."""
    config.REMINDERS_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.REMINDERS_DIR / ".lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _load():
    items = []
    try:
        lines = _path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return items
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("text"):
            items.append(item)
    return items


def _save(items):
    temporary = _path().with_name("reminders.jsonl.tmp")
    temporary.write_text("".join(json.dumps(item) + "\n" for item in items), encoding="utf-8")
    os.replace(temporary, _path())


def _fresh(items, now):
    cutoff = now - config.REMINDER_KEEP_DAYS * 86400
    return [item for item in items if item.get("ts", 0) >= cutoff]


def notify(text):
    """A macOS notification, best effort."""
    try:
        subprocess.run(["osascript", "-e", "on run argv", "-e",
                        'display notification (item 1 of argv) with title "Echo" subtitle "Reminder"',
                        "-e", "end run", text[:200]],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def add(text, source, urgent=False, now=None):
    """Save a reminder. Returns what the agent is told."""
    now = now or time.time()
    text = str(text or "").strip()
    if not text:
        return "Error: a reminder needs text."
    if len(text) > config.REMINDER_MAX_CHARS:
        return f"Error: {len(text)} characters is over the {config.REMINDER_MAX_CHARS} limit. Keep it short."
    with _locked():
        items = _fresh(_load(), now)
        if any(item["text"] == text and not item.get("seen") for item in items):
            return f"That reminder is already waiting for {config.USER_NAME}."
        items.append({"id": f"{int(now * 1000):x}", "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
                      "ts": now, "from": source, "urgency": "urgent" if urgent else "normal",
                      "text": text, "seen": None})
        _save(items)
    if urgent:
        notify(text)
    return f"Reminder saved ({'urgent' if urgent else 'normal'}). It comes up the next time {config.USER_NAME} opens Echo."


def unseen(now=None):
    now = now or time.time()
    return [item for item in _fresh(_load(), now) if not item.get("seen")]


def mark_seen(ids, now=None):
    if not ids:
        return
    now = now or time.time()
    with _locked():
        items = _fresh(_load(), now)
        for item in items:
            if item.get("id") in ids and not item.get("seen"):
                item["seen"] = now
        _save(items)


def prune(now=None):
    now = now or time.time()
    with _locked():
        items = _load()
        kept = _fresh(items, now)
        if len(kept) != len(items):
            _save(kept)


def _line(item):
    return f"- [{item.get('time', '?')}, from {item.get('from', '?')}] {item['text']}"


def prompt_section(items):
    """The system prompt's note about unseen reminders, or ''."""
    urgent = [i for i in items if i.get("urgency") == "urgent"]
    normal = [i for i in items if i.get("urgency") != "urgent"]
    if not items:
        return ""
    parts = ["# Reminders waiting for the user",
             "Left by you or your scheduled tasks since you last talked. Bring up the urgent ones briefly at the "
             "start of your next reply, before anything else. Mention normal ones only when they're relevant or "
             "the user asks about reminders."]
    if urgent:
        parts.append("Urgent:\n" + "\n".join(_line(i) for i in urgent))
    if normal:
        listed = "\n".join(_line(i) for i in normal[-NORMAL_LISTED:])
        more = len(normal) - NORMAL_LISTED
        parts.append("Normal:\n" + listed + (f"\n(and {more} older; the reminders tool lists them)" if more > 0 else ""))
    return "\n".join(parts)


def summary(items):
    """'3 reminders waiting (1 urgent)' for the banner, or ''."""
    if not items:
        return ""
    urgent = sum(1 for i in items if i.get("urgency") == "urgent")
    text = f"{len(items)} reminder{'s' if len(items) != 1 else ''} waiting"
    return text + (f" ({urgent} urgent)" if urgent else "")


def recent(limit=20):
    """The reminders tool: the latest ones, seen or not."""
    items = _fresh(_load(), time.time())[-limit:]
    if not items:
        return "No reminders."
    return "\n".join(f"{_line(i)} ({i.get('urgency', 'normal')}, {'seen' if i.get('seen') else 'unseen'})"
                     for i in reversed(items))


def chat_remind(text, urgent=False):
    return add(text, "chat", bool(urgent))


REMIND_DESCRIPTION = (f"Leave {config.USER_NAME} a reminder you'll bring up the next time you talk: something {config.USER_NAME} should know "
                      "or decide. urgent: true only if it can't wait (also shows a notification). Keep it to a sentence "
                      "or two.")
REMIND_PARAMETERS = {
    "text": {"type": "string", "description": "The reminder."},
    "urgent": {"type": "boolean", "description": "True only if it can't wait."},
}


def task_tool(source):
    """The remind tool for one task run, capped at REMINDERS_PER_RUN."""
    count = [0]

    def remind(text="", urgent=False):
        if count[0] >= config.REMINDERS_PER_RUN:
            return f"Error: this run already left {config.REMINDERS_PER_RUN} reminders. Put the rest in your report."
        result = add(text, source, bool(urgent))
        if result.startswith("Reminder saved"):
            count[0] += 1
        return result

    return {"function": remind,
            "schema": {"type": "function", "function": {
                "name": "remind", "description": REMIND_DESCRIPTION,
                "parameters": {"type": "object", "properties": REMIND_PARAMETERS, "required": ["text"]}}}}
