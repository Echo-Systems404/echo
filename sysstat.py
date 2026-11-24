"""What Echo can see about its own machine (the system_status tool), and the
soft memory lock: which installed models can run as sub-agents right now.

Deterministic, no model involved. The lock's budget is the lower of what the
GPU may use (or the memory guard) minus what's loaded, and the memory macOS
reports as free, minus SUBAGENT_HEADROOM_GB. A model qualifies when its
weights plus a cache of at least SUBAGENT_MIN_CTX fit; it gets the largest
context up to SUBAGENT_CTX that does. If it only fits once Echo's own model is
unloaded, the run parks Echo's model and it reloads afterward.
"""

import config
import context
import inference

GiB = context.GiB
BESIDE, PARKS, SHARED = "beside", "parks Echo's model", "shared"


def _memory():
    """(used, total) bytes as macOS counts them, or None."""
    try:
        import panel
        used, total = panel.memory_gb()
        return used * GiB, total * GiB
    except Exception:
        return None


def _loaded_bytes(entry):
    if entry.get("bytes"):
        return entry["bytes"]
    try:
        facts = inference.new_driver(entry["driver"]).facts(entry["name"])
        return context.estimate(facts, entry.get("context_length") or config.NUM_CTX) or 0
    except Exception:
        return 0


def _best_ctx(facts, free):
    """The largest context from SUBAGENT_MIN_CTX up to SUBAGENT_CTX that fits in free bytes."""
    sizes = sorted({s for s in context.STANDARD_SIZES + [config.SUBAGENT_CTX]
                    if config.SUBAGENT_MIN_CTX <= s <= config.SUBAGENT_CTX}, reverse=True)
    for size in sizes:
        if facts.get("max_ctx") and size > facts["max_ctx"]:
            continue
        need = context.estimate(facts, size)
        if need is not None and need <= free:
            return size, need
    return None, None


def plan(echo_model=None, driver=None):
    """The soft lock's verdict: {"budget", "loaded", "free_beside", "free_parked",
    "models": [{"name", "mode", "ctx", "bytes", "tools"}], "skipped": [(name, why)]}."""
    driver = driver or inference.driver()
    headroom = config.SUBAGENT_HEADROOM_GB * GiB
    limit = context.budget()
    loaded = inference.loaded()
    sizes = {entry["name"]: _loaded_bytes(entry) for entry in loaded}
    in_use = sum(sizes.values())
    echo_bytes = sizes.get(echo_model, 0)
    memory = _memory()
    system_free = (memory[1] - memory[0]) if memory else limit
    free_beside = min(limit - in_use, system_free) - headroom
    free_parked = min(limit - (in_use - echo_bytes), system_free + echo_bytes) - headroom
    result = {"budget": limit, "loaded": loaded, "loaded_bytes": sizes, "memory": memory,
              "free_beside": free_beside, "free_parked": free_parked, "models": [], "skipped": []}
    try:
        installed = driver.models()
    except Exception as exc:
        result["skipped"].append(("(all)", f"can't list models: {exc}"))
        return result
    echo_entry = next((e for e in loaded if e["name"] == echo_model), None)
    for name in installed:
        try:
            tools = driver.supports_tools(name)
        except Exception:
            tools = False
        if echo_entry and name == echo_model:
            result["models"].append({"name": name, "mode": SHARED, "ctx": echo_entry.get("context_length"),
                                     "bytes": 0, "tools": tools})
            continue
        try:
            facts = driver.facts(name)
        except Exception as exc:
            result["skipped"].append((name, f"can't read its size ({type(exc).__name__})"))
            continue
        if facts.get("weights") is None or facts.get("kv_per_token") is None:
            result["skipped"].append((name, "its memory needs are unknown"))
            continue
        ctx, need = _best_ctx(facts, free_beside)
        mode = BESIDE
        if ctx is None and echo_bytes:
            ctx, need = _best_ctx(facts, free_parked)
            mode = PARKS
        if ctx is None:
            result["skipped"].append((name, f"needs {(facts['weights'] + context.OVERHEAD) / GiB:.1f} GB+, "
                                            f"too big right now"))
            continue
        result["models"].append({"name": name, "mode": mode, "ctx": ctx, "bytes": need, "tools": tools})
    result["models"].sort(key=lambda m: (m["mode"] != SHARED, m["mode"] != BESIDE, m["bytes"]))
    return result


def _gb(value):
    return f"{value / GiB:.1f} GB"


def describe_plan(found):
    lines = []
    for m in found["models"]:
        cost = "no extra memory (already loaded)" if m["mode"] == SHARED else f"{_gb(m['bytes'])}"
        ctx = context.format_size(m["ctx"]) if m["ctx"] else "?"
        lines.append(f"- {m['name']}: {m['mode']}, context {ctx}, {cost}"
                     + ("" if m["tools"] else ", can't call tools (rule work only)"))
    if found["skipped"]:
        lines.append("Don't qualify: " + "; ".join(f"{n} ({why})" for n, why in found["skipped"][:12]))
    return "\n".join(lines) or "- none"


def system_status(launcher=None):
    """Echo's view of itself and the machine, as text."""
    launcher = launcher or {}
    settings = inference.settings()
    model, num_ctx, tokens = launcher.get("model"), launcher.get("num_ctx"), launcher.get("tokens") or 0
    lines = [f"Model: {model or inference.default_model()} on {inference.LABELS[settings['driver']]}"]
    if num_ctx:
        lines.append(f"Context: {context.format_size(num_ctx)}, {tokens:,} tokens used "
                     f"({round(100 * tokens / num_ctx) if num_ctx else 0}%)")
    guard = settings["guard_gb"]
    lines.append(f"Memory guard: {f'{guard:g} GB' if guard else 'off'}; models may use up to {_gb(context.budget())}")
    found = plan(model)
    if found["memory"]:
        used, total = found["memory"]
        lines.append(f"Memory: {_gb(used)} of {_gb(total)} in use, {_gb(total - used)} free")
    loaded = [f"{e['name']} ({inference.LABELS.get(e['driver'], e['driver'])}, "
              f"{context.format_size(e['context_length']) if e.get('context_length') else '?'} context, "
              f"{_gb(found['loaded_bytes'].get(e['name'], 0))})" for e in found["loaded"]]
    lines.append("Loaded: " + (", ".join(loaded) if loaded else "nothing"))
    try:
        import panel
        temp = panel.ChipSensors().read()
        if temp is not None:
            lines.append(f"Chip: {temp:.0f}°C")
    except Exception:
        pass
    lines += ["", f"Sub-agent models (soft memory lock: {_gb(max(found['free_beside'], 0))} free beside your "
                  f"model, {_gb(max(found['free_parked'], 0))} if yours is parked; "
                  f"{config.SUBAGENT_HEADROOM_GB} GB kept for the system):",
              describe_plan(found),
              "", f"Limits: {config.SWARM_MAX_AGENTS} agents and {config.SWARM_MAX_PASSES} passes per swarm, "
                  f"{config.SUBAGENT_TIME_LIMIT}s per agent per pass. Agents run one after another on one model."]
    return "\n".join(lines)
