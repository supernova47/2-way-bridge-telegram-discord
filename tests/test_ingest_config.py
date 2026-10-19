from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import discord
from telegram import Update

from bridge.config import Config, ConfigError, Pair, load_config
from bridge.ingest import Ingestor, discord_files, telegram_text
from bridge.store import Store


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.sqlite3")
        self.config = Config((Pair("one", 123, -100),), Path(self.tmp.name) / "state.sqlite3")
        self.ingest = Ingestor(self.config, self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def discord(self, **kwargs):
        data = dict(
            channel=NS(id=123, name="channel"),
            author=NS(id=1, display_name="Автор", bot=False),
            webhook_id=None,
            type=discord.MessageType.default,
            content="hello",
            clean_content="hello",
            reference=None,
            attachments=[],
            id=10,
            stickers=[],
            embeds=[],
        )
        data.update(kwargs)
        return NS(**data)

    def telegram(self, *, update_id=1, edited=False, **kwargs):
        message = {
            "message_id": 10,
            "date": 1_700_000_000,
            "chat": {"id": -100, "type": "supergroup", "title": "group"},
            "from": {"id": 1, "is_bot": False, "first_name": "Автор"},
            "text": "hello",
        }
        message.update(kwargs)
        return Update.de_json(
            {"update_id": update_id, "edited_message" if edited else "message": message}, None
        )

    def count(self):
        return self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0]

    def test_discord_own_bot_other_bots_webhooks_commands_services_are_filtered(self):
        cases = [
            self.discord(author=NS(id=99, bot=False)),
            self.discord(author=NS(id=2, bot=True)),
            self.discord(webhook_id=99),
            self.discord(type=discord.MessageType.pins_add),
            self.discord(content=" /cmd"),
            self.discord(content="!help"),
            self.discord(channel=NS(id=321)),
        ]
        for message in cases:
            self.ingest.discord_message(message, 99)
        self.assertEqual(self.count(), 0)
        self.ingest.discord_message(self.discord(), 99)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.store.snapshot("one", "discord", 123, 10).author_id, 1)

    def test_self_is_filtered_even_when_other_bots_enabled(self):
        object.__setattr__(self.config, "ignore_bots", False)
        self.ingest.discord_message(self.discord(author=NS(id=99, bot=True)), 99)
        self.ingest.telegram_update(
            self.telegram(**{"from": {"id": 99, "is_bot": True, "first_name": "Bridge"}}), 99
        )
        self.assertEqual(self.count(), 0)

    def test_telegram_filters_services_commands_other_chats_and_own_messages(self):
        messages = [
            self.telegram(text="/start"),
            self.telegram(text=" !help"),
            self.telegram(text=None, new_chat_title="new"),
            self.telegram(chat={"id": -999, "type": "group", "title": "other"}),
            self.telegram(**{"from": {"id": 99, "is_bot": True, "first_name": "Bridge"}}),
            self.telegram(**{"from": {"id": 2, "is_bot": True, "first_name": "Other"}}),
        ]
        for message in messages:
            self.ingest.telegram_update(message, 99)
        self.assertEqual(self.count(), 0)

    def test_telegram_display_name_without_username_and_dedup(self):
        update = self.telegram()
        self.ingest.telegram_update(update, 99)
        self.ingest.telegram_update(update, 99)
        self.assertEqual(self.count(), 1)
        msg = self.store.snapshot("one", "telegram", -100, 10)
        self.assertEqual(msg.author, "Автор")

    def test_telegram_anonymous_admin_keeps_group_attribution(self):
        update = self.telegram(
            sender_chat={"id": -100, "type": "supergroup", "title": "Анонимный админ"},
            **{"from": {"id": 1, "is_bot": True, "first_name": "GroupAnonymousBot"}},
        )
        self.ingest.telegram_update(update, 99)
        msg = self.store.snapshot("one", "telegram", -100, 10)
        self.assertEqual(msg.author, "Анонимный админ")

    def test_telegram_photos_documents_and_unsupported_content(self):
        photo = {"file_id": "fid", "file_unique_id": "uid", "width": 10, "height": 10, "file_size": 123}
        self.ingest.telegram_update(self.telegram(text=None, photo=[photo], caption="photo caption"), 99)
        msg = self.store.snapshot("one", "telegram", -100, 10)
        self.assertEqual(msg.attachments[0].media_type, "image")
        self.assertEqual(msg.text, "photo caption")
        self.ingest.telegram_update(
            self.telegram(
                message_id=11,
                text=None,
                document={"file_id": "doc", "file_unique_id": "doc1", "file_name": "a.pdf", "file_size": 42},
            ),
            99,
        )
        self.assertEqual(self.store.snapshot("one", "telegram", -100, 11).attachments[0].name, "a.pdf")
        self.ingest.telegram_update(
            self.telegram(message_id=12, text=None, dice={"emoji": "🎲", "value": 1}), 99
        )
        self.assertIn("not support", self.store.snapshot("one", "telegram", -100, 12).notice)

    def test_discord_gifs_and_videos_keep_their_media_type(self):
        files = discord_files(
            [
                {"id": "1", "filename": "loop.gif", "size": 42, "url": "https://cdn.test/1"},
                {
                    "id": "2",
                    "filename": "clip.bin",
                    "content_type": "video/mp4",
                    "size": 84,
                    "url": "https://cdn.test/2",
                },
                {
                    "id": "3",
                    "filename": "photo.png",
                    "content_type": "image/png",
                    "size": 21,
                    "url": "https://cdn.test/3",
                },
            ]
        )
        self.assertEqual([item.media_type for item in files], ["animation", "video", "image"])

    def test_telegram_gif_and_video_documents_keep_their_media_type(self):
        self.ingest.telegram_update(
            self.telegram(
                text=None,
                document={
                    "file_id": "gif",
                    "file_unique_id": "gif-unique",
                    "file_name": "loop.gif",
                    "mime_type": "image/gif",
                    "file_size": 42,
                },
            ),
            99,
        )
        gif = self.store.snapshot("one", "telegram", -100, 10).attachments[0]
        self.assertEqual(gif.media_type, "animation")

        self.ingest.telegram_update(
            self.telegram(
                message_id=11,
                text=None,
                document={
                    "file_id": "video",
                    "file_unique_id": "video-unique",
                    "file_name": "clip.mp4",
                    "mime_type": "video/mp4",
                    "file_size": 84,
                },
            ),
            99,
        )
        video = self.store.snapshot("one", "telegram", -100, 11).attachments[0]
        self.assertEqual(video.media_type, "video")

    def test_telegram_protected_content_is_not_forwarded(self):
        self.ingest.telegram_update(self.telegram(has_protected_content=True, text="private"), 99)
        msg = self.store.snapshot("one", "telegram", -100, 10)
        self.assertEqual(msg.text, "")
        self.assertIn("Protected", msg.notice)

    def test_hidden_link_target_and_emoji_utf16_offsets_are_preserved(self):
        update = self.telegram(
            text="😀 site",
            entities=[{"type": "text_link", "offset": 3, "length": 4, "url": "https://example.org/path"}],
        )
        self.assertEqual(telegram_text(update.message), "😀 site (https://example.org/path)")

    def test_tg_topics_do_not_leak_into_whole_group_route(self):
        self.ingest.telegram_update(self.telegram(message_thread_id=5, is_topic_message=True), 99)
        self.assertEqual(self.count(), 0)
        object.__setattr__(self.config, "pairs", (Pair("topic", 123, -100, telegram_thread_id=5),))
        self.ingest.telegram_update(self.telegram(message_thread_id=6, is_topic_message=True), 99)
        self.ingest.telegram_update(self.telegram(message_thread_id=5, is_topic_message=True), 99)
        self.assertEqual(self.count(), 1)

    def test_discord_raw_edit_delete_and_command_redaction(self):
        self.ingest.discord_message(self.discord(), 99)
        payload = NS(
            channel_id=123,
            message_id=10,
            data={"edited_timestamp": "2026-10-19T00:00:00.001Z", "content": "new"},
        )
        self.ingest.discord_edit(payload)
        self.ingest.discord_edit(payload)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.store.snapshot("one", "discord", 123, 10).text, "new")
        payload.data = {"edited_timestamp": "2026-10-19T00:00:01Z", "content": "/command"}
        self.ingest.discord_edit(payload)
        self.assertIn("hidden", self.store.snapshot("one", "discord", 123, 10).text)
        self.ingest.discord_delete(123, 10)
        self.ingest.discord_delete(123, 10)
        self.assertEqual(self.count(), 4)

    def test_telegram_edits_known_messages_only(self):
        self.ingest.telegram_update(self.telegram(edited=True, update_id=2, text="unknown"), 99)
        self.assertEqual(self.count(), 0)
        self.ingest.telegram_update(self.telegram(), 99)
        edit = self.telegram(edited=True, update_id=3, text="updated")
        self.ingest.telegram_update(edit, 99)
        self.ingest.telegram_update(edit, 99)
        self.assertEqual(self.count(), 2)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.base = '[[pairs]]\nname="one"\ndiscord_channel_id=123\ntelegram_chat_id=-100\n'
        self.path.write_text(self.base, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_tokens_names_only(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(ConfigError) as caught:
            load_config(self.path)
        self.assertIn("DISCORD_TOKEN", str(caught.exception))
        self.assertIn("TELEGRAM_TOKEN", str(caught.exception))

    def test_secrets_are_excluded_from_repr(self):
        with patch.dict("os.environ", {"DISCORD_TOKEN": "secret-discord", "TELEGRAM_TOKEN": "secret-tg"}):
            config = load_config(self.path)
        self.assertNotIn("secret", repr(config))

    def test_duplicate_endpoints_rejected(self):
        self.path.write_text(
            self.base + '\n[[pairs]]\nname="two"\ndiscord_channel_id=123\ntelegram_chat_id=-200\n',
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            load_config(self.path, require_tokens=False)

    def test_invalid_settings_are_rejected_safely(self):
        prefixes = [
            "max_attempts=0\n",
            'ignore_bots="yes"\n',
            'command_prefixes=[""]\n',
            "max_file_mb=2.5\n",
            "telegram_interval_seconds=0\n",
            'token="secret"\n',
        ]
        for prefix in prefixes:
            self.path.write_text(prefix + self.base, encoding="utf-8")
            with self.assertRaises(ConfigError) as caught:
                load_config(self.path, require_tokens=False)
            self.assertNotIn("secret", str(caught.exception))

    def test_database_resolves_relative_to_config_file(self):
        config = load_config(self.path, require_tokens=False)
        self.assertEqual(config.database, self.path.parent / "data" / "bridge.sqlite3")

    def test_webhook_secret_stays_outside_config_and_repr(self):
        self.path.write_text(self.base + 'discord_webhook_env="HOOK"\n', encoding="utf-8")
        with patch.dict("os.environ", {"HOOK": "https://discord.com/api/webhooks/123/secret-token"}):
            config = load_config(self.path, require_tokens=False)
        self.assertNotIn("secret-token", repr(config))
        self.assertIn("one", config.webhooks)

    def test_avatar_token_urls_are_rejected(self):
        self.path.write_text(
            self.base + '\n[avatars]\n"123"="https://api.telegram.org/file/botSECRET/a.jpg"\n',
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError) as caught:
            load_config(self.path, require_tokens=False)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_user_mapping_matches_platform_ids_and_rejects_ambiguous_ids(self):
        mapping = (
            "\n[[user_mappings]]\ntelegram_user_id=111\ndiscord_user_id=222\n"
            'telegram_name="Ivan Romanov"\ndiscord_name="SuperNova"\n'
        )
        self.path.write_text(self.base + mapping, encoding="utf-8")
        config = load_config(self.path, require_tokens=False)
        self.assertEqual(config.mapped_name("telegram", 111), "SuperNova")
        self.assertEqual(config.mapped_name("discord", 222), "Ivan Romanov")
        self.assertIsNone(config.mapped_name("telegram", 999))

        self.path.write_text(self.base + mapping + mapping, encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.path, require_tokens=False)
