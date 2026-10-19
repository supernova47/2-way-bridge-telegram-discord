from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telegram import Update

from bridge.config import Config, Pair
from bridge.discovery import telegram_users


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_user_discovery_prints_ids_without_message_text(self):
        def update(update_id, chat_id, user_id, display_name, text):
            return Update.de_json(
                {
                    "update_id": update_id,
                    "message": {
                        "message_id": update_id + 1,
                        "date": 1_700_000_000,
                        "chat": {"id": chat_id, "type": "supergroup", "title": "group"},
                        "from": {"id": user_id, "is_bot": False, "first_name": display_name},
                        "text": text,
                    },
                },
                None,
            )

        bot = AsyncMock()
        bot.__aenter__.return_value = bot
        bot.__aexit__.return_value = False
        bot.get_webhook_info.return_value = NS(url="")
        bot.get_updates.return_value = [
            update(1, -100, 111, "Ivan", "private message text"),
            update(2, -999, 222, "Outside", "outside message"),
        ]
        output = io.StringIO()
        config = Config((Pair("configured", 123, -100),), database=Path("unused"))
        with patch("bridge.discovery.Bot", return_value=bot), redirect_stdout(output):
            await telegram_users("not-a-real-token", config)

        self.assertEqual(
            json.loads(output.getvalue()),
            [{"telegram_user_id": 111, "display_name": "Ivan"}],
        )
        self.assertNotIn("222", output.getvalue())
        self.assertNotIn("private message text", output.getvalue())
        bot.get_updates.assert_awaited_once_with(timeout=0, allowed_updates=["message", "edited_message"])
