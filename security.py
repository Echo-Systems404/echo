"""Security scan: the harness checks this Mac and acts by fixed rules.

The model never decides what to cut. It calls security_scan, reads the plain
report, and explains it. Everything here is deterministic:

- Scanning needs no admin password (netstat, ps, the firewall tool, ...).
- The first scan sets a baseline: whatever remote access is already on goes
  into options/security/allowed.txt, so Echo never fights a choice the user made.
- After that, remote access that isn't allowed is cut: Remote Login, Screen
  Sharing and File Sharing are turned off, Remote Management is deactivated,
  remote-control apps running as the user are quit, and the firewall is turned on
  if it goes off. Nothing else is ever cut, including internet connections.
- Cuts run with `sudo -n` and only work once the user installs the sudoers file
  this module writes, which allows exactly those commands (each one only turns
  something off or a protection on).
- Findings become reminders written by the harness: urgent for cuts, failed
  cuts, new startup items, and protections turning off; normal for new apps
  listening on the network or connecting out. A problem that stays is
  reminded again at most every SECURITY_REPEAT_HOURS.
"""

import getpass
import ipaddress
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import config
import reminders

FIREWALL = "/usr/libexec/ApplicationFirewall/socketfilterfw"
KICKSTART = "/System/Library/CoreServices/RemoteManagement/ARDAgent.app/Contents/Resources/kickstart"
STARTUP_DIRS = ("~/Library/LaunchAgents", "/Library/LaunchAgents", "/Library/LaunchDaemons")

# Remote access services, by the port they listen on.
SERVICE_PORTS = {22: "remote-login", 5900: "screen-sharing", 3283: "remote-management", 445: "file-sharing"}
SERVICE_NAMES = {"remote-login": "Remote Login (SSH)", "screen-sharing": "Screen Sharing",
                 "remote-management": "Remote Management", "file-sharing": "File Sharing",
                 "firewall-off": "Firewall off"}
# Remote-control and tunnel apps, by a word in their process name.
REMOTE_APPS = {"teamviewer": "TeamViewer", "anydesk": "AnyDesk", "rustdesk": "RustDesk", "splashtop": "Splashtop",
               "logmein": "LogMeIn", "screenconnect": "ScreenConnect", "connectwise": "ConnectWise",
               "remoting_me2me_host": "Chrome Remote Desktop", "vncserver": "VNC server", "realvnc": "RealVNC",
               "parsecd": "Parsec", "jumpconnect": "Jump Desktop Connect", "ngrok": "ngrok",
               "cloudflared": "Cloudflare tunnel"}

SYSTEM_PATHS = ("/System/", "/usr/libexec/", "/usr/sbin/", "/sbin/")  # read-only, signed by Apple

# The only commands a cut may run, each with sudo -n. The sudoers file allows
# exactly these, so they're also all a shell task could ever do with it.
CUTS = {
    "remote-login": [["/bin/launchctl", "disable", "system/com.openssh.sshd"],
                     ["/bin/launchctl", "bootout", "system/com.openssh.sshd"]],
    "screen-sharing": [["/bin/launchctl", "disable", "system/com.apple.screensharing"],
                       ["/bin/launchctl", "bootout", "system/com.apple.screensharing"]],
    "file-sharing": [["/bin/launchctl", "disable", "system/com.apple.smbd"],
                     ["/bin/launchctl", "bootout", "system/com.apple.smbd"]],
    "remote-management": [[KICKSTART, "-deactivate", "-stop"]],
    "firewall-off": [[FIREWALL, "--setglobalstate", "on"]],
}
PROTECTIONS = {"firewall": "Firewall", "stealth": "Stealth mode", "filevault": "FileVault",
               "gatekeeper": "Gatekeeper", "sip": "System Integrity Protection"}
# How to turn a protection back on, for reminders.
FIXES = {"stealth": f"sudo {FIREWALL} --setstealthmode on",
         "filevault": "System Settings > Privacy & Security > FileVault",
         "gatekeeper": "sudo spctl --master-enable",
         "sip": "csrutil enable, from macOS Recovery"}
SUDOERS_NAME = "echo-security"


def _dir():
    return config.OPTIONS_DIR / "security"


def _state_path():
    return _dir() / "state.json"


def allowed_path():
    return _dir() / "allowed.txt"


def sudoers_path():
    return _dir() / f"{SUDOERS_NAME}.sudoers"


