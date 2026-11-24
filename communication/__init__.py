"""Communication: the ways the user reaches Echo outside the terminal.

Each app lives in its own folder (communication/telegram/, later others) with
its code and a private data/ folder for tokens, settings, and conversation
logs. data/ folders are git-ignored, and Echo's own tools can't read or list
them: only the harness code and the recall tools touch them.
"""

from pathlib import Path

import config


def private(path):
    """Whether a path is inside any app's private data folder."""
    try:
        resolved = Path(str(path)).expanduser().resolve()
        root = Path(config.COMMUNICATION_DIR).resolve()
        relative = resolved.relative_to(root)
    except (ValueError, OSError, RuntimeError):
        return False
    return len(relative.parts) >= 2 and relative.parts[1] == "data"
