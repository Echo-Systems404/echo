"""Inference drivers: Ollama, llama.cpp, or MLX, picked in /options.

Every model call in Echo goes through the current driver, which also answers
what's installed, what a model needs in memory, and what's loaded. Rules
every driver follows (the lease engine):

- One model at a time. Before a model loads, everything else Echo can see is
  unloaded: Ollama's loaded models and any server an Echo process started.
  The one exception is a sub-agent model the soft memory lock says fits
  beside Echo's (beside() keeps Echo's model loaded while it runs).
- The memory guard (/options): a model loads only if its weights plus its
  conversation cache fit; the context is lowered until they do.
- Alive only while the harness is. Ollama gets a short keep_alive lease that
  chat renews while it's in use; llama.cpp and MLX servers run under
  tether.py, which kills them as soon as the Echo process that started them
  is gone. Nothing restarts a model on its own.

Drivers speak Echo's (Ollama-style) message format. llama.cpp and MLX use the
OpenAI chat format, so history and tool calls are converted both ways, and
their streams are turned back into Ollama-style chunks.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import config
import context
import gguf

DRIVERS = ("ollama", "llamacpp", "mlx")
LABELS = {"ollama": "Ollama", "llamacpp": "llama.cpp", "mlx": "MLX"}
OLLAMA_BUNDLED_SERVER = "/Applications/Ollama.app/Contents/Resources/llama-server"
TETHER = Path(__file__).resolve().with_name("tether.py")


class GuardError(RuntimeError):
    """A model that can't load under the memory guard."""


# -- settings (options/engine.json) -------------------------------------------

