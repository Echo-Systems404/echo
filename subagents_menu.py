"""Subagents menu methods for Echo."""

from pathlib import Path
import config
import context
import inference
import subagents
import sysstat
import ui
from cli_style import GREY, RESET


class SubagentsMenu:
    def subagents_menu(self):
        commands_file = config.SUBAGENTS_DIR / "commands.txt"
        if not commands_file.exists():
            commands_file.parent.mkdir(parents=True, exist_ok=True)
            commands_file.write_text("# Commands rule agents may run, one per line as  name: command\n"
                                     "# They run in the target folder. Example:\n# tests: python3 -m unittest\n",
                                     encoding="utf-8")
        while True:
            model = subagents.settings().get("model") or "picked automatically"
            choice = ui.menu("Sub-agents", [
                ("auto", f"Autonomous sub-agent (model: {model})"),
                ("agents", f"Deterministic agents ({len(subagents.agents())})"),
                ("swarms", f"Deterministic swarms ({len(subagents.swarms())})"),
                ("commands", f"Allowed commands ({len(subagents.allowed_commands())})"),
                ("back", "Back")], echo=False)
            if choice in (None, "back"):
                return
            if choice == "auto":
                self.pick_subagent_model()
            elif choice == "agents":
                self.agents_menu()
            elif choice == "swarms":
                self.swarms_menu()
            elif choice == "commands":
                current = commands_file.read_text(encoding="utf-8")
                text = ui.multiline("Allowed commands (name: command):", current)
                if text is not None and text is not ui.BACK:
                    commands_file.write_text(text.rstrip() + "\n", encoding="utf-8")

    def pick_subagent_model(self):
        print(f"{GREY}Checking what fits in memory (the soft memory lock)...{RESET}")
        found = sysstat.plan(self.agent.model)
        options = [("", "Pick automatically (Echo's own model if loaded, else the smallest that fits)")]
        for m in found["models"]:
            if not m["tools"]:
                continue
            cost = "already loaded" if m["mode"] == sysstat.SHARED else f"{m['bytes'] / context.GiB:.1f} GB"
            options.append((m["name"], f"{m['name']}  ({m['mode']}, {context.format_size(m['ctx'] or 0)}, {cost})"))
        if found["skipped"]:
            print(f"{GREY}Too big right now: {', '.join(n for n, _ in found['skipped'][:10])}{RESET}")
        choice = ui.menu("Which model should autonomous sub-agents use?", options,
                         current=subagents.settings().get("model") or "", echo=False)
        if choice is not None and choice is not ui.BACK:
            subagents.save_settings(model=choice or None)
            print(f"Sub-agents use {choice or 'a model picked automatically'}.")

    def agents_menu(self):
        while True:
            items = subagents.agents()
            choice = ui.menu("Deterministic agents", [("add", "Add agent"), ("library", f"Agent library ({len(items)})"),
                                                      ("back", "Back")], echo=False)
            if choice in (None, "back"):
                return
            if choice == "add":
                self.add_agent()
            elif not items:
                print(f"{GREY}No agents yet. Add one first.{RESET}")
            else:
                pick = ui.menu("Agent library", [(a["name"], f"{a['name']}  ({a['kind']})  - {a.get('description', '')[:60]}")
                                                 for a in items] + [("back", "Back")], echo=False)
                if pick not in (None, "back"):
                    self.agent_actions(subagents.load_agent(pick))

    def ask_checks(self, current=()):
        """Rule checks picked in a menu, with settings asked for pattern and command."""
        current_names = [c["check"] for c in current]
        simple = [(name, text) for name, text in subagents.CHECKS.items() if name not in ("pattern", "command")]
        options = simple + [("pattern", "Add a pattern search...")]
        commands = subagents.allowed_commands()
        if commands:
            options.append(("command", f"Run allowed commands... ({', '.join(commands)})"))
        chosen = ui.menu("Which checks?", options, multi=True,
                         checked=[n for n in current_names if n not in ("pattern", "command")], echo=False)
        if chosen is None or chosen is ui.BACK:
            return None
        checks = [{"check": name} for name in chosen if name not in ("pattern", "command")]
        checks += [c for c in current if c["check"] in ("pattern", "command")]
        if "pattern" in chosen:
            regex = ui.line("Regular expression: ")
            if regex and regex is not ui.BACK:
                files = ui.line("Files (like *.py, or * for all): ", "*")
                label = ui.line("What a match means: ", "matches the pattern")
                checks.append({"check": "pattern", "regex": regex, "files": files or "*", "label": label or regex})
        if "command" in chosen:
            names = ui.menu("Which commands?", [(n, f"{n}: {c}") for n, c in commands.items()], multi=True, echo=False)
            checks = [c for c in checks if c["check"] != "command"]
            checks += [{"check": "command", "name": n} for n in (names or [])]
        return checks

    def ask_persona_model(self, current=None):
        found = sysstat.plan(self.agent.model)
        options = [("", "The autonomous sub-agent model (from /subagents)")]
        options += [(m["name"], f"{m['name']}  ({m['mode']})") for m in found["models"] if m["tools"]]
        if current and current not in dict(options):
            options.append((current, f"{current}  (doesn't fit right now)"))
        return ui.menu("Which model?", options, current=current or "", echo=False)

    def add_agent(self):
        name = ui.line("Agent name (like security-hunter): ")
        if not name or name is ui.BACK:
            return
        kind = ui.menu("What kind?", [("rules", "Rule agent: Python checks, no model, almost free"),
                                      ("persona", "Persona agent: a model with a personality and a focus")], echo=False)
        if kind is None:
            return
        spec = {"name": name, "kind": kind}
        if kind == "rules":
            spec["checks"] = self.ask_checks()
            if not spec["checks"]:
                return print("No checks picked; not saved.")
        else:
            spec["instructions"] = ui.multiline("Personality and focus:")
            if not spec["instructions"] or spec["instructions"] is ui.BACK:
                return
            model = self.ask_persona_model()
            if model is None or model is ui.BACK:
                return
            spec["model"] = model or None
        description = ui.line("One-line description: ")
        if description is None or description is ui.BACK:
            return
        spec["description"] = description
        try:
            agent = subagents.save_agent(spec)
        except ValueError as exc:
            return print(f"Not saved: {exc}")
        print(f"Saved agent {agent['name']}.")

    def agent_actions(self, agent):
        if agent is None:
            return
        while True:
            fields = [("description", f"Description ({agent.get('description', '')[:50]})")]
            if agent["kind"] == "rules":
                fields.append(("checks", f"Checks ({', '.join(c['check'] for c in agent['checks'])})"))
            else:
                fields += [("instructions", "Personality and focus"),
                           ("model", f"Model ({agent.get('model') or 'autonomous sub-agent model'})")]
            choice = ui.menu(f"Agent: {agent['name']} ({agent['kind']})",
                             fields + [("test", "Test it on a folder"), ("delete", "Delete"), ("back", "Back")], echo=False)
            if choice in (None, "back"):
                return
            if choice == "delete":
                if ui.menu(f"Delete {agent['name']}?", [(False, "Keep it"), (True, "Delete")], echo=False):
                    subagents.delete("agents", agent["name"])
                    print(f"Deleted {agent['name']}.")
                    return
                continue
            if choice == "test":
                folder = self.ask_any_folder()
                if folder:
                    self.run_swarm_here({"name": agent["name"], "passes": 1, "stop_early": True}, [(agent, 1)], folder)
                continue
            changed = dict(agent)
            if choice == "description":
                value = ui.line("One-line description: ", agent.get("description", ""))
            elif choice == "checks":
                value = self.ask_checks(agent["checks"])
            elif choice == "instructions":
                value = ui.multiline("Personality and focus:", agent["instructions"])
            else:
                value = self.ask_persona_model(agent.get("model"))
                value = (value or None) if value is not None and value is not ui.BACK else ui.BACK
            if value is None or value is ui.BACK or (choice == "checks" and not value):
                continue
            changed[choice] = value
            try:
                agent = subagents.save_agent(changed)
                print(f"{GREY}Saved.{RESET}")
            except ValueError as exc:
                print(f"Not saved: {exc}")

    def ask_any_folder(self):
        """A folder or file to point sub-agents at (the harness is fine: they only read)."""
        typed = ui.line("Folder to look at: ", str(Path.cwd()))
        if not typed or typed is ui.BACK:
            return None
        path = Path(typed.strip("'\"").replace("\\ ", " ")).expanduser().resolve()
        if not path.exists():
            print(f"Can't find {path}")
            return None
        return str(path)

    def ask_members(self, current=()):
        items = subagents.agents()
        if not items:
            print(f"{GREY}Add some agents first (Deterministic agents > Add agent).{RESET}")
            return None
        counts = {m["agent"]: m["count"] for m in current}
        chosen = ui.menu("Which agents?", [(a["name"], f"{a['name']}  ({a['kind']})") for a in items], multi=True,
                         checked=list(counts), echo=False)
        if not chosen or chosen is ui.BACK:
            return None
        members = []
        for agent in items:
            if agent["name"] not in chosen:
                continue
            count = 1
            if agent["kind"] == "persona":
                while True:
                    typed = ui.line(f"How many copies of {agent['name']}? ", str(counts.get(agent["name"], 1)))
                    if typed is None or typed is ui.BACK:
                        return None
                    if typed.isdigit() and 1 <= int(typed) <= config.SWARM_MAX_AGENTS:
                        count = int(typed)
                        break
                    print(f"Enter 1 to {config.SWARM_MAX_AGENTS}.")
            members.append({"agent": agent["name"], "count": count})
        total = sum(m["count"] for m in members)
        if total > config.SWARM_MAX_AGENTS:
            print(f"That's {total} agents; the limit is {config.SWARM_MAX_AGENTS}.")
            return None
        return members

    def ask_passes(self, current=None):
        return ui.menu("How many passes before it's done?",
                       [(n, f"{n} pass{'es' if n > 1 else ''}") for n in range(1, config.SWARM_MAX_PASSES + 1)],
                       current=current or config.SWARM_PASSES, echo=False)

    def swarms_menu(self):
        while True:
            items = subagents.swarms()
            choice = ui.menu("Deterministic swarms", [("add", "Add swarm"), ("library", f"Swarm library ({len(items)})"),
                                                      ("back", "Back")], echo=False)
            if choice in (None, "back"):
                return
            if choice == "add":
                self.add_swarm()
            elif not items:
                print(f"{GREY}No swarms yet. Add one first.{RESET}")
            else:
                pick = ui.menu("Swarm library", [(s["name"], f"{s['name']}  ({sum(m['count'] for m in s['members'])} agents, "
                                                             f"{s['passes']} pass{'es' if s['passes'] > 1 else ''})")
                                                 for s in items] + [("back", "Back")], echo=False)
                if pick not in (None, "back"):
                    self.swarm_actions(subagents.load_swarm(pick))

    def add_swarm(self):
        name = ui.line("Swarm name (like bug-hunt): ")
        if not name or name is ui.BACK:
            return
        members = self.ask_members()
        if not members:
            return
        passes = self.ask_passes()
        if passes is None or passes is ui.BACK:
            return
        stop_early = True
        if passes > 1:
            stop_early = ui.menu("Stop early when a pass finds nothing new?", [(True, "Yes"), (False, "No, run every pass")],
                                 echo=False)
            if stop_early is None:
                return
        description = ui.line("One-line description: ")
        if description is None or description is ui.BACK:
            return
        try:
            swarm = subagents.save_swarm({"name": name, "members": members, "passes": passes,
                                          "stop_early": stop_early, "description": description})
        except ValueError as exc:
            return print(f"Not saved: {exc}")
        print(f"Saved swarm {swarm['name']}.")

    def swarm_actions(self, swarm):
        if swarm is None:
            return
        while True:
            members = ", ".join(m["agent"] + (f" x{m['count']}" if m["count"] > 1 else "") for m in swarm["members"])
            choice = ui.menu(f"Swarm: {swarm['name']}", [
                ("members", f"Agents ({members})"),
                ("passes", f"Passes ({swarm['passes']}{', stops early' if swarm['stop_early'] else ''})"),
                ("description", f"Description ({swarm.get('description', '')[:50]})"),
                ("run", "Run it on a folder"), ("delete", "Delete"), ("back", "Back")], echo=False)
            if choice in (None, "back"):
                return
            if choice == "delete":
                if ui.menu(f"Delete {swarm['name']}?", [(False, "Keep it"), (True, "Delete")], echo=False):
                    subagents.delete("swarms", swarm["name"])
                    print(f"Deleted {swarm['name']}.")
                    return
                continue
            if choice == "run":
                folder = self.ask_any_folder()
                if folder:
                    try:
                        members_list = subagents.library_members(swarm)
                    except ValueError as exc:
                        print(exc)
                        continue
                    self.run_swarm_here(swarm, members_list, folder)
                continue
            changed = dict(swarm)
            if choice == "members":
                value = self.ask_members(swarm["members"])
                if not value:
                    continue
                changed["members"] = value
            elif choice == "passes":
                value = self.ask_passes(swarm["passes"])
                if value is None or value is ui.BACK:
                    continue
                changed["passes"] = value
                if value > 1:
                    early = ui.menu("Stop early when a pass finds nothing new?",
                                    [(True, "Yes"), (False, "No, run every pass")], echo=False)
                    if early is not None:
                        changed["stop_early"] = early
            else:
                value = ui.line("One-line description: ", swarm.get("description", ""))
                if value is None or value is ui.BACK:
                    continue
                changed["description"] = value
            try:
                swarm = subagents.save_swarm(changed)
                print(f"{GREY}Saved.{RESET}")
            except ValueError as exc:
                print(f"Not saved: {exc}")

    def run_swarm_here(self, swarm, members, folder):
        """Run a swarm from the menu and show its summary. Esc stops it."""
        launcher = {"model": self.agent.model, "num_ctx": self.agent.num_ctx, "driver": inference.driver(),
                    "areas": None, "powers": None}
        outcome = {}

        def work():
            outcome["summary"] = subagents.run(swarm, members, folder, launcher,
                                               lambda text: print(f"{GREY}  swarm · {text}{RESET}"))[0]

        self.run_task(work)
        if "summary" in outcome:
            print(outcome["summary"])
