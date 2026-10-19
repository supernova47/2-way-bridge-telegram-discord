from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .config import ConfigError, UserMapping, load_config


def add_mapping(path: Path, mapping: UserMapping) -> int:
    """Validate a mapping with the normal config loader, then append it atomically."""
    path = path.resolve()
    config = load_config(path, require_tokens=False)
    if len(config.user_mappings) >= 1000:
        raise ConfigError("The configuration already contains the maximum of 1000 mappings.")

    block = "\n[[user_mappings]]\n"
    block += f"telegram_user_id = {mapping.telegram_user_id}\n"
    block += f"discord_user_id = {mapping.discord_user_id}\n"
    block += f"telegram_name = {json.dumps(mapping.telegram_name, ensure_ascii=False)}\n"
    block += f"discord_name = {json.dumps(mapping.discord_name, ensure_ascii=False)}\n"

    try:
        original = path.read_text(encoding="utf-8")
    except OSError:
        raise ConfigError("Could not read the configuration file; check its permissions.") from None
    separator = "\n" if original.endswith(("\n", "\r")) else "\n\n"
    candidate = original + separator + block.lstrip("\n")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            stream.write(candidate)
        # Reuse all production validation, including duplicate IDs and TOML syntax.
        updated = load_config(temp_path, require_tokens=False)
        if len(updated.user_mappings) != len(config.user_mappings) + 1:
            raise ConfigError("The mapping was not added; configuration validation failed.")
        os.replace(temp_path, path)
        return len(updated.user_mappings)
    except ConfigError:
        raise
    except OSError:
        raise ConfigError("Could not safely update the configuration file; check its permissions.") from None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def prompt_mapping() -> UserMapping:
    print("Get IDs while the bridge is running: /bridge_id in Telegram and !bridge_id in Discord.")
    telegram_user_id = _prompt_id("Telegram user ID")
    discord_user_id = _prompt_id("Discord user ID")
    telegram_name = _prompt_name("Name to show in Telegram for this person")
    discord_name = _prompt_name("Name to show in Discord for this person")
    return UserMapping(telegram_user_id, discord_user_id, telegram_name, discord_name)


def _prompt_id(label: str) -> int:
    value = input(f"{label}: ").strip()
    if not value.isdecimal() or int(value) <= 0 or int(value) >= 2**64:
        raise ConfigError(f"{label} must be a positive numeric ID; run mapping add again.")
    return int(value)


def _prompt_name(label: str) -> str:
    value = input(f"{label}: ").strip()
    if not value or len(value) > 80:
        raise ConfigError(f"{label} must contain 1 to 80 characters; run mapping add again.")
    return value
