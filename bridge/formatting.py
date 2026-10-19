from __future__ import annotations

import re
import unicodedata

from .model import Message


def visible(text: str, *, single_line: bool = False) -> str:
    # Keep Unicode/emoji, remove controls and bidi overrides used for attribution spoofing.
    text = "".join(
        c
        for c in text
        if c in "\n\t"
        or unicodedata.category(c) not in {"Cc", "Cs"}
        and c not in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    )
    return " ".join(text.split()) if single_line else text


def escape_discord(text: str) -> str:
    text = re.sub(r"([\\`*_{}\[\]()#+.!|>~\-])", r"\\\1", text)
    # allowed_mentions.none is also set on every API call.
    return text.replace("@", "@\u200b").replace("<", "<\u200b")


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def chunks(text: str, limit: int, *, discord: bool = False) -> list[str]:
    """Split by UTF-16 units *after* escaping, without cutting escape pairs or emoji."""
    result, current, size = [], [], 0
    for char in visible(text):
        token = escape_discord(char) if discord else char
        count = units(token)
        if size + count > limit:
            result.append("".join(current))
            current, size = [], 0
        current.append(token)
        size += count
    if current:
        result.append("".join(current))
    return result or ["(пустое сообщение)"]


def render(
    message: Message, target: str, *, reply_known: bool, profile_attribution: bool = False
) -> list[str]:
    platform = "Discord" if message.platform == "discord" else "Telegram"
    author = visible(message.author, single_line=True)[:100] or "Без имени"
    if profile_attribution:
        header = ""
    elif message.platform == "discord" and target == "telegram":
        header = f"[{platform}] {author}:\n"
    else:
        header = f"[{platform}] {author}: "
    body = message.text
    if message.reply_id is not None and not reply_known:
        quote = visible(message.quote, single_line=True)[:240] or "исходное сообщение недоступно"
        body = f"↪ Ответ: {quote}" + (f"\n{body}" if body else "")
    if message.notice:
        body = f"{body}\n{message.notice}" if body else message.notice
    if not body:
        return []
    # A repeated attribution on each part makes long messages understandable.
    escaped_header = escape_discord(header) if target == "discord" else header
    limit = (2000 if target == "discord" else 4096) - units(escaped_header)
    return [escaped_header + part for part in chunks(body, limit, discord=target == "discord")]
