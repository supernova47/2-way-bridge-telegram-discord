from __future__ import annotations

import json

from telegram import Bot

from .config import Config, ConfigError
from .formatting import visible


async def telegram_chats(token: str):
    """An explicit local discovery command, never part of the runtime logs."""
    async with Bot(token) as bot:
        if (await bot.get_webhook_info()).url:
            raise ConfigError(
                "Telegram already has a webhook configured; remove it before discovering groups."
            )
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


async def telegram_users(token: str, config: Config):
    """List IDs from configured group updates without acknowledging or printing messages."""
    async with Bot(token) as bot:
        if (await bot.get_webhook_info()).url:
            raise ConfigError(
                "Telegram already has a webhook configured; remove it before discovering users."
            )
        updates = await bot.get_updates(timeout=0, allowed_updates=["message", "edited_message"])
        users = {}
        for update in updates:
            message = update.message or update.edited_message
            user = message.from_user if message else None
            thread = message.message_thread_id if message and message.is_topic_message else None
            if (
                message
                and message.chat.type in {"group", "supergroup"}
                and config.route("telegram", message.chat_id, thread)
                and user
                and not user.is_bot
                and not message.sender_chat
            ):
                users[user.id] = {
                    "telegram_user_id": user.id,
                    "display_name": visible(user.full_name, single_line=True),
                }
        print(json.dumps(list(users.values()), ensure_ascii=True, indent=2))
        if not users:
            print("No configured-group users found. Stop the bridge, send /bridge_id, then run again.")
