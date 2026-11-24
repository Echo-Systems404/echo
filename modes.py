"""Self layers: optional modes stacked on top of self.md.

Every .md file in the "self layers" folder is a mode, named after the file
(coding.md -> /mode coding). The first line is its description in the dropdown.
The folder is read again each time /mode is used, so new files need no restart.
"""

from pathlib import Path

import memory

OFF = "off"  # reserved: /mode off returns to plain self.md


def scan(folder, max_chars):
    """{name: {"text", "description", "too_long"}} for each usable mode file."""
    layers = {}
    for path in sorted(Path(folder).glob("*.md")):
        name = path.stem.strip().lower()
        if not name or name.startswith(".") or name == OFF:
            continue
        try:
            text = memory.read_markdown(path)
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            layers[name] = {
                "text": text,
                "description": text.splitlines()[0].lstrip("# ").strip(),
                "too_long": len(text) > max_chars,
            }
    return layers
