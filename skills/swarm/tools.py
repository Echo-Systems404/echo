"""The swarm skill's tools: deploy sub-agents and swarms (see subagents.py)."""

import config
import subagents
import tools


def _progress(launcher):
    activity = (launcher or {}).get("activity")

    def say(text):
        if activity is not None:
            activity.status(f"Swarm: {text}...")
            activity.line(f"\033[90m  swarm · {text}\033[0m")
    return say


def list_subagents():
    """The user's agents and swarms."""
    lines = ["Agents:"]
    for agent in subagents.agents():
        detail = (", ".join(c["check"] for c in agent.get("checks", [])) if agent["kind"] == "rules"
                  else (agent.get("model") or "default model"))
        lines.append(f"- {agent['name']} ({agent['kind']}: {detail}) {agent.get('description', '')}".rstrip())
    if len(lines) == 1:
        lines.append("- none yet")
    lines.append("Swarms:")
    found = subagents.swarms()
    for swarm in found:
        members = ", ".join(f"{m['agent']}" + (f" x{m['count']}" if m["count"] > 1 else "") for m in swarm["members"])
        lines.append(f"- {swarm['name']}: {members}; {swarm['passes']} pass(es) {swarm.get('description', '')}".rstrip())
    if not found:
        lines.append("- none yet")
    commands = subagents.allowed_commands()
    lines.append("Allowed commands for rule agents: " + (", ".join(commands) if commands else "none"))
    return "\n".join(lines)


def _commands_ok(launcher):
    powers = (launcher or {}).get("powers")
    return powers is None or "shell" in powers  # a task without shell can't run commands through agents


def run_swarm(name, target, launcher=None):
    """Run one of the user's swarms, or a single agent from the library."""
    swarm = subagents.load_swarm(name)
    try:
        if swarm is not None:
            members = subagents.library_members(swarm)
        else:
            agent = subagents.load_agent(name)
            if agent is None:
                return f"Error: no agent or swarm named '{name}'. Call list_subagents to see them."
            swarm, members = {"name": name, "passes": 1, "stop_early": True}, [(agent, 1)]
        if not _commands_ok(launcher) and any(c["check"] == "command" for a, _ in members
                                              for c in a.get("checks", [])):
            return "Error: this task can't run commands, and that swarm uses one."
        summary, _ = subagents.run(swarm, members, target, launcher, _progress(launcher))
    except ValueError as exc:
        return f"Error: {exc}"
    return summary


def run_echo_swarm(target, agents, passes=1, stop_early=True, launcher=None):
    """Run a swarm (or one sub-agent) Echo designed for this job."""
    try:
        if not isinstance(agents, list) or not agents:
            raise ValueError("give at least one agent")
        members = []
        for spec in agents:
            count = int((spec or {}).get("count") or 1)
            agent = subagents.validate_agent(spec, commands_ok=_commands_ok(launcher))
            members.append((agent, 1 if agent["kind"] == "rules" else max(1, count)))
        names = [a["name"] for a, _ in members]
        if len(set(names)) != len(names):
            raise ValueError("each agent needs its own name")
        passes = int(passes or 1)
        if not 1 <= passes <= config.SWARM_MAX_PASSES:
            raise ValueError(f"passes must be 1 to {config.SWARM_MAX_PASSES}")
        swarm = {"name": "", "passes": passes, "stop_early": bool(stop_early)}
        summary, _ = subagents.run(swarm, members, target, launcher, _progress(launcher))
    except (ValueError, TypeError) as exc:
        return f"Error: {exc}"
    return summary


_AGENT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Short name, lowercase-with-dashes."},
        "kind": {"type": "string", "enum": ["rules", "persona"]},
        "checks": {"type": "array", "description": "Rule agents: check names, or objects like "
                   "{\"check\": \"pattern\", \"regex\": \"...\", \"files\": \"*.py\", \"label\": \"...\"} "
                   "or {\"check\": \"command\", \"name\": \"tests\"}.",
                   "items": {}},
        "instructions": {"type": "string", "description": "Persona agents: personality and focus."},
        "count": {"type": "integer", "description": "Persona agents: copies (default 1)."},
        "model": {"type": "string", "description": "Persona agents: a model from system_status (optional)."},
    },
    "required": ["name", "kind"],
}

TOOLS = {
    "system_status": {**tools.TOOLS["system_status"], "on_demand": False},
    "list_subagents": {
        "function": list_subagents,
        "schema": tools._schema("list_subagents", f"{config.USER_NAME}'s agents and swarms, and the commands rule agents may run.",
                                {}, []),
    },
    "run_swarm": {
        "launcher": True,
        "function": run_swarm,
        "schema": tools._schema(
            "run_swarm", f"Run one of {config.USER_NAME}'s swarms (or a single agent from the library) over a folder.",
            {"name": {"type": "string", "description": "Swarm or agent name from list_subagents."},
             "target": {"type": "string", "description": "Folder (or file) to look at, absolute path."}},
            ["name", "target"]),
    },
    "run_echo_swarm": {
        "launcher": True,
        "function": run_echo_swarm,
        "schema": tools._schema(
            "run_echo_swarm", "Run your own swarm, or one sub-agent, built for this job. It exists only for "
                              "this run.",
            {"target": {"type": "string", "description": "Folder (or file) to look at, absolute path."},
             "agents": {"type": "array", "items": _AGENT_SCHEMA, "description": "The agents."},
             "passes": {"type": "integer", "description": "1 to 5 (default 1)."},
             "stop_early": {"type": "boolean", "description": "Stop when a pass finds nothing new (default true)."}},
            ["target", "agents"]),
    },
}
