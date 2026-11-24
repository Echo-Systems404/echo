"""The sentinel: the harness watches this Mac on its own, every
SENTINEL_MINUTES, while Echo or the background runner is open and a pilot task
with the security power is active. No model is involved in watching.

Each scan runs the remote-access scan (security.py: cuts and reminders as
before) and the malware checks (malware.py), and appends what it finds to
options/security/events.jsonl. The first scan is the baseline: what's already
there isn't flagged, only changes after it.

High findings are contained at once by fixed rules, and only the item that was
flagged: the user's own process is stopped, a file is moved to quarantine (never
deleted), a user startup item is unloaded and its file quarantined. Anything
that needs root is left alone and the user gets the exact command. A high finding
opens an incident: the pilot security task runs right away in response mode,
the harness rescans after each run, and after SENTINEL_RESPONSE_ROUNDS without
clearing it, the user is told to step in.

The model's tools (pilot security tasks): security_events (what's new since
its last summary), security_rescan, and security_contain (only high items the
harness flagged).
"""

import fcntl
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import config
import malware
import reminders
import security

SOURCE = "sentinel"
_shown = set()  # event ids the model was shown in this run


def _dir():
    return config.OPTIONS_DIR / "security"


def _state_path():
    return _dir() / "sentinel.json"


def _events_path():
    return _dir() / "events.jsonl"


def quarantine_dir():
    return _dir() / "quarantine"


def _load_state():
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state):
    _dir().mkdir(parents=True, exist_ok=True)
    temporary = _state_path().with_name("sentinel.json.tmp")
    temporary.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(temporary, _state_path())


def _lock():
    _dir().mkdir(parents=True, exist_ok=True)
    handle = open(_dir() / ".sentinel.lock", "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


# -- events ---------------------------------------------------------------------

def events():
    try:
        lines = _events_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return found


def _write_events(items):
    _dir().mkdir(parents=True, exist_ok=True)
    temporary = _events_path().with_name("events.jsonl.tmp")
    temporary.write_text("".join(json.dumps(e) + "\n" for e in items), encoding="utf-8")
    os.replace(temporary, _events_path())


def _append(new):
    if not new:
        return
    _dir().mkdir(parents=True, exist_ok=True)
    with open(_events_path(), "a", encoding="utf-8") as handle:
        for event in new:
            handle.write(json.dumps(event) + "\n")


def _event(found, now, number, done=None):
    return {"id": f"e{int(now)}-{number}", "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "ts": now, "kind": found["kind"], "severity": found["severity"], "text": found["text"],
            "item": found["item"], "done": done or [], "summarized": False}


# -- who's on duty ---------------------------------------------------------------

def security_task():
    """The active pilot task with the security power, or None."""
    import tasks
    for task in tasks.pilot_tasks():
        if not task["error"] and "security" in task["powers"] and tasks.status(task) == "active":
            return task
    return None


def active():
    return security_task() is not None


# -- containment (fixed rules, only flagged items) ---------------------------------

def _owned(path):
    try:
        return os.stat(path).st_uid == os.getuid() and os.access(os.path.dirname(path) or "/", os.W_OK)
    except OSError:
        return False


def _quarantine(path, event_id, reason):
    """Move a file into quarantine with a manifest. (ok, message)."""
    path = str(path)
    if not os.path.exists(path):
        return True, f"{path} is already gone"
    if not _owned(path):
        return False, f"{path} needs your password to move: sudo mv '{path}' '{quarantine_dir()}/'"
    folder = quarantine_dir() / event_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / Path(path).name
    try:
        shutil.move(path, str(target))
        os.chmod(target, 0o600)  # never runnable from quarantine
    except OSError as exc:
        return False, f"couldn't move {path}: {exc}"
    manifest = folder / "manifest.json"
    entries = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
    entries.append({"original": path, "quarantined": str(target), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "reason": reason, "restore": f"mv '{target}' '{path}'"})
    manifest.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    return True, f"moved {path} to quarantine ({target})"


def _stop(pid, path):
    """Stop a process (the user's own only). (ok, message)."""
    try:
        if not any(p["pid"] == pid and p["path"] == path for p in malware.processes()):
            return True, f"pid {pid} isn't running anymore"  # (or the pid now belongs to something else)
        os.kill(pid, signal.SIGTERM)
    except PermissionError:
        return False, f"pid {pid} runs as another user: sudo kill -9 {pid}"
    except ProcessLookupError:
        return True, f"pid {pid} isn't running anymore"
    for _ in range(20):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True, f"stopped pid {pid}"
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True, f"stopped pid {pid} (forced)"


def _bootout(plist):
    """Unload a user launch agent. (ok, message)."""
    try:
        subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}", plist], capture_output=True,
                       timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"couldn't unload {plist}: {exc}"
    return True, f"unloaded {plist}"


