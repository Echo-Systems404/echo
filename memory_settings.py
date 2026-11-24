"""Memory layer settings: what /options → Memory layers changes.

Numbers are saved in options/memory.json. The defaults come from config.py, so
nothing changes until the user changes it. Every number has a fixed range that
is enforced when saving and again when loading: a hand-edited value outside it
falls back to the default.

Format rubrics are markdown files in "memory formats/": the defaults ship in
defaults/, and the user's edited copies sit beside them (git-ignored). Both are
in the harness folder, so Echo can read them but never write them. A layer's
rubric is added to its tools' descriptions, so it costs context only when the
tool is loaded.
"""

import json
import os
import re
from pathlib import Path

import config

FILE = "memory.json"
FORMATS_DIR = Path(__file__).parent / "memory formats"
RUBRICS = ("transient", "ephemeral", "task-notes", "vault", "compact")
SECTION_MAX = 60  # characters in a fixed transient section title


def _spec():
    """key: (default, low, high) for numbers, (default, choices) for choices."""
    return {
        "context_trigger": (round(config.CONTEXT_PROMPT_AT * 100), 60, 95),
        "context_action": ("ask", ("ask", "compact", "update")),
        "history_messages": (config.MAX_HISTORY_MESSAGES, 10, 100),
        "transient_entries": (config.TRANSIENT_ENTRIES, 1, 30),
        "transient_entry_chars": (config.TRANSIENT_ENTRY_MAX_CHARS, 1000, 8000),
        "transient_total_chars": (config.TRANSIENT_MAX_CHARS, 2000, 40000),
        "transient_sections": ([], None),
        "ephemeral_keep": (config.EPHEMERAL_KEEP, 1, 31),
        "ephemeral_chars": (config.EPHEMERAL_MAX_CHARS, 2000, 20000),
        "task_notes": (True, (True, False)),
        "task_notes_off": ("compact", ("compact", "end")),
        "task_notes_chars": (config.TASK_NOTES_MAX_CHARS, 2000, 20000),
        "vault_dir": ("", None),
        "vault_access": ("auto", ("auto", "ask", "off")),
        "vault_read_chars": (config.VAULT_READ_MAX_CHARS, 2000, 40000),
        "vault_write_chars": (config.VAULT_WRITE_MAX_CHARS, 1000, 40000),
    }


def _clean(key, value):
    spec = _spec()[key]
    default = spec[0]
    if key == "transient_sections":
        if not isinstance(value, list):
            return []
        seen, kept = set(), []
        for item in value:
            title = " ".join(str(item).split())[:SECTION_MAX]
            if title and title.lower() not in seen:
                seen.add(title.lower())
                kept.append(title)
        return kept[:_spec()["transient_entries"][2]]
    if key == "vault_dir":  # a folder that went missing (a drive unplugged) is kept; vault.py says so
        return value if isinstance(value, str) and (not value or vault_folder_problem(value, False) is None) else ""
    if len(spec) == 2:
        return value if value in spec[1] and type(value) is type(default) else default
    low, high = spec[1], spec[2]
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return default
    return value


def _path():
    return config.OPTIONS_DIR / FILE


def load():
    """Every setting, checked."""
    try:
        saved = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    return {key: _clean(key, saved.get(key, spec[0])) for key, spec in _spec().items()}


def get(key):
    return load()[key]


def save(**changes):
    """Save settings; returns the problems (empty when all were saved)."""
    current, problems = load(), []
    for key, value in changes.items():
        if key not in current:
            problems.append(f"unknown setting {key}")
            continue
        if key == "vault_dir" and value:
            problem = vault_folder_problem(value)
            if problem:
                problems.append(problem)
                continue
            value = str(Path(value).expanduser().resolve())
        cleaned = _clean(key, value)
        if cleaned != value and not (key == "transient_sections" and isinstance(value, list)):
            spec = _spec()[key]
            problems.append(f"{key} must be from {spec[1]} to {spec[2]}" if len(spec) == 3 else
                            f"{key} can't be {value!r}")
            continue
        current[key] = cleaned
    _path().parent.mkdir(parents=True, exist_ok=True)
    temporary = _path().with_name(FILE + ".tmp")
    temporary.write_text(json.dumps(current, indent=1), encoding="utf-8")
    os.replace(temporary, _path())
    return problems


def range_of(key):
    spec = _spec()[key]
    return (spec[1], spec[2]) if len(spec) == 3 else None


# -- the vault folder -----------------------------------------------------------

SYSTEM_FOLDERS = ("/System", "/Library", "/usr", "/bin", "/sbin", "/private", "/etc", "/var", "/tmp",
                  "/Applications", "/opt", "/cores", "/dev")


def vault_folder_problem(raw, must_exist=True):
    """Why a folder can't be the vault, or None. Deterministic: only the path."""
    try:
        path = Path(str(raw)).expanduser().resolve()
    except (OSError, RuntimeError):
        return "that path can't be read"
    harness = Path(__file__).resolve().parent
    if must_exist and not path.is_dir():
        return f"{path} isn't an existing folder"
    if path == Path("/") or path == Path.home().resolve():
        return "the vault can't be / or your whole home folder"
    if any(path == Path(f) or Path(f) in path.parents for f in SYSTEM_FOLDERS):
        if not str(path).startswith(str(Path.home().resolve())):
            return "the vault can't be a system folder"
    if path == harness or harness in path.parents:
        return "the vault can't be inside Echo's own folder"
    if any(part.startswith(".") for part in path.relative_to(path.anchor).parts):
        return "the vault can't be a hidden folder"
    return None


