# Spec: protected harness, seed system, context resets, cycle clock

Agreed on 2026-11-23. Every number below is a setting in `config.py`,
not a fixed rule. Code must run on Python 3.9 (the background runner uses it).

## 1. Protected harness

The harness folder is the user's to manage. Echo changes its own memory only
through the memory tools.

- [x] In chat and in tasks, `write_file` and `append_file` are blocked anywhere
      inside the harness folder (symlinks resolved). The model is told why with
      a security seed.
- [x] In chat, a command that names the harness folder, or that runs while the
      working folder is inside it and can change files (`>`, `mv`, `cp`, `tee`,
      `sed -i`, `git checkout/restore/reset/stash/apply/commit`, `python`, ...),
      needs the user's approval. Read-only commands (`ls`, `cat`, `grep`,
      `git status/log/diff`) don't. Best effort, like every command check.
- [x] Tasks keep their stricter rules: the gate blocks the harness outright.

## 2. Interaction seeds

Folders: `short term`, `long term`, `memory vault seeds`. State in `.state.json`.

**Who does what.** The harness creates, moves, and deletes seeds. Echo only
edits what is inside them. No merging, ever.

- [x] **Creation.** Every chat conversation gets its short-term seed when it
      starts (the harness creates the file). A context flush keeps the same
      conversation and the same seed.
- [x] **Topics.** A seed is a title line plus numbered topics:
      `## t1 · Website`. Echo adds topics and edits their lines with
      `interaction_seed`. The harness numbers topics; Echo can't remove one.
- [x] **References, per topic.** `interaction_recall(id, topic)` from another
      conversation counts one reference for that topic and keeps the seed
      alive. Reading a whole seed counts nothing. Scheduled tasks never count.
- [x] **Promotion (deterministic).** When a topic has `PROMOTE_AFTER_REFS` (2)
      references from other conversations (3 counting its own), the harness
      copies it into a new long-term seed named `<topic-slug>-<date>`, and
      replaces it in the original seed with `Moved to long term: <id>`.
- [x] **Short term** expires `SHORT_TERM_DAYS` (14) after its last edit or
      reference and is deleted by the harness (when a chat starts or a clock
      cycle begins). A seed from another conversation that never got a topic
      is deleted after an hour (so a second open Echo window keeps its own).
- [x] **Long term** expires `LONG_TERM_DAYS` (30) after its last edit,
      reference, or review. It is deleted only if a seed-consolidation task has
      run since it expired, so a broken consolidation task can't wipe memory.
- [x] **Memory vault seeds** never expire. They are frozen: editable only in a
      chat with the user, never by a task.
- [x] **Saving a memory** (`memory_save`, chat only) writes two files:
      1. the Obsidian note (summary, brief, footnotes, ...), with the harness
         adding the seed's name and location at the very bottom;
      2. a vault seed named `<brief-description>-<date>`, holding a copy of the
         source topics (so it survives when they expire) and a link to the note.
      Each source topic gets a line `Saved to the vault: <id>`.
- [x] **Search** lists vault seeds first, then long-term seeds that link to the
      vault, then the rest, with tier, expiry, and per-topic reference counts.
- [x] **Consolidation.** A task with the new `seeds` power can edit short- and
      long-term seeds and mark them reviewed (`interaction_review`). Editing or
      reviewing restarts a long-term seed's window.
- [x] **User seeds** (`seeds/user.seeds.md`): things Echo learns about the user.
      Echo adds, prunes, or rewrites it (`user_seeds`, chat only). No expiry,
      size-capped, shown in the system prompt from the next conversation (or
      flush) on. `user.md` stays the user's alone.

## 3. Context resets

The harness measures real context use from Ollama's token counts after every
model call.

- [x] **Chat, at `CONTEXT_PROMPT_AT` (80%)**, checked before each model call
      (so mid-task too), the user picks:
      - **Memory layer update:** Echo updates transient notes (nuance: project
        names, renames, locations; coding: dependencies, ports, functions,
        variables), ephemeral memory (summary and footnotes), and its
        interaction seed. Not the vault. Then the context is flushed.
      - **Compact:** Echo writes a summary that replaces the history.
      - **Not now:** asked again at the next request.
- [x] **After a flush**, Echo starts at an empty context with a harness note
      holding its saved summary and transient notes, so it is grounded without
      having to go looking. Mid-task, the note also says what it was doing and
      lists the files changed so far, and tells it to continue without redoing
      or overwriting finished work. The model isn't reloaded; clearing the
      history is the flush.
