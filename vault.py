"""The long-term memory layer: one Obsidian vault, which is a folder of markdown.

Every path is confined to the vault folder (memory_settings.vault_dir(), set in
/options → Memory layers). Hidden folders such as .obsidian are off limits. Notes can be created or added to, but never overwritten here.
Search is plain keywords, so it works with any folder structure.
"""

import re
from pathlib import Path

import memory_settings

MAX_RESULTS = 10
MAX_SEARCH_BYTES = 1_000_000


def _root():
    root = memory_settings.vault_dir().resolve()
    if not root.is_dir():
        raise ValueError(f"no vault found at {root}. The user picks the vault folder in /options → Memory layers")
    return root


def _notes(root):
    for path in sorted(root.rglob("*.md")):
        if not any(part.startswith(".") for part in path.relative_to(root).parts):
            yield path


def _resolve(raw, root):
    """A path inside the vault from a path, note name, or [[wikilink]]."""
    name = str(raw or "").strip()
    if name.startswith("[[") and name.endswith("]]"):
        name = name[2:-2]
    name = name.split("|")[0].split("#")[0].strip()
    if not name:
        raise ValueError("a note path is needed")
    if not name.lower().endswith(".md"):
        name += ".md"
    path = (root / name).resolve()
    if root not in path.parents:
        raise ValueError("that path is outside the vault")
    if any(part.startswith(".") for part in path.relative_to(root).parts):
        raise ValueError("hidden vault folders are off limits")
    return path


def _find(raw, root):
    path = _resolve(raw, root)
    if path.exists():
        return path
    wanted = path.stem.lower()
    return next((note for note in _notes(root) if note.stem.lower() == wanted), None)


def exists(raw):
    try:
        root = _root()
        return _find(raw, root) is not None
    except ValueError:
        return False


def vault_search(query):
    """Find notes by keywords in their names and text."""
    try:
        root = _root()
    except ValueError as exc:
        return f"Error: {exc}."
    words = [w for w in re.findall(r"\w+", str(query).lower())]
    if not words:
        return "Error: enter something to search for."
    hits = []
    for path in _notes(root):
        if path.stat().st_size > MAX_SEARCH_BYTES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lower, name = text.lower(), path.stem.lower()
        score = sum(lower.count(w) + 5 * name.count(w) for w in words)
        if score:
            at = min((lower.find(w) for w in words if w in lower), default=0)
            snippet = " ".join(text[max(at - 40, 0):at + 100].split())
            hits.append((-score, str(path.relative_to(root)), snippet))
    if not hits:
        return "No matching notes."
    return "\n".join(f"- {name}: {snippet}" for _, name, snippet in sorted(hits)[:MAX_RESULTS])


def vault_read(path):
    """Read a note by path, name, or [[wikilink]]."""
    try:
        root = _root()
        note = _find(path, root)
    except ValueError as exc:
        return f"Error: {exc}."
    if note is None:
        return f"Error: no note called '{path}'. Use vault_search."
    text = note.read_text(encoding="utf-8", errors="replace")
    limit = memory_settings.get("vault_read_chars")
    if len(text) > limit:
        return text[:limit] + f"\n... [the rest of this note ({len(text) - limit} characters) is past the read limit]"
    return text


def vault_write(path, content, mode="create"):
    """Create a new note, or append to one. Existing text is never replaced."""
    text = str(content).strip()
    if not text:
        return "Error: a note can't be empty."
    limit = memory_settings.get("vault_write_chars")
    if len(text) > limit:
        return f"Error: notes are limited to {limit} characters at a time."
    try:
        root = _root()
        note = _resolve(path, root)
    except ValueError as exc:
        return f"Error: {exc}."
    if mode == "create":
        if note.exists():
            return f"Error: '{note.relative_to(root)}' already exists. Use mode append, or choose another name."
        note.parent.mkdir(parents=True, exist_ok=True)
        with open(note, "x", encoding="utf-8") as output:
            output.write(text + "\n")
        return f"Created {note.relative_to(root)}"
    if mode == "append":
        if not note.exists():
            return f"Error: no note at '{note.relative_to(root)}' to append to."
        with open(note, "a", encoding="utf-8") as output:
            output.write("\n" + text + "\n")
        return f"Appended to {note.relative_to(root)}"
    return "Error: mode must be create or append."
