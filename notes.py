"""The agent's two note layers, kept in config.NOTES_DIR.

Ephemeral: one summary per saved conversation; the newest N are kept (set in
/options → Memory layers). Saving again in the same conversation updates its
entry. The model reads these only when the conversation calls for it.

Transient: a notebook of titled entries, one per project or topic, that the
model edits freely. Each entry starts with a "## <title>" line; the harness
keeps that structure, so new information lands in the entry it belongs to.
"""

import os
import re
import time

import config
import memory_settings

_ID = re.compile(r"^\d{8}-\d{6}(-\d+)?$")

conversation_id = None


def new_conversation():
    """Start a new ephemeral slot (at launch and after /clear)."""
    global conversation_id
    conversation_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"


new_conversation()


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)  # a crash never leaves a half-written note


# -- ephemeral -----------------------------------------------------------

def _ephemeral_dir():
    return config.NOTES_DIR / "ephemeral"


def _saved():
    """Saved conversations, most recently updated first."""
    folder = _ephemeral_dir()
    paths = folder.glob("*.md") if folder.exists() else []
    return sorted(paths, key=lambda p: p.stat().st_mtime_ns, reverse=True)


def ephemeral_save(title, summary):
    """Save or update this conversation's summary."""
    title, summary = " ".join(title.split()), summary.strip()
    if not title or not summary:
        return "Error: both a title and a summary are needed."
    limit = memory_settings.get("ephemeral_chars")
    if len(summary) > limit:
        return (f"Error: {len(summary)} characters is over the {limit} limit; "
                "tighten the summary and keep the important nuance.")
    path = _ephemeral_dir() / f"{conversation_id}.md"
    existed = path.exists()
    _write(path, f"# {title}\nSaved: {time.strftime('%Y-%m-%d %H:%M')}\n\n{summary}\n")
    for old in _saved()[memory_settings.get("ephemeral_keep"):]:
        old.unlink()
    return f"{'Updated' if existed else 'Saved'} this conversation as '{title}'."


def ephemeral_list():
    """List saved conversations, newest first."""
    lines = []
    for path in _saved():
        head = path.read_text(encoding="utf-8").splitlines()
        title = head[0].lstrip("# ") if head else "(untitled)"
        saved = head[1].replace("Saved: ", "") if len(head) > 1 else ""
        lines.append(f"- {path.stem} · {saved} · {title}")
    return "\n".join(lines) or "(no saved conversations yet)"


def ephemeral_read(id):
    """Read one saved conversation."""
    if not _ID.match(id or ""):
        return "Error: use an id from ephemeral_list."
    path = _ephemeral_dir() / f"{id}.md"
    if not path.exists():
        return f"Error: no saved conversation '{id}'. Use ephemeral_list."
    return path.read_text(encoding="utf-8")


def ephemeral_current():
    """This conversation's saved summary, or ""."""
    path = _ephemeral_dir() / f"{conversation_id}.md"
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def grounding():
    """What the model is given right after a memory layer update clears its
    context: the same conversation, continued from its memory layers."""
    parts = ["# Before the context flush",
             "Your context was cleared after a memory layer update. This is the same conversation, "
             "continued: your memory layers hold what came before. Your seed, the vault, and past "
             "conversations are there if you need more."]
    summary = ephemeral_current()
    parts += ["## Your summary of this conversation (ephemeral memory)", summary or "(none was saved)"]
    transient = transient_text() or "(empty)"
    parts += ["## Your transient notes", transient]
    return "\n\n".join(parts)


# -- transient -----------------------------------------------------------

_HEADING = re.compile(r"^## (.+?)\s*$", re.M)


def _transient_file():
    return config.NOTES_DIR / "transient.md"


def _entries():
    """[(title, body)] from the notebook. An old notebook (entries divided by
    ---) is converted once; the original is kept as transient.md.bak."""
    path = _transient_file()
    text = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    if not text:
        return []
    if not text.startswith("## "):
        entries = _convert(text)
        backup = path.with_name("transient.md.bak")
        if not backup.exists():
            _write(backup, text + "\n")
        _save_entries(entries)
        return entries
    parts = _HEADING.split(text)  # ["", title, body, title, body, ...]
    return [(parts[i].strip(), parts[i + 1].strip()) for i in range(1, len(parts) - 1, 2)]


def _convert(text):
    entries, used = [], set()
    for number, block in enumerate(re.split(r"(?m)^---\s*$", text), 1):
        block = block.strip()
        if not block:
            continue
        first, _, rest = block.partition("\n")
        title = " ".join(first.lstrip("#").split())[:memory_settings.SECTION_MAX] or f"Notes {number}"
        body = rest.strip() if first.startswith("#") else block
        title = _unique(title, used)
        used.add(title.lower())
        entries.append((title, _demote(body)))
    return entries


