from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import discord
from telegram.error import BadRequest, NetworkError, RetryAfter

from bridge.config import Config, Pair
from bridge.model import (
    Attachment,
    AttachmentUnavailable,
    Message,
    PermanentError,
    RetryableError,
    UncertainError,
)
from bridge.transports import DiscordTransport, TelegramTransport, download, filename


class Response:
    def __init__(self, status=200, body=None, blocks=(), length=None):
        self.status, self.body = status, body or {"id": "1234"}
        self.blocks, self.content_length = blocks, length
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.body

    async def iter_chunked(self, size):
        for block in self.blocks:
            yield block


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pair = Pair("one", 123, -100)
        self.config = Config((self.pair,), Path("unused"), discord_token="not-a-real-token")
        self.session = NS(post=Mock(return_value=Response()), get=Mock(return_value=Response()))
        self.channel = NS(
            guild=NS(filesize_limit=10_000_000),
            id=123,
            get_partial_message=Mock(return_value=NS(edit=AsyncMock(), delete=AsyncMock())),
        )
        self.client = NS(get_channel=Mock(return_value=self.channel), is_ready=Mock(return_value=True))
        self.bot = NS(
            send_message=AsyncMock(return_value=NS(message_id=1234)),
            send_photo=AsyncMock(return_value=NS(message_id=1235)),
            send_animation=AsyncMock(return_value=NS(message_id=1237)),
            send_video=AsyncMock(return_value=NS(message_id=1238)),
            send_document=AsyncMock(return_value=NS(message_id=1236)),
            edit_message_text=AsyncMock(),
            delete_message=AsyncMock(),
            get_file=AsyncMock(),
        )
        self.dt = DiscordTransport(self.client, self.session, self.config)
        self.tt = TelegramTransport(self.bot, self.session, self.config)
        self.dt.pacer.wait = AsyncMock()
        self.tt.pacer.wait = AsyncMock()

    async def test_telegram_plain_text_no_markup_or_link_previews(self):
        await self.tt.send(self.pair, "<b> & *literal*", reply_id=55)
        args = self.bot.send_message.call_args.kwargs
        self.assertIsNone(args["parse_mode"])
        self.assertEqual(args["text"], "<b> & *literal*")
        self.assertTrue(args["link_preview_options"].is_disabled)
        self.assertEqual(args["reply_parameters"].message_id, 55)
        self.assertTrue(args["reply_parameters"].allow_sending_without_reply)

    async def test_photo_preview_and_document_fallback(self):
        image = Attachment("a", "pic.jpg", 3, "url", "image")
        await self.tt.send(self.pair, "", attachment=image, data=b"abc")
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_document.assert_not_awaited()
        self.bot.send_photo.side_effect = BadRequest("Wrong file identifier/http url specified")
        await self.tt.send(self.pair, "", attachment=image, data=b"abc")
        self.bot.send_document.assert_awaited_once()
        self.assertIn("Preview unavailable", self.bot.send_document.call_args.kwargs["caption"])

    async def test_telegram_media_only_has_newline_attribution_caption(self):
        image = Attachment("a", "pic.jpg", 3, "url", "image")
        source = Message("discord", 1, 2, "Иван", "chat", text="")
        await self.tt.send(self.pair, "", attachment=image, data=b"abc", source_message=source)
        self.assertEqual(self.bot.send_photo.call_args.kwargs["caption"], "[Discord] Иван:\n")

    async def test_gif_is_sent_as_animation_and_falls_back_to_document(self):
        gif = Attachment("gif", "loop.gif", 3, "url", "animation")
        await self.tt.send(self.pair, "", attachment=gif, data=b"GIF89a")
        self.bot.send_animation.assert_awaited_once()
        self.assertEqual(self.bot.send_animation.call_args.kwargs["filename"], "loop.gif")
        self.bot.send_document.assert_not_awaited()

        self.bot.send_animation.side_effect = BadRequest("wrong animation file")
        await self.tt.send(self.pair, "", attachment=gif, data=b"GIF89a")
        self.bot.send_document.assert_awaited_once()
        self.assertIn("without a preview", self.bot.send_document.call_args.kwargs["caption"])

    async def test_video_is_sent_as_video_and_falls_back_to_document(self):
        video = Attachment("video", "clip.mp4", 3, "url", "video")
        await self.tt.send(self.pair, "", attachment=video, data=b"mp4")
        self.bot.send_video.assert_awaited_once()
        self.assertEqual(self.bot.send_video.call_args.kwargs["filename"], "clip.mp4")
        self.bot.send_document.assert_not_awaited()

        self.bot.send_video.side_effect = BadRequest("unsupported video codec")
        await self.tt.send(self.pair, "", attachment=video, data=b"mp4")
        self.bot.send_document.assert_awaited_once()
        self.assertIn("without a preview", self.bot.send_document.call_args.kwargs["caption"])

    async def test_telegram_rate_limit_safe_retry_and_ambiguous_network_hold(self):
        self.bot.send_message.side_effect = RetryAfter(7)
        with self.assertRaises(RetryableError) as caught:
            await self.tt.send(self.pair, "text")
        self.assertEqual(caught.exception.delay, 7)
        self.bot.send_message.side_effect = NetworkError("secret-url")
        with self.assertRaises(UncertainError) as caught:
            await self.tt.send(self.pair, "text")
        self.assertNotIn("secret-url", str(caught.exception))

    async def test_telegram_idempotent_mutations(self):
        self.bot.edit_message_text.side_effect = BadRequest("Message is not modified")
        await self.tt.edit(self.pair, 123, "text")
        self.bot.delete_message.side_effect = BadRequest("Message to delete not found")
        await self.tt.delete(self.pair, 123)
        self.bot.delete_message.side_effect = BadRequest("Message can't be deleted")
        with self.assertRaises(PermanentError):
            await self.tt.delete(self.pair, 123)

    async def test_discord_official_api_nonce_and_no_mentions(self):
        mid = await self.dt.send(self.pair, "text", nonce="stable", reply_id=77)
        args = self.session.post.call_args
        self.assertEqual(mid, 1234)
        self.assertEqual(args.args[0], "https://discord.com/api/v10/channels/123/messages")
        self.assertTrue(args.kwargs["json"]["enforce_nonce"])
        self.assertEqual(args.kwargs["json"]["allowed_mentions"]["parse"], [])
        self.assertFalse(args.kwargs["json"]["message_reference"]["fail_if_not_exists"])
        nonce = args.kwargs["json"]["nonce"]
        await self.dt.send(self.pair, "text", nonce="stable")
        self.assertEqual(self.session.post.call_args.kwargs["json"]["nonce"], nonce)

    async def test_webhook_author_and_public_avatar_with_explicit_source(self):
        self.config.webhooks["one"] = "https://discord.com/api/webhooks/1/not-a-real-webhook"
        self.config.avatars["88"] = "https://example.com/avatar.jpg"
        msg = Message("telegram", -100, 10, "Имя", "chat", "hello", author_id=88)
        await self.dt.send(self.pair, "text", source_message=msg)
        args = self.session.post.call_args.kwargs
        self.assertEqual(args["json"]["username"], "Имя · Telegram")
        self.assertEqual(args["json"]["avatar_url"], "https://example.com/avatar.jpg")
        self.assertEqual(args["params"], {"wait": "true"})
        self.assertNotIn("Authorization", args["headers"])
        self.assertNotIn("nonce", args["json"])

    async def test_discord_429_retries_but_500_and_timeouts_are_held(self):
        self.session.post.return_value = Response(429, {"retry_after": 9})
        with self.assertRaises(RetryableError) as caught:
            await self.dt.send(self.pair, "text")
        self.assertEqual(caught.exception.delay, 9)
        self.session.post.return_value = Response(502)
        with self.assertRaises(UncertainError):
            await self.dt.send(self.pair, "text")
        self.session.post.side_effect = TimeoutError()
        with self.assertRaises(UncertainError):
            await self.dt.send(self.pair, "text")

    async def test_no_automatic_post_retries(self):
        self.session.post.return_value = Response(502)
        with self.assertRaises(UncertainError):
            await self.dt.send(self.pair, "text")
        self.session.post.assert_called_once()

    async def test_discord_file_size_uses_real_guild_limit(self):
        self.assertEqual(self.dt.file_limit(self.pair), 10_000_000)
        with self.assertRaises(AttachmentUnavailable):
            await self.dt.download(Attachment("a", "big.bin", 30_000_000, "url"), 10_000_000)
        self.session.get.assert_not_called()

    async def test_telegram_download_limit_and_no_token_logging(self):
        with self.assertRaises(AttachmentUnavailable):
            await self.tt.download(Attachment("a", "big.bin", 21_000_000, "fid"), 30_000_000)
        self.bot.get_file.assert_not_awaited()

    async def test_streaming_limit_blocks_unknown_size_files(self):
        self.session.get.return_value = Response(blocks=[b"abc", b"def"])
        with self.assertRaises(AttachmentUnavailable):
            await download(self.session, "https://cdn.discordapp.com/a", 5, {"cdn.discordapp.com"})

    async def test_downloads_reject_arbitrary_hosts_and_redirects(self):
        for url in ("http://cdn.discordapp.com/a", "https://127.0.0.1/a", "https://evil.example/a"):
            with self.assertRaises(AttachmentUnavailable):
                await download(self.session, url, 100, {"cdn.discordapp.com"})
        self.session.get.assert_not_called()
        self.session.get.return_value = Response(status=302)
        with self.assertRaises(AttachmentUnavailable):
            await download(self.session, "https://cdn.discordapp.com/a", 100, {"cdn.discordapp.com"})
        self.assertFalse(self.session.get.call_args.kwargs["allow_redirects"])

    async def test_discord_webhook_mutations_use_webhook_credentials(self):
        pair = Pair("one", 123, -100, discord_webhook_env="HOOK")
        self.config.webhooks["one"] = "https://discord.com/api/webhooks/1/not-a-real-webhook"
        webhook = NS(edit_message=AsyncMock(), delete_message=AsyncMock())
        with patch.object(discord.Webhook, "from_url", return_value=webhook):
            await self.dt.edit(pair, 55, "edited")
            await self.dt.delete(pair, 55)
        webhook.edit_message.assert_awaited_once()
        webhook.delete_message.assert_awaited_once_with(55)
        self.channel.get_partial_message.return_value.edit.assert_not_awaited()

    def test_filename_never_uses_sender_path(self):
        self.assertEqual(filename("../../a.zip"), "a.zip")
        self.assertEqual(filename("C:\\temp\\a.jpg"), "a.jpg")
        self.assertNotIn("\x00", filename("bad\x00.jpg"))


class LoggingTests(unittest.TestCase):
    def test_dependency_logs_are_suppressed(self):
        import io
        import logging

        from bridge.__main__ import configure_logging

        stream = io.StringIO()
        with patch("sys.stderr", stream):
            configure_logging()
            logging.getLogger("httpx").error("token-bearing-url")
            logging.getLogger("discord.http").error("private message text")
            logging.getLogger("bridge").info("Bridge ready")
        self.assertIn("Bridge ready", stream.getvalue())
        self.assertNotIn("token-bearing-url", stream.getvalue())
        self.assertNotIn("private message text", stream.getvalue())
