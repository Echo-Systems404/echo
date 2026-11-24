"""Unit tests for the Telegram pipeline (communication/telegram/). Telegram
itself is faked; nothing goes over the network.
Run: python3 -B -m unittest test_telegram
"""

import json
import os
import threading
import time
import urllib.error
from unittest import mock

import autonomy
import communication
import config
import gate
import reminders
import tasks
import tools
import turns
from communication.telegram import api as telegram_api
from communication.telegram import bridge, store
from test_helpers import Sandbox

OWNER = 1111


class FakeApi:
    def __init__(self):
        self.token = "123:secret"
        self.sent, self.pressed, self.edits = [], [], []

    def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons))
        return {"message_id": len(self.sent)}

    def send_long(self, chat_id, text):
        for part in telegram_api.split(text, config.TELEGRAM_MAX_MESSAGE_CHARS):
            self.send(chat_id, part)

    def typing(self, chat_id):
        pass

    def answer_button(self, callback_id, text):
        self.pressed.append(text)

    def edit(self, chat_id, message_id, text):
        self.edits.append(text)


class FakeAgent:
    def __init__(self, reply="hi there"):
        self.reply, self.heard = reply, []

    def run(self, text):
        self.heard.append(text)
        return self.reply


class FakeDriver:
    released = 0

    def release(self):
        FakeDriver.released += 1


def message(text, sender=OWNER, date=None, chat="private"):
    return {"update_id": 1, "message": {"from": {"id": sender}, "chat": {"id": sender, "type": chat},
                                        "date": int(date or time.time()), "text": text}}


class Base(Sandbox):
    def setUp(self):
        super().setUp()
        extra = [mock.patch.object(config, "REMINDERS_DIR", self.tmp / "reminders"),
                 mock.patch.object(reminders, "notify", lambda text: None)]
        for p in extra:
            p.start()
        self.patchers += extra

    def pair(self):
        store.save_token("123:secret")
        store.save_settings(enabled=True, user_id=OWNER, user_name="Sam (@sam)")

    def bridge(self):
        b = bridge.Bridge(api=FakeApi())
        b.build = lambda: setattr(b, "agent", FakeAgent()) or setattr(b, "driver", FakeDriver())
        return b


class Api(Base):
    def test_long_replies_are_split_at_breaks(self):
        text = ("word " * 300 + "\n\n") * 5
        parts = telegram_api.split(text, 1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertEqual("".join(parts).replace(" ", "").replace("\n", ""), text.replace(" ", "").replace("\n", ""))
        self.assertEqual(telegram_api.split("short", 1000), ["short"])

    def test_errors_never_show_the_token_and_409_is_a_conflict(self):
        client = telegram_api.Api("123:secret")
        failure = urllib.error.URLError("can't reach https://api.telegram.org/bot123:secret/getMe")
        with mock.patch.object(telegram_api.urllib.request, "urlopen", side_effect=failure):
            with self.assertRaises(OSError) as caught:
                client.me()
        self.assertNotIn("secret", str(caught.exception))

        class Reply:
            def __init__(self, data):
                self.data = data

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self):
                return json.dumps(self.data).encode()
        conflict = {"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates"}
        with mock.patch.object(telegram_api.urllib.request, "urlopen", return_value=Reply(conflict)):
            with self.assertRaises(telegram_api.Conflict):
                client.updates(0, 1)
        with mock.patch.object(telegram_api.urllib.request, "urlopen",
                               return_value=Reply({"ok": True, "result": [{"update_id": 5}]})):
            self.assertEqual(client.updates(0, 1), [{"update_id": 5}])


class Store(Base):
    def test_settings_token_and_ready(self):
        self.assertEqual(store.settings()["idle_minutes"], 10)
        self.assertFalse(store.ready())
        store.save_token("123:secret")
        self.assertEqual(oct(os.stat(store.data_dir() / "token").st_mode & 0o777), "0o600")
        self.assertFalse(store.ready())  # not paired or on yet
        store.save_settings(user_id=OWNER, enabled=True)
        self.assertTrue(store.ready())
        self.assertTrue(autonomy.wanted())  # the runner serves Telegram even with the clock off
        store.save_settings(enabled=False)
        self.assertFalse(autonomy.wanted())

    def test_recall_lists_searches_and_reads(self):
        self.assertIn("No Telegram conversations yet", store.recall_list())
        for number, words in enumerate(["the guitar amp is broken", "dinner plans for friday"]):
            store.log_conversation({"id": f"t{number}", "started": "2026-11-24 07:00:00", "model": "m",
                                    "messages": [{"role": "user", "text": words, "time": "2026-11-24 07:00:00"},
                                                 {"role": "echo", "text": "noted", "time": "2026-11-24 07:01:00"}]},
                                   f"ended by {config.USER_NAME}")
        self.assertIn("t1", store.recall_list())
        found = store.recall_list("guitar")
        self.assertIn("t0", found)
        self.assertNotIn("t1", found)
        self.assertIn("dinner plans", store.recall_read("last"))
        self.assertIn(f"{config.USER_NAME}: the guitar amp", store.recall_read("t0"))
        self.assertIn("Error", store.recall_read("t9"))
        self.assertIn("telegram_conversations", tools.TOOLS)
        self.assertTrue(tools.TOOLS["telegram_read"]["on_demand"])


