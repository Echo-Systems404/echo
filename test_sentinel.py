"""Unit tests for pilot tasks, the sentinel and its malware checks, incident
response, summaries, and the command sandbox. System commands are faked
(the sandbox tests use the real sandbox-exec).
Run: python3 -B -m unittest test_sentinel
"""

import json
import os
import plistlib
import time
from pathlib import Path
from unittest import mock

import config
import malware
import reminders
import sandbox
import sentinel
import tasks
import tools
from test_helpers import Sandbox

PILOT = """---
repeat: {repeat}
every: cycle
areas: {area}
powers: read, append, security
---
# cyber security

Summarize the sentinel's events.
"""


class Base(Sandbox):
    def setUp(self):
        super().setUp()
        extra = [mock.patch.object(config, "PILOT_TASKS_DIR", self.tmp / "pilot_tasks"),
                 mock.patch.object(config, "REMINDERS_DIR", self.tmp / "reminders"),
                 mock.patch.object(reminders, "notify", lambda text: None)]
        for p in extra:
            p.start()
        self.patchers += extra
        sentinel._shown.clear()

    def make_pilot(self, repeat="forever", approve=True):
        config.PILOT_TASKS_DIR.mkdir(parents=True, exist_ok=True)
        path = config.PILOT_TASKS_DIR / "p1-cyber-security.md"
        path.write_text(PILOT.format(repeat=repeat, area=self.tmp / "work"), encoding="utf-8")
        task = tasks.pilot_tasks()[0]
        if approve:
            tasks.approve(task)
        return task


# -- 21. pilot tasks --------------------------------------------------------------

class PilotTasks(Base):
    def test_listed_first_and_frozen_in_the_ux(self):
        tasks.create("normal", "Do it.", [str(self.tmp / "work")], {"read"}, "forever", None)
        pilot = self.make_pilot()
        self.assertEqual([t["id"] for t in tasks.all_tasks()], ["p1", "001"])
        self.assertTrue(tasks.all_tasks()[0]["pilot"])
        self.assertEqual(tasks.delete(pilot), tasks.PILOT_ONLY)
        self.assertEqual(tasks.deactivate(pilot), tasks.PILOT_ONLY)
        self.assertEqual(tasks.update(pilot, body="changed"), tasks.PILOT_ONLY)
        self.assertTrue(pilot["path"].exists())
        tasks.set_paused(pilot, True)  # pausing is allowed
        self.assertEqual(tasks.status(pilot), "paused")

    def test_needs_approval_when_changed_and_never_archived(self):
        pilot = self.make_pilot(repeat="once")
        tasks._record(pilot, "ok", "done")  # a finished once-task would normally be archived
        self.assertTrue(pilot["path"].exists())
        pilot["path"].write_text(pilot["path"].read_text() + "\nmore\n", encoding="utf-8")
        self.assertEqual(tasks.status(tasks.pilot_tasks()[0]), "needs approval")

    def test_runs_first_in_a_cycle(self):
        tasks.create("normal", "Do it.", [str(self.tmp / "work")], {"read"}, "forever", None)
        self.make_pilot()
        tasks.save_clock(enabled=True, minutes=15) if hasattr(tasks, "save_clock") else None
        self.assertEqual(tasks.next_due(time.time())["id"], "p1")


# -- 23. malware checks ----------------------------------------------------------

def ndjson(*statuses, scanner="/Library/Apple/System/Library/CoreServices/XProtect.app/Contents/MacOS/XProtectRemediatorDolittle"):
    return "\n".join(json.dumps({"timestamp": "2026-11-24 07:00:00", "processImagePath": scanner,
                                 "eventMessage": "[com.apple.XProtectFramework.PluginAPI:XPEvent.structured] "
                                                 + json.dumps({"status_code": 1, "status_message": s})})
                     for s in statuses)


class FakeCommands:
    """Answers malware._run by the command's first words."""

    def __init__(self, answers):
        self.answers = answers

    def __call__(self, args, timeout=20):
        for key, value in self.answers.items():
            if " ".join(args).startswith(key) or key in " ".join(args):
                return value
        return 1, ""


