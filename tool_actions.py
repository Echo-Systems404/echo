"""Shell, file, and task-creation tool implementations and permission checks."""

import json
import os
import re
import signal
import subprocess
from pathlib import Path
import communication
import config
import memory_settings

DEFAULT_COMMAND_TIMEOUT = 60
MAX_COMMAND_TIMEOUT = 600


def run_command(cmd, timeout=DEFAULT_COMMAND_TIMEOUT, writable=None):
    """Run a shell command non-interactively and return its output.
    writable: folders a sandboxed command may write in (tasks, sub-agents);
    None runs it unsandboxed (chat, where the user approves commands)."""
    import sandbox
    timeout = max(1, min(int(timeout), MAX_COMMAND_TIMEOUT))
    if writable is not None and not sandbox.available():
        return "Error: the command sandbox (sandbox-exec) isn't available, so task commands can't run."
    with subprocess.Popen(
        sandbox.wrap(cmd, writable) if writable is not None else cmd,
        shell=writable is None,
        stdin=subprocess.DEVNULL,  # never wait on the user's terminal
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "CI": "1"},  # most CLIs skip prompts when CI is set
        start_new_session=True,  # own process group, so it can be killed whole
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except BaseException as exc:  # timeout, or the user interrupted
            # Kill the shell *and* everything it started (e.g. a dev server);
            # a surviving child would keep the pipes open and hang us.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            if not isinstance(exc, subprocess.TimeoutExpired):
                raise
            return (
                f"Error: command timed out after {timeout}s. If it is a server or "
                "watcher, don't run it; otherwise retry with a larger timeout."
            )
    parts = [f"exit code: {proc.returncode}"]
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    return "\n".join(parts)