- [x] **Tasks, at 80%:** Echo writes **task notes** (a nuanced summary of the
      work so far), then the same flush and continue happens, with the task's
      instructions, its notes, and the harness's list of changed files.
- [x] The old message-count compaction keeps earlier summaries instead of
      cutting them down each time.

## 4. Internal clock: cycles

- [x] The clock fires every cycle (`minutes` in `/options`). A cycle runs every
      due task back to back in number order, each in a fresh instance.
- [x] A task is due every cycle unless it says `every: 24h`, which means: skip
      it in a cycle if it ran less than 24 hours ago. `every` is optional;
      `every: per internal clock` means every cycle. A task with unfinished task
      notes is always due.
- [x] Only one task runs at a time. If a cycle runs past the next one, the
      missed cycles collapse into one that starts right after.
- [x] **Time limit** per task, defaulting to the cycle length. At the limit,
      Echo writes task notes and stops; the run doesn't count as a failure, and
      the next cycle picks up from the notes. Notes are cleared when the task
      finishes successfully.
- [x] **Finished tasks** (repeat count reached) are archived as one line in
      `options/tasks/done.jsonl` (settings, text, runs, last report) and their
      files removed. Deleted tasks are archived the same way. The tasks after
      them move up a number (files, state, logs, notes), only while no task is
      running.
- [x] The background runner restarts itself when the harness code changes.

## 5. Checks

- [x] Unit tests that need no model: protection, seed life cycle, promotion,
      vault save, frozen vault seeds, context reset, task notes, cycles,
      archiving and renumbering.
- [x] `pilot.md` describes the new seed rules (the user's file, updated once for
      this change).

---

# Spec: task options, reminders, security scan

Agreed on 2026-11-24. Numbers are settings in `config.py`.

## 6. Task options

The task menu keeps its layout; new rows are added to it.

- [x] **Options** shows Tasks, Saved tasks, Internal clock, Back.
- [x] **A task** shows (as they apply) Approve, Run now, Pause/Resume, Edit task
      spec, Deactivate, Delete, Back.
- [x] **Deactivate** moves the task to `options/tasks/saved/` without a number
      (its state, log, and task notes go with it) and the tasks after it move up.
      Pause stays as it is: the task keeps its number but is skipped.
- [x] **Saved tasks** lists each saved task with its name and a one-line
      description. A saved task shows Activate, Edit task spec, Delete, Back.
- [x] **Activate** puts it back at the end of the line with the next number,
      unpaused, keeping its run history.
- [x] **Edit task spec** opens a menu: Name, Model, Instructions (the .md text in
      the same editor as the wizard), Timing (how many times, how often), Where,
      What it can do, Done. Each change is saved right away and stays approved,
      because the user made it. Edits made outside Echo still need approval.
- [x] Deactivate, activate, edit, and delete wait while a task is running.

## 7. Reminders

Things an agent should bring up next time the user talks to Echo.

- [x] Stored in `reminders/reminders.jsonl` in the harness, one JSON line each:
      time, who wrote it, urgency (`urgent` or `normal`), text, and when it was
      seen. Only the harness writes the file; agents use tools.
- [x] **`remind(text, urgent)`** is given to every task and is on-demand in chat.
      An identical unseen reminder isn't added twice. At most
      `REMINDERS_PER_RUN` per task run.
- [x] An urgent reminder also shows a macOS notification (best effort).
- [x] **When chat starts**, the banner says how many reminders are waiting.
      Unseen urgent reminders go into the system prompt, and Echo brings them up
      in its first reply. Unseen normal ones are listed there too as a short note
      for when they're relevant. They're marked seen after Echo's first reply,
      then dropped from the prompt.
- [x] A task that runs while chat is open prints its new urgent reminders and
      they join the prompt the same way.
- [x] **`reminders()`** (chat, on-demand) lists recent reminders.
- [x] Reminders older than `REMINDER_KEEP_DAYS` (30) are removed.
- [x] Task memory stays read-only: a reminder is outgoing mail, not memory.

## 8. Security scan

The harness scans and acts by fixed rules. The model never decides what to cut;
it explains and logs.

