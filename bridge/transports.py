from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import time
from urllib.parse import urlsplit

import aiohttp
import discord
from telegram import LinkPreviewOptions, ReplyParameters
from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, RetryAfter, TelegramError

from .config import Config, Pair
from .formatting import visible
from .model import Attachment, AttachmentUnavailable, PermanentError, RetryableError, UncertainError

MB = 1_000_000


def filename(name: str) -> str:
    name = re.split(r"[/\\]", visible(name, single_line=True))[-1]
    return re.sub(r'[<>:"|?*]', "_", name)[:180] or "attachment.bin"


class Pacer:
    """Reserve request times without keeping a lock during network I/O."""

    def __init__(self, interval: float):
        self.interval, self.global_at, self.chats = interval, 0.0, {}
        self.lock = asyncio.Lock()

    async def wait(self, chat: int):
        async with self.lock:
            now = time.monotonic()
            at = max(now, self.chats.get(chat, 0))
            self.chats[chat] = at + self.interval
        await asyncio.sleep(max(0, at - time.monotonic()))
        # A busy group's future reservation must not stall requests to other groups.
        async with self.lock:
            at = max(time.monotonic(), self.global_at)
            self.global_at = at + 0.04
        await asyncio.sleep(max(0, at - time.monotonic()))

    def defer(self, seconds: float):
        self.global_at = max(self.global_at, time.monotonic() + seconds)


async def download(session: aiohttp.ClientSession, url: str, limit: int, hosts: set[str]) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.password:
        raise AttachmentUnavailable("unsafe_download_url")
    try:
        async with session.get(url, allow_redirects=False) as response:
            if response.status == 429 or response.status >= 500:
                raise RetryableError("download_temporary", 5)
            if response.status != 200:
                raise AttachmentUnavailable("download_unavailable")
            if response.content_length is not None and response.content_length > limit:
                raise AttachmentUnavailable("file_too_large")
            result = bytearray()
            async for block in response.content.iter_chunked(64 * 1024):
                result.extend(block)
                if len(result) > limit:
                    raise AttachmentUnavailable("file_too_large")
            return bytes(result)
    except (aiohttp.ClientError, TimeoutError):
        raise RetryableError("download_network") from None


