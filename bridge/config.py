from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Safe to show to the operator; never contains configuration values."""


@dataclass(frozen=True)
class Pair:
    name: str
    discord_channel_id: int
    telegram_chat_id: int
    telegram_thread_id: int | None = None
    discord_webhook_env: str | None = None

    def chat(self, platform: str) -> int:
        return self.discord_channel_id if platform == "discord" else self.telegram_chat_id


@dataclass(frozen=True)
class Config:
    pairs: tuple[Pair, ...]
    database: Path
    discord_token: str = field(repr=False, default="")
    telegram_token: str = field(repr=False, default="")
    ignore_bots: bool = True
    command_prefixes: tuple[str, ...] = ("/", "!")
    max_file_mb: int = 20
    max_attempts: int = 5
    retry_base_seconds: float = 2
    retry_uncertain: bool = False
    telegram_interval_seconds: float = 3.2
    webhooks: dict[str, str] = field(default_factory=dict, repr=False)
    avatars: dict[str, str] = field(default_factory=dict, repr=False)

    def route(self, platform: str, chat_id: int, thread_id: int | None = None) -> Pair | None:
        for pair in self.pairs:
            if pair.chat(platform) != chat_id:
                continue
            if platform == "telegram" and pair.telegram_thread_id != thread_id:
                continue
            return pair
        return None

    def is_command(self, text: str) -> bool:
        return text.lstrip().startswith(self.command_prefixes)


def load_config(path: Path, *, require_tokens: bool = True) -> Config:
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except FileNotFoundError:
        raise ConfigError(
            "Не найден файл конфигурации; скопируйте config.example.toml в config.toml."
        ) from None
    except (OSError, tomllib.TOMLDecodeError):
        raise ConfigError("Не удалось прочитать TOML-конфигурацию; проверьте синтаксис и доступ.") from None
    allowed = set(Config.__dataclass_fields__) - {"discord_token", "telegram_token", "webhooks"}
    if set(raw) - allowed:
        raise ConfigError("Неизвестный параметр конфигурации; сверьтесь с config.example.toml.")
    pairs = []
    seen_names, seen_discord, seen_telegram = set(), set(), set()
    if not isinstance(raw.get("pairs"), list) or not 1 <= len(raw["pairs"]) <= 32:
        raise ConfigError("Укажите от 1 до 32 секций [[pairs]].")
    for item in raw["pairs"]:
        if not isinstance(item, dict) or set(item) - set(Pair.__dataclass_fields__):
            raise ConfigError("Неизвестный параметр в [[pairs]].")
        try:
            pair = Pair(**item)
        except TypeError:
            raise ConfigError("В каждой паре нужны name, discord_channel_id и telegram_chat_id.") from None
        if not isinstance(pair.name, str) or not pair.name or len(pair.name) > 64:
            raise ConfigError("name пары должен быть строкой от 1 до 64 символов.")
        if type(pair.discord_channel_id) is not int or not 0 < pair.discord_channel_id < 2**64:
            raise ConfigError("discord_channel_id должен быть положительным целым ID.")
        if type(pair.telegram_chat_id) is not int or not -(2**52) < pair.telegram_chat_id < 0:
            raise ConfigError("telegram_chat_id группы должен быть отрицательным целым ID.")
        if pair.telegram_thread_id is not None and (
            type(pair.telegram_thread_id) is not int or pair.telegram_thread_id <= 0
        ):
            raise ConfigError("telegram_thread_id должен быть положительным целым ID.")
        if pair.discord_webhook_env is not None and (
            not isinstance(pair.discord_webhook_env, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", pair.discord_webhook_env)
        ):
            raise ConfigError("discord_webhook_env должен содержать имя переменной окружения A–Z, 0–9, _.")
        tg = (pair.telegram_chat_id, pair.telegram_thread_id)
        if pair.name in seen_names or pair.discord_channel_id in seen_discord or tg in seen_telegram:
            raise ConfigError("Имена пар и конечные точки не должны повторяться.")
        seen_names.add(pair.name)
        seen_discord.add(pair.discord_channel_id)
        seen_telegram.add(tg)
        pairs.append(pair)
    values = {k: v for k, v in raw.items() if k not in {"pairs", "database", "avatars"}}
    avatars = raw.get("avatars", {})
    if not isinstance(avatars, dict):
        raise ConfigError("avatars должен быть таблицей Telegram ID → публичный HTTPS URL аватара.")
    for key, value in avatars.items():
        if not isinstance(key, str) or not key.isdigit() or not isinstance(value, str):
            raise ConfigError("avatars: ключ — числовой Telegram ID, значение — публичный HTTPS URL.")
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.hostname == "api.telegram.org"
        ):
            raise ConfigError(
                "avatars: нужен публичный HTTPS URL без пароля, query и ссылки Telegram Bot API."
            )
    webhooks = {}
    for pair in pairs:
        if pair.discord_webhook_env:
            url = os.environ.get(pair.discord_webhook_env, "").strip()
            if not url and require_tokens:
                raise ConfigError("Не задана переменная окружения webhook: " + pair.discord_webhook_env)
            if url and not re.fullmatch(r"https://discord\.com/api(?:/v10)?/webhooks/\d+/[\w-]+", url):
                raise ConfigError("Webhook должен быть URL Discord Incoming Webhook без query-параметров.")
            webhooks[pair.name] = url
    for key in ("ignore_bots", "retry_uncertain"):
        if key in values and type(values[key]) is not bool:
            raise ConfigError(f"{key} должен быть true или false.")
    prefixes = values.get("command_prefixes", ["/", "!"])
    if not isinstance(prefixes, list) or any(not isinstance(p, str) or not p for p in prefixes):
        raise ConfigError("command_prefixes должен быть массивом непустых строк.")
    values["command_prefixes"] = tuple(prefixes)
    for key, low, high in (
        ("max_file_mb", 1, 50),
        ("max_attempts", 1, 10),
        ("retry_base_seconds", 0.1, 300),
        ("telegram_interval_seconds", 3, 60),
    ):
        if key in values and (
            type(values[key]) not in (int, float)
            or not low <= values[key] <= high
            or (key in {"max_file_mb", "max_attempts"} and type(values[key]) is not int)
        ):
            raise ConfigError(f"{key}: допустимый диапазон {low}–{high}.")
    db = raw.get("database", "data/bridge.sqlite3")
    if not isinstance(db, str) or not db.strip():
        raise ConfigError("database должен быть непустым путём.")
    tokens = {key: os.environ.get(key, "").strip() for key in ("DISCORD_TOKEN", "TELEGRAM_TOKEN")}
    if require_tokens:
        missing = [key for key, value in tokens.items() if not value]
        if missing:
            raise ConfigError("Не заданы переменные окружения: " + ", ".join(missing))
    return Config(
        pairs=tuple(pairs),
        database=(path.resolve().parent / db).resolve(),
        discord_token=tokens["DISCORD_TOKEN"],
        telegram_token=tokens["TELEGRAM_TOKEN"],
        **values,
        webhooks=webhooks,
        avatars=avatars,
    )