class Privacy(Base):
    def test_echo_cant_read_list_or_search_the_private_data(self):
        self.pair()
        token = store.data_dir() / "token"
        self.assertTrue(communication.private(token))
        self.assertFalse(communication.private(config.COMMUNICATION_DIR / "telegram" / "bridge.py"))
        guard = gate.Gate()
        for name in ("read_file", "list_directory", "search_files", "write_file"):
            self.assertIsNotNone(guard.check(name, {"path": str(token if name != "list_directory"
                                                                 else store.data_dir())}), name)
        self.assertNotIn("secret", tools.search_files("secret", str(config.COMMUNICATION_DIR)))


class Conversation(Base):
    def test_only_the_paired_account_is_answered(self):
        self.pair()
        b = self.bridge()
        b.handle(message("hello", sender=9999))
        b.handle(message("hello", chat="group"))
        self.assertEqual(b.api.sent, [])
        self.assertEqual(store.state()["ignored"], 2)
        b.handle(message("old", date=time.time() - 3600))  # sent while Echo was off
        self.assertTrue(b.inbox.empty())
        b.handle({"update_id": 2, "message": {"from": {"id": OWNER}, "chat": {"type": "private"},
                                              "date": int(time.time()), "photo": []}})
        self.assertEqual(b.api.sent[-1][1], bridge.TEXT_ONLY)
        b.handle(message("/start"))
        self.assertEqual(b.api.sent[-1][1], bridge.HELLO)
        b.handle(message("what's up"))
        b.handle(message("/bye"))
        self.assertEqual([b.inbox.get(), b.inbox.get()], [("text", "what's up"), ("end", None)])
        b.handle(message("/tasks"))  # straight from the harness, no model
        self.assertIn("Internal clock", b.api.sent[-1][1])

    def test_reply_then_idle_end_logs_and_unloads(self):
        self.pair()
        b = self.bridge()
        start = time.time()
        self.assertEqual(b.answer("hello", OWNER), "hi there")
        self.assertEqual(len(store.current()["messages"]), 2)
        again = bridge.Bridge(api=FakeApi())  # a runner restart picks the conversation up
        self.assertEqual(again.conversation["id"], b.conversation["id"])
        before = FakeDriver.released
        b.expire(now=start + 5 * 60)
        self.assertIsNotNone(b.conversation)  # not quiet long enough yet
        b.expire(now=start + 11 * 60)
        self.assertIsNone(b.conversation)
        self.assertIsNone(store.current())
        logged = store.conversations()
        self.assertEqual(len(logged), 1)
        self.assertIn("no message for 10 minutes", logged[0]["reason"])
        self.assertEqual([m["role"] for m in logged[0]["messages"]], ["user", "echo"])
        self.assertEqual(FakeDriver.released, before + 1)

    def test_end_command_saves(self):
        self.pair()
        b = self.bridge()
        b.answer("hello", OWNER)
        b.inbox.put(("end", None))
        worker = threading.Thread(target=b.work, daemon=True)
        worker.start()
        deadline = time.time() + 3
        while not store.conversations() and time.time() < deadline:
            time.sleep(0.05)
        b.stop.set()
        worker.join(2)
        self.assertEqual(store.conversations()[0]["reason"], f"ended by {config.USER_NAME}")
        self.assertIn("conversation is saved", b.api.sent[-1][1])

    def test_waits_for_the_model_and_says_so(self):
        self.pair()
        b = self.bridge()
        held = turns.hold()
        result = {}
        worker = threading.Thread(target=lambda: result.update(reply=b.answer("hello", OWNER)), daemon=True)
        worker.start()
        time.sleep(0.5)
        self.assertNotIn("reply", result)
        self.assertIn("Your message is next", b.api.sent[-1][1])
        held.close()
        worker.join(3)
        self.assertEqual(result["reply"], "hi there")

    def test_tasks_wait_while_a_message_is_answered(self):
        held = turns.hold()
        try:
            result, text = tasks.run({"id": "001"}, "m", 1000)
        finally:
            held.close()
        self.assertEqual(result, "busy")
        self.assertIn("answering a message", text)


