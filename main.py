#!/usr/bin/env python3
"""Interactive CLI for the agent harness.

Type / to see the commands. While the agent works, press Esc to interrupt it
and give new instructions. Ctrl-C clears the line; Ctrl-D or /bye! quits.
"""

import json
import os
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
from pathlib import Path

from prompt_toolkit import PromptSession, prompt
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.application import get_app
from prompt_toolkit.completion import Completer, Completion, WordCompleter
from prompt_toolkit.history import FileHistory

import autonomy
import config
import context
import engine
import errorlog
import gate
import inference
import interaction
import memory_settings
import memory
import modes
import notes
import panel as status_panel
import reminders
import seeds
import sentinel
import skills
import subagents
import tasks
import tools
import turns
import ui

from cli_style import BOLD, GREY, CYAN, YELLOW, RESET
from options_menu import OptionsMenu
from tasks_menu import TasksMenu
from skills_menu import SkillsMenu
from subagents_menu import SubagentsMenu
from communication.telegram.menu import TelegramMenu
from memory_menu import MemoryMenu
from tasks_menu import EVERY_OPTIONS, EVERY_TITLE, POWER_NAMES, POWER_OPTIONS, REPEAT_OPTIONS, powers_text, timing_text

WAKE = "\0clock"  # what the prompt returns when the internal clock wakes the agent
IDLE = "\0idle"  # what it returns when chat has been idle for CHAT_IDLE_MINUTES

COMMANDS = {
    "/help": "Show the commands",
    "/resume": "Continue the unfinished task",
    "/clear": "Start a fresh conversation",
    "/model": "Show or switch the model: /model <name>",
    "/mode": "Pick a mode from self layers, or /mode off",
    "/context": "Show or change the context window: /context <size>",
    "/tasks": "Create and manage scheduled tasks",
    "/options": "Tasks, the internal clock, inference driver, memory guard, memory layers, Telegram",
    "/memory": "Memory layers: context resets, notes, ephemeral memory, task notes, the vault",
    "/telegram": "Talk to Echo from your phone: setup, model, and settings",
    "/skills": "Turn skills on or off, edit, add, or remove them",
    "/subagents": "Sub-agent model, deterministic agents, and swarms",
    "/status": "Model, folder, conversation size, loaded models",
    "/bye!": "Quit",
}

class EscListener:
    """While running, a press of Esc raises engine.Interrupted in the main
    thread (via SIGUSR1, which also breaks out of a blocking network read or
    tool). Fires once per start(); Ctrl-C keeps working as usual."""

    def __init__(self):
        self.fd = sys.stdin.fileno()
        self._stop = threading.Event()
        self._thread = None
        self._saved_tty = None
        signal.signal(signal.SIGUSR1, self._raise_interrupted)

    @staticmethod
    def _raise_interrupted(signum, frame):
        raise engine.Interrupted()

    def start(self):
        self._saved_tty = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)  # read keys one at a time, without echo
        self._stop.clear()
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved_tty)

    def _listen(self):
        while not self._stop.is_set():
            if not select.select([self.fd], [], [], 0.1)[0]:
                continue
            if os.read(self.fd, 1) != b"\x1b":
                continue  # other keys typed while the agent works are ignored
            if select.select([self.fd], [], [], 0.05)[0]:
                os.read(self.fd, 32)  # rest of an arrow/function-key sequence
                continue
            os.kill(os.getpid(), signal.SIGUSR1)
            return


class CommandCompleter(Completer):
    """Dropdown for /commands, and for the values after /model, /mode and /context."""

    def __init__(self, list_models, context_sizes, mode_choices):
        self.list_models = list_models
        self.context_sizes = context_sizes
        self.mode_choices = mode_choices

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        if " " not in text:
            for command, description in COMMANDS.items():
                if command.startswith(text):
                    yield Completion(command, start_position=-len(text), display_meta=description)
        elif text.startswith("/model "):
            word = text[len("/model "):]
            for name in self.list_models():
                if name.startswith(word):
                    yield Completion(name, start_position=-len(word))
        elif text.startswith("/mode "):
            word = text[len("/mode "):].lower()
            for name, note in self.mode_choices():
                if name.startswith(word):
                    yield Completion(name, start_position=-len(word), display_meta=note)
        elif text.startswith("/context "):
            word = text[len("/context "):]
            for size, note in self.context_sizes():
                if size.startswith(word):
                    yield Completion(size, start_position=-len(word), display_meta=note)


class CLI(TasksMenu, OptionsMenu, SkillsMenu, SubagentsMenu, TelegramMenu, MemoryMenu):
    def __init__(self):
        self.interactive = sys.stdin.isatty()
        self.listener = EscListener() if self.interactive else None
        if self.interactive:
            interaction.session_start()
        self.mode = None  # always boots on plain self.md
        self.mode_text = ""
        self.mode_note = None  # tells the model about a switch on the next prompt
        self.reminder_ids = []  # unseen reminders now in the system prompt
        if self.interactive:
            reminders.prune()
        self.memory = memory.ConversationMemory(system_prompt=self.system_prompt())
        self.agent = engine.AgentEngine(
            config=config, tools=tools.TOOLS, memory=self.memory,
            ask=self.ask if self.interactive else None,
            on_turn=self.report_turn if self.interactive and config.STATUS_PANEL else None,
            gate=gate.Gate(on_flag=self.note_gate_flag),
            on_full=self.context_full if self.interactive else None,
            on_flush=self.after_flush,
            skills=skills.usable(),
        )
        self.agent.model = inference.default_model()
        self.last_input = time.monotonic()  # for the idle unload
        self.idle_done = False  # the model was unloaded for idleness; reset on the next input
        self.status_file = Path(tempfile.gettempdir()) / f"echo-status-{os.getpid()}.json"
        self.panel_status = {}  # the latest turn's stats plus model and context
        self.worked = 0  # seconds spent on the current prompt, minus waiting for the user
        self.work_start = None
        self._models = None
        self._facts = {}  # model -> context.model_facts(...)
        self._has_tools = {}  # model -> whether Ollama says it supports tool calls
        if self.interactive:
            self.session = PromptSession(
                history=FileHistory(str(config.HISTORY_FILE)),
                auto_suggest=AutoSuggestFromHistory(),
                completer=CommandCompleter(self.tool_models, self.context_choices, self.mode_choices),
                complete_while_typing=True,
            )

    def system_prompt(self):
        prompt_text = memory.build_system_prompt(
            config.SYSTEM_PROMPT, config.SELF_FILE, config.USER_FILE, seeds.render_pilot(tools.TOOLS),
            mode=self.mode_text, user_seeds=interaction.snapshot()["user"],
        )
        waiting = reminders.unseen() if self.interactive else []
        self.reminder_ids = [item["id"] for item in waiting]
        sections = [skills.prompt_section(skills.usable()), reminders.prompt_section(waiting)]
        return "\n\n".join([prompt_text] + [part for part in sections if part])

    def reminders_shown(self):
        """After Echo's reply, the reminders it had are seen and leave the prompt."""
        if self.reminder_ids:
            reminders.mark_seen(set(self.reminder_ids))
            self.memory.system_prompt = self.system_prompt()

    def context_full(self, percent):
        """Engine callback once the context passes the trigger (/options → Memory layers)."""
        action = memory_settings.get("context_action")
        if action != "ask":
            return action
        self.listener.stop()
        self.stop_timer(paused=True)
        try:
            print(f"\n{BOLD}The context window is {percent}% full.{RESET}")
            return ui.menu("What should Echo do?", [
                ("update", "Memory layer update: save to memory layers, then clear the context"),
                ("compact", "Compact: summarize the conversation and continue from the summary"),
                ("later", "Not now (asked again on the next request)"),
            ])
        finally:
            self.start_timer()
            self.listener.start()

    def after_flush(self):
        """The context was cleared: rebuild the prompt with fresh user seeds."""
        interaction.refresh_snapshot()
        self.memory.system_prompt = self.system_prompt()
        self.report_turn({"tokens": 0})

    # -- status panel --------------------------------------------------

    def open_panel(self):
        """Open panel.py in its own Terminal window, leaving focus here."""
        if not (self.interactive and config.STATUS_PANEL and sys.platform == "darwin"):
            return
        self.report_turn()
        panel = Path(__file__).with_name("panel.py")
        try:
            tty = os.ttyname(0)  # so the panel can sit beside this window
        except OSError:
            tty = ""
        command = "exec " + " ".join(shlex.quote(str(part)) for part in
                                     (sys.executable, panel, os.getpid(), self.status_file, tty))
        command = command.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{command}"'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def sync_panel(self):
        """Tell the panel about any change it hasn't seen (model, context,
        mode), whichever menu or command made it."""
        if not (self.interactive and config.STATUS_PANEL):
            return
        seen = (self.panel_status.get("model"), self.panel_status.get("num_ctx"), self.panel_status.get("mode"))
        if seen != (self.agent.model, self.agent.num_ctx, self.mode):
            self.report_turn()

    def report_turn(self, stats=None):
        """Update the panel: after a turn, or with no stats after /model or
        /context. Never lets it disturb the agent."""
        if not (self.interactive and config.STATUS_PANEL):
            return
        self.panel_status.update(stats or {})
        self.panel_status.update(model=self.agent.model, mode=self.mode, num_ctx=self.agent.num_ctx,
                                 max_ctx=self.model_facts(self.agent.model)["max_ctx"],
                                 ctx_lock=config.CONTEXT_LOCK)
        try:
            temporary = self.status_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.panel_status))
            os.replace(temporary, self.status_file)
        except OSError:
            pass

    def note_gate_flag(self):
        """Shown only in the panel; the model never hears about a flag."""
        self.panel_status["gate_flags"] = self.panel_status.get("gate_flags", 0) + 1

    def start_timer(self):
        self.work_start = time.monotonic()
        self.report_turn({"worked": self.worked, "working_since": time.time(), "paused": False})

    def stop_timer(self, paused=False):
        if self.work_start is None:
            return
        self.worked += time.monotonic() - self.work_start
        self.work_start = None
        self.report_turn({"worked": self.worked, "working_since": None, "paused": paused})

    # -- terminal helpers ----------------------------------------------

    def read_line(self):
        print()
        if self.interactive:
            return self.session.prompt([("bold", "user: ")])
        return input("user: ")

    def ask(self, question):
        """Ask the user something while a task is running (engine callback)."""
        self.listener.stop()  # back to normal line input for the prompt
        self.stop_timer(paused=True)  # waiting for the user isn't work
        try:
            return prompt(question)
        except (EOFError, KeyboardInterrupt):
            return ""
        finally:
            self.start_timer()
            self.listener.start()

    def pick(self, label, choices, current):
        """Open a picker with the dropdown already showing. choices: list of
        (value, note). Returns the chosen value, or None to keep the current one."""
        if not self.interactive or not choices:
            return None
        completer = WordCompleter([value for value, _ in choices], meta_dict=dict(choices),
                                  sentence=True, match_middle=True)
        try:
            answer = prompt(f"{label} (Enter keeps {current}): ", completer=completer,
                            complete_while_typing=True,
                            pre_run=lambda: get_app().current_buffer.start_completion(select_first=False))
        except (EOFError, KeyboardInterrupt):
            return None
        return answer.strip() or None

    def installed_models(self, refresh=False):
        if self._models is None or refresh:
            try:
                self._models = inference.driver().models()
            except Exception:
                self._models = []
        return self._models

    def supports_tools(self, model):
        if model not in self._has_tools:
            try:
                self._has_tools[model] = inference.driver().supports_tools(model)
            except Exception:
                return False  # not cached, so it's checked again next time
        return self._has_tools[model]

    def tool_models(self, refresh=False):
        """Installed models that can make tool calls; the agent needs them."""
        return [m for m in self.installed_models(refresh) if self.supports_tools(m)]

    def model_facts(self, model):
        if model not in self._facts:
            try:
                self._facts[model] = inference.driver().facts(model)
            except Exception:
                return {"max_ctx": None, "weights": None, "kv_per_token": None}
        return self._facts[model]

    def forget_models(self):
        """After a driver switch: model lists and facts are per driver."""
        self._models, self._facts, self._has_tools = None, {}, {}

    def context_choices(self):
        """(size, "fits · ~21.3 GB") pairs for the current model's dropdown."""
        facts, budget = self.model_facts(self.agent.model), context.budget()
        choices = []
        for size in context.options(facts):
            if size > config.CONTEXT_LOCK:
                continue
            total = context.estimate(facts, size)
            note = context.verdict(total, budget)
            if total is not None:
                note += f" · ~{total / context.GiB:.1f} GB"
            choices.append((context.format_size(size), note))
        return choices

    # -- main loop -----------------------------------------------------

    def banner(self):
        folder = os.getcwd().replace(os.path.expanduser("~"), "~", 1)
        print(f"{BOLD}{config.AGENT_NAME}{RESET} · {self.agent.model} · {folder}")
        print(f"{GREY}/help for commands · esc interrupts the agent · ctrl-d quits{RESET}")
        waiting = reminders.summary(reminders.unseen()) if self.interactive else ""
        if waiting:
            print(f"{CYAN}{waiting}. Echo will bring them up.{RESET}")
        pending = self.agent.pending_task()
        if pending:
            print(f"Unfinished task found: {pending[:80]!r}. Type /resume to continue it.")

    def loop(self):
        self.banner()
        if self.interactive:
            self.session_lock = autonomy.hold_session()  # the background runner waits while Echo is open
            autonomy.sync()
            for task in tasks.missing_model_tasks():  # the runner found one while chat was closed
                self.fix_task_model(task)
            threading.Thread(target=self.clock_loop, daemon=True).start()
        hupped = False

        def _hup(*_):
            nonlocal hupped
            hupped = True
            raise KeyboardInterrupt

        try:
            signal.signal(signal.SIGHUP, _hup)
        except (ValueError, AttributeError):
            pass
        try:
            while True:
                try:
                    text = self.read_line().strip()
                except KeyboardInterrupt:
                    if hupped:
                        break
                    continue  # Ctrl-C clears the line
                except EOFError:
                    break
                if text == WAKE:
                    task = tasks.next_due()
                    if task and self.run_scheduled(task) == "exit":
                        break
                    if not tasks.cycle_open():
                        inference.driver().release()  # the cycle is over: free the memory
                    continue
                if text == IDLE:
                    if self.idle_unload() == "exit":
                        break
                    continue
                self.last_input = time.monotonic()
                self.idle_done = False
                if not text:
                    continue
                if text.startswith("/") or text in ("exit", "quit"):
                    if self.command(text) == "exit":
                        break
                else:
                    outcome = self.chat_turn(text)
                    if outcome == "exit":
                        break
                    if outcome != "cancelled":
                        self.reminders_shown()
        except KeyboardInterrupt:
            pass
        finally:
            inference.driver().release()  # quitting unloads the model right away
            print("Goodbye.")

    def chat_turn(self, text):
        """One chat message, once the model is free (Telegram or a task may be
        using it). Returns "exit", "cancelled", or None."""
        turn = turns.hold()
        if turn is None:
            print(f"{GREY}{config.AGENT_NAME} is answering on Telegram or running a task. Your message goes "
                  f"next (ctrl-c cancels it).{RESET}")
            try:
                turn = turns.hold(wait=True)
            except KeyboardInterrupt:
                print("Cancelled.")
                return "cancelled"
        try:
            return self.run_task(lambda: self.agent.run(self.with_mode_note(text)))
        finally:
            turn.close()

    # -- internal clock and tasks ---------------------------------------

    def clock_loop(self):
        """Wakes the agent for a due task or an idle unload, only at an empty
        prompt, so it never interrupts a running request or something you're
        typing. While chat is in use it renews the model's lease."""
        last_renew = time.monotonic()
        while True:
            time.sleep(config.CLOCK_POLL_SECONDS)
            try:
                self.sync_panel()
                sentinel.tick()  # the harness's own security watch: no model, every SENTINEL_MINUTES
                if not self.idle_done and time.monotonic() - last_renew >= config.LEASE_MINUTES * 30:
                    last_renew = time.monotonic()
                    inference.driver().renew(self.agent.model)  # never loads it, only extends
                app = self.session.app
                # Only at an idle prompt: never while a menu is open or a task runs.
                if not (app.is_running and not app.current_buffer.text):
                    continue
                if self.idle_due():
                    app.loop.call_soon_threadsafe(self.wake_prompt, app, IDLE)
                    continue
                urgent = sentinel.due_response() or tasks.requested() is not None  # these run even with the clock off
                if ((not tasks.load_clock()["enabled"] and not urgent) or tasks.running() or turns.busy()
                        or tasks.next_due() is None):
                    continue
                app.loop.call_soon_threadsafe(self.wake_prompt, app, WAKE)
            except Exception as exc:
                errorlog.log_error("main.clock_loop", exc)
                continue  # the clock must never disturb the session

    @staticmethod
    def wake_prompt(app, why):
        if app.is_running and not app.is_done and not app.current_buffer.text:
            app.exit(result=why)

    def idle_due(self):
        return (not self.idle_done and config.CHAT_IDLE_MINUTES
                and time.monotonic() - self.last_input >= config.CHAT_IDLE_MINUTES * 60)

    def idle_unload(self):
        """Idle too long: save to the memory layers and flush (if there's
        anything new), then unload the model."""
        turn = turns.hold()
        if turn is None:
            return None  # Telegram or a task has the model; try again at the next check
        self.idle_done = True
        try:
            if self.memory.history:
                exit_now = self.run_task(lambda: self.agent.idle_update(config.CHAT_IDLE_MINUTES))
                if exit_now == "exit":
                    return "exit"
        finally:
            turn.close()
        inference.driver().release()
        print(f"{GREY}Idle for {config.CHAT_IDLE_MINUTES} min: the model is unloaded. "
              f"Your next message reloads it.{RESET}")
        return None

    def fix_task_model(self, task):
        """A task whose model isn't installed is paused and the user picks
        another. Returns the task, ready to run, or None if it stays paused."""
        model = task.get("model")
        try:
            installed = model is None or model in self.installed_models(refresh=True)
        except Exception:
            return task  # can't tell (the server is down); the run reports it
        if installed:
            if any(t["id"] == task["id"] for t in tasks.missing_model_tasks()):
                tasks.set_paused(task, False)  # it's back
            return task
        tasks.mark_missing_model(task, model)
        print(f"{YELLOW}Task {task['id']}: {task['name']} uses {model}, which isn't installed. "
              f"It's paused until you pick another model.{RESET}")
        choice = ui.menu("Which model should it use?",
                         [(m, m) for m in self.tool_models(refresh=True)]
                         + [("echo", f"Whatever Echo is using (now {self.agent.model})"),
                            ("", "Leave it paused")], echo=False)
        reminders.mark_seen([item["id"] for item in reminders.unseen()
                             if item.get("from") == tasks.missing_model_source(task)])
        if not choice:
            print(f"{GREY}Task {task['id']} stays paused. Resume it in /options → Tasks to pick a model.{RESET}")
            return None
        result = tasks.update(task, model=None if choice == "echo" else choice)
        if isinstance(result, str):
            print(result)
            return None
        tasks.set_paused(result, False)
        print(f"{GREY}Task {task['id']} now uses {choice if choice != 'echo' else self.agent.model} "
              f"and is active again. Review it any time in Edit task spec.{RESET}")
        return result

    def run_scheduled(self, task):
        task = self.fix_task_model(task)
        if task is None:
            return None
        print(f"\n{CYAN}Task {task['id']}: {task['name']}{RESET}")
        outcome = {}
        before = {item["id"] for item in reminders.unseen()}

        def work():
            outcome["result"], outcome["text"] = tasks.run(
                task, self.agent.model, self.agent.num_ctx, on_turn=self.agent.on_turn,
                on_flag=self.note_gate_flag, fit_context=self.fitted_context)

        exit_now = self.run_task(work)
        result = outcome.get("result", "stopped")
        if result in ("failed", "busy", "skipped") and outcome.get("text"):
            result += f" ({outcome['text'][:200]})"  # a run that ends at once says why
        after = tasks.describe(task) if task["path"].exists() else "Finished and archived."
        print(f"{GREY}Task {task['id']} finished: {result}. {after}{RESET}")
        new = [item for item in reminders.unseen() if item["id"] not in before]
        for item in new:
            if item.get("urgency") == "urgent":
                print(f"{CYAN}Reminder from {item.get('from', 'a task')}: {item['text']}{RESET}")
        if new:
            self.memory.system_prompt = self.system_prompt()
        return exit_now

    def fitted_context(self, model, size):
        """The largest context up to size that this model can hold in memory."""
        return context.fitted(self.model_facts(model), size)

    def with_mode_note(self, text):
        """Mark a mode switch in the conversation, so the model doesn't just
        keep imitating its earlier replies."""
        note, self.mode_note = self.mode_note, None
        return f"[Harness note: {note}]\n\n{text}" if note else text

    def run_task(self, task):
        """Run an agent task with Esc enabled. Returns "exit" on Ctrl-C."""
        self.memory.system_prompt = self.system_prompt()  # picks up seed changes
        self.worked = 0
        self.start_timer()
        try:
            if self.listener:
                self.listener.start()
            try:
                task()
            finally:
                if self.listener:
                    self.listener.stop()
                self.stop_timer()
            print()
            if self.worked >= 1:
                print(f"{GREY}✻ Worked for {status_panel.format_duration(self.worked)}{RESET}")
        except engine.Interrupted:
            pass  # Esc pressed just as the task finished; nothing to stop
        except KeyboardInterrupt:
            print("\nInterrupted. Type /resume next time to continue.")
            return "exit"
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
        return None

    # -- commands ------------------------------------------------------

    def command(self, text):
        name, _, arg = text.partition(" ")
        arg = arg.strip()
        matches = [c for c in COMMANDS if c.startswith(name)]
        if name not in COMMANDS and len(matches) == 1:
            name = matches[0]  # a unique prefix, e.g. /con -> /context
        if name in ("/bye!", "exit", "quit"):
            return "exit"
        if name == "/help":
            for command, description in COMMANDS.items():
                print(f"  {CYAN}{command:<{max(map(len, COMMANDS))}}{RESET} {description}")
        elif name == "/resume":
            return self.run_task(self.agent.resume)
        elif name == "/clear":
            self.memory.clear()
            self.agent.last_tokens = 0
            self.mode_note = None
            notes.new_conversation()
            interaction.session_start()
            print("Started a fresh conversation.")
        elif name == "/model":
            self.switch_model(arg)
            self.report_turn()
        elif name == "/mode":
            self.set_mode(arg)
            self.report_turn()
        elif name == "/context":
            self.set_context(arg)
            self.report_turn()
        elif name == "/tasks":
            return self.tasks_menu() if self.interactive else print("/tasks needs a terminal.")
        elif name == "/options":
            result = self.options_menu() if self.interactive else print("/options needs a terminal.")
            self.sync_panel()
            return result
        elif name == "/skills":
            if not self.interactive:
                return print("/skills needs a terminal.")
            self.skills_menu()
            self.refresh_skills()
        elif name == "/subagents":
            if not self.interactive:
                return print("/subagents needs a terminal.")
            self.subagents_menu()
        elif name == "/telegram":
            if not self.interactive:
                return print("/telegram needs a terminal.")
            self.telegram_menu()
        elif name == "/memory":
            if not self.interactive:
                return print("/memory needs a terminal.")
            self.memory_menu()
        elif name == "/status":
            self.status()
        else:
            print(f"Unknown command: {name}. Type /help to see the commands.")
        return None

    def switch_model(self, name):
        models = self.tool_models(refresh=True)
        if not models:
            print(f"Current model: {self.agent.model}")
            if self.installed_models():
                print("None of the installed models support tool calls, so there's nothing to switch to.")
            else:
                print(f"No models found for {inference.driver().label}. Check /options > Inference driver.")
            return
        if not name:
            print(f"Current model: {self.agent.model}")
            name = self.pick("Switch to", [(m, "current" if m == self.agent.model else "") for m in models],
                             self.agent.model)
            if not name or name == self.agent.model:
                return
        match = next((m for m in self.installed_models() if m in (name, f"{name}:latest")), None)
        if match is None:
            print(f"'{name}' is not installed. Type /model to list the models you can use.")
            return
        if match not in models:
            print(f"{match} doesn't support tool calls, so it can't run the agent. "
                  f"Staying on {self.agent.model}.")
            return
        self.agent.model = match
        models_by_driver = inference.settings()["models"]
        models_by_driver[inference.settings()["driver"]] = match
        inference.save_settings(models=models_by_driver)
        print(f"Switched to {match}. It loads on the next request, and stays the default for "
              f"{inference.driver().label}.")
        facts = self.model_facts(match)
        if facts["max_ctx"] and self.agent.num_ctx > facts["max_ctx"]:
            self.agent.num_ctx = facts["max_ctx"]
            print(f"Context lowered to {context.format_size(facts['max_ctx'])}, this model's maximum.")
        budget = context.budget()
        if context.verdict(context.estimate(facts, self.agent.num_ctx), budget) == "too big":
            fitting = [s for s in context.options(facts)
                       if s < self.agent.num_ctx and context.verdict(context.estimate(facts, s), budget) != "too big"]
            if fitting:
                self.agent.num_ctx = fitting[-1]
                print(f"Context lowered to {context.format_size(fitting[-1])} so this model fits in memory. "
                      f"Type /context for options.")
            else:
                print("Warning: this model doesn't fit in memory even with a small context.")

    def mode_choices(self):
        """(name, description) pairs for the /mode dropdown, read fresh from disk."""
        choices = []
        for name, layer in modes.scan(config.MODES_DIR, config.MODE_MAX_CHARS).items():
            note = "too long" if layer["too_long"] else layer["description"]
            choices.append((name, f"{note} (current)" if name == self.mode else note))
        return choices + [(modes.OFF, "Standard self.md" + (" (current)" if not self.mode else ""))]

    def set_mode(self, arg):
        if not arg:
            print(f"Current mode: {self.mode or 'standard'}")
            arg = self.pick("Mode", self.mode_choices(), self.mode or modes.OFF)
            if not arg:
                return
        name = arg.strip().lower()
        if name == modes.OFF:
            if not self.mode:
                print("Already on standard self.md.")
                return
            self.mode, self.mode_text = None, ""
            print("Mode off. Back to standard self.md.")
            note = "Mode turned off. Return to your standard behavior."
        else:
            layers = modes.scan(config.MODES_DIR, config.MODE_MAX_CHARS)
            layer = layers.get(name)
            if layer is None:
                print(f"No mode called '{name}'. Add {name}.md to '{config.MODES_DIR.name}' or type /mode to list them.")
                return
            if layer["too_long"]:
                print(f"{name}.md is {len(layer['text'])} characters; the limit is {config.MODE_MAX_CHARS}. "
                      f"Staying on {self.mode or 'standard'}.")
                return
            if name == self.mode and layer["text"] == self.mode_text:
                print(f"Already in {name} mode.")
                return
            self.mode, self.mode_text = name, layer["text"]
            print(f"Mode: {name}. It applies from your next message.")
            note = f"Mode changed to {name}. Follow the Active mode section from now on."
        self.mode_note = note if self.memory.history else None

    def set_context(self, arg):
        facts, budget = self.model_facts(self.agent.model), context.budget()
        if not arg:
            print(f"  Context  {context.format_size(self.agent.num_ctx)} tokens  (model: {self.agent.model}, "
                  f"max {context.format_size(facts['max_ctx']) if facts['max_ctx'] else 'unknown'})")
            print(f"  Locked at {context.format_size(config.CONTEXT_LOCK)} (CONTEXT_LOCK in config.py)")
            print(f"  GPU memory available: ~{budget / context.GiB:.1f} GB. Estimated memory per size:")
            for size, note in self.context_choices():
                marker = "←" if context.parse_size(size) == self.agent.num_ctx else " "
                print(f"   {marker} {size:>5}  {note}")
            print(f"  {GREY}Bigger windows remember more but make long conversations slower "
                  f"(every turn re-reads the whole conversation).{RESET}")
            arg = self.pick("Context size", self.context_choices(), context.format_size(self.agent.num_ctx))
            if not arg:
                return
        size = context.parse_size(arg)
        if not size or size < 2048:
            print(f"Not a valid size: {arg!r}. Try e.g. /context 64k.")
            return
        if size > config.CONTEXT_LOCK:
            print(f"Locked: the context can't go past {context.format_size(config.CONTEXT_LOCK)}. "
                  f"Change CONTEXT_LOCK in config.py to raise it.")
            return
        if facts["max_ctx"] and size > facts["max_ctx"]:
            print(f"{self.agent.model} supports at most {context.format_size(facts['max_ctx'])}.")
            return
        total = context.estimate(facts, size)
        fit = context.verdict(total, budget)
        if fit == "too big":
            print(f"Not set: {context.format_size(size)} needs ~{total / context.GiB:.1f} GB, but only "
                  f"~{budget / context.GiB:.1f} GB is available; the model would crash. Type /context to see what fits.")
            return
        self.agent.num_ctx = size
        note = f" (~{total / context.GiB:.1f} GB, {fit})" if total is not None else " (memory use unknown for this model)"
        print(f"Context set to {context.format_size(size)}{note}. The model reloads on the next request. "
              f"(This session only; set NUM_CTX in config.py to change the default.)")

    def status(self):
        tokens = sum(len(m["content"] or "") for m in self.memory.messages) // 4
        folder = os.getcwd().replace(os.path.expanduser("~"), "~", 1)
        print(f"  Model         {self.agent.model}")
        print(f"  Mode          {self.mode or 'standard'}")
        print(f"  Context       {context.format_size(self.agent.num_ctx)} tokens (/context)")
        print(f"  Folder        {folder}")
        print(f"  Conversation  {len(self.memory.history)} messages, ~{tokens} tokens incl. system prompt")
        try:
            loaded = inference.loaded()
            text = ", ".join(f"{m['name']} ({inference.LABELS[m['driver']]}, {m.get('context_length') or '?'} ctx)"
                             for m in loaded) or "none"
        except Exception:
            text = "unknown"
        guard = inference.settings()["guard_gb"]
        print(f"  Engine        {inference.driver().label}, memory guard "
              f"{f'{guard:g} GB' if guard else 'off'} (/options)")
        print(f"  Loaded        {text}")
        pending = self.agent.pending_task()
        print(f"  Unfinished    {pending[:60]!r} (/resume)" if pending else "  Unfinished    none")


def main() -> None:
    if sys.platform == "darwin":
        # Keep the Mac from idle-sleeping while the agent runs; caffeinate
        # exits on its own when this process ends.
        try:
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        except OSError:
            pass  # Echo still works; the Mac may just sleep
    cli = CLI()
    cli.open_panel()
    cli.loop()


if __name__ == "__main__":
    main()