- [x] **`security_scan` tool**, given by the new `security` power (and on-demand
      in chat). It needs no admin password to scan. It checks:
      - TCP ports listening on the network (not just this Mac), by app;
      - remote access: Remote Login/SSH (22), Screen Sharing (5900), Remote
        Management (3283), File Sharing (445);
      - remote-control and tunnel apps running (TeamViewer, AnyDesk, RustDesk,
        ngrok, ...);
      - apps with connections to the internet;
      - firewall, stealth mode, FileVault, Gatekeeper, System Integrity Protection;
      - startup items (LaunchAgents and LaunchDaemons).
- [x] **Baseline.** The first scan records what is there, and anything already on
      goes into `options/security/allowed.txt`. After that each scan reports
      what's new once, then adds it to what's known.
- [x] **Cuts (rule B).** Remote access found that isn't in `allowed.txt` is cut by
      the harness: Remote Login, Screen Sharing, and File Sharing are turned off,
      Remote Management is deactivated, and remote-control apps running as the user
      are quit. The firewall is turned on if it goes off and `firewall-off` isn't
      allowed. Nothing else is ever cut, including internet connections.
- [x] Cuts use `sudo -n` with a sudoers file that allows only those exact
      commands (all of them turn things off or protections on). Echo writes the
      file and the user installs it. Without it, a cut fails and the reminder gives
      the command to run.
- [x] **Reminders from the harness**, not the model: urgent for a cut, a failed
      cut, remote access, a new startup item, or a protection turning off; normal
      for a new app listening on the network or connecting out. A problem that
      stays is reminded again at most every `SECURITY_REPEAT_HOURS` (24).
- [x] The tool returns a plain report (findings, cuts, fix commands) for the
      model to explain in its log.
- [x] **The "cyber security" task**: every 15 minutes, forever, in
      `~/Documents/Echo tasks/cyber security`, powers read, append, security. It
      runs the scan and appends one JSON line to `security-log.jsonl`.

## 9. Checks

- [x] Unit tests: deactivate/activate/renumber, edits stay approved, saved-task
      delete; reminders (dedupe, seen, prune, per-run cap, harness-only); security
      parsing, baseline, cut rules, allow list, repeat limit — with commands faked.

---

# Spec: inference drivers, lease engine, memory guard

Agreed on 2026-11-24. Goal: a lean engine that is only alive while the
harness is, holds one model at a time, and never lets the harness go over a
memory limit the user sets. Saved conversation caches aren't worth memory: the real
context lives in the memory layers.

## 10. Swappable inference drivers

- [x] `/options` > **Inference driver**: Ollama, llama.cpp, or MLX. Each is
      listed with whether it's available on this Mac. Switching unloads the
      current model, then asks which model to use with the new driver
      (remembered per driver).
- [x] One driver interface (`inference.py`) used everywhere Echo touched Ollama:
      chat streaming, model list, tool support, model facts (size, max context,
      cache per token), what's loaded, unload, health.
- [x] **Ollama**: as today, through its API.
- [x] **llama.cpp**: Echo starts `llama-server` (Homebrew's if installed, else
      the one inside Ollama.app) on a private local port, with the GGUF files
      Ollama already stores (plus any in `LLAMACPP_MODELS_DIR`). Model facts come
      from the GGUF file itself.
- [x] **MLX**: Echo starts `mlx_lm.server` with a Python that has `mlx_lm`
      (`MLX_PYTHON`, found automatically), using complete MLX models in the
      Hugging Face cache (plus `MLX_MODELS_DIR`), prompt cache limited to one
      conversation and capped in bytes.
- [x] llama.cpp and MLX speak the OpenAI chat format; Echo converts its history
      and tool calls both ways and reads thinking, tool calls, and token counts.
- [x] Tasks run on the current driver. A task whose model the driver doesn't have
      fails with "Model not installed", as today; Edit task spec lists the
      driver's models.

## 11. Lease engine: alive only while the harness is

- [x] **One model at a time**, across drivers and Echo windows: before a model
      loads, every other model Echo can see is unloaded (Ollama's loaded models,
      and any server Echo started).
- [x] **Ollama lease**: every request asks Ollama to keep the model only
      `LEASE_MINUTES` (5). While a chat is open and not idle, a heartbeat renews
      it. If Echo dies, the model unloads on its own when the lease runs out.
- [x] **llama.cpp / MLX tether**: the server runs under a small tether process
      that kills it the moment the Echo process that started it is gone (even if
      Echo is force-killed or the terminal closes). Echo never restarts it on
      its own.
