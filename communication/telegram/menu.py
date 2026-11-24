"""/telegram (also in /options): set up the bot, pair your account, and pick
the model, driver, context window, and how long a quiet conversation lasts."""

import time

from prompt_toolkit import prompt

import autonomy
import config
import context
import inference
import ui
from cli_style import GREY, RESET, YELLOW
from communication.telegram import bridge, store
from communication.telegram.api import Api, Conflict, TelegramError

PAIR_SECONDS = 120


class TelegramMenu:
    def telegram_menu(self):
        while True:
            current, state = store.settings(), store.state()
            name, model, size = bridge.choice()
            same = current["driver"] is None
            has_token, paired = bool(store.token()), bool(current["user_id"])
            power = "on" if store.ready() else ("off" if has_token and paired else "not set up")
            print(f"{GREY}Telegram is {power}. Conversations are saved to communication/telegram/data/"
                  f"conversations.jsonl when they end.{RESET}")
            if state["status"] and store.ready():
                print(f"{YELLOW}{state['status']}{RESET}")
            options = []
            if has_token and paired:
                options.append(("power", "Turn off" if current["enabled"] else "Turn on"))
            options += [
                ("token", f"Bot token ({'@' + current['bot_name'] if has_token else 'not set'})"),
                ("account", f"Your Telegram account ({current['user_name'] or 'not paired'})"),
                ("driver", f"Inference driver ({'same as Echo: ' if same else ''}{inference.LABELS[name]})"),
                ("model", f"Model ({'same as Echo: ' if not current['model'] else ''}{model})"),
                ("context", f"Context window ({'same as Echo: ' if not current['num_ctx'] else ''}"
                            f"{context.format_size(size)})"),
                ("idle", f"Conversation ends after {current['idle_minutes']} min without a message"),
                ("back", "Back"),
            ]
            pick = ui.menu("Telegram", options)
            if pick == "power":
                store.save_settings(enabled=not current["enabled"])
                autonomy.sync()
                print("Telegram is on. Message your bot from your phone." if store.ready() else
                      "Telegram is off. A conversation in progress is kept for next time.")
            elif pick == "token":
                self.telegram_token()
            elif pick == "account":
                self.telegram_pair()
            elif pick == "driver":
                self.telegram_driver()
            elif pick == "model":
                self.telegram_model()
            elif pick == "context":
                self.telegram_context()
            elif pick == "idle":
                minutes = ui.menu("End a conversation after this long without a message",
                                  [(5, "5 minutes"), (10, "10 minutes"), (15, "15 minutes"), (30, "30 minutes"),
                                   (60, "1 hour")], current=current["idle_minutes"])
                if minutes:
                    store.save_settings(idle_minutes=minutes)
            else:
                return None

    def telegram_token(self):
        print(f"{GREY}Make a bot for {config.AGENT_NAME}: in Telegram, message @BotFather, send /newbot, and "
              "follow the steps. It gives you a token like 123456:ABC-... Use a new bot, not one another "
              f"program (like another agent) reads, since only one program can read a bot's messages.{RESET}")
        try:
            token = prompt("Paste the bot token (hidden): ", is_password=True).strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not token:
            return
        try:
            me = Api(token).me()
        except (TelegramError, OSError) as exc:
            print(f"That token didn't work: {exc}")
            return
        store.save_token(token)
        store.save_settings(bot_name=me.get("username", ""))
        store.save_state(offset=0, status="")
        print(f"Saved. Your bot is @{me.get('username')}. Only you can read the token file.")
        if not store.settings()["user_id"]:
            self.telegram_pair()
        else:
            autonomy.sync()

    def telegram_pair(self):
        token = store.token()
        if not token:
            print("Set the bot token first.")
            return
        was_on = store.settings()["enabled"]
        store.save_settings(enabled=False)  # the runner lets go of the bot while you pair
        autonomy.sync()
        api, bot = Api(token), store.settings()["bot_name"]
        print(f"Send any message to @{bot} from your phone now. Waiting up to {PAIR_SECONDS // 60} minutes "
              "(ctrl-c stops).")
        deadline, offset = time.time() + PAIR_SECONDS, 0
        try:
            while time.time() < deadline:
                try:
                    updates = api.updates(offset, 10)
                except Conflict:
                    time.sleep(3)  # the runner's last poll is still open
                    continue
                except (TelegramError, OSError) as exc:
                    print(f"Can't reach Telegram: {exc}")
                    time.sleep(3)
                    continue
                for update in updates:
                    offset = update["update_id"] + 1
                    message = update.get("message") or {}
                    sender = message.get("from") or {}
                    if message.get("chat", {}).get("type") != "private" or not sender.get("id"):
                        continue
                    who = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))
                    handle = f"@{sender['username']}" if sender.get("username") else "no username"
                    print(f"Message from {who} ({handle}, id {sender['id']}): "
                          f"{(message.get('text') or '(not text)')[:60]!r}")
                    if ui.menu("Is this you?", [(True, "Yes, pair this account"), (False, "No, keep waiting")],
                               echo=False):
                        store.save_settings(user_id=sender["id"], user_name=f"{who} ({handle})", enabled=True)
                        store.save_state(offset=offset, status="")
                        bridge.mark_reminders_pushed()
                        autonomy.sync()
                        try:
                            api.send(sender["id"], bridge.HELLO)
                        except (TelegramError, OSError):
                            pass
                        print(f"Paired. Telegram is on: only this account gets answers. {GREY}It runs in the "
                              f"background runner, so it keeps working after you close {config.AGENT_NAME}, "
                              f"until the Mac restarts.{RESET}")
                        return
            print("Nothing arrived. Try again from /telegram.")
        except KeyboardInterrupt:
            print("Stopped.")
        if was_on:
            store.save_settings(enabled=True)
            autonomy.sync()

    def telegram_driver(self):
        current = store.settings()["driver"]
        options = [("same", f"Same as {config.AGENT_NAME} (now {inference.LABELS[inference.settings()['driver']]})")]
        for name in inference.DRIVERS:
            ok, note = inference.new_driver(name).available()
            options.append((name, f"{inference.LABELS[name]}  ({note})" if ok else
                            f"{inference.LABELS[name]}  (unavailable: {note})"))
        print(f"{GREY}One model is loaded at a time. If Telegram uses a different model than the terminal, or MLX "
              f"or llama.cpp (one server each), answering on one side unloads the other's model; it reloads "
              f"on its next message. Same as {config.AGENT_NAME} on Ollama shares the model.{RESET}")
        pick = ui.menu("Telegram's inference driver", options, current=current or "same")
        if pick is None:
            return
        pick = None if pick == "same" else pick
        if pick != current:
            store.save_settings(driver=pick, model=None)  # a model belongs to a driver
            print("Saved. It applies from the next conversation.")

    def telegram_model(self):
        name = bridge.choice()[0]
        driver = inference.new_driver(name)
        try:
            models = [m for m in driver.models() if driver.supports_tools(m)]
        except Exception as exc:
            print(f"Can't list {inference.LABELS[name]}'s models: {exc}")
            return
        if not models:
            print(f"{inference.LABELS[name]} has no models that can use tools.")
            return
        echo_model = inference.settings()["models"].get(name) or config.MODEL
        current = store.settings()["model"]
        pick = ui.menu(f"Telegram's model ({inference.LABELS[name]})",
                       [("", f"Same as {config.AGENT_NAME} (now {echo_model})")] + [(m, m) for m in models],
                       current=current or "")
        if pick is None:
            return
        store.save_settings(model=pick or None)
        print("Saved. It applies from the next conversation.")

    def telegram_context(self):
        name, model, _ = bridge.choice()
        try:
            facts = inference.new_driver(name).facts(model)
        except Exception:
            facts = {"max_ctx": None, "weights": None, "kv_per_token": None}
        budget = context.budget()
        options = [(0, f"Same as {config.AGENT_NAME} ({context.format_size(config.NUM_CTX)})")]
        for size in context.options(facts):
            if size > config.CONTEXT_LOCK:
                continue
            total = context.estimate(facts, size)
            note = context.verdict(total, budget) + (f" · ~{total / context.GiB:.1f} GB" if total else "")
            options.append((size, f"{context.format_size(size)}  ({note})"))
        pick = ui.menu(f"Telegram's context window ({model})", options, current=store.settings()["num_ctx"] or 0)
        if pick is None:
            return
        store.save_settings(num_ctx=pick or None)
        print("Saved. It applies from the next conversation.")
