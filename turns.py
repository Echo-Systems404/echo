"""One model turn at a time, across processes.

The terminal chat, Telegram, and scheduled tasks each hold this lock while
they use the model, so two conversations never load or call models at once.
Chat and Telegram wait their turn; a task that finds it taken is "busy" and
runs at the next check.
"""

import fcntl
import time

import config


def _path():
    config.OPTIONS_DIR.mkdir(parents=True, exist_ok=True)
    return config.OPTIONS_DIR / ".turn.lock"


def hold(wait=False, stop=None, poll=0.25):
    """The lock (keep the returned file open while working), or None if it's
    taken and wait is False, or stop (a threading.Event) was set."""
    while True:
        handle = open(_path(), "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            handle.close()
        if not wait or (stop is not None and stop.is_set()):
            return None
        time.sleep(poll)


def busy():
    handle = hold()
    if handle is None:
        return True
    handle.close()
    return False
