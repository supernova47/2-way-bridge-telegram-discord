from __future__ import annotations

import json

from telegram import Bot

from .config import ConfigError
from .formatting import visible


async def telegram_chats(token: str):
    """An explicit local discovery command, never part of the runtime logs."""
    async with Bot(token) as bot:
        if (await bot.get_webhook_info()).url:
            raise ConfigError("Telegram: настроен webhook; отключите его перед обнаружением групп.")
        updates = await bot.get_updates(timeout=0, allowed_updates=["message", "edited_message"])
        chats = {}
        for update in updates:
            message = update.message or update.edited_message
            if message and message.chat.type in {"group", "supergroup"}:
                thread = message.message_thread_id if message.is_topic_message else None
                key = (message.chat_id, thread)
                chats[key] = {
                    "telegram_chat_id": message.chat_id,
                    "title": visible(message.chat.title or "", single_line=True),
                    "telegram_thread_id": thread,
                }
        # No offset is sent, so discovery never acknowledges or drops these updates.
        print(json.dumps(list(chats.values()), ensure_ascii=True, indent=2))
        if not chats:
            print("No groups found. Add the bot and send /bridge_id in the group, then run again.")
