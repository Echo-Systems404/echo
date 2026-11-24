"""Unit tests for the protected harness, seeds, context resets, and the task
clock. No model needed: everything runs in temporary folders with a scripted
fake model. Run: python3 -B -m unittest test_memory_clock
"""

import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

import config
import engine
import gate
import inference
import interaction
import memory
import notes
import tasks
import tools
from test_helpers import FakeModel, HARNESS, Sandbox

DAY = 86400


# -- 1. protected harness ---------------------------------------------------

class ProtectedHarness(Sandbox):
    def test_chat_file_tools_blocked_in_harness(self):
        g = gate.Gate()
        for name in ("write_file", "append_file"):
            for path in ("gate.py", str(HARNESS / "seeds/security/git.md"), str(HARNESS / "options/tasks/.state.json")):
                with mock.patch("os.getcwd", return_value=str(HARNESS)):
                    blocked = g.check(name, {"path": str(HARNESS / path) if not path.startswith("/") else path,
                                             "content": "x"})
                self.assertTrue(blocked and blocked.startswith("Blocked"), (name, path))
        self.assertIsNone(g.check("write_file", {"path": str(self.tmp / "work/a.txt"), "content": "x"}))

    def test_symlink_into_harness_blocked(self):
        link = self.tmp / "work" / "sneaky"
        link.symlink_to(HARNESS)
        self.assertTrue(gate.Gate().check("write_file", {"path": str(link / "gate.py"), "content": "x"}))

    def test_task_also_blocked(self):
        g = gate.Gate(policy={"areas": [self.tmp / "work"], "powers": {"read", "write", "shell"}})
        self.assertTrue(g.check("write_file", {"path": str(HARNESS / "tools.py"), "content": "x"}))
        self.assertTrue(g.check("run_command", {"cmd": f"cat '{HARNESS}/gate.py'"}))

    def test_commands_near_harness(self):
        here = os.getcwd()
        try:
            os.chdir(HARNESS)
            for cmd in ("mv gate.py x", "echo x > tools.py", "git restore .", "git checkout -- .",
                        "sed -i '' s/a/b/ gate.py", "python3 -c 'open(\"gate.py\",\"w\")'", "cp a gate.py"):
                self.assertIsNotNone(tools.run_command_needs_permission(cmd), cmd)
            for cmd in ("ls", "git status", "git log --oneline", "grep -n x *.py 2>/dev/null", "cat gate.py 2>&1"):
                self.assertIsNone(tools.run_command_needs_permission(cmd), cmd)
            os.chdir(self.tmp / "work")
            self.assertIsNone(tools.run_command_needs_permission("mv a b"))
            self.assertIsNotNone(tools.run_command_needs_permission(f"mv '{HARNESS}/gate.py' /tmp/x"))
            home_form = str(HARNESS).replace(str(Path.home()), "~", 1)
            self.assertIsNotNone(tools.run_command_needs_permission(f"cp x {home_form.replace(' ', chr(92) + ' ')}/a"))
        finally:
            os.chdir(here)


# -- 2. seeds ---------------------------------------------------------------