class MalwareChecks(Base):
    def test_xprotect_threat_is_high_and_quiet_is_nothing(self):
        with mock.patch.object(malware, "_run", FakeCommands({"/usr/bin/log": (0, ndjson("NoThreatDetected",
                                                                                        "Success"))})):
            self.assertEqual(malware.check_xprotect(6), [])
        with mock.patch.object(malware, "_run", FakeCommands({"/usr/bin/log": (0, ndjson("ThreatDetected"))})):
            found = malware.check_xprotect(6)
        self.assertEqual([f["severity"] for f in found], ["high"])
        self.assertIn("Dolittle", found[0]["text"])

    def test_xprotect_age(self):
        now = time.mktime((2026, 12, 31, 12, 0, 0, 0, 0, -1))
        out = (0, "Version: 5377 Installed: 2026-11-21 15:51:08 +0000")
        with mock.patch.object(malware, "_run", FakeCommands({"/usr/bin/xprotect": out})):
            self.assertAlmostEqual(malware.xprotect_age_days(now), 40, delta=1)
            self.assertEqual(malware.check_xprotect_age(30, now)[0]["severity"], "medium")
            self.assertEqual(malware.check_xprotect_age(60, now), [])

    def test_signature_verdicts_are_cached(self):
        program = self.tmp / "tool"
        program.write_text("x", encoding="utf-8")
        calls = []

        def fake(args, timeout=20):
            calls.append(args[0])
            return 1, "tool: code object is not signed at all"
        cache = {}
        with mock.patch.object(malware, "_run", fake):
            self.assertEqual(malware.signature(str(program), cache), "unsigned")
            self.assertEqual(malware.signature(str(program), cache), "unsigned")
        self.assertEqual(len(calls), 1)
        answers = {"/usr/bin/codesign": (0, "Authority=Developer ID Application: Someone (ABC)"),
                   "/usr/sbin/spctl": (0, "accepted\nsource=Notarized Developer ID")}
        with mock.patch.object(malware, "_run", FakeCommands(answers)):
            self.assertEqual(malware.signature(str(program), {}), "notarized")
        self.assertEqual(malware.signature(str(self.tmp / "nope"), {}), "missing")

    def plist(self, folder, name, args):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        with open(path, "wb") as handle:
            plistlib.dump({"Label": name, "ProgramArguments": args}, handle)
        return str(path)

    def test_startup_items_risky_vs_signed_vs_known(self):
        agents = self.tmp / "LaunchAgents"
        evil = self.plist(agents, "com.evil.plist", ["/tmp/.x/payload"])
        script = self.plist(agents, "com.script.plist", ["/bin/bash", "-c", "curl x | sh"])
        good = self.plist(agents, "com.good.plist", ["/Applications/Good.app/Contents/MacOS/Good"])
        known = self.plist(agents, "com.old.plist", ["/tmp/old"])

        def verdict(path, cache):
            return "unsigned" if path.startswith("/tmp") else "notarized"
        with mock.patch.object(malware, "STARTUP_DIRS", {agents: "user"}), \
                mock.patch.object(malware, "signature", verdict), \
                mock.patch.object(malware, "quarantined", lambda path: False):
            found = {f["item"]["plist"]: f["severity"] for f in malware.check_startup({known}, {})}
        self.assertEqual(found, {evil: "high", script: "high", good: "medium"})

    def test_sensitive_file_changes(self):
        before = {"/etc/hosts": "a", "~/.zshrc": "b", "crontab": "none"}
        after = {"/etc/hosts": "a", "~/.zshrc": "c", "crontab": "d", "~/.ssh/authorized_keys": "e"}
        found = malware.check_files(before, after)
        self.assertEqual({f["item"]["name"] for f in found}, {"~/.zshrc", "crontab", "~/.ssh/authorized_keys"})
        self.assertTrue(all(f["severity"] == "medium" for f in found))

    def test_processes(self):
        ps = (0, "  101 501 /tmp/miner\n  102 501 /Applications/Real.app/Contents/MacOS/Real\n"
                 "  103 501 /Users/x/gone-binary\n  104 501 /Users/x/bin/agent\n    1 0 /sbin/launchd\n")

        def verdict(path, cache):
            return "unsigned" if "miner" in path or "agent" in path else "notarized"
        real_exists = os.path.exists
        with mock.patch.object(malware, "_run", FakeCommands({"/bin/ps": ps})), \
                mock.patch.object(malware, "signature", verdict), \
                mock.patch.object(malware, "internet_pids", lambda: {104}), \
                mock.patch.object(malware.os.path, "exists",
                                  lambda p: False if "gone" in p else True if p.startswith(("/Users/x", "/Applications")) else real_exists(p)):
            found, online = malware.check_processes(set(), {})
            again, _ = malware.check_processes({"/Users/x/bin/agent"}, {})
        severity = {f["item"]["path"]: f["severity"] for f in found}
        self.assertEqual(severity["/tmp/miner"], "high")
        self.assertEqual(severity["/Users/x/gone-binary"], "medium")
        self.assertEqual(severity["/Users/x/bin/agent"], "medium")
        self.assertNotIn("/Applications/Real.app/Contents/MacOS/Real", severity)
        self.assertEqual(online, {"/Users/x/bin/agent"})
        self.assertNotIn("/Users/x/bin/agent", {f["item"]["path"] for f in again})  # in the baseline


