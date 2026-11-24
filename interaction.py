"""Interaction seeds: one cue note per conversation, split into topics.

short term -> long term -> memory vault seeds (a seed plus an Obsidian note)

The harness creates, moves, and deletes seeds; the model only edits what is
inside them. That keeps the records consistent: a vault note never points at a
seed the model deleted. Nothing is ever merged.

- Every chat conversation gets a short-term seed when it starts.
- References count per topic: recalling a topic from another conversation.
  Once a topic has PROMOTE_AFTER_REFS of them, the harness copies it into a new
  long-term seed and leaves a pointer behind, so seeds distill themselves.
- Short-term seeds expire SHORT_TERM_DAYS after their last edit or reference.
  Long-term seeds expire LONG_TERM_DAYS after their last edit, reference, or
  consolidation review, and are deleted only once a consolidation task has run
  since they expired. Vault seeds never expire and only change in chat.

The seed files hold the text; .state.json holds the facts (references, expiry).
"""

import json
import os
import re
import time

import config
import notes
import vault

TIERS = {"short": "short term", "long": "long term", "vault": "memory vault seeds"}
ORDER = {"vault": 0, "long": 1, "short": 2}
DAY = 86400
MAX_LISTED = 20
EMPTY_GRACE = 3600  # an untouched seed from another conversation is removed after this
_TOPIC = re.compile(r"^## (t\d+) · (.*)$")
_snapshots = {}  # conversation id -> {"summary", "user"}, fixed for the prompt cache


# -- files and state -------------------------------------------------------

def _state_file():
    return config.INTERACTION_DIR / ".state.json"


def _path(seed_id, tier):
    return config.INTERACTION_DIR / TIERS[tier] / f"{seed_id}.md"


def _life(tier):
    return (config.SHORT_TERM_DAYS if tier == "short" else config.LONG_TERM_DAYS) * DAY


def _parse(text):
    """(title, intro, [[id, title, body], ...]) from a seed file."""
    title, intro, topics = "", [], []
    for line in text.splitlines():
        match = _TOPIC.match(line)
        if match:
            topics.append([match.group(1), match.group(2).strip(), []])
        elif topics:
            topics[-1][2].append(line)
        elif not title and line.startswith("# "):
            title = line[2:].strip()
        else:
            intro.append(line)
    return title, "\n".join(intro).strip(), [[i, t, "\n".join(b).strip()] for i, t, b in topics]


def _render(title, intro, topics):
    parts = [f"# {title}"] + ([intro] if intro else [])
    parts += [f"## {tid} · {name}\n{body}".rstrip() for tid, name, body in topics]
    return "\n\n".join(parts) + "\n"


def _read(seed_id, tier):
    return _parse(_path(seed_id, tier).read_text(encoding="utf-8", errors="replace"))


def _write(seed_id, tier, title, intro, topics, limit=True):
    text = _render(title, intro, topics)
    if limit and len(text) > config.INTERACTION_MAX_CHARS:
        raise ValueError(f"the seed would be {len(text)} characters, over the {config.INTERACTION_MAX_CHARS} "
                         "limit. Keep cues, not the whole story: prune or rewrite a topic.")
    notes._write(_path(seed_id, tier), text)


def _load():
    """The state, matched to the seed files that actually exist."""
    try:
        state = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state.setdefault("seeds", {})
    state.setdefault("last_consolidation", None)
    seeds, now = state["seeds"], time.time()
    for seed_id in [s for s, e in seeds.items() if not _path(s, e["tier"]).exists()]:
        del seeds[seed_id]
    for tier, folder in TIERS.items():
        directory = config.INTERACTION_DIR / folder
        for path in sorted(directory.glob("*.md")) if directory.exists() else []:
            if path.stem not in seeds:  # a seed added by hand
                seeds[path.stem] = {"tier": tier, "created": now, "updated": now, "conversation": None,
                                    "topics": {}, "expires": None if tier == "vault" else now + _life(tier)}
    for seed_id, entry in seeds.items():  # topics added to a file by hand
        for tid, _, _ in _read(seed_id, entry["tier"])[2]:
            entry["topics"].setdefault(tid, {"refs": [], "moved": None})
    return state


