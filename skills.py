"""Skills: abilities the user adds to Echo as folders in config.SKILLS_DIR.

    skills/<name>/SKILL.md    front matter (name, description), then the
                              instructions Echo gets when it loads the skill
    skills/<name>/tools.py    optional: TOOLS = {name: tool entry}, the same
                              shape as tools.TOOLS

Echo's prompt lists only each skill's name and description; load_skill gives
it the instructions and adds the tools. skills/ is inside the harness, so
Echo can read it but never change it. Which skills are on, and the digest
the user approved, are kept in options/skills.json: a skill whose files changed
outside /skills is off until the user approves it again (as with tasks).
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import time

import config

STATE_FILE = "skills.json"
REMOVED = "removed"
_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


class Skill:
    def __init__(self, folder):
        self.folder = folder
        self.name = folder.name
        self.description = ""
        self.instructions = ""
        self.error = None
        self._tools = None
        self._tools_digest = None
        try:
            match = _FRONT.match((folder / "SKILL.md").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            match = None
        if not match:
            self.error = "SKILL.md is missing or has no front matter"
            return
        fields = dict(line.split(":", 1) for line in match.group(1).splitlines() if ":" in line)
        fields = {k.strip(): v.strip() for k, v in fields.items()}
        self.description = fields.get("description", "")
        self.instructions = match.group(2).strip()
        if not self.description:
            self.error = "SKILL.md needs a description"

    @property
    def tools_file(self):
        return self.folder / "tools.py"

    def digest(self):
        sha = hashlib.sha256()
        for path in (self.folder / "SKILL.md", self.tools_file):
            if path.exists():
                sha.update(path.name.encode() + b"\0" + path.read_bytes())
        return sha.hexdigest()

    def tools(self):
        """The skill's tools (from tools.py), loaded once per version."""
        if not self.tools_file.exists():
            return {}
        digest = self.digest()
        if self._tools is None or self._tools_digest != digest:
            spec = importlib.util.spec_from_file_location(f"echo_skill_{self.name}", self.tools_file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            found = getattr(module, "TOOLS", None)
            if not isinstance(found, dict):
                raise ValueError("tools.py has no TOOLS dict")
            self._tools, self._tools_digest = found, digest
        return dict(self._tools)


# -- state (options/skills.json) --------------------------------------------

def _state():
    try:
        return json.loads((config.OPTIONS_DIR / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state):
    path = config.OPTIONS_DIR / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(STATE_FILE + ".tmp")
    temporary.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(temporary, path)


# -- reading ------------------------------------------------------------------

def all_skills():
    if not config.SKILLS_DIR.is_dir():
        return []
    return [Skill(folder) for folder in sorted(config.SKILLS_DIR.iterdir())
            if folder.is_dir() and folder.name != REMOVED and not folder.name.startswith((".", "_"))]


def status(skill, state=None):
    """on, off, needs approval, or broken."""
    if skill.error:
        return "broken"
    entry = (_state() if state is None else state).get(skill.name, {})
    if entry.get("approved") != skill.digest():
        return "needs approval"
    return "on" if entry.get("enabled") else "off"


def usable(names=None):
    """{name: Skill} for skills that are on (and among names, if given)."""
    state = _state()
    found = {}
    for skill in all_skills():
        if (names is None or skill.name in names) and status(skill, state) == "on":
            found[skill.name] = skill
    return found


def prompt_section(available):
    """The lines that tell Echo which skills it can load."""
    if not available:
        return ""
    lines = ["# Skills", "Call load_skill with a skill's name to get its instructions and tools."]
    lines += [f"- {name}: {skill.description}" for name, skill in available.items()]
    return "\n".join(lines)


# -- changing (the user, from /skills) ---------------------------------------------

def approve(skill, enabled=True):
    state = _state()
    state[skill.name] = {"enabled": enabled, "approved": skill.digest(), "changed": time.time()}
    _save_state(state)


def set_enabled(skill, enabled):
    state = _state()
    entry = state.setdefault(skill.name, {})
    entry["enabled"] = enabled
    _save_state(state)


def save_instructions(skill, text):
    """Rewrite SKILL.md's instructions (the user's edit, so it stays approved)."""
    path = skill.folder / "SKILL.md"
    path.write_text(f"---\nname: {skill.name}\ndescription: {skill.description}\n---\n\n{text.strip()}\n",
                    encoding="utf-8")
    state = _state()
    if state.get(skill.name, {}).get("approved"):
        state[skill.name]["approved"] = Skill(skill.folder).digest()
        _save_state(state)


def remove(skill):
    """Moved to skills/removed/, never deleted."""
    target_dir = config.SKILLS_DIR / REMOVED
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{skill.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.move(str(skill.folder), str(target))
    state = _state()
    state.pop(skill.name, None)
    _save_state(state)
    return target


def add_from(folder):
    """Copy a skill folder into skills/. Returns the Skill or an error message."""
    if not folder.is_dir():
        return f"Can't find that folder: {folder}"
    name = folder.name.lower().replace(" ", "-")
    if not NAME.match(name):
        return "Skill folder names use lowercase letters, digits, '-' and '_'."
    target = config.SKILLS_DIR / name
    if target.exists():
        return f"A skill named {name} already exists."
    skill = Skill(folder)
    if skill.error:
        return f"That folder isn't a skill: {skill.error}."
    shutil.copytree(folder, target, ignore=shutil.ignore_patterns("__pycache__", ".*"))
    added = Skill(target)
    approve(added)
    return added
