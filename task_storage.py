"""Task files, approvals, saved tasks, and persisted clock state."""

import fcntl
import hashlib
import json
import os
import re
import time
import config
import skills

_FRONT = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
_EVERY = re.compile(r"^(\d+)\s*([mhdw])$")
_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
MIN_EVERY = 300
POWER_ORDER = ("read", "append", "write", "shell", "delete", "git", "seeds", "security")
KNOWN_POWERS = set(POWER_ORDER)
LOG_MAX_CHARS = 20000
STATE_FILE = ".state.json"
CYCLE_FILE = ".cycle.json"
ARCHIVE_FILE = "done.jsonl"
SAVED = "saved"  # deactivated tasks, without a number
EVERY_CYCLE = {"", "cycle", "every cycle", "each cycle", "clock", "internal clock", "per internal clock", "per cycle"}


def parse_every(text):
    """'6h' -> 21600, or None if it isn't valid (minimum 5 minutes)."""
    match = _EVERY.match((text or "").strip().lower())
    if not match:
        return None
    seconds = int(match.group(1)) * _UNITS[match.group(2)]
    return seconds if seconds >= MIN_EVERY else None


def every_cycle(text):
    """Whether an 'every' setting means: run in every cycle."""
    return " ".join((text or "").lower().split()) in EVERY_CYCLE


# -- storage ---------------------------------------------------------------

def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _state():
    return _read_json(config.TASKS_DIR / STATE_FILE, {})


def _save_state(state):
    _write_json(config.TASKS_DIR / STATE_FILE, state)


def load_clock():
    saved = _read_json(config.OPTIONS_DIR / "clock.json", {})
    return {"enabled": bool(saved.get("enabled", False)), "minutes": int(saved.get("minutes", 15)),
            "autonomous": bool(saved.get("autonomous", False))}


def save_clock(enabled, minutes, autonomous=False):
    _write_json(config.OPTIONS_DIR / "clock.json", {"enabled": enabled, "minutes": minutes, "autonomous": autonomous})


