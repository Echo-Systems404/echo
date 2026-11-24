"""Context-window sizing: how large a context each model supports, and an
estimate of the memory it needs at a given size, so /context can show which
sizes fit on this machine.

Memory estimate = model weights + KV cache (bytes per token x tokens) + a
fixed overhead. The KV cache only grows for layers that attend to the whole
context; sliding-window layers are bounded by their window and ignored.
Estimates, not measurements: they assume Ollama's default f16 KV cache.
"""

import json
import os
import subprocess
from pathlib import Path

import config

GiB = 1024 ** 3
OVERHEAD = 1.5 * GiB  # runner buffers, draft model, sliding-window caches
KV_BYTES = 2          # f16


def parse_size(text):
    """'64k' -> 65536, '1m' -> 1048576, '50000' -> 50000; None if invalid."""
    text = text.strip().lower()
    scale = {"k": 1024, "m": 1024 ** 2}.get(text[-1:], 1)
    number = text[:-1] if scale > 1 else text
    try:
        return int(float(number) * scale)
    except ValueError:
        return None


def format_size(tokens):
    if tokens % (1024 ** 2) == 0:
        return f"{tokens // 1024 ** 2}M"
    return f"{tokens // 1024}k" if tokens % 1024 == 0 else str(tokens)


def gpu_budget():
    """Memory the GPU may use: macOS's wired limit if one is set, else ~75% of RAM."""
    def sysctl(name):
        try:
            return int(subprocess.run(["sysctl", "-n", name], capture_output=True, text=True).stdout)
        except (ValueError, OSError):
            return 0
    limit_mb = sysctl("iogpu.wired_limit_mb")
    return limit_mb * 1024 ** 2 if limit_mb > 0 else int(sysctl("hw.memsize") * 0.75)


def guard_bytes():
    """The memory guard from /options, in bytes, or None when it's off."""
    try:
        gb = json.loads((config.OPTIONS_DIR / "engine.json").read_text(encoding="utf-8")).get("guard_gb")
    except (OSError, ValueError):
        return None
    return int(gb * GiB) if isinstance(gb, (int, float)) and gb > 0 else None


def budget():
    """What a model may use: the memory guard, or the GPU limit if that's lower."""
    guard = guard_bytes()
    return min(gpu_budget(), guard) if guard else gpu_budget()


def model_facts(ollama_api, model):
    """{'max_ctx', 'weights', 'kv_per_token'} for an installed model; values
    are None when Ollama doesn't say. ollama_api is AgentEngine.ollama_api."""
    show = ollama_api("/api/show", {"model": model})
    info = show.get("model_info") or {}
    arch = info.get("general.architecture", "")
    weights = next((m["size"] for m in ollama_api("/api/tags")["models"] if m["name"] == model), None)
    kv = _kv_from_gguf(info, arch) if show.get("details", {}).get("format") == "gguf" else _kv_from_config(model)
    return {"max_ctx": info.get(f"{arch}.context_length"), "weights": weights, "kv_per_token": kv}


def estimate(facts, tokens):
    """Estimated memory in bytes at this context size, or None if unknown."""
    if facts["weights"] is None or facts["kv_per_token"] is None:
        return None
    return facts["weights"] + facts["kv_per_token"] * tokens + OVERHEAD


def verdict(total, budget):
    if total is None:
        return "unknown"
    if total <= 0.9 * budget:
        return "fits"
    return "tight" if total <= budget else "too big"


STANDARD_SIZES = [k * 1024 for k in (8, 16, 32, 64, 128, 200, 256, 512, 1024)]


def options(facts):
    """Context sizes worth offering: the standard sizes up to the model's
    maximum, plus the maximum itself."""
    if not facts["max_ctx"]:
        return []
    sizes = [s for s in STANDARD_SIZES if s <= facts["max_ctx"]]
    if facts["max_ctx"] not in sizes:
        sizes.append(facts["max_ctx"])
    return sizes


