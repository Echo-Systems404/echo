"""Tasks: markdown files the internal clock points the agent at.

Each task is options/tasks/<number>-<name>.md. The harness, not the model,
decides what is due and keeps the run count in options/tasks/.state.json.
A task only runs if its file is exactly what the user approved, so a file
written or edited by anything else waits for approval in /tasks.

The clock works in cycles. When a cycle starts, every due task runs back to
back in number order, each in a fresh instance, one at a time. A task is due
every cycle unless it sets `every: 24h` (skip it while it ran less than 24h
ago). A cycle that runs long just delays the next one; missed cycles collapse
into one. Finished and deleted tasks are archived in done.jsonl, and the tasks
after them move up a number between cycles.

Runs start in a clean conversation with memory read-only (a task with the
"seeds" power may edit seeds) and only the tools the task was given. A run
that fills its context writes task notes and continues; one that hits its time
limit writes them and stops, and the next cycle continues from them. With task
notes off (/options → Memory layers), a full context compacts and continues,
or ends the run, as the user chose.
"""

import os
import time
from pathlib import Path
from types import SimpleNamespace
import config
import engine
import errorlog
import gate
import interaction
import memory
import memory_settings
import reminders
import seeds
import skills
import tools
import turns

from task_storage import (
    _FRONT, _EVERY, _UNITS, MIN_EVERY, POWER_ORDER, KNOWN_POWERS, LOG_MAX_CHARS, STATE_FILE,
    CYCLE_FILE, ARCHIVE_FILE, SAVED, EVERY_CYCLE, parse_every, every_cycle, _write_json,
    _read_json, _state, _save_state, load_clock, save_clock, _run_lock, running, _digest,
    _parse, PILOT_ONLY, pilot_tasks, all_tasks, numbered_tasks, status, describe, _cycle,
    _save_cycle, cycle_open, _mark_done_in_cycle, _render, _slug, _next_number, create, approve,
    set_paused, MISSING_MODEL, mark_missing_model, missing_model_tasks, missing_model_source,
    delete, _log_path, _archive, renumber, _renumber, _saved_dir, _saved_state,
    _save_saved_state, saved_tasks, saved_status, brief, _move, BUSY, deactivate, activate,
    update, runs, delete_saved, _record, notes_path, TaskNotes,
)

READ_TOOLS = {"read_file", "list_directory", "search_files", "system_status", "clock", "calendar", "calculator", "seed_read",
              "interaction_search", "interaction_recall", "vault_search", "vault_read", "transient_read", "ephemeral_list", "ephemeral_read"}


REQUEST_FILE = "run_now.json"  # one task Echo was asked to run now (from chat or Telegram)


def request_run(task_id, source):
    _write_json(config.TASKS_DIR / REQUEST_FILE, {"id": task_id, "from": source, "ts": time.time()})


def requested():
    """The pending run-now request, or None."""
    request = _read_json(config.TASKS_DIR / REQUEST_FILE, None)
    return request if isinstance(request, dict) and request.get("id") else None


def clear_request():
    try:
        (config.TASKS_DIR / REQUEST_FILE).unlink()
    except OSError:
        pass


def _due(task, entry, now):
    if notes_path(task["id"]).exists():  # unfinished work continues
        return True
    last = entry.get("last_run")
    return task["every"] is None or last is None or now - last >= task["every"]


def next_due(now=None):
    """The next task to run in the current cycle, or None. Starts a cycle
    once the clock's interval has passed since the last one started, and
    closes it when no due task is left."""
    now = now or time.time()
    import sentinel
    if sentinel.due_response() and not running():  # an incident: the security task answers first
        urgent = sentinel.security_task()
        if urgent is not None:
            return urgent
    request = requested()
    if request and not running():  # The user asked for this one now; it doesn't wait for the clock
        wanted = next((t for t in all_tasks() if t["id"] == request["id"]), None)
        if wanted is not None and status(wanted) == "active":
            return wanted
        clear_request()  # gone or paused since it was asked for
    cycle = _cycle()
    if not cycle.get("open"):
        if cycle.get("start") and now - cycle["start"] < load_clock()["minutes"] * 60:
            return None
        cycle = {"start": now, "open": True, "done": []}
        _save_cycle(cycle)
        try:
            interaction.maintain(now=now)  # expired seeds go even while no chat is opened
        except Exception as exc:
            errorlog.log_error("tasks.next_due.maintain", exc)
            pass  # seed upkeep must never stop the clock
    state = _state()
    for task in all_tasks():
        if task["id"] in cycle["done"] or status(task, state) != "active":
            continue
        if _due(task, state.get(task["id"], {}), now):
            return task
    cycle["open"] = False
    _save_cycle(cycle)
    renumber()  # between cycles, so numbers never shift under a running cycle
    return None