def _run_lock():
    """Only one task runs at a time, across every Echo process."""
    config.OPTIONS_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(config.OPTIONS_DIR / ".run.lock", "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def running():
    handle = _run_lock()
    if handle is None:
        return True
    handle.close()
    return False


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- reading tasks ---------------------------------------------------------

def _parse(path):
    task = {"id": path.name.split("-")[0], "path": path, "name": path.stem, "error": None}
    try:
        match = _FRONT.match(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        match = None
    if not match:
        return {**task, "error": "can't read its settings"}
    fields = dict(line.split(":", 1) for line in match.group(1).splitlines() if ":" in line)
    fields = {k.strip(): v.strip() for k, v in fields.items()}
    body = match.group(2).strip()
    lines = body.splitlines()
    if lines and lines[0].startswith("#"):
        task["name"] = lines[0].lstrip("# ").strip()
        body = "\n".join(lines[1:]).strip()
    repeat = fields.get("repeat", "")
    repeat = int(repeat) if repeat.isdigit() and int(repeat) > 0 else repeat
    every_text = fields.get("every", "")
    every = None if every_cycle(every_text) else parse_every(every_text)
    areas = [a.strip() for a in fields.get("areas", "").split("|") if a.strip()]
    powers = {p.strip() for p in fields.get("powers", "").split(",") if p.strip()}
    if repeat == "once":
        repeat = 1
    if not body or not areas or not (repeat == "forever" or isinstance(repeat, int)):
        return {**task, "error": "missing a description, folder, or repeat setting"}
    if every is None and not every_cycle(every_text):
        return {**task, "error": "'every' must be like 6h (5 minutes or more), or 'cycle'"}
    if not powers <= KNOWN_POWERS:
        return {**task, "error": f"unknown power: {', '.join(sorted(powers - KNOWN_POWERS))}"}
    return {**task, "body": body, "repeat": repeat, "every": every, "every_text": every_text,
            "areas": areas, "powers": powers | {"read"}, "model": fields.get("model") or None,
            "skills": [n.strip() for n in fields.get("skills", "").split(",") if n.strip()]}


PILOT_ONLY = "Pilot tasks can only be changed in the codebase (pilot_tasks/)."


def pilot_tasks():
    """Frozen tasks from config.PILOT_TASKS_DIR, ids like p1."""
    found = []
    for path in sorted(config.PILOT_TASKS_DIR.glob("p[0-9]*-*.md")) if config.PILOT_TASKS_DIR.is_dir() else ():
        task = _parse(path)
        task["pilot"] = True
        found.append(task)
    return found


def all_tasks():
    """Pilot tasks first, then the numbered ones."""
    return pilot_tasks() + [_parse(p) for p in sorted(config.TASKS_DIR.glob("[0-9]*.md"))]


def numbered_tasks():
    return [t for t in all_tasks() if not t.get("pilot")]


def status(task, state=None):
    """active, paused, done, invalid, or needs approval."""
    state = _state() if state is None else state
    entry = state.get(task["id"], {})
    if task["error"]:
        return "invalid"
    if entry.get("approved") != _digest(task["path"]):
        return "needs approval"
    if entry.get("paused"):
        return "paused"
    if task["repeat"] != "forever" and entry.get("runs", 0) >= task["repeat"]:
        return "done"
    return "active"


def describe(task, state=None):
    """'3 of 5 · last ok' for the task list."""
    state = _state() if state is None else state
    entry = state.get(task["id"], {})
    if task["error"]:
        return task["error"]
    runs = entry.get("runs", 0)
    count = f"{runs} runs, forever" if task["repeat"] == "forever" else f"{runs} of {task['repeat']}"
    pace = "every cycle" if task["every"] is None else f"at most every {task['every_text']}"
    text = f"{status(task, state)} · {count} · {pace}"
    if entry.get("last_status"):
        text += f" · last {entry['last_status']}"
    if notes_path(task["id"]).exists():
        text += " · has task notes"
    return text


# -- the clock's cycles ----------------------------------------------------

def _cycle():
    return _read_json(config.OPTIONS_DIR / CYCLE_FILE, {})


def _save_cycle(cycle):
    _write_json(config.OPTIONS_DIR / CYCLE_FILE, cycle)


def cycle_open():
    return bool(_cycle().get("open"))


def _mark_done_in_cycle(task_id):
    cycle = _cycle()
    if cycle.get("open") and task_id not in cycle["done"]:
        cycle["done"].append(task_id)
        _save_cycle(cycle)


# -- changing tasks --------------------------------------------------------

def _render(name, body, areas, powers, repeat, every, model=None, skills=()):
    """A task file's text."""
    lines = ["---", f"repeat: {'once' if repeat == 1 else repeat}"]
    if repeat != 1:
        lines.append(f"every: {every or 'cycle'}")
    if model:
        lines.append(f"model: {model}")
    if skills:
        lines.append(f"skills: {', '.join(sorted(skills))}")
    lines += [f"areas: {' | '.join(areas)}", f"powers: {', '.join(p for p in POWER_ORDER if p in powers)}",
              "---", f"# {' '.join(name.split())}", "", body.strip(), ""]
    return "\n".join(lines)


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "task"


def _next_number():
    numbers = [int(p.name.split("-")[0]) for p in config.TASKS_DIR.glob("[0-9]*.md") if p.name.split("-")[0].isdigit()]
    return f"{max(numbers, default=0) + 1:03d}"


def create(name, description, areas, powers, repeat, every, model=None, skills=()):
    config.TASKS_DIR.mkdir(parents=True, exist_ok=True)
    number = _next_number()
    path = config.TASKS_DIR / f"{number}-{_slug(name)}.md"
    path.write_text(_render(name, description, areas, powers, repeat, every, model, skills), encoding="utf-8")
    state = _state()
    state[number] = {"runs": 0, "created": time.time(), "approved": _digest(path)}
    _save_state(state)
    return _parse(path)


def approve(task):
    state = _state()
    entry = state.setdefault(task["id"], {"runs": 0, "created": time.time()})
    entry["approved"] = _digest(task["path"])
    _save_state(state)


def set_paused(task, paused):
    state = _state()
    entry = state.setdefault(task["id"], {"runs": 0, "created": time.time()})
    entry["paused"] = paused
    if not paused:
        entry["failures"] = 0
        entry.pop("missing_model", None)
    _save_state(state)


MISSING_MODEL = "Model not installed"


def mark_missing_model(task, model):
    """Pause a task whose model isn't installed, until the user picks another."""
    state = _state()
    entry = state.setdefault(task["id"], {"runs": 0, "created": time.time()})
    entry["paused"], entry["missing_model"] = True, model
    _save_state(state)


def missing_model_tasks():
    """Tasks paused because their model isn't installed."""
    state = _state()
    return [t for t in all_tasks() if state.get(t["id"], {}).get("missing_model")]


def missing_model_source(task):
    return f"task {task['id']}: {task['name']}"


def delete(task):
    if task.get("pilot"):
        return PILOT_ONLY
    lock = _run_lock()
    if lock is None:
        return BUSY
    try:
        _archive(task, "deleted")
        _renumber()
    finally:
        lock.close()
    return None


def _log_path(stem):
    return config.TASKS_DIR / "logs" / f"{stem}.md"


def _archive(task, how, saved=False):
    """One line in done.jsonl, then the task's files are removed."""
    state = _saved_state() if saved else _state()
    entry = state.pop(task["id"], {})
    record = {"id": task["id"], "name": task["name"], "how": how, "at": time.strftime("%Y-%m-%d %H:%M"),
              "file": task["path"].name, "runs": entry.get("runs", 0), "last_status": entry.get("last_status"),
              "last_report": entry.get("last_report", "")}
    if not task.get("error"):
        record.update(repeat=task["repeat"], every=task["every_text"] or "cycle", areas=task["areas"],
                      powers=sorted(task["powers"]), model=task.get("model"), text=task["body"])
    with open(config.TASKS_DIR / ARCHIVE_FILE, "a", encoding="utf-8") as output:
        output.write(json.dumps(record) + "\n")
    if saved:
        _save_saved_state(state)
        leftovers = (_saved_dir() / "logs" / f"{task['path'].stem}.md", _saved_dir() / "notes" / f"{task['id']}.md")
    else:
        _save_state(state)
        leftovers = (_log_path(task["path"].stem), notes_path(task["id"]))
    for path in (task["path"],) + leftovers:
        path.unlink(missing_ok=True)


def renumber():
    """Close the gaps left by finished, deleted, or saved tasks: 001, 002, ...
    Only while no task runs. Moves files, state, logs, notes, and the cycle."""
    lock = _run_lock()
    if lock is None:
        return
    try:
        _renumber()
    finally:
        lock.close()


def _renumber():
    paths = sorted((p for p in config.TASKS_DIR.glob("[0-9]*.md") if p.name.split("-")[0].isdigit()),
                   key=lambda p: int(p.name.split("-")[0]))
    state, cycle, moves = _state(), _cycle(), {}
    for index, path in enumerate(paths, 1):
        old, new = path.name.split("-")[0], f"{index:03d}"
        if old == new:
            continue
        rest = path.name[len(old):]
        path.rename(path.with_name(new + rest))
        log = _log_path(path.stem)
        if log.exists():
            log.rename(_log_path(new + rest[:-3]))
        if notes_path(old).exists():
            notes_path(old).rename(notes_path(new))
        moves[old] = new
    if moves:
        state = {moves.get(key, key): value for key, value in state.items()}
        cycle["done"] = [moves.get(key, key) for key in cycle.get("done", [])]
        _save_state(state)
        if cycle:
            _save_cycle(cycle)


# -- saved tasks (deactivated) ----------------------------------------------

def _saved_dir():
    return config.TASKS_DIR / SAVED


def _saved_state():
    return _read_json(_saved_dir() / STATE_FILE, {})


def _save_saved_state(state):
    _write_json(_saved_dir() / STATE_FILE, state)


def saved_tasks():
    tasks = []
    for path in sorted(_saved_dir().glob("*.md")):
        task = _parse(path)
        task["id"], task["saved"] = path.stem, True
        tasks.append(task)
    return tasks


def saved_status(task):
    if task["error"]:
        return "invalid"
    entry = _saved_state().get(task["id"], {})
    return "saved" if entry.get("approved") == _digest(task["path"]) else "needs approval"


def brief(task):
    """The first line of a task's description, for the saved list."""
    if task["error"]:
        return task["error"]
    line = next((l.strip("#*- ").strip() for l in task["body"].splitlines() if l.strip()), "")
    return line[:70] + ("..." if len(line) > 70 else "")


def _move(old_path, old_id, new_path, new_id, from_saved):
    """Move a task's file, log, and notes between the line-up and saved."""
    saved_dir = _saved_dir()
    old_log = (saved_dir / "logs" / f"{old_path.stem}.md") if from_saved else _log_path(old_path.stem)
    new_log = _log_path(new_path.stem) if from_saved else (saved_dir / "logs" / f"{new_path.stem}.md")
    old_notes = (saved_dir / "notes" / f"{old_id}.md") if from_saved else notes_path(old_id)
    new_notes = notes_path(new_id) if from_saved else (saved_dir / "notes" / f"{new_id}.md")
    new_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)
    for old, new in ((old_log, new_log), (old_notes, new_notes)):
        if old.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)