def settings():
    try:
        saved = json.loads((config.OPTIONS_DIR / "engine.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    driver_name = saved.get("driver") if saved.get("driver") in DRIVERS else "ollama"
    guard = saved.get("guard_gb")
    return {"driver": driver_name, "guard_gb": guard if isinstance(guard, (int, float)) and guard > 0 else None,
            "models": saved.get("models") if isinstance(saved.get("models"), dict) else {}}


def save_settings(**changes):
    data = {**settings(), **changes}
    path = config.OPTIONS_DIR / "engine.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("engine.json.tmp")
    temporary.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def default_model():
    """The model for the current driver: the one picked for it, else config.MODEL."""
    current = settings()
    return current["models"].get(current["driver"]) or config.MODEL


_instance = None


def driver():
    """This process's driver for the current setting."""
    global _instance
    name = settings()["driver"]
    if _instance is None or _instance.name != name:
        if _instance is not None:
            _instance.release()
        _instance = {"ollama": OllamaDriver, "llamacpp": LlamaCppDriver, "mlx": MLXDriver}[name]()
    return _instance


def new_driver(name=None):
    """A separate driver (its own server, its own loaded model), for a sub-agent."""
    name = name or settings()["driver"]
    return {"ollama": OllamaDriver, "llamacpp": LlamaCppDriver, "mlx": MLXDriver}[name]()


def switch(name):
    """Change the driver: release the current model first."""
    if _instance is not None:
        _instance.release()
    save_settings(driver=name)
    return driver()


# -- helpers --------------------------------------------------------------------

def _http(url, body=None, timeout=10):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def ollama_api(path, body=None):
    """An Ollama API call (GET, or POST with body), e.g. ollama_api("/api/tags")."""
    return _http(urllib.parse.urljoin(config.OLLAMA_URL, path), body)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# -- servers Echo started (any process) ---------------------------------------

def _servers_dir():
    return config.OPTIONS_DIR / "engine"


def _kill_server_group(pid, timeout=5.0):
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if not _alive(pid):
            return
        time.sleep(0.05)


def servers():
    """Live servers started by any Echo process: dicts with tether, port,
    driver, model, ctx, owner. Dead entries are removed."""
    found = []
    for path in _servers_dir().glob("*.json"):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if _alive(entry.get("tether", 0)) and _alive(entry.get("owner", 0)):
            found.append(entry)
        else:
            server_pid = entry.get("server")
            if server_pid and _alive(server_pid):
                model_path = str(entry.get("path") or "")  # recorded at load; never kill without it
                if model_path:
                    try:
                        p = subprocess.run(["ps", "-o", "command=", "-p", str(server_pid)],
                                           capture_output=True, text=True)
                        if p.returncode == 0 and model_path in p.stdout:
                            _kill_server_group(server_pid)
                    except OSError:
                        pass
            path.unlink(missing_ok=True)
    return found


def _register(entry):
    _servers_dir().mkdir(parents=True, exist_ok=True)
    (_servers_dir() / f"{entry['tether']}.json").write_text(json.dumps(entry), encoding="utf-8")


def _stop_server(tether_pid, wait=10):
    try:
        os.kill(tether_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + wait
    while _alive(tether_pid) and time.monotonic() < deadline:
        try:
            os.waitpid(tether_pid, os.WNOHANG)  # reap it if it's our child
        except ChildProcessError:
            pass
        time.sleep(0.1)
    (_servers_dir() / f"{tether_pid}.json").unlink(missing_ok=True)


def _ollama_loaded():
    try:
        return ollama_api("/api/ps").get("models", [])
    except Exception:
        return []  # Ollama isn't running: nothing loaded there


def _ollama_unload(name):
    try:
        _http(urllib.parse.urljoin(config.OLLAMA_URL, "/api/generate"), {"model": name, "keep_alive": 0}, timeout=30)
    except Exception:
        pass


_kept = {"ollama": set(), "tethers": set()}  # models beside() keeps loaded


def make_room(keep_driver, keep_model, keep_tether=None):
    """One model at a time: unload every model Echo can see except this one
    (and a model beside() is keeping for a sub-agent run)."""
    for model in _ollama_loaded():
        if not (keep_driver == "ollama" and model.get("name") == keep_model) and model.get("name") not in _kept["ollama"]:
            _ollama_unload(model.get("name"))
    for entry in servers():
        if entry["tether"] != keep_tether and entry["tether"] not in _kept["tethers"]:
            _stop_server(entry["tether"])


class beside:
    """with beside(echo_driver): a sub-agent's model loads next to Echo's
    instead of replacing it. Only for what the soft memory lock approved."""

    def __init__(self, keep):
        self.names = set(getattr(keep, "used", set())) & {m.get("name") for m in _ollama_loaded()}
        tether = getattr(keep, "tether", None)
        self.tethers = {tether.pid} if tether is not None else set()

    def __enter__(self):
        _kept["ollama"] |= self.names
        _kept["tethers"] |= self.tethers
        return self

    def __exit__(self, *_):
        _kept["ollama"] -= self.names
        _kept["tethers"] -= self.tethers


def loaded():
    """What's loaded right now, for /status and the panel: [{"name", "driver",
    "context_length", "bytes"}]. bytes: Ollama's own count, or None."""
    items = [{"name": m.get("name"), "driver": "ollama", "context_length": m.get("context_length"),
              "bytes": m.get("size")} for m in _ollama_loaded()]
    items += [{"name": e["model"], "driver": e["driver"], "context_length": e.get("ctx"), "bytes": None}
              for e in servers()]
    return items


def check_guard(facts, num_ctx, model):
    """The context to use under the memory guard. Raises GuardError when even
    the weights don't fit."""
    limit = context.budget()
    if facts.get("weights") and facts["weights"] + context.OVERHEAD > limit:
        raise GuardError(f"{model} needs about {(facts['weights'] + context.OVERHEAD) / context.GiB:.1f} GB "
                         f"just to load, over the {limit / context.GiB:.0f} GB memory guard. Pick a smaller model, "
                         "or raise the guard in /options.")
    if facts.get("weights") is None or facts.get("kv_per_token") is None:
        return num_ctx
    return context.fitted(facts, num_ctx, limit)


# -- OpenAI format conversion -------------------------------------------------

def to_openai(messages):
    """Echo's history (Ollama style) as OpenAI chat messages. Tool results are
    matched to the calls before them, in order."""
    out, pending = [], []
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            calls = []
            for number, call in enumerate(message["tool_calls"]):
                function = call.get("function", {})
                arguments = function.get("arguments")
                calls.append({"id": call.get("id") or f"call_{index}_{number}", "type": "function",
                              "function": {"name": function.get("name", ""),
                                           "arguments": arguments if isinstance(arguments, str)
                                           else json.dumps(arguments or {})}})
            pending = [(c["id"], c["function"]["name"]) for c in calls]
            out.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
        elif role == "tool":
            call_id, name = pending.pop(0) if pending else (f"call_{index}", message.get("tool_name", ""))
            out.append({"role": "tool", "tool_call_id": call_id, "name": message.get("tool_name") or name,
                        "content": message.get("content") or ""})
        else:
            out.append({"role": role, "content": message.get("content") or ""})
    return out


def _arguments(text):
    try:
        value = json.loads(text) if text.strip() else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def openai_stream(url, payload, timeout):
    """POST an OpenAI chat request and yield Ollama-style chunks: messages
    with thinking/content as they stream, then a final done chunk with the
    tool calls and token counts."""
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
    started = time.monotonic()
    first = None
    calls, usage, timings = {}, None, None
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"server error {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}") from None
    with response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("error"):
                error = chunk["error"]
                raise RuntimeError(error.get("message") if isinstance(error, dict) else str(error))
            usage = chunk.get("usage") or usage
            timings = chunk.get("timings") or timings
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                message = {}
                thinking = delta.get("reasoning_content") or delta.get("reasoning")
                if thinking:
                    message["thinking"] = thinking
                if delta.get("content"):
                    message["content"] = delta["content"]
                for call in delta.get("tool_calls") or []:
                    slot = calls.setdefault(call.get("index", len(calls)), {"id": None, "name": "", "arguments": ""})
                    function = call.get("function") or {}
                    slot["id"] = call.get("id") or slot["id"]
                    slot["name"] += function.get("name") or ""
                    slot["arguments"] += function.get("arguments") or ""
                if (message or delta.get("tool_calls")) and first is None:
                    first = time.monotonic()
                if message:
                    yield {"message": message, "done": False}
        else:
            raise ConnectionError("stream ended before the response was complete")
    ended = time.monotonic()
    first = first or ended
    done = {"done": True, "message": {}}
    if calls:
        done["message"]["tool_calls"] = [
            {"id": slot["id"], "function": {"name": slot["name"], "arguments": _arguments(slot["arguments"])}}
            for _, slot in sorted(calls.items())]
    timings = timings or {}
    processed = timings.get("prompt_n")
    prompt_total = (usage or {}).get("prompt_tokens")
    if prompt_total is None and processed is not None:
        prompt_total = processed + (timings.get("cache_n") or 0)
    done["prompt_eval_count"] = prompt_total or 0
    done["prompt_processed"] = processed if processed is not None else (prompt_total or 0)
    done["eval_count"] = (usage or {}).get("completion_tokens") or timings.get("predicted_n") or 0
    done["prompt_eval_duration"] = int((timings["prompt_ms"] / 1000 if "prompt_ms" in timings
                                        else first - started) * 1e9)
    done["eval_duration"] = int((timings["predicted_ms"] / 1000 if "predicted_ms" in timings
                                 else ended - first) * 1e9)
    yield done


# -- drivers -------------------------------------------------------------------

class Driver:
    name = ""

    def __init__(self):
        self._facts = {}
        self.notices = []  # things to tell the user once (a lowered context)
        self.temperature = None  # None = the model's default; sub-agents set it

    @property
    def label(self):
        return LABELS[self.name]

    def facts(self, model):
        if model not in self._facts:
            self._facts[model] = self._read_facts(model)
        return self._facts[model]

    def prepare(self, model, num_ctx):
        """Before a model call: apply the guard, make room, load. Returns the context to use."""
        fitted = check_guard(self.facts(model), num_ctx, model)
        if fitted < num_ctx:
            self.notices.append(f"Context lowered to {context.format_size(fitted)} to stay under the memory guard.")
        self._load(model, fitted)
        return fitted

    def take_notices(self):
        notices, self.notices = self.notices, []
        return notices

    def tool_models(self):
        return [m for m in self.models() if self.supports_tools(m)]

    # the rest is per driver
    def available(self):
        """(True/False, note)."""
        raise NotImplementedError

    def models(self):
        raise NotImplementedError

    def supports_tools(self, model):
        raise NotImplementedError

    def _read_facts(self, model):
        raise NotImplementedError

    def _load(self, model, num_ctx):
        raise NotImplementedError

    def chat(self, model, messages, tools, num_ctx, timeout):
        raise NotImplementedError

    def renew(self, model):
        """Lease heartbeat while chat is in use."""

    def release(self):
        """Unload what this process loaded."""
        raise NotImplementedError

    def healthy(self):
        raise NotImplementedError

    def restart(self):
        """After a failed call: bring the server back (wait for it)."""
        raise NotImplementedError


class OllamaDriver(Driver):
    name = "ollama"

    def __init__(self):
        super().__init__()
        self.used = set()

    def available(self):
        if self.healthy():
            return True, "running"
        return (True, "installed, not running") if Path("/Applications/Ollama.app").exists() else (False, "not found")

    def models(self):
        return [m["name"] for m in ollama_api("/api/tags")["models"]]

    def supports_tools(self, model):
        try:
            return "tools" in (ollama_api("/api/show", {"model": model}).get("capabilities") or [])
        except Exception:
            return False

    def _read_facts(self, model):
        return context.model_facts(ollama_api, model)

    def _load(self, model, num_ctx):
        make_room("ollama", model)  # Ollama loads it with the request itself
        self.used.add(model)

    def chat(self, model, messages, tools, num_ctx, timeout):
        options = {"num_ctx": num_ctx}
        if self.temperature is not None:
            options["temperature"] = self.temperature
        payload = {"model": model, "messages": messages, "tools": tools, "options": options,
                   "stream": True, "keep_alive": f"{config.LEASE_MINUTES}m"}
        request = urllib.request.Request(config.OLLAMA_URL, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for line in response:
                chunk = json.loads(line)
                if "error" in chunk:
                    raise RuntimeError(chunk["error"])
                yield chunk
                if chunk.get("done"):
                    return
        raise ConnectionError("stream ended before the response was complete")

    def renew(self, model):
        if any(m.get("name") == model for m in _ollama_loaded()):  # never loads it, only extends
            try:
                _http(urllib.parse.urljoin(config.OLLAMA_URL, "/api/generate"),
                      {"model": model, "keep_alive": f"{config.LEASE_MINUTES}m"}, timeout=30)
            except Exception:
                pass

    def release(self):
        loaded_now = {m.get("name") for m in _ollama_loaded()}
        for model in self.used & loaded_now:
            _ollama_unload(model)
        self.used.clear()

    def healthy(self):
        try:
            ollama_api("/api/version")
            return True
        except Exception:
            return False

    def restart(self):
        if config.OLLAMA_RESTART_CMD:
            subprocess.run(config.OLLAMA_RESTART_CMD, shell=True, stdin=subprocess.DEVNULL, capture_output=True)
        deadline = time.monotonic() + config.OLLAMA_RESTART_WAIT
        while time.monotonic() < deadline:
            if self.healthy():
                return True
            time.sleep(2)
        return False


class ServerDriver(Driver):
    """A model server Echo starts itself, one model per server, under the tether."""

    def __init__(self):
        super().__init__()
        self.tether = None  # Popen
        self.port = None
        self.loaded = None  # (model, ctx)
        self._paths = None

    # per driver
    def command(self, path, model, num_ctx, port):
        raise NotImplementedError

    def health_path(self):
        raise NotImplementedError

    def model_paths(self):
        """{name: path} of the models this driver can run."""
        raise NotImplementedError

    def request_model(self, model):
        return model

    def models(self):
        self._paths = self.model_paths()
        return sorted(self._paths)

    def path(self, model):
        if self._paths is None or model not in self._paths:
            self._paths = self.model_paths()
        if model not in self._paths:
            raise RuntimeError(f"Model not installed for {self.label}: {model}")
        return self._paths[model]

    def _running(self):
        return self.tether is not None and self.tether.poll() is None

    def _load(self, model, num_ctx):
        if self._running() and self.loaded == (model, num_ctx):
            make_room(self.name, model, keep_tether=self.tether.pid)  # someone else may have loaded one since
            return
        self.release()
        make_room(self.name, model)
        path = self.path(model)
        self.port = _free_port()
        log_path = config.OPTIONS_DIR / "logs" / f"{self.name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        engine_dir = config.OPTIONS_DIR / "engine"
        engine_dir.mkdir(parents=True, exist_ok=True)
        pidfile = engine_dir / f"server-{os.getpid()}-{self.port}.pid"
        pidfile.unlink(missing_ok=True)
        with open(log_path, "w") as log:
            self.tether = subprocess.Popen(
                [sys.executable, "-B", str(TETHER), str(os.getpid()), "--pidfile", str(pidfile), "--"]
                + [str(part) for part in self.command(path, model, num_ctx, self.port)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        entry = {"tether": self.tether.pid, "owner": os.getpid(), "port": self.port, "driver": self.name,
                 "model": model, "ctx": num_ctx, "path": str(path)}
        deadline_pid = time.monotonic() + 5.0
        while time.monotonic() < deadline_pid:
            if pidfile.exists():
                try:
                    entry["server"] = int(pidfile.read_text(encoding="utf-8").strip())
                    pidfile.unlink(missing_ok=True)
                    break
                except (ValueError, OSError):
                    pass
            if not self._running():
                break
            time.sleep(0.05)
        _register(entry)
        self.loaded = (model, num_ctx)
        deadline = time.monotonic() + config.SERVER_LOAD_TIMEOUT
        while time.monotonic() < deadline:
            if not self._running():
                tail = log_path.read_text(errors="replace")[-600:].strip()
                self.release()
                raise RuntimeError(f"{self.label} server stopped while loading {model}: {tail}")
            if "server" not in entry and pidfile.exists():
                try:
                    entry["server"] = int(pidfile.read_text(encoding="utf-8").strip())
                    _register(entry)
                    pidfile.unlink(missing_ok=True)
                except (ValueError, OSError):
                    pass
            if self.healthy():
                return
            time.sleep(0.5)
        self.release()
        raise RuntimeError(f"{self.label} server didn't get ready within {config.SERVER_LOAD_TIMEOUT}s")

    def chat(self, model, messages, tools, num_ctx, timeout):
        payload = {"model": self.request_model(model), "messages": to_openai(messages), "stream": True,
                   "stream_options": {"include_usage": True}, "max_tokens": config.MAX_OUTPUT_TOKENS}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if tools:
            payload["tools"] = tools
        return openai_stream(f"http://127.0.0.1:{self.port}/v1/chat/completions", payload, timeout)

    def release(self):
        if self.tether is not None:
            _stop_server(self.tether.pid)
            try:
                self.tether.wait(5)
            except subprocess.TimeoutExpired:
                pass
        self.tether, self.loaded = None, None

    def healthy(self):
        if not self._running():
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{self.health_path()}", timeout=5):
                return True
        except Exception:
            return False

    def restart(self):
        if self.loaded is None:
            return False
        model, num_ctx = self.loaded
        self.release()
        try:
            self._load(model, num_ctx)
        except RuntimeError:
            return False
        return True


def _ollama_store():
    return Path(os.environ.get("OLLAMA_MODELS", Path.home() / ".ollama" / "models"))


def ollama_gguf_models():
    """{ollama name: GGUF path} from Ollama's model store."""
    root, found = _ollama_store(), {}
    for top in ("manifests", "manifests-v2"):
        base = root / top
        for manifest in base.glob("*/*/*/*") if base.exists() else []:
            if not manifest.is_file():
                continue
            host, namespace, name, tag = manifest.relative_to(base).parts
            if namespace == "library" and host in ("registry.ollama.ai", "ollama.com"):
                full = f"{name}:{tag}"
            elif host in ("registry.ollama.ai", "ollama.com"):
                full = f"{namespace}/{name}:{tag}"
            else:
                full = f"{host}/{namespace}/{name}:{tag}"
            try:
                layers = json.loads(manifest.read_text())["layers"]
                layer = next(l for l in layers if l.get("mediaType", "").endswith("image.model"))
            except (OSError, ValueError, KeyError, StopIteration):
                continue
            blob = root / "blobs" / layer["digest"].replace(":", "-")
            if gguf.is_gguf(blob):
                found[full] = blob
    return found


class LlamaCppDriver(ServerDriver):
    name = "llamacpp"

    @staticmethod
    def binary():
        for candidate in (config.LLAMACPP_SERVER, shutil.which("llama-server"), OLLAMA_BUNDLED_SERVER):
            if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return candidate
        return None

    def available(self):
        found = self.binary()
        if not found:
            return False, "llama-server not found (brew install llama.cpp)"
        return True, "the one inside Ollama.app" if found == OLLAMA_BUNDLED_SERVER else found

    def model_paths(self):
        found = ollama_gguf_models()
        folder = Path(config.LLAMACPP_MODELS_DIR).expanduser() if config.LLAMACPP_MODELS_DIR else None
        if folder and folder.is_dir():
            for path in sorted(folder.rglob("*.gguf")):
                if gguf.is_gguf(path) and "mmproj" not in path.name:
                    found[path.stem] = path
        return found

    def _metadata(self, model):
        return gguf.metadata(self.path(model))

    def supports_tools(self, model):
        try:
            return "tool" in (self._metadata(model).get("tokenizer.chat_template") or "")
        except (OSError, ValueError, RuntimeError):
            return False

    def _read_facts(self, model):
        return context.facts_from_gguf(self._metadata(model), Path(self.path(model)).stat().st_size)

    def command(self, path, model, num_ctx, port):
        command = [self.binary(), "--model", path, "--host", "127.0.0.1", "--port", port, "--ctx-size", num_ctx,
                   "--parallel", "1", "--n-gpu-layers", "999", "--jinja", "--no-webui", "--flash-attn", "auto"]
        if config.LLAMACPP_KV_CACHE_TYPE:
            command += ["--cache-type-k", config.LLAMACPP_KV_CACHE_TYPE,
                        "--cache-type-v", config.LLAMACPP_KV_CACHE_TYPE]
        return command

    def health_path(self):
        return "/health"


_mlx_python = {}


class MLXDriver(ServerDriver):
    name = "mlx"

    @staticmethod
    def python():
        """A Python that can import mlx_lm."""
        candidates = [config.MLX_PYTHON, shutil.which("python3")]
        for candidate in candidates:
            if not candidate or not Path(candidate).exists():
                continue
            if candidate not in _mlx_python:
                try:
                    _mlx_python[candidate] = subprocess.run(
                        [candidate, "-c", "import mlx_lm"], capture_output=True, timeout=60).returncode == 0
                except (OSError, subprocess.SubprocessError):
                    _mlx_python[candidate] = False
            if _mlx_python[candidate]:
                return candidate
        return None

    def available(self):
        found = self.python()
        if not found:
            return False, "no Python with mlx_lm (set MLX_PYTHON)"
        count = len(self.model_paths())
        return True, f"{count} MLX model{'s' if count != 1 else ''} found" if count else "no MLX models downloaded"

    @staticmethod
    def _complete(folder):
        """A model folder with its config and every weight file present."""
        if not (folder / "config.json").exists():
            return False
        weights = list(folder.glob("*.safetensors"))
        return bool(weights) and all(w.exists() for w in weights)  # broken links = deleted weights

    def model_paths(self):
        found = {}
        hub = Path(os.environ.get("HF_HUB_CACHE", Path.home() / ".cache" / "huggingface" / "hub"))
        for repo in hub.glob("models--*") if hub.exists() else []:
            for snapshot in sorted((repo / "snapshots").glob("*")):
                if self._complete(snapshot):
                    found[repo.name[len("models--"):].replace("--", "/")] = snapshot
        folder = Path(config.MLX_MODELS_DIR).expanduser() if config.MLX_MODELS_DIR else None
        if folder and folder.is_dir():
            for child in sorted(folder.iterdir()):
                if child.is_dir() and self._complete(child):
                    found[child.name] = child
        return found

    def request_model(self, model):
        return str(self.path(model))  # the same path the server loaded, so it never loads another

    def _template(self, model):
        folder = Path(self.path(model))
        text = ""
        try:
            text = json.loads((folder / "tokenizer_config.json").read_text()).get("chat_template") or ""
        except (OSError, ValueError):
            pass
        jinja = folder / "chat_template.jinja"
        if jinja.exists():
            text += jinja.read_text(errors="replace")
        return text if isinstance(text, str) else json.dumps(text)

    def supports_tools(self, model):
        try:
            return "tool" in self._template(model)
        except RuntimeError:
            return False

    def _read_facts(self, model):
        folder = Path(self.path(model))
        weights = sum(w.stat().st_size for w in folder.glob("*.safetensors"))
        return context.facts_from_config(json.loads((folder / "config.json").read_text()), weights)

    def command(self, path, model, num_ctx, port):
        facts = self.facts(model)
        room = context.budget() - (facts.get("weights") or 0) - context.OVERHEAD
        if facts.get("kv_per_token"):
            room = min(room, facts["kv_per_token"] * num_ctx)
        cache_bytes = max(int(room), 256 * 1024 ** 2)
        return [self.python(), "-m", "mlx_lm.server", "--model", path, "--host", "127.0.0.1", "--port", port,
                "--prompt-cache-size", "1", "--prompt-cache-bytes", cache_bytes,
                "--max-tokens", config.MAX_OUTPUT_TOKENS]

    def health_path(self):
        return "/v1/models"
