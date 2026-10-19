from __future__ import annotations

import asyncio
import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from bridge.config import Config, ConfigError, Pair
from bridge.engine import Engine
from bridge.formatting import chunks, render, units
from bridge.model import (
    Attachment,
    AttachmentUnavailable,
    Message,
    PermanentError,
    RetryableError,
    UncertainError,
)
from bridge.store import Store, operation_tag


class FakeTransport:
    def __init__(self):
        self.sent, self.edited, self.deleted = [], [], []
        self.errors = []
        self.download_error = None
        self.block = None

    def file_limit(self, pair):
        return 20_000_000

    async def send(self, pair, content, **kwargs):
        if self.block:
            await self.block.wait()
        if self.errors:
            raise self.errors.pop(0)
        mid = 1000 + len(self.sent)
        self.sent.append({"id": mid, "pair": pair.name, "content": content, **kwargs})
        return mid

    async def edit(self, pair, message_id, content):
        self.edited.append((message_id, content))

    async def delete(self, pair, message_id):
        self.deleted.append(message_id)

    async def download(self, attachment, limit):
        if self.download_error:
            raise self.download_error
        return b"file bytes"


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bridge.sqlite3"
        self.store = Store(self.path)
        self.pair = Pair("one", 123, -100)
        self.config = Config((self.pair,), self.path, max_attempts=2, retry_base_seconds=0.1)
        self.discord, self.telegram = FakeTransport(), FakeTransport()
        self.engine = Engine(self.config, self.store, {"discord": self.discord, "telegram": self.telegram})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def message(self, platform="discord", message_id=10, **kwargs):
        return Message(
            platform,
            self.pair.chat(platform),
            message_id,
            "Имя без username",
            "chat",
            text="hello <b> & @everyone",
            **kwargs,
        )

    def accept(self, message, kind="create", revision=""):
        return self.store.accept(self.pair.name, message, kind, revision)

    async def process(self, platform="discord"):
        return await self.engine.process_one(self.pair, platform)

    def make_ready(self):
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET next_at=0")

    async def test_bidirectional_delivery_and_persistent_id_mapping(self):
        for platform, destination in (("discord", self.telegram), ("telegram", self.discord)):
            msg = self.message(platform)
            self.accept(msg)
            await self.process(platform)
            self.assertEqual(len(destination.sent), 1)
            self.assertIn(msg.author, destination.sent[0]["content"])
            self.assertIn(platform.title(), destination.sent[0]["content"])
            self.assertEqual(self.store.copies(msg.source_key("one"))["text:0"]["message_id"], "1000")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.status(), {"done": 2})

    async def test_duplicate_create_event_has_one_delivery(self):
        msg = self.message()
        self.assertTrue(self.accept(msg))
        self.assertFalse(self.accept(msg))
        await self.process()
        self.assertFalse(self.accept(msg))
        self.assertFalse(await self.process())
        self.assertEqual(len(self.telegram.sent), 1)

    async def test_reply_to_original_maps_to_copy(self):
        original = self.message()
        self.accept(original)
        await self.process()
        reply = self.message(message_id=11, reply_id=10, quote="hello")
        self.accept(reply)
        await self.process()
        self.assertEqual(self.telegram.sent[1]["reply_id"], 1000)

    async def test_reply_to_bridge_copy_maps_back_to_original(self):
        self.accept(self.message())
        await self.process()
        reply = self.message("telegram", message_id=12, reply_id=1000)
        self.accept(reply)
        await self.process("telegram")
        self.assertEqual(self.discord.sent[0]["reply_id"], 10)

    async def test_unknown_reply_contains_quote(self):
        self.accept(self.message(reply_id=99, quote="original text"))
        await self.process()
        self.assertIn("↪ Ответ: original text", self.telegram.sent[0]["content"])

    async def test_webhook_reply_uses_quote_even_when_mapping_known(self):
        self.pair = replace(self.pair, discord_webhook_env="HOOK")
        original = self.message("telegram")
        self.accept(original)
        await self.process("telegram")
        self.accept(self.message("telegram", message_id=11, reply_id=10, quote="quoted"))
        await self.process("telegram")
        self.assertIn("↪ Ответ: quoted", self.discord.sent[1]["content"])
        self.assertNotIn("\\[Telegram\\]", self.discord.sent[1]["content"])

    async def test_edits_are_idempotent_and_do_not_resend_text(self):
        msg = self.message()
        self.accept(msg)
        await self.process()
        msg.text = "edited"
        self.assertTrue(self.accept(msg, "edit", "v2"))
        self.assertFalse(self.accept(msg, "edit", "v2"))
        await self.process()
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertIn("edited", self.telegram.edited[0][1])

    async def test_long_message_grows_and_shrinks_with_cleanup(self):
        msg = self.message()
        msg.text = "😀" * 5000
        self.accept(msg)
        await self.process()
        self.assertGreater(len(self.telegram.sent), 2)
        self.assertTrue(all(units(p["content"]) <= 4096 for p in self.telegram.sent))
        msg.text = "short"
        self.accept(msg, "edit", "short")
        await self.process()
        self.assertEqual(len(self.store.copies(msg.source_key("one"))), 1)
        self.assertGreaterEqual(len(self.telegram.deleted), 2)

    async def test_delete_removes_all_parts_and_files(self):
        msg = self.message(attachments=[Attachment("a", "photo.jpg", 3, "cdn", "image")])
        self.accept(msg)
        await self.process()
        self.accept(msg, "delete")
        await self.process()
        self.assertCountEqual(self.telegram.deleted, [1000, 1001])
        self.assertEqual(self.store.copies(msg.source_key("one")), {})

    async def test_attachment_changes_replace_only_changed_files(self):
        msg = self.message(attachments=[Attachment("a", "a.bin", 3, "cdn")])
        self.accept(msg)
        await self.process()
        msg.attachments = [Attachment("b", "b.bin", 3, "cdn2")]
        self.accept(msg, "edit", "v2")
        await self.process()
        self.assertEqual(len(self.telegram.sent), 3)
        self.assertEqual(self.telegram.deleted, [1001])

    async def test_unsupported_file_sends_notice_and_text(self):
        self.discord.download_error = AttachmentUnavailable()
        self.accept(self.message(attachments=[Attachment("a", "big.zip", 99_000_000, "cdn")]))
        await self.process()
        self.assertEqual(self.store.status(), {"done": 1})
        self.assertIn("hello", self.telegram.sent[0]["content"])
        self.assertIn("не передан", self.telegram.sent[1]["content"])

    async def test_media_only_message_has_no_attachment_placeholder(self):
        message = self.message(attachments=[Attachment("a", "photo.jpg", 3, "cdn", "image")])
        message.text = ""
        self.accept(message)
        await self.process()
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertEqual(self.telegram.sent[0]["content"], "")
        self.assertEqual(self.telegram.sent[0]["attachment"].name, "photo.jpg")

    async def test_media_only_reply_attaches_to_the_known_source_copy(self):
        self.accept(self.message())
        await self.process()
        reply = self.message(
            message_id=11,
            reply_id=10,
            attachments=[Attachment("a", "clip.mp4", 3, "cdn", "video")],
        )
        reply.text = ""
        self.accept(reply)
        await self.process()
        self.assertEqual(self.telegram.sent[1]["reply_id"], 1000)

    async def test_oversized_attachment_notice_reports_actual_size_and_limit(self):
        self.discord.download_error = AttachmentUnavailable("file_too_large")
        self.accept(self.message(attachments=[Attachment("a", "clip.mp4", 23_000_000, "cdn", "video")]))
        await self.process()
        notice = self.telegram.sent[1]["content"]
        self.assertIn("23,0 МБ", notice)
        self.assertIn("20,0 МБ", notice)
        self.assertIn("max_file_mb", notice)

    async def test_retry_resumes_after_sent_text_without_duplication(self):
        self.discord.download_error = RetryableError("download_network")
        self.accept(self.message(attachments=[Attachment("a", "a.bin", 3, "cdn")]))
        await self.process()
        self.assertEqual(len(self.telegram.sent), 1)
        self.discord.download_error = None
        self.make_ready()
        await self.process()
        self.assertEqual(len(self.telegram.sent), 2)
        self.assertEqual(self.store.status(), {"done": 1})

    async def test_bounded_retries_and_manual_retry(self):
        self.telegram.errors = [RetryableError("temporary"), RetryableError("temporary")]
        self.accept(self.message())
        await self.process()
        self.assertEqual(self.store.status(), {"pending": 1})
        self.make_ready()
        await self.process()
        self.assertEqual(self.store.status(), {"failed": 1})
        self.assertFalse(await self.process())
        self.store.retry(1)
        await self.process()
        self.assertEqual(self.store.status(), {"done": 1})

    async def test_permanent_failure_does_not_retry(self):
        self.telegram.errors = [PermanentError("permissions")]
        self.accept(self.message())
        await self.process()
        self.assertEqual(self.store.status(), {"failed": 1})

    async def test_uncertain_send_is_held_until_operator_resolves_it(self):
        self.telegram.errors = [UncertainError()]
        msg = self.message()
        self.accept(msg)
        await self.process()
        self.assertEqual(self.store.status(), {"uncertain": 1})
        self.assertFalse(await self.process())
        with self.assertRaises(ConfigError):
            self.store.retry(1)
        self.store.resolve(1, 9999, self.pair)
        await self.process()
        self.assertEqual(self.telegram.sent, [])
        self.assertEqual(self.store.copies(msg.source_key("one"))["text:0"]["message_id"], "9999")

    async def test_crash_recovery_quarantines_inflight_send(self):
        self.accept(self.message())
        job = self.store.next_job("one", "discord")
        self.store.begin_operation(
            job["id"], operation_tag("send", "text:0", "text"), "send", "text:0", "text"
        )
        self.store.close()
        self.store = Store(self.path)
        self.store.recover()
        self.assertEqual(self.store.status(), {"uncertain": 1})

    async def test_crash_recovery_can_retry_idempotent_edits(self):
        msg = self.message()
        self.accept(msg)
        await self.process()
        self.accept(msg, "edit", "2")
        job = self.store.next_job("one", "discord")
        self.store.begin_operation(job["id"], "edit", "edit", "text:0", "text")
        self.store.recover()
        self.assertEqual(self.store.status(), {"done": 1, "pending": 1})

    async def test_failed_create_blocks_its_edit_but_other_message_progresses(self):
        msg = self.message()
        self.accept(msg)
        self.telegram.errors = [PermanentError("permissions")]
        await self.process()
        msg.text = "edit"
        self.accept(msg, "edit", "v2")
        self.accept(self.message(message_id=11))
        await self.process()
        self.assertEqual(self.store.status(), {"failed": 1, "pending": 1, "done": 1})

    async def test_slow_send_does_not_block_other_message(self):
        self.accept(self.message())
        self.accept(self.message(message_id=11))
        blocked = asyncio.Event()
        original_send = self.telegram.send

        async def send(pair, content, **kwargs):
            if kwargs["source_message"].message_id == 10:
                await blocked.wait()
            return await original_send(pair, content, **kwargs)

        self.telegram.send = send
        first = asyncio.create_task(self.process())
        await asyncio.sleep(0)
        await asyncio.wait_for(self.process(), timeout=1)
        self.assertEqual(len(self.telegram.sent), 1)
        blocked.set()
        await first

    async def test_routes_do_not_cross(self):
        other = Pair("two", 456, -200)
        msg = copy.deepcopy(self.message())
        msg.chat_id = 456
        self.store.accept("two", msg, "create")
        self.assertFalse(await self.process())
        await self.engine.process_one(other, "discord")
        self.assertEqual(self.telegram.sent[0]["pair"], "two")


