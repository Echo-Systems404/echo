"""Memory layers configuration (SPEC §33–40): settings, rubrics, transient
entries, ephemeral memory, task notes, the vault, and context resets."""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

import config
import engine
import gate
import memory
import memory_settings as ms
import notes
import seeds
import tools
import vault
from test_helpers import FakeModel, HARNESS, Sandbox


def call(name, **args):
    return {"function": {"name": name, "arguments": args}}


class Settings(Sandbox):
    def test_defaults_are_todays_numbers(self):
        s = ms.load()
        self.assertEqual(s["ephemeral_keep"], config.EPHEMERAL_KEEP)
        self.assertEqual(s["context_trigger"], round(config.CONTEXT_PROMPT_AT * 100))
        self.assertEqual(s["history_messages"], config.MAX_HISTORY_MESSAGES)
        self.assertEqual(s["vault_access"], "auto")
        self.assertTrue(s["task_notes"])

    def test_ranges_enforced_when_saving(self):
        self.assertEqual(ms.save(ephemeral_keep=31), [])
        self.assertEqual(ms.get("ephemeral_keep"), 31)
        self.assertTrue(ms.save(ephemeral_keep=32))
        self.assertEqual(ms.get("ephemeral_keep"), 31)
        self.assertTrue(ms.save(vault_access="sometimes"))
        self.assertTrue(ms.save(task_notes="yes"))
        self.assertTrue(ms.save(nonsense=1))

    def test_ranges_enforced_when_loading(self):
        path = config.OPTIONS_DIR / ms.FILE
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"ephemeral_keep": 500, "context_trigger": True, "history_messages": 30,
                                    "vault_dir": str(HARNESS)}))
        s = ms.load()
        self.assertEqual(s["ephemeral_keep"], config.EPHEMERAL_KEEP)
        self.assertEqual(s["context_trigger"], round(config.CONTEXT_PROMPT_AT * 100))
        self.assertEqual(s["history_messages"], 30)
        self.assertEqual(s["vault_dir"], "")  # the harness can't be the vault
        path.write_text("not json")
        self.assertEqual(ms.get("ephemeral_keep"), config.EPHEMERAL_KEEP)

    def test_fixed_sections_cleaned(self):
        ms.save(transient_sections=["Website", " website ", "", "Garden  plan", "x" * 100])
        self.assertEqual(ms.get("transient_sections"), ["Website", "Garden plan", "x" * ms.SECTION_MAX])


class Rubrics(Sandbox):
    def setUp(self):
        super().setUp()
        self.formats = self.tmp / "formats"
        self.formats.mkdir()
        patcher = mock.patch.object(ms, "FORMATS_DIR", self.formats)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rubric_reaches_its_tool_and_is_capped(self):
        (self.formats / "transient.md").write_text("<!-- note -->\nUse bullet points." + "x" * 3000)
        agent = FakeModel([], tools=tools.TOOLS, memory=memory.ConversationMemory("s"))
        schema = next(s for s in agent._schemas() if s["function"]["name"] == "transient_edit")
        description = schema["function"]["description"]
        self.assertIn("Use bullet points.", description)
        self.assertNotIn("<!--", description)
        self.assertLessEqual(len(ms.rubric("transient")), config.MEMORY_RUBRIC_MAX_CHARS)
        self.assertTrue(ms.rubric_too_long("transient"))
        other = next(s for s in agent._schemas() if s["function"]["name"] == "read_file")
        self.assertNotIn("rubric", other["function"]["description"])

    def test_echo_cant_write_the_rubrics(self):
        guard = gate.Gate()
        for name in ("write_file", "append_file"):
            blocked = guard.check(name, {"path": str(HARNESS / "memory formats" / "transient.md"), "content": "x"})
            self.assertTrue(blocked and blocked.startswith("Blocked"))

    def test_shipped_defaults_exist(self):
        for name in ms.RUBRICS:
            self.assertTrue((HARNESS / "memory formats" / "defaults" / f"{name}.md").exists(), name)

    def test_default_until_the_user_edits_one(self):
        (self.formats / "defaults").mkdir()
        (self.formats / "defaults" / "ephemeral.md").write_text("Default format.")
        self.assertEqual(ms.rubric("ephemeral"), "Default format.")
        self.assertTrue(ms.rubric_is_default("ephemeral"))
        (self.formats / "ephemeral.md").write_text("My format.")
        self.assertEqual(ms.rubric("ephemeral"), "My format.")
        self.assertFalse(ms.rubric_is_default("ephemeral"))


