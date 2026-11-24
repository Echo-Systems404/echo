"""Unit tests for the inference drivers, the lease engine, and the memory
guard. No real model: a fake OpenAI-style server and a fake Ollama stand in.
Run: python3 -B -m unittest test_engine_drivers
"""

import json
import os
import signal
import struct
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import config
import context
import engine
import gguf
import inference
import memory
import notes
import tools
from test_helpers import FakeDriver, FakeModel, GiB, Sandbox, ScriptedDriver


def sse(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


STREAM = sse(
    {"choices": [{"delta": {"reasoning_content": "Let me "}}]},
    {"choices": [{"delta": {"reasoning_content": "think."}}]},
    {"choices": [{"delta": {"content": "Writing "}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "write_file",
                                                                                 "arguments": '{"path": "a.'}}]}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'txt", "content": "hi"}'}}]}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "c2", "function": {"name": "clock",
                                                                                 "arguments": "{}"}}]}}]},
    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
     "timings": {"cache_n": 900, "prompt_n": 100, "prompt_ms": 500.0, "predicted_n": 40, "predicted_ms": 1000.0}},
    {"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 40}},
)


class FakeServer:
    """A local HTTP server: GET paths return 200, POSTs are recorded and get
    the reply body."""

    def __init__(self, reply="", content_type="text/event-stream"):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                outer.requests.append((self.path, body))
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.end_headers()
                text = outer.reply(self.path, body) if callable(outer.reply) else outer.reply
                self.wfile.write(text.encode())

        self.reply = reply
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Base(Sandbox):
    def setUp(self):
        super().setUp()
        inference._instance = None

    def tearDown(self):
        if inference._instance is not None:
            inference._instance.release()
        inference._instance = None
        super().tearDown()


# -- conversion and streaming ------------------------------------------------------

class OpenAIFormat(Base):
    def test_history_converts_with_matching_tool_ids(self):
        history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "a"}}},
                {"function": {"name": "clock", "arguments": {}}}]},
            {"role": "tool", "tool_name": "read_file", "content": "A"},
            {"role": "tool", "tool_name": "clock", "content": "noon"},
            {"role": "assistant", "content": "done"},
        ]
        out = inference.to_openai(history)
        calls = out[2]["tool_calls"]
        self.assertEqual([c["function"]["arguments"] for c in calls], ['{"path": "a"}', "{}"])
        self.assertEqual([out[3]["tool_call_id"], out[4]["tool_call_id"]], [calls[0]["id"], calls[1]["id"]])
        self.assertEqual(out[4]["name"], "clock")
        self.assertEqual(out[5], {"role": "assistant", "content": "done"})

    def test_stream_becomes_ollama_chunks(self):
        server = FakeServer(STREAM)
        try:
            chunks = list(inference.openai_stream(f"http://127.0.0.1:{server.port}/v1/chat/completions",
                                                  {"messages": []}, 10))
        finally:
            server.close()
        text = [c["message"] for c in chunks if not c["done"]]
        self.assertEqual("".join(m.get("thinking", "") for m in text), "Let me think.")
        self.assertEqual("".join(m.get("content", "") for m in text), "Writing ")
        done = chunks[-1]
        self.assertTrue(done["done"])
        self.assertEqual(done["message"]["tool_calls"], [
            {"id": "c1", "function": {"name": "write_file", "arguments": {"path": "a.txt", "content": "hi"}}},
            {"id": "c2", "function": {"name": "clock", "arguments": {}}}])
        self.assertEqual((done["prompt_eval_count"], done["prompt_processed"], done["eval_count"]), (1000, 100, 40))
        self.assertEqual(engine._speeds(done, 0, 0)["decoding"], 200.0)  # 100 new tokens in 0.5 s

    def test_cut_off_stream_is_an_error(self):
        server = FakeServer('data: {"choices": [{"delta": {"content": "half"}}]}\n\n')
        try:
            with self.assertRaises(ConnectionError):
                list(inference.openai_stream(f"http://127.0.0.1:{server.port}/x", {}, 10))
        finally:
            server.close()