def _save(state):
    config.INTERACTION_DIR.mkdir(parents=True, exist_ok=True)
    temporary = _state_file().with_name(".state.tmp")
    temporary.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(temporary, _state_file())


def _new_id(state, name, now):
    base = (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-") or "seed")
    base += time.strftime("-%Y-%m-%d", time.localtime(now))
    seed_id, number = base, 2
    while seed_id in state["seeds"] or any(_path(seed_id, t).exists() for t in TIERS):
        seed_id, number = f"{base}-{number}", number + 1
    return seed_id


def _touch(entry, now):
    entry["updated"] = now
    if entry["tier"] != "vault":
        entry["expires"] = now + _life(entry["tier"])


# -- life cycle (harness only) ---------------------------------------------

def _create(state, seed_id, now):
    title = time.strftime("Conversation %Y-%m-%d %H:%M", time.localtime(now))
    _write(seed_id, "short", title, "", [])
    state["seeds"][seed_id] = {"tier": "short", "created": now, "updated": now, "conversation": seed_id,
                               "topics": {}, "expires": now + _life("short")}


def maintain(state=None, now=None):
    """Delete what has run out. A long-term seed is only deleted once a
    consolidation task has run after it expired."""
    now = now or time.time()
    own = state is None
    state = _load() if own else state
    seeds, consolidated = state["seeds"], state["last_consolidation"] or 0
    for seed_id, entry in list(seeds.items()):
        tier = entry["tier"]
        if tier == "vault":
            continue
        empty = not entry["topics"] and not _read(seed_id, tier)[2]
        stale = (tier == "short" and empty and seed_id != notes.conversation_id
                 and now - entry["created"] >= EMPTY_GRACE)
        expired = entry["expires"] is not None and now >= entry["expires"]
        if stale or (tier == "short" and expired) or (tier == "long" and expired and consolidated > entry["expires"]):
            _path(seed_id, tier).unlink(missing_ok=True)
            del seeds[seed_id]
    if own:
        _save(state)
    return state


def session_start(now=None):
    """A chat conversation begins (at launch, /clear, or after a flush with a
    new conversation): clean up, then give this conversation its seed."""
    now = now or time.time()
    state = maintain(_load(), now)
    if notes.conversation_id not in state["seeds"]:
        _create(state, notes.conversation_id, now)
    _save(state)
    _snapshots.pop(notes.conversation_id, None)
    snapshot()


def _ensure_current(state, now):
    """The harness recreates this conversation's seed if it went missing."""
    if notes.conversation_id not in state["seeds"]:
        _create(state, notes.conversation_id, now)


def mark_consolidated(now=None):
    """A consolidation task finished: expired long-term seeds may go now."""
    state = _load()
    state["last_consolidation"] = now or time.time()
    maintain(state, now)
    _save(state)


def _summary(state):
    seeds = state["seeds"].values()
    count = lambda tier: sum(e["tier"] == tier for e in seeds)
    return f"{count('short')} short term, {count('long')} long term, {count('vault')} in the vault."


def snapshot():
    """The seed summary and user seeds for the system prompt, fixed for the
    whole conversation (until a flush) so the prompt prefix stays cached."""
    key = notes.conversation_id
    if key not in _snapshots:
        _snapshots[key] = {"summary": _summary(_load()), "user": user_seeds_text()}
    return _snapshots[key]


def summary():
    return snapshot()["summary"]


def refresh_snapshot():
    _snapshots.pop(notes.conversation_id, None)


# -- editing (the model) ---------------------------------------------------

_EDIT_HELP = ("Actions: title (set the seed's title line), topic (new topic: title + content), "
              "add (lines to a topic), prune (remove exact lines from a topic), rewrite (replace a topic).")


def _edit(action, content, topic, title, seed_id, in_task):
    now, state = time.time(), _load()
    if seed_id is None:
        if in_task:
            return "Error: tasks must name the seed id to edit."
        _ensure_current(state, now)
        seed_id = notes.conversation_id
    entry = state["seeds"].get(seed_id)
    if entry is None:
        return f"Error: no seed '{seed_id}'. Use interaction_search."
    if entry["tier"] == "vault" and in_task:
        return "Error: memory vault seeds are frozen. They change only in a conversation with the user."
    content = str(content or "").strip()
    seed_title, intro, topics = _read(seed_id, entry["tier"])
    found = next((t for t in topics if t[0] == topic), None)
    if action == "title":
        if not content:
            return "Error: title needs content."
        seed_title = " ".join(content.split())
    elif action == "topic":
        name = " ".join(str(title or "").split())
        if not name or not content:
            return "Error: a new topic needs a title and content."
        numbers = [int(t[1:]) for t in list(entry["topics"]) + [t[0] for t in topics]]
        topic = f"t{max(numbers, default=0) + 1}"
        topics.append([topic, name, content])
        entry["topics"][topic] = {"refs": [], "moved": None}
    elif action in ("add", "prune", "rewrite"):
        if found is None:
            names = ", ".join(f"{t[0]} ({t[1]})" for t in topics) or "none yet; use action topic"
            return f"Error: name the topic to change. Topics: {names}."
        moved = entry["topics"].get(topic, {}).get("moved")
        if moved:
            return f"Error: {topic} moved to long term as {moved}. Edit that seed instead."
        if not content:
            return f"Error: '{action}' needs content."
        if action == "add":
            found[2] = f"{found[2]}\n{content}".strip()
        elif action == "rewrite":
            found[2] = content
        else:
            remove = {line.strip() for line in content.splitlines() if line.strip()}
            kept = [line for line in found[2].splitlines() if line.strip() not in remove]
            if len(kept) == len(found[2].splitlines()):
                return "Error: no line matched. Use the exact line text."
            if not "\n".join(kept).strip():
                return "Error: that would empty the topic. Use rewrite instead."
            found[2] = "\n".join(kept).strip()
    else:
        return f"Error: unknown action. {_EDIT_HELP}"
    try:
        _write(seed_id, entry["tier"], seed_title, intro, topics)
    except ValueError as exc:
        return f"Error: {exc}"
    _touch(entry, now)
    _save(state)
    return f"Seed {seed_id} updated" + (f" ({topic})." if topic else ".")


def interaction_seed(action, content="", topic=None, title=None, id=None):
    """Edit a seed's contents. Without id: this conversation's seed."""
    return _edit(action, content, topic, title, id, in_task=False)


def edit_for_task(action, content="", topic=None, title=None, id=None):
    """Consolidation tasks edit short- and long-term seeds, never vault seeds."""
    return _edit(action, content, topic, title, id, in_task=True)


def interaction_review(id):
    """A consolidation task looked at a long-term seed and it still holds up."""
    state = _load()
    entry = state["seeds"].get(id)
    if entry is None:
        return f"Error: no seed '{id}'. Use interaction_search."
    if entry["tier"] != "long":
        return "Error: only long-term seeds are reviewed. Short-term ones live on references."
    _touch(entry, time.time())
    _save(state)
    return f"Reviewed {id}. It stays for another {config.LONG_TERM_DAYS} days."


# -- reading ---------------------------------------------------------------

def _links_vault(seed_id, entry, state):
    text = _path(seed_id, entry["tier"]).read_text(encoding="utf-8", errors="replace")
    return any(e["tier"] == "vault" and other in text for other, e in state["seeds"].items())


def interaction_search(query=""):
    """Seeds and their topics: vault seeds first, then long-term seeds that
    link to the vault, then the rest, newest first."""
    state = _load()
    words = re.findall(r"\w+", str(query).lower())
    rows = []
    for seed_id, entry in state["seeds"].items():
        text = _path(seed_id, entry["tier"]).read_text(encoding="utf-8", errors="replace")
        if words and not any(word in text.lower() or word in seed_id for word in words):
            continue
        linked = entry["tier"] == "long" and _links_vault(seed_id, entry, state)
        title, _, topics = _parse(text)
        facts = [TIERS[entry["tier"]]]
        if entry["expires"]:
            facts.append(f"expires {time.strftime('%Y-%m-%d', time.localtime(entry['expires']))}")
        if linked:
            facts.append("linked to the vault")
        if entry.get("note"):
            facts.append(f"note [[{entry['note']}]]" + ("" if vault.exists(entry["note"]) else " BROKEN LINK"))
        lines = [f"- {seed_id} · {' · '.join(facts)} · {title or '(no title)'}"]
        for tid, name, _ in topics:
            info = entry["topics"].get(tid, {})
            moved = info.get("moved")
            lines.append(f"    {tid} {name}" + (f" → moved to {moved}" if moved else f" ({len(info.get('refs', []))} refs)"))
        rank = (ORDER[entry["tier"]] if not linked else 0.5, -entry.get("updated", 0))
        rows.append((rank, "\n".join(lines)))
    if not rows:
        return "No matching seeds."
    rows.sort(key=lambda row: row[0])
    extra = f"\n({len(rows) - MAX_LISTED} more; search to narrow it down)" if len(rows) > MAX_LISTED else ""
    return "\n".join(text for _, text in rows[:MAX_LISTED]) + extra


def interaction_recall(id, topic=None, count=True):
    """Read a seed, or one topic. Recalling a topic from another conversation
    counts a reference for it and keeps the seed alive."""
    state = _load()
    entry = state["seeds"].get(id)
    if entry is None:
        return f"Error: no seed '{id}'. Use interaction_search."
    title, intro, topics = _read(id, entry["tier"])
    if not topic:
        return _render(title, intro, topics)
    found = next((t for t in topics if t[0] == topic), None)
    if found is None:
        return f"Error: {id} has no topic {topic}. Topics: {', '.join(t[0] for t in topics) or 'none'}."
    info = entry["topics"].setdefault(topic, {"refs": [], "moved": None})
    note = ""
    conversation = notes.conversation_id
    if (count and not info["moved"] and conversation not in (entry.get("conversation"), id)
            and conversation not in info["refs"]):
        now = time.time()
        info["refs"].append(conversation)
        _touch(entry, now)
        if entry["tier"] == "short" and len(info["refs"]) >= config.PROMOTE_AFTER_REFS:
            note = f"\n\n(This topic is now long term as {_promote(state, id, entry, found, now)}.)"
        _save(state)
    return _render(title, "", [found]) + note


def recall_for_task(id, topic=None):
    """Scheduled tasks read seeds without counting as a reference."""
    return interaction_recall(id, topic, count=False)


def _promote(state, seed_id, entry, found, now):
    tid, name, body = found
    new_id = _new_id(state, name, now)
    info = entry["topics"][tid]
    _write(new_id, "long", name, "", [["t1", name, f"{body}\nFrom: {seed_id} {tid}"]], limit=False)
    state["seeds"][new_id] = {"tier": "long", "created": now, "updated": now,
                              "conversation": entry.get("conversation"), "expires": now + _life("long"),
                              "topics": {"t1": {"refs": list(info["refs"]), "moved": None}}}
    title, intro, topics = _read(seed_id, entry["tier"])
    for topic in topics:
        if topic[0] == tid:
            topic[2] = f"Moved to long term: {new_id}"
    _write(seed_id, entry["tier"], title, intro, topics, limit=False)
    info["moved"] = new_id
    return new_id


# -- saving a memory to the vault ------------------------------------------

def footer(seed_id):
    """What goes at the very bottom of a vault note."""
    return f"---\nEcho seed: {seed_id}\nLocation: {TIERS['vault']}/{seed_id}.md"


def memory_save(title, note_path, note_content="", topics=""):
    """Save a memory: an Obsidian note plus a vault seed with the same name at
    its bottom. topics: the source topics, like '20261123-1400-55 t1, website-2026-11-20 t1'."""
    title = " ".join(str(title or "").split())
    if not title or not note_path:
        return "Error: a memory needs a title (a brief description) and a note_path in the vault."
    now, state = time.time(), _load()
    sources = []
    for part in [p.strip() for p in re.split(r"[,;\n]", str(topics or "")) if p.strip()]:
        seed_id, _, tid = part.replace(":", " ").rpartition(" ")
        seed_id, tid = seed_id.strip(), tid.strip()
        entry = state["seeds"].get(seed_id)
        if entry is None:
            return f"Error: no seed '{seed_id}' (from '{part}'). Write topics as '<seed id> <topic>'."
        found = next((t for t in _read(seed_id, entry["tier"])[2] if t[0] == tid), None)
        if found is None:
            return f"Error: seed {seed_id} has no topic '{tid}'."
        moved = entry["topics"].get(tid, {}).get("moved")
        if moved:
            return f"Error: {seed_id} {tid} moved to {moved}. Use that seed's topic."
        sources.append((seed_id, entry, found))
    try:
        exists = vault.exists(note_path)
    except Exception as exc:
        return f"Error: {exc}"
    if exists and str(note_content or "").strip():
        return "Error: that note already exists and vault notes are never replaced. Choose a new note_path, or omit note_content to link the existing note."
    if not exists and not str(note_content or "").strip():
        return "Error: that note doesn't exist yet. Include note_content: the summary, brief, and footnotes."

    seed_id = _new_id(state, title, now)
    name = str(note_path).strip().strip("[]")
    name = name[:-3] if name.lower().endswith(".md") else name
    copies = [[f"t{i}", found[1], f"{found[2]}\nFrom: {src} {found[0]}"]
              for i, (src, _, found) in enumerate(sources, 1)]
    _write(seed_id, "vault", title, f"Note: [[{name}]]", copies, limit=False)
    if exists:
        result = vault.vault_write(note_path, footer(seed_id), mode="append")
    else:
        result = vault.vault_write(note_path, f"{str(note_content).strip()}\n\n{footer(seed_id)}")
    if result.startswith("Error"):
        _path(seed_id, "vault").unlink(missing_ok=True)
        return result
    state["seeds"][seed_id] = {"tier": "vault", "created": now, "updated": now, "conversation": notes.conversation_id,
                               "expires": None, "note": name,
                               "topics": {c[0]: {"refs": [], "moved": None} for c in copies}}
    for src, entry, found in sources:  # point the sources at their saved memory
        stitle, intro, stopics = _read(src, entry["tier"])
        for topic in stopics:
            if topic[0] == found[0]:
                topic[2] = f"{topic[2]}\nSaved to the vault: {seed_id}"
        _write(src, entry["tier"], stitle, intro, stopics, limit=False)
        _touch(entry, now)
    _save(state)
    return f"Saved the memory: note [[{name}]] and vault seed {seed_id} ({len(copies)} topics). Both are permanent."


# -- user seeds ------------------------------------------------------------

def user_seeds_text():
    try:
        return config.USER_SEEDS_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def user_seeds(action, content=""):
    """add: lines. prune: remove exact lines. rewrite: replace it all."""
    content = str(content or "").strip()
    if not content:
        return f"Error: '{action}' needs content."
    current = user_seeds_text()
    if action == "add":
        updated = f"{current}\n{content}".strip()
    elif action == "rewrite":
        updated = content
    elif action == "prune":
        remove = {line.strip() for line in content.splitlines() if line.strip()}
        kept = [line for line in current.splitlines() if line.strip() not in remove]
        if len(kept) == len(current.splitlines()):
            return "Error: no line matched. Use the exact line text."
        updated = "\n".join(kept).strip()
    else:
        return "Error: action must be add, prune, or rewrite."
    if len(updated) > config.USER_SEEDS_MAX_CHARS:
        return (f"Error: user seeds would be {len(updated)} characters, over the {config.USER_SEEDS_MAX_CHARS} "
                "limit. Prune or rewrite them.")
    notes._write(config.USER_SEEDS_FILE, updated + "\n" if updated else "")
    return f"User seeds updated ({len(updated)} characters). They appear in your prompt from the next conversation."
