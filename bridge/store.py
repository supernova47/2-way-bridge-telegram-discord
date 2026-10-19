from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .config import ConfigError
from .model import Message, digest


class Store:
    """Single-process SQLite inbox/outbox. Transactions never span network awaits."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                root TEXT PRIMARY KEY, pair TEXT NOT NULL, platform TEXT NOT NULL,
                chat_id TEXT NOT NULL, message_id TEXT NOT NULL, payload TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0,
                UNIQUE(pair, platform, chat_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL,
                root TEXT NOT NULL REFERENCES messages(root), pair TEXT NOT NULL,
                platform TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0, error TEXT, created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(pair, platform, state, next_at, id);
            CREATE TABLE IF NOT EXISTS copies (
                root TEXT NOT NULL REFERENCES messages(root), slot TEXT NOT NULL,
                platform TEXT NOT NULL, chat_id TEXT NOT NULL, message_id TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(root, slot)
            );
            CREATE INDEX IF NOT EXISTS copies_lookup ON copies(platform, chat_id, message_id);
            CREATE TABLE IF NOT EXISTS operations (
                job_id INTEGER NOT NULL REFERENCES jobs(id), tag TEXT NOT NULL,
                kind TEXT NOT NULL, slot TEXT NOT NULL, content TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT,
                PRIMARY KEY(job_id, tag)
            );
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def bind_routes(self, pairs):
        """Do not deliver an existing queue to an accidentally reconfigured destination."""
        active = {
            row[0]
            for row in self.db.execute("SELECT DISTINCT pair FROM jobs WHERE state NOT IN ('done','skipped')")
        }
        if active - {pair.name for pair in pairs}:
            raise ConfigError(
                "The database has unfinished jobs for a removed pair; restore its configuration first."
            )
        with self.db:
            for pair in pairs:
                key = "route:" + pair.name
                signature = digest(
                    [
                        pair.discord_channel_id,
                        pair.telegram_chat_id,
                        pair.telegram_thread_id,
                        pair.discord_webhook_env,
                    ]
                )
                old = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
                if old and old[0] != signature:
                    raise ConfigError("An existing pair's endpoints changed; use a new database.")
                self.db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)", (key, signature))

    def recover(self):
        with self.db:
            # A process can die between remote acceptance and committing the returned ID.
            self.db.execute("""
                UPDATE jobs SET state='uncertain', error='interrupted_send'
                WHERE id IN (SELECT job_id FROM operations WHERE kind='send' AND state='inflight')
                AND state IN ('running', 'pending')
            """)
            self.db.execute("UPDATE jobs SET state='pending' WHERE state='running'")

    def accept(self, pair: str, message: Message, kind: str, revision: str = "") -> bool:
        root = message.source_key(pair)
        event_key = f"{root}:{kind}:{revision}"
        payload = json.dumps(message.to_dict(), ensure_ascii=True)
        with self.db:
            old = self.db.execute("SELECT deleted FROM messages WHERE root=?", (root,)).fetchone()
            if kind != "create" and (old is None or old["deleted"]):
                return False
            if kind == "create" and old is not None:
                return False
            if self.db.execute("SELECT 1 FROM jobs WHERE event_key=?", (event_key,)).fetchone():
                return False
            self.db.execute(
                """
                INSERT INTO messages(root,pair,platform,chat_id,message_id,payload,deleted)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(root) DO UPDATE SET
                payload=excluded.payload,deleted=excluded.deleted
            """,
                (
                    root,
                    pair,
                    message.platform,
                    str(message.chat_id),
                    str(message.message_id),
                    payload,
                    int(kind == "delete"),
                ),
            )
            self.db.execute(
                """
                INSERT INTO jobs(event_key,root,pair,platform,kind,payload,created_at)
                VALUES(?,?,?,?,?,?,?)
            """,
                (event_key, root, pair, message.platform, kind, payload, time.time()),
            )
        return True

    def snapshot(self, pair: str, platform: str, chat_id: int, message_id: int) -> Message | None:
        row = self.db.execute(
            """
            SELECT payload FROM messages WHERE pair=? AND platform=? AND chat_id=? AND message_id=?
        """,
            (pair, platform, str(chat_id), str(message_id)),
        ).fetchone()
        return Message.from_dict(json.loads(row[0])) if row else None

    def next_job(self, pair: str, platform: str):
        # Retrying a message blocks its own edits/deletion, not other conversations.
        row = self.db.execute(
            """
            SELECT j.* FROM jobs j WHERE pair=? AND platform=? AND state='pending' AND next_at<=?
            AND NOT EXISTS (
                SELECT 1 FROM jobs older WHERE older.root=j.root AND older.id<j.id
                AND older.state IN ('pending','running','uncertain','failed')
            ) ORDER BY id LIMIT 1
        """,
            (pair, platform, time.time()),
        ).fetchone()
        if row:
            with self.db:
                self.db.execute("UPDATE jobs SET state='running' WHERE id=?", (row["id"],))
        return row

    def finish_job(self, job_id: int, state: str, *, attempts: int = 0, delay: float = 0, error=None):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state=?,attempts=?,next_at=?,error=? WHERE id=?",
                (state, attempts, time.time() + delay, error, job_id),
            )

    def copies(self, root: str):
        return {
            row["slot"]: row
            for row in self.db.execute("SELECT * FROM copies WHERE root=? AND active=1", (root,))
        }

    def reply_target(self, pair: str, message: Message, target: str) -> int | None:
        if message.reply_id is None:
            return None
        row = self.db.execute(
            """
            SELECT root,platform,message_id,deleted FROM messages
            WHERE pair=? AND platform=? AND chat_id=? AND message_id=?
        """,
            (pair, message.platform, str(message.chat_id), str(message.reply_id)),
        ).fetchone()
        if row is None:
            row = self.db.execute(
                """
                SELECT m.root,m.platform,m.message_id,m.deleted FROM copies c
                JOIN messages m ON m.root=c.root WHERE m.pair=? AND c.platform=?
                AND c.chat_id=? AND c.message_id=? AND c.active=1
            """,
                (pair, message.platform, str(message.chat_id), str(message.reply_id)),
            ).fetchone()
        if not row or row["deleted"]:
            return None
        if row["platform"] == target:
            return int(row["message_id"])
        copy = self.db.execute(
            """
            SELECT message_id FROM copies WHERE root=? AND platform=? AND active=1
            ORDER BY CASE WHEN slot='text:0' THEN 0 ELSE 1 END, slot LIMIT 1
        """,
            (row["root"], target),
        ).fetchone()
        return int(copy[0]) if copy else None

    def operation(self, job_id: int, tag: str):
        return self.db.execute("SELECT * FROM operations WHERE job_id=? AND tag=?", (job_id, tag)).fetchone()

    def begin_operation(self, job_id: int, tag: str, kind: str, slot: str, content: str):
        with self.db:
            self.db.execute(
                """
                INSERT INTO operations(job_id,tag,kind,slot,content,state) VALUES(?,?,?,?,?,'inflight')
                ON CONFLICT(job_id,tag) DO UPDATE SET state='inflight'
            """,
                (job_id, tag, kind, slot, content),
            )

    def finish_operation(
        self, job, tag: str, kind: str, slot: str, target: str, chat_id: int, message_id: int, content: str
    ):
        # Both the mapping and operation checkpoint commit atomically.
        with self.db:
            self.db.execute(
                "UPDATE operations SET state='done',result=? WHERE job_id=? AND tag=?",
                (str(message_id), job["id"], tag),
            )
            if kind == "delete":
                self.db.execute("UPDATE copies SET active=0 WHERE root=? AND slot=?", (job["root"], slot))
            else:
                self.db.execute(
                    """
                    INSERT INTO copies(root,slot,platform,chat_id,message_id,content,active)
                    VALUES(?,?,?,?,?,?,1) ON CONFLICT(root,slot) DO UPDATE SET
                    message_id=excluded.message_id,content=excluded.content,active=1
                """,
                    (job["root"], slot, target, str(chat_id), str(message_id), content),
                )

    def reset_operation(self, job_id: int, tag: str):
        with self.db:
            self.db.execute("UPDATE operations SET state='retry' WHERE job_id=? AND tag=?", (job_id, tag))

    def get_offset(self) -> int:
        row = self.db.execute("SELECT value FROM metadata WHERE key='telegram_offset'").fetchone()
        return int(row[0]) if row else 0

    def set_offset(self, offset: int):
        with self.db:
            self.db.execute(
                "INSERT INTO metadata(key,value) VALUES('telegram_offset',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(offset),),
            )

    def status(self):
        return {row[0]: row[1] for row in self.db.execute("SELECT state,count(*) FROM jobs GROUP BY state")}

    def problems(self):
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT id,state,attempts,error FROM jobs WHERE state IN ('failed','uncertain') ORDER BY id"
            )
        ]

    def retry(self, job_id: int, *, allow_uncertain: bool = False):
        row = self.db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row[0] not in {"failed", "uncertain"}:
            raise ConfigError("Job not found or does not need a retry.")
        if row[0] == "uncertain" and not allow_uncertain:
            raise ConfigError(
                "Uncertain delivery: check the destination chat first; use --allow-duplicate-risk to retry."
            )
        with self.db:
            self.db.execute("UPDATE operations SET state='retry' WHERE job_id=? AND state!='done'", (job_id,))
            self.db.execute(
                "UPDATE jobs SET state='pending',attempts=0,next_at=0,error=NULL WHERE id=?", (job_id,)
            )

    def resolve(self, job_id: int, delivered_id: int, pair):
        job = self.db.execute("SELECT * FROM jobs WHERE id=? AND state='uncertain'", (job_id,)).fetchone()
        op = self.db.execute(
            "SELECT * FROM operations WHERE job_id=? AND kind='send' AND state='inflight'", (job_id,)
        ).fetchone()
        if not job or not op or delivered_id <= 0:
            raise ConfigError("No uncertain delivery found, or the delivered message ID is invalid.")
        target = "telegram" if job["platform"] == "discord" else "discord"
        self.finish_operation(
            job, op["tag"], "send", op["slot"], target, pair.chat(target), delivered_id, op["content"]
        )
        self.finish_job(job_id, "pending")


def operation_tag(kind: str, slot: str, content: str = "") -> str:
    return f"{kind}:{slot}:{digest(content)[:16]}"
