"""Unit tests for the skills engine, the soft memory lock, sub-agents and
swarms, Echo creating tasks, and task skills. No real model: drivers are
scripted. Run: python3 -B -m unittest test_skills_subagents
"""

import json
from pathlib import Path
from unittest import mock

import config
import engine
import inference
import memory
import skills
import subagents
import sysstat
import tasks
import tools
from test_helpers import FactsDriver, GiB, Sandbox, ScriptedDriver

GiB = 1024 ** 3
SKILL_MD = "---\nname: {name}\ndescription: {description}\n---\n\nDo the thing carefully.\n"
SKILL_TOOLS = '''
def hello(launcher=None):
    return "hi from " + str((launcher or {}).get("model"))

TOOLS = {"hello": {"function": hello, "launcher": True,
                   "schema": {"type": "function", "function": {"name": "hello", "description": "Say hi.",
                              "parameters": {"type": "object", "properties": {}, "required": []}}}}}
'''


class Base(Sandbox):
    def setUp(self):
        super().setUp()
        extra = [mock.patch.object(config, "SKILLS_DIR", self.tmp / "skills"),
                 mock.patch.object(config, "SUBAGENTS_DIR", self.tmp / "options" / "subagents")]
        for p in extra:
            p.start()
        self.patchers += extra
        inference._instance = None

    def tearDown(self):
        inference._instance = None
        super().tearDown()

    def make_skill(self, name="greet", description="Greets.", with_tools=True):
        folder = config.SKILLS_DIR / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(SKILL_MD.format(name=name, description=description), encoding="utf-8")
        if with_tools:
            (folder / "tools.py").write_text(SKILL_TOOLS, encoding="utf-8")
        return skills.Skill(folder)


# -- 14. skills engine -------------------------------------------------------------

class SkillsEngine(Base):
    def test_new_skill_needs_approval_then_loads_with_tools(self):
        skill = self.make_skill()
        self.assertEqual(skills.status(skill), "needs approval")
        self.assertEqual(skills.usable(), {})
        skills.approve(skill)
        self.assertEqual(list(skills.usable()), ["greet"])
        self.assertIn("- greet: Greets.", skills.prompt_section(skills.usable()))

        agent = engine.AgentEngine(config=config, tools={}, memory=memory.ConversationMemory("sys"),
                                   skills=skills.usable(), quiet=True)
        self.assertIn("load_skill", [s["function"]["name"] for s in agent._schemas()])
        text = agent._run_tool("load_skill", {"name": "greet"})
        self.assertIn("Do the thing carefully.", text)
        self.assertNotIn("load_skill", [s["function"]["name"] for s in agent._schemas()])
        agent.model = "tiny:1b"
        self.assertEqual(agent._run_tool("hello", {}), "hi from tiny:1b")  # skill tools get the launcher
        self.assertNotIn("hello", tools.TOOLS)  # the shared table is never changed

    def test_changed_outside_needs_approval_again_but_own_edit_stays(self):
        skill = self.make_skill()
        skills.approve(skill)
        skills.save_instructions(skills.Skill(skill.folder), "New instructions.")
        self.assertEqual(skills.status(skills.Skill(skill.folder)), "on")
        (skill.folder / "tools.py").write_text(SKILL_TOOLS + "\n# changed\n", encoding="utf-8")
        self.assertEqual(skills.status(skills.Skill(skill.folder)), "needs approval")

    def test_broken_skill_is_reported_not_raised(self):
        folder = config.SKILLS_DIR / "bad"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("no front matter", encoding="utf-8")
        self.assertEqual(skills.status(skills.Skill(folder)), "broken")
        skill = self.make_skill("oops")
        (skill.folder / "tools.py").write_text("raise ImportError('nope')", encoding="utf-8")
        skills.approve(skills.Skill(skill.folder))
        agent = engine.AgentEngine(config=config, tools={}, memory=memory.ConversationMemory("sys"),
                                   skills=skills.usable(), quiet=True)
        self.assertIn("couldn't load its tools", agent._run_tool("load_skill", {"name": "oops"}))

    def test_remove_moves_and_add_from_copies(self):
        skill = self.make_skill()
        skills.approve(skill)
        moved = skills.remove(skill)
        self.assertTrue((moved / "SKILL.md").exists())
        self.assertEqual(skills.all_skills(), [])
        added = skills.add_from(moved)
        self.assertTrue(isinstance(added, str) or added.name.startswith("greet"))

    def test_shipped_swarm_skill_is_valid(self):
        real = skills.Skill(Path(__file__).resolve().parent / "skills" / "swarm")
        self.assertIsNone(real.error)
        self.assertEqual(set(real.tools()), {"system_status", "list_subagents", "run_swarm", "run_echo_swarm"})


