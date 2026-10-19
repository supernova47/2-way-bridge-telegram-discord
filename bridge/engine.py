from __future__ import annotations

import asyncio
import json
import logging
import random

from .config import Config, Pair
from .formatting import chunks, render, visible
from .model import AttachmentUnavailable, Message, PermanentError, RetryableError, UncertainError
from .store import Store, operation_tag

log = logging.getLogger("bridge")


class Engine:
    def __init__(self, config: Config, store: Store, transports: dict):
        self.config, self.store, self.transports = config, store, transports

    async def operation(
        self, job, pair, target, kind, slot, content="", *, message_id=None, reply_id=None, attachment=None
    ):
        tag = operation_tag(kind, slot, content)
        prior = self.store.operation(job["id"], tag)
        if prior and prior["state"] == "done":
            return int(prior["result"])
        if prior and prior["state"] == "inflight" and kind == "send":
            raise UncertainError("interrupted_send")
        transport = self.transports[target]
        # Download before marking an outbound send inflight: downloads are safe to repeat.
        data = None
        if attachment:
            data = await self.transports[job["platform"]].download(attachment, transport.file_limit(pair))
        self.store.begin_operation(job["id"], tag, kind, slot, content)
        try:
            if kind == "send":
                message_id = await transport.send(
                    pair,
                    content,
                    reply_id=reply_id,
                    attachment=attachment,
                    data=data,
                    nonce=f"{job['id']}:{tag}",
                    source_message=Message.from_dict(json.loads(job["payload"])),
                )
            elif kind == "edit":
                await transport.edit(pair, message_id, content)
            else:
                await transport.delete(pair, message_id)
        except (RetryableError, PermanentError, AttachmentUnavailable):
            self.store.reset_operation(job["id"], tag)
            raise
        except UncertainError:
            raise
        except asyncio.CancelledError:
            raise  # recovery will quarantine inflight sends on the next start
        except Exception:
            if kind == "send":
                raise UncertainError("unexpected_send_failure") from None
            self.store.reset_operation(job["id"], tag)
            raise
        self.store.finish_operation(job, tag, kind, slot, target, pair.chat(target), message_id, content)
        return message_id

    async def deliver(self, job, pair: Pair):
        message = Message.from_dict(json.loads(job["payload"]))
        target = "telegram" if message.platform == "discord" else "discord"
        existing = self.store.copies(job["root"])
        if job["kind"] == "delete":
            for slot, copy in existing.items():
                await self.operation(job, pair, target, "delete", slot, message_id=int(copy["message_id"]))
            return
        reply_id = self.store.reply_target(pair.name, message, target)
        native_reply = reply_id is not None and not (target == "discord" and pair.discord_webhook_env)
        texts = render(
            message,
            target,
            reply_known=native_reply,
            profile_attribution=bool(target == "discord" and pair.discord_webhook_env),
        )
        desired = set()
        first_id = None
        for index, text in enumerate(texts):
            slot = f"text:{index}"
            desired.add(slot)
            copy = existing.get(slot)
            if copy and copy["content"] == text:
                result = int(copy["message_id"])
            else:
                result = await self.operation(
                    job,
                    pair,
                    target,
                    "edit" if copy else "send",
                    slot,
                    text,
                    message_id=int(copy["message_id"]) if copy else None,
                    reply_id=reply_id if index == 0 else first_id,
                )
            if first_id is None:
                first_id = result
        for attachment in message.attachments:
            slot = f"file:{attachment.key}"
            desired.add(slot)
            if slot in existing:
                continue
            # Fallback notices are mapped too; retries/edits do not generate duplicates.
            try:
                await self.operation(
                    job, pair, target, "send", slot, "", reply_id=first_id, attachment=attachment
                )
            except AttachmentUnavailable:
                name = visible(attachment.name, single_line=True)[:180]
                text = f"[Файл «{name}» не передан: превышен лимит, формат отклонён или файл недоступен.]"
                text = chunks(text, 1000, discord=target == "discord")[0]
                await self.operation(job, pair, target, "send", slot, text, reply_id=first_id)
        for slot, copy in existing.items():
            if slot not in desired:
                await self.operation(job, pair, target, "delete", slot, message_id=int(copy["message_id"]))

    async def process_one(self, pair: Pair, source: str) -> bool:
        job = self.store.next_job(pair.name, source)
        if job is None:
            return False
        attempts = job["attempts"] + 1
        try:
            await self.deliver(job, pair)
        except RetryableError as exc:
            state = "failed" if attempts >= self.config.max_attempts else "pending"
            delay = max(exc.delay, min(300, self.config.retry_base_seconds * 2 ** (attempts - 1)))
            delay += random.uniform(0, min(1, delay / 4))
            self.store.finish_job(job["id"], state, attempts=attempts, delay=delay, error=exc.code)
            log.warning("delivery job=%s state=%s code=%s", job["id"], state, exc.code)
        except UncertainError:
            if self.config.retry_uncertain and attempts < self.config.max_attempts:
                self.store.finish_job(job["id"], "uncertain", attempts=attempts, error="ambiguous_send")
                self.store.retry(job["id"], allow_uncertain=True)
                self.store.finish_job(
                    job["id"],
                    "pending",
                    attempts=attempts,
                    delay=min(300, self.config.retry_base_seconds * 2 ** (attempts - 1)),
                    error="ambiguous_send",
                )
            else:
                self.store.finish_job(job["id"], "uncertain", attempts=attempts, error="ambiguous_send")
            log.error("delivery job=%s code=ambiguous_send; inspect local status", job["id"])
        except PermanentError as exc:
            self.store.finish_job(job["id"], "failed", attempts=attempts, error=str(exc))
            log.error("delivery job=%s state=failed code=%s", job["id"], exc)
        except Exception:
            self.store.finish_job(job["id"], "failed", attempts=attempts, error="internal_error")
            log.error("delivery job=%s code=internal_error", job["id"])
        else:
            self.store.finish_job(job["id"], "done", attempts=attempts)
        return True

    async def worker(self, pair: Pair, source: str):
        while True:
            if not await self.process_one(pair, source):
                await asyncio.sleep(0.25)
