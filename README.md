# Echo

A local-first agent harness for macOS. Echo runs on your own Mac with local
models (Ollama, llama.cpp, or MLX), keeps layered memory, runs scheduled tasks,
watches the Mac for security problems, and can be reached from your phone over
Telegram. The harness, not the model, decides what is allowed: every rule is
plain code you can read.

## What it does

- **Chat in the terminal** with tool use (files, shell, search, math, calendar),
  asking permission before anything risky.
- **Three inference drivers**: Ollama, llama.cpp, and MLX, switchable in `/options`.
  A memory guard keeps models inside your GPU budget.
- **Memory layers**: seeds, interaction memory, a notes vault, reminders, and
  self/user profiles.
- **Scheduled tasks**: markdown files the internal clock runs, each with only the
  folders and powers you approved. Task commands run inside macOS's sandbox.
- **Skills and sub-agents**: add skills as folders, and run deterministic agents
  or model-driven swarms over a codebase.
- **Sentinel**: a security watch that runs every few minutes without a model.
  It checks remote access, XProtect reports, startup items, sensitive files, and
  programs talking to the internet. High-risk finds are contained by fixed rules,
  quarantined and never deleted.
- **Telegram**: talk to Echo from your phone, approve actions with buttons, and
  run tasks while you're away.

## Requirements

- macOS on Apple Silicon (it's only been tested there).
- Python 3.9 or newer (the one at `/usr/bin/python3` is fine).
- At least one engine: [Ollama](https://ollama.com), llama.cpp, or MLX
  (`pip install mlx-lm`), with a model that supports tool calls.

## Install

```sh
git clone https://github.com/Echo-Systems404/echo.git
cd echo
python3 -m pip install -r requirements.txt
ollama pull qwen3:8b          # or any tool-calling model you like
python3 main.py
```

### Start Echo from any folder

Echo works in the folder you start it from. To open it by typing `echo` anywhere,
add this to `~/.zshrc` (change the path to where you cloned it), then open a new
terminal. With arguments, `echo` still prints text as usual, so scripts keep working.

```sh
echo() {
  if [ $# -eq 0 ]; then
    python3 "$HOME/echo/main.py"
  else
    builtin echo "$@"
  fi
}
```

Echo starts with the example profiles. To make it yours:

1. Copy `user.example.md` to `user.md` and describe yourself.
2. Copy `self.example.md` to `self.md` to change who Echo is.
3. Optional: create `local_settings.py` to override settings in `config.py`:

   ```python
   USER_NAME = "Sam"
   MODEL = "qwen3:14b"
   ```

All three files are git-ignored, so they never leave your Mac. Your memory,
tasks, logs, and Telegram data (`options/`, `notes/`, `reminders/`,
`communication/*/data/`) are git-ignored too.

## Commands

| Command | What it does |
| --- | --- |
| `/help` | List the commands |
| `/model`, `/context`, `/mode` | Switch the model, the context window, or the mode |
| `/tasks` | Create and manage scheduled tasks |
| `/options` | Tasks, the internal clock, inference driver, memory guard, memory layers, Telegram |
| `/memory` | Fine-tune each memory layer (see below) |
| `/skills` | Turn skills on or off, edit, add, or remove them |
| `/subagents` | Deterministic agents and swarms |
| `/telegram` | Set up and configure the Telegram pipeline |
| `/status` | Model, folder, conversation size, loaded models |
| `/resume`, `/clear`, `/bye!` | Continue an unfinished task, start fresh, quit |

## Memory layers

Every layer works out of the box; `/memory` only fine-tunes it.

- **Conversation**: when the context window fills (60–95%, default 80), ask you, compact, or
  save to the memory layers and continue. How many messages carry over between requests.
- **Transient notes**: Echo's notebook, one titled entry per project, so new details land
  where they belong. Entry count, sizes, and optional fixed section names.
- **Ephemeral memory**: summaries of saved conversations, keep 1 to 31.
- **Task notes**: what a task writes when its context fills, so the next run continues.
  Turn them off to compact or end the run instead.
- **Memory vault**: your long-term memory, a folder of markdown notes. We recommend
  pointing it at an [Obsidian](https://obsidian.md) vault, but any notes app that keeps
  notes as `.md` files works. Choose whether Echo reads it on its own, asks first, or not
  at all. Echo only writes a note when you ask it to remember something, and never
  replaces one without asking.

Each layer has a format rubric (how Echo should write it) that you edit in `/memory`.
Echo can read the rubrics but never change them.

## Telegram

1. In Telegram, message @BotFather, send `/newbot`, and copy the token.
2. In Echo, open `/telegram`, paste the token, then send your bot any message
   and confirm it's you.

Only your account gets answers. On your phone, `/bye` ends the conversation,
`/tasks` lists your tasks, and permission prompts show Allow and Deny buttons.
Telegram runs in the background runner, so it keeps working after you close the
terminal, until the Mac restarts.

## Security watch

The pilot task `pilot_tasks/p1-cyber-security.md` turns the sentinel on. Approve
it in `/tasks` first. Cutting remote access needs one admin step, done once.
After the first scan, install the generated sudoers file:

```sh
sudo visudo -cf options/security/echo-security.sudoers && \
sudo install -m 440 options/security/echo-security.sudoers /etc/sudoers.d/echo-security
```

Echo can't edit its own harness folder, its safety rules, or pilot tasks. Only
you change those, by editing the files.

## Project layout and tests

`directory.md` maps every module and folder, and `SPEC.md` is the design record.
Run the tests with:

```sh
python3 -B -m unittest test_memory_layers test_telegram test_sentinel \
    test_skills_subagents test_engine_drivers test_options_security test_memory_clock
```

## How Echo was built

Designed by 404 over two years of iteration, and built with:

- **Claude Opus 5.5**: co-wrote the code.
- **GPT-6.2 Astra**: refactored it.
- **Gemini 3.8**: reviewed and audited it.

## License

MIT. See `LICENSE`.
