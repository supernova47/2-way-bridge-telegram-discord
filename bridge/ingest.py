from __future__ import annotations

import re
from pathlib import PurePath

import discord

from .config import Config
from .model import Attachment, Message, digest
from .store import Store


def discord_text(text: str, mentions: list[dict] | None = None) -> str:
    for user in mentions or []:
        name = user.get("global_name") or user.get("username") or "User"
        text = re.sub(rf"<@!?{int(user['id'])}>", lambda _, name=name: "@" + name, text)
    text = re.sub(r"<@!?\d+>", "@User", text)
    text = re.sub(r"<@&\d+>", "@Role", text)
    return re.sub(r"<#\d+>", "#channel", text)


def telegram_text(message) -> str:
    text = message.text or message.caption or ""
    entities = message.entities if message.text else message.caption_entities
    # Entity offsets are UTF-16; retain hidden hyperlink destinations as visible text.
    encoded = text.encode("utf-16-le")
    for entity in sorted(entities or [], key=lambda e: e.offset, reverse=True):
        if entity.type == "text_link" and entity.url:
            at = (entity.offset + entity.length) * 2
            encoded = encoded[:at] + f" ({entity.url})".encode("utf-16-le") + encoded[at:]
    return encoded.decode("utf-16-le")


def discord_files(files) -> list[Attachment]:
    result = []
    for item in files:
        item = item if isinstance(item, dict) else item.to_dict()
        mime = item.get("content_type") or ""
        name = item.get("filename", "file.bin")
        extension = PurePath(name).suffix.lower()
        if mime == "image/gif" or extension == ".gif":
            media_type = "animation"
        elif mime.startswith("video/") or extension in {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}:
            media_type = "video"
        elif mime in {"image/jpeg", "image/png", "image/webp"} or extension in {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
        }:
            media_type = "image"
        else:
            media_type = "file"
        result.append(
            Attachment(
                str(item["id"]),
                name,
                item.get("size"),
                item["url"],
                media_type,
            )
        )
    return result


