"""Skills menu methods for Echo."""

from pathlib import Path
import skills
import ui
from cli_style import BOLD, GREY, YELLOW, RESET


class SkillsMenu:
    def refresh_skills(self):
        """Skills changed: the agent's list and the prompt follow."""
        self.agent.skills = skills.usable()
        self.agent.loaded_skills &= set(self.agent.skills)
        self.memory.system_prompt = self.system_prompt()

    def skills_menu(self):
        while True:
            state = skills._state()
            found = skills.all_skills()
            options = [(s.name, f"{s.name}  [{skills.status(s, state)}]  - {(s.description or s.error or '')[:60]}")
                       for s in found]
            choice = ui.menu("Skills", options + [("add", "+ Add a skill from a folder"), ("back", "Back")], echo=False)
            if choice in (None, "back"):
                return
            if choice == "add":
                path = ui.line("Skill folder (with SKILL.md): ")
                if path and path is not ui.BACK:
                    result = skills.add_from(Path(path.strip("'\"").replace("\\ ", " ")).expanduser())
                    print(result if isinstance(result, str) else f"Added {result.name}. It's on.")
                continue
            self.skill_actions(next(s for s in found if s.name == choice))

    def skill_actions(self, skill):
        state = skills.status(skill)
        options = []
        if state == "broken":
            print(f"{YELLOW}{skill.name} is broken: {skill.error}. Fix it in {skill.folder}.{RESET}")
        if state == "needs approval":
            print(f"{GREY}New or changed outside Echo. Read it before approving: {skill.folder}{RESET}")
            options.append(("approve", "Approve and turn on"))
        if state == "on":
            options.append(("off", "Turn off"))
        if state == "off":
            options.append(("on", "Turn on"))
        if state != "broken":
            options += [("view", "View instructions"), ("edit", "Edit instructions")]
        options.append(("remove", "Remove (moved to skills/removed)"))
        choice = ui.menu(f"Skill: {skill.name}", options + [("back", "Back")], echo=False)
        if choice == "approve":
            skills.approve(skill)
            print(f"{skill.name} is on.")
        elif choice in ("on", "off"):
            skills.set_enabled(skill, choice == "on")
            print(f"{skill.name} is {choice}.")
        elif choice == "view":
            print(f"\n{BOLD}{skill.name}{RESET}  {GREY}{skill.description}{RESET}\n\n{skill.instructions}\n")
            tool_names = []
            try:
                tool_names = list(skill.tools()) if state == "on" else []
            except Exception as exc:
                print(f"{YELLOW}Its tools don't load: {exc}{RESET}")
            if tool_names:
                print(f"{GREY}Tools: {', '.join(tool_names)}{RESET}\n")
        elif choice == "edit":
            text = ui.multiline(f"Instructions for {skill.name}:", skill.instructions)
            if text and text is not ui.BACK:
                skills.save_instructions(skill, text)
                print(f"{GREY}Saved {skill.folder / 'SKILL.md'}.{RESET}")
        elif choice == "remove":
            if ui.menu(f"Remove {skill.name}?", [(False, "Keep it"), (True, "Remove")], echo=False):
                print(f"Moved to {skills.remove(skill)}.")
