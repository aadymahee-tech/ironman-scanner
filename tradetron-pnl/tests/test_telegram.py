"""Telegram transport: chunking, escaping, retries, fallbacks, redaction."""

from __future__ import annotations

import json
import unittest
from urllib.error import URLError
from urllib.parse import parse_qs

from tradetron_pnl import telegram

TOKEN = "123456:ABC-secret-token"


class FakePost:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, body, timeout):
        self.calls.append((url, parse_qs(body.decode("utf-8")), timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(message_id=1):
    return 200, {}, json.dumps({"ok": True, "result": {"message_id": message_id}}).encode()


def bot(post, **kw):
    return telegram.TelegramBot(TOKEN, "-1001", transport=post, sleep=lambda _s: None, **kw)


class TestHelpers(unittest.TestCase):
    def test_escape(self):
        self.assertEqual(telegram.escape("a<b & c>d"), "a&lt;b &amp; c&gt;d")
        self.assertEqual(telegram.escape('say "hi"'), 'say "hi"')

    def test_short_text_is_one_chunk(self):
        self.assertEqual(telegram.chunk_message("hello"), ["hello"])

    def test_chunks_split_on_lines(self):
        lines = [f"line {i:03d} " + "x" * 90 for i in range(60)]
        chunks = telegram.chunk_message("\n".join(lines), limit=1000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertEqual("\n".join(chunks).split("\n"), lines)

    def test_pathological_single_line_is_hard_split(self):
        chunks = telegram.chunk_message("y" * 2500, limit=1000)
        self.assertEqual([len(c) for c in chunks], [1000, 1000, 500])


class TestBot(unittest.TestCase):
    def test_send_posts_html_to_chat(self):
        post = FakePost([ok(7)])
        ids = bot(post).send("<b>hi</b>")
        self.assertEqual(ids, [7])
        url, params, _ = post.calls[0]
        self.assertEqual(url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(params["chat_id"], ["-1001"])
        self.assertEqual(params["text"], ["<b>hi</b>"])
        self.assertEqual(params["parse_mode"], ["HTML"])
        self.assertEqual(params["disable_web_page_preview"], ["true"])
        self.assertNotIn("disable_notification", params)

    def test_silent_flag(self):
        post = FakePost([ok()])
        bot(post).send("x", silent=True)
        self.assertEqual(post.calls[0][1]["disable_notification"], ["true"])

    def test_long_message_is_sent_in_pieces(self):
        post = FakePost([ok(1), ok(2)])
        text = "\n".join("row " + "z" * 100 for _ in range(60))
        ids = bot(post).send(text)
        self.assertEqual(ids, [1, 2])
        self.assertEqual(len(post.calls), 2)

    def test_429_waits_retry_after_then_succeeds(self):
        waits = []
        post = FakePost([
            (429, {}, json.dumps({"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 3}}).encode()),
            ok(),
        ])
        b = telegram.TelegramBot(TOKEN, "1", transport=post, sleep=waits.append)
        b.send("x")
        self.assertEqual(waits, [3.0])

    def test_html_parse_error_falls_back_to_plain_text(self):
        post = FakePost([
            (400, {}, json.dumps({"ok": False, "description": "Bad Request: can't parse entities"}).encode()),
            ok(9),
        ])
        ids = bot(post).send("<b>broken")
        self.assertEqual(ids, [9])
        self.assertNotIn("parse_mode", post.calls[1][1])

    def test_other_400_raises_without_token(self):
        post = FakePost([(400, {}, json.dumps({"ok": False, "description": f"chat not found {TOKEN}"}).encode())])
        with self.assertRaises(telegram.TelegramError) as ctx:
            bot(post).send("x")
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIn("chat not found", str(ctx.exception))
        self.assertEqual(ctx.exception.status, 400)

    def test_network_errors_retry_then_raise(self):
        post = FakePost([URLError("offline"), URLError("offline"), URLError("offline")])
        with self.assertRaises(telegram.TelegramError) as ctx:
            bot(post, retries=2).send("x")
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(len(post.calls), 3)

    def test_missing_credentials(self):
        with self.assertRaises(telegram.TelegramError):
            telegram.TelegramBot("", "1")
        with self.assertRaises(telegram.TelegramError):
            telegram.TelegramBot("t", "")


if __name__ == "__main__":
    unittest.main()