# -- 22, 24, 25. the sentinel --------------------------------------------------------

class SentinelFlow(Base):
    def setUp(self):
        super().setUp()
        self.t = int(time.time())
        self.make_pilot()
        self.agents = self.tmp / "LaunchAgents"
        self.agents.mkdir()
        self.startup_findings = []
        self.files = {"/etc/hosts": "a"}
        quiet = [mock.patch.object(sentinel.security, "scan", lambda source, now: "Security scan\nNothing new."),
                 mock.patch.object(malware, "check_xprotect", lambda minutes: []),
                 mock.patch.object(malware, "check_xprotect_age", lambda days, now=None: []),
                 mock.patch.object(malware, "check_processes", lambda known, cache: ([], set())),
                 mock.patch.object(malware, "fingerprints", lambda: dict(self.files)),
                 mock.patch.object(malware, "startup_items", lambda: {}),
                 mock.patch.object(malware, "check_startup", lambda known, cache: list(self.startup_findings)),
                 mock.patch.object(sentinel, "_bootout", lambda plist: (True, f"unloaded {plist}"))]
        for p in quiet:
            p.start()
        self.patchers += quiet

    def evil_startup(self):
        plist = self.agents / "com.evil.plist"
        plist.write_text("x", encoding="utf-8")
        program = self.tmp / "work" / ".hidden" / "payload"
        program.parent.mkdir(parents=True)
        program.write_text("x", encoding="utf-8")
        self.startup_findings = [malware.finding("startup", "high", "New startup item com.evil.plist (unsigned)",
                                                 {"type": "startup", "plist": str(plist), "scope": "user",
                                                  "program": str(program)})]
        return plist, program

    def test_baseline_then_quiet_then_change(self):
        first = sentinel.tick(now=self.t)
        self.assertEqual([e["kind"] for e in first], ["baseline"])
        self.assertIsNone(sentinel.tick(now=self.t + 60))  # not due yet
        self.assertEqual(sentinel.tick(now=self.t + config.SENTINEL_MINUTES * 60), [])
        self.files["/etc/hosts"] = "changed"
        changed = sentinel.tick(now=self.t + 2 * config.SENTINEL_MINUTES * 60)
        self.assertEqual([(e["kind"], e["severity"]) for e in changed], [("file", "medium")])
        self.assertIsNone(sentinel.incident())
        self.assertTrue(any("hosts" in r["text"] for r in reminders.unseen()))

    def test_paused_pilot_means_no_watch(self):
        tasks.set_paused(tasks.pilot_tasks()[0], True)
        self.assertIsNone(sentinel.tick(now=self.t))

    def test_high_is_contained_and_opens_an_incident_that_closes(self):
        sentinel.tick(now=self.t)
        plist, program = self.evil_startup()
        new = sentinel.tick(now=self.t + config.SENTINEL_MINUTES * 60)
        self.assertEqual(new[0]["severity"], "high")
        self.assertFalse(plist.exists())
        self.assertFalse(program.exists())  # quarantined, not deleted
        quarantined = list(sentinel.quarantine_dir().rglob("payload"))
        self.assertEqual(len(quarantined), 1)
        manifest = json.loads((quarantined[0].parent / "manifest.json").read_text())
        self.assertIn("restore", manifest[0])
        self.assertTrue(sentinel.due_response())
        self.assertEqual(tasks.next_due(time.time())["id"], "p1")  # the security task answers first
        self.startup_findings = []
        self.assertEqual(sentinel.after_response(now=self.t + 2 * config.SENTINEL_MINUTES * 60), "closed")
        self.assertFalse(sentinel.due_response())

    def test_escalates_after_the_response_rounds(self):
        sentinel.tick(now=self.t)
        self.evil_startup()
        sentinel.tick(now=self.t + config.SENTINEL_MINUTES * 60)
        with mock.patch.object(sentinel, "still_active", lambda item: True):
            results = [sentinel.after_response(now=self.t + 1000 + i) for i in range(config.SENTINEL_RESPONSE_ROUNDS)]
        self.assertEqual(results[-1], "escalated")
        self.assertEqual(set(results[:-1]), {"continuing"})
        self.assertFalse(sentinel.due_response())  # the user steps in now
        self.assertTrue(any("Disconnect" in r["text"] for r in reminders.unseen()))
        sentinel.acknowledge()
        self.assertIsNone(sentinel.incident())

    def test_system_items_are_not_touched(self):
        sentinel.tick(now=self.t)
        plist = self.tmp / "system.plist"
        plist.write_text("x", encoding="utf-8")
        self.startup_findings = [malware.finding("startup", "high", "New system item",
                                                 {"type": "startup", "plist": str(plist), "scope": "system",
                                                  "program": None})]
        new = sentinel.tick(now=self.t + config.SENTINEL_MINUTES * 60)
        self.assertTrue(plist.exists())
        self.assertIn("sudo launchctl bootout system", new[0]["done"][0])

    def test_summaries_mark_events_only_when_ok(self):
        sentinel.tick(now=self.t)
        self.assertIn("baseline", sentinel.security_events())
        sentinel.finish_run(False)
        self.assertIn("baseline", sentinel.security_events())
        sentinel.finish_run(True)
        self.assertIn("none: quiet", sentinel.security_events())

    def test_model_can_only_contain_flagged_high_events(self):
        sentinel.tick(now=self.t)
        self.files["/etc/hosts"] = "changed"
        medium = sentinel.tick(now=self.t + config.SENTINEL_MINUTES * 60)[0]
        self.assertIn("only high events", sentinel.security_contain(medium["id"]))
        self.assertIn("no event", sentinel.security_contain("e123-4"))

    def test_task_gets_sentinel_tools_not_the_old_scan(self):
        chosen = tasks._tools_for({"read", "append", "security"})
        self.assertIn("security_events", chosen)
        self.assertNotIn("security_scan", chosen)