def fitted(facts, size, limit=None):
    """The largest context up to size that this model can hold in memory
    (limit: bytes, default the memory guard or GPU limit)."""
    limit = limit or budget()
    if facts["max_ctx"]:
        size = min(size, facts["max_ctx"])
    if verdict(estimate(facts, size), limit) != "too big":
        return size
    fitting = [s for s in options(facts) if s < size and verdict(estimate(facts, s), limit) != "too big"]
    return fitting[-1] if fitting else size


def facts_from_gguf(info, weights):
    """Model facts from a GGUF file's metadata (gguf.metadata)."""
    arch = info.get("general.architecture", "")
    try:
        kv = _kv_from_gguf(info, arch)
    except (TypeError, KeyError, ZeroDivisionError, IndexError):
        kv = None
    return {"max_ctx": info.get(f"{arch}.context_length"), "weights": weights, "kv_per_token": kv}


def facts_from_config(model_config, weights):
    """Model facts from a Hugging Face config.json (MLX models)."""
    text = model_config.get("text_config", model_config)
    try:
        kv = kv_from_config_dict(model_config)
    except (TypeError, KeyError, ZeroDivisionError):
        kv = None
    return {"max_ctx": text.get("max_position_embeddings"), "weights": weights, "kv_per_token": kv}


# -- KV cache size per token ---------------------------------------------

def _kv_from_gguf(info, arch):
    def get(key, default=None):
        return info.get(f"{arch}.{key}", default)

    layers = get("block_count")
    if not layers:
        return None
    if get("attention.kv_lora_rank"):  # MLA (e.g. GLM): one compressed vector per layer
        # its size is the larger key length (converters disagree on which key holds it)
        return layers * max(get("attention.key_length", 0), get("attention.key_length_mla", 0)) * KV_BYTES
    heads_kv = get("attention.head_count_kv")
    heads_kv = heads_kv if isinstance(heads_kv, list) else [heads_kv] * layers
    head_dim = get("embedding_length") // get("attention.head_count")
    key_len = get("attention.key_length", head_dim)
    value_len = get("attention.value_length", head_dim)
    sliding = get("attention.sliding_window_pattern") or [False] * layers
    return sum(heads_kv[i] * (key_len + value_len) * KV_BYTES for i in range(layers) if not sliding[i])


def _kv_from_config(model):
    """Safetensors models (MLX runner): read config.json from Ollama's store."""
    stored = _stored_config(model)
    return kv_from_config_dict(stored) if stored else None


def kv_from_config_dict(model_config):
    """KV cache bytes per token from a config.json dict."""
    model_config = model_config.get("text_config", model_config)
    layers = model_config["num_hidden_layers"]
    local = set(model_config.get("local_layer_ids", []))  # sliding-window layers
    loop = model_config.get("loop_config") or {}
    caches = 0
    for i in range(layers):
        if i in local:
            continue
        looped = loop and loop["core_start"] <= i < loop["core_end"]
        caches += loop.get("num_passes", 1) if looped else 1  # each pass has its own cache
    head_dim = model_config.get("head_dim") or model_config["hidden_size"] // model_config["num_attention_heads"]
    return caches * model_config["num_key_value_heads"] * head_dim * 2 * KV_BYTES


def _stored_config(model):
    """The model's config.json, found through its manifest in Ollama's store
    (older layout: manifests/registry.ollama.ai/..., newer: manifests-v2/ollama.com/...)."""
    root = Path(os.environ.get("OLLAMA_MODELS", Path.home() / ".ollama" / "models"))
    name, _, tag = model.partition(":")
    parts = name.split("/")
    if "." not in parts[0]:  # no registry host: default registry, "library" namespace
        parts = (["library"] if len(parts) == 1 else []) + parts
        candidates = [Path("manifests-v2", "ollama.com", *parts), Path("manifests", "registry.ollama.ai", *parts)]
    else:
        candidates = [Path("manifests-v2", *parts), Path("manifests", *parts)]
    for candidate in candidates:
        try:
            manifest = json.loads((root / candidate / (tag or "latest")).read_text())
            layer = next(l for l in manifest["layers"] if l.get("name") == "config.json")
            return json.loads((root / "blobs" / layer["digest"].replace(":", "-")).read_text())
        except (OSError, StopIteration, KeyError, ValueError):
            continue
    return None