def vault_dir():
    """The vault folder: the one picked in the menu, else config.VAULT_DIR."""
    return Path(get("vault_dir") or config.VAULT_DIR).expanduser()


# -- rubrics ----------------------------------------------------------------------

def rubric_path(name):
    """The user's own rubric (git-ignored); until they edit one, the default is used."""
    return FORMATS_DIR / f"{name}.md"


def default_rubric_path(name):
    return FORMATS_DIR / "defaults" / f"{name}.md"


def _rubric_text(name):
    for path in (rubric_path(name), default_rubric_path(name)):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()
    return ""


def rubric(name):
    """The rubric for a layer, capped ("" if there is none)."""
    return _rubric_text(name)[:config.MEMORY_RUBRIC_MAX_CHARS]


def rubric_too_long(name):
    return len(_rubric_text(name)) > config.MEMORY_RUBRIC_MAX_CHARS


def rubric_is_default(name):
    return not rubric_path(name).exists()


# Which tools carry which rubric.
TOOL_RUBRICS = {"transient_edit": "transient", "ephemeral_save": "ephemeral", "task_notes": "task-notes",
                "memory_save": "vault"}
VAULT_READ_TOOLS = ("vault_search", "vault_read")
VAULT_TOOLS = VAULT_READ_TOOLS + ("memory_save",)


def schema(name, original):
    """A tool's schema with its layer's rubric added to the description."""
    which = TOOL_RUBRICS.get(name)
    text = rubric(which) if which else ""
    if not text:
        return original
    function = dict(original["function"])
    function["description"] = f"{function['description']}\n\nFormat to follow ({which} rubric):\n{text}"
    return {**original, "function": function}


def hidden(name, in_task=False):
    """Tools the vault setting turns off (Ask first is off in tasks: nobody can answer)."""
    access = get("vault_access")
    return name in VAULT_TOOLS and (access == "off" or (access == "ask" and in_task))


def needs_permission(name, args):
    """The reason a vault read needs approval under Ask first, or None."""
    if name not in VAULT_READ_TOOLS or get("vault_access") != "ask":
        return None
    what = args.get("query") or args.get("path") or ""
    verb = "search" if name == "vault_search" else "read"
    return f"Echo wants to {verb} your memory vault: {what}"


def output_limit(name):
    """Tool output allowed for memory reads, so their own caps decide (None: the usual limit)."""
    settings = load()
    limits = {"vault_read": settings["vault_read_chars"] + 300,
              "transient_read": max(settings["transient_entry_chars"], 2000) + 300,
              "ephemeral_read": settings["ephemeral_chars"] + 300}
    limit = limits.get(name)
    return max(limit, config.MAX_TOOL_OUTPUT_CHARS) if limit else None


# -- the live description for the pilot seed ----------------------------------

def describe():
    """Every layer as it is set right now, for the pilot seed's {{memory}}."""
    s = load()
    action = {"ask": "the user chooses a memory layer update or a compaction",
              "compact": "the conversation is compacted automatically",
              "update": "a memory layer update runs automatically"}[s["context_action"]]
    sections = (f" Only these entry titles are allowed: {', '.join(s['transient_sections'])}."
                if s["transient_sections"] else " You name the entries yourself, one per project or topic.")
    task = ("on: when a task's context fills, you write task notes and continue from them"
            if s["task_notes"] else
            "off: when a task's context fills, " + ("it is compacted and continues" if s["task_notes_off"] == "compact"
                                                   else "the run ends and the next cycle starts fresh"))
    vault = {"auto": "you may search and read it when it helps",
             "ask": "each search or read asks the user first (off during tasks)",
             "off": "off: no vault tools, and memory_save can't be used"}[s["vault_access"]]
    lines = [
        f"- Conversation: at {s['context_trigger']}% of the context window, {action}. "
        f"Between requests the last {s['history_messages']} messages are kept.",
        f"- Transient notes (transient_read, transient_edit): your notebook of titled entries, up to "
        f"{s['transient_entries']} entries of {s['transient_entry_chars']} characters, "
        f"{s['transient_total_chars']} in all.{sections} Add new information to the entry it belongs to.",
        f"- Ephemeral memory (ephemeral_save, ephemeral_list, ephemeral_read): summaries of your last "
        f"{s['ephemeral_keep']} saved conversations, up to {s['ephemeral_chars']} characters each. Save only "
        "when the user asks; read only when they ask if you remember or the conversation depends on an earlier one.",
        f"- Task notes (task_notes): {task}. Up to {s['task_notes_chars']} characters.",
        f"- Memory vault (vault_search, vault_read, memory_save): the user's notes folder; {vault}. "
        "memory_save writes a note only when the user asks; existing notes are never replaced.",
        "- Interaction seeds, user seeds, and seeds: described in their own sections.",
    ]
    return "\n".join(lines)