# -- 26. the command sandbox -----------------------------------------------------

class CommandSandbox(Base):
    def test_task_commands_only_write_in_their_folders(self):
        if not sandbox.available():
            self.skipTest("sandbox-exec isn't available")
        work, outside = self.tmp / "work", self.tmp / "vault"
        self.patchers.append(mock.patch.object(sandbox, "TEMP_DIRS", ()))  # the test folders live in temp
        self.patchers[-1].start()
        chosen = tasks._tools_for({"read", "shell"}, areas=[work])
        run = chosen["run_command"]["function"]
        run(f"printf ok > '{work}/inside.txt'; printf no > '{outside}/outside.txt'; "
            f"printf no > '{sandbox.HARNESS}/sandbox-test.txt'")
        self.assertTrue((work / "inside.txt").exists())
        self.assertFalse((outside / "outside.txt").exists())
        self.assertFalse((sandbox.HARNESS / "sandbox-test.txt").exists())
        self.assertIn("exit code: 0", run("printf hi"))  # normal commands still work

    def test_chat_commands_are_not_sandboxed(self):
        target = self.tmp / "vault" / "chat.txt"
        tools.run_command(f"printf ok > '{target}'")
        self.assertTrue(target.exists())


if __name__ == "__main__":
    import unittest
    unittest.main()