def contain(event):
    """Contain one high finding by the fixed rules. Returns [(ok, message)]."""
    item, done = event["item"], []
    reason = event["text"]
    if item["type"] == "process":
        done.append(_stop(item["pid"], item["path"]))
        if item["path"].startswith(malware.SUSPICIOUS_DIRS):
            done.append(_quarantine(item["path"], event["id"], reason))
    elif item["type"] == "startup":
        if item["scope"] == "user":
            done.append(_bootout(item["plist"]))
            done.append(_quarantine(item["plist"], event["id"], reason))
            if item.get("program") and _owned(item["program"]):
                done.append(_quarantine(item["program"], event["id"], reason))
        else:
            done.append((False, f"{item['plist']} is a system startup item. To switch it off: "
                                f"sudo launchctl bootout system '{item['plist']}' && "
                                f"sudo mv '{item['plist']}' '{quarantine_dir()}/'"))
    elif item["type"] == "report":
        done.append((True, "XProtect handles its own findings; check System Settings > Privacy & Security"))
    return done


def still_active(item):
    """Whether a contained item is still there."""
    if item["type"] == "process":
        return any(p["pid"] == item["pid"] and p["path"] == item["path"] for p in malware.processes())
    if item["type"] == "startup":
        return os.path.exists(item["plist"])
    return False


# -- scanning -----------------------------------------------------------------------

REMOTE_TAGS = {"CUT:": "info", "QUIT:": "info", "CUT FAILED:": "medium", "QUIT FAILED:": "medium",
               "TURNED OFF:": "medium", "NEW ON THE NETWORK:": "info", "NEW INTERNET CONNECTIONS:": "info"}


def _remote_findings(report):
    """The remote-access scan's own lines, as findings (it already acted and reminded)."""
    found = []
    for line in report.splitlines():
        for tag, severity in REMOTE_TAGS.items():
            if line.startswith(tag):
                found.append(malware.finding("remote", severity, line, {"type": "setting", "name": "remote"}))
                break
    return found


def due(now=None, state=None):
    now = now or time.time()
    state = _load_state() if state is None else state
    return now - state.get("last_scan", 0) >= config.SENTINEL_MINUTES * 60


def tick(now=None, force=False):
    """Scan if it's time (or force). Returns the new events, or None if no
    scan ran. Safe to call often from several processes."""
    now = now or time.time()
    if not active():
        return None
    lock = _lock()
    if lock is None:
        return None
    try:
        state = _load_state()
        if not force and not due(now, state):
            return None
        return _scan(state, now)
    finally:
        lock.close()