BUSY = "A task is running. Try again when it finishes."


def deactivate(task):
    """Move a task to saved tasks; the ones after it move up. Returns an
    error message, or None."""
    if task.get("pilot"):
        return PILOT_ONLY
    lock = _run_lock()
    if lock is None:
        return BUSY
    try:
        stem = task["path"].stem.split("-", 1)[-1] if "-" in task["path"].stem else task["path"].stem
        target, n = _saved_dir() / f"{stem}.md", 2
        while target.exists():
            target, n = _saved_dir() / f"{stem}-{n}.md", n + 1
        _move(task["path"], task["id"], target, target.stem, from_saved=False)
        state, saved = _state(), _saved_state()
        entry = state.pop(task["id"], {})
        entry.pop("paused", None)
        saved[target.stem] = entry
        _save_saved_state(saved)
        _save_state(state)
        cycle = _cycle()
        if task["id"] in cycle.get("done", []):
            cycle["done"].remove(task["id"])
            _save_cycle(cycle)
        _renumber()
    finally:
        lock.close()
    return None


def activate(task):
    """Put a saved task back at the end of the line. Returns an error
    message, or None."""
    lock = _run_lock()
    if lock is None:
        return BUSY
    try:
        number = _next_number()
        target = config.TASKS_DIR / f"{number}-{task['id']}.md"
        _move(task["path"], task["id"], target, number, from_saved=True)
        state, saved = _state(), _saved_state()
        entry = saved.pop(task["id"], {})
        entry["failures"] = 0
        state[number] = entry
        _save_state(state)
        _save_saved_state(saved)
    finally:
        lock.close()
    return None


