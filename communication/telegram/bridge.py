"""The Telegram conversation, served by the background runner.

Two threads: the poller asks Telegram for new messages (long polling) and
hands them to the worker, which answers them one at a time. Only the paired
account is answered; anyone else gets no reply at all.

Echo here is the same Echo (self, user, seeds, skills, tools) in its own
conversation, on the model, driver, and context picked in /telegram. It waits
its turn for the model (turns.py) while the terminal chat or a task is using
it. Anything that asks permission in the terminal asks here with Allow and
Deny buttons; no answer within TELEGRAM_APPROVAL_MINUTES counts as Deny.

A conversation ends after the idle time picked in /telegram (default 10 min)
without a message from the user, or when they send /end. The harness then appends
it to data/conversations.jsonl, where Echo's recall tools can read it, and
unloads the model. Urgent reminders (like a security incident) are pushed to
the phone as they appear.
"""

import os
import queue
import secrets
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import config
import context
import engine
import errorlog
import gate
import inference
import interaction
import memory
import reminders
import seeds
import skills
import tools
import turns
from communication.telegram import store
from communication.telegram.api import Api, Conflict, TelegramError

HELLO = (f"{config.AGENT_NAME} here. Send a message to talk. /bye ends the conversation and saves it (it also "
         "ends by itself after a quiet while). /tasks lists your tasks. /help shows this again.")
COMMANDS = [("bye", "End the conversation and save it"), ("tasks", "List your tasks"),
            ("help", "What I can do here")]
TASK_TOOLS = ("list_tasks", "run_task", "create_task")  # always loaded here, so small models see them
TEXT_ONLY = f"{config.AGENT_NAME} only reads text messages for now."


def telegram_note():
    return (f"[You're talking with {config.USER_NAME} on Telegram, on their phone. Keep replies short and in "
            "plain text: no tables, headings, or markdown. Anything that needs permission shows him Allow and "
            "Deny buttons. You can list their tasks (list_tasks) and run one now when they ask (run_task), even while "
            "they're away; they get the result here. Past Telegram conversations: telegram_conversations and "
            "telegram_read.]")


def _when_full(percent):
    """The memory layers' setting; "ask me" compacts, since nobody is at the terminal."""
    import memory_settings
    action = memory_settings.get("context_action")
    return "compact" if action == "ask" else action


class TelegramEngine(engine.AgentEngine):
    """The agent with permission asked through Telegram buttons."""

    def __init__(self, approve, **kwargs):
        super().__init__(**kwargs)
        self.approve = approve

    def _permission_granted(self, reason):
        return self.approve(reason)


def choice():
    """(driver name, model, context size) Telegram uses now."""
    current = store.settings()
    engine_settings = inference.settings()
    name = current["driver"] if current["driver"] in inference.DRIVERS else engine_settings["driver"]
    model = current["model"] or engine_settings["models"].get(name) or config.MODEL
    return name, model, current["num_ctx"] or config.NUM_CTX


