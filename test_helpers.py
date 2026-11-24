"""Shared sandbox and simulated inference backends for Echo tests."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import engine
import inference
import interaction
import notes

HARNESS = Path(config.__file__).resolve().parent
GiB = 1024 ** 3


class Sandbox(unittest.TestCase):
    """Points every data folder at a temporary directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="echo-test-")).resolve()
        (self.tmp / "vault").mkdir()
        (self.tmp / "work").mkdir()
        patches = {
            "INTERACTION_DIR": self.tmp / "seeds" / "interaction seeds",
            "SEEDS_DIR": self.tmp / "seeds",
            "USER_SEEDS_FILE": self.tmp / "seeds" / "user.seeds.md",
            "NOTES_DIR": self.tmp / "notes",
            "VAULT_DIR": self.tmp / "vault",
            "OPTIONS_DIR": self.tmp / "options",
            "TASKS_DIR": self.tmp / "options" / "tasks",
            "PILOT_TASKS_DIR": self.tmp / "pilot_tasks",
            "COMMUNICATION_DIR": self.tmp / "communication",
            "GATE_LOG": self.tmp / "gate.jsonl",
            "CHECKPOINT_FILE": str(self.tmp / "checkpoint.json"),
        }
        self.patchers = [mock.patch.object(config, k, v) for k, v in patches.items()]
        for p in self.patchers:
            p.start()
        self.conversation = notes.conversation_id
        interaction._snapshots.clear()

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        notes.conversation_id = self.conversation
        shutil.rmtree(self.tmp, ignore_errors=True)

    def in_conversation(self, name):
        notes.conversation_id = name


class FakeModel(engine.AgentEngine):
    """Replies from a script: (tokens used after the call, reply)."""

    def __init__(self, script, **kwargs):
        super().__init__(config=config, memory=kwargs.pop("memory"), verbose=False, **kwargs)
        self.num_ctx = 1000
        self.script = list(script)
        self.sent = []

    def _chat(self, messages, label):
        self.sent.append([dict(m) for m in messages])
        tokens, reply = self.script.pop(0)
        self.last_tokens = tokens
        return {"role": "assistant", "content": "", **reply}


class FakeDriver(inference.ServerDriver):
    name = "llamacpp"
    script = None

    def model_paths(self):
        return {"tiny": self.script}

    def supports_tools(self, model):
        return True

    def _read_facts(self, model):
        return {"max_ctx": 4096, "weights": 1 * GiB, "kv_per_token": 1024}

    def command(self, path, model, num_ctx, port):
        return [sys.executable, path, port]

    def health_path(self):
        return "/health"


class ScriptedDriver(inference.Driver):
    name = "ollama"

    def __init__(self, replies, fitted=None, guard_error=None):
        super().__init__()
        self.replies, self.fitted, self.guard_error, self.released = list(replies), fitted, guard_error, 0

    def prepare(self, model, num_ctx):
        if self.guard_error:
            raise inference.GuardError(self.guard_error)
        if self.fitted and self.fitted < num_ctx:
            self.notices.append("Context lowered.")
            return self.fitted
        return num_ctx

    def chat(self, model, messages, tools, num_ctx, timeout):
        reply = self.replies.pop(0)
        yield {"message": {"content": reply.get("content", "")}, "done": False}
        yield {"message": {"tool_calls": reply.get("tool_calls", [])}, "done": True,
               "prompt_eval_count": 50, "eval_count": 5}

    def release(self):
        self.released += 1

    def healthy(self):
        return True


class FactsDriver:
    name = "ollama"

    def __init__(self, models):
        self._models = models  # name -> (weights GB, kv bytes per token, tools)

    def models(self):
        return list(self._models)

    def supports_tools(self, name):
        return self._models[name][2]

    def facts(self, name):
        weights, kv, _ = self._models[name]
        return {"max_ctx": 131072, "weights": int(weights * GiB), "kv_per_token": kv}