# -- GGUF metadata -----------------------------------------------------------------

def write_gguf(path, pairs):
    def string(text):
        data = text.encode()
        return struct.pack("<Q", len(data)) + data

    body = b"GGUF" + struct.pack("<IQQ", 3, 0, len(pairs))
    for key, kind, value in pairs:
        body += string(key) + struct.pack("<I", kind)
        if kind == 4:
            body += struct.pack("<I", value)
        elif kind == 8:
            body += string(value)
        elif kind == 9:
            item, items = value
            body += struct.pack("<IQ", item, len(items))
            for v in items:
                body += string(v) if item == 8 else struct.pack("<I", v)
    path.write_bytes(body)


class GGUF(Base):
    def test_reads_metadata_and_facts(self):
        path = self.tmp / "m.gguf"
        write_gguf(path, [
            ("general.architecture", 8, "llama"),
            ("llama.block_count", 4, 32), ("llama.context_length", 4, 32768),
            ("llama.embedding_length", 4, 4096), ("llama.attention.head_count", 4, 32),
            ("llama.attention.head_count_kv", 4, 8),
            ("tokenizer.ggml.tokens", 9, (8, [f"t{i}" for i in range(gguf.KEEP_ARRAY_MAX + 5)])),
            ("tokenizer.chat_template", 8, "{% if tools %}...{% endif %}"),
        ])
        info = gguf.metadata(path)
        self.assertIsNone(info["tokenizer.ggml.tokens"])  # long arrays are skipped
        facts = context.facts_from_gguf(info, 4 * GiB)
        self.assertEqual(facts["max_ctx"], 32768)
        self.assertEqual(facts["kv_per_token"], 32 * 8 * (128 + 128) * 2)
        self.assertFalse(gguf.is_gguf(self.tmp / "missing"))


# -- tether --------------------------------------------------------------------------