class FormattingTests(unittest.TestCase):
    def test_literal_html_to_telegram(self):
        msg = Message("discord", 1, 2, "<admin>", "chat", "<b>tag</b> & **literal**")
        text = render(msg, "telegram", reply_known=False)[0]
        self.assertEqual(text, "[Discord] <admin>: <b>tag</b> & **literal**")

    def test_discord_markdown_mentions_and_bidi_are_safe(self):
        msg = Message("telegram", 1, 2, "Имя\n\u202eadmin", "chat", "**bold** @everyone <@123> `code`")
        text = render(msg, "discord", reply_known=False)[0]
        self.assertIn("\\*\\*bold\\*\\*", text)
        self.assertNotIn("@everyone", text)
        self.assertNotIn("<@123>", text)
        self.assertNotIn("\u202e", text)

    def test_emoji_chunks_respect_utf16_and_escape_boundaries(self):
        source = "😀*_\\" * 3000
        parts = chunks(source, 90, discord=True)
        self.assertTrue(all(units(p) <= 90 for p in parts))
        self.assertEqual("".join(parts), "😀\\*\\_\\\\" * 3000)

    def test_empty_and_control_only_text_is_not_silently_lost(self):
        self.assertEqual(chunks("\x00", 2000), ["(пустое сообщение)"])

    def test_media_only_message_does_not_render_an_attachment_placeholder(self):
        msg = Message("discord", 1, 2, "Автор", "chat", attachments=[Attachment("a", "pic.png", 1, "url")])
        self.assertEqual(render(msg, "telegram", reply_known=False), [])