def _run(args, timeout=15):
    """A command's output, or '' if it fails. Tests replace this."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _sudo(args):
    """(ok, message) for one cut command. Tests replace this."""
    try:
        done = subprocess.run(["/usr/bin/sudo", "-n"] + args, capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if done.returncode == 0:
        return True, ""
    error = (done.stderr or done.stdout).strip()
    if "password" in error.lower():
        error = "Echo isn't allowed to run it without your password yet (the sudoers file isn't installed)"
    return False, error or f"exit code {done.returncode}"


def _quit(pid):
    """(ok, message) for quitting an app. Tests replace this."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True, ""
    except OSError as exc:
        return False, str(exc)


# -- collecting facts ------------------------------------------------------

def _split_address(text):
    host, _, port = text.rpartition(".")
    return host, int(port) if port.isdigit() else None


def _is_local(host):
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")


def _is_private(host):
    """Loopback, LAN, or link-local: not the internet."""
    if host in ("*", ""):
        return True
    try:
        address = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        # netstat cuts long IPv6 addresses short; judge by the prefix
        lowered = host.lower()
        return lowered.startswith(("fe80", "fc", "fd", "::1"))
    return address.is_private or address.is_loopback or address.is_link_local


def parse_netstat(text, proto):
    """Rows from `netstat -anv -p tcp|udp`: dicts with local/foreign host and
    port, state, and process name. Process names may contain spaces."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if not parts or not parts[0].startswith(proto) or len(parts) < 14:
            continue
        has_state = proto == "tcp"
        process_start = 10 if has_state else 9
        process = " ".join(parts[process_start:-8])
        name, _, pid = process.rpartition(":")
        local_host, local_port = _split_address(parts[3])
        foreign_host, _ = _split_address(parts[4])
        rows.append({"proto": proto, "local": local_host, "port": local_port, "foreign": foreign_host,
                     "state": parts[5] if has_state else "", "process": name or process,
                     "pid": int(pid) if pid.isdigit() else None})
    return rows


def _flag(text, on_words):
    lowered = text.lower()
    if not lowered.strip():
        return None
    return any(word in lowered for word in on_words)


def collect():
    """What the scan sees right now."""
    tcp = parse_netstat(_run(["netstat", "-anv", "-p", "tcp"]), "tcp")
    udp = parse_netstat(_run(["netstat", "-anv", "-p", "udp"]), "udp")
    listening = [r for r in tcp if r["state"] == "LISTEN" and not _is_local(r["local"])]
    services = sorted({SERVICE_PORTS[r["port"]] for r in listening if r["port"] in SERVICE_PORTS}
                      | {"remote-management" for r in udp if r["port"] == 3283 and not _is_local(r["local"])})
    exposed = {}
    for r in listening:
        if r["port"] not in SERVICE_PORTS:
            exposed.setdefault(r["process"], set()).add(r["port"])
    outbound = sorted({r["process"] for r in tcp if r["state"] == "ESTABLISHED" and not _is_private(r["foreign"])})
    apps, me = [], os.getuid()
    for line in _run(["ps", "-axo", "pid=,uid=,comm="]).splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or parts[2].startswith(SYSTEM_PATHS):
            continue  # macOS's own services can share a name (CoreParsec's parsecd is Spotlight)
        base = Path(parts[2]).name.lower().replace(" ", "")
        for key, name in REMOTE_APPS.items():
            if key in base:
                apps.append({"key": key, "name": name, "pid": int(parts[0]), "mine": parts[1] == str(me)})
                break
    settings = {  # "disabled" doesn't contain "enabled", so these read both answers
        "firewall": _flag(_run([FIREWALL, "--getglobalstate"]), ("enabled",)),
        "stealth": _flag(_run([FIREWALL, "--getstealthmode"]), ("is on", "enabled")),
        "filevault": _flag(_run(["fdesetup", "status"]), ("is on",)),
        "gatekeeper": _flag(_run(["spctl", "--status"]), ("enabled",)),
        "sip": _flag(_run(["csrutil", "status"]), ("enabled",)),
    }
    startup = []
    for folder in STARTUP_DIRS:
        path = Path(folder).expanduser()
        try:
            startup += [str(path / name) for name in sorted(os.listdir(path)) if not name.startswith(".")]
        except OSError:
            pass
    return {"services": services, "exposed": {k: sorted(v) for k, v in exposed.items()}, "outbound": outbound,
            "apps": apps, "settings": settings, "startup": startup}


# -- the allow list and state -----------------------------------------------

ALLOWED_HEADER = """# Remote access Echo leaves alone. One per line; # starts a comment.
# Services: remote-login, screen-sharing, remote-management, file-sharing
# The firewall being off: firewall-off
# Apps: teamviewer, anydesk, rustdesk, splashtop, logmein, screenconnect, connectwise,
#   remoting_me2me_host, vncserver, realvnc, parsecd, jumpconnect, ngrok, cloudflared
# Anything not listed here is cut when a scan finds it.
"""


def load_allowed():
    try:
        lines = allowed_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {line.split("#")[0].strip().lower() for line in lines if line.split("#")[0].strip()}


def _write_allowed(keys, note):
    _dir().mkdir(parents=True, exist_ok=True)
    text = ALLOWED_HEADER + "".join(f"{key}  # {note}\n" for key in sorted(keys))
    allowed_path().write_text(text, encoding="utf-8")


def _load_state():
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_state(state):
    _dir().mkdir(parents=True, exist_ok=True)
    temporary = _state_path().with_name("state.json.tmp")
    temporary.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(temporary, _state_path())


# -- deciding (pure: no commands) ------------------------------------------

def decide(facts, state, allowed):
    """What to cut and what's new since the last scan.
    Returns {"cuts": [key, ...], "quits": [app, ...], "new": {...}, "turned_off": [...]}."""
    cuts = [key for key in facts["services"] if key not in allowed]
    if facts["settings"].get("firewall") is False and "firewall-off" not in allowed:
        cuts.append("firewall-off")
    quits = [app for app in facts["apps"] if app["key"] not in allowed]
    before = state.get("settings", {})
    turned_off = [key for key in PROTECTIONS if key != "firewall"
                  and before.get(key) is True and facts["settings"].get(key) is False]
    new = {
        "startup": [s for s in facts["startup"] if s not in state.get("startup", [])],
        "exposed": [p for p in facts["exposed"] if p not in state.get("exposed", [])],
        "outbound": [p for p in facts["outbound"] if p not in state.get("outbound", [])],
    }
    return {"cuts": cuts, "quits": quits, "new": new, "turned_off": turned_off}


def _remember(state, facts):
    """Add what this scan saw to what's known, so it's reported only once."""
    for key in ("startup", "outbound"):
        state[key] = sorted(set(state.get(key, [])) | set(facts[key]))
    state["exposed"] = sorted(set(state.get("exposed", [])) | set(facts["exposed"]))
    state["settings"] = facts["settings"]


# -- the tool --------------------------------------------------------------

def write_sudoers():
    """The sudoers file allowing exactly the cut commands, for the user to install."""
    commands = [" ".join(command) for steps in CUTS.values() for command in steps]
    text = ("# Echo security scan: lets Echo cut remote access without a password.\n"
            "# Every command here only turns something off or a protection on.\n"
            "Cmnd_Alias ECHO_CUTS = " + ", \\\n    ".join(commands) + "\n"
            f"{getpass.getuser()} ALL=(root) NOPASSWD: ECHO_CUTS\n")
    _dir().mkdir(parents=True, exist_ok=True)
    sudoers_path().write_text(text, encoding="utf-8")
    return sudoers_path()


def _fix_command(key):
    return " && ".join("sudo " + " ".join(command) for command in CUTS[key])


def _remind(state, key, text, urgent, now, source):
    """A reminder, at most once every SECURITY_REPEAT_HOURS per problem."""
    issues = state.setdefault("issues", {})
    if now - issues.get(key, 0) < config.SECURITY_REPEAT_HOURS * 3600:
        return False
    issues[key] = now
    reminders.add(text, source, urgent, now=now)
    return True


def scan(source="security scan", now=None):
    """Scan, act by the fixed rules, leave reminders, and return a report."""
    now = now or time.time()
    facts = collect()
    state = _load_state()
    lines = [f"Security scan, {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}"]
    left = []

    if state is None:  # the first scan: a baseline
        already = set(facts["services"]) | {a["key"] for a in facts["apps"]}
        if facts["settings"].get("firewall") is False:
            already.add("firewall-off")
        allowed = load_allowed() | already
        _write_allowed(allowed, "on when the baseline was set")
        state = {"baseline_at": now}
        _remember(state, facts)
        _save_state(state)
        if not sudoers_path().exists():
            write_sudoers()
        lines.append("Baseline set: everything below is now what's known. Anything already on was added to "
                     f"{allowed_path()} and left alone.")
        if "firewall-off" in already:
            text = (f"Security baseline set. The firewall is off; to turn it on: sudo {FIREWALL} --setglobalstate on "
                    "(then remove firewall-off from options/security/allowed.txt).")
            reminders.add(text, source, False, now=now)
            left.append(text)
        lines += _posture(facts, allowed)
        return "\n".join(lines + _left(left))

    allowed = load_allowed()
    plan = decide(facts, state, allowed)
    for key in plan["cuts"]:
        name = SERVICE_NAMES[key]
        failures = [message for ok, message in (_sudo(c) for c in CUTS[key]) if not ok]
        if not failures:
            text = (f"{name} was on and not in your allowed list, so Echo turned it off. If you meant to turn it "
                    f"on, add {key} to options/security/allowed.txt and turn it back on.")
            lines.append(f"CUT: {name} turned off.")
            state.get("issues", {}).pop(f"cut:{key}", None)
            reminders.add(text, source, True, now=now)
            left.append(text)
        else:
            text = (f"{name} is on and not in your allowed list, but Echo couldn't turn it off: {failures[0]}. "
                    f"To turn it off: {_fix_command(key)}. To keep it, add {key} to options/security/allowed.txt.")
            lines.append(f"CUT FAILED: {name}: {failures[0]}")
            if _remind(state, f"cut:{key}", text, True, now, source):
                left.append(text)
    for app in plan["quits"]:
        ok, message = _quit(app["pid"]) if app["mine"] else (False, "it runs as another user")
        if ok:
            text = (f"{app['name']} was running and isn't in your allowed list, so Echo quit it. If you didn't "
                    f"install it, remove it. To keep it, add {app['key']} to options/security/allowed.txt.")
            lines.append(f"QUIT: {app['name']} (pid {app['pid']}).")
            reminders.add(text, source, True, now=now)
            left.append(text)
        else:
            text = (f"{app['name']} is running (pid {app['pid']}) and isn't in your allowed list, but Echo couldn't "
                    f"quit it: {message}. To stop it: sudo kill {app['pid']}, then uninstall it if you didn't "
                    f"install it. To keep it, add {app['key']} to options/security/allowed.txt.")
            lines.append(f"QUIT FAILED: {app['name']}: {message}")
            if _remind(state, f"quit:{app['key']}", text, True, now, source):
                left.append(text)
    for key in plan["turned_off"]:
        text = f"{PROTECTIONS[key]} was on and is now off. If you didn't turn it off, turn it back on: {FIXES[key]}."
        lines.append(f"TURNED OFF: {PROTECTIONS[key]}")
        if _remind(state, f"off:{key}", text, True, now, source):
            left.append(text)
    if plan["new"]["startup"]:
        text = ("New startup item: " + ", ".join(plan["new"]["startup"]) + ". It starts with the Mac. If you didn't "
                "just install something that explains it, check what it runs.")
        lines.append("NEW STARTUP ITEMS: " + ", ".join(plan["new"]["startup"]))
        reminders.add(text, source, True, now=now)
        left.append(text)
    if plan["new"]["exposed"]:
        ports = "; ".join(f"{p} (ports {', '.join(map(str, facts['exposed'][p]))})" for p in plan["new"]["exposed"])
        text = f"New app open to your network: {ports}."
        lines.append("NEW ON THE NETWORK: " + ports)
        reminders.add(text, source, False, now=now)
        left.append(text)
    if plan["new"]["outbound"]:
        text = "New apps connected to the internet: " + ", ".join(plan["new"]["outbound"]) + "."
        lines.append("NEW INTERNET CONNECTIONS: " + ", ".join(plan["new"]["outbound"]))
        reminders.add(text, source, False, now=now)
        left.append(text)
    if len(lines) == 1:
        lines.append("Nothing new since the last scan.")
    _remember(state, facts)
    state["last_scan"] = now
    _save_state(state)
    lines += _posture(facts, allowed)
    return "\n".join(lines + _left(left))


def _posture(facts, allowed):
    on = [SERVICE_NAMES[s] + (" (allowed)" if s in allowed else "") for s in facts["services"]]
    apps = [a["name"] + (" (allowed)" if a["key"] in allowed else "") for a in facts["apps"]]
    settings = ", ".join(f"{PROTECTIONS[k]} {'on' if v else 'off' if v is False else 'unknown'}"
                         for k, v in facts["settings"].items())
    exposed = "; ".join(f"{p} ({', '.join(map(str, ports))})" for p, ports in facts["exposed"].items())
    return ["", "Current state:",
            f"- Remote access services: {', '.join(on) or 'none on'}",
            f"- Remote-control apps running: {', '.join(apps) or 'none'}",
            f"- Protections: {settings}",
            f"- Open to the network: {exposed or 'nothing'}",
            f"- Apps connected to the internet: {len(facts['outbound'])}",
            f"- Startup items: {len(facts['startup'])}"]


def _left(left):
    if not left:
        return ["", "Reminders left by the harness: none."]
    return ["", f"Reminders the harness left for {config.USER_NAME} (don't repeat them with remind):"] + [f"- {t}" for t in left]


def security_scan():
    """The tool."""
    try:
        return scan()
    except Exception as exc:  # a scan must never crash a task
        return f"Error: the scan failed: {type(exc).__name__}: {exc}"