class Ingestor:
    def __init__(self, config: Config, store: Store):
        self.config, self.store = config, store

    def discord_message(self, raw: discord.Message, self_id: int):
        pair = self.config.route("discord", raw.channel.id)
        if (
            not pair
            or raw.author.id == self_id
            or raw.webhook_id
            or self.config.ignore_bots
            and raw.author.bot
            or raw.type not in (discord.MessageType.default, discord.MessageType.reply)
            or self.config.is_command(raw.content)
        ):
            return
        quote = ""
        if raw.reference and isinstance(raw.reference.resolved, discord.Message):
            quote = raw.reference.resolved.clean_content
        message = Message(
            platform="discord",
            chat_id=raw.channel.id,
            message_id=raw.id,
            author=raw.author.display_name,
            channel=raw.channel.name,
            text=raw.clean_content,
            attachments=discord_files(raw.attachments),
            reply_id=raw.reference.message_id if raw.reference else None,
            quote=quote,
            author_id=raw.author.id,
        )
        if raw.stickers:
            message.notice = "[Discord stickers are not supported by this bridge.]"
        if getattr(raw, "poll", None):
            message.notice += "\n[Discord polls are not supported by this bridge.]"
        if raw.embeds and not message.text and not message.attachments:
            message.notice += "\n[This message contains an embed that this bridge cannot display.]"
        self.store.accept(pair.name, message, "create")

    def discord_edit(self, payload):
        pair = self.config.route("discord", payload.channel_id)
        if not pair:
            return
        data = payload.data
        # Discord also emits embed/unfurl updates without a user edit.
        if not data.get("edited_timestamp") or not ({"content", "attachments"} & data.keys()):
            return
        message = self.store.snapshot(pair.name, "discord", payload.channel_id, payload.message_id)
        if message is None:
            return
        if "content" in data:
            message.text = discord_text(data["content"], data.get("mentions"))
        if "attachments" in data:
            message.attachments = discord_files(data["attachments"])
        if self.config.is_command(message.text):
            message.text, message.attachments = "[This message was edited into a command and hidden.]", []
        self.store.accept(pair.name, message, "edit", data["edited_timestamp"] + ":" + digest(data)[:16])

    def discord_delete(self, channel_id: int, message_id: int):
        pair = self.config.route("discord", channel_id)
        if not pair:
            return
        message = self.store.snapshot(pair.name, "discord", channel_id, message_id)
        if message:
            self.store.accept(pair.name, message, "delete")

    def telegram_update(self, update, self_id: int):
        raw = update.message or update.edited_message
        if raw is None or raw.chat.type not in {"group", "supergroup"}:
            return
        pair = self.config.route(
            "telegram", raw.chat_id, raw.message_thread_id if raw.is_topic_message else None
        )
        if pair is None:
            return
        user = raw.from_user
        # Anonymous group admins have sender_chat; don't discard them as GroupAnonymousBot.
        anonymous = raw.sender_chat is not None
        if user and not anonymous and (user.id == self_id or self.config.ignore_bots and user.is_bot):
            return
        if user and user.id == self_id:
            return
        edited = update.edited_message is not None
        # whitelist user content; service events (join/pin/rename/etc.) have none of these
        content_types = (
            "photo",
            "document",
            "video",
            "audio",
            "voice",
            "animation",
            "video_note",
            "sticker",
            "poll",
            "contact",
            "location",
            "venue",
            "dice",
            "paid_media",
            "story",
            "live_photo",
            "rich_message",
        )
        if not raw.text and not raw.caption and not any(getattr(raw, k, None) for k in content_types):
            return
        text = telegram_text(raw)
        if self.config.is_command(text) and not edited:
            return
        author = raw.sender_chat.title if anonymous else user.full_name if user else "Unknown user"
        message = Message(
            platform="telegram",
            chat_id=raw.chat_id,
            message_id=raw.message_id,
            author=author,
            channel=raw.chat.title or "group",
            text=text,
            reply_id=raw.reply_to_message.message_id if raw.reply_to_message else None,
            quote=(raw.reply_to_message.text or raw.reply_to_message.caption or "")
            if raw.reply_to_message and not raw.reply_to_message.has_protected_content
            else "",
            thread_id=pair.telegram_thread_id,
            author_id=user.id if user and not anonymous else None,
        )
        if raw.photo:
            photo = raw.photo[-1]
            message.attachments.append(
                Attachment(photo.file_unique_id, "photo.jpg", photo.file_size, photo.file_id, "image")
            )
        for attr in ("document", "video", "audio", "voice", "animation", "video_note"):
            media = getattr(raw, attr, None)
            if not media:
                continue
            # animation is also returned as document for compatibility; avoid duplicate uploads.
            if any(a.locator == media.file_id for a in message.attachments):
                continue
            name = getattr(media, "file_name", None) or f"{attr}.bin"
            mime = getattr(media, "mime_type", "") or ""
            extension = PurePath(name).suffix.lower()
            if attr == "animation" or mime == "image/gif" or extension == ".gif":
                media_type = "animation"
            elif (
                attr in {"video", "video_note"}
                or mime.startswith("video/")
                or extension in {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
            ):
                media_type = "video"
            elif mime in {"image/jpeg", "image/png", "image/webp"} or attr == "photo":
                media_type = "image"
            else:
                media_type = "file"
            message.attachments.append(
                Attachment(media.file_unique_id, name, media.file_size, media.file_id, media_type)
            )
        if any(
            getattr(raw, k, None)
            for k in (
                "sticker",
                "poll",
                "contact",
                "location",
                "venue",
                "dice",
                "paid_media",
                "story",
                "live_photo",
                "rich_message",
            )
        ):
            message.notice = "[This message contains a content type that this bridge does not support.]"
        if raw.has_protected_content:
            message.text, message.attachments = "", []
            message.notice = "[Protected Telegram content cannot be forwarded.]"
        if self.config.is_command(text):
            message.text, message.attachments = "[This message was edited into a command and hidden.]", []
        self.store.accept(
            pair.name, message, "edit" if edited else "create", str(update.update_id) if edited else ""
        )
