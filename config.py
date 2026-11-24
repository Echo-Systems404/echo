"""Runtime settings for the agent harness (local inference backend)."""

from pathlib import Path

AGENT_NAME = "Echo"  # shown in the banner
USER_NAME = "the user"  # who Echo talks to; set your name in local_settings.py
STATUS_PANEL = True  # open the status window (panel.py) when Echo starts

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen3:8b"

# Default context window in tokens, sent to the model server with every request
# (changing it reloads the model once); /context shows what fits for the
# current model and changes it for the session. Bigger windows remember more
# but make turns slower once the conversation fills them, since each turn
# re-reads the whole conversation. Written files are kept in the history
# until it reaches about half the window.
NUM_CTX = 65536

# Hard ceiling for the context window: /context refuses anything above it.
CONTEXT_LOCK = 65536

# --- Inference engine (inference.py) ---
# The driver (Ollama, llama.cpp, or MLX) and the memory guard are picked in
# /options and saved in options/engine.json. These are the defaults around them.
LEASE_MINUTES = 5  # Ollama keeps a model this long after a request; chat renews it while open
CHAT_IDLE_MINUTES = 15  # idle chat: memory layer update, context flush, model unloaded
SERVER_LOAD_TIMEOUT = 300  # seconds a llama.cpp or MLX server may take to load a model
MAX_OUTPUT_TOKENS = 8192  # per reply, for llama.cpp and MLX
LLAMACPP_SERVER = None  # path to llama-server; None = Homebrew's, else the one inside Ollama.app
LLAMACPP_MODELS_DIR = None  # extra folder of .gguf files (Ollama's store is always used)
LLAMACPP_KV_CACHE_TYPE = None  # e.g. "q8_0" halves the conversation cache; None = f16
MLX_PYTHON = None  # a Python with mlx_lm; None = found automatically
MLX_MODELS_DIR = None  # extra folder of MLX models (the Hugging Face cache is always used)

# Max model calls per user request.
MAX_TURNS = 30

# Seconds the HTTP connection may sit idle before giving up. The inference driver
# sends a tool call only once its arguments are complete, so while the model writes a
# large file nothing arrives on the wire. Keep this generous for local models.
REQUEST_TIMEOUT = 900

# Tool output longer than this is truncated (head + tail) before it goes back
# into the context, so one noisy command can't flood the context window.
MAX_TOOL_OUTPUT_CHARS = 8000

# Conversation history is compacted to this many messages between requests.
MAX_HISTORY_MESSAGES = 20

# --- Watchdog ---
# How many times a failed or stalled model call is retried (same turn, same
# messages) before the run stops. The progress stays saved for /resume.
MAX_RETRIES = 2
# Shell command that (re)starts the Ollama server if it stops responding.
# Set to None to only wait for it instead.
OLLAMA_RESTART_CMD = "open -a Ollama"
# Seconds to wait for Ollama to come back after a restart.
OLLAMA_RESTART_WAIT = 90
# Progress for the current task is saved here (in the working directory) at
# the start of every turn and deleted when the task finishes.
CHECKPOINT_FILE = ".agent_checkpoint.json"

SYSTEM_PROMPT = (
    "# How you work\n"
    "You work as an autonomous coding agent in the user's current directory. "
    "Use the tools to complete the task, check the results, and keep going until "
    "the task is done. Then reply with a short summary of what you did.\n"
    "Guidelines:\n"
    "- Prefer several small files over one huge file (e.g. index.html, style.css, "
    "script.js), and write each file with a single write_file call.\n"
    "- Commands run non-interactively with no stdin: use flags like --yes and never "
    "start long-running processes such as dev servers or file watchers.\n"
    "- Deleting files or folders needs the user's permission: the harness asks "
    "them before such a command runs. If they decline, don't delete it another way.\n"
    "- Only ask the user a question if the task is genuinely impossible without it.\n\n"
    "# Your memory layers\n"
    "Ephemeral memory holds summaries of your recent saved conversations. Don't check it "
    "on your own. Use ephemeral_list and ephemeral_read only when the user asks if you "
    "remember something, asks you to check, or the conversation clearly depends on an "
    "earlier one. When the user asks you to save the conversation, call ephemeral_save "
    "with a nuanced summary of the whole interaction: what was discussed and worked on, "
    "decisions and why, outcomes, files touched, and what is still open.\n"
    "Transient notes are your own notebook, one titled entry per project or topic. Read and "
    "edit them whenever it helps, and add new information to the entry it belongs to. "
    "The pilot's memory section has each layer's current limits. Never store secrets in "
    "any layer."
)