# -- 16. soft memory lock ----------------------------------------------------------

class SoftLock(Base):
    def plan(self, loaded, used_gb, models, budget_gb=24, echo="big:30b"):
        with mock.patch.object(context_mod(), "budget", return_value=int(budget_gb * GiB)), \
                mock.patch.object(inference, "loaded", return_value=loaded), \
                mock.patch.object(sysstat, "_memory", return_value=(used_gb * GiB, 32 * GiB)):
            return sysstat.plan(echo, FactsDriver(models))

    def test_beside_parks_shared_and_too_big(self):
        loaded = [{"name": "big:30b", "driver": "ollama", "context_length": 32768, "bytes": int(18 * GiB)}]
        models = {"big:30b": (17, 100_000, True), "small:3b": (2, 40_000, True),
                  "mid:12b": (8, 80_000, True), "huge:70b": (40, 200_000, True), "embed": (0.3, 1000, False)}
        found = self.plan(loaded, used_gb=24, models=models)
        modes = {m["name"]: m["mode"] for m in found["models"]}
        self.assertEqual(modes["big:30b"], sysstat.SHARED)
        self.assertEqual(modes["small:3b"], sysstat.BESIDE)  # 2 GB + cache fits in the ~4 GB left
        self.assertEqual(modes["mid:12b"], sysstat.PARKS)  # only fits once Echo's model is unloaded
        self.assertNotIn("huge:70b", modes)
        self.assertIn("huge:70b", [n for n, _ in found["skipped"]])
        small = next(m for m in found["models"] if m["name"] == "small:3b")
        self.assertGreaterEqual(small["ctx"], config.SUBAGENT_MIN_CTX)
        self.assertLessEqual(small["ctx"], config.SUBAGENT_CTX)
        self.assertFalse(next(m for m in found["models"] if m["name"] == "embed")["tools"])

    def test_context_is_lowered_to_fit(self):
        models = {"small:3b": (2, 100_000, True)}  # big cache per token
        found = self.plan([], used_gb=25, models=models, echo=None)  # 7 GB free - 2 headroom = 5
        entry = found["models"][0]
        self.assertEqual(entry["ctx"], 8192)  # 8k needs ~4.3 GB; 16k would need ~5.1 GB


class Beside(Base):
    def test_beside_keeps_echos_model_while_a_sub_agent_loads(self):
        unloaded = []
        loaded = [{"name": "big:30b"}, {"name": "old:7b"}]
        echo = inference.OllamaDriver()
        echo.used = {"big:30b"}
        with mock.patch.object(inference, "_ollama_loaded", return_value=loaded), \
                mock.patch.object(inference, "_ollama_unload", side_effect=unloaded.append), \
                mock.patch.object(inference, "servers", return_value=[]):
            with inference.beside(echo):
                inference.make_room("ollama", "small:3b")
            self.assertEqual(unloaded, ["old:7b"])  # Echo's model stayed
            inference.make_room("ollama", "small:3b")
            self.assertEqual(unloaded, ["old:7b", "big:30b", "old:7b"])  # afterward, one model again


def context_mod():
    import context
    return context


# -- 17. rule agents ---------------------------------------------------------------