- [x] **Quitting Echo** unloads the model right away.
- [x] **Idle in chat** for `CHAT_IDLE_MINUTES` (15) at an empty prompt: if the
      conversation has anything new, Echo runs a memory layer update (as at 80%),
      the context is flushed, then the model unloads. The next message reloads
      it and continues from the memory layers.
- [x] **Background runner**: the model unloads when a cycle ends (and after a
      task run while chat is open). Reloading costs a few seconds; memory stays
      free.

## 12. Memory guard

- [x] `/options` > **Memory guard**: Off, or a limit in GB (presets + custom),
      saved in `options/engine.json`.
- [x] Before a model loads, the harness estimates weights + conversation cache
      at the chosen context. Over the guard: the context is lowered until it
      fits (Echo says so); if the weights alone don't fit, the model isn't
      loaded and Echo says why.
- [x] `/context` choices are judged against the guard (or the GPU limit,
      whichever is lower). MLX's prompt cache is capped at what's left.
- [x] `/status` and the status panel show what's loaded through the driver.

## 13. Checks

- [x] Unit tests: history/tool-call conversion and OpenAI stream parsing (with a
      local fake server), GGUF metadata reading, the tether killing its server
      when its parent dies, the guard (lower context / refuse), one-model
      unloading, Ollama lease and release, idle update + flush + unload.
- [x] A real run on llama.cpp with a model from Ollama's store.

---

# Spec: skills engine, system status, sub-agents and swarms

Echo's first real skill is running sub-agents. It sits on a small skills engine
so more skills can be added and removed later. Numbers are config settings.

## 14. Skills engine

- [x] A skill is a folder in `skills/<name>/`: `SKILL.md` (front matter: name,
      one-line description; body: instructions for Echo) and optionally
      `tools.py` (Python tools the skill adds, as a `TOOLS` dict like tools.py).
- [x] Echo's system prompt lists each enabled skill's name and description only.
      `load_skill(name)` gives Echo the full instructions and adds the skill's
      tools for the rest of the conversation (like `load_tool` today).
- [x] `skills/` is part of the harness folder: Echo can read it, never write it.
      Only the user adds, edits, and removes skills.
- [x] Which skills are on is kept in `options/skills.json`. A skill whose
      `SKILL.md` or `tools.py` changed outside Echo is off until the user approves it
      (same digest check as tasks).
- [ ] `/skills` (listed after `/options`): the list of skills with on/off, and
      per skill: Turn on / Turn off, View instructions, Edit instructions (same
      editor as task specs), Remove (moved to `skills/removed/`, not deleted).
      Add skill: from a folder path.
- [x] A skill that fails to load (bad front matter, `tools.py` doesn't import)
      is shown as broken with the reason and never crashes Echo.

## 15. System status for Echo

- [x] Tool `system_status` (always available, read-only, deterministic): the
      model in use, driver, context size and how full it is, memory guard,
      what's loaded and its memory, total / used / free memory, chip heat.
      The same data the status panel shows.
- [x] It also lists which installed models qualify as sub-agents right now
      (section 16), each with the context it would get and its memory cost.

## 16. Soft memory lock

- [x] Deterministic, no model involved. Budget = the lower of the GPU limit and
      the memory guard, minus what's already loaded (weights + cache at its
      current context), minus `SUBAGENT_HEADROOM_GB` (2) for the system.
- [x] A model qualifies when its weights + cache at `SUBAGENT_MIN_CTX` (8k)
      fit the budget. It gets the largest context up to `SUBAGENT_CTX` (16k)
      that fits. Models that can't call tools are only used for agents that
      don't need tools.
- [x] Two models at once is allowed only for sub-agents and only when the lock
      says they fit. If the sub-agent model doesn't fit beside Echo's, Echo's
      model is parked (unloaded) for the run and reloaded after. Everything
      else keeps the one-model rule.
- [ ] Parallel agents (not built yet; agents run one after another): on llama.cpp and MLX, a swarm may run
      `SWARM_PARALLEL` (1) agents at once only if the extra cache for each
      still fits; otherwise they run one after another. Ollama runs them one
      after another.

## 17. Sub-agents

- [x] Two kinds, both saved as config files (never code) in
      `options/subagents/agents/<name>.json`:
      - **Rule agent** (no model): a list of checks from a fixed set of Python
        building blocks: pattern search (regex over files), Python checks
        (functions nothing calls, unused imports, bare `except`, `shell=True`,
        hardcoded secrets, TODO/FIXME), and commands from the user's allow list
        (tests, linters). Same input, same output.
      - **Persona agent** (a model): instructions (its personality and focus),
        a model or "any that qualifies", context size, temperature 0,
        read-only tools only (read file, list folder, search).
