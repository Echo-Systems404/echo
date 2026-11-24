"""Unit tests for task options (saved tasks, edits), reminders, and the
security scan. No model and no real system changes: commands are faked.
Run: python3 -B -m unittest test_options_security
"""

import copy
import json
import time
from unittest import mock

import config
import gate
import reminders
import security
import tasks
from test_helpers import HARNESS, Sandbox

HOUR = 3600


class Base(Sandbox):
    def setUp(self):
        super().setUp()
        extra = [mock.patch.object(config, "REMINDERS_DIR", self.tmp / "reminders"),
                 mock.patch.object(reminders, "notify", lambda text: self.notified.append(text))]
        self.notified = []
        for p in extra:
            p.start()
        self.patchers += extra


# -- 6. task options ----------------------------------------------------------

class TaskOptions(Base):
    def make(self, name):
        return tasks.create(name, f"Do {name}.\nMore lines.", [str(self.tmp / "work")], {"read", "append"}, "forever",
                            None)

    def ids(self):
        return [t["path"].name for t in tasks.all_tasks()]

    def test_deactivate_moves_to_saved_and_renumbers(self):
        self.make("alpha"), self.make("beta"), self.make("gamma")
        beta = tasks.all_tasks()[1]
        tasks._record(beta, "ok", "first run")
        tasks.TaskNotes(beta["id"]).write("half done")
        self.assertIsNone(tasks.deactivate(beta))
        self.assertEqual(self.ids(), ["001-alpha.md", "002-gamma.md"])
        saved = tasks.saved_tasks()
        self.assertEqual([t["id"] for t in saved], ["beta"])
        self.assertEqual(tasks.saved_status(saved[0]), "saved")
        self.assertEqual(tasks.runs(saved[0]), 1)
        self.assertEqual(tasks.brief(saved[0]), "Do beta.")
        self.assertTrue((config.TASKS_DIR / "saved" / "logs" / "beta.md").exists())
        self.assertTrue((config.TASKS_DIR / "saved" / "notes" / "beta.md").exists())
        self.assertNotIn("002", {k for k, v in tasks._state().items() if v.get("runs")})  # gamma has no runs

    def test_missing_model_pauses_and_reminds_instead_of_failing_each_cycle(self):
        (self.tmp / "work").mkdir(exist_ok=True)
        task = tasks.create("thought", "Think.", [str(self.tmp / "work")], {"read"}, "forever", None, model="gone:1b")
        driver = mock.Mock()
        driver.models.return_value = ["here:3b"]
        with mock.patch.object(tasks.engine.AgentEngine, "driver", driver, create=True):
            result, text = tasks.run(task, "here:3b", 4096)
        self.assertEqual(result, "failed")
        self.assertTrue(text.startswith(tasks.MISSING_MODEL))
        self.assertEqual(tasks.status(task), "paused")
        self.assertEqual([t["id"] for t in tasks.missing_model_tasks()], ["001"])
        self.assertIn("gone:1b", reminders.unseen()[0]["text"])
        self.assertIsNone(tasks.next_due(time.time()))  # the clock stops retrying it
        tasks.set_paused(tasks.update(task, model="here:3b"), False)  # the user picked another
        self.assertEqual(tasks.missing_model_tasks(), [])
        self.assertEqual(tasks.status(tasks.all_tasks()[0]), "active")

    def test_activate_goes_to_the_end_and_stays_approved(self):
        self.make("alpha"), self.make("beta"), self.make("gamma")
        beta = tasks.all_tasks()[1]
        tasks._record(beta, "ok", "ran")
        tasks.set_paused(tasks.all_tasks()[1], True)
        tasks.deactivate(tasks.all_tasks()[1])
        self.assertIsNone(tasks.activate(tasks.saved_tasks()[0]))
        self.assertEqual(self.ids(), ["001-alpha.md", "002-gamma.md", "003-beta.md"])
        back = tasks.all_tasks()[2]
        self.assertEqual(tasks.status(back), "active")  # approved, and no longer paused
        self.assertEqual(tasks._state()["003"]["runs"], 1)
        self.assertTrue(tasks._log_path("003-beta").exists())
        self.assertEqual(tasks.saved_tasks(), [])

    def test_edits_stay_approved_but_outside_edits_dont(self):
        task = self.make("alpha")
        task = tasks.update(task, body="New instructions.", model="gemma4:12b", every="15m", repeat="forever")
        self.assertEqual(task["body"], "New instructions.")
        self.assertEqual(task["model"], "gemma4:12b")
        self.assertEqual(task["every"], 900)
        self.assertEqual(tasks.status(task), "active")
        task = tasks.update(task, repeat=1, every=None, powers={"read", "security"})
        self.assertEqual((task["repeat"], task["every"]), (1, None))
        self.assertIn("security", task["powers"])
        self.assertEqual(tasks.status(task), "active")
        task["path"].write_text(task["path"].read_text() + "sneaky\n")
        self.assertEqual(tasks.status(tasks.all_tasks()[0]), "needs approval")

    def test_saved_task_edit_and_delete(self):
        self.make("alpha")
        tasks.deactivate(tasks.all_tasks()[0])
        saved = tasks.update(tasks.saved_tasks()[0], name="Alpha two")
        self.assertEqual((saved["name"], tasks.saved_status(saved)), ("Alpha two", "saved"))
        tasks.delete_saved(saved)
        self.assertEqual(tasks.saved_tasks(), [])
        record = json.loads((config.TASKS_DIR / "done.jsonl").read_text().splitlines()[-1])
        self.assertEqual((record["name"], record["how"]), ("Alpha two", "deleted"))

    def test_changes_wait_while_a_task_runs(self):
        task = self.make("alpha")
        lock = tasks._run_lock()
        try:
            self.assertEqual(tasks.deactivate(task), tasks.BUSY)
            self.assertEqual(tasks.update(task, body="x"), tasks.BUSY)
            self.assertEqual(tasks.delete(task), tasks.BUSY)
        finally:
            lock.close()
        self.assertEqual(len(tasks.all_tasks()), 1)

    def test_same_name_saved_twice(self):
        self.make("alpha")
        tasks.deactivate(tasks.all_tasks()[0])
        self.make("alpha")
        tasks.deactivate(tasks.all_tasks()[0])
        self.assertEqual(sorted(t["id"] for t in tasks.saved_tasks()), ["alpha", "alpha-2"])

    def test_security_power_gives_the_scan(self):
        self.assertIn("security_events", tasks._tools_for({"read", "security"}))  # the sentinel's log
        self.assertNotIn("security_scan", tasks._tools_for({"read"}))


