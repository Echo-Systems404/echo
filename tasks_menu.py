"""Tasks menu methods for Echo."""

from pathlib import Path
import sentinel
import skills
import tasks
import ui
from cli_style import BOLD, GREY, RESET


# Task setup choices, shared by the new-task wizard and Edit task spec.
POWER_OPTIONS = [
    ("append", "Add to files (append-only: can't change what's there)"),
    ("write", "Create and edit files"),
    ("shell", "Run commands (full trust: can reach outside its folders)"),
    ("delete", "Delete files (needs commands)"),
    ("git", "Git push and reset (needs commands)"),
    ("seeds", "Edit seeds (seed consolidation; memory vault seeds stay frozen)"),
    ("security", "Security scan (checks this Mac; cuts remote access you haven't allowed)"),
]
POWER_NAMES = {"append": "Add to files", "write": "Edit files", "shell": "Run commands", "delete": "Delete files",
               "git": "Git push and reset", "seeds": "Edit seeds", "security": "Security scan"}
REPEAT_OPTIONS = [(1, "Once"), ("forever", "Indefinitely"), ("number", "A set number of times")]
EVERY_OPTIONS = [("cycle", "Every clock cycle"), ("1h", "At most every hour"), ("6h", "At most every 6 hours"),
                 ("24h", "At most once a day"), ("1w", "At most once a week"), ("custom", "Custom")]
EVERY_TITLE = "How often? (it runs in a clock cycle, at most this often)"


def powers_text(powers):
    return ", ".join(POWER_NAMES[p] for p in tasks.POWER_ORDER if p in powers and p in POWER_NAMES) or "Read only"


def timing_text(task):
    runs = "once" if task["repeat"] == 1 else "forever" if task["repeat"] == "forever" else f"{task['repeat']} times"
    if task["repeat"] == 1:
        return runs
    return f"{runs}, " + ("every cycle" if task["every"] is None else f"at most every {task['every_text']}")