- [x] Sub-agents only report. They can't change files, run other commands,
      launch sub-agents, or use memory tools. They never get more power than
      whoever launched them.
- [ ] `/subagents`:
      - Autonomous sub-agent: pick the default model for persona agents from
        the models that qualify (with their memory cost shown).
      - Deterministic agents: Add agent (each setting asked before it's
        created), Agent library (list; each agent: edit any setting, test it
        on a folder, delete).
      - Deterministic swarms: Add swarm, Swarm library (list; each swarm: edit,
        delete).

## 18. Swarms

- [x] A swarm (`options/subagents/swarms/<name>.json`): members (library
      agents, each with a count), passes (`SWARM_PASSES` 1 by default),
      stop early when a pass finds nothing new (on by default), and the
      target (a folder or files, given when it runs).
- [x] Order: rule agents first, then persona agents, which see the rule
      agents' findings. Each pass after the first sees all findings so far and
      is told to find what's missing, not repeat it.
- [x] Results are merged: duplicates combined, each finding shows how many
      agents reported it and which. Saved to `options/subagents/runs/` and
      returned to whoever launched it.
- [x] Limits: `SWARM_MAX_AGENTS` (25), `SWARM_MAX_PASSES` (5),
      `SUBAGENT_TIME_LIMIT` per agent, and Esc stops the whole swarm.

## 19. The swarm skill (Echo's)

- [x] Tasks can use skills: a task's `skills:` line (picked in the new-task
      wizard and Edit task spec) lists the skills it may load. Only skills that
      are on are offered; the task file's approval covers the list.
- [x] Echo can create tasks (`create_task`, chat only): the user sees every setting
      and the full instructions in the permission prompt and the task is saved
      only on yes (already approved). Same checks as the wizard: folders exist
      and aren't the harness, known powers, a valid timing, an installed model,
      skills that are on. Deleting tasks stays in /options only.

- [x] `skills/swarm/`: tells Echo to call `system_status` first and plan
      within what fits.
- [x] `run_swarm(swarm or agent name, target)`: runs one from the user's library.
- [x] `run_echo_swarm(agents, passes, target)`: an Echo swarm or Echo
      sub-agent. Echo picks the settings (personas, rule checks, counts,
      passes, model from those that qualify) within the same limits. It exists
      only for that run: nothing is saved but the run's report.
- [x] Echo can use the library but never edit it. Echo can't use commands
      outside the user's allow list.

## 20. Checks

- [x] Unit tests: skill loading, approval, and broken skills; the soft lock
      (qualifies / doesn't / lower context / park Echo's model); each rule
      check on a sample folder; swarm passes, stop early, merge counts;
      Echo swarms leave nothing behind; sub-agents can't write.
- [ ] A real run: a 25-agent Echo swarm on a sample codebase.

---

# Spec: the sentinel (cyber security), pilot tasks, command sandbox

The harness watches the Mac on its own, often and cheaply; the model only
reads what the harness logged and writes the entry, or responds to an
incident inside fixed limits. No outside tools (no ClamAV): only what macOS
ships. Numbers are config settings.

## 21. Pilot tasks

- [x] `pilot_tasks/` in the harness folder: task files in the usual format,
      named `p<n>-<name>.md` (id `p<n>`). Echo can't change them (harness
      folder); only the user, by editing the files.
- [x] `/tasks` shows them first, under "Pilot tasks". Actions: Run now, Pause /
      Resume, View spec, Approve (when the file changed). No edit, delete, or
      deactivate in the UX; the task functions refuse them too.
- [x] They run in the clock's cycles before numbered tasks. State, logs and
      task notes work as for any task.
- [x] Ships with `p1-cyber-security.md` (Echo's model, powers read, append,
      security, folder `~/Documents/Echo tasks/cyber security`, made on the first run).

## 22. The sentinel: the harness watches

- [x] Runs every `SENTINEL_MINUTES` (5) while Echo or the background runner
      is open, as long as a pilot task with the security power is active. One
      process scans at a time (a lock plus the last scan time). No model.
- [x] Each scan: the remote-access scan as today (cuts and reminders
      unchanged), plus the malware checks (23). What it finds is appended to
      `options/security/events.jsonl`: id, time, kind, severity (info,
      medium, high), text, the item, what was done.