class Seeds(Sandbox):
    def start(self, name):
        self.in_conversation(name)
        interaction.session_start()

    def state(self):
        return interaction._load()["seeds"]

    def test_created_per_conversation_and_topics(self):
        self.start("conv-a")
        self.assertIn("conv-a", self.state())
        self.assertIn("Seed conv-a updated (t1)", interaction.interaction_seed("topic", "redesign, calm palette", title="Website"))
        interaction.interaction_seed("topic", "react native idea", title="App")
        interaction.interaction_seed("add", "picked a domain", topic="t1")
        interaction.interaction_seed("title", "Website, app, and the cat")
        text = interaction.interaction_recall("conv-a")
        self.assertIn("# Website, app, and the cat", text)
        self.assertIn("## t1 · Website", text)
        self.assertIn("picked a domain", text)
        self.assertIn("Error", interaction.interaction_seed("add", "x"))  # no topic named
        self.assertIn("Error", interaction.interaction_seed("prune", "redesign, calm palette\npicked a domain", topic="t1"))

    def test_no_model_tool_creates_or_deletes_seeds(self):
        names = set(tools.TOOLS) | set(tools.SEED_TASK_TOOLS)
        self.assertNotIn("interaction_manage", names)
        self.assertNotIn("vault_write", names)

    def test_per_topic_references_and_promotion(self):
        self.start("conv-a")
        interaction.interaction_seed("topic", "redesign", title="Website")
        interaction.interaction_seed("topic", "idea", title="App")
        interaction.interaction_seed("topic", "likes boxes", title="Cat")
        interaction.interaction_recall("conv-a", "t1")  # its own conversation: no reference
        self.start("conv-b")
        interaction.interaction_recall("conv-a")  # the whole seed: no reference
        interaction.interaction_recall("conv-a", "t1")
        interaction.interaction_recall("conv-a", "t1")  # twice in one conversation counts once
        interaction.interaction_recall("conv-a", "t2")
        self.assertEqual(self.state()["conv-a"]["topics"]["t1"]["refs"], ["conv-b"])
        self.in_conversation("task-runner")
        interaction.recall_for_task("conv-a", "t1")  # tasks never count
        self.assertEqual(len(self.state()["conv-a"]["topics"]["t1"]["refs"]), 1)
        self.start("conv-c")
        result = interaction.interaction_recall("conv-a", "t1")
        self.assertIn("now long term", result)
        seeds = self.state()
        long_ids = [s for s, e in seeds.items() if e["tier"] == "long"]
        self.assertEqual(len(long_ids), 1)
        self.assertTrue(long_ids[0].startswith("website-"))
        long_text = interaction.interaction_recall(long_ids[0])
        self.assertIn("redesign", long_text)
        self.assertIn("From: conv-a t1", long_text)
        origin = interaction.interaction_recall("conv-a")
        self.assertIn(f"Moved to long term: {long_ids[0]}", origin)
        self.assertIn("## t2 · App", origin)
        self.assertIn("likes boxes", origin)
        self.assertIn("Error", interaction.interaction_seed("add", "x", topic="t1", id="conv-a"))
        self.assertEqual(seeds["conv-a"]["topics"]["t2"]["refs"], ["conv-b"])  # not promoted yet

    def test_expiry(self):
        self.start("conv-a")
        interaction.interaction_seed("topic", "x", title="Cat")
        now = time.time()
        interaction.maintain(now=now + 13 * DAY)
        self.assertIn("conv-a", self.state())
        interaction.maintain(now=now + 15 * DAY)
        self.assertNotIn("conv-a", self.state())

    def test_long_term_waits_for_consolidation(self):
        self.start("conv-a")
        interaction.interaction_seed("topic", "x", title="Website")
        for other in ("conv-b", "conv-c"):
            self.start(other)
            interaction.interaction_recall("conv-a", "t1")
        long_id = next(s for s, e in self.state().items() if e["tier"] == "long")
        later = time.time() + 31 * DAY
        interaction.maintain(now=later)
        self.assertIn(long_id, self.state())  # no consolidation has run since it expired
        interaction.mark_consolidated(now=later + 60)
        self.assertNotIn(long_id, self.state())

    def test_review_and_task_edits_restart_window(self):
        self.start("conv-a")
        interaction.interaction_seed("topic", "x", title="Website")
        for other in ("conv-b", "conv-c"):
            self.start(other)
            interaction.interaction_recall("conv-a", "t1")
        long_id = next(s for s, e in self.state().items() if e["tier"] == "long")
        with mock.patch("time.time", return_value=time.time() + 20 * DAY):
            self.assertIn("Reviewed", interaction.interaction_review(long_id))
        self.assertGreater(self.state()[long_id]["expires"], time.time() + 45 * DAY)
        self.assertIn("Error", interaction.interaction_review("conv-a"))
        self.assertIn("updated", interaction.edit_for_task("add", "consolidated", topic="t1", id=long_id))
        self.assertIn("Error", interaction.edit_for_task("add", "x", topic="t1"))  # tasks must name the seed

    def test_empty_seed_from_another_conversation_removed(self):
        self.start("conv-a")
        self.start("conv-b")
        interaction.maintain(now=time.time() + 2 * 3600)
        self.assertNotIn("conv-a", self.state())
        self.assertIn("conv-b", self.state())  # the current conversation keeps its seed

    def test_memory_save_two_files(self):
        self.start("conv-a")
        interaction.interaction_seed("topic", "shipped v1", title="Website")
        interaction.interaction_seed("topic", "likes boxes", title="Cat")
        result = interaction.memory_save("Website launch", "Projects/Website launch.md",
                                         "## Summary\nShipped.\n\n## Footnotes\n1. hosting", "conv-a t1")
        self.assertIn("Saved the memory", result)
        vault_id = next(s for s, e in self.state().items() if e["tier"] == "vault")
        self.assertTrue(vault_id.startswith("website-launch-"))
        note = (self.tmp / "vault" / "Projects" / "Website launch.md").read_text()
        self.assertTrue(note.rstrip().endswith(f"Location: memory vault seeds/{vault_id}.md"))
        self.assertIn(f"Echo seed: {vault_id}", note)
        seed = interaction.interaction_recall(vault_id)
        self.assertIn("Note: [[Projects/Website launch]]", seed)
        self.assertIn("shipped v1", seed)
        self.assertIn(f"Saved to the vault: {vault_id}", interaction.interaction_recall("conv-a", "t1"))
        # frozen outside chat, permanent, listed first
        self.assertIn("frozen", interaction.edit_for_task("add", "x", topic="t1", id=vault_id))
        self.assertIn("updated", interaction.interaction_seed("add", "with the user", topic="t1", id=vault_id))
        interaction.maintain(now=time.time() + 400 * DAY)
        self.assertIn(vault_id, self.state())
        self.assertTrue(interaction.interaction_search().startswith(f"- {vault_id}"))
        # never replaces a note; can link an existing one
        self.assertIn("never replaced", interaction.memory_save("Again", "Projects/Website launch.md", "new text"))
        self.assertIn("Saved the memory", interaction.memory_save("Link it", "Projects/Website launch.md"))

    def test_memory_save_rejects_bad_sources_without_writing(self):
        self.start("conv-a")
        self.assertIn("Error", interaction.memory_save("X", "x.md", "text", "nope t1"))
        self.assertFalse((self.tmp / "vault" / "x.md").exists())

    def test_user_seeds_in_prompt(self):
        self.start("conv-a")
        interaction.user_seeds("add", "has a cat")
        self.assertEqual(interaction.snapshot()["user"], "")  # fixed for this conversation
        interaction.refresh_snapshot()
        prompt = memory.build_system_prompt("base", self.tmp / "none", self.tmp / "none",
                                            user_seeds=interaction.snapshot()["user"])
        self.assertIn("has a cat", prompt)