def _scan(state, now):
    cache = state.setdefault("signatures", {})
    first = not state.get("baseline")
    minutes = max(config.SENTINEL_MINUTES + 1, int((now - state.get("last_scan", now)) / 60) + 1)
    found = _remote_findings(security.scan(source=SOURCE, now=now))  # sets its own baseline the first time
    found += malware.check_xprotect(minutes)
    startup = malware.check_startup(set(state.get("startup", [])), cache)
    prints = malware.fingerprints()
    files = malware.check_files(state.get("files", {}), prints)
    procs, online_unsigned = malware.check_processes(set(state.get("online_unsigned", [])), cache)
    if now - state.get("last_slow", 0) >= config.SENTINEL_SLOW_MINUTES * 60:
        found += malware.check_xprotect_age(config.XPROTECT_MAX_AGE_DAYS, now)
        state["last_slow"] = now
    state["startup"] = sorted(malware.startup_items())
    state["files"] = prints
    state["online_unsigned"] = sorted(set(state.get("online_unsigned", [])) | online_unsigned)
    state["last_scan"] = now
    if first:
        state["baseline"] = now
        new = [_event(malware.finding("baseline", "info", f"Sentinel baseline set: {len(state['startup'])} startup "
                                      f"items, {len(prints)} sensitive files, {len(online_unsigned)} unsigned "
                                      "programs online. Only changes from now on are reported.",
                                      {"type": "setting", "name": "baseline"}), now, 0)]
        new += [_event(f, now, i + 1) for i, f in enumerate(procs + found) if f["severity"] == "high"]
    else:
        new = [_event(f, now, i) for i, f in enumerate(found + startup + files + procs)]
    _prune(state, now)
    opened = []
    for event in new:
        if event["severity"] == "high":
            results = contain(event)
            event["done"] = [message for _, message in results]
            opened.append(event)
        elif event["severity"] == "medium":
            _remind(state, event, now)
    if opened:
        _open_incident(state, opened, now)
    _save_state(state)
    _append(new)
    return new


def _remind(state, event, now):
    key = f"{event['kind']}:{json.dumps(event['item'], sort_keys=True)}"
    issues = state.setdefault("reminded", {})
    if now - issues.get(key, 0) < config.SECURITY_REPEAT_HOURS * 3600:
        return
    issues[key] = now
    urgent = event["kind"] in ("startup", "file", "xprotect-age")
    reminders.add(f"Security: {event['text']}", SOURCE, urgent, now=now)


def _prune(state, now):
    keep = config.SECURITY_EVENTS_KEEP_DAYS * 86400
    items = events()
    kept = [e for e in items if not (e.get("summarized") and now - e.get("ts", now) > keep)]
    if len(kept) != len(items):
        _write_events(kept)


# -- incidents ------------------------------------------------------------------------

def _open_incident(state, opened, now):
    incident = state.get("incident") or {"opened": now, "rounds": 0, "items": [], "escalated": False}
    for event in opened:
        incident["items"].append({"event": event["id"], "item": event["item"], "text": event["text"]})
    state["incident"] = incident
    lines = "; ".join(f"{e['text']} ({', '.join(e['done']) or 'nothing done'})" for e in opened)
    reminders.add(f"Security incident: {lines}. Echo is responding.", SOURCE, True, now=now)


def incident():
    return _load_state().get("incident")


def due_response():
    """Whether the pilot security task should run now to respond."""
    current = incident()
    return bool(current and not current.get("escalated") and active())


def active_items(current=None):
    current = current or incident() or {}
    return [entry for entry in current.get("items", []) if still_active(entry["item"])]


def after_response(now=None):
    """After a response run: rescan, then close, continue, or escalate."""
    now = now or time.time()
    tick(now, force=True)
    lock = _lock()
    try:
        state = _load_state()
        current = state.get("incident")
        if not current:
            return "no incident"
        current["rounds"] += 1
        left = active_items(current)
        if not left:
            state["incident"] = None
            _append([_event(malware.finding("incident", "info", "Incident closed: nothing flagged is active "
                                            "anymore.", {"type": "setting", "name": "incident"}), now, 0)])
            reminders.add("Security incident closed: what was flagged has been stopped or quarantined. "
                          f"Quarantined files are in {quarantine_dir()} (each folder has a manifest).",
                          SOURCE, False, now=now)
            result = "closed"
        elif current["rounds"] >= config.SENTINEL_RESPONSE_ROUNDS:
            current["escalated"] = True
            reminders.add("Security incident NOT cleared after "
                          f"{current['rounds']} response rounds: " + "; ".join(e["text"] for e in left)
                          + ". Disconnect this Mac from the network (turn off Wi-Fi) and look at it yourself.",
                          SOURCE, True, now=now)
            result = "escalated"
        else:
            result = "continuing"
        _save_state(state)
        return result
    finally:
        if lock is not None:
            lock.close()