- [x] The first sentinel scan sets the baseline (startup items and their
      signatures, sensitive-file fingerprints, processes): nothing in the
      baseline is flagged. Only changes after it are.
- [x] Slow checks run every `SENTINEL_SLOW_MINUTES` (60).
- [x] Events stay until the model has summarized them; summarized events are
      kept `SECURITY_EVENTS_KEEP_DAYS` (30), then removed.

## 23. Malware checks (macOS's own tools only)

- [x] XProtect Remediator, Apple's malware scanner: its reports in the
      system log since the last scan. A threat found = high.
- [x] XProtect's signatures older than `XPROTECT_MAX_AGE_DAYS` (30) = medium
      (slow check).
- [x] Startup items (launch agents and daemons): the program each one runs is
      checked with codesign and Gatekeeper (cached by path, size, and
      modified time). A new item whose program is unsigned, ad-hoc signed, or
      rejected = high; a new signed item = medium.
- [x] Sensitive files fingerprinted: /etc/hosts, /etc/sudoers and
      /etc/sudoers.d, ~/.ssh/authorized_keys, shell startup files (.zshrc,
      .zprofile, .bash_profile, .bashrc, .profile), the user crontab. A change
      = medium (never reverted automatically).
- [x] Processes: a program running from /tmp, /var/tmp or /Users/Shared =
      high if unsigned, medium if signed. A process whose program file was
      deleted = medium. An unsigned program connected to the internet that
      wasn't there at the baseline = medium.

## 24. Responding

- [x] High: the harness contains at once, by fixed rules, only the item it
      flagged: stops the process (the user's own processes), moves the file to
      `options/security/quarantine/` with a manifest of where it came from
      (never deletes), switches off a user startup item (unloads it and
      quarantines its file). What needs root isn't touched: the user gets the
      exact command. An urgent notification goes out.
- [x] A high event opens an incident. The pilot security task runs right
      away (even if the clock's cycle isn't due or the clock is off, as long
      as Echo or the runner is open), with `security_events`,
      `security_rescan`, and `security_contain`. The model investigates
      (read-only) and may only re-contain high items the harness flagged.
- [x] After each response run the harness rescans. The incident closes when
      nothing high is active. Otherwise the task runs again, up to
      `SENTINEL_RESPONSE_ROUNDS` (5); then the user gets an urgent reminder to
      disconnect from the network and look, and the incident stays open.
- [x] Medium: an urgent or normal reminder (as today) and the next summary
      covers it. Nothing is touched.

## 25. Summaries

- [x] The pilot security task's normal runs read only the events since its
      last summary (`security_events`) and write one entry to its log file.
      The events it saw are marked summarized only when the run finishes ok.

## 26. Command sandbox

- [x] Task commands (`run_command` in a task) and sub-agent commands run
      under macOS's sandbox (`sandbox-exec`): writing is allowed only in the
      task's folders (the target, for sub-agents) and temp folders; the
      harness folder can't be written at all. Enforced by macOS, not by
      reading the command. The gate's pattern checks stay as an early warning.
- [x] Chat commands are unchanged (the user approves them).

## 27. Checks

- [x] Unit tests: pilot tasks can't be edited, deleted or deactivated and run
      first; each malware check on fake system output; baseline then change;
      high event contains and opens an incident; response rounds and
      escalation; summaries mark events only on ok; the sandbox blocks a
      write outside the task folder and into the harness.
- [ ] A real sentinel scan on this Mac (baseline, then quiet). The checks ran read-only
      here (about 2 s first, then cached); the first real scan runs once the user approves p1.

# Spec: Telegram (communication/telegram/)

Talk to Echo from your phone. Telegram is the one pipeline in Mark 1. Built on
the usual bot design (token plus allowed user, long polling, handled-once
updates, stale messages dropped, 4096-char splitting, approval buttons), on the
standard library instead of python-telegram-bot, so its connection handling
can't deadlock.

## 28. Layout
- [x] `communication/` holds every pipeline, one folder per app; `communication/telegram/`
      has api.py (Bot API client), store.py (data and recall), bridge.py (the
      conversation), menu.py (/telegram).
- [x] Each app's `data/` folder is private: git-ignored, folder 700 and files 600, and
      Echo's file tools can't read, list, search, or write it (gate + search_files).

## 29. /telegram (also in /options)
- [x] Bot token (hidden input, checked with getMe), pairing (message the bot, confirm
      "Is this you?"), on/off.
- [x] Model, inference driver, context window: each "same as Echo" by default.
- [x] Conversation ends after N minutes without a message (5/10/15/30/60, default 10).

## 30. The conversation
- [x] Served by the background runner, which now runs while Telegram is set up even with
      the clock off (and keeps the Mac awake, as before). It keeps working after the
      terminal closes, until the Mac restarts.
- [x] Only the paired account in a private chat is answered; everyone else gets nothing.
- [x] Its own conversation with the same self, user, seeds, skills, and tools.
- [x] One model turn at a time (turns.py): terminal chat, Telegram, tasks, and the
      terminal's idle save wait for each other; Telegram says "your message is next".
- [x] Permission prompts become Allow/Deny buttons; no answer in
      TELEGRAM_APPROVAL_MINUTES is Deny.
- [x] /end saves the conversation now; /start says hello; non-text gets a short note.
- [x] A conversation in progress survives a runner restart (current.json).
- [x] Urgent reminders (security incidents) are pushed to the phone once.

## 31. Logging and recall
- [x] An ended conversation is appended to data/conversations.jsonl (id, times, model,
      reason, messages) and its model is unloaded.
- [x] Echo's tools `telegram_conversations` (list or search) and `telegram_read`
      (one conversation, or "last") work in chat and on Telegram.

## 31b. Commands and tasks from Telegram
- [x] /bye (also /end, /new) ends the conversation, saves it, and unloads the model;
      the bot keeps listening, so the next message starts fresh. /tasks lists tasks
      straight from the harness (no model); /help. Telegram shows them in its / menu.
- [x] Echo's `list_tasks` (tasks, clock, waiting run) and `run_task` (the user approves with
      Allow/Deny). A run is queued in options/tasks/run_now.json and starts once the
      model is free, even with the clock off, from the runner or the open terminal.