class Permission(Base):
    def test_buttons_allow_deny_and_time_out(self):
        self.pair()
        b = self.bridge()
        for button, expected in (("allow", True), ("deny", False)):
            result = {}
            worker = threading.Thread(target=lambda: result.update(ok=b.ask_permission("Run ls?")), daemon=True)
            worker.start()
            deadline = time.time() + 2
            while b.approval is None and time.time() < deadline:
                time.sleep(0.02)
            nonce = b.approval["nonce"]
            b.handle({"update_id": 3, "callback_query": {"id": "x", "from": {"id": 9999},
                                                         "data": f"allow:{nonce}"}})  # a stranger's press
            self.assertTrue(worker.is_alive())
            b.handle({"update_id": 4, "callback_query": {"id": "y", "from": {"id": OWNER},
                                                         "data": f"{button}:{nonce}"}})
            worker.join(2)
            self.assertEqual(result["ok"], expected)
        self.assertEqual(b.api.sent[-1][2][0][0], "Allow")
        with mock.patch.object(config, "TELEGRAM_APPROVAL_MINUTES", 0.001):
            self.assertFalse(b.ask_permission("Run ls?"))
        self.assertIn("No answer", b.api.edits[-1])
        b.handle({"update_id": 5, "callback_query": {"id": "z", "from": {"id": OWNER}, "data": "allow:old"}})
        self.assertIn("already ended", b.api.pressed[-1])

    def test_the_engine_asks_through_telegram(self):
        asked = []
        agent = bridge.TelegramEngine(approve=lambda reason: asked.append(reason) or False, config=config,
                                      tools={}, memory=None, quiet=True, verbose=False)
        self.assertFalse(agent._permission_granted("Delete x?"))
        self.assertEqual(asked, ["Delete x?"])


class RunTasks(Base):
    def make(self, name="site"):
        tasks.create(name, "Do it.", [str(self.tmp / "work")], {"read"}, "forever", None)
        task = tasks.all_tasks()[-1]
        tasks.approve(task)
        return task

    def test_list_and_run_now_even_with_the_clock_off(self):
        task = self.make()
        self.assertIn("001: site", tools.list_tasks())
        self.assertIn("tasks run only when asked", tools.list_tasks())
        self.assertIn("Run task 001 (site) now", tools.run_task_needs_permission("001"))
        self.assertTrue(tools.TOOLS["run_task"]["needs_permission"])
        self.assertIn("Queued", tools.run_task("1", source="telegram"))
        self.assertIn("Waiting to run now: task 001", tools.list_tasks())
        self.assertEqual(tasks.next_due()["id"], "001")  # the clock is off, but it was asked for
        with mock.patch.object(tasks, "_run", lambda *a: ("ok", "Built it.")):
            tasks.run(task, "m", 1000)
        self.assertIsNone(tasks.requested())
        b = bridge.Bridge(api=FakeApi())
        self.pair()
        b.push_reminders()
        self.assertIn("Task 001 (site) finished: ok. Built it.", b.api.sent[-1][1])
        self.assertFalse(any("finished" in r["text"] for r in reminders.unseen()))  # only on the phone

    def test_run_refusals(self):
        task = self.make()
        self.assertIn("no task", tools.run_task("009"))
        tasks.set_paused(task, True)
        self.assertIn("paused", tools.run_task("001"))
        tasks.set_paused(task, False)
        self.make("other")
        tools.run_task("001")
        self.assertIn("already waiting", tools.run_task("002"))

    def test_a_busy_model_keeps_the_request(self):
        task = self.make()
        tools.run_task("001")
        held = turns.hold()
        try:
            self.assertEqual(tasks.run(task, "m", 1000)[0], "busy")
        finally:
            held.close()
        self.assertEqual(tasks.requested()["id"], "001")


class Reminders(Base):
    def test_urgent_reminders_are_pushed_once(self):
        self.pair()
        reminders.add("old urgent", "sentinel", True)
        bridge.mark_reminders_pushed()  # what waited before pairing isn't dumped on the phone
        b = self.bridge()
        reminders.add("Security incident: something", "sentinel", True)
        reminders.add("just a note", "task", False)
        b.push_reminders()
        b.push_reminders()
        self.assertEqual([text for _, text, _ in b.api.sent], ["⚠ Security incident: something"])


if __name__ == "__main__":
    import unittest
    unittest.main()