def _notes_tool(store):
    return {
        "function": lambda content="": store.write(content),
        "schema": tools._schema(
            "task_notes",
            "Write your task notes: a nuanced summary of your work so far and what you need to continue. "
            "Replaces earlier notes. Use it when the harness asks.",
            {"content": {"type": "string", "description": "The notes, in markdown."}},
            ["content"],
        ),
    }


# -- running ---------------------------------------------------------------

def _tools_for(powers, store=None, areas=None):
    allowed = set(READ_TOOLS)
    if powers & {"append", "write"}:
        allowed.add("append_file")
    if "write" in powers:
        allowed.add("write_file")
    if "shell" in powers:
        allowed.add("run_command")
    chosen = {name: tool for name, tool in tools.TOOLS.items() if name in allowed}
    chosen["interaction_recall"] = {**chosen["interaction_recall"], "function": interaction.recall_for_task}
    if "seeds" in powers:
        chosen.update(tools.SEED_TASK_TOOLS)
        for name in ("interaction_search", "interaction_recall"):  # the consolidation job's main tools
            chosen[name] = {key: value for key, value in chosen[name].items() if key != "on_demand"}
    if "run_command" in chosen:  # macOS enforces where task commands may write
        writable = [str(a) for a in (areas or [])]

        def sandboxed(cmd, timeout=tools.DEFAULT_COMMAND_TIMEOUT):
            return tools.run_command(cmd, timeout, writable=writable)
        chosen["run_command"] = {**chosen["run_command"], "function": sandboxed}
    if "security" in powers:
        import sentinel
        chosen.update(sentinel.task_tools())
    if store is not None:
        chosen["task_notes"] = _notes_tool(store)
    return chosen


def time_limit_seconds():
    minutes = config.TASK_TIME_LIMIT_MINUTES or load_clock()["minutes"]
    return minutes * 60


def run(task, model, num_ctx, on_turn=None, on_flag=None, fit_context=None):
    """Run one task in a clean conversation, on its own model if it has one.
    fit_context(model, size) lowers the context to what that model can hold.
    Returns (result, final text)."""
    lock = _run_lock()
    if lock is None:
        return "busy", "Another task is running."
    turn = turns.hold()  # the model isn't in use by a chat or Telegram turn
    if turn is None:
        lock.close()
        return "busy", "Echo is answering a message (terminal or Telegram); the task runs next."
    request = requested()
    result, text = "failed", ""
    try:
        result, text = _run(task, model, num_ctx, on_turn, on_flag, fit_context)
        return result, text
    finally:
        turn.close()
        lock.close()
        if request and request["id"] == task["id"]:
            clear_request()
            if request.get("from") == "telegram":  # the bridge pushes this to the phone
                reminders.add(f"Task {task['id']} ({task['name']}) finished: {result}. {text.strip()[:600]}",
                              "telegram")