class Bridge:
    def __init__(self, api=None, stop=None):
        self.api = api
        self.stop = stop or threading.Event()
        self.inbox = queue.Queue()
        self.lock = threading.Lock()  # guards the conversation between the two threads
        self.busy = False
        self.approval = None
        self.conversation = store.current()  # one left by a restart continues
        self.agent = None
        self.driver = None

    # -- the poller ----------------------------------------------------------------

    def serve(self):
        worker = threading.Thread(target=self.work, name="telegram-worker", daemon=True)
        worker.start()
        backoff, menu_set = 1, False
        try:
            while not self.stop.is_set() and store.ready():
                token = store.token()
                if self.api is None or self.api.token != token:
                    self.api = Api(token)
                try:
                    if not menu_set:  # the command list Telegram shows when you type /
                        self.api.call("setMyCommands", {"commands": [{"command": c, "description": d}
                                                                     for c, d in COMMANDS]})
                        menu_set = True
                    self.push_reminders()
                    self.expire()
                    self.poll_once()
                    backoff = 1
                except Conflict:
                    store.save_state(status="Another program is reading this bot's messages (is another agent "
                                            "using the same bot?). Retrying every minute.")
                    self.stop.wait(60)
                except (TelegramError, OSError) as exc:
                    store.save_state(status=f"Can't reach Telegram: {exc}")
                    self.stop.wait(backoff)
                    backoff = min(backoff * 2, 60)
                except Exception as exc:
                    errorlog.log_error("telegram.serve", exc)
                    self.stop.wait(backoff)
                    backoff = min(backoff * 2, 60)
        finally:
            self.stop.set()
            worker.join(timeout=5)
            self.unload()

    def poll_once(self, wait=None):
        offset = store.state()["offset"]
        updates = self.api.updates(offset, config.TELEGRAM_POLL_SECONDS if wait is None else wait)
        if store.state()["status"]:
            store.save_state(status="")
        for update in updates:
            store.save_state(offset=update["update_id"] + 1)  # handled at most once, even after a crash
            try:
                self.handle(update)
            except Exception as exc:
                errorlog.log_error("telegram.handle", exc)

    def handle(self, update, now=None):
        now = now or time.time()
        owner = store.settings()["user_id"]
        if "callback_query" in update:
            press = update["callback_query"]
            if press.get("from", {}).get("id") != owner:
                return
            self.button(press)
            return
        message = update.get("message") or {}
        sender = message.get("from", {}).get("id")
        if sender != owner or message.get("chat", {}).get("type") != "private":
            store.save_state(ignored=store.state()["ignored"] + 1)  # strangers get nothing
            return
        if now - message.get("date", now) > config.TELEGRAM_STALE_MINUTES * 60:
            return  # sent while Echo was off long ago
        text = message.get("text")
        if text is None:
            self.api.send(owner, TEXT_ONLY)
            return
        command = text.strip().split("@")[0].lower()
        if command in ("/start", "/help"):
            self.api.send(owner, HELLO)
        elif command == "/tasks":
            self.api.send_long(owner, tools.list_tasks())  # straight from the harness, no model
        elif command in ("/bye", "/end", "/new"):
            self.inbox.put(("end", None))
        else:
            self.inbox.put(("text", text))

    def button(self, press):
        kind, _, nonce = str(press.get("data", "")).partition(":")
        waiting = self.approval
        if waiting is None or waiting["nonce"] != nonce:
            self.api.answer_button(press["id"], "That request already ended.")
            return
        waiting["answer"] = kind == "allow"
        waiting["event"].set()
        self.api.answer_button(press["id"], "Allowed." if waiting["answer"] else "Denied.")

    # -- the worker ------------------------------------------------------------------

    def work(self):
        while not self.stop.is_set():
            try:
                kind, text = self.inbox.get(timeout=1)
            except queue.Empty:
                continue
            owner = store.settings()["user_id"]
            try:
                if kind == "end":
                    ended = self.end(f"ended by {config.USER_NAME}")
                    self.api.send(owner, "Goodbye. The conversation is saved and the model is unloaded. "
                                         "Your next message starts a new one."
                                  if ended else "There was no conversation to end.")
                    continue
                reply = self.answer(text, owner)
                if reply is not None:
                    self.api.send_long(owner, reply)
            except Exception as exc:
                errorlog.log_error("telegram.work", exc)
                try:
                    self.api.send(owner, f"Something went wrong: {type(exc).__name__}: {exc}"[:500])
                except Exception:
                    pass

    def answer(self, text, owner):
        """Wait for the model, then answer. Returns the reply (None if stopped)."""
        self.busy = True
        typing = threading.Event()
        threading.Thread(target=self._typing, args=(owner, typing), daemon=True).start()
        try:
            turn = turns.hold()
            if turn is None:
                self.api.send(owner, f"{config.AGENT_NAME} is busy in the terminal or with a task. "
                                     "Your message is next.")
                turn = turns.hold(wait=True, stop=self.stop)
                if turn is None:
                    return None
            try:
                return self.respond(text)
            finally:
                turn.close()
        finally:
            typing.set()
            self.busy = False

    def _typing(self, owner, done):
        while not done.is_set() and not self.stop.is_set():
            if self.approval is None:
                try:
                    self.api.typing(owner)
                except Exception:
                    pass
            done.wait(4.5)

    def respond(self, text, now=None):
        now = now or time.time()
        with self.lock:
            if self.conversation is None:
                name, model, _ = choice()
                self.conversation = {"id": "t" + time.strftime("%Y%m%d-%H%M%S", time.localtime(now)),
                                     "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                                     "driver": name, "model": model, "messages": []}
            if self.agent is None:
                problem = self.build()
                if problem:
                    return problem
            self.conversation["messages"].append({"role": "user", "text": text, "time": _stamp(now)})
            self.conversation["last"] = now
            store.save_current(self.conversation)
        start = os.getcwd()
        try:
            os.chdir(Path.home())  # Echo's working folder on Telegram
            reply = self.agent.run(text)
        finally:
            os.chdir(start)
        with self.lock:
            self.conversation["messages"].append({"role": "echo", "text": reply, "time": _stamp()})
            self.conversation["last"] = time.time()
            store.save_current(self.conversation)
        return reply

    def build(self):
        """Make the agent for this conversation. Returns a problem to tell
        the user, or None."""
        name, model, size = self.conversation["driver"], self.conversation["model"], choice()[2]
        driver = inference.new_driver(name)
        try:
            installed = driver.models()
        except Exception:
            installed = None
        if installed is not None and model not in installed:
            return (f"The Telegram model {model} isn't installed on {inference.LABELS[name]}. "
                    "Pick another in /telegram on the Mac.")
        try:
            size = context.fitted(driver.facts(model), size)
        except Exception:
            pass
        settings = SimpleNamespace(**{k: v for k, v in vars(config).items() if k.isupper()})
        settings.MODEL, settings.NUM_CTX = model, size
        allowed = skills.usable()
        toolset = {name: dict(tool) for name, tool in tools.TOOLS.items()}
        for name in TASK_TOOLS:
            toolset[name].pop("on_demand", None)
        toolset["run_task"]["function"] = lambda id: tools.run_task(id, source="telegram")
        prompt = memory.build_system_prompt(
            config.SYSTEM_PROMPT, config.SELF_FILE, config.USER_FILE, seeds.render_pilot(tools.TOOLS),
            user_seeds=interaction.user_seeds_text())
        parts = [prompt, skills.prompt_section(allowed), telegram_note()]
        conversation = memory.ConversationMemory(system_prompt="\n\n".join(p for p in parts if p))
        for message in self.conversation["messages"]:  # a conversation carried over a restart
            conversation.add("user" if message["role"] == "user" else "assistant", message["text"])
        self.driver = driver
        self.agent = TelegramEngine(
            approve=self.ask_permission, config=settings, tools=toolset, memory=conversation,
            gate=gate.Gate(), on_full=_when_full, driver=driver, skills=allowed,
            checkpoints=False, quiet=True, verbose=False)
        return None

    def ask_permission(self, reason):
        owner = store.settings()["user_id"]
        nonce = secrets.token_hex(4)
        self.approval = {"nonce": nonce, "event": threading.Event(), "answer": False}
        sent = None
        try:
            sent = self.api.send(owner, f"⚠ Permission needed. {reason}",
                                 buttons=[("Allow", f"allow:{nonce}"), ("Deny", f"deny:{nonce}")])
            answered = self.approval["event"].wait(config.TELEGRAM_APPROVAL_MINUTES * 60)
            allowed = answered and self.approval["answer"]
            verdict = "Allowed." if allowed else ("Denied." if answered else "No answer, so it was denied.")
            try:
                self.api.edit(owner, sent["message_id"], f"⚠ Permission needed. {reason}\n\n{verdict}")
            except Exception:
                pass
            return allowed
        except Exception as exc:
            errorlog.log_error("telegram.permission", exc)
            return False
        finally:
            self.approval = None

    # -- endings and reminders -------------------------------------------------------

    def end(self, reason, now=None):
        """Log the conversation and unload its model. Returns whether there was one."""
        with self.lock:
            conversation, self.conversation, self.agent = self.conversation, None, None
            store.clear_current()
        store.log_conversation(conversation, reason, now=now)
        self.unload()
        return bool(conversation and conversation.get("messages"))

    def unload(self):
        driver, self.driver = self.driver, None
        if driver is not None:
            try:
                driver.release()
            except Exception as exc:
                errorlog.log_error("telegram.unload", exc)

    def expire(self, now=None):
        """End a conversation after the idle time without a message."""
        now = now or time.time()
        quiet = store.settings()["idle_minutes"] * 60
        conversation = self.conversation
        if (conversation and not self.busy and self.inbox.empty()
                and now - conversation.get("last", now) >= quiet):
            self.end(f"no message for {store.settings()['idle_minutes']} minutes", now=now)

    def push_reminders(self):
        """Urgent reminders go to the phone as they appear (they stay unseen
        for the terminal too). Results of tasks run from Telegram go only here."""
        owner = store.settings()["user_id"]
        pushed = store.state()["pushed"]
        for item in reminders.unseen():
            mine = item.get("from") == "telegram"
            if (item.get("urgency") == "urgent" or mine) and item["id"] not in pushed:
                self.api.send_long(owner, item["text"] if mine else f"⚠ {item['text']}")
                pushed.append(item["id"])
                store.save_state(pushed=pushed[-200:])
                if mine:
                    reminders.mark_seen([item["id"]])


def _stamp(now=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now or time.time()))


def mark_reminders_pushed():
    """At pairing: what's already waiting isn't pushed all at once."""
    store.save_state(pushed=[item["id"] for item in reminders.unseen()][-200:])


_running = None  # (thread, bridge) served by this process


def start():
    """Serve Telegram on a thread of this process (the runner) if it's set up
    and not already being served here."""
    global _running
    if _running is not None and _running[0].is_alive():
        return
    if not store.ready():
        return
    bridge = Bridge()
    thread = threading.Thread(target=bridge.serve, name="telegram", daemon=True)
    _running = (thread, bridge)
    thread.start()


def stop():
    """The runner is stopping: stop polling and unload Telegram's model. A
    conversation in progress stays in current.json and continues next time."""
    if _running is not None:
        thread, bridge = _running
        bridge.stop.set()
        thread.join(timeout=10)