class Tether(Base):
    def test_server_dies_with_its_owner(self):
        marker = f"echo-tether-test-{os.getpid()}-{time.time()}"
        owner = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import subprocess, sys, time, os
            subprocess.Popen([sys.executable, {str(inference.TETHER)!r}, str(os.getpid()), "--",
                              sys.executable, "-c", "import time; time.sleep(600)", {marker!r}],
                             start_new_session=True)
            time.sleep(600)
        """)])
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not self.server_pids(marker):
                time.sleep(0.2)
            self.assertTrue(self.server_pids(marker), "the server never started")
            owner.send_signal(signal.SIGKILL)  # Echo force-killed
            owner.wait()
            deadline = time.time() + 10
            while time.time() < deadline and self.server_pids(marker):
                time.sleep(0.2)
            self.assertEqual(self.server_pids(marker), [])
        finally:
            for pid in self.server_pids(marker):
                os.kill(pid, signal.SIGKILL)
            if owner.poll() is None:
                owner.kill()

    def test_killed_tether_leaves_server_reaped_by_servers(self):
        model_path = str(self.tmp / f"test-model-{os.getpid()}-{time.time()}.gguf")
        pidfile = self.tmp / "server.pid"
        cmd = [
            sys.executable, "-B", str(inference.TETHER), str(os.getpid()),
            "--pidfile", str(pidfile), "--",
            sys.executable, "-c", "import time; time.sleep(600)", model_path
        ]
        tether_proc = subprocess.Popen(cmd, start_new_session=True)
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not pidfile.exists():
                time.sleep(0.05)
            self.assertTrue(pidfile.exists(), "pidfile was not created")
            server_pid = int(pidfile.read_text().strip())
            self.assertTrue(inference._alive(server_pid), "server process is not running")

            entry = {
                "tether": tether_proc.pid,
                "owner": os.getpid(),
                "server": server_pid,
                "driver": "llamacpp",
                "model": "fake",
                "path": model_path,
                "port": 9999,
                "ctx": 2048,
            }
            inference._register(entry)

            tether_proc.send_signal(signal.SIGKILL)
            tether_proc.wait()

            self.assertTrue(inference._alive(server_pid), "server died before inference.servers()")
            inference.servers()
            self.assertFalse(inference._alive(server_pid), "server process was not killed")
        finally:
            if tether_proc.poll() is None:
                tether_proc.kill()
            if 'server_pid' in locals() and inference._alive(server_pid):
                try:
                    os.killpg(server_pid, signal.SIGKILL)
                except OSError:
                    pass

    @staticmethod
    def server_pids(marker):
        out = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout
        return [int(p) for p in out.split() if int(p) != os.getpid()]


# -- the memory guard and one model at a time ----------------------------------------

class Guard(Base):
    facts = {"max_ctx": 131072, "weights": 10 * GiB, "kv_per_token": 128 * 1024}

    def test_context_lowered_to_fit(self):
        inference.save_settings(guard_gb=16)
        size = inference.check_guard(self.facts, 65536, "m")  # 10 + 8 + 1.5 GB > 16
        self.assertLess(size, 65536)
        self.assertLessEqual(context.estimate(self.facts, size), 16 * GiB)

    def test_weights_over_the_guard_refused(self):
        inference.save_settings(guard_gb=8)
        with self.assertRaises(inference.GuardError):
            inference.check_guard(self.facts, 8192, "big")

    def test_guard_off_uses_gpu_limit(self):
        with mock.patch.object(context, "gpu_budget", return_value=64 * GiB):
            self.assertEqual(inference.check_guard(self.facts, 65536, "m"), 65536)

    def test_make_room_keeps_only_the_target(self):
        unloaded, stopped = [], []
        with mock.patch.object(inference, "_ollama_loaded", return_value=[{"name": "a"}, {"name": "b"}]), \
                mock.patch.object(inference, "_ollama_unload", unloaded.append), \
                mock.patch.object(inference, "servers", return_value=[{"tether": 11}, {"tether": 22}]), \
                mock.patch.object(inference, "_stop_server", stopped.append):
            inference.make_room("ollama", "b")
            self.assertEqual((unloaded, stopped), (["a"], [11, 22]))
            unloaded.clear(), stopped.clear()
            inference.make_room("llamacpp", "x", keep_tether=22)
            self.assertEqual((unloaded, stopped), (["a", "b"], [11]))


# -- Ollama lease ----------------------------------------------------------------------

class OllamaLease(Base):
    def setUp(self):
        super().setUp()
        self.loaded = []

        def reply(path, body):
            if path == "/api/chat":
                return json.dumps({"message": {"content": "hi"}, "done": False}) + "\n" + json.dumps(
                    {"message": {}, "done": True, "prompt_eval_count": 5, "eval_count": 1}) + "\n"
            return "{}"

        self.server = FakeServer(reply, "application/json")
        url = f"http://127.0.0.1:{self.server.port}/api/chat"
        for p in (mock.patch.object(config, "OLLAMA_URL", url),
                  mock.patch.object(inference, "_ollama_loaded", lambda: [{"name": n} for n in self.loaded])):
            p.start()
            self.patchers.append(p)

    def tearDown(self):
        super().tearDown()
        self.server.close()

    def posts(self, path):
        return [body for p, body in self.server.requests if p == path]

    def test_requests_carry_the_lease_and_release_unloads(self):
        driver = inference.OllamaDriver()
        driver._facts["m"] = {"max_ctx": None, "weights": None, "kv_per_token": None}
        size = driver.prepare("m", 8192)
        chunks = list(driver.chat("m", [], [], size, 10))
        self.assertTrue(chunks[-1]["done"])
        self.assertEqual(self.posts("/api/chat")[0]["keep_alive"], f"{config.LEASE_MINUTES}m")
        driver.renew("m")  # not loaded: never loads it
        self.assertEqual(self.posts("/api/generate"), [])
        self.loaded = ["m"]
        driver.renew("m")
        self.assertEqual(self.posts("/api/generate")[-1], {"model": "m", "keep_alive": f"{config.LEASE_MINUTES}m"})
        driver.release()
        self.assertEqual(self.posts("/api/generate")[-1], {"model": "m", "keep_alive": 0})


# -- a server driver, end to end with a fake server -----------------------------------

FAKE_SERVER = textwrap.dedent('''
    import json, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    STREAM = %r
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200); self.end_headers(); self.wfile.write(STREAM.encode())
    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
''') % STREAM


class ServerLifecycle(Base):
    def test_start_chat_release(self):
        script = self.tmp / "fake_server.py"
        script.write_text(FAKE_SERVER)
        FakeDriver.script = str(script)
        driver = FakeDriver()
        with mock.patch.object(inference, "_ollama_loaded", return_value=[]):
            size = driver.prepare("tiny", 8192)
            self.assertEqual(size, 4096)  # the model's maximum
            self.assertEqual([s["model"] for s in inference.servers()], ["tiny"])
            first = driver.tether.pid
            driver.prepare("tiny", 4096)  # same model and size: the same server
            self.assertEqual(driver.tether.pid, first)
            chunks = list(driver.chat("tiny", [{"role": "user", "content": "hi"}], [], size, 10))
            self.assertEqual(chunks[-1]["message"]["tool_calls"][1]["function"]["name"], "clock")
            driver.release()
        self.assertEqual(inference.servers(), [])
        self.assertFalse(inference._alive(first))


# -- the engine on a driver, and the idle unload ---------------------------------------

class EngineOnDriver(Base):
    def make(self, driver, **kwargs):
        return engine.AgentEngine(config=config, tools=tools.TOOLS, memory=memory.ConversationMemory("sys"),
                                  verbose=False, driver=driver, **kwargs)

    def test_runs_through_the_driver_and_applies_the_fitted_context(self):
        driver = ScriptedDriver([{"tool_calls": [{"function": {"name": "clock", "arguments": {}}}]},
                                 {"content": "It is noon."}], fitted=8192)
        agent = self.make(driver)
        self.assertEqual(agent.run("what time is it?"), "It is noon.")
        self.assertEqual(agent.num_ctx, 8192)
        self.assertEqual(agent.last_tokens, 55)

    def test_guard_refusal_stops_cleanly(self):
        agent = self.make(ScriptedDriver([], guard_error="too big for the guard"))
        self.assertEqual(agent.run("hi"), "Stopped: too big for the guard")

    def test_idle_update_saves_then_flushes(self):
        self.in_conversation("idle-test")
        conversation = memory.ConversationMemory("sys")
        conversation.add("user", "We renamed the site to Lumen.")
        conversation.add("assistant", "Noted.")
        agent = FakeModel([
            (100, {"tool_calls": [{"function": {"name": "transient_edit",
                                                "arguments": {"action": "new", "title": "Website",
                                                              "content": "Site renamed to Lumen."}}}]}),
            (120, {"content": "done"})], tools=tools.TOOLS, memory=conversation)
        flushed = []
        agent.on_flush = lambda: flushed.append(True)
        agent.idle_update(15)
        self.assertEqual(conversation.history, [])
        self.assertIn("Lumen", conversation.carry)
        self.assertEqual(flushed, [True])
        self.assertIn("Lumen", notes.transient_read())

    def test_run_command_extra_arg_never_calls_tool_or_approval(self):
        tool_called = []
        approval_called = []
        fake_tool = {
            "function": lambda **kw: tool_called.append(kw),
            "schema": tools.TOOLS["run_command"]["schema"],
            "needs_permission": tools.TOOLS["run_command"]["needs_permission"],
        }
        test_tools = dict(tools.TOOLS)
        test_tools["run_command"] = fake_tool
        agent = engine.AgentEngine(config=config, tools=test_tools, memory=memory.ConversationMemory("sys"),
                                  verbose=False)
        agent._permission_granted = lambda reason: approval_called.append(reason) or True

        result = agent._run_tool("run_command", {"cmd": "echo hi", "unexpected_extra": 123})
        self.assertTrue(result.startswith("Error: invalid arguments for 'run_command'"))
        self.assertEqual(tool_called, [])
        self.assertEqual(approval_called, [])
