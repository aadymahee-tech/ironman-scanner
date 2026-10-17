"""Telegram Bot API ``sendMessage`` with HTML formatting, chunking and 429 handling.

Push-only: the scheduled run posts to one chat. No polling, no webhook, no
third-party package. The bot token never appears in error messages.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from html import escape as _html_escape
from typing import Callable, Dict, List, Mapping, Optional, Tuple

API_URL = "https://api.telegram.org/bot{token}/{method}"
MESSAGE_LIMIT = 4096
SAFE_CHUNK = 3900          # leave room for the HTML tags that chunking preserves

PostTransport = Callable[[str, bytes, float], Tuple[int, Mapping[str, str], bytes]]


class TelegramError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def escape(text: object) -> str:
    """Escape for Telegram HTML parse mode (``<``, ``>``, ``&`` only matter)."""
    return _html_escape(str(text), quote=False)


def urllib_post(url: str, body: bytes, timeout: float):
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict((exc.headers or {}).items()), (exc.read() if exc.fp is not None else b"")


def chunk_message(text: str, limit: int = SAFE_CHUNK) -> List[str]:
    """Split on line boundaries so no chunk exceeds ``limit`` characters."""
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for line in text.split("\n"):
        while len(line) > limit:                     # pathological single line
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        extra = len(line) + (1 if current else 0)
        if size + extra > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0
            extra = len(line)
        current.append(line)
        size += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


class TelegramBot:
    def __init__(self, token: str, chat_id: str, *, timeout: float = 20.0, retries: int = 2,
                 transport: Optional[PostTransport] = None,
                 sleep: Callable[[float], None] = time.sleep):
        if not token or not chat_id:
            raise TelegramError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set")
        self._token = token.strip()
        self.chat_id = str(chat_id).strip()
        self.timeout = timeout
        self.retries = max(0, retries)
        self.transport = transport or urllib_post
        self._sleep = sleep
        self.sent: List[str] = []

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<token>")

    def _call(self, method: str, params: Dict[str, str]) -> dict:
        url = API_URL.format(token=self._token, method=method)
        body = urllib.parse.urlencode(params).encode("utf-8")
        last: Optional[TelegramError] = None
        for attempt in range(self.retries + 1):
            try:
                status, headers, raw = self.transport(url, body, self.timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = TelegramError(f"telegram network error: {self._redact(str(exc))}", retryable=True)
                if attempt < self.retries:
                    self._sleep(1.0 * (attempt + 1))
                continue
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            if status == 200 and payload.get("ok"):
                return payload
            description = self._redact(str(payload.get("description") or f"HTTP {status}"))
            retry_after = (payload.get("parameters") or {}).get("retry_after")
            if status == 429 or status >= 500:
                last = TelegramError(f"telegram {method} failed: {description}", status=status, retryable=True)
                if attempt < self.retries:
                    self._sleep(min(float(retry_after or 1.0 * (attempt + 1)), 30.0))
                continue
            raise TelegramError(f"telegram {method} failed: {description}", status=status)
        assert last is not None
        raise last

    def send(self, text: str, *, parse_mode: str = "HTML", silent: bool = False) -> List[int]:
        """Send ``text`` (chunked if long). Returns the Telegram message ids."""
        ids: List[int] = []
        for chunk in chunk_message(text):
            params = {
                "chat_id": self.chat_id,
                "text": chunk,
                "disable_web_page_preview": "true",
            }
            if parse_mode:
                params["parse_mode"] = parse_mode
            if silent:
                params["disable_notification"] = "true"
            try:
                payload = self._call("sendMessage", params)
            except TelegramError as exc:
                # A bad HTML entity makes Telegram reject the whole message; fall
                # back to plain text so the update still arrives.
                if parse_mode and exc.status == 400 and "parse" in str(exc).lower():
                    params.pop("parse_mode", None)
                    payload = self._call("sendMessage", params)
                else:
                    raise
            ids.append(int((payload.get("result") or {}).get("message_id") or 0))
            self.sent.append(chunk)
        return ids


__all__ = ["MESSAGE_LIMIT", "TelegramBot", "TelegramError", "chunk_message", "escape"]