# -- 3. context resets ------------------------------------------------------

def call(name, **args):
    return {"function": {"name": name, "arguments": args}}


class ContextResets(Sandbox):
    def test_memory_layer_update_flushes_and_continues(self):
        self.in_conversation("conv-a")
        interaction.session_start()
        out = self.tmp / "work" / "a.txt"
        mem = memory.ConversationMemory(system_prompt="SYSTEM")
        asked = []
        agent = FakeModel([
            (850, {"tool_calls": [call("write_file", path=str(out), content="hello")]}),
            (900, {"tool_calls": [call("transient_edit", action="new", title="Atlas", content="Project: Atlas, port 5173")]}),
            (920, {"tool_calls": [call("ephemeral_save", title="Atlas", summary="Built a.txt for Atlas.")]}),
            (930, {"content": "done"}),
            (120, {"content": "Finished."}),
        ], tools=tools.TOOLS, memory=mem, on_full=lambda p: asked.append(p) or "update")
        self.assertEqual(agent.run("build atlas"), "Finished.")
        self.assertEqual(asked, [85])
        self.assertIn("Project: Atlas, port 5173", mem.carry)
        self.assertIn("Built a.txt for Atlas.", mem.carry)
        last = agent.sent[-1]
        self.assertEqual(len(last), 2)  # system + the continue note: the old context is gone
        self.assertIn("Project: Atlas", last[0]["content"])
        self.assertIn(str(out), last[1]["content"])
        self.assertIn("Don't redo finished steps", last[1]["content"])
        self.assertEqual(agent.last_tokens, 120)

    def test_reset_before_a_new_request_keeps_the_request(self):
        mem = memory.ConversationMemory(system_prompt="SYSTEM")
        agent = FakeModel([(10, {"content": "The summary."}), (50, {"content": "Answer."})],
                          tools=tools.TOOLS, memory=mem, on_full=lambda p: "compact")
        agent.last_tokens = 900  # the previous request left the context 90% full
        self.assertEqual(agent.run("next question"), "Answer.")
        self.assertIn("The summary.", mem.carry)
        self.assertEqual(agent.sent[-1][-1]["content"], "next question")

    def test_not_now_asks_once_per_request(self):
        mem = memory.ConversationMemory(system_prompt="SYSTEM")
        asked = []
        agent = FakeModel([(850, {"tool_calls": [call("clock")]}), (900, {"tool_calls": [call("clock")]}),
                           (950, {"content": "ok"})],
                          tools=tools.TOOLS, memory=mem, on_full=lambda p: asked.append(p) or "later")
        agent.run("q")
        self.assertEqual(len(asked), 1)
        self.assertEqual(mem.carry, "")

    def test_task_notes_flush_and_time_limit(self):
        store = tasks.TaskNotes("001")
        out = self.tmp / "work" / "site.html"
        task_tools = {**tools.TOOLS, "task_notes": tasks._notes_tool(store)}
        mem = memory.ConversationMemory(system_prompt="SYSTEM")
        agent = FakeModel([
            (850, {"tool_calls": [call("write_file", path=str(out), content="<html>")]}),
            (900, {"tool_calls": [call("task_notes", content="Header done; footer next.")]}),
            (910, {"content": "ready"}),
            (100, {"content": "Report: done."}),
        ], tools=task_tools, memory=mem, task_notes=store)
        self.assertEqual(agent.run("build a site"), "Report: done.")
        note = agent.sent[-1][-1]["content"]
        self.assertIn("Header done; footer next.", note)
        self.assertIn(str(out), note)
        self.assertIn("build a site", note)

        agent = FakeModel([(10, {"tool_calls": [call("task_notes", content="Stopped mid-footer.")]}),
                           (20, {"content": "ready"})],
                          tools=task_tools, memory=memory.ConversationMemory(), task_notes=store,
                          deadline=time.monotonic() - 1)
        self.assertTrue(agent.run("build a site").startswith(engine.TIME_LIMIT_TEXT))
        self.assertEqual(store.read(), "Stopped mid-footer.")

    def test_compaction_keeps_earlier_summary(self):
        mem = memory.ConversationMemory()
        mem.add("user", "FIRST-FACT")
        for i in range(60):
            mem.add("assistant" if i % 2 else "user", f"message {i}")
            mem.compact(20)
        self.assertIn("FIRST-FACT", mem.history[0]["content"])