class TelegramTransport:
    def __init__(self, bot, session: aiohttp.ClientSession, config: Config):
        self.bot, self.session, self.config = bot, session, config
        self.pacer = Pacer(config.telegram_interval_seconds)

    def file_limit(self, pair: Pair) -> int:
        return min(self.config.max_file_mb, 50) * MB

    async def call(self, pair: Pair, method: str, *, sending=False, file=False, **kwargs):
        await self.pacer.wait(pair.telegram_chat_id)
        try:
            return await getattr(self.bot, method)(chat_id=pair.telegram_chat_id, **kwargs)
        except RetryAfter as exc:
            delay = exc.retry_after
            delay = delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)
            self.pacer.defer(delay)
            raise RetryableError("telegram_rate_limit", delay) from None
        except (Forbidden, InvalidToken):
            raise PermanentError("telegram_permissions_or_token") from None
        except BadRequest as exc:
            # Match only locally; never log API responses containing user text/URLs.
            reason = str(exc).lower()
            if method.startswith("edit") and "message is not modified" in reason:
                return None
            if method == "delete_message" and "message to delete not found" in reason:
                return None
            if file and any(
                s in reason
                for s in (
                    "file",
                    "document",
                    "media",
                    "content type",
                    "photo",
                    "image",
                    "video",
                    "animation",
                    "unsupported",
                    "too large",
                    "size",
                    "dimensions",
                )
            ):
                raise AttachmentUnavailable("telegram_file_rejected") from None
            raise PermanentError("telegram_bad_request") from None
        except NetworkError:
            if sending:
                raise UncertainError("telegram_send_network") from None
            raise RetryableError("telegram_network") from None
        except TelegramError:
            raise PermanentError("telegram_api_error") from None

    async def send(
        self, pair, content, *, reply_id=None, attachment=None, data=None, nonce="", source_message=None
    ) -> int:
        kwargs = {"message_thread_id": pair.telegram_thread_id}
        if reply_id:
            kwargs["reply_parameters"] = ReplyParameters(
                message_id=reply_id, allow_sending_without_reply=True
            )
        if attachment:
            media_attribution = ""
            if source_message and source_message.platform == "discord" and not source_message.text.strip():
                author = visible(source_message.author, single_line=True)[:100] or "Unknown user"
                media_attribution = f"[Discord] {author}:\n"
                kwargs["caption"] = media_attribution
            result = None
            if attachment.media_type == "image" and len(data) <= 10 * MB:
                try:
                    result = await self.call(
                        pair, "send_photo", sending=True, file=True, photo=data, **kwargs
                    )
                except AttachmentUnavailable:
                    pass  # Rejected photo (dimensions/encoding): preserve it as a document.
            elif attachment.media_type in {"animation", "video"}:
                method = "send_animation" if attachment.media_type == "animation" else "send_video"
                media_key = "animation" if attachment.media_type == "animation" else "video"
                try:
                    result = await self.call(
                        pair,
                        method,
                        sending=True,
                        file=True,
                        **{media_key: data, "filename": filename(attachment.name)},
                        **kwargs,
                    )
                except AttachmentUnavailable:
                    pass  # Preserve files rejected as previewable media as documents.
            if result is None:
                if attachment.media_type == "image":
                    fallback_caption = "[Preview unavailable; image was sent as a file.]"
                elif attachment.media_type == "animation":
                    fallback_caption = "[Animation was sent as a file without a preview.]"
                elif attachment.media_type == "video":
                    fallback_caption = "[Video was sent as a file without a preview.]"
                else:
                    fallback_caption = ""
                if fallback_caption:
                    kwargs["caption"] = f"{media_attribution}{fallback_caption}"
                result = await self.call(
                    pair,
                    "send_document",
                    sending=True,
                    file=True,
                    document=data,
                    filename=filename(attachment.name),
                    **kwargs,
                )
        else:
            result = await self.call(
                pair,
                "send_message",
                sending=True,
                text=content,
                parse_mode=None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                **kwargs,
            )
        return result.message_id

    async def edit(self, pair, message_id, content):
        await self.call(
            pair,
            "edit_message_text",
            message_id=message_id,
            text=content,
            parse_mode=None,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def delete(self, pair, message_id):
        await self.call(pair, "delete_message", message_id=message_id)

    async def download(self, attachment: Attachment, target_limit: int) -> bytes:
        limit = min(target_limit, self.config.max_file_mb * MB, 20 * MB)
        if attachment.size is not None and attachment.size > limit:
            raise AttachmentUnavailable("file_too_large")
        try:
            remote = await self.bot.get_file(attachment.locator)
        except RetryAfter as exc:
            delay = exc.retry_after
            raise RetryableError(
                "telegram_rate_limit",
                delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay),
            ) from None
        except BadRequest:
            raise AttachmentUnavailable("telegram_file_unavailable") from None
        except NetworkError:
            raise RetryableError("telegram_download_network") from None
        if remote.file_size and remote.file_size > limit or not remote.file_path:
            raise AttachmentUnavailable("file_too_large_or_missing")
        return await download(self.session, remote.file_path, limit, {"api.telegram.org"})