# -- 7. reminders -------------------------------------------------------------

class Reminders(Base):
    def test_add_dedupe_seen(self):
        self.assertTrue(reminders.add("Check the router.", "task 001").startswith("Reminder saved"))
        self.assertIn("already waiting", reminders.add("Check the router.", "task 002"))
        waiting = reminders.unseen()
        self.assertEqual(len(waiting), 1)
        reminders.mark_seen({waiting[0]["id"]})
        self.assertEqual(reminders.unseen(), [])
        self.assertTrue(reminders.add("Check the router.", "task 001", now=time.time() + 1).startswith("Reminder saved"))

    def test_urgent_notifies_and_leads_the_prompt(self):
        reminders.add("Normal thing.", "task 001")
        reminders.add("Screen Sharing was turned off.", "security scan", urgent=True)
        self.assertEqual(self.notified, ["Screen Sharing was turned off."])
        section = reminders.prompt_section(reminders.unseen())
        self.assertLess(section.index("Urgent:"), section.index("Normal:"))
        self.assertEqual(reminders.summary(reminders.unseen()), "2 reminders waiting (1 urgent)")

    def test_per_run_cap_and_size(self):
        tool = reminders.task_tool("task 003: news")["function"]
        for i in range(config.REMINDERS_PER_RUN):
            self.assertTrue(tool(f"item {i}").startswith("Reminder saved"))
        self.assertTrue(tool("one more").startswith("Error"))
        self.assertTrue(reminders.add("x" * (config.REMINDER_MAX_CHARS + 1), "chat").startswith("Error"))

    def test_old_reminders_pruned(self):
        old = time.time() - (config.REMINDER_KEEP_DAYS + 1) * 86400
        reminders.add("Ancient.", "chat", now=old)
        reminders.add("Fresh.", "chat")
        reminders.prune()
        self.assertEqual([i["text"] for i in reminders._load()], ["Fresh."])

    def test_only_the_harness_writes_reminders(self):
        g = gate.Gate()
        self.assertTrue(g.check("append_file", {"path": str(HARNESS / "reminders" / "reminders.jsonl"),
                                                "content": "{}"}))