class Transient(Sandbox):
    def test_titled_entries_and_append_to_the_right_one(self):
        self.assertIn("Added", notes.transient_edit("new", "Website", "Port 5173."))
        self.assertIn("Added", notes.transient_edit("new", "Garden", "Tomatoes in bed 2."))
        self.assertIn("Appended", notes.transient_edit("append", "website", "Renamed to Lumen."))
        site = notes.transient_read("WEBSITE")
        self.assertIn("Port 5173.", site)
        self.assertIn("Renamed to Lumen.", site)
        self.assertNotIn("Tomatoes", site)
        self.assertIn("- Garden", notes.transient_read())

    def test_missing_entry_lists_titles(self):
        notes.transient_edit("new", "Website", "a")
        result = notes.transient_edit("append", "Webshop", "b")
        self.assertTrue(result.startswith("Error"))
        self.assertIn("'Website'", result)

    def test_replace_delete_wipe(self):
        notes.transient_edit("new", "Website", "old")
        notes.transient_edit("replace", "Website", "new text")
        self.assertNotIn("old", notes.transient_read("Website"))
        notes.transient_edit("delete", "Website")
        self.assertIn("empty", notes.transient_read())
        notes.transient_edit("new", "A", "a")
        notes.transient_edit("wipe")
        self.assertIn("empty", notes.transient_read())

    def test_headings_in_content_cant_break_entries(self):
        notes.transient_edit("new", "Website", "## Not an entry\nline")
        self.assertEqual(len(notes._entries()), 1)

    def test_limits(self):
        ms.save(transient_entries=2, transient_entry_chars=1000, transient_total_chars=2000)
        notes.transient_edit("new", "A", "a")
        notes.transient_edit("new", "B", "b")
        full = notes.transient_edit("new", "C", "c")
        self.assertTrue(full.startswith("Error"))
        self.assertIn("'A'", full)  # names the oldest
        self.assertTrue(notes.transient_edit("append", "A", "x" * 1200).startswith("Error"))
        notes.transient_edit("replace", "A", "x" * 990)
        self.assertTrue(notes.transient_edit("replace", "B", "y" * 1020).startswith("Error"))
        self.assertIn("x" * 990, notes.transient_read("A"))

    def test_fixed_sections(self):
        ms.save(transient_sections=["Projects", "Open questions"])
        self.assertTrue(notes.transient_edit("new", "Recipes", "x").startswith("Error"))
        self.assertIn("Added", notes.transient_edit("new", "projects", "x"))

    def test_old_notebook_converted_once(self):
        path = config.NOTES_DIR / "transient.md"
        path.parent.mkdir(parents=True)
        path.write_text("# Atlas\nport 5173\n\n---\n\nloose line about the garden\n")
        entries = notes._entries()
        self.assertEqual([t for t, _ in entries], ["Atlas", "loose line about the garden"])
        self.assertIn("port 5173", notes.transient_read("Atlas"))
        self.assertTrue((config.NOTES_DIR / "transient.md.bak").exists())
        self.assertTrue(path.read_text().startswith("## Atlas"))

    def test_grounding_has_the_whole_notebook(self):
        notes.transient_edit("new", "Website", "Lumen")
        self.assertIn("Lumen", notes.grounding())