class DiscordTransport:
    def __init__(self, client: discord.Client, session: aiohttp.ClientSession, config: Config):
        self.client, self.session, self.config = client, session, config
        self.pacer = Pacer(0.6)

    def channel(self, pair):
        channel = self.client.get_channel(pair.discord_channel_id)
        if not self.client.is_ready() or channel is None:
            raise RetryableError("discord_not_ready", 5)
        return channel

    def file_limit(self, pair):
        channel = self.channel(pair)
        return min(channel.guild.filesize_limit, self.config.max_file_mb * MB)

    async def send(
        self, pair, content, *, reply_id=None, attachment=None, data=None, nonce="", source_message=None
    ) -> int:
        self.channel(pair)
        await self.pacer.wait(pair.discord_channel_id)
        # Public REST API: discord.py's send does not expose enforce_nonce. A single HTTP
        # attempt avoids the SDK's automatic POST retries after ambiguous server errors.
        payload = {
            "content": content,
            "allowed_mentions": {"parse": [], "replied_user": False},
            "nonce": hashlib.sha256(nonce.encode()).hexdigest()[:24],
            "enforce_nonce": True,
            "flags": 4,
        }  # suppress link embeds
        webhook = self.config.webhooks.get(pair.name)
        url = f"https://discord.com/api/v10/channels/{pair.discord_channel_id}/messages"
        headers = {
            "Authorization": f"Bot {self.config.discord_token}",
            "User-Agent": "DiscordBot (https://discord.com/developers/docs, 0.1.0)",
        }
        params = {}
        if webhook:
            url, headers, params = webhook, {}, {"wait": "true"}
            channel = self.channel(pair)
            if isinstance(channel, discord.Thread):
                params["thread_id"] = str(channel.id)
            payload.pop("nonce")
            payload.pop("enforce_nonce")
            author = visible(source_message.author if source_message else "Participant", single_line=True)
            author = re.sub(r"discord|clyde", "Participant", author, flags=re.IGNORECASE)
            payload["username"] = (author[:60] or "Participant") + " · Telegram"
            avatar = self.config.avatars.get(str(source_message.author_id)) if source_message else None
            if avatar:
                payload["avatar_url"] = avatar
        if reply_id and not webhook:
            payload["message_reference"] = {"message_id": str(reply_id), "fail_if_not_exists": False}
        request = {"json": payload}
        if attachment:
            payload["attachments"] = [{"id": "0", "filename": filename(attachment.name)}]
            form = aiohttp.FormData()
            form.add_field("payload_json", json.dumps(payload))
            form.add_field(
                "files[0]",
                data,
                filename=filename(attachment.name),
                content_type=mimetypes.guess_type(filename(attachment.name))[0] or "application/octet-stream",
            )
            request = {"data": form}
        try:
            async with self.session.post(
                url,
                headers=headers,
                params=params,
                allow_redirects=False,
                **request,
            ) as response:
                if response.status == 429:
                    body = await response.json()
                    delay = max(1, float(body.get("retry_after", 5)))
                    self.pacer.defer(delay)
                    raise RetryableError("discord_rate_limit", delay)
                if response.status >= 500:
                    raise UncertainError("discord_send_server_error")
                if response.status in (401, 403):
                    raise PermanentError("discord_permissions_or_token")
                if attachment and response.status in (400, 413, 415):
                    raise AttachmentUnavailable("discord_file_rejected")
                if response.status not in (200, 201):
                    raise PermanentError("discord_bad_request")
                body = await response.json()
                return int(body["id"])
        except aiohttp.ClientConnectorError:
            raise RetryableError("discord_connect") from None
        except (aiohttp.ClientError, TimeoutError):
            raise UncertainError("discord_send_network") from None

    async def mutate(self, pair, message_id, *, content=None):
        channel = self.channel(pair)
        await self.pacer.wait(pair.discord_channel_id)
        partial = channel.get_partial_message(message_id)
        try:
            if pair.discord_webhook_env:
                webhook = discord.Webhook.from_url(self.config.webhooks[pair.name], session=self.session)
                kwargs = {"thread": discord.Object(channel.id)} if isinstance(channel, discord.Thread) else {}
                if content is None:
                    await webhook.delete_message(message_id, **kwargs)
                else:
                    await webhook.edit_message(
                        message_id, content=content, allowed_mentions=discord.AllowedMentions.none(), **kwargs
                    )
                return
            if content is None:
                await partial.delete()
            else:
                await partial.edit(content=content, allowed_mentions=discord.AllowedMentions.none())
        except discord.NotFound:
            if content is not None:
                raise PermanentError("discord_copy_missing") from None
        except discord.Forbidden:
            raise PermanentError("discord_permissions") from None
        except discord.HTTPException as exc:
            if exc.status == 429 or exc.status >= 500:
                raise RetryableError("discord_mutation_temporary") from None
            raise PermanentError("discord_bad_request") from None
        except (aiohttp.ClientError, TimeoutError):
            raise RetryableError("discord_network") from None

    async def edit(self, pair, message_id, content):
        await self.mutate(pair, message_id, content=content)

    async def delete(self, pair, message_id):
        await self.mutate(pair, message_id)

    async def download(self, attachment, target_limit):
        limit = min(target_limit, self.config.max_file_mb * MB)
        if attachment.size is not None and attachment.size > limit:
            raise AttachmentUnavailable("file_too_large")
        return await download(
            self.session, attachment.locator, limit, {"cdn.discordapp.com", "media.discordapp.net"}
        )