SAMPLE = '''import os, subprocess, json

PASSWORD = "hunter2hunter2"

def used():
    return os.getcwd()

def never_called():
    try:
        return 1 / 0
    except:
        pass

def run(x):
    # TODO: sanitize x
    return subprocess.run("ls " + x, shell=True), eval(x)

used()
'''


class RuleAgents(Base):
    def setUp(self):
        super().setUp()
        self.target = self.tmp / "work"
        (self.target / "app.py").write_text(SAMPLE, encoding="utf-8")
        (self.target / "broken.py").write_text("def oops(:\n", encoding="utf-8")

    def findings(self, *checks):
        agent = subagents.validate_agent({"name": "r", "kind": "rules", "checks": list(checks)})
        return subagents.run_rules(agent, subagents.Target(self.target))[0]

    def test_each_check_finds_its_problem(self):
        def lines(check):
            return {(f["file"], f["line"]) for f in self.findings(check)}
        self.assertIn(("app.py", 8), lines("unused_functions"))
        self.assertNotIn(("app.py", 5), lines("unused_functions"))
        self.assertIn(("app.py", 1), lines("unused_imports"))  # json
        self.assertIn(("app.py", 11), lines("bare_except"))
        self.assertIn(("app.py", 16), lines("shell_true"))
        self.assertIn(("app.py", 16), lines("eval_exec"))
        self.assertIn(("broken.py", 1), lines("syntax_errors"))
        self.assertIn(("app.py", 3), lines("secrets"))
        self.assertIn(("app.py", 15), lines("todo"))
        pattern = self.findings({"check": "pattern", "regex": r"subprocess\.run", "files": "*.py", "label": "spawns"})
        self.assertEqual([(f["file"], f["line"]) for f in pattern], [("app.py", 16)])
        self.assertNotIn("hunter2", json.dumps(self.findings("secrets")))  # the value isn't repeated

    def test_commands_only_from_the_allow_list(self):
        with self.assertRaises(ValueError):
            subagents.validate_agent({"name": "c", "kind": "rules", "checks": [{"check": "command", "name": "rm"}]})
        config.SUBAGENTS_DIR.mkdir(parents=True)
        (config.SUBAGENTS_DIR / "commands.txt").write_text("# list\nfail: exit 3\nok: true\n", encoding="utf-8")
        found = self.findings({"check": "command", "name": "fail"}, {"check": "command", "name": "ok"})
        self.assertEqual(len(found), 1)
        self.assertIn("exit 3", found[0]["text"])
        with self.assertRaises(ValueError):
            subagents.validate_agent({"name": "c", "kind": "rules", "checks": [{"check": "command", "name": "ok"}]},
                                     commands_ok=False)


# -- 18. swarms --------------------------------------------------------------------

def persona(name, instructions="Find bugs."):
    return subagents.validate_agent({"name": name, "kind": "persona", "instructions": instructions})