class TasksMenu:
    def ask_folder(self, default=""):
        """A folder path, as typed or dragged into the terminal."""
        harness = Path(__file__).resolve().parent
        while True:
            typed = ui.line("Folder path: ", default)
            if typed is None or typed is ui.BACK:
                return typed
            default = typed
            path = Path(typed.strip("'\"").replace("\\ ", " ")).expanduser().resolve()
            if not path.is_dir():
                print(f"Can't find that folder: {path}")
            elif path == harness or harness in path.parents:
                print("Tasks can't work inside Echo's own folder. Pick another one.")
            else:
                return str(path)

    def tasks_menu(self):
        while True:
            items = tasks.all_tasks()
            pilots = [t for t in items if t.get("pilot")]
            options = [(t["id"], f"{t['id']}  {t['name']}  ({tasks.describe(t)})") for t in items if not t.get("pilot")]
            if pilots:  # frozen: only changed in the codebase
                options = ([(t["id"], f"◆ {t['name']}  ({tasks.describe(t)})  · pilot") for t in pilots]
                           + options)
            choice = ui.menu("Tasks", options + [("new", "+ New task"), ("back", "Back")])
            if choice == "new":
                self.new_task()
            elif choice in (None, "back"):
                return None
            else:
                task = next(t for t in items if t["id"] == choice)
                if self.task_actions(task) == "exit":
                    return "exit"

    def task_actions(self, task):
        if task.get("pilot"):
            return self.pilot_actions(task)
        state = tasks.status(task)
        options = []
        if state == "needs approval":
            print(f"{GREY}This file changed or wasn't created in /tasks. Read it before approving: {task['path']}{RESET}")
            options.append(("approve", "Approve this file"))
        if state == "active":
            options += [("run", "Run now"), ("pause", "Pause")]
        if state == "paused":
            options.append(("resume", "Resume"))
        if not task["error"]:
            options.append(("edit", "Edit task spec"))
        options.append(("deactivate", "Deactivate (move to saved tasks)"))
        choice = ui.menu(f"Task {task['id']}: {task['name']}", options + [("delete", "Delete"), ("back", "Back")])
        if choice == "approve":
            tasks.approve(task)
        elif choice == "run":
            return self.run_scheduled(task)
        elif choice == "resume":
            if self.fix_task_model(task):  # a missing model is picked first
                tasks.set_paused(task, False)
        elif choice == "pause":
            tasks.set_paused(task, True)
        elif choice == "edit":
            self.edit_spec(task)
        elif choice == "deactivate":
            problem = tasks.deactivate(task)
            print(problem or f"Moved {task['name']} to saved tasks. The tasks after it moved up.")
        elif choice == "delete":
            if ui.menu(f"Delete {task['path'].name}? (it's archived in done.jsonl)", [(False, "Keep it"), (True, "Delete")]):
                problem = tasks.delete(task)
                if problem:
                    print(problem)
        return None

    def pilot_actions(self, task):
        """Pilot tasks: run, pause, view, approve. Never edited or deleted here."""
        state = tasks.status(task)
        options = []
        if state == "needs approval":
            print(f"{GREY}New or changed in the codebase. Read it before approving: {task['path']}{RESET}")
            options.append(("approve", "Approve this file"))
        if state == "active":
            options += [("run", "Run now"), ("pause", "Pause")]
        if state == "paused":
            options.append(("resume", "Resume"))
        options.append(("view", "View spec"))
        incident = sentinel.incident() if "security" in task.get("powers", ()) else None
        if incident and incident.get("escalated"):
            options.append(("ack", "Close the security incident (I've dealt with it)"))
        choice = ui.menu(f"Pilot task: {task['name']}  (change it in pilot_tasks/)",
                         options + [("back", "Back")], echo=False)
        if choice == "approve":
            tasks.approve(task)
        elif choice == "run":
            return self.run_scheduled(task)
        elif choice == "resume":
            if self.fix_task_model(task):
                tasks.set_paused(task, False)
        elif choice == "pause":
            tasks.set_paused(task, True)
        elif choice == "view":
            print(f"\n{task['path'].read_text(encoding='utf-8')}")
        elif choice == "ack":
            sentinel.acknowledge()
            print("Incident closed.")
        return None

    def saved_menu(self):
        while True:
            items = tasks.saved_tasks()
            if not items:
                print(f"{GREY}No saved tasks. Deactivate a task to keep it here.{RESET}")
                return
            options = []
            for t in items:
                mark = "" if tasks.saved_status(t) == "saved" else f"  [{tasks.saved_status(t)}]"
                options.append((t["id"], f"{t['name']}  - {tasks.brief(t)}{mark}"))
            choice = ui.menu("Saved tasks", options + [("back", "Back")], echo=False)
            if choice in (None, "back"):
                return
            self.saved_actions(next(t for t in items if t["id"] == choice))

    def saved_actions(self, task):
        if tasks.saved_status(task) == "needs approval":
            print(f"{GREY}This file changed outside Echo. Read it before activating: {task['path']}{RESET}")
        options = [("activate", "Activate (back in line, at the end)")]
        if not task["error"]:
            options.append(("edit", "Edit task spec"))
        choice = ui.menu(f"Saved task: {task['name']}", options + [("delete", "Delete"), ("back", "Back")])
        if choice == "activate":
            problem = tasks.activate(task)
            print(problem or f"{task['name']} is back in line, at the end.")
        elif choice == "edit":
            self.edit_spec(task)
        elif choice == "delete":
            if ui.menu(f"Delete {task['path'].name}? (it's archived in done.jsonl)", [(False, "Keep it"), (True, "Delete")]):
                tasks.delete_saved(task)

    def task_places(self):
        here = Path.cwd().resolve()
        harness = Path(__file__).resolve().parent
        places = [("~/Desktop", "Desktop"), ("~/Documents", "Documents"), ("~/Downloads", "Downloads")]
        if here != harness and harness not in here.parents:
            places.append((str(here), f"This folder ({here.name})"))
        return places

    def edit_spec(self, task):
        """Edit a task's settings and text on its own screen. Each change is
        saved right away and stays approved."""
        changed = False
        print("\033[?1049h", end="", flush=True)
        try:
            while True:
                label = f"saved task {task['name']}" if task.get("saved") else f"task {task['id']}: {task['name']}"
                print(f"\033[H\033[2J{BOLD}Edit {label}{RESET}  {GREY}esc: done{RESET}\n")
                choice = ui.menu("What do you want to change?", [
                    ("name", f"Name ({task['name']})"),
                    ("model", f"Model ({task['model'] or 'whatever Echo is using'})"),
                    ("body", "Instructions (the task's text)"),
                    ("timing", f"Timing ({timing_text(task)})"),
                    ("areas", f"Where ({', '.join(task['areas'])})"),
                    ("powers", f"What it can do ({powers_text(task['powers'])})"),
                    ("skills", f"Skills ({', '.join(task.get('skills') or []) or 'none'})"),
                    ("done", "Done"),
                ], echo=False)
                if choice in (None, "done"):
                    return
                changes = self.ask_change(choice, task)
                if not changes:
                    continue
                result = tasks.update(task, **changes)
                if isinstance(result, str):
                    print(result)
                    ui.menu("", [(True, "OK")], echo=False)
                    return
                task, changed = result, True
        finally:
            print("\033[?1049l", end="", flush=True)
            if changed:
                print(f"{GREY}Saved {task['path'].name}. It stays approved.{RESET}")

    def ask_change(self, what, task):
        """One setting's new value as changes for tasks.update, or None."""
        def picked(value):
            return value is not None and value is not ui.BACK

        if what == "name":
            value = ui.line("Task name: ", task["name"])
            return {"name": value} if picked(value) else None
        if what == "model":
            value = ui.menu("Which model?", [("", f"Whatever Echo is using (now {self.agent.model})")]
                            + [(m, m) for m in self.tool_models(refresh=True)], current=task["model"] or "", echo=False)
            return {"model": value or None} if picked(value) else None
        if what == "body":
            value = ui.multiline("Task description:", task["body"])
            return {"body": value} if picked(value) and value else None
        if what == "timing":
            current = task["repeat"] if task["repeat"] in (1, "forever") else "number"
            repeat = ui.menu("How many times?", REPEAT_OPTIONS, current=current, echo=False)
            if not picked(repeat):
                return None
            if repeat == "number":
                done = tasks.runs(task)
                while True:
                    count = ui.line(f"Number of times (it has run {done}): ",
                                    str(task["repeat"]) if isinstance(task["repeat"], int) else "")
                    if not picked(count):
                        return None
                    if count.isdigit() and int(count) > max(done, 0):
                        repeat = int(count)
                        break
                    print(f"Enter a whole number above {done}.")
            if repeat == 1:
                return {"repeat": 1, "every": None}
            current = "cycle" if task["every"] is None else next(
                (k for k, _ in EVERY_OPTIONS if k == task["every_text"]), "custom")
            every = ui.menu(EVERY_TITLE, EVERY_OPTIONS, current=current, echo=False)
            if not picked(every):
                return None
            if every == "custom":
                while True:
                    every = ui.line("Every (like 30m, 2h, 3d): ", task["every_text"] if current == "custom" else "")
                    if not picked(every):
                        return None
                    if tasks.parse_every(every):
                        break
                    print("Use a time like 30m, 2h, or 3d (5 minutes minimum).")
            return {"repeat": repeat, "every": None if every == "cycle" else every}
        if what == "areas":
            places = self.task_places()
            known = {value for value, _ in places}
            options = places + [(a, a) for a in task["areas"] if a not in known] + [("other", "Another folder...")]
            chosen = ui.menu("Where can it work?", options, multi=True, checked=task["areas"], echo=False)
            if not picked(chosen):
                return None
            areas = [a for a in chosen if a != "other"]
            if "other" in chosen:
                folder = self.ask_folder()
                if picked(folder):
                    areas.append(folder)
            return {"areas": areas} if areas else None
        if what == "powers":
            chosen = ui.menu("What can it do? (it can always read its folders)", POWER_OPTIONS, multi=True,
                             checked=[p for p in task["powers"] if p != "read"], echo=False)
            if not picked(chosen):
                return None
            powers = set(chosen) | {"read"}
            if powers & {"delete", "git"}:
                powers.add("shell")
            return {"powers": powers}
        if what == "skills":
            options = self.skill_options(task.get("skills") or [])
            if not options:
                print(f"{GREY}No skills are on. Turn one on in /skills.{RESET}")
                ui.menu("", [(True, "OK")], echo=False)
                return None
            chosen = ui.menu("Which skills may it load?", options, multi=True,
                             checked=task.get("skills") or [], echo=False)
            return {"skills": sorted(chosen)} if picked(chosen) else None
        return None

    @staticmethod
    def skill_options(current=()):
        on = skills.usable()
        options = [(name, f"{name}  - {skill.description[:70]}") for name, skill in on.items()]
        return options + [(name, f"{name}  (not on)") for name in current if name not in on]

    def new_task(self):
        """Walk through the setup on its own screen, so going back redraws it
        cleanly and the chat is untouched afterward. Delete on an empty step or
        Shift+Tab returns to the previous step with its answer kept; Esc cancels."""
        places = self.task_places()
        answers = {}

        def text(label, key, valid=bool, problem="", can_back=True):
            while True:
                value = ui.line(label, answers.get(key, ""), can_back=can_back)
                if value is None or value is ui.BACK or valid(value):
                    return value
                print(problem)

        def choose(title, key, options):
            return ui.menu(title, options, back=True, current=answers.get(key), echo=False)

        def choose_many(title, key, options, **extra):
            return ui.menu(title, options, multi=True, back=True, checked=answers.get(key, ()), echo=False, **extra)

        def summary(done):
            """The answers so far, shown above the current step."""
            rows = []
            if "name" in done:
                rows.append(("Name", answers["name"]))
            if "description" in done:
                lines = answers["description"].splitlines()
                rows.append(("Description", lines[0][:60] + (f" (+{len(lines) - 1} lines)" if len(lines) > 1 else "")))
            if "areas" in done:
                where = [dict(places)[a] for a in answers["areas"] if a != "other"]
                if "folder" in done:
                    where.append(answers["folder"])
                rows.append(("Where", ", ".join(where) or "Another folder"))
            if "powers" in done:
                rows.append(("Can", powers_text(answers["powers"])))
            if "model" in done:
                rows.append(("Model", answers["model"] or "Whatever Echo is using"))
            if "skills" in done:
                rows.append(("Skills", ", ".join(answers["skills"]) or "none"))
            if "repeat" in done and answers["repeat"] != "number":
                rows.append(("Runs", "Once" if answers["repeat"] == 1 else "Indefinitely"))
            if "count" in done:
                rows.append(("Runs", f"{answers['count']} times"))
            if "custom" in done:
                rows.append(("Every", answers["custom"]))
            elif "every" in done and answers["every"] != "custom":
                rows.append(("Every", "clock cycle" if answers["every"] == "cycle" else answers["every"]))
            return rows

        steps = [
            ("name", lambda: text("Task name: ", "name", can_back=False), lambda: True),
            ("description", lambda: ui.multiline("Task description:", answers.get("description", "")), lambda: True),
            ("areas", lambda: choose_many("Where can it work?", "areas", places + [("other", "Another folder...")],
                                          enter_picks=True), lambda: True),
            ("folder", lambda: self.ask_folder(answers.get("folder", "")), lambda: "other" in answers["areas"]),
            ("powers", lambda: choose_many("What can it do? (it can always read its folders)", "powers",
                                           POWER_OPTIONS), lambda: True),
            ("model", lambda: choose("Which model?", "model", [("", f"Whatever Echo is using (now {self.agent.model})")]
                                     + [(m, m) for m in self.tool_models(refresh=True)]), lambda: True),
            ("skills", lambda: choose_many("Which skills may it load? (none is fine)", "skills",
                                           self.skill_options()), lambda: bool(skills.usable())),
            ("repeat", lambda: choose("How many times?", "repeat", REPEAT_OPTIONS), lambda: True),
            ("count", lambda: text("Number of times: ", "count", lambda v: v.isdigit() and int(v) > 0,
                                   "Enter a whole number, like 5."), lambda: answers["repeat"] == "number"),
            ("every", lambda: choose(EVERY_TITLE, "every", EVERY_OPTIONS), lambda: answers["repeat"] != 1),
            ("custom", lambda: text("Every (like 30m, 2h, 3d): ", "custom", tasks.parse_every,
                                    "Use a time like 30m, 2h, or 3d (5 minutes minimum)."),
             lambda: answers["repeat"] != 1 and answers["every"] == "custom"),
            ("save", lambda: choose("Save this task?", "save", [(True, "Save"), (False, "Cancel")]), lambda: True),
        ]
        index = 0
        cancelled = False
        print("\033[?1049h", end="", flush=True)  # a separate screen for the setup
        try:
            while index < len(steps):
                key, ask, applies = steps[index]
                if not applies():
                    index += 1
                    continue
                print(f"\033[H\033[2J{BOLD}New task{RESET}  {GREY}shift+tab: back · esc: cancel{RESET}\n")
                rows = summary({steps[i][0] for i in range(index) if steps[i][2]()})
                for label, value in rows:
                    print(f"  {GREY}{label:<12}{RESET}{value}")
                if rows:
                    print()
                value = ask()
                if value is None:
                    cancelled = True
                    break
                if value is ui.BACK:
                    index -= 1
                    while index > 0 and not steps[index][2]():
                        index -= 1
                    continue
                answers[key] = value
                index += 1
        finally:
            print("\033[?1049l", end="", flush=True)
        if cancelled:
            print("Cancelled.")
            return
        if not answers["save"]:
            print("Not saved.")
            return

        areas = [a for a in answers["areas"] if a != "other"]
        if "other" in answers["areas"]:
            areas.append(answers["folder"])
        powers = set(answers["powers"]) | {"read"}
        if powers & {"delete", "git"}:
            powers.add("shell")
        repeat = int(answers["count"]) if answers["repeat"] == "number" else answers["repeat"]
        every = (None if repeat == 1 or answers["every"] == "cycle"
                 else answers["custom"] if answers["every"] == "custom" else answers["every"])
        task = tasks.create(answers["name"], answers["description"], areas, powers, repeat, every,
                            model=answers["model"] or None, skills=answers.get("skills") or ())
        print(f"Saved task {task['id']}: {task['path'].name}")
        if not tasks.load_clock()["enabled"]:
            print("The internal clock is off, so it won't run yet. Turn it on in /options.")