# Shown after pressing Esc while the agent works. Enter with no text stops the task.
REDIRECT_PROMPT = "→ What should I do instead? "

# Personality and user profile, loaded into the system prompt before each
# request. HTML comments (<!-- -->) are ignored.
def _profile(name):
    """Your own self.md / user.md (git-ignored), or the example until you make one."""
    own = Path(__file__).parent / f"{name}.md"
    return own if own.exists() else Path(__file__).parent / f"{name}.example.md"


SELF_FILE = _profile("self")   # who the agent is
USER_FILE = _profile("user")   # who the user is

# Modes: each .md file in this folder is a layer on top of self.md (/mode).
MODES_DIR = Path(__file__).parent / "self layers"
MODE_MAX_CHARS = 4000  # the active mode is sent with every turn

# Input history for the prompt (up-arrow), kept across sessions.
HISTORY_FILE = Path(__file__).parent / ".history"

# Seeds: notes the agent maintains. pilot.md (written by you) goes into the
# system prompt with the tool list and seed index filled in.
SEEDS_DIR = Path(__file__).parent / "seeds"
SEED_MAX_CHARS = 4000  # per seed, to keep them small

# Ephemeral memory and transient notes (see notes.py).
NOTES_DIR = Path(__file__).parent / "notes"
EPHEMERAL_KEEP = 10  # saved conversations kept; the oldest is dropped
EPHEMERAL_MAX_CHARS = 6000  # per saved conversation
TRANSIENT_MAX_CHARS = 7500  # the whole notebook
TRANSIENT_ENTRIES = 12  # titled entries (one per project or topic)
TRANSIENT_ENTRY_MAX_CHARS = 4000  # per entry; reads go one entry at a time
# These are defaults: /options → Memory layers changes them (options/memory.json,
# see memory_settings.py), and the format rubrics are in "memory formats/".
MEMORY_RUBRIC_MAX_CHARS = 1500  # per rubric, added to its layer's tool descriptions

# Tasks and the internal clock (/options). The clock is off until turned on.
# Each cycle runs the due tasks in number order (see tasks.py).
OPTIONS_DIR = Path(__file__).parent / "options"
TASKS_DIR = OPTIONS_DIR / "tasks"
TASK_FAILURE_LIMIT = 3  # failures in a row before a task pauses
# Pilot tasks: frozen tasks only the user changes, by editing these files. They run
# first in every cycle and can't be edited, deleted, or deactivated in /tasks.
PILOT_TASKS_DIR = Path(__file__).parent / "pilot_tasks"
CLOCK_POLL_SECONDS = 5  # how often the clock looks for a quiet moment

# Interaction seeds (interaction.py): one cue note per conversation, split into
# topics. The harness creates, promotes, and deletes them; the model edits them.
INTERACTION_DIR = SEEDS_DIR / "interaction seeds"
INTERACTION_MAX_CHARS = 3000  # per seed
SHORT_TERM_DAYS = 14  # unreferenced and unedited this long: deleted
LONG_TERM_DAYS = 30  # restarts on every edit, reference, or consolidation review
PROMOTE_AFTER_REFS = 2  # other conversations recalling a topic before it moves to long term

# User seeds: what Echo learns about the user (user.md stays the user's own).
USER_SEEDS_FILE = SEEDS_DIR / "user.seeds.md"
USER_SEEDS_MAX_CHARS = 3000

# Context resets: at this share of the context window, chat asks whether to run
# a memory layer update or compact, and a task writes task notes and continues.
CONTEXT_PROMPT_AT = 0.8
RESET_MAX_TURNS = 8  # model calls allowed for writing memory or task notes
TASK_NOTES_MAX_CHARS = 6000
TASK_TIME_LIMIT_MINUTES = None  # per task run; None = the clock's cycle length