class Ephemeral(Sandbox):
    def test_keep_number(self):
        ms.save(ephemeral_keep=2)
        for i in range(4):
            self.in_conversation(f"20261124-10000{i}")
            notes.ephemeral_save(f"Talk {i}", "summary")
            folder = config.NOTES_DIR / "ephemeral"
            os.utime(folder / f"20261124-10000{i}.md", (1000 + i, 1000 + i))
        self.assertEqual(len(notes._saved()), 2)

    def test_size_cap(self):
        ms.save(ephemeral_chars=2000)
        self.assertTrue(notes.ephemeral_save("Talk", "x" * 2001).startswith("Error"))
        self.assertTrue(notes.ephemeral_save("Talk", "x" * 1999).startswith("Saved"))


class TaskNotesAndResets(Sandbox):
    def test_trigger_percent(self):
        ms.save(context_trigger=60)
        mem = memory.ConversationMemory("SYSTEM")
        asked = []
        agent = FakeModel([(10, {"content": "Answer."})], tools=tools.TOOLS, memory=mem,
                          on_full=lambda p: asked.append(p))
        agent.last_tokens = 650
        agent.run("next")
        self.assertEqual(asked, [65])
        ms.save(context_trigger=70)
        agent = FakeModel([(10, {"content": "Answer."})], tools=tools.TOOLS, memory=mem,
                          on_full=lambda p: asked.append(p))
        agent.last_tokens = 650
        agent.run("next")
        self.assertEqual(asked, [65])

    def test_history_kept(self):
        ms.save(history_messages=10)
        mem = memory.ConversationMemory("SYSTEM")
        for i in range(10):
            agent = FakeModel([(10, {"content": f"a{i}"})], tools=tools.TOOLS, memory=mem)
            agent.run(f"q{i}")
        self.assertLessEqual(len(mem.history), 10)

    def test_task_notes_off_end_the_run(self):
        guard = gate.Gate(policy={"areas": [self.tmp / "work"], "powers": {"read"}})
        agent = FakeModel([(900, {"tool_calls": [call("clock")]}), (10, {"content": "never"})],
                          tools=tools.TOOLS, memory=memory.ConversationMemory("S"), gate=guard,
                          on_full=lambda p: "end", checkpoints=False)
        self.assertEqual(agent.run("work"), engine.CONTEXT_END_TEXT)

    def test_task_notes_off_compact_and_continue(self):
        guard = gate.Gate(policy={"areas": [self.tmp / "work"], "powers": {"read"}})
        mem = memory.ConversationMemory("S")
        agent = FakeModel([(900, {"tool_calls": [call("clock")]}), (20, {"content": "The summary."}),
                           (30, {"content": "Finished."})],
                          tools=tools.TOOLS, memory=mem, gate=guard, on_full=lambda p: "compact", checkpoints=False)
        self.assertEqual(agent.run("work"), "Finished.")
        self.assertIn("The summary.", mem.carry)

    def test_task_notes_size(self):
        from task_storage import TaskNotes
        ms.save(task_notes_chars=2000)
        store = TaskNotes("001")
        self.assertTrue(store.write("x" * 2001).startswith("Error"))
        self.assertTrue(store.write("x" * 1500).startswith("Task notes saved"))


