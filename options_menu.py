"""Options menu methods for Echo."""

import autonomy
import context
import inference
import tasks
import ui
from communication.telegram import store as telegram_store
from cli_style import GREY, RESET


class OptionsMenu:
    def options_menu(self):
        while True:
            clock = tasks.load_clock()
            kind = "autonomous" if clock["autonomous"] else "while active"
            state = f"on, {kind}, a cycle every {clock['minutes']} min" if clock["enabled"] else "off"
            saved = len(tasks.saved_tasks())
            engine_settings = inference.settings()
            guard = f"{engine_settings['guard_gb']:g} GB" if engine_settings["guard_gb"] else "off"
            telegram_state = ("on" if telegram_store.ready() else
                              "off" if telegram_store.token() and telegram_store.settings()["user_id"] else "not set up")
            choice = ui.menu("Options", [("tasks", "Tasks"), ("saved", f"Saved tasks ({saved})"),
                                         ("clock", f"Internal clock ({state})"),
                                         ("driver", f"Inference driver ({inference.LABELS[engine_settings['driver']]})"),
                                         ("guard", f"Memory guard ({guard})"),
                                         ("memory", "Memory layers"),
                                         ("telegram", f"Telegram ({telegram_state})"), ("back", "Back")])
            if choice == "tasks":
                if self.tasks_menu() == "exit":
                    return "exit"
            elif choice == "saved":
                self.saved_menu()
            elif choice == "driver":
                self.driver_menu()
            elif choice == "guard":
                self.guard_menu()
            elif choice == "clock":
                self.clock_menu()
            elif choice == "memory":
                self.memory_menu()
            elif choice == "telegram":
                self.telegram_menu()
            else:
                return None

    def driver_menu(self):
        current = inference.settings()["driver"]
        options = []
        for name in inference.DRIVERS:
            ok, note = {"ollama": inference.OllamaDriver, "llamacpp": inference.LlamaCppDriver,
                        "mlx": inference.MLXDriver}[name]().available()
            options.append((name, f"{inference.LABELS[name]}  ({note})" if ok else f"{inference.LABELS[name]}  "
                            f"(unavailable: {note})"))
        print(f"{GREY}One model is loaded at a time. Switching unloads the current one.{RESET}")
        choice = ui.menu("Inference driver", options, current=current)
        if choice is None or choice == current:
            return
        driver = inference.switch(choice)
        self.forget_models()
        try:
            models = self.tool_models(refresh=True)
        except Exception:
            models = []
        if not models:
            print(f"{driver.label} has no models that can make tool calls yet. Echo will use "
                  f"{inference.default_model()} until you pick one with /model.")
            self.agent.model = inference.default_model()
            return
        remembered = inference.default_model()
        pick = ui.menu(f"Which model on {driver.label}?", [(m, m) for m in models],
                       current=remembered if remembered in models else models[0])
        pick = pick or (remembered if remembered in models else models[0])
        by_driver = inference.settings()["models"]
        by_driver[choice] = pick
        inference.save_settings(models=by_driver)
        self.agent.model = pick
        print(f"Echo now runs on {driver.label} with {pick}. Tasks use this driver too; a task whose model "
              f"isn't here fails with 'Model not installed' until you pick one in Edit task spec.")

    def guard_menu(self):
        guard = inference.settings()["guard_gb"]
        total = context.gpu_budget() / context.GiB
        print(f"{GREY}The harness never loads more than this: a bigger model's context is lowered to fit, and a "
              f"model whose weights don't fit isn't loaded. Your GPU limit is {total:.0f} GB.{RESET}")
        presets = [(0, "Off (the GPU limit)")] + [(gb, f"{gb} GB") for gb in (8, 12, 16, 20, 24) if gb < total]
        choice = ui.menu("Memory guard", presets + [("custom", "Custom")],
                         current=guard if guard in dict(presets) else ("custom" if guard else 0))
        if choice is None:
            return
        if choice == "custom":
            while True:
                typed = ui.line("Limit in GB: ", f"{guard:g}" if guard else "")
                if typed is None or typed is ui.BACK:
                    return
                try:
                    choice = float(typed)
                except ValueError:
                    choice = 0
                if 2 <= choice <= total:
                    break
                print(f"Enter a number of GB from 2 to {total:.0f}.")
        inference.save_settings(guard_gb=choice or None)
        print(f"Memory guard {'set to ' + format(choice, 'g') + ' GB' if choice else 'off'}. "
              "It applies from the next model load.")

    def clock_menu(self):
        clock = tasks.load_clock()
        print(f"{GREY}Each cycle runs the due tasks in number order, one at a time. "
              f"While Echo is open, tasks wait for an empty prompt.{RESET}")
        enabled = ui.menu("Internal clock", [(True, "On"), (False, "Off")], current=clock["enabled"])
        if enabled is None:
            return
        minutes, autonomous = clock["minutes"], clock["autonomous"]
        if enabled:
            autonomous = ui.menu("Run tasks", [
                (False, "While active (only while Echo is open)"),
                (True, "Autonomous (keeps going after Echo closes, until the Mac restarts)"),
            ], current=autonomous)
            if autonomous is None:
                return
            minutes = ui.menu("Run a task cycle every", [(5, "5 minutes"), (15, "15 minutes"), (30, "30 minutes"),
                                                            (60, "1 hour"), (360, "6 hours"), (1440, "24 hours")],
                              current=minutes)
            if minutes is None:
                return
        tasks.save_clock(enabled, minutes, autonomous)
        autonomy.sync()
        if enabled and autonomous:
            print(f"{GREY}Autonomous is on. The Mac won't idle-sleep while it runs. "
                  f"After a restart, open Echo once to bring it back.{RESET}")
