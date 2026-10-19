from __future__ import annotations

import re

_TELEGRAM_ID_COMMAND = re.compile(r"/bridge_id(?:@[A-Za-z0-9_]+)?\Z", re.IGNORECASE)


def is_discord_id_command_text(text: str) -> bool:
    return (text or "").strip().casefold() == "!bridge_id"


def is_telegram_id_command_text(text: str) -> bool:
    return bool(_TELEGRAM_ID_COMMAND.fullmatch((text or "").strip()))


def discord_id_command(message, config, self_id: int | None) -> str | None:
    author = message.author
    if (
        config.route("discord", message.channel.id) is None
        or author.id == self_id
        or author.bot
        or message.webhook_id
        or not is_discord_id_command_text(message.content)
    ):
        return None
    return f"Discord user ID: {author.id}\nChannel ID: {message.channel.id}"


def telegram_id_command(update, config, self_id: int) -> tuple[object, str] | None:
    message = update.message
    if message is None or message.chat.type not in {"group", "supergroup"}:
        return None
    if (
        config.route(
            "telegram", message.chat_id, message.message_thread_id if message.is_topic_message else None
        )
        is None
    ):
        return None
    user = message.from_user
    if (
        user is None
        or message.sender_chat is not None
        or user.id == self_id
        or user.is_bot
        or not is_telegram_id_command_text(message.text)
    ):
        return None
    return message, f"Telegram user ID: {user.id}\nChat ID: {message.chat_id}"
