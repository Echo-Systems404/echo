"""Seeds: small markdown notes the agent keeps in config.SEEDS_DIR.

The pilot seed (pilot.md) is a template the user writes; {{tools}} and
{{seeds}} in it are filled with the current tool list (on-demand tools are
listed here instead of being sent to the model until it loads them) and the
seed index, and the result goes into the system prompt. Every other seed is
<name>.md, whose first line is its one-line summary; the agent reads, adds,
trims and removes those with the seed_* tools.
"""

import re

import config
import interaction

PILOT = "pilot"
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def render_pilot(tools, in_task=False):
    """The pilot seed with its placeholders filled in ("" if there is none).
    Tools the memory settings turn off aren't listed."""
    path = config.SEEDS_DIR / f"{PILOT}.md"
    if not path.exists():
        return ""
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.DOTALL).strip()
    import memory_settings
    shown = {name: tool for name, tool in tools.items() if not memory_settings.hidden(name, in_task)}
    return (text.replace("{{tools}}", _tool_list(shown)).replace("{{seeds}}", _index())
            .replace("{{interaction}}", interaction.summary()).replace("{{memory}}", memory_settings.describe()))


def _tool_list(tools):
    def lines(on_demand):
        return [f"- {name}: {tool['schema']['function']['description']}"
                for name, tool in tools.items() if bool(tool.get("on_demand")) == on_demand]
    text = "Ready to use:\n" + "\n".join(lines(False))
    if lines(True):
        text += "\n\nOn demand (call load_tool with the name first):\n" + "\n".join(lines(True))
    return text


def _index():
    lines = []
    for path in sorted(config.SEEDS_DIR.glob("*.md")):
        if path.stem == PILOT or path == config.USER_SEEDS_FILE:
            continue
        first = next((l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()), "")
        lines.append(f"- {path.stem}: {first.lstrip('# ').strip() or '(no summary)'}")
    return "\n".join(lines) or "(no seeds yet)"


def _path(name):
    if not NAME.match(name or ""):
        raise ValueError("seed names use lowercase letters, digits, '-' and '_' (max 41 characters)")
    if name == PILOT:
        raise ValueError("the pilot seed is maintained by the user and can't be changed")
    return config.SEEDS_DIR / f"{name}.md"


def seed_read(name):
    """Return a seed's content."""
    path = _path(name)
    if not path.exists():
        return f"Error: no seed named '{name}'."
    return path.read_text(encoding="utf-8")


def seed_write(name, content):
    """Create a seed, or replace it (to update or trim it)."""
    path = _path(name)
    content = content.strip()
    if not content:
        return "Error: a seed can't be empty; use seed_remove to delete it."
    if len(content) > config.SEED_MAX_CHARS:
        return (f"Error: {len(content)} characters is over the {config.SEED_MAX_CHARS} limit; "
                "trim it to the essentials.")
    existed = path.exists()
    config.SEEDS_DIR.mkdir(exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")
    return f"{'Updated' if existed else 'Added'} seed '{name}' ({len(content)} characters)."


def seed_remove(name):
    """Delete a seed."""
    path = _path(name)
    if not path.exists():
        return f"Error: no seed named '{name}'."
    path.unlink()
    return f"Removed seed '{name}'."


def seed_remove_needs_permission(name):
    return f"This removes the seed '{name}' ({config.SEEDS_DIR / (name + '.md')})."