def acknowledge():
    """The user closes an escalated incident."""
    state = _load_state()
    state["incident"] = None
    _save_state(state)


# -- the model's tools ----------------------------------------------------------------

def _format(event):
    done = f" Done: {'; '.join(event['done'])}." if event.get("done") else ""
    return f"- [{event['severity']}] {event['time']} {event['id']}: {event['text']}{done}"


def security_events():
    """New events since the last summary, and the incident, if any."""
    new = [e for e in events() if not e.get("summarized")]
    _shown.update(e["id"] for e in new)
    state = _load_state()
    lines = [f"Sentinel: scans every {config.SENTINEL_MINUTES} min; last scan "
             f"{time.strftime('%H:%M', time.localtime(state.get('last_scan', 0))) if state.get('last_scan') else 'never'}."]
    current = state.get("incident")
    if current:
        left = active_items(current)
        lines.append(f"INCIDENT open since {time.strftime('%H:%M', time.localtime(current['opened']))}, "
                     f"response round {current['rounds'] + 1} of {config.SENTINEL_RESPONSE_ROUNDS}. "
                     f"Still active: {'; '.join(e['text'] for e in left) or 'nothing'}.")
    lines.append(f"Events since the last summary: {len(new)}")
    lines += [_format(e) for e in new[-60:]] or ["- none: quiet."]
    if len(new) > 60:
        lines.append(f"({len(new) - 60} older events not shown)")
    return "\n".join(lines)


def security_rescan():
    """Scan now and report what's new and what's still active."""
    new = tick(force=True) or []
    left = active_items()
    lines = [f"Rescanned: {len(new)} new events."] + [_format(e) for e in new]
    lines.append("Still active from the incident: " + ("; ".join(e["text"] for e in left) if left else "nothing."))
    return "\n".join(lines)


def security_contain(event_id):
    """Contain a high event the harness flagged, again."""
    event = next((e for e in events() if e["id"] == event_id), None)
    if event is None:
        return f"Error: no event {event_id}."
    if event["severity"] != "high":
        return f"Error: only high events can be contained automatically; {config.USER_NAME} decides the rest."
    if not still_active(event["item"]):
        return f"{event_id} is already contained."
    return "; ".join(message for _, message in contain(event))


def finish_run(ok):
    """End of a pilot security run: events it saw are summarized when it finished ok."""
    if ok and _shown:
        items = events()
        for event in items:
            if event["id"] in _shown:
                event["summarized"] = True
        _write_events(items)
    _shown.clear()


def task_tools():
    import tools

    def tool(function, name, description, properties=None, required=()):
        return {"function": function,
                "schema": tools._schema(name, description, properties or {}, list(required))}
    return {
        "security_events": tool(security_events, "security_events",
                                "What the harness's sentinel logged since your last summary, and any open incident."),
        "security_rescan": tool(security_rescan, "security_rescan",
                                "Have the harness scan this Mac now; shows new events and what's still active."),
        "security_contain": tool(security_contain, "security_contain",
                                 "Contain a HIGH event the harness flagged (stop it, quarantine it, switch it off). "
                                 "Only flagged high events; nothing is ever deleted.",
                                 {"event_id": {"type": "string", "description": "The event id, like e1795520000-3."}},
                                 ["event_id"]),
    }
