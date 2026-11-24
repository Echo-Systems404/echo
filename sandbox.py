"""The command sandbox: task and sub-agent commands run under macOS's own
sandbox (sandbox-exec, the Seatbelt profile language), so where they can
write is enforced by macOS, not by reading the command.

A profile allows everything except writing files, then allows writing only
in the given folders, temp folders, and /dev; the harness folder is denied
last, so nothing inside it can ever be written, even through a symlink.
"""

import os
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
HARNESS = Path(__file__).resolve().parent
TEMP_DIRS = ("/private/tmp", "/private/var/tmp", "/private/var/folders")


def available():
    return os.path.exists(SANDBOX_EXEC)


def _quote(path):
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def profile(writable):
    """A Seatbelt profile that only allows writing inside writable folders."""
    folders = [Path(p).expanduser().resolve() for p in writable]
    allow = " ".join(f"(subpath {_quote(p)})" for p in folders + [Path(t) for t in TEMP_DIRS])
    return "\n".join([
        "(version 1)",
        "(allow default)",
        '(deny file-write* (subpath "/"))',
        f'(allow file-write* {allow} (subpath "/dev"))',
        f"(deny file-write* (subpath {_quote(HARNESS)}))",
    ])


def wrap(cmd, writable):
    """argv that runs a shell command inside the sandbox."""
    return [SANDBOX_EXEC, "-p", profile(writable), "/bin/sh", "-c", cmd]
