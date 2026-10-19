from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import discord
from telegram import Update

from bridge.config import Config, ConfigError, Pair
from bridge.ingest import Ingestor
from bridge.locking import InstanceLock
from bridge.runtime import (
    BridgeClient,
    _reply_telegram_id,
    register_telegram_commands,
    telegram_poll,
    validate_remote,
)
from bridge.store import Store


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bridge.sqlite3"
        self.store = Store(self.path)
        self.pair = Pair("one", 123, -100)
        self.config = Config((self.pair,), self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def update(self):
        return Update.de_json(
            {
                "update_id": 7,
                "message": {
                    "message_id": 10,
                    "date": 1700000000,
                    "chat": {"id": -100, "type": "group", "title": "group"},
                    "from": {"id": 1, "is_bot": False, "first_name": "A"},
                    "text": "hello",
                },
            },
            None,
        )

    async def test_poll_acknowledges_only_durably_accepted_updates(self):
        bot = NS(id=99, get_updates=AsyncMock(side_effect=[[self.update()], asyncio.CancelledError()]))
        with self.assertRaises(asyncio.CancelledError):
            await telegram_poll(bot, Ingestor(self.config, self.store), self.store)
        self.assertEqual(self.store.get_offset(), 8)
        self.assertEqual(bot.get_updates.call_args_list[0].kwargs["offset"], 0)
        self.assertEqual(bot.get_updates.call_args_list[1].kwargs["offset"], 8)
        self.assertEqual(self.store.status(), {"pending": 1})

    async def test_repeated_telegram_id_commands_each_receive_a_reply(self):
        updates = [
            Update.de_json(
                {
                    "update_id": update_id,
                    "message": {
                        "message_id": message_id,
                        "date": 1_700_000_000,
                        "chat": {"id": -100, "type": "group", "title": "group"},
                        "from": {"id": 1, "is_bot": False, "first_name": "A"},
                        "text": "/bridge_id",
                    },
                },
                None,
            )
            for update_id, message_id in ((7, 10), (8, 11))
        ]
        calls = 0
        second_reply_sent = asyncio.Event()

        async def get_updates(**kwargs):
            nonlocal calls
            calls += 1
            if calls <= len(updates):
                return [updates[calls - 1]]
            await asyncio.Event().wait()

        sent_ids = []

        async def send_message(**kwargs):
            sent_ids.append(kwargs["reply_parameters"].message_id)
            if len(sent_ids) == 2:
                second_reply_sent.set()

        bot = NS(
            id=99,
            get_updates=AsyncMock(side_effect=get_updates),
            send_message=AsyncMock(side_effect=send_message),
        )
        task = asyncio.create_task(telegram_poll(bot, Ingestor(self.config, self.store), self.store))
        await asyncio.wait_for(second_reply_sent.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(sent_ids, [10, 11])
        self.assertEqual(self.store.get_offset(), 9)
        self.assertEqual([call.kwargs["offset"] for call in bot.get_updates.await_args_list], [0, 8, 9])

    async def test_telegram_command_is_registered_only_for_configured_group(self):
        bot = NS(set_my_commands=AsyncMock())
        await register_telegram_commands(bot, self.config)
        bot.set_my_commands.assert_awaited_once()
        kwargs = bot.set_my_commands.await_args.kwargs
        self.assertEqual(kwargs["scope"].chat_id, -100)
        self.assertEqual(bot.set_my_commands.await_args.args[0][0].command, "bridge_id")

    async def test_intake_failure_does_not_advance_offset(self):
        bot = NS(id=99, get_updates=AsyncMock(return_value=[self.update()]))
        with patch.object(self.store, "accept", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await telegram_poll(bot, Ingestor(self.config, self.store), self.store)
        self.assertEqual(self.store.get_offset(), 0)

    async def test_telegram_id_reply_stays_in_topic_and_replies_to_command(self):
        bot = NS(send_message=AsyncMock())
        message = NS(chat_id=-100, message_id=17, is_topic_message=True, message_thread_id=5)
        await _reply_telegram_id(bot, message, "Telegram user ID: 11\nChat ID: -100")
        kwargs = bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["message_thread_id"], 5)
        self.assertEqual(kwargs["reply_parameters"].message_id, 17)

    async def test_discord_id_command_replies_and_never_enters_bridge_queue(self):
        client = NS(ingestor=Ingestor(self.config, self.store), user=NS(id=99))
        message = NS(
            author=NS(id=22, bot=False),
            channel=NS(id=123),
            content="!bridge_id",
            webhook_id=None,
            reply=AsyncMock(),
        )
        await BridgeClient.on_message(client, message)
        message.reply.assert_awaited_once()
        self.assertEqual(message.reply.await_args.args[0], "Discord user ID: 22\nChannel ID: 123")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    async def test_id_reply_failure_is_logged_without_raising(self):
        bot = NS(send_message=AsyncMock(side_effect=RuntimeError("private transport details")))
        message = NS(chat_id=-100, message_id=17, is_topic_message=False, message_thread_id=None)
        with self.assertLogs("bridge", level="WARNING") as caught:
            await _reply_telegram_id(bot, message, "IDs")
        self.assertTrue(any("ID command reply failed" in line for line in caught.output))
        self.assertNotIn("private transport details", "\n".join(caught.output))

    def remote(self):
        channel = Mock(spec=discord.TextChannel)
        channel.guild = NS(me=NS())
        channel.id = 123
        channel.permissions_for.return_value = NS(
            view_channel=True, read_message_history=True, attach_files=True, send_messages=True
        )
        client = NS(get_channel=Mock(return_value=channel))
        bot = NS(
            get_webhook_info=AsyncMock(return_value=NS(url="")),
            get_me=AsyncMock(return_value=NS(id=99, can_read_all_group_messages=True)),
            get_chat=AsyncMock(
                return_value=NS(
                    type="supergroup",
                    is_forum=False,
                    permissions=NS(
                        can_send_messages=True,
                        can_send_photos=True,
                        can_send_videos=True,
                        can_send_documents=True,
                    ),
                )
            ),
            get_chat_member=AsyncMock(return_value=NS(status="member")),
        )
        return channel, client, bot

    async def test_remote_validation_passes_for_minimal_permissions(self):
        _, client, bot = self.remote()
        await validate_remote(self.config, client, bot, None)

    async def test_remote_validation_rejects_missing_discord_permission(self):
        channel, client, bot = self.remote()
        channel.permissions_for.return_value.attach_files = False
        with self.assertRaises(ConfigError):
            await validate_remote(self.config, client, bot, None)

    async def test_remote_validation_rejects_missing_telegram_video_permission(self):
        _, client, bot = self.remote()
        bot.get_chat.return_value.permissions.can_send_videos = False
        with self.assertRaises(ConfigError) as caught:
            await validate_remote(self.config, client, bot, None)
        self.assertIn("videos", str(caught.exception))

    async def test_remote_validation_rejects_privacy_mode_for_non_admin(self):
        _, client, bot = self.remote()
        bot.get_me.return_value.can_read_all_group_messages = False
        with self.assertRaises(ConfigError):
            await validate_remote(self.config, client, bot, None)

    async def test_remote_validation_rejects_existing_telegram_webhook(self):
        _, client, bot = self.remote()
        bot.get_webhook_info.return_value.url = "https://example.com/existing"
        with self.assertRaises(ConfigError):
            await validate_remote(self.config, client, bot, None)

    async def test_remote_validation_rejects_wrong_discord_webhook_channel(self):
        pair = Pair("one", 123, -100, discord_webhook_env="HOOK")
        config = Config((pair,), self.path, webhooks={"one": "https://discord.com/api/webhooks/1/token"})
        _, client, bot = self.remote()
        hook = NS(fetch=AsyncMock(return_value=NS(channel_id=999)))
        with patch.object(discord.Webhook, "from_url", return_value=hook), self.assertRaises(ConfigError):
            await validate_remote(config, client, bot, None)


class LocalStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bridge.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_single_instance_lock_is_released_after_shutdown(self):
        with InstanceLock(self.path), self.assertRaises(ConfigError):
            with InstanceLock(self.path):
                self.fail("Second process should not acquire the database lock")
        with InstanceLock(self.path):
            pass

    def test_saved_route_cannot_be_repointed_to_another_chat(self):
        store = Store(self.path)
        try:
            store.bind_routes((Pair("one", 123, -100),))
            store.bind_routes((Pair("one", 123, -100),))
            with self.assertRaises(ConfigError):
                store.bind_routes((Pair("one", 123, -999),))
        finally:
            store.close()
