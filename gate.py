"""The gate: deterministic safety checks around every tool call. No model involved.

It stays silent. Suspicious text is only logged, so asking about personality
or configuration never puts an alert in the model's thoughts. The model hears
from the gate only when a task's action is actually blocked, and then it gets
the matching security seed as the reason.

In chat there is no policy: the normal permission prompts apply. In a task the
policy decides, because nobody is there to answer a prompt.

The command patterns are best effort. A task given "shell" is trusted: a shell
can reach anything the user can, so folder limits only bind the file tools.
"""

import json
import re
import time
from collections import Counter, deque
from pathlib import Path

import communication
import config
import memory
import tools

LOOP_WINDOW = 6  # recent tool calls compared
LOOP_LIMIT = 3  # the same call with the same result this many times = stuck
SEED_MAX_CHARS = 1000

PATH_TOOLS = {"read_file", "write_file", "append_file", "list_directory", "search_files"}
READ_PATH_TOOLS = {"read_file", "list_directory", "search_files"}
RULE_POWERS = {"deletes files": "delete", "pushes or discards git history": "git"}
RULE_SEEDS = {"delete": "deleting", "git": "git"}

_INJECTION_PATTERNS = [re.compile(p, re.I) for p in (
    r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|context)",
    r"disregard\s+(your|all|the)\s+(previous|prior|system|instructions?)",
    r"(system\s+prompt|your\s+instructions?)\s+(have\s+been\s+)?(changed|updated|replaced|overridden)",
    r"new\s+persona[:\s]",
    r"forget\s+(everything|all|your\s+(previous|prior))",
    r"<\s*/?(?:system|instruction|prompt)\s*>",
    r"\[/?INST\]|<\|im_start\|>|<\|im_end\|>",
    r"print\s+(your\s+)?(system\s+prompt|instructions?)",
    r"(reveal|show|output|repeat)\s+(your\s+)?(full\s+)?system\s+prompt",
)]


def is_degenerate(tail):
    """True when streamed output has collapsed into a loop like '7, 7, 7, 7'.
    Only letters and digits count, so a long ---- divider in code is fine."""
    core = re.sub(r"[\s,.;]", "", tail)
    return len(core) >= 40 and core.isalnum() and len(set(core)) <= 2


def _inside(path, folder):
    return path == folder or folder in path.parents


class Gate:
    def __init__(self, policy=None, on_flag=None):
        """policy: None in chat, or {"areas": [Path], "powers": set} for a task
        or sub-agent ("harness_readable": True lets a sub-agent read the harness)."""
        self.policy = policy
        self.on_flag = on_flag
        self.harness = Path(config.__file__).resolve().parent
        self.start_run()

    def start_run(self):
        self.recent = deque(maxlen=LOOP_WINDOW)
        self.stuck = False
        self.flagged = False
        self.shell_off = False

    # -- before a tool runs ------------------------------------------------

    def check(self, name, args):
        """None to allow, or the message the model gets when it is blocked."""
        if not isinstance(args, dict):
            return None
        if name in PATH_TOOLS and communication.private(args.get("path", ".")):  # tokens and message logs
            return self._blocked("that's the harness's private message data. Past Telegram conversations "
                                 "are read with telegram_conversations and telegram_read.", "protected")
        if name in ("write_file", "append_file"):
            target = Path(str(args.get("path", ""))).expanduser().resolve()
            if _inside(target, Path(config.INTERACTION_DIR).resolve()):
                return self._blocked("interaction seeds have their own tools.", "interaction")
            if _inside(target, self.harness):  # the harness is the user's to change, in chat too
                return self._blocked("this path is inside the harness.", "harness" if not self.policy else "protected")
        if not self.policy:
            return None
        if name in PATH_TOOLS:
            return self._check_path(args.get("path", "."), reading=name in READ_PATH_TOOLS)
        if name == "run_command":
            return self._check_command(str(args.get("cmd", "")))
        return None

    def _check_path(self, raw, reading=False):
        path = Path(str(raw)).expanduser().resolve()  # also follows symlinks out of a folder
        # A sub-agent sent to review the harness itself may read it (never write).
        if _inside(path, self.harness) and not (reading and self.policy.get("harness_readable")):
            return self._blocked("this path is inside the harness.", "protected")
        if not any(_inside(path, area) for area in self.policy["areas"]):
            return self._blocked("this path is outside the task's folders.", "folders")
        return None

    def _check_command(self, cmd):
        if self.shell_off:
            return self._blocked("commands are off for this run after untrusted content was read.", "untrusted")
        if "shell" not in self.policy["powers"]:
            return self._blocked("this task can't run commands.", "commands")
        if tools.mentions_harness(cmd):
            return self._blocked("this command touches the harness.", "protected")
        for why, pattern in tools.PERMISSION_RULES:
            if pattern.search(cmd):
                power = RULE_POWERS.get(why)
                if power not in self.policy["powers"]:
                    return self._blocked(f"this command {why}.", RULE_SEEDS.get(power, "commands"))
        return None

    def _blocked(self, why, seed):
        self._log("blocked", why)
        text = memory.read_markdown(config.SECURITY_SEEDS_DIR / f"{seed}.md")[:SEED_MAX_CHARS]
        return f"Blocked: {why}" + (f"\n\n{text}" if text else "")

    # -- after a tool runs -------------------------------------------------

    def observe(self, name, args, result):
        """Silently flag injections (once per run) and notice a stuck loop."""
        if not self.flagged and any(p.search(result) for p in _INJECTION_PATTERNS):
            self.flagged = True
            self.shell_off = bool(self.policy)
            self._log("flagged", f"suspicious text in {name} output")
            if self.on_flag:
                self.on_flag()
        call = (name, json.dumps(args, sort_keys=True, default=str), result)
        self.recent.append(call)
        if Counter(self.recent)[call] >= LOOP_LIMIT:
            self.stuck = True
            self._log("loop", f"{name} repeated with the same result")

    def _log(self, kind, detail):
        try:
            config.GATE_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(config.GATE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind,
                                    "task": bool(self.policy), "detail": detail}) + "\n")
        except OSError:
            pass
