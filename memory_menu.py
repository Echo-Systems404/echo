"""/memory (also /options → Memory layers): fine-tune each memory layer.

Every layer works without being configured: the defaults are today's numbers.
Numbers are checked against fixed ranges (memory_settings.py), and the format
rubrics are the user's files in "memory formats/".
"""

from pathlib import Path

import config
import memory_settings as ms
import notes
import ui
from cli_style import GREY, RESET, YELLOW

STARTER_NOTE = ("# Welcome\n\nThis folder is {agent}'s memory vault: notes it saves when you ask it to remember "
                "something. Open it in any notes app that keeps markdown files, such as Obsidian.\n")


class MemoryMenu:
    def memory_menu(self):
        while True:
            s = ms.load()
            action = {"ask": "ask me", "compact": "compact", "update": "memory update"}[s["context_action"]]
            notes_state = "on" if s["task_notes"] else f"off, {'compact' if s['task_notes_off'] == 'compact' else 'end the run'}"
            access = {"auto": "autonomous", "ask": "ask first", "off": "off"}[s["vault_access"]]
            pick = ui.menu("Memory layers", [
                ("conversation", f"Conversation (at {s['context_trigger']}%: {action}; keep {s['history_messages']} messages)"),
                ("transient", f"Transient notes ({s['transient_entries']} entries, {s['transient_total_chars']} characters)"),
                ("ephemeral", f"Ephemeral memory (keep {s['ephemeral_keep']}, {s['ephemeral_chars']} characters each)"),
                ("task", f"Task notes ({notes_state})"),
                ("vault", f"Memory vault ({access}, {ms.vault_dir()})"),
                ("back", "Back"),
            ])
            if pick == "conversation":
                self._memory_conversation()
            elif pick == "transient":
                self._memory_transient()
            elif pick == "ephemeral":
                self._memory_ephemeral()
            elif pick == "task":
                self._memory_task_notes()
            elif pick == "vault":
                self._memory_vault()
            else:
                return None
            self._memory_changed()

    def _memory_changed(self):
        """The pilot seed's memory section is rebuilt with the new values."""
        agent_memory = getattr(self, "memory", None)
        if agent_memory is not None and hasattr(self, "system_prompt"):
            agent_memory.system_prompt = self.system_prompt()

    # -- helpers ------------------------------------------------------------

    def _number(self, key, label):
        low, high = ms.range_of(key)
        while True:
            typed = ui.line(f"{label} ({low}–{high}): ", str(ms.get(key)))
            if typed is None or typed is ui.BACK:
                return None
            try:
                value = int(typed.replace(",", ""))
            except ValueError:
                value = None
            if value is not None and low <= value <= high:
                return value
            print(f"Enter a whole number from {low} to {high}.")

    def _save(self, **changes):
        problems = ms.save(**changes)
        print("; ".join(problems) if problems else "Saved.")
        return not problems

    def _rubric(self, name):
        path = ms.rubric_path(name)
        text = ms.rubric(name)
        where = "the default" if ms.rubric_is_default(name) else str(path.relative_to(Path(config.__file__).parent))
        print(f"{GREY}The format Echo follows for this layer ({where}). Echo can read it but never change it."
              f"{RESET}")
        print(text or "(empty)")
        if ms.rubric_too_long(name):
            print(f"{YELLOW}It's over {config.MEMORY_RUBRIC_MAX_CHARS} characters; Echo only gets the first "
                  f"{config.MEMORY_RUBRIC_MAX_CHARS}.{RESET}")
        pick = ui.menu("Format", [("edit", "Edit it here"), ("default", "Restore the default"), ("back", "Back")])
        if pick == "edit":
            typed = ui.multiline("The format", text)
            if typed and typed is not ui.BACK:
                if len(typed) > config.MEMORY_RUBRIC_MAX_CHARS:
                    print(f"That's {len(typed)} characters; the limit is {config.MEMORY_RUBRIC_MAX_CHARS}. Not saved.")
                    return
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(typed.strip() + "\n", encoding="utf-8")
                print("Saved. It applies from Echo's next message.")
        elif pick == "default":
            path.unlink(missing_ok=True)  # without your copy, the default applies
            print("Restored the default format.")

    # -- the layers ---------------------------------------------------------------

    def _memory_conversation(self):
        while True:
            s = ms.load()
            pick = ui.menu("Conversation", [
                ("trigger", f"Act when the context window is {s['context_trigger']}% full"),
                ("action", "Then: " + {"ask": "ask me", "compact": "compact automatically",
                                       "update": "memory layer update automatically"}[s["context_action"]]),
                ("history", f"Messages kept between requests: {s['history_messages']}"),
                ("format", "Compaction format"),
                ("back", "Back"),
            ])
            if pick == "trigger":
                value = self._number("context_trigger", "Percent of the context window")
                if value:
                    self._save(context_trigger=value)
            elif pick == "action":
                value = ui.menu("When the context fills", [
                    ("ask", "Ask me each time"),
                    ("compact", "Compact: summarize and continue from the summary"),
                    ("update", "Memory layer update: save to the memory layers, clear, and continue")],
                    current=s["context_action"])
                if value:
                    self._save(context_action=value)
            elif pick == "history":
                value = self._number("history_messages", "Messages kept")
                if value:
                    self._save(history_messages=value)
            elif pick == "format":
                self._rubric("compact")
            else:
                return

    def _memory_transient(self):
        while True:
            s = ms.load()
            sections = ", ".join(s["transient_sections"]) or "any (Echo names them)"
            pick = ui.menu("Transient notes", [
                ("entries", f"Entries allowed: {s['transient_entries']}"),
                ("entry", f"Characters per entry: {s['transient_entry_chars']}"),
                ("total", f"Characters in all: {s['transient_total_chars']}"),
                ("sections", f"Fixed sections: {sections}"),
                ("format", "Format"),
                ("back", "Back"),
            ])
            if pick in ("entries", "entry", "total"):
                key, label = {"entries": ("transient_entries", "Entries allowed"),
                              "entry": ("transient_entry_chars", "Characters per entry"),
                              "total": ("transient_total_chars", "Characters in all")}[pick]
                value = self._number(key, label)
                if value:
                    self._save(**{key: value})
                    current = len([t for t in notes.transient_text().splitlines() if t.startswith("## ")])
                    if key == "transient_entries" and current > value:
                        print(f"{GREY}The notebook has {current} entries now; Echo can't add more until it "
                              f"merges or deletes some.{RESET}")
            elif pick == "sections":
                print(f"{GREY}Leave it empty to let Echo name its entries. Otherwise only these titles can be "
                      f"used, one per line.{RESET}")
                typed = ui.multiline("Allowed entry titles", "\n".join(s["transient_sections"]))
                if typed is None or typed is ui.BACK:
                    continue
                titles = [line.strip() for line in typed.splitlines() if line.strip()]
                self._save(transient_sections=titles)
            elif pick == "format":
                self._rubric("transient")
            else:
                return

    def _memory_ephemeral(self):
        while True:
            s = ms.load()
            pick = ui.menu("Ephemeral memory", [
                ("keep", f"Saved conversations kept: {s['ephemeral_keep']}"),
                ("chars", f"Characters per summary: {s['ephemeral_chars']}"),
                ("format", "Format"),
                ("back", "Back"),
            ])
            if pick == "keep":
                value = self._number("ephemeral_keep", "Saved conversations kept")
                if not value:
                    continue
                saved = len(notes._saved())
                if value < saved:
                    sure = ui.menu(f"You have {saved} saved. The next save deletes the {saved - value} oldest.",
                                   [(True, "Save anyway"), (False, "Cancel")])
                    if not sure:
                        continue
                self._save(ephemeral_keep=value)
            elif pick == "chars":
                value = self._number("ephemeral_chars", "Characters per summary")
                if value:
                    self._save(ephemeral_chars=value)
            elif pick == "format":
                self._rubric("ephemeral")
            else:
                return

    def _memory_task_notes(self):
        while True:
            s = ms.load()
            options = [("power", f"Task notes: {'on' if s['task_notes'] else 'off'}")]
            if not s["task_notes"]:
                options.append(("off", "When a task's context fills: " +
                                ("compact and continue" if s["task_notes_off"] == "compact" else "end the run")))
            options += [("chars", f"Characters: {s['task_notes_chars']}"), ("format", "Format"), ("back", "Back")]
            pick = ui.menu("Task notes", options)
            if pick == "power":
                self._save(task_notes=not s["task_notes"])
            elif pick == "off":
                value = ui.menu("When a task's context fills (task notes off)", [
                    ("compact", "Compact and continue"),
                    ("end", "End the run (not a failure; the next cycle starts fresh)")],
                    current=s["task_notes_off"])
                if value:
                    self._save(task_notes_off=value)
            elif pick == "chars":
                value = self._number("task_notes_chars", "Characters")
                if value:
                    self._save(task_notes_chars=value)
            elif pick == "format":
                self._rubric("task-notes")
            else:
                return

    def _memory_vault(self):
        while True:
            s = ms.load()
            folder = ms.vault_dir()
            found = folder.is_dir()
            print(f"{GREY}The vault is a folder of markdown notes; any notes app that keeps notes as files "
                  f"works (Obsidian, for example).{RESET}")
            if not found:
                print(f"{YELLOW}No folder at {folder} yet.{RESET}")
            access = {"auto": "autonomous", "ask": "ask first", "off": "off"}[s["vault_access"]]
            options = [("folder", f"Folder: {folder}")]
            if not found and not s["vault_dir"]:
                options.append(("create", f"Create {folder} with a starter note"))
            options += [("access", f"Access: {access}"),
                        ("read", f"Characters per read: {s['vault_read_chars']}"),
                        ("write", f"Characters per write: {s['vault_write_chars']}"),
                        ("format", "Format of saved notes"),
                        ("back", "Back")]
            pick = ui.menu("Memory vault", options)
            if pick == "folder":
                typed = ui.line("Vault folder: ", str(folder))
                if typed and typed is not ui.BACK:
                    if self._save(vault_dir=typed):
                        print(f"The vault is now {ms.vault_dir().resolve()}.")
            elif pick == "create":
                folder.mkdir(parents=True, exist_ok=True)
                welcome = folder / "Welcome.md"
                if not welcome.exists():
                    welcome.write_text(STARTER_NOTE.format(agent=config.AGENT_NAME), encoding="utf-8")
                print(f"Created {folder}.")
            elif pick == "access":
                value = ui.menu("Vault access", [
                    ("auto", "Autonomous: Echo reads it when it helps"),
                    ("ask", "Ask first: every search or read asks you (off during tasks)"),
                    ("off", "Off: no vault tools at all")], current=s["vault_access"])
                if value:
                    self._save(vault_access=value)
            elif pick in ("read", "write"):
                key = "vault_read_chars" if pick == "read" else "vault_write_chars"
                value = self._number(key, "Characters per " + pick)
                if value:
                    self._save(**{key: value})
            elif pick == "format":
                self._rubric("vault")
            else:
                return