class Vault(Sandbox):
    def setUp(self):
        super().setUp()
        (self.tmp / "vault" / "Garden.md").write_text("Tomatoes " * 2000)

    def test_folder_checks(self):
        self.assertIsNotNone(ms.vault_folder_problem(str(Path.home())))
        self.assertIsNotNone(ms.vault_folder_problem("/"))
        self.assertIsNotNone(ms.vault_folder_problem(str(HARNESS)))
        self.assertIsNotNone(ms.vault_folder_problem(str(HARNESS / "notes")))
        self.assertIsNotNone(ms.vault_folder_problem(str(self.tmp / "missing")))
        self.assertIsNotNone(ms.vault_folder_problem("/System/Library"))
        hidden = self.tmp / ".secret"
        hidden.mkdir()
        self.assertIsNotNone(ms.vault_folder_problem(str(hidden)))
        with mock.patch.object(ms, "SYSTEM_FOLDERS", ("/System",)):
            self.assertIsNone(ms.vault_folder_problem(str(self.tmp / "vault")))
            other = self.tmp / "notes app"
            other.mkdir()
            self.assertEqual(ms.save(vault_dir=str(other)), [])
            self.assertEqual(ms.vault_dir(), other)
            self.assertIn("No matching", vault.vault_search("tomatoes"))

    def test_missing_vault_points_to_options(self):
        with mock.patch.object(config, "VAULT_DIR", self.tmp / "nowhere"):
            self.assertIn("/options", vault.vault_search("x"))

    def test_off_hides_the_tools(self):
        ms.save(vault_access="off")
        agent = FakeModel([], tools=tools.TOOLS, memory=memory.ConversationMemory("s"))
        agent.loaded = {"vault_read", "vault_search", "memory_save"}
        names = {s["function"]["name"] for s in agent._schemas()}
        self.assertFalse(names & set(ms.VAULT_TOOLS))
        self.assertIn("off", agent._run_tool("vault_read", {"path": "Garden"}))
        self.assertIn("off", agent._load_tool("vault_search"))
        self.assertNotIn("vault_read", seeds._tool_list({n: t for n, t in tools.TOOLS.items()
                                                         if not ms.hidden(n)}))

    def test_ask_first(self):
        ms.save(vault_access="ask")
        answers = []
        agent = FakeModel([], tools=tools.TOOLS, memory=memory.ConversationMemory("s"))
        agent._permission_granted = lambda reason: answers.append(reason) or False
        self.assertIn("Permission denied", agent._run_tool("vault_read", {"path": "Garden"}))
        self.assertIn("Garden", answers[0])
        agent._permission_granted = lambda reason: True
        self.assertIn("Tomatoes", agent._run_tool("vault_read", {"path": "Garden"}))

    def test_ask_first_is_off_in_tasks(self):
        ms.save(vault_access="ask")
        guard = gate.Gate(policy={"areas": [self.tmp / "work"], "powers": {"read"}})
        agent = FakeModel([], tools=tools.TOOLS, memory=memory.ConversationMemory("s"), gate=guard)
        self.assertIn("off", agent._run_tool("vault_read", {"path": "Garden"}))
        names = {s["function"]["name"] for s in agent._schemas()}
        self.assertNotIn("vault_read", names)

    def test_read_and_write_caps(self):
        ms.save(vault_read_chars=2000, vault_write_chars=1000)
        agent = FakeModel([], tools=tools.TOOLS, memory=memory.ConversationMemory("s"))
        text = agent._run_tool("vault_read", {"path": "Garden"})
        self.assertIn("past the read limit", text)
        self.assertLess(len(text), 2400)
        self.assertTrue(vault.vault_write("New", "x" * 1001).startswith("Error"))
        self.assertTrue(vault.vault_write("New", "x" * 900).startswith("Created"))

    def test_bigger_reads_arent_cut_by_tool_output(self):
        ms.save(vault_read_chars=12000)
        agent = FakeModel([], tools=tools.TOOLS, memory=memory.ConversationMemory("s"))
        text = agent._run_tool("vault_read", {"path": "Garden"})
        self.assertNotIn("truncated", text)
        self.assertGreater(len(text), config.MAX_TOOL_OUTPUT_CHARS)


class Pilot(Sandbox):
    def test_memory_block_is_live(self):
        config.SEEDS_DIR.mkdir(parents=True, exist_ok=True)
        (config.SEEDS_DIR / "pilot.md").write_text((HARNESS / "seeds" / "pilot.md").read_text())
        ms.save(ephemeral_keep=17, vault_access="off")
        text = seeds.render_pilot(tools.TOOLS)
        self.assertIn("last 17 saved conversations", text)
        self.assertIn("off: no vault tools", text)
        self.assertNotIn("{{memory}}", text)
        self.assertNotIn("- vault_read:", text)
        self.assertNotIn("last 10", config.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
