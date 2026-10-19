from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from telegram import Update

from bridge.commands import discord_id_command, telegram_id_command
from bridge.config import ConfigError, UserMapping, load_config
from bridge.mappings import add_mapping


class MappingAndIdCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text(
            'database = "state.sqlite3"\n\n[[pairs]]\nname="one"\ndiscord_channel_id=123\n'
            "telegram_chat_id=-100\n",
            encoding="utf-8",
        )
        self.config = load_config(self.path, require_tokens=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_mapping_is_appended_atomically_with_quoted_unicode_names(self):
        original = self.path.read_text(encoding="utf-8")
        count = add_mapping(self.path, UserMapping(111, 222, 'Аня "А"', "SuperNova"))
        self.assertEqual(count, 1)
        self.assertTrue(self.path.read_text(encoding="utf-8").startswith(original))
        config = load_config(self.path, require_tokens=False)
        self.assertEqual(config.user_mappings[0].telegram_name, 'Аня "А"')
        self.assertEqual(config.user_mappings[0].discord_name, "SuperNova")
        self.assertFalse(list(self.path.parent.glob(".config.toml.*.tmp")))

    def test_duplicate_mapping_is_rejected_without_changing_config(self):
        mapping = UserMapping(111, 222, "Telegram name", "Discord name")
        add_mapping(self.path, mapping)
        before = self.path.read_bytes()
        with self.assertRaises(ConfigError):
            add_mapping(self.path, UserMapping(111, 333, "Another TG name", "Another Discord name"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_discord_id_command_only_responds_in_routed_human_messages(self):
        message = NS(
            author=NS(id=22, bot=False),
            channel=NS(id=123),
            webhook_id=None,
            content="  !Bridge_ID  ",
        )
        self.assertEqual(
            discord_id_command(message, self.config, 99),
            "Discord user ID: 22\nChannel ID: 123",
        )
        message.channel.id = 456
        self.assertIsNone(discord_id_command(message, self.config, 99))
        message.channel.id = 123
        message.author.bot = True
        self.assertIsNone(discord_id_command(message, self.config, 99))
        message.author.bot = False
        self.assertIsNone(discord_id_command(message, self.config, 22))
        message.webhook_id = 7
        self.assertIsNone(discord_id_command(message, self.config, 99))

    def telegram_update(self, *, text="/bridge_id", user_id=11, bot=False, chat_id=-100, **message_fields):
        message = {
            "message_id": 17,
            "date": 1_700_000_000,
            "chat": {"id": chat_id, "type": "supergroup", "title": "group"},
            "from": {"id": user_id, "is_bot": bot, "first_name": "Name"},
            "text": text,
        }
        message.update(message_fields)
        return Update.de_json({"update_id": 7, "message": message}, None)

    def test_telegram_id_command_accepts_bot_mention_and_is_chat_scoped(self):
        update = self.telegram_update(text="/bridge_id@bridge_bot")
        result = telegram_id_command(update, self.config, 99)
        self.assertIsNotNone(result)
        _, text = result
        self.assertEqual(text, "Telegram user ID: 11\nChat ID: -100")
        self.assertIsNone(telegram_id_command(self.telegram_update(chat_id=-200), self.config, 99))
        self.assertIsNone(telegram_id_command(self.telegram_update(bot=True), self.config, 99))
        self.assertIsNone(telegram_id_command(self.telegram_update(user_id=99, bot=True), self.config, 99))

    def test_id_commands_are_not_mapped_as_regular_messages(self):
        from bridge.ingest import Ingestor
        from bridge.store import Store

        store = Store(Path(self.tmp.name) / "queue.sqlite3")
        try:
            ingestor = Ingestor(self.config, store)
            ingestor.telegram_update(self.telegram_update(), 99)
            self.assertEqual(store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
