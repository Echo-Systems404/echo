"""Conversation memory across user requests.

Only the user's requests and the agent's final answers are kept between
requests; tool calls made while handling a request live only for that request.
"""

import re
from pathlib import Path


def build_system_prompt(base, self_file, user_file, pilot="", mode="", user_seeds=""):
    """The system prompt: self.md (identity and personality), the base
    operating rules, the pilot seed, user.md (who the user is), what the agent
    has learned about the user, then the active mode, if any. Missing files,
    empty files and HTML comments are skipped."""
    sections = [
        read_markdown(self_file),
        base,
        pilot,
        _section("About the user", read_markdown(user_file)),
        _section("What you've learned about the user (your user seeds)", user_seeds),
        _section("Active mode", mode),
    ]
    return "\n\n".join(s for s in sections if s)


def read_markdown(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()


def _section(title, body):
    return f"# {title}\n{body}" if body else ""


SUMMARY_HEADER = "Summary of earlier conversation:"
SUMMARY_MAX_CHARS = 4000


class ConversationMemory:
    def __init__(self, system_prompt=None):
        self.system_prompt = system_prompt
        self.carry = ""  # what came before a context flush, kept until /clear
        self.history = []

    @property
    def full_system_prompt(self):
        if not self.carry:
            return self.system_prompt
        return "\n\n".join(s for s in (self.system_prompt, self.carry) if s)

    @property
    def messages(self):
        """Full message list to send to the model, system prompt first."""
        system = self.full_system_prompt
        prefix = [{"role": "system", "content": system}] if system else []
        return prefix + [dict(m) for m in self.history]

    def clear(self):
        """Forget the conversation; the system prompt stays."""
        self.history = []
        self.carry = ""

    def flush(self, carry):
        """Empty the context; carry is what the model is grounded with after."""
        self.history = []
        self.carry = carry

    def add(self, role, content):
        self.history.append({"role": role, "content": content})

    def compact(self, max_messages):
        """Fold the oldest messages into one summary once the history grows past
        max_messages. An earlier summary is kept whole, and the oldest lines go
        first once the summary is full."""
        if len(self.history) <= max_messages:
            return
        keep = max(max_messages - 1, 0)
        dropped, kept = self.history[: len(self.history) - keep], self.history[len(self.history) - keep :]
        lines = []
        for m in dropped:
            if m["role"] == "user" and m["content"].startswith(SUMMARY_HEADER):
                lines += m["content"][len(SUMMARY_HEADER):].strip().splitlines()
            else:
                lines.append(f"- {m['role']}: {' '.join(m['content'].split())[:200]}")
        while lines and len("\n".join(lines)) > SUMMARY_MAX_CHARS - len(SUMMARY_HEADER):
            lines.pop(0)
        self.history = [{"role": "user", "content": SUMMARY_HEADER + "\n" + "\n".join(lines)}] + kept