# -- 4. the clock's cycles --------------------------------------------------

class Cycles(Sandbox):
    def setUp(self):
        super().setUp()
        tasks.save_clock(True, 60, False)
        self.area = str(self.tmp / "work")

    def make(self, name, repeat="forever", every=None):
        return tasks.create(name, f"Do {name}.", [self.area], {"read"}, repeat, every)

    def finish(self, task, result="ok"):
        tasks._record(task, result, f"{task['name']} report")

    def test_cycle_runs_in_order_then_waits(self):
        for name in ("one", "two", "three"):
            self.make(name)
        now = time.time()
        order = []
        while True:
            task = tasks.next_due(now)
            if task is None:
                break
            order.append(task["id"])
            self.finish(task)
        self.assertEqual(order, ["001", "002", "003"])
        self.assertIsNone(tasks.next_due(now + 30 * 60))  # mid-interval: nothing
        self.assertEqual(tasks.next_due(now + 61 * 60)["id"], "001")  # next cycle

    def test_overrun_collapses_missed_cycles(self):
        self.make("slow")
        start = time.time()
        task = tasks.next_due(start)
        self.finish(task)  # pretend it took 5 hours: 4 cycles were missed
        self.assertIsNone(tasks.next_due(start + 5 * 3600))  # closes the overrun cycle
        self.assertEqual(tasks.next_due(start + 5 * 3600 + 1)["id"], "001")  # one cycle, right away
        self.assertTrue(tasks._cycle()["open"])
        self.assertEqual(tasks._cycle()["done"], [])

    def test_every_and_task_notes(self):
        self.make("often")
        self.make("daily", every="24h")
        now = time.time()
        for _ in range(2):
            self.finish(tasks.next_due(now))
        self.assertIsNone(tasks.next_due(now))
        self.assertEqual(tasks.next_due(now + 2 * 3600)["id"], "001")
        self.finish(tasks.next_due(now + 2 * 3600))
        self.assertIsNone(tasks.next_due(now + 2 * 3600))  # daily ran less than 24h ago
        tasks.TaskNotes("002").write("half done")
        self.assertEqual(tasks.next_due(now + 4 * 3600 + 1)["id"], "001")
        self.finish(tasks.next_due(now + 4 * 3600 + 1))
        self.assertEqual(tasks.next_due(now + 4 * 3600 + 1)["id"], "002")  # unfinished work is always due

    def test_per_internal_clock_is_valid(self):
        path = config.TASKS_DIR / "001-thought.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nrepeat: forever\nevery: per internal clock\nareas: /tmp\npowers: read, append\n---\n# T\n\nThink.\n")
        task = tasks._parse(path)
        self.assertIsNone(task["error"])
        self.assertIsNone(task["every"])
        path.write_text(path.read_text().replace("per internal clock", "sometimes"))
        self.assertIsNotNone(tasks._parse(path)["error"])

    def test_finished_tasks_archive_and_renumber(self):
        self.make("one")
        self.make("two", repeat=1)
        self.make("three")
        tasks.TaskNotes("003").write("notes for three")
        now = time.time()
        while True:
            task = tasks.next_due(now)
            if task is None:
                break
            self.finish(task)
        names = sorted(p.name for p in config.TASKS_DIR.glob("[0-9]*.md"))
        self.assertEqual(names, ["001-one.md", "002-three.md"])
        archived = [json.loads(line) for line in (config.TASKS_DIR / "done.jsonl").read_text().splitlines()]
        self.assertEqual([(a["name"], a["how"], a["runs"]) for a in archived], [("two", "finished", 1)])
        self.assertEqual(archived[0]["last_report"], "two report")
        state = tasks._state()
        self.assertEqual(set(state), {"001", "002"})
        self.assertEqual(state["002"]["last_report"], "three report")
        self.assertTrue((config.TASKS_DIR / "logs" / "002-three.md").exists())
        self.assertEqual(tasks.TaskNotes("002").read(), "notes for three")
        three = next(t for t in tasks.all_tasks() if t["name"] == "three")
        self.assertEqual(tasks.status(three), "active")  # approval survives the rename

    def test_delete_archives(self):
        self.make("one")
        task = self.make("two")
        tasks.delete(task)
        self.assertEqual(json.loads((config.TASKS_DIR / "done.jsonl").read_text())["how"], "deleted")
        self.assertFalse(task["path"].exists())

    def test_run_outcomes(self):
        installed = mock.patch.object(inference.OllamaDriver, "models", lambda self: [config.MODEL])
        installed.start()  # whatever config.MODEL is, it counts as installed here
        self.addCleanup(installed.stop)
        seeds_task = tasks.create("consolidate seeds", "Review long-term seeds.", [self.area], {"read", "seeds"},
                                  "forever", None)
        chosen = tasks._tools_for(seeds_task["powers"], tasks.TaskNotes("001"))
        self.assertIs(chosen["interaction_seed"]["function"], interaction.edit_for_task)
        self.assertIn("interaction_review", chosen)
        self.assertNotIn("interaction_review", tasks._tools_for({"read"}))
        self.assertNotIn("interaction_seed", tasks._tools_for({"read"}))
        api = lambda self, path, body=None: {"models": [{"name": config.MODEL}]}
        with mock.patch.object(engine.AgentEngine, "ollama_api", api), \
                mock.patch.object(engine.AgentEngine, "run", lambda self, text: "All reviewed."), \
                mock.patch.object(interaction, "mark_consolidated") as marked:
            tasks.TaskNotes("001").write("old notes")
            self.assertEqual(tasks.run(seeds_task, config.MODEL, 8192)[0], "ok")
            marked.assert_called_once()
            self.assertEqual(tasks.TaskNotes("001").read(), "")
        with mock.patch.object(engine.AgentEngine, "ollama_api", api), \
                mock.patch.object(engine.AgentEngine, "run", lambda self, text: engine.TIME_LIMIT_TEXT):
            tasks.TaskNotes("001").write("mid-way")
            self.assertEqual(tasks.run(seeds_task, config.MODEL, 8192)[0], "timeout")
            entry = tasks._state()["001"]
            self.assertEqual((entry["runs"], entry["failures"]), (1, 0))  # not a failure, not a run
            self.assertEqual(tasks.TaskNotes("001").read(), "mid-way")


if __name__ == "__main__":
    unittest.main()
