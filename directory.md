# Echo directory map

Start at `main.py` for the chat loop and slash-command dispatch. Open the matching menu module for a change to an interactive screen. Paths below are relative to the harness root unless marked otherwise.

## Python modules

| Area | Files and responsibilities |
| --- | --- |
| Chat and screens | `main.py` owns the CLI and clock wakeups. `tasks_menu.py`, `options_menu.py`, `skills_menu.py`, `subagents_menu.py`, and `memory_menu.py` (/memory) supply its menu methods. `ui.py` provides terminal input widgets, `cli_style.py` defines chat and menu colors, and `panel.py` displays status. |
| Task lifecycle | `tasks.py` is the caller-facing API. It schedules cycles, selects tools, and executes tasks. `task_storage.py` parses and renders task files and handles approvals, state, notes, saves, and archives. `autonomy.py` handles unattended clock operation. |
| Tools and extensions | `tools.py` holds the tool registries and schemas. `tool_actions.py` implements shell, file, search, and task-creation actions and their permission checks. `instruments.py` implements utilities. `skills.py` manages skills and `subagents.py` runs agents and swarms. |
| Inference | `engine.py` drives model turns and tool calls. `inference.py` manages inference backends, `context.py` handles context sizing, `gguf.py` reads model metadata, `tether.py` supervises server lifetimes, and `sysstat.py` reports system resources and plans model placement. |
| Memory | `memory.py` assembles conversation context. `seeds.py`, `interaction.py`, `notes.py`, `vault.py`, `modes.py`, and `reminders.py` manage their respective memory layers, modes, and reminders. `memory_settings.py` holds the memory layer settings (options/memory.json, checked against fixed ranges) and the format rubrics. |
| Permissions and monitoring | `gate.py` checks tool policy, `sandbox.py` confines commands, `security.py` implements security scans, `malware.py` collects malware-related findings, and `sentinel.py` monitors and handles incidents. |
| Communication | `communication/` holds each outside pipeline in its own folder. `communication/telegram/` has `api.py` (Bot API client), `store.py` (settings, token, conversation log, recall tools), `bridge.py` (the conversation, run by the background runner), and `menu.py` (/telegram). `turns.py` lets one conversation use the model at a time. |
| Configuration and diagnostics | `config.py` defines defaults and paths. `errorlog.py` records errors and `live_check.py` runs a live check. |

## Files and data directories

Runtime directories and their contents depend on which features have been used.

```text
echo/
  README.md                     What Echo is, install, and setup
  SPEC.md                       Harness specification
  directory.md                  This map
  requirements.txt              Python dependencies
  self.md, user.md              Your profiles (git-ignored; the .example files are used until you make them)
  self.example.md, user.example.md
  local_settings.py             Your overrides of config.py (git-ignored, optional)
  self layers/                  Mode overlays
  memory formats/
    defaults/                   Default format rubrics for each memory layer
    <layer>.md                  Your edited rubrics (git-ignored)
  pilot_tasks/                  Built-in task specifications
  communication/
    telegram/                   Telegram pipeline code
      data/                     Private, git-ignored: token, settings, conversations.jsonl
  seeds/
    pilot.md                    Pilot prompt
    security/                   Security guidance
    interaction seeds/          Interaction memory
    user.seeds.md               User seeds
  skills/
    swarm/
      SKILL.md                  Skill definition
      tools.py                  Skill-local tools
  options/                      Runtime settings and state
    tasks/                      Numbered tasks, state, logs, notes, saved tasks
    subagents/                  Agent, swarm, and command settings
    logs/                       Runtime logs
    memory.json                 Memory layer settings (/memory)
  notes/                        Transient and ephemeral memory
  reminders/                    Reminder data
```

The vault defaults to `~/Documents/Echo`; pick any folder of markdown notes in /memory. Session history uses `.history` in the harness. The agent checkpoint uses `.agent_checkpoint.json` in the current working directory.

## Tests

`test_helpers.py` holds the temporary sandbox and fake model and driver classes. The seven suite modules are `test_memory_layers.py`, `test_telegram.py`, `test_sentinel.py`, `test_skills_subagents.py`, `test_engine_drivers.py`, `test_options_security.py`, and `test_memory_clock.py`.
