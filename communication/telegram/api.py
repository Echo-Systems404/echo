"""A small client for Telegram's Bot API, with only the standard library.

Long polling (getUpdates) means no web server and no open ports: Echo asks
Telegram for new messages and waits up to TELEGRAM_POLL_SECONDS for them.
Every call is one plain HTTPS request, so nothing can deadlock.
"""

import json
import urllib.error
import urllib.request

import config

BASE = "https://api.telegram.org"


class TelegramError(Exception):
    def __init__(self, code, description):
        super().__init__(f"Telegram error {code}: {description}")
        self.code = code
        self.description = description


class Conflict(TelegramError):
    """Another program is reading this bot's messages (409)."""


class Api:
    def __init__(self, token):
        self.token = token

    def _clean(self, text):
        return str(text).replace(self.token, "<token>") if self.token else str(text)

    def call(self, method, params=None, seconds=30):
        """One Bot API call; returns its result. Raises TelegramError (or
        Conflict) for an API error and OSError for a network one. The token
        never appears in an error."""
        body = json.dumps({k: v for k, v in (params or {}).items() if v is not None}).encode("utf-8")
        request = urllib.request.Request(f"{BASE}/bot{self.token}/{method}", data=body,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8"))
            except (ValueError, OSError):
                data = {"ok": False, "error_code": exc.code, "description": exc.reason}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise OSError(self._clean(exc)) from None
        if not data.get("ok"):
            kind = Conflict if data.get("error_code") == 409 else TelegramError
            raise kind(data.get("error_code"), self._clean(data.get("description", "unknown")))
        return data.get("result")

    # -- the calls Echo uses ------------------------------------------------------

    def me(self):
        return self.call("getMe", seconds=15)

    def updates(self, offset, wait):
        params = {"offset": offset, "timeout": wait, "allowed_updates": ["message", "callback_query"]}
        return self.call("getUpdates", params, seconds=wait + 15) or []

    def send(self, chat_id, text, buttons=None):
        """Send plain text (no formatting, so nothing can fail to parse).
        buttons: [(label, data), ...] shown in one row. Returns the message."""
        markup = ({"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]}
                  if buttons else None)
        return self.call("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": markup,
                                         "link_preview_options": {"is_disabled": True}})

    def send_long(self, chat_id, text):
        for part in split(text, config.TELEGRAM_MAX_MESSAGE_CHARS):
            self.send(chat_id, part)

    def typing(self, chat_id):
        self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, seconds=10)

    def answer_button(self, callback_id, text):
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]}, seconds=10)

    def edit(self, chat_id, message_id, text):
        """Replace a message's text (and drop its buttons)."""
        self.call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text})


def split(text, limit):
    """Pieces of at most limit characters, cut at paragraph or line breaks
    when possible."""
    text = text.strip() or "(empty reply)"
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    parts.append(text)
    return parts
