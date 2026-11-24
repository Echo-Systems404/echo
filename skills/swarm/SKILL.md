---
name: swarm
description: Send sub-agents or a swarm (rule checkers and model personas) over a folder, like a codebase hunt for bugs, and get their merged findings.
---

You can deploy sub-agents: helpers that look at a folder and report what they
find. They never change anything; you decide what to do with their findings.

Before you deploy, call system_status. It shows your model, your context, free
memory, and which models fit as sub-agents right now (the soft memory lock).
Plan within that: a model marked "parks Echo's model" works, but your own model
unloads during the run and reloads after (slower). "shared" means your own
model, already loaded: the cheapest choice.

Two kinds of agent:
- rule agents (no model, almost free, always the same answer): checks from this
  list: pattern (regex, files, label), unused_functions, unused_imports,
  bare_except, shell_true, eval_exec, syntax_errors, secrets, todo, and command
  (only names from the user's allow list). Use them first: they're fast and exact.
- persona agents (a model): your instructions give each one a personality and
  a focus, e.g. "a paranoid security reviewer who distrusts every input". Give
  each persona a different angle so they don't all find the same things.

Ways to deploy:
- list_subagents: the user's own agents and swarms. If the user names one, use it with
  run_swarm. You can use the user's library but never change it.
- run_echo_swarm: your own swarm or a single sub-agent, built for this job. It
  exists only for this run. Give agents (each with name, kind, and checks or
  instructions, and optionally count and model), passes (1 is usually enough;
  more passes dig deeper and stop early when a pass finds nothing new), and the
  target folder.

Limits: at most 25 agents in all (counting copies) and 5 passes. Persona agents
run one after another, so 25 personas take a while: say roughly how long you
expect before a large run. The user can press Esc to stop a swarm.

Findings come back merged: each shows how many agents reported it. Many agents
agreeing is a strong signal; a lone finding still deserves a look. Check the
important ones yourself (read the code) before you present them as facts.