# Reminders (reminders.py): what agents leave for the next chat.
REMINDERS_DIR = Path(__file__).parent / "reminders"
REMINDER_KEEP_DAYS = 30  # older reminders are removed
REMINDER_MAX_CHARS = 500  # per reminder
REMINDERS_PER_RUN = 3  # per task run

# Security scan (security.py): a problem that stays is reminded again this often.
SECURITY_REPEAT_HOURS = 24

# Long-term memory: an Obsidian vault, or any folder of markdown notes.
VAULT_DIR = Path.home() / "Documents" / "Echo"  # default; pick another in /options → Memory layers
VAULT_READ_MAX_CHARS = 8000  # per note read
VAULT_WRITE_MAX_CHARS = 20000  # per note written

# The gate (gate.py): what a task is told when blocked, and its silent log.
SECURITY_SEEDS_DIR = SEEDS_DIR / "security"
GATE_LOG = OPTIONS_DIR / "logs" / "gate.jsonl"

# Skills (skills.py): folders in skills/, each with SKILL.md and optional
# tools.py. Only the user adds or edits them; which are on is in options/skills.json.
SKILLS_DIR = Path(__file__).parent / "skills"

# Sub-agents and swarms (subagents.py, the swarm skill). Library in
# options/subagents/. Memory numbers are for the soft memory lock.
SUBAGENTS_DIR = OPTIONS_DIR / "subagents"
SUBAGENT_HEADROOM_GB = 2  # kept free for macOS and other apps
SUBAGENT_MIN_CTX = 8192  # a model qualifies only if it fits at least this
SUBAGENT_CTX = 16384  # the most context a sub-agent gets
SUBAGENT_MAX_TURNS = 15  # model calls per persona agent per pass
SUBAGENT_TIME_LIMIT = 300  # seconds per persona agent per pass
SUBAGENT_TEMPERATURE = 0  # persona agents: repeatable answers
SUBAGENT_COPY_TEMPERATURE = 0.6  # extra copies of one persona, so they differ
SUBAGENT_MAX_FILES = 3000  # files a rule agent looks at
SUBAGENT_COMMAND_TIMEOUT = 300  # seconds for an allow-listed command
SWARM_MAX_AGENTS = 25
SWARM_MAX_PASSES = 5
SWARM_PASSES = 1  # default for a new swarm
SWARM_PARALLEL = 1  # persona agents at once (1 = one after another)
SWARM_REPORT_MAX_CHARS = 6000  # what the launcher gets back; the full report is saved

# The sentinel (sentinel.py, malware.py): the harness watches the Mac while the
# pilot security task is active; the model summarizes and responds.
SENTINEL_MINUTES = 5  # how often the harness scans
SENTINEL_SLOW_MINUTES = 60  # how often the slower checks run
SENTINEL_RESPONSE_ROUNDS = 5  # model response runs before the user is told to step in
SECURITY_EVENTS_KEEP_DAYS = 30  # summarized events are kept this long
XPROTECT_MAX_AGE_DAYS = 30  # older malware signatures are reported

# Communication (communication/<app>/): Telegram is the one pipeline in Mark 1.
COMMUNICATION_DIR = Path(__file__).parent / "communication"  # each app's data/ is private and git-ignored
TELEGRAM_IDLE_MINUTES = 10  # default: a conversation ends (and is logged) after this long without a message
TELEGRAM_POLL_SECONDS = 25  # long poll: how long one request waits for new messages
TELEGRAM_APPROVAL_MINUTES = 5  # no answer to Allow/Deny within this counts as Deny
TELEGRAM_STALE_MINUTES = 10  # messages older than this (sent while Echo was off) are skipped
TELEGRAM_MAX_MESSAGE_CHARS = 4000  # Telegram's limit is 4096; longer replies are split
TELEGRAM_RECALL_MAX_CHARS = 12000  # most of one past conversation the recall tool returns


# Your own settings: local_settings.py (git-ignored) overrides anything above,
# for example USER_NAME = "Sam" or MODEL = "qwen3:14b".
try:
    from local_settings import *  # noqa: F401,F403
except ImportError:
    pass