- [x] A task run from Telegram sends its result to the phone (not to the terminal).
- [x] On Telegram the task tools are always loaded, so small models see them.

## 32. Tests
- [x] test_telegram.py: splitting, token never in errors, 409, settings and ready,
      recall, privacy, strangers ignored, stale messages, reply and idle end, /end,
      waiting for the model, tasks busy during a reply, buttons, pushed reminders.
- [x] A real agent build on this Mac and a real API call (bad token: 401, token hidden).
- [x] A live conversation from a phone (done before release).

---

# Memory layers configuration

Every layer works out of the box; the menu only fine-tunes it. Nothing changes
until the user changes it.

## 33. The menu and its rules
- [x] `/options` → "Memory layers" (also `/memory`): Conversation, Transient notes,
      Ephemeral memory, Task notes, Memory vault, then Back. Each entry shows its
      current values.
- [x] Settings are saved in `options/memory.json`. Defaults are today's numbers, which stay
      in config.py. Every number has a fixed range that the harness enforces when saving
      and again when loading, so a hand-edited value outside the range falls back to the
      default.
- [x] **Format rubrics** live in `memory formats/`: `transient`, `ephemeral`, `task-notes`,
      `vault`, and `compact`. The defaults ship in `memory formats/defaults/` with today's
      wording. A rubric the user edits is saved beside them (`memory formats/<name>.md`,
      git-ignored, so personal formats are never published); without one, the default applies.
      - Each rubric is part of the harness folder, so Echo can read it but never write it,
        like the safety rules.
      - A layer's rubric goes into that layer's tool description, so it costs context only
        when the tool is loaded.
      - Each rubric is capped at `MEMORY_RUBRIC_MAX_CHARS` (1500). A longer one is cut,
        and the menu says so.
      - The menu shows each rubric, edits it in the terminal, and can restore the default
        (by removing the user's copy).
- [x] The system prompt and the pilot seed get the live values from the harness (no more
      hardcoded "last 10"). The pilot seed gets a `{{memory}}` block: each layer, what it
      is for, its tools, its limits, and whether it's on. The harness fills it in, so it
      is always true.

## 34. Conversation
- [x] **When to act:** at 60–95% of the context window (default 80).
- [x] **What to do:** ask me (default), always compact, or always run a memory layer update.
      Tasks keep their own rule (§37).
- [x] **History kept between requests:** 10–100 messages (default 20).
- [x] Telegram can't ask, so there "ask me" compacts; the other two apply as set.
- KV cache quantization is left out of Mark 1 (llama.cpp keeps `LLAMACPP_KV_CACHE_TYPE`
  in config.py).

## 35. Transient notes
- [x] The notebook becomes **titled entries**, one per project or topic (`## <title>`). The
      model adds new information to the entry it belongs to, not only the latest one.
      - The tools become `transient_read(title?)`, which shows the list of entries (titles
        and sizes) or one entry, and `transient_edit(action, title, content)`.
      - The actions are `new`, `append`, `replace`, `delete`, and `wipe`.
      - Titles match without regard to case. Appending to a title that doesn't exist
        returns the list of titles, so the model can pick the right one.
- [x] **Entries allowed:** 1–30 (default 12). Past the limit, `new` is refused and the
      message names the oldest entry to merge or delete.
- [x] **Fixed sections** (optional): a list of titles that the user sets in the menu. When
      the list is set, only those titles can be used. When it's empty (the default), the
      model names its own entries.
- [x] **Size:** a cap per entry (1,000–8,000, default 4,000) and for the whole notebook
      (2,000–40,000, default 7,500). Reads go one entry at a time, so a bigger notebook
      never gets cut off by the tool output limit.
- [x] **Format:** `memory formats/transient.md`.
- [x] **The old notebook is converted once:** each `---` block becomes an entry, titled by
      its first heading or line. The original is kept as `transient.md.bak`.

## 36. Ephemeral memory
- [x] **Saved conversations kept:** 1–31 (default 10).
- [x] **Size per summary:** 2,000–20,000 characters (default 6,000).
- [x] **Lowering the number** shows how many of the oldest summaries the next save will
      delete, and asks before saving.
- [x] **Format:** `memory formats/ephemeral.md`. It covers how to write a summary and how
      to update it when saving again in the same conversation.

## 37. Task notes
- [x] **On** (default): today's behavior. At the trigger the task writes its notes and
      continues, and the next cycle resumes from them.
- [x] **Off:** a toggle sets what happens when a task's context fills:
      - **Compact and continue** (default): the run goes on from a summary.
      - **End the run:** the run stops there. It doesn't count as a failure, and the next
        cycle starts fresh.
- [x] **Size:** 2,000–20,000 characters (default 6,000).
- [x] **Format:** `memory formats/task-notes.md`.

## 38. Memory vault
- [x] **Folder:** the user types a path. The harness checks that it:
      - is an existing folder;
      - isn't the home folder itself, `/`, or a system folder (`/System`, `/Library`,
        `/usr`, `/private`, …);
      - isn't Echo's own folder or inside it;
      - isn't hidden.

      If no vault exists yet, the menu offers to create `~/Documents/Echo` with a starter
      note. Any app that keeps notes as markdown files works, Obsidian for example.