# -- 8. security scan ---------------------------------------------------------

NETSTAT_TCP = """Active Internet connections (including servers)
Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)          rxbytes      txbytes  rhiwat  shiwat          process:pid    state  options           gencnt    flags   flags1 usecnt rtncnt fltrs
tcp4       0      0  127.0.0.1.11434        *.*                    LISTEN                 0            0  131072  131072           ollama:16262  00100 00000006 00000000000a1fa6 00000000 00000800      1      0 000000
tcp4       0      0  *.5900                 *.*                    LISTEN                 0            0  131072  131072          launchd:1  00100 00000006 0000000000001907 00000001 00000800      1      0 000000
tcp46      0      0  *.7000                 *.*                    LISTEN                 0            0  131072  131072    ControlCenter:709    00100 00000006 0000000000001906 00000001 00000800      1      0 000000
tcp6       0      0  2600:8800:181b:8.59597 2606:50c0:8003::.443   ESTABLISHED         3856         2550  131072  131376  Obsidian Helper:30892  00102 00000008 00000000000ddc87 00000080 04000900      2      0 000000
tcp4       0      0  192.168.1.20.50000     192.168.1.5.8009       ESTABLISHED         3856         2550  131072  131376  Google Home:333  00102 00000008 00000000000ddc87 00000080 04000900      2      0 000000
"""
NETSTAT_UDP = """udp4       0      0  *.3283                 *.*                                           0         8367  786896    9216          ARDAgent:389    00180 00000000 00000000000db6e8 00000000 04000800      1      0 000002
"""