# Commands the user must approve before they run: (what it does, pattern).
# Best effort, not a sandbox; when unsure the patterns err toward asking.
PERMISSION_RULES = [
    ("deletes files", re.compile(
        # rm/rmdir/unlink/shred/trash as a command word (also `git rm`,
        # `xargs rm`, `-exec rm`), find -delete, git clean, Python deletes
        r"(?<![\w.-])(rm|rmdir|unlink|shred|trash)(?![\w.-])"
        r"|\s-delete\b|\bgit\s+clean\b|\b(os\.remove|os\.unlink|os\.rmdir|shutil\.rmtree)\b|\.unlink\(")),
    ("pushes or discards git history", re.compile(
        r"\bgit\s+(push|reset\s+--hard|branch\s+-D|checkout\s+(-f|--force))(?![\w-])")),
    ("stops running processes", re.compile(r"(?<![\w.-])(kill|killall|pkill)(?![\w.-])")),
    ("changes background services (launchctl)", re.compile(r"\blaunchctl\b")),
    ("runs a script straight from the internet", re.compile(
        r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(sh|bash|zsh|python3?)\b|<\(\s*(curl|wget)\b")),
    ("runs as administrator (sudo)", re.compile(r"(?<![\w.-])sudo(?![\w.-])")),
    ("shuts down the Mac or changes disks", re.compile(
        r"(?<![\w.-])(shutdown|reboot|halt|diskutil|mkfs|dd)(?![\w.-])")),
]


HARNESS = Path(__file__).resolve().parent

# A command that could change files. Used only near the harness, so ordinary
# reading (ls, cat, grep, git status/log/diff) there needs no approval.
_CHANGES_FILES = re.compile(
    r">|(?<![\w.-])(tee|mv|cp|ln|touch|chmod|chown|chflags|truncate|install|rsync|patch|mkdir|tar|unzip"
    r"|ditto|dd|perl|ruby|node|python3?|osascript|vim?|nano|open)(?![\w.-])"
    r"|\bsed\b[^|;&]*\s-i|\bgit\s+(checkout|restore|reset|stash|apply|commit|merge|rebase|pull|cherry-pick"
    r"|revert|mv|am|switch|rm|clean)\b")
_HARMLESS_REDIRECTS = re.compile(r"\d?>\s*/dev/null|\d?>&\d")


def mentions_harness(cmd):
    """Whether a command names the harness folder (best effort)."""
    text = cmd.replace("\\ ", " ")
    home = str(Path.home())
    forms = {str(HARNESS), str(HARNESS).replace(home, "~", 1), str(HARNESS).replace(home, "$HOME", 1)}
    return any(form in text for form in forms)


def touches_harness(cmd):
    """Whether a command could change the harness: it names the harness, or it
    runs inside the harness folder and could change files."""
    if mentions_harness(cmd):
        return True
    try:
        here = Path.cwd().resolve()
    except OSError:
        return False
    inside = here == HARNESS or HARNESS in here.parents
    return inside and bool(_CHANGES_FILES.search(_HARMLESS_REDIRECTS.sub("", cmd)))


def run_command_needs_permission(cmd, timeout=None):
    """Return why the user must approve this command, or None."""
    reasons = [why for why, pattern in PERMISSION_RULES if pattern.search(cmd)]
    if touches_harness(cmd):
        reasons.append("could change the harness, which only you manage")
    if reasons:
        return f"This command {' and '.join(reasons)}:\n  {cmd}"
    return None


def read_file(path):
    """Return the text content of a file."""
    return Path(path).read_text(encoding="utf-8")


def write_file(path, content):
    """Write text to a file, creating parent directories as needed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {path}"


def write_file_needs_permission(path, content=None):
    """Replacing an existing vault note takes the user's approval."""
    target = Path(path).resolve()
    if target.exists() and memory_settings.vault_dir().resolve() in target.parents:
        return f"This overwrites a note in your vault:\n  {target}"
    return None


def append_file(path, content):
    """Add text to the end of a file, creating it if needed. It can't change
    what's already there. In a .jsonl file every new line must be valid JSON."""
    target = Path(path)
    text = content.strip("\n")
    if not text.strip():
        return "Error: nothing to append."
    if target.suffix.lower() == ".jsonl":
        for number, entry in enumerate(text.splitlines(), 1):
            try:
                json.loads(entry)
            except ValueError:
                return f"Error: line {number} isn't valid JSON, so nothing was added. Put each entry on one line."
    target.parent.mkdir(parents=True, exist_ok=True)
    needs_break = False
    if target.exists() and target.stat().st_size:
        with open(target, "rb") as existing:
            existing.seek(-1, os.SEEK_END)
            needs_break = existing.read(1) != b"\n"
    with open(target, "a", encoding="utf-8") as output:
        output.write(("\n" if needs_break else "") + text + "\n")
    return f"Appended {len(text.splitlines())} line(s) to {path}"


def list_directory(path="."):
    """List the entries of a directory with their type and size."""
    entries = []
    for item in sorted(Path(path).iterdir()):
        if item.is_dir():
            entries.append(f"[DIR]  {item.name}/")
        else:
            entries.append(f"[FILE] {item.name} ({item.stat().st_size} bytes)")
    return "\n".join(entries) or "(empty directory)"


SEARCH_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist"}
SEARCH_MAX_LINES = 100


def search_files(pattern, path=".", files="*"):
    """Lines matching a regular expression in the files under path."""
    import fnmatch
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"Error: bad regular expression: {exc}"
    root = Path(path).expanduser()
    if not root.exists():
        return f"Error: {root} doesn't exist."
    found, scanned = [], 0
    walker = [(str(root.parent), [], [root.name])] if root.is_file() else os.walk(root)
    for folder, dirs, names in walker:
        dirs[:] = sorted(d for d in dirs if d not in SEARCH_SKIP_DIRS and not d.startswith(".")
                         and not communication.private(Path(folder) / d))  # tokens and message logs
        for name in sorted(names):
            if not fnmatch.fnmatch(name, files):
                continue
            file_path = Path(folder) / name
            try:
                if file_path.stat().st_size > 1_000_000:
                    continue
                text = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            scanned += 1
            for number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    found.append(f"{file_path}:{number}: {line.strip()[:200]}")
                    if len(found) >= SEARCH_MAX_LINES:
                        return "\n".join(found) + f"\n(stopped at {SEARCH_MAX_LINES} matches; narrow the search)"
    return "\n".join(found) if found else f"No matches in {scanned} files."


# -- Echo sets up tasks (chat only; the user approves each one in the prompt) ------

def _task_plan(name, instructions, folders, powers=None, repeat="once", every="cycle", model=None, skills=None):
    """(clean settings, None) or (None, error) for create_task."""
    import tasks
    import skills as skill_registry
    name = " ".join(str(name or "").split())
    if not name or not str(instructions or "").strip():
        return None, "a task needs a name and instructions"
    folders = [folders] if isinstance(folders, str) else list(folders or [])
    areas = []
    for folder in folders:
        path = Path(str(folder)).expanduser().resolve()
        if not path.is_dir():
            return None, f"folder not found: {path}"
        if path == HARNESS or HARNESS in path.parents:
            return None, "tasks can't work inside Echo's own folder"
        areas.append(str(path))
    if not areas:
        return None, "a task needs at least one folder to work in"
    powers = set(powers or []) | {"read"}
    unknown = powers - tasks.KNOWN_POWERS
    if unknown:
        return None, f"unknown powers: {', '.join(sorted(unknown))}. Known: {', '.join(tasks.POWER_ORDER)}"
    repeat_text = str(repeat).strip().lower()
    if repeat_text in ("once", "1"):
        repeat = 1
    elif repeat_text == "forever":
        repeat = "forever"
    elif repeat_text.isdigit() and int(repeat_text) > 0:
        repeat = int(repeat_text)
    else:
        return None, "repeat must be once, forever, or a number"
    every = str(every or "cycle").strip()
    if not tasks.every_cycle(every) and tasks.parse_every(every) is None:
        return None, "every must be 'cycle' or like 6h or 30m (5 minutes or more)"
    if model:
        import inference
        try:
            if model not in inference.driver().tool_models():
                return None, f"model {model} isn't installed (or can't call tools)"
        except Exception:
            pass
    wanted = set(skills or [])
    missing = wanted - set(skill_registry.usable())
    if missing:
        return None, f"skills not available: {', '.join(sorted(missing))}"
    return {"name": name, "description": str(instructions).strip(), "areas": areas, "powers": powers,
            "repeat": repeat, "every": None if tasks.every_cycle(every) else every, "model": model or None,
            "skills": sorted(wanted)}, None


def create_task_needs_permission(**args):
    plan, error = _task_plan(**args)
    if error:
        return None  # create_task reports the problem
    import tasks
    timing = "once" if plan["repeat"] == 1 else f"{plan['repeat']} times" if plan["repeat"] != "forever" else "forever"
    pace = f", at most every {plan['every']}" if plan["every"] else ", every cycle"
    lines = [f"{config.AGENT_NAME} wants to create a task: {plan['name']}",
             f"  Runs: {timing}{pace if plan['repeat'] != 1 else ''}",
             f"  Model: {plan['model'] or 'whatever Echo is using'}",
             f"  Folders: {', '.join(plan['areas'])}",
             f"  Powers: {', '.join(p for p in tasks.POWER_ORDER if p in plan['powers'])}"]
    if plan["skills"]:
        lines.append(f"  Skills: {', '.join(plan['skills'])}")
    lines += ["  Instructions:"] + [f"    {line}" for line in plan["description"].splitlines()[:30]]
    return "\n".join(lines)


def list_tasks():
    """The user's tasks, the clock, and any run waiting to start."""
    import tasks
    clock = tasks.load_clock()
    pace = (f"on, a cycle every {clock['minutes']} min, "
            f"{'autonomous' if clock['autonomous'] else 'only while the terminal is open'}"
            if clock["enabled"] else "off (tasks run only when asked)")
    lines = [f"Internal clock: {pace}."]
    items = tasks.all_tasks()
    if not items:
        lines.append("No tasks.")
    for task in items:
        model = task.get("model") or "Echo's model"
        kind = "pilot task, " if task.get("pilot") else ""
        lines.append(f"- {task['id']}: {task['name']} ({kind}{tasks.describe(task)}; model {model}). "
                     f"{tasks.brief(task)}")
    request = tasks.requested()
    if request:
        lines.append(f"Waiting to run now: task {request['id']}.")
    saved = tasks.saved_tasks()
    if saved:
        lines.append(f"Saved (inactive) tasks: {', '.join(t['name'] for t in saved)}.")
    return "\n".join(lines)


def _find_task(task_id):
    import tasks
    wanted = str(task_id).strip().lstrip("#")
    return next((t for t in tasks.all_tasks() if t["id"] == wanted or t["id"] == wanted.zfill(3)
                 or t["name"].lower() == wanted.lower()), None)


def run_task_needs_permission(id):
    task = _find_task(id)
    if task is None:
        return None  # run_task reports it
    import tasks
    return (f"Run task {task['id']} ({task['name']}) now, with {task.get('model') or 'Echo' + chr(39) + 's model'}, "
            f"in {', '.join(task['areas'])} (powers: {', '.join(sorted(task['powers'])) or 'read'})? "
            f"It's {tasks.status(task)}.")


def run_task(id, source="chat"):
    """Queue one task to run as soon as the model is free (the user approved it)."""
    import tasks
    task = _find_task(id)
    if task is None:
        return f"Error: no task '{id}'. Use list_tasks to see them."
    state = tasks.status(task)
    if state != "active":
        return f"Error: task {task['id']} is {state}; {config.USER_NAME} changes that in /tasks on the Mac."
    waiting = tasks.requested()
    if waiting and waiting["id"] != task["id"]:
        return f"Error: task {waiting['id']} is already waiting to run; ask again after it finishes."
    tasks.request_run(task["id"], source)
    after = (f" {config.USER_NAME} gets the result on Telegram when it finishes." if source == "telegram"
             else " It runs at the next quiet moment in the terminal or in the background runner.")
    return f"Queued: task {task['id']} ({task['name']}) starts as soon as the model is free (after this reply).{after}"


def create_task(name, instructions, folders, powers=None, repeat="once", every="cycle", model=None, skills=None):
    """Create a task (after the user approved it in the prompt)."""
    import tasks
    plan, error = _task_plan(name, instructions, folders, powers, repeat, every, model, skills)
    if error:
        return f"Error: {error}"
    task = tasks.create(plan["name"], plan["description"], plan["areas"], plan["powers"], plan["repeat"],
                        plan["every"], plan["model"], plan["skills"])
    clock = tasks.load_clock()
    note = "" if clock["enabled"] else f" The internal clock is off, so it won't run until {config.USER_NAME} turns it on in /options."
    return f"Created task {task['id']}: {task['name']}. It runs in the next clock cycle.{note}"