- [x] **Access:**
      - **Autonomous** (default, today): Echo reads the vault when it decides to.
      - **Ask first:** every vault read asks permission: the usual prompt in the terminal,
        Allow/Deny buttons in Telegram. Tasks can't ask, so for them the vault is off.
      - **Off:** no vault tools at all, and `memory_save` explains that the vault is off.
- [x] **Size:** a cap per read (2,000–40,000, default 8,000, cut with a note) and per
      write (1,000–40,000, default 20,000).
- [x] **Format:** `memory formats/vault.md`, which describes how `memory_save` structures a
      note (sections, frontmatter, tags, `[[links]]`).
- [x] Replacing an existing note still needs approval in every mode.
- [x] The vault tools' error points to `/options` → Memory layers, not to config.py.

## 39. Not in this menu (Mark 1)
Interaction seeds, user seeds, and reminders keep their config.py numbers. The pilot
seed's `{{memory}}` block explains them along with the rest.

## 40. Tests
- [x] Ranges are clamped when saving and when loading; a bad memory.json falls back to the
      defaults.
- [x] Rubrics reach the tool descriptions and are capped; Echo's write tools can't write
      `memory formats/`.
- [x] Transient: titled entries, appending to the right entry, the entry and size limits,
      fixed sections, and converting the old notebook.
- [x] Ephemeral: the keep number from 1 to 31, the size cap, and the warning before
      deleting.
- [x] Task notes off: compact-and-continue, and end-the-run (not a failure).
- [x] Vault: folder checks, Off/Ask/Auto (Ask is off in tasks, buttons on Telegram), and the
      read and write caps.
- [x] Conversation: trigger percent and history kept.
