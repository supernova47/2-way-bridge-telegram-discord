from __future__ import annotations

import asyncio
import contextlib
import logging

import aiohttp
import discord
from telegram import Bot
from telegram.error import Conflict, Forbidden, InvalidToken, NetworkError, RetryAfter
from telegram.request import HTTPXRequest

from .config import Config, ConfigError
from .engine import Engine
from .ingest import Ingestor
from .store import Store
from .transports import DiscordTransport, TelegramTransport

log = logging.getLogger("bridge")


class BridgeClient(discord.Client):
    def __init__(self, ingestor: Ingestor):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none(), max_messages=None)
        self.ingestor = ingestor

    async def on_message(self, message):
        self.ingestor.discord_message(message, self.user.id)

    async def on_raw_message_edit(self, payload):
        self.ingestor.discord_edit(payload)

    async def on_raw_message_delete(self, payload):
        self.ingestor.discord_delete(payload.channel_id, payload.message_id)

    async def on_raw_bulk_message_delete(self, payload):
        for message_id in sorted(payload.message_ids):
            self.ingestor.discord_delete(payload.channel_id, message_id)

    async def on_ready(self):
        log.info("Discord connected")

    async def on_disconnect(self):
        log.warning("Discord disconnected; waiting for SDK reconnect")

    async def on_error(self, event_method, *args, **kwargs):
        # Stop on intake persistence failures instead of silently losing accepted events.
        log.error("Discord intake failed; stopping for restart")
        await self.close()


async def validate_remote(config: Config, client: BridgeClient, bot: Bot, session):
    if (await bot.get_webhook_info()).url:
        raise ConfigError("Telegram: уже настроен webhook; удалите его перед запуском polling-моста.")
    me = await bot.get_me()
    for index, pair in enumerate(config.pairs, 1):
        channel = client.get_channel(pair.discord_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise ConfigError(
                f"Пара {index}: Discord-канал не найден или не является текстовым каналом/веткой."
            )
        permissions = channel.permissions_for(channel.guild.me)
        required = [permissions.view_channel, permissions.read_message_history, permissions.attach_files]
        required.append(
            permissions.send_messages_in_threads
            if isinstance(channel, discord.Thread)
            else permissions.send_messages
        )
        if not all(required):
            raise ConfigError(f"Пара {index}: не хватает прав Discord View/Read History/Send/Attach Files.")
        if pair.discord_webhook_env:
            webhook = await discord.Webhook.from_url(config.webhooks[pair.name], session=session).fetch()
            expected_channel = channel.parent_id if isinstance(channel, discord.Thread) else channel.id
            if webhook.channel_id != expected_channel:
                raise ConfigError(f"Пара {index}: webhook принадлежит другому Discord-каналу.")
        chat = await bot.get_chat(pair.telegram_chat_id)
        if chat.type not in {"group", "supergroup"}:
            raise ConfigError(f"Пара {index}: Telegram ID должен принадлежать группе или супергруппе.")
        member = await bot.get_chat_member(pair.telegram_chat_id, me.id)
        if member.status in {"left", "kicked"}:
            raise ConfigError(f"Пара {index}: Telegram-бот отсутствует в группе.")
        admin = member.status in {"administrator", "creator"}
        if not admin and not me.can_read_all_group_messages:
            raise ConfigError(
                "Telegram: отключите Group Privacy у BotFather и заново добавьте бота в группу."
            )
        if not admin:
            rights = member if member.status == "restricted" else chat.permissions
            if rights and not all(
                getattr(rights, k, False)
                for k in ("can_send_messages", "can_send_documents", "can_send_photos")
            ):
                raise ConfigError(
                    f"Пара {index}: Telegram-боту нужны права отправки текста, фото и документов."
                )
        if pair.telegram_thread_id is not None and not chat.is_forum:
            raise ConfigError(f"Пара {index}: тема задана для Telegram-группы без режима форума.")
        # No IDs, names of people/chats, URLs or tokens in logs.
        log.info(
            "pair=%s Discord=text Telegram=group webhook=%s topic=%s configured",
            index,
            bool(pair.discord_webhook_env),
            pair.telegram_thread_id is not None,
        )
    log.info("Telegram connected; configured pairs=%s", len(config.pairs))


async def telegram_poll(bot: Bot, ingestor: Ingestor, store: Store):
    failures = 0
    while True:
        try:
            updates = await bot.get_updates(
                offset=store.get_offset(), timeout=30, allowed_updates=["message", "edited_message"]
            )
            for update in updates:
                # Acknowledge only after acceptance into SQLite, including on restarts.
                ingestor.telegram_update(update, bot.id)
                store.set_offset(update.update_id + 1)
            failures = 0
        except RetryAfter as exc:
            delay = exc.retry_after
            await asyncio.sleep(delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay))
        except (Conflict, Forbidden, InvalidToken):
            raise ConfigError(
                "Telegram polling: конфликт другого процесса, токен или доступ к группе."
            ) from None
        except NetworkError:
            failures += 1
            if failures >= 10:
                raise ConfigError(
                    "Telegram недоступен после 10 попыток; нужен перезапуск процесса."
                ) from None
            log.warning("Telegram polling temporarily unavailable; attempt=%s", failures)
            await asyncio.sleep(min(60, 2**failures))


async def heartbeat(store: Store):
    while True:
        await asyncio.sleep(60)
        states = store.status()
        log.info(
            "health pending=%s failed=%s uncertain=%s",
            states.get("pending", 0),
            states.get("failed", 0),
            states.get("uncertain", 0),
        )


async def run(config: Config, store: Store):
    store.recover()
    ingestor = Ingestor(config, store)
    request = HTTPXRequest(
        connection_pool_size=8, read_timeout=45, write_timeout=60, connect_timeout=10, media_write_timeout=60
    )
    poll_request = HTTPXRequest(read_timeout=45, connect_timeout=10)
    bot = Bot(config.telegram_token, request=request, get_updates_request=poll_request)
    client = BridgeClient(ingestor)
    tasks = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90, sock_connect=10)) as session:
        try:
            await bot.initialize()
            async with client:
                gateway = asyncio.create_task(client.start(config.discord_token, reconnect=True))
                tasks.append(gateway)
                ready = asyncio.create_task(client.wait_until_ready())
                tasks.append(ready)
                done, _ = await asyncio.wait(
                    [gateway, ready], timeout=60, return_when=asyncio.FIRST_COMPLETED
                )
                if gateway in done:
                    await gateway
                    raise ConfigError("Discord отключился при запуске.")
                if ready not in done:
                    raise ConfigError("Discord: время ожидания подключения истекло.")
                await validate_remote(config, client, bot, session)
                engine = Engine(
                    config,
                    store,
                    {
                        "discord": DiscordTransport(client, session, config),
                        "telegram": TelegramTransport(bot, session, config),
                    },
                )
                tasks.extend(
                    asyncio.create_task(engine.worker(pair, source))
                    for pair in config.pairs
                    for source in ("discord", "telegram")
                    for _ in range(2)
                )
                tasks.append(asyncio.create_task(telegram_poll(bot, ingestor, store)))
                tasks.append(asyncio.create_task(heartbeat(store)))
                log.info("Bridge ready; durable queue active")
                done, _ = await asyncio.wait(
                    [t for t in tasks if t is not ready], return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    await task
                raise ConfigError("Задача моста остановилась; нужен перезапуск процесса.")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await client.close()
            with contextlib.suppress(Exception):
                await bot.shutdown()