class Swarms(Base):
    def setUp(self):
        super().setUp()
        self.target = self.tmp / "work"
        (self.target / "app.py").write_text(SAMPLE, encoding="utf-8")
        self.lock = {"models": [{"name": "small:3b", "mode": sysstat.SHARED, "ctx": 8192, "bytes": 0, "tools": True}],
                     "skipped": []}

    def run_swarm(self, members, replies, passes=1, stop_early=True, launcher=None):
        driver = ScriptedDriver([{"content": r} for r in replies])
        launcher = launcher or {"model": "small:3b", "driver": driver, "areas": None, "powers": None}
        with mock.patch.object(sysstat, "plan", return_value=self.lock):
            return subagents.run({"name": "t", "passes": passes, "stop_early": stop_early}, members,
                                 str(self.target), launcher)

    def test_merge_counts_agents_and_rules_run_once(self):
        rules = subagents.validate_agent({"name": "sweep", "kind": "rules", "checks": ["shell_true"]})
        summary, path = self.run_swarm(
            [(rules, 1), (persona("a"), 1), (persona("b"), 1)],
            ["- app.py:16 — shell injection via x\n- app.py:3 — hardcoded password",
             "- app.py:16 — command built from input"])
        self.assertIn("app.py:16", summary)
        self.assertIn("(3: sweep, a, b)", summary)  # three agents agree
        self.assertIn("(1: a)", summary)
        self.assertTrue(path.exists() and path.parent == config.SUBAGENTS_DIR / "runs")

    def test_passes_stop_early_when_nothing_new(self):
        summary, _ = self.run_swarm([(persona("a"), 1)],
                                    ["- app.py:16 — bad", "- app.py:16 — bad again", "never used"],
                                    passes=3)
        self.assertIn("2 pass(es), stopped early", summary)

    def test_runs_every_pass_without_stop_early(self):
        summary, _ = self.run_swarm([(persona("a"), 1)], ["- app.py:16 — bad", "No findings.", "No findings."],
                                    passes=3, stop_early=False)
        self.assertIn("3 pass(es)", summary)

    def test_copies_get_different_temperatures(self):
        temps = []
        driver = ScriptedDriver([{"content": "No findings."}] * 2)
        original = driver.chat

        def chat(*args):
            temps.append(driver.temperature)
            return original(*args)
        driver.chat = chat
        with mock.patch.object(sysstat, "plan", return_value=self.lock):
            subagents.run({"name": "t", "passes": 1}, [(persona("a"), 2)], str(self.target),
                          {"model": "small:3b", "driver": driver, "areas": None, "powers": None})
        self.assertEqual(temps, [config.SUBAGENT_TEMPERATURE, config.SUBAGENT_COPY_TEMPERATURE])
        self.assertIsNone(driver.temperature)  # restored

    def test_sub_agents_cant_write_and_stay_in_their_folder(self):
        outside = self.tmp / "vault" / "secret.txt"
        outside.write_text("x", encoding="utf-8")
        replies = [{"tool_calls": [{"function": {"name": "write_file",
                                                 "arguments": {"path": str(self.target / "x.txt"), "content": "hi"}}},
                                   {"function": {"name": "read_file", "arguments": {"path": str(outside)}}}]},
                   {"content": "No findings."}]
        driver = ScriptedDriver(replies)
        seen = []
        original = driver.chat

        def chat(model, messages, *rest):
            seen.append([m for m in messages if m["role"] == "tool"])
            return original(model, messages, *rest)
        driver.chat = chat
        with mock.patch.object(sysstat, "plan", return_value=self.lock):
            subagents.run({"name": "t"}, [(persona("a"), 1)], str(self.target),
                          {"model": "small:3b", "driver": driver, "areas": None, "powers": None})
        results = [m["content"] for m in seen[-1]]
        self.assertTrue(results[0].startswith("Error: unknown tool 'write_file'"))
        self.assertTrue(results[1].startswith("Blocked"))
        self.assertFalse((self.target / "x.txt").exists())

    def test_task_launcher_limits_the_target(self):
        with self.assertRaises(ValueError):
            self.run_swarm([(persona("a"), 1)], [], launcher={"model": "small:3b", "driver": ScriptedDriver([]),
                                                               "areas": [self.tmp / "vault"], "powers": {"read"}})

    def test_too_many_agents_refused(self):
        with self.assertRaises(ValueError):
            self.run_swarm([(persona("a"), config.SWARM_MAX_AGENTS + 1)], [])

    def test_echo_swarm_leaves_only_its_report(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("swarm_tools", Path(__file__).resolve().parent / "skills"
                                                      / "swarm" / "tools.py")
        swarm_tools = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(swarm_tools)
        driver = ScriptedDriver([{"content": "- app.py:16 — bad"}])
        with mock.patch.object(sysstat, "plan", return_value=self.lock):
            text = swarm_tools.run_echo_swarm(str(self.target), [
                {"name": "sweep", "kind": "rules", "checks": ["eval_exec"]},
                {"name": "skeptic", "kind": "persona", "instructions": "Distrust inputs."}],
                launcher={"model": "small:3b", "driver": driver, "areas": None, "powers": None})
        self.assertIn("app.py:16", text)
        self.assertEqual(subagents.agents(), [])
        self.assertEqual(subagents.swarms(), [])
        self.assertEqual(len(list((config.SUBAGENTS_DIR / "runs").glob("*.md"))), 1)
        self.assertIn("Error", swarm_tools.run_echo_swarm(str(self.target), [
            {"name": "x", "kind": "rules", "checks": [{"check": "command", "name": "anything"}]}]))

    def test_library_round_trip(self):
        subagents.save_agent({"name": "Security Hunter", "kind": "persona", "instructions": "Paranoid."})
        swarm = subagents.save_swarm({"name": "hunt", "members": [{"agent": "security-hunter", "count": 3}],
                                      "passes": 2})
        self.assertEqual(subagents.load_swarm("hunt")["passes"], 2)
        self.assertEqual(subagents.library_members(swarm)[0][1], 3)
        with self.assertRaises(ValueError):
            subagents.save_swarm({"name": "bad", "members": [{"agent": "a"}], "passes": config.SWARM_MAX_PASSES + 1})


# -- Echo creates tasks; tasks use skills ---------------------------------------------

class EchoTasks(Base):
    def test_create_task_shows_everything_then_creates_approved(self):
        args = {"name": "nightly notes", "instructions": "Summarize the day.", "folders": [str(self.tmp / "work")],
                "powers": ["append"], "repeat": "forever", "every": "6h"}
        with mock.patch.object(inference, "driver", side_effect=RuntimeError):
            reason = tools.create_task_needs_permission(**args)
            self.assertIn("nightly notes", reason)
            self.assertIn("Powers: read, append", reason)
            self.assertIn("Summarize the day.", reason)
            text = tools.create_task(**args)
        self.assertIn("Created task 001", text)
        task = tasks.all_tasks()[0]
        self.assertEqual(tasks.status(task), "active")
        self.assertEqual(task["every_text"], "6h")

    def test_create_task_refuses_bad_settings(self):
        work = str(self.tmp / "work")
        harness = str(Path(__file__).resolve().parent)
        self.assertIn("Error", tools.create_task("x", "y", [harness]))
        self.assertIn("Error", tools.create_task("x", "y", [work], powers=["root"]))
        self.assertIn("Error", tools.create_task("x", "y", [work], skills=["swarm"]))  # not on in this sandbox
        self.assertIsNone(tools.create_task_needs_permission(name="x", instructions="y", folders=[harness]))
        self.assertEqual(tasks.all_tasks(), [])

    def test_create_task_is_chat_only(self):
        self.assertNotIn("create_task", tasks._tools_for(set(tasks.KNOWN_POWERS)))

    def test_task_skills_round_trip_and_reach_the_run(self):
        skill = self.make_skill()
        skills.approve(skill)
        task = tasks.create("t", "Do it.", [str(self.tmp / "work")], {"read"}, 1, None, skills=["greet"])
        self.assertEqual(tasks.all_tasks()[0]["skills"], ["greet"])
        task = tasks.update(tasks.all_tasks()[0], body="Do it better.")
        self.assertEqual(task["skills"], ["greet"])
        captured = {}

        class Capture(engine.AgentEngine):
            def __init__(self, **kw):
                captured.update(kw)
                super().__init__(**kw)

            def run(self, text):
                return "done"
        with mock.patch.object(engine, "AgentEngine", Capture), \
                mock.patch.object(engine.AgentEngine, "driver", mock.Mock(models=lambda: ["m"])):
            tasks.run(tasks.all_tasks()[0], "m", 4096)
        self.assertEqual(list(captured["skills"]), ["greet"])
        self.assertIn("- greet: Greets.", captured["memory"].system_prompt)

    def test_task_runs_never_clear_chats_resume_checkpoint(self):
        Path(config.CHECKPOINT_FILE).write_text("{}", encoding="utf-8")
        agent = engine.AgentEngine(config=config, tools={}, memory=memory.ConversationMemory("s"), checkpoints=False)
        agent._clear_checkpoint()
        self.assertTrue(Path(config.CHECKPOINT_FILE).exists())


if __name__ == "__main__":
    import unittest
    unittest.main()