def _run(task, model, num_ctx, on_turn, on_flag, fit_context):
    if status(task) != "active":
        return "skipped", f"Task is {status(task)}."
    areas = [Path(a).expanduser().resolve() for a in task["areas"]]
    if task.get("pilot"):  # a pilot task's folders come from the codebase, so it may make them
        for area in areas:
            if not area.exists():
                try:
                    area.mkdir(parents=True)
                except OSError:
                    pass
    missing = [str(a) for a in areas if not a.is_dir()]
    if missing:
        _record(task, "failed", f"Folder not found: {', '.join(missing)}")
        return "failed", f"Folder not found: {', '.join(missing)}"

    settings = SimpleNamespace(**{k: v for k, v in vars(config).items() if k.isupper()})
    model = task.get("model") or model
    settings.MODEL, settings.NUM_CTX = model, fit_context(model, num_ctx) if fit_context else num_ctx
    store = TaskNotes(task["id"])
    notes_on = memory_settings.get("task_notes")  # off: a full context compacts or ends the run
    allowed = _tools_for(task["powers"], store if notes_on else None, areas)
    allowed["remind"] = reminders.task_tool(f"task {task['id']}: {task['name']}")
    guard = gate.Gate(policy={"areas": areas, "powers": task["powers"]}, on_flag=on_flag)
    usable_skills = skills.usable(set(task.get("skills") or ()))  # only the task's skills that are on
    prompt = memory.build_system_prompt(
        config.SYSTEM_PROMPT, config.SELF_FILE, config.USER_FILE, seeds.render_pilot(allowed, in_task=True),
        user_seeds=interaction.user_seeds_text())
    section = skills.prompt_section(usable_skills)
    conversation = memory.ConversationMemory(system_prompt=prompt + ("\n\n" + section if section else ""))
    agent = engine.AgentEngine(config=settings, tools=allowed, memory=conversation, gate=guard, on_turn=on_turn,
                               skills=usable_skills,
                               task_notes=store if notes_on else None,
                               on_full=None if notes_on else (lambda percent: memory_settings.get("task_notes_off")),
                               deadline=time.monotonic() + time_limit_seconds(),
                               checkpoints=False)
    try:
        installed = agent.driver.models()
    except Exception:
        installed = None  # the server is down; the watchdog handles that during the run
    if installed is not None and model not in installed:
        text = f"{MISSING_MODEL}: {model}"
        _record(task, "failed", text)
        mark_missing_model(task, model)  # retrying can't help; wait for a new model
        reminders.add(f"Task {task['id']} ({task['name']}) is paused: its model {model} isn't installed. "
                      f"Echo will ask {config.USER_NAME} to pick another model.", missing_model_source(task))
        return "failed", text
    memory_rule = ("You may edit short- and long-term seeds (memory vault seeds are frozen); the rest of your "
                   "memory is read-only." if "seeds" in task["powers"] else "Your memory is read-only during tasks.")
    wake = (f"[Scheduled task {task['id']}: {task['name']}]\n\n{task['body']}\n\n"
            f"The current local time is {time.strftime('%A, %Y-%m-%d %H:%M %Z')}. "
            f"You were woken by the internal clock and nobody is watching, so don't ask questions. "
            f"Work only in: {', '.join(str(a) for a in areas)}. {memory_rule} "
            f"If something is blocked, follow the reason you're given. "
            f"If {config.USER_NAME} should hear about something the next time you talk, use remind. "
            f"Finish with a short report.")
    responding = False
    if "security" in task["powers"]:
        import sentinel
        responding = bool(sentinel.incident()) and task.get("pilot")
        if responding:
            wake = (f"[INCIDENT RESPONSE: task {task['id']}: {task['name']}]\n\n"
                    "The harness's sentinel found something high-risk on this Mac and already contained what it "
                    "could. You are the response. Steps:\n"
                    "1. security_events: read the incident and what the harness did.\n"
                    "2. Investigate with your read-only tools (read files, list folders, search).\n"
                    "3. If a flagged high item is still active, security_contain it (only flagged high events).\n"
                    "4. security_rescan to confirm.\n"
                    "5. Append one line to your log file describing the incident, what was done, and what's "
                    "still active, then finish with a short report: what happened, what was contained, and "
                    f"what {config.USER_NAME} should check. The harness rescans after you and runs you again if needed.\n\n"
                    f"The current local time is {time.strftime('%A, %Y-%m-%d %H:%M %Z')}. Nobody is watching; "
                    f"don't ask questions. Work only in: {', '.join(str(a) for a in areas)}.")
    earlier = store.read()
    if earlier and not responding:
        wake += ("\n\n[Harness note: you started this task in an earlier run and stopped before finishing. "
                 "Continue from your task notes below. Don't redo finished steps or overwrite finished work; "
                 "read a file first if you need to check it.]\n\nYour task notes:\n" + earlier)

    start = os.getcwd()
    result, text = "failed", ""
    try:
        os.chdir(areas[0])
        text = agent.run(wake)
        if text == engine.CONTEXT_END_TEXT:
            result = "ended"  # not a failure; the next cycle starts fresh
        elif text == "Stopped by the user.":
            result = "stopped"
        elif text.startswith(engine.TIME_LIMIT_TEXT):
            result = "timeout"
        elif not text.startswith("Stopped") and text != "(no response from model)":
            result = "ok"
    except (engine.Interrupted, KeyboardInterrupt):
        result, text = "stopped", "Stopped by the user."
        raise
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}"
    finally:
        os.chdir(start)
        agent._clear_checkpoint()
        if result == "ok":
            store.clear()
            if "seeds" in task["powers"]:
                interaction.mark_consolidated()
        elif agent.changed and store.read():
            store.write(store.read() + "\n\nFiles changed in that run (harness record):\n"
                        + "\n".join(f"- {path}" for path in agent.changed))
        _record(task, result, text)
        if "security" in task["powers"]:
            import sentinel
            sentinel.finish_run(result == "ok")
            if responding:
                sentinel.after_response()
    return result, text