class SecurityScan(Base):
    def setUp(self):
        super().setUp()
        self.facts = {"services": [], "exposed": {"ControlCenter": [5000, 7000]}, "outbound": ["Obsidian Helper"],
                      "apps": [], "settings": {"firewall": True, "stealth": False, "filevault": True,
                                               "gatekeeper": True, "sip": True},
                      "startup": ["/Library/LaunchAgents/com.google.keystone.agent.plist"]}
        self.sudo_calls, self.sudo_ok, self.quit_calls = [], True, []
        extra = [mock.patch.object(security, "collect", lambda: copy.deepcopy(self.facts)),
                 mock.patch.object(security, "_sudo", self.fake_sudo),
                 mock.patch.object(security, "_quit", lambda pid: (self.quit_calls.append(pid) or True, ""))]
        for p in extra:
            p.start()
        self.patchers += extra

    def fake_sudo(self, args):
        self.sudo_calls.append(args)
        return (True, "") if self.sudo_ok else (False, "a password is required")

    def texts(self):
        return [(i["urgency"], i["text"]) for i in reminders._load()]

    def test_parse_netstat(self):
        tcp = security.parse_netstat(NETSTAT_TCP, "tcp")
        self.assertEqual([r["process"] for r in tcp], ["ollama", "launchd", "ControlCenter", "Obsidian Helper",
                                                       "Google Home"])
        self.assertEqual((tcp[1]["local"], tcp[1]["port"], tcp[1]["state"]), ("*", 5900, "LISTEN"))
        self.assertFalse(security._is_private(tcp[3]["foreign"]))
        self.assertTrue(security._is_private(tcp[4]["foreign"]))
        udp = security.parse_netstat(NETSTAT_UDP, "udp")
        self.assertEqual((udp[0]["port"], udp[0]["process"]), (3283, "ARDAgent"))

    def test_collect_reads_remote_access(self):
        outputs = {"tcp": NETSTAT_TCP, "udp": NETSTAT_UDP}

        def fake_run(args, timeout=15):
            if args[0] == "netstat":
                return outputs[args[-1]]
            if args[0] == "ps":
                return ("  12 501 /System/Library/PrivateFrameworks/CoreParsec.framework/parsecd\n"
                        "  13 501 /Applications/AnyDesk.app/Contents/MacOS/AnyDesk\n")
            return "Firewall is disabled. (State = 0)" if "--getglobalstate" in args else "enabled"

        self.patchers[-3].stop()  # use the real collect
        try:
            with mock.patch.object(security, "_run", fake_run):
                facts = security.collect()
        finally:
            self.patchers[-3].start()
        self.assertEqual(facts["services"], ["remote-management", "screen-sharing"])
        self.assertEqual(facts["exposed"], {"ControlCenter": [7000]})
        self.assertEqual(facts["outbound"], ["Obsidian Helper"])
        self.assertEqual([a["name"] for a in facts["apps"]], ["AnyDesk"])  # Apple's parsecd is skipped
        self.assertIs(facts["settings"]["firewall"], False)

    def test_first_scan_sets_a_baseline_and_allows_what_is_on(self):
        self.facts["services"] = ["remote-login"]
        self.facts["settings"]["firewall"] = False
        report = security.scan()
        self.assertIn("Baseline set", report)
        self.assertEqual(self.sudo_calls, [])
        self.assertEqual(security.load_allowed(), {"remote-login", "firewall-off"})
        self.assertTrue(security.sudoers_path().exists())
        security.scan()
        self.assertEqual(self.sudo_calls, [])  # allowed things are never cut

    def test_unallowed_remote_access_is_cut(self):
        security.scan()
        self.facts["services"] = ["screen-sharing"]
        report = security.scan()
        self.assertEqual(self.sudo_calls, security.CUTS["screen-sharing"])
        self.assertIn("CUT: Screen Sharing", report)
        self.assertEqual(self.texts()[-1][0], "urgent")
        self.assertTrue(self.notified)

    def test_firewall_turned_back_on(self):
        security.scan()
        self.facts["settings"]["firewall"] = False
        security.scan()
        self.assertEqual(self.sudo_calls, security.CUTS["firewall-off"])

    def test_failed_cut_reminds_at_most_daily(self):
        security.scan()
        self.facts["services"] = ["remote-login"]
        self.sudo_ok = False
        now = time.time()
        report = security.scan(now=now)
        self.assertIn("CUT FAILED", report)
        self.assertIn("sudo /bin/launchctl disable system/com.openssh.sshd", self.texts()[-1][1])
        count = len(self.texts())
        security.scan(now=now + HOUR)
        self.assertEqual(len(self.texts()), count)
        reminders.mark_seen({i["id"] for i in reminders._load()})  # the user saw it; it's still on a day later
        security.scan(now=now + (config.SECURITY_REPEAT_HOURS + 1) * HOUR)
        self.assertEqual(len(self.texts()), count + 1)

    def test_remote_app_quit_only_if_mine_and_not_allowed(self):
        security.scan()
        self.facts["apps"] = [{"key": "anydesk", "name": "AnyDesk", "pid": 77, "mine": True},
                              {"key": "teamviewer", "name": "TeamViewer", "pid": 88, "mine": False}]
        report = security.scan()
        self.assertEqual(self.quit_calls, [77])
        self.assertIn("QUIT FAILED: TeamViewer", report)
        security.allowed_path().write_text("anydesk\nteamviewer\n")
        self.quit_calls.clear()
        security.scan(now=time.time() + 2 * 86400)
        self.assertEqual(self.quit_calls, [])

    def test_new_things_reported_once(self):
        security.scan()
        self.facts["startup"].append("/Library/LaunchDaemons/com.evil.plist")
        self.facts["outbound"].append("curl")
        self.facts["exposed"]["nc"] = [4444]
        security.scan()
        urgency = dict((t, u) for u, t in self.texts())
        self.assertTrue(any("com.evil.plist" in t and u == "urgent" for t, u in urgency.items()))
        self.assertTrue(any("curl" in t and u == "normal" for t, u in urgency.items()))
        self.assertTrue(any("nc (ports 4444)" in t for t in urgency))
        count = len(self.texts())
        self.assertIn("Nothing new", security.scan())
        self.assertEqual(len(self.texts()), count)

    def test_protection_turned_off_is_urgent_and_never_cut(self):
        security.scan()
        self.facts["settings"]["gatekeeper"] = False
        report = security.scan()
        self.assertIn("TURNED OFF: Gatekeeper", report)
        self.assertEqual(self.sudo_calls, [])
        self.assertIn("spctl --master-enable", self.texts()[-1][1])

    def test_sudoers_allows_only_the_cuts(self):
        text = security.write_sudoers().read_text()
        allowed = [line.strip(" ,\\") for line in text.split("=", 1)[1].split("\n") if line.strip().startswith("/")]
        expected = [" ".join(c) for steps in security.CUTS.values() for c in steps]
        self.assertEqual(sorted(allowed), sorted(expected))
        self.assertNotIn("ALL\n", text.replace("ALL=(root)", ""))
