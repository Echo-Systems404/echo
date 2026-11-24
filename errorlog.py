"""Error logging helper: logs suppressed exceptions to errors.jsonl with per-location rate limiting."""

import json
import time

import config

_LAST_LOGGED = {}


def log_error(where, exc):
    """Write one JSON line {"time", "where", "error"} to errors.jsonl,
    at most once per minute per 'where'."""
    now = time.time()
    last = _LAST_LOGGED.get(where, 0)
    if now - last < 60:
        return
    _LAST_LOGGED[where] = now
    try:
        log_dir = config.OPTIONS_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "errors.jsonl"
        record = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "where": where,
            "error": f"{type(exc).__name__}: {exc}",
        }
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass
