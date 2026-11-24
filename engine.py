"""The agent loop: call the model, run the tools it asks for, repeat.

Includes a watchdog: a failed or stalled model call is retried from the same
turn (restarting Ollama if it is down), and progress is checkpointed every
turn so an interrupted task can be resumed. The user can interrupt a running
task (Interrupted) and redirect it with new instructions, and must approve
tool calls that need permission (e.g. deleting files).
"""

import inspect
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import inference
import memory_settings
import notes
from gate import is_degenerate

GREY, YELLOW, GREEN, RED, CYAN, RESET = (
    "\033[90m", "\033[33m", "\033[32m", "\033[31m", "\033[36m", "\033[0m"
)

ANSI = re.compile(r"\033\[[0-9;]*[A-Za-z]")

# When the history grows past half the context window, the content of the
# oldest written files (longer than OMIT_CONTENT_OVER) is replaced by a note
# starting with OMITTED_PREFIX.
OMIT_CONTENT_OVER = 2000
OMITTED_PREFIX = "[harness note:"


LOAD_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "load_tool",
        "description": "Load an on-demand tool listed in the pilot so you can call it. "
                       "It stays loaded for the rest of the task.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Tool name from the pilot."}},
            "required": ["name"],
        },
    },
}


LOAD_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": "Load a skill listed in your prompt: you get its instructions and its tools. "
                       "It stays loaded for the rest of the conversation.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Skill name."}},
            "required": ["name"],
        },
    },
}


TIME_LIMIT_TEXT = "Stopped: reached the task's time limit."
CONTEXT_END_TEXT = ("Stopped: the context window filled and task notes are off, so this run ends here. "
                    "The next cycle starts fresh.")
FILE_TOOLS = ("write_file", "append_file")

MEMORY_UPDATE_PROMPT = (
    "[Harness: your context window is {percent}% full, and the user chose a memory layer update. "
    "Update your memory layers now, then reply with just 'done'. Your context will be cleared afterward and "
    "you'll continue from your memory layers alone, so be nuanced.\n"
    "- transient_edit: the nuance you'd need to pick this back up, appended to each project's own entry (new for a "
    "new project): project names and any renames or moves, "
    "locations and paths, and for code: dependencies, ports, functions, variables, what was changed and why.\n"
    "- ephemeral_save: a summary of this conversation so far, with footnotes.\n"
    "- interaction_seed: update this conversation's seed topics (short cues).\n"
    "Don't use the vault or memory_save.]"
)
COMPACT_PROMPT = (
    "[Harness: your context window is {percent}% full, so the conversation is being compacted. Reply with only a "
    "summary of the conversation so far that you could continue from: what was asked, what was done and "
    "decided, names and paths, and what is still open. No tool calls.]"
)
IDLE_UPDATE_PROMPT = (
    "[Harness: the user has been away for {minutes} minutes, so your model will be unloaded and your context "
    "cleared to free memory. Update your memory layers now, then reply with just 'done'. You'll continue from "
    "them alone, so be nuanced.\n"
    "- transient_edit, in each project's own entry: project names and any renames or moves, locations and paths, "
    "and for code: dependencies, "
    "ports, functions, variables, what was changed and why.\n"
    "- ephemeral_save: a summary of this conversation so far, with footnotes.\n"
    "- interaction_seed: update this conversation's seed topics (short cues).\n"
    "Don't use the vault or memory_save.]"
)
TASK_NOTES_PROMPT = (
    "[Harness: {reason} Write your task notes now with task_notes: a nuanced summary of the work so far, "
    "what is finished, what remains, and everything you need to continue (names, paths, decisions). "
    "Writing them replaces any earlier notes, so include everything still relevant. Then reply with just 'ready'.]"
)


class Interrupted(BaseException):
    """Raised in the main thread when the user interrupts the agent (Esc).
    A BaseException, like KeyboardInterrupt, so error handlers don't swallow it."""


class QuietActivity:
    """For sub-agents: they work silently inside the launcher's tool call."""

    def start(self):
        pass

    def stop(self):
        pass

    def status(self, label, show_now=True):
        pass

    def write(self, text):
        pass

    def line(self, text):
        pass


class Activity:
    """Owns the terminal while the agent works: all output goes through
    write(), and an animated status line appears whenever nothing has been
    printed for a moment (model writing a tool call, a tool running, ...),
    so the agent never looks frozen."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    IDLE_DELAY = 1.0  # seconds without output before the status line shows

    def __init__(self, hint=""):
        self._hint = hint
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._label = "Working"
        self._since = time.monotonic()
        self._last_output = float("-inf")
        self._drawn = False
        self._at_line_start = True

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():  # an interrupt can land before start()
            self._thread.join()
        with self._lock:
            self._erase()
            sys.stdout.flush()

    def status(self, label, show_now=True):
        """Set the status label; show_now displays it without the idle delay."""
        with self._lock:
            self._label = label
            self._since = time.monotonic()
            if show_now:
                self._last_output = float("-inf")

    def write(self, text):
        if not text:
            return
        with self._lock:
            self._erase()
            sys.stdout.write(text)
            sys.stdout.flush()
            plain = ANSI.sub("", text)
            if plain:
                self._at_line_start = plain.endswith("\n")
            self._last_output = time.monotonic()

    def line(self, text):
        """Print text as its own line(s)."""
        self.write(("" if self._at_line_start else "\n") + text + "\n")

    def _erase(self):
        if self._drawn:
            sys.stdout.write("\r\033[K")
            self._drawn = False

    def _spin(self):
        frame = 0
        while not self._stop.wait(0.1):
            with self._lock:
                now = time.monotonic()
                if now - self._last_output < self.IDLE_DELAY:
                    continue
                if not self._at_line_start:  # don't draw over streamed text
                    sys.stdout.write("\n")
                    self._at_line_start = True
                elapsed = int(now - max(self._since, self._last_output))
                symbol = self.FRAMES[frame % len(self.FRAMES)]
                sys.stdout.write(f"\r{CYAN}{symbol}{RESET} {self._label} {GREY}{elapsed}s{self._hint}{RESET}\033[K")
                sys.stdout.flush()
                self._drawn = True
                frame += 1


def _speeds(done, think_tokens, think_secs):
    """Tokens per second for one turn, from Ollama's final stream chunk.
    Ollama counts all generated tokens together, so thinking is measured from
    its stream chunks (one per token) and writing is the rest."""
    def rate(tokens, seconds):
        return round(tokens / seconds, 1) if tokens > 0 and seconds > 0 else None

    generated = done.get("eval_count", 0)
    generating_secs = done.get("eval_duration", 0) / 1e9
    processed = done.get("prompt_processed", done.get("prompt_eval_count", 0))  # cached tokens aren't read again
    return {
        "decoding": rate(processed, done.get("prompt_eval_duration", 0) / 1e9),
        "thinking": rate(think_tokens, think_secs),
        "writing": rate(generated - think_tokens, generating_secs - think_secs),
    }


class AgentEngine:
    def __init__(self, config, tools, memory, verbose=True, ask=None, on_turn=None, gate=None,
                 on_full=None, on_flush=None, task_notes=None, deadline=None, driver=None,
                 checkpoints=True, skills=None, quiet=False):
        """ask: optional callback ask(prompt) -> str that asks the user a
        question in the terminal. Without it, the agent can't be redirected
        and tool calls that need permission are declined. on_turn: optional
        callback on_turn(stats) after each completed model call. gate:
        optional gate.Gate that checks every tool call.

        Context resets: on_full(percent) -> "update", "compact", or None asks
        the user (chat) once the context passes CONTEXT_PROMPT_AT; on_flush()
        runs after a flush. task_notes (a task's notes store, with read()) makes
        a task write notes and continue instead. deadline: time.monotonic()
        after which a task writes its notes and stops. driver: the inference
        driver (default: the one picked in /options). skills: {name: skill}
        the agent may load with load_skill. quiet: no output (sub-agents)."""
        self.config = config
        self._driver = driver
        self.gate = gate
        self.model = config.MODEL
        self.num_ctx = min(config.NUM_CTX, config.CONTEXT_LOCK)  # /context changes it
        self.ask = ask
        self.on_turn = on_turn
        self.on_full = on_full
        self.on_flush = on_flush
        self.task_notes = task_notes
        self.deadline = deadline
        self._hint = " · esc to interrupt" if ask else ""
        self.tools = dict(tools)  # skills add to this copy, never to the shared table
        self.skills = dict(skills or {})
        self.loaded_skills = set()
        self.quiet = quiet
        self.memory = memory
        self.verbose = verbose
        self.checkpoints = checkpoints
        self.loaded = set()  # on-demand tools loaded during the current task
        self.last_tokens = 0  # context used by the latest model call, from Ollama's counts
        self.changed = []  # files written during the current task
        self._snoozed = False

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    @property
    def driver(self):
        return self._driver or inference.driver()

    def idle_update(self, minutes):
        """The user is away: update the memory layers, then flush the context.
        The caller unloads the model afterward."""
        self.activity = self._activity()
        self.activity.start()
        try:
            self.activity.line(f"{CYAN}Idle for {minutes} minutes: Echo is saving to its memory layers...{RESET}")
            self.loaded = set()
            self._side_job(self.memory.messages, IDLE_UPDATE_PROMPT.format(minutes=minutes),
                           tools=("ephemeral_save", "interaction_search"))
            self.memory.flush(notes.grounding())
            self.last_tokens = 0
            if self.on_flush:
                self.on_flush()
        finally:
            self.activity.stop()

    def run(self, user_query):
        """Handle one user request and return the agent's final answer."""
        messages = self.memory.messages + [{"role": "user", "content": user_query}]
        return self._loop(user_query, messages, first_turn=1)

    def pending_task(self):
        """The task text of an unfinished checkpoint, or None."""
        checkpoint = self._load_checkpoint()
        return checkpoint["task"] if checkpoint else None

    def resume(self):
        """Continue an unfinished task from the turn where it stopped."""
        checkpoint = self._load_checkpoint()
        if checkpoint is None:
            return "Nothing to resume."
        print(f"{CYAN}Resuming at turn {checkpoint['turn']}: {_preview(checkpoint['task'], 80)}{RESET}")
        messages = checkpoint["messages"]
        notes.conversation_id = checkpoint.get("conversation") or notes.conversation_id  # same seed
        self.memory.carry = checkpoint.get("carry") or self.memory.carry
        self.changed = checkpoint.get("changed") or []
        if messages and messages[0]["role"] == "system":
            messages[0]["content"] = self.memory.full_system_prompt  # the current mode applies
        return self._loop(checkpoint["task"], messages, checkpoint["turn"], keep_changed=True)

    # ------------------------------------------------------------------
    # agent loop
    # ------------------------------------------------------------------

    def _loop(self, task, messages, first_turn, keep_changed=False):
        self.loaded = set()
        self._snoozed = False
        if not keep_changed:
            self.changed = []
        if self.gate:
            self.gate.start_run()
        self.activity = self._activity()
        self.activity.start()
        try:
            return self._turns(task, messages, first_turn)
        finally:
            self.activity.stop()

    def _turns(self, task, messages, first_turn):
        max_turns = self.config.MAX_TURNS
        turn = first_turn
        while turn <= max_turns:
            try:
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    self._write_task_notes(messages, "This task reached its time limit and stops now.")
                    final_text = TIME_LIMIT_TEXT + (" Task notes were saved; the next cycle continues from them."
                                                    if self.task_notes else "")
                    self.activity.line(f"{YELLOW}{final_text}{RESET}")
                    break
                if self._context_full():
                    messages = self._reset(task, messages, started=turn > first_turn)
                    if messages is None:  # a task with task notes off that ends when full
                        final_text = CONTEXT_END_TEXT
                        self.activity.line(f"{YELLOW}{final_text}{RESET}")
                        break
                self._trim_history(messages)
                self._save_checkpoint(task, messages, turn)
                try:
                    reply = self._chat_with_watchdog(messages, turn)
                except inference.GuardError as exc:
                    final_text = f"Stopped: {exc}"
                    self.activity.line(f"{RED}{final_text}{RESET}")
                    break
                if reply is None:
                    # Leave the checkpoint in place so the task can be resumed.
                    final_text = "Stopped: the model is not responding. Progress is saved; type /resume to continue."
                    self.activity.line(f"{RED}{final_text}{RESET}")
                    return final_text

                messages.append(reply)
                tool_calls = reply.get("tool_calls")
                if not tool_calls:
                    final_text = reply["content"] or "(no response from model)"
                    break
                self._run_tool_calls(tool_calls, messages, turn)
                if self.gate and self.gate.stuck:
                    final_text = "Stopped: the agent kept repeating the same step with the same result."
                    self.activity.line(f"{RED}{final_text}{RESET}")
                    break
            except Interrupted:
                redirect = self._redirect()
                if not redirect:
                    final_text = "Stopped by the user."
                    break
                task += f"\n[Redirected by the user] {redirect}"
                messages.append({"role": "user", "content": f"[The user interrupted you] {redirect}"})
            turn += 1
        else:
            final_text = f"Stopped: reached the maximum number of turns ({max_turns})."
            self.activity.line(f"{RED}{final_text}{RESET}")

        self.memory.add("user", task)
        self.memory.add("assistant", final_text)
        self.memory.compact(memory_settings.get("history_messages"))
        self._clear_checkpoint()
        return final_text

    # ------------------------------------------------------------------
    # context resets
    # ------------------------------------------------------------------

    def _percent(self):
        return round(100 * self.last_tokens / self.num_ctx) if self.num_ctx else 0

    def _context_full(self):
        if self._snoozed or not self.num_ctx or not (self.on_full or self.task_notes):
            return False
        return self.last_tokens >= memory_settings.get("context_trigger") / 100 * self.num_ctx

    def _side_job(self, messages, instruction, tools=()):
        """Have the model do one bounded job (write memory, notes, or a
        summary) on top of the current context. Returns its final text."""
        self.loaded |= {t for t in tools if t in self.tools}
        work = messages + [{"role": "user", "content": instruction}]
        for turn in range(1, self.config.RESET_MAX_TURNS + 1):
            reply =self._chat_with_watchdog(work, f"memory {turn}/{self.config.RESET_MAX_TURNS}")
            if reply is None:
                return ""
            work.append(reply)
            if not reply.get("tool_calls"):
                return reply.get("content") or ""
            self._run_tool_calls(reply["tool_calls"], work, f"memory {turn}")
        return ""

    def _write_task_notes(self, messages, reason):
        if self.task_notes:
            self._side_job(messages, TASK_NOTES_PROMPT.format(reason=reason), tools=("task_notes",))

    def _reset(self, task, messages, started):
        """The context passed CONTEXT_PROMPT_AT: write memory, then flush and
        continue from it. Returns the new message list."""
        percent = self._percent()
        if self.task_notes:
            self._write_task_notes(messages, f"Your context window is {percent}% full, so it will be cleared.")
            self.last_tokens = 0
            notes_text = self.task_notes.read() or "(you didn't write any)"
            prefix = [messages[0]] if messages and messages[0]["role"] == "system" else []
            return prefix + [{"role": "user", "content": self._continue_note(task, notes_text)}]

        self.activity.stop()
        try:
            choice = self.on_full(percent)
        finally:
            self.activity = self._activity()
            self.activity.start()
        if choice == "end":
            return None
        if choice not in ("update", "compact"):
            self._snoozed = True  # asked again on the next request
            return messages
        if choice == "update":
            self.activity.line(f"{CYAN}Memory layer update: Echo is saving to its memory layers...{RESET}")
            self._side_job(messages, MEMORY_UPDATE_PROMPT.format(percent=percent),
                           tools=("ephemeral_save", "interaction_search"))
            carry = notes.grounding()
        else:
            self.activity.line(f"{CYAN}Compacting the conversation...{RESET}")
            rubric = memory_settings.rubric("compact")
            instruction = COMPACT_PROMPT.format(percent=percent)
            if rubric:
                instruction = instruction[:-1] + f"\nFormat to follow:\n{rubric}]"
            summary = self._side_job(messages, instruction).strip()
            carry = ("# Before the compaction\nThe conversation was compacted. Your summary of it so far:\n\n"
                     + (summary or "(no summary was written)"))
        self.memory.flush(carry)
        self.last_tokens = 0
        if self.on_flush:
            self.on_flush()
        self.activity.line(f"{GREEN}Context cleared. Continuing from memory.{RESET}")
        fresh = self.memory.messages
        if not started and messages and messages[-1]["role"] == "user":
            return fresh + [messages[-1]]  # the request that was about to be sent
        return fresh + [{"role": "user", "content": self._continue_note(task, None)}]

    def _continue_note(self, task, task_notes):
        lines = ["[Harness note: your context was just cleared in the middle of this work, so you are "
                 "continuing from your notes, not starting over.]", "", "The work:", task]
        if task_notes is not None:
            lines += ["", "Your task notes:", task_notes]
        if self.changed:
            lines += ["", "Files you already created or changed (finished work, already on disk):"]
            lines += [f"- {path}" for path in self.changed]
        lines += ["", "Continue from where you left off. Don't redo finished steps, and don't overwrite "
                  "work that's already done; read a file first if you need to check it."]
        return "\n".join(lines)

    def _run_tool_calls(self, tool_calls, messages, turn):
        for i, call in enumerate(tool_calls):
            name = call["function"]["name"]
            args = call["function"].get("arguments") or {}
            if self.verbose:
                self.activity.line(f"\n{YELLOW}[turn {turn}] {name}({_preview(json.dumps(args), 100)}){RESET}")
            self.activity.status(f"Running {name}...")
            try:
                result = self._run_tool(name, args)
            except Interrupted:
                # Every tool call gets a result, so the history stays consistent.
                for skipped in tool_calls[i:]:
                    messages.append({"role": "tool", "tool_name": skipped["function"]["name"],
                                     "content": "Not completed: interrupted by the user."})
                raise
            if self.verbose:
                self.activity.line(f"{GREEN}   -> {_preview(result, 100)}{RESET}")
            messages.append({"role": "tool", "tool_name": name, "content": result})

    def _redirect(self):
        """Ask the user what to do instead of the interrupted work."""
        return self._ask_user(f"{YELLOW}⏹ Interrupted.{RESET}", self.config.REDIRECT_PROMPT)

    def _permission_granted(self, reason):
        answer = self._ask_user(f"{YELLOW}⚠ Permission needed. {reason}{RESET}", "Allow? [y/N] ")
        return answer.lower() in ("y", "yes")

    def _ask_user(self, header, prompt):
        """Pause the output, show header, and return the user's answer
        ("" when there is no terminal to ask)."""
        if not self.ask:
            return ""
        self.activity.stop()
        print(f"\n{header}")
        try:
            return self.ask(prompt).strip()
        finally:
            self.activity = self._activity()
            self.activity.start()

    def _activity(self):
        return QuietActivity() if self.quiet else Activity(self._hint)

    def _trim_history(self, messages):
        """Written files stay in the history so the model remembers what it
        wrote (class names, ids, ...). Only once the history passes about half
        the context window (~4 characters per token) is the content of the
        oldest large writes replaced by a short note; the model can read_file
        them again."""
        budget = self.num_ctx * 2
        size = sum(len(json.dumps(m)) for m in messages)
        for message in messages:
            for call in message.get("tool_calls") or []:
                if size <= budget:
                    return
                args = call["function"].get("arguments")
                if call["function"]["name"] != "write_file" or not isinstance(args, dict):
                    continue
                content = str(args.get("content", ""))
                if len(content) > OMIT_CONTENT_OVER and not content.startswith(OMITTED_PREFIX):
                    note = (f"{OMITTED_PREFIX} {len(content)} characters of file content were "
                            "removed from this history to save space; use read_file to see the file]")
                    args["content"] = note
                    size -= len(content) - len(note)

    # ------------------------------------------------------------------
    # watchdog
    # ------------------------------------------------------------------

    def _chat_with_watchdog(self, messages, turn):
        """Call the model, retrying the same turn if the call fails or stalls.
        Returns the reply, or None once the retries are used up. A model that
        doesn't fit under the memory guard isn't retried (GuardError)."""
        retries = self.config.MAX_RETRIES
        for attempt in range(retries + 1):
            label = f"turn {turn}/{self.config.MAX_TURNS}"
            if attempt:
                label += f", retry {attempt}/{retries}"
            try:
                return self._chat(messages, label)
            except inference.GuardError:
                raise
            except Exception as exc:  # timeout/stall, connection error, server error
                self.activity.line(f"{RED}[watchdog] turn {turn} failed: {type(exc).__name__}: {exc}{RESET}")
                if attempt == retries:
                    return None
                if not self.driver.healthy():
                    self._restart_server()
        return None

    def ollama_api(self, path, body=None):
        """An Ollama API call, e.g. ollama_api("/api/tags")."""
        return inference.ollama_api(path, body)

    def _restart_server(self):
        driver = self.driver
        self.activity.line(f"{YELLOW}[watchdog] {driver.label} is not responding; restarting it.{RESET}")
        self.activity.status(f"Waiting for {driver.label} to come back...")
        if driver.restart():
            self.activity.line(f"{GREEN}[watchdog] {driver.label} is back.{RESET}")
        else:
            self.activity.line(f"{RED}[watchdog] {driver.label} did not come back.{RESET}")

    # ------------------------------------------------------------------
    # checkpoint
    # ------------------------------------------------------------------

    def _save_checkpoint(self, task, messages, turn):
        if not self.checkpoints:
            return
        path = self.config.CHECKPOINT_FILE
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"task": task, "turn": turn, "messages": messages, "conversation": notes.conversation_id,
                       "carry": self.memory.carry, "changed": self.changed}, f)
        os.replace(path + ".tmp", path)  # atomic: never leaves a half-written file

    def _load_checkpoint(self):
        try:
            with open(self.config.CHECKPOINT_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, AttributeError, TypeError):
            return None

    def _clear_checkpoint(self):
        if not self.checkpoints:
            return  # tasks and sub-agents never touch chat's /resume checkpoint
        try:
            os.remove(self.config.CHECKPOINT_FILE)
        except (FileNotFoundError, AttributeError, TypeError):
            pass

    # ------------------------------------------------------------------
    # model call
    # ------------------------------------------------------------------

    def _chat(self, messages, label):
        """Stream one model response to the terminal and return it as an
        assistant message: {"role", "content", and "tool_calls" if any}."""
        driver = self.driver
        self.activity.status(f"Loading {self.model} ({driver.label})...")
        self.num_ctx = driver.prepare(self.model, self.num_ctx)  # memory guard, one model at a time
        for notice in driver.take_notices():
            self.activity.line(f"{YELLOW}{notice}{RESET}")

        content, tool_calls = [], []
        in_thinking = started = False
        think_tokens, think_start, think_end, done = 0, None, None, None
        tail = ""  # the latest output, to catch a collapse like "7, 7, 7, 7"
        out = self.activity
        out.status(f"Waiting for the model ({label})...")
        try:
            for chunk in driver.chat(self.model, messages, self._schemas(), self.num_ctx,
                                     self.config.REQUEST_TIMEOUT):
                msg = chunk.get("message", {})
                if not started and (msg.get("thinking") or msg.get("content")):
                    # From here on, any pause is the model still working:
                    # Ollama sends nothing while it writes a tool call.
                    out.status(f"Working ({label})...", show_now=False)
                    started = True
                if msg.get("thinking"):
                    think_end = time.monotonic()
                    think_start = think_start if think_start is not None else think_end
                    think_tokens += 1
                    if not in_thinking:
                        out.line(f"\n{GREY}[thinking]{RESET}")
                        in_thinking = True
                    out.write(f"{GREY}{msg['thinking']}{RESET}")
                if msg.get("content"):
                    if in_thinking:
                        out.write("\n\n")
                        in_thinking = False
                    out.write(msg["content"])
                    content.append(msg["content"])
                if msg.get("tool_calls"):
                    tool_calls.extend(msg["tool_calls"])
                tail = (tail + (msg.get("thinking") or "") + (msg.get("content") or ""))[-80:]
                if is_degenerate(tail):
                    raise RuntimeError("the model's output collapsed into a repeating loop")

                if chunk.get("done"):
                    done = chunk
                    break
            else:
                raise ConnectionError("stream ended before the response was complete")
        finally:
            if in_thinking or content:
                out.write("\n")

        reply = {"role": "assistant", "content": "".join(content)}
        if tool_calls:
            reply["tool_calls"] = tool_calls
        # Everything the model saw plus what it wrote: the context in use now.
        self.last_tokens = done.get("prompt_eval_count", 0) + done.get("eval_count", 0)
        if self.on_turn:
            think_secs = think_end - think_start if think_tokens else 0
            self.on_turn({"turn": label, "tokens": self.last_tokens, **_speeds(done, think_tokens, think_secs)})
        return reply

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------

    def _schemas(self):
        """Tools sent to the model: everything not on-demand, the on-demand
        tools loaded so far, load_tool while there is something to load, and
        load_skill while a skill is left to load."""
        in_task = self.in_task()
        shown = {n: t for n, t in self.tools.items() if not memory_settings.hidden(n, in_task)}
        schemas = [memory_settings.schema(n, t["schema"]) for n, t in shown.items()
                   if not t.get("on_demand") or n in self.loaded]
        if any(t.get("on_demand") and n not in self.loaded for n, t in shown.items()):
            schemas.append(LOAD_TOOL_SCHEMA)
        if set(self.skills) - self.loaded_skills:
            schemas.append(LOAD_SKILL_SCHEMA)
        return schemas

    def in_task(self):
        """A scheduled task or sub-agent: nobody is there to answer a question."""
        return self.task_notes is not None or bool(self.gate and self.gate.policy)

    def _load_skill(self, name=None, **_):
        skill = self.skills.get(name)
        if skill is None:
            return f"Error: no skill named '{name}'. Available: {', '.join(self.skills) or 'none'}."
        try:
            added = skill.tools()
        except Exception as exc:
            return f"Error: the skill '{name}' couldn't load its tools: {type(exc).__name__}: {exc}"
        self.tools.update(added)
        self.loaded_skills.add(name)
        listed = f"\n\nIts tools, ready to call: {', '.join(added)}." if added else ""
        return f"Skill {name} loaded.\n\n{skill.instructions}{listed}"

    def launcher(self):
        """What a skill tool may know about the agent that called it."""
        policy = self.gate.policy if self.gate else None
        return {"model": self.model, "num_ctx": self.num_ctx, "tokens": self.last_tokens,
                "driver": self.driver, "activity": getattr(self, "activity", None),
                "areas": list(policy["areas"]) if policy else None,
                "powers": set(policy["powers"]) if policy else None}

    def _load_tool(self, name=None, **_):
        tool = self.tools.get(name)
        if tool is not None and memory_settings.hidden(name, self.in_task()):
            return "Error: the memory vault is off. The user changes that in /options → Memory layers."
        if tool is None or not tool.get("on_demand"):
            available = [n for n, t in self.tools.items()
                         if t.get("on_demand") and not memory_settings.hidden(n, self.in_task())]
            return f"Error: '{name}' is not an on-demand tool. Available: {', '.join(available)}."
        self.loaded.add(name)
        return f"Loaded {name}. You can call it now."

    def _run_tool(self, name, args):
        """Run a tool and return its result as text. Errors are returned to the
        model as text so it can correct itself. An on-demand tool that wasn't
        loaded still runs if the model calls it by name."""
        if name == "load_tool":
            return self._load_tool(**args) if isinstance(args, dict) else self._load_tool(args)
        if name == "load_skill":
            return self._load_skill(**args) if isinstance(args, dict) else self._load_skill(args)
        tool = self.tools.get(name)
        if tool is None:
            return f"Error: unknown tool '{name}'. Available: {', '.join(self.tools)}."
        if isinstance(args, str):  # some models send arguments as a JSON string
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return f"Error: arguments for '{name}' are not valid JSON."
        if not isinstance(args, dict):
            return f"Error: invalid arguments for '{name}': arguments must be a dict"
        if memory_settings.hidden(name, self.in_task()):
            return "Error: the memory vault is off. The user changes that in /options → Memory layers."
        if name == "write_file" and str(args.get("content", "")).startswith(OMITTED_PREFIX):
            return "Error: that is a history placeholder, not file content. Write the real file content."
        blocked = self.gate.check(name, args) if self.gate else None
        if blocked:
            return blocked
        # A task's powers were approved when it was created; its gate policy decides.
        check = None if self.gate and self.gate.policy else tool.get("needs_permission")
        try:
            reason = check(**args) if check else None
        except TypeError as exc:
            return f"Error: invalid arguments for '{name}': {exc}"
        reason = reason or memory_settings.needs_permission(name, args)  # the vault on Ask first
        if reason and not self._permission_granted(reason):
            return ("Permission denied: the user did not allow this. Don't try to do it "
                    "another way; continue without it, or ask the user in your final reply.")
        extra = {"launcher": self.launcher()} if tool.get("launcher") else {}
        try:
            inspect.signature(tool["function"]).bind(**args, **extra)
        except TypeError as exc:
            return f"Error: invalid arguments for '{name}': {exc}"
        except ValueError:
            pass  # no signature to check (a builtin); the call reports any problem
        try:
            result = str(tool["function"](**args, **extra))
        except Exception as exc:  # a bug inside the tool, not bad arguments
            result = f"Error: {name} failed: {type(exc).__name__}: {exc}"
        if name in FILE_TOOLS and not result.startswith("Error"):
            path = str(Path(str(args.get("path", ""))).expanduser().resolve())
            if path not in self.changed:
                self.changed.append(path)
        if self.gate:
            self.gate.observe(name, args, result)
        return _truncate(result, memory_settings.output_limit(name) or self.config.MAX_TOOL_OUTPUT_CHARS)


def _truncate(text, limit):
    """Keep the head and tail of long output; the end often holds the error."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} characters truncated] ...\n{text[-half:]}"


def _preview(text, limit):
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 3] + "..."