def _unique(title, used):
    name, count = title, 2
    while name.lower() in used:
        name, count = f"{title} ({count})", count + 1
    return name


def _demote(body):
    """Headings inside an entry go one level down, so "## " only ever starts an entry."""
    return re.sub(r"(?m)^(#{1,2}) ", "### ", body.strip())


def _render(entries):
    return "\n\n".join(f"## {title}\n{body}".rstrip() for title, body in entries)


def _save_entries(entries):
    _write(_transient_file(), (_render(entries) + "\n") if entries else "")


def _find(entries, title):
    wanted = " ".join(str(title or "").split()).lower()
    return next((i for i, (name, _) in enumerate(entries) if name.lower() == wanted), None)


def _titles(entries):
    return ", ".join(f"'{name}'" for name, _ in entries) or "(none yet)"


def transient_text():
    """The whole notebook (for grounding after a context flush)."""
    return _render(_entries())


def transient_read(title=""):
    """No title: the list of entries, plus all of them when they fit. A title: that entry."""
    entries = _entries()
    if not entries:
        return "(your transient notes are empty)"
    if title:
        found = _find(entries, title)
        if found is None:
            return f"Error: no entry '{title}'. Your entries: {_titles(entries)}."
        name, body = entries[found]
        return f"## {name}\n{body}"
    settings = memory_settings.load()
    total = sum(len(body) for _, body in entries)
    index = [f"Transient notes: {len(entries)} of {settings['transient_entries']} entries, "
             f"{total} of {settings['transient_total_chars']} characters."]
    index += [f"- {name} ({len(body)} characters)" for name, body in entries]
    whole = _render(entries)
    if len(whole) + 400 <= config.MAX_TOOL_OUTPUT_CHARS:
        return "\n".join(index) + "\n\n" + whole
    return "\n".join(index) + "\n\nRead one entry with transient_read(title)."


def transient_edit(action, title="", content=""):
    """new: a new titled entry. append: add to an entry. replace: rewrite an
    entry. delete: remove an entry. wipe: empty the notebook."""
    settings = memory_settings.load()
    entries = _entries()
    if action == "wipe":
        _save_entries([])
        return "Transient notes wiped."
    if action not in ("new", "append", "replace", "delete"):
        return "Error: action must be new, append, replace, delete or wipe."
    title = " ".join(str(title or "").split())
    if not title:
        return f"Error: '{action}' needs the entry's title. Your entries: {_titles(entries)}."
    found = _find(entries, title)
    content = _demote(str(content or ""))
    if action == "delete":
        if found is None:
            return f"Error: no entry '{title}'. Your entries: {_titles(entries)}."
        del entries[found]
        _save_entries(entries)
        return f"Deleted the entry '{title}'."
    if not content:
        return f"Error: '{action}' needs content."
    if action == "new":
        if found is not None:
            return f"Error: '{entries[found][0]}' already exists. Use append to add to it, or replace."
        if len(title) > memory_settings.SECTION_MAX:
            return f"Error: keep titles under {memory_settings.SECTION_MAX} characters."
        allowed = settings["transient_sections"]
        if allowed and title.lower() not in {s.lower() for s in allowed}:
            return f"Error: the user set the allowed entries: {', '.join(allowed)}. Use one of those titles."
        if len(entries) >= settings["transient_entries"]:
            return (f"Error: you have {len(entries)} entries, the most allowed. Append to the entry this "
                    f"belongs to, or merge or delete one (the oldest is '{entries[0][0]}'). "
                    f"Your entries: {_titles(entries)}.")
        entries.append((title, content))
        index = len(entries) - 1
    else:
        if found is None:
            return (f"Error: no entry '{title}'. Your entries: {_titles(entries)}. "
                    "Append to the one this belongs to, or use new.")
        name, body = entries[found]
        entries[found] = (name, body + "\n\n" + content if action == "append" else content)
        index = found
    size = len(entries[index][1])
    if size > settings["transient_entry_chars"]:
        return (f"Error: '{entries[index][0]}' would be {size} characters, over the "
                f"{settings['transient_entry_chars']} per-entry limit. Nothing was changed; tighten it with replace.")
    total = sum(len(body) for _, body in entries)
    if total > settings["transient_total_chars"]:
        return (f"Error: the notebook would be {total} characters, over the {settings['transient_total_chars']} "
                "limit. Nothing was changed; trim or delete an entry first.")
    _save_entries(entries)
    verb = {"new": "Added", "append": "Appended to", "replace": "Replaced"}[action]
    return f"{verb} '{entries[index][0]}' ({size} characters; {len(entries)} entries, {total} in all)."