def update(task, **changes):
    """Rewrite a task's settings or text (an edit the user made in /tasks, so it
    stays approved). changes: name, body, areas, powers, repeat, every, model, skills.
    Returns the updated task, or an error message."""
    if task.get("pilot"):
        return PILOT_ONLY
    if task["error"]:
        return "This task's file can't be read, so it can't be edited here."
    lock = _run_lock()
    if lock is None:
        return BUSY
    try:
        fields = {key: task[key] for key in ("name", "body", "areas", "powers", "repeat", "model", "skills")}
        fields["every"] = None if task["every"] is None else task["every_text"]
        fields.update(changes)
        task["path"].write_text(_render(**fields), encoding="utf-8")
        saved = task.get("saved")
        state = _saved_state() if saved else _state()
        state.setdefault(task["id"], {"runs": 0, "created": time.time()})["approved"] = _digest(task["path"])
        (_save_saved_state if saved else _save_state)(state)
    finally:
        lock.close()
    updated = _parse(task["path"])
    updated["id"] = task["id"]
    if saved:
        updated["saved"] = True
    return updated


def runs(task):
    state = _saved_state() if task.get("saved") else _state()
    return state.get(task["id"], {}).get("runs", 0)


def delete_saved(task):
    _archive(task, "deleted", saved=True)


def _record(task, result, text):
    state = _state()
    entry = state.setdefault(task["id"], {"runs": 0})
    entry["last_run"] = time.time()
    entry["last_status"] = result
    entry["last_report"] = str(text).strip()[:2000]
    if result == "ok":
        entry["runs"] = entry.get("runs", 0) + 1
        entry["failures"] = 0
    elif result == "failed":
        entry["failures"] = entry.get("failures", 0) + 1
        if entry["failures"] >= config.TASK_FAILURE_LIMIT:
            entry["paused"] = True
    _save_state(state)
    _mark_done_in_cycle(task["id"])
    log = _log_path(task["path"].stem)
    log.parent.mkdir(parents=True, exist_ok=True)
    old = log.read_text(encoding="utf-8") if log.exists() else ""
    entry_text = f"## {time.strftime('%Y-%m-%d %H:%M')} · {result}\n\n{str(text).strip()[:4000]}\n\n"
    log.write_text((old + entry_text)[-LOG_MAX_CHARS:], encoding="utf-8")
    if result == "ok" and task["repeat"] != "forever" and entry["runs"] >= task["repeat"] and not task.get("pilot"):
        _archive(task, "finished")


# -- task notes ------------------------------------------------------------

def notes_path(task_id):
    return config.TASKS_DIR / "notes" / f"{task_id}.md"


class TaskNotes:
    """A task's notes: written by the model when its context fills or its time
    runs out, read back when it continues. Kept until the task succeeds."""

    def __init__(self, task_id):
        self.path = notes_path(task_id)

    def read(self):
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def write(self, content):
        text = str(content or "").strip()
        if not text:
            return "Error: task notes can't be empty."
        import memory_settings
        limit = memory_settings.get("task_notes_chars")
        if len(text) > limit:
            return (f"Error: {len(text)} characters is over the {limit} limit. "
                    "Keep what you need to continue.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(text + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
        return f"Task notes saved ({len(text)} characters)."

    def clear(self):
        self.path.unlink(missing_ok=True)
