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
class UserMapping:
    telegram_user_id: int
    discord_user_id: int
    telegram_name: str
    discord_name: str


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
    user_mappings: tuple[UserMapping, ...] = ()

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

    def mapped_name(self, source_platform: str, user_id: int | None) -> str | None:
        """Return the configured name for this user on the opposite platform."""
        if user_id is None:
            return None
        if source_platform == "telegram":
            return next(
                (
                    mapping.discord_name
                    for mapping in self.user_mappings
                    if mapping.telegram_user_id == user_id
                ),
                None,
            )
        if source_platform == "discord":
            return next(
                (
                    mapping.telegram_name
                    for mapping in self.user_mappings
                    if mapping.discord_user_id == user_id
                ),
                None,
            )
        return None


def load_config(path: Path, *, require_tokens: bool = True) -> Config:
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except FileNotFoundError:
        raise ConfigError("Configuration file not found; copy config.example.toml to config.toml.") from None
    except (OSError, tomllib.TOMLDecodeError):
        raise ConfigError(
            "Could not read TOML configuration; check its syntax and file permissions."
        ) from None
    allowed = set(Config.__dataclass_fields__) - {"discord_token", "telegram_token", "webhooks"}
    if set(raw) - allowed:
        raise ConfigError("Unknown configuration option; compare your file with config.example.toml.")
    pairs = []
    seen_names, seen_discord, seen_telegram = set(), set(), set()
    if not isinstance(raw.get("pairs"), list) or not 1 <= len(raw["pairs"]) <= 32:
        raise ConfigError("Configure between 1 and 32 [[pairs]] sections.")
    for item in raw["pairs"]:
        if not isinstance(item, dict) or set(item) - set(Pair.__dataclass_fields__):
            raise ConfigError("Unknown option in a [[pairs]] section.")
        try:
            pair = Pair(**item)
        except TypeError:
            raise ConfigError("Each pair requires name, discord_channel_id, and telegram_chat_id.") from None
        if not isinstance(pair.name, str) or not pair.name or len(pair.name) > 64:
            raise ConfigError("Pair name must be a string between 1 and 64 characters.")
        if type(pair.discord_channel_id) is not int or not 0 < pair.discord_channel_id < 2**64:
            raise ConfigError("discord_channel_id must be a positive integer ID.")
        if type(pair.telegram_chat_id) is not int or not -(2**52) < pair.telegram_chat_id < 0:
            raise ConfigError("telegram_chat_id must be a negative integer group ID.")
        if pair.telegram_thread_id is not None and (
            type(pair.telegram_thread_id) is not int or pair.telegram_thread_id <= 0
        ):
            raise ConfigError("telegram_thread_id must be a positive integer ID.")
        if pair.discord_webhook_env is not None and (
            not isinstance(pair.discord_webhook_env, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", pair.discord_webhook_env)
        ):
            raise ConfigError(
                "discord_webhook_env must be an environment variable name using A-Z, 0-9, and _."
            )
        tg = (pair.telegram_chat_id, pair.telegram_thread_id)
        if pair.name in seen_names or pair.discord_channel_id in seen_discord or tg in seen_telegram:
            raise ConfigError("Pair names and endpoints must be unique.")
        seen_names.add(pair.name)
        seen_discord.add(pair.discord_channel_id)
        seen_telegram.add(tg)
        pairs.append(pair)
    values = {k: v for k, v in raw.items() if k not in {"pairs", "database", "avatars", "user_mappings"}}
    user_mappings = raw.get("user_mappings", [])
    if not isinstance(user_mappings, list) or len(user_mappings) > 1000:
        raise ConfigError("user_mappings must be an array with at most 1000 entries.")
    parsed_mappings = []
    telegram_users, discord_users = set(), set()
    for item in user_mappings:
        if not isinstance(item, dict) or set(item) != set(UserMapping.__dataclass_fields__):
            raise ConfigError(
                "Each [[user_mappings]] entry requires telegram_user_id, discord_user_id, "
                "telegram_name, and discord_name."
            )
        try:
            mapping = UserMapping(**item)
        except TypeError:
            raise ConfigError("Invalid [[user_mappings]] entry.") from None
        if (
            type(mapping.telegram_user_id) is not int
            or not 0 < mapping.telegram_user_id < 2**64
            or type(mapping.discord_user_id) is not int
            or not 0 < mapping.discord_user_id < 2**64
        ):
            raise ConfigError("Mapped Telegram and Discord user IDs must be positive integers.")
        for name in (mapping.telegram_name, mapping.discord_name):
            if not isinstance(name, str) or not name.strip() or len(name) > 80:
                raise ConfigError("Mapped names must contain 1 to 80 characters.")
        if mapping.telegram_user_id in telegram_users or mapping.discord_user_id in discord_users:
            raise ConfigError("Each Telegram and Discord user ID can appear in only one mapping.")
        telegram_users.add(mapping.telegram_user_id)
        discord_users.add(mapping.discord_user_id)
        parsed_mappings.append(mapping)
    avatars = raw.get("avatars", {})
    if not isinstance(avatars, dict):
        raise ConfigError("avatars must be a table mapping Telegram user IDs to public HTTPS avatar URLs.")
    for key, value in avatars.items():
        if not isinstance(key, str) or not key.isdigit() or not isinstance(value, str):
            raise ConfigError("avatars keys must be numeric Telegram user IDs and values public HTTPS URLs.")
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
                "Avatar URLs must be public HTTPS URLs without credentials, query strings, or "
                "Telegram Bot API links."
            )
    webhooks = {}
    for pair in pairs:
        if pair.discord_webhook_env:
            url = os.environ.get(pair.discord_webhook_env, "").strip()
            if not url and require_tokens:
                raise ConfigError("Missing webhook environment variable: " + pair.discord_webhook_env)
            if url and not re.fullmatch(r"https://discord\.com/api(?:/v10)?/webhooks/\d+/[\w-]+", url):
                raise ConfigError("Webhook must be a Discord Incoming Webhook URL without query parameters.")
            webhooks[pair.name] = url
    for key in ("ignore_bots", "retry_uncertain"):
        if key in values and type(values[key]) is not bool:
            raise ConfigError(f"{key} must be true or false.")
    prefixes = values.get("command_prefixes", ["/", "!"])
    if not isinstance(prefixes, list) or any(not isinstance(p, str) or not p for p in prefixes):
        raise ConfigError("command_prefixes must be an array of non-empty strings.")
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
            raise ConfigError(f"{key}: allowed range is {low}–{high}.")
    db = raw.get("database", "data/bridge.sqlite3")
    if not isinstance(db, str) or not db.strip():
        raise ConfigError("database must be a non-empty path.")
    tokens = {key: os.environ.get(key, "").strip() for key in ("DISCORD_TOKEN", "TELEGRAM_TOKEN")}
    if require_tokens:
        missing = [key for key, value in tokens.items() if not value]
        if missing:
            raise ConfigError("Missing environment variables: " + ", ".join(missing))
    return Config(
        pairs=tuple(pairs),
        database=(path.resolve().parent / db).resolve(),
        discord_token=tokens["DISCORD_TOKEN"],
        telegram_token=tokens["TELEGRAM_TOKEN"],
        **values,
        webhooks=webhooks,
        avatars=avatars,
        user_mappings=tuple(parsed_mappings),
    )
