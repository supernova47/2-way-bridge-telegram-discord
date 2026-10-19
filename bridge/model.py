from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True).encode()).hexdigest()


@dataclass
class Attachment:
    key: str
    name: str
    size: int | None
    locator: str  # Discord CDN URL or Telegram file_id; never logged.
    media_type: str = "file"


@dataclass
class Message:
    platform: str
    chat_id: int
    message_id: int
    author: str
    channel: str
    text: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    reply_id: int | None = None
    quote: str = ""
    thread_id: int | None = None
    notice: str = ""
    author_id: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> Message:
        value = dict(value)
        value["attachments"] = [Attachment(**a) for a in value.get("attachments", [])]
        return cls(**value)

    def source_key(self, pair: str) -> str:
        return f"{pair}:{self.platform}:{self.chat_id}:{self.message_id}"


class RetryableError(Exception):
    def __init__(self, code: str = "temporary", delay: float = 0):
        super().__init__(code)
        self.code, self.delay = code, delay


class PermanentError(Exception):
    """Contains only a fixed safe error code."""


class UncertainError(Exception):
    """A send may have succeeded. Do not blindly repeat it."""


class AttachmentUnavailable(Exception):
    """An attachment cannot be transferred; send an explicit notice instead."""
