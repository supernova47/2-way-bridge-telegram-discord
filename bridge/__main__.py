from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .locking import InstanceLock
from .store import Store


def configure_logging():
    # SDK HTTP errors may contain token-bearing Telegram/webhook URLs or message data.
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(logging.NullHandler())
    log = logging.getLogger("bridge")
    log.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def main() -> int:
    parser = argparse.ArgumentParser(description="Discord-Telegram bridge")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "check", help="Validate local configuration and required environment variables; no network"
    )
    sub.add_parser("run", help="Connect and run the bridge")
    sub.add_parser("required-env", help="Print only the environment variable names needed by the launcher")
    sub.add_parser("telegram-chats", help="List groups in pending Telegram updates; stop the bridge first")
    sub.add_parser("telegram-users", help="List user IDs in pending Telegram updates; stop the bridge first")
    mappings = sub.add_parser("mapping", help="Manage cross-platform user mappings")
    mapping_actions = mappings.add_subparsers(dest="mapping_action", required=True)
    mapping_actions.add_parser("list", help="List configured mappings")
    mapping_actions.add_parser("add", help="Interactively add a mapping; stop the bridge first")
    sub.add_parser("status", help="Show local queue counts and problem job IDs, without message contents")
    retry = sub.add_parser("retry", help="Retry a problem job while bridge is stopped")
    retry.add_argument("job_id", type=int)
    retry.add_argument("--allow-duplicate-risk", action="store_true")
    resolve = sub.add_parser("resolve", help="Record the verified ID of an uncertain delivery")
    resolve.add_argument("job_id", type=int)
    resolve.add_argument("delivered_id", type=int)
    args = parser.parse_args()
    configure_logging()
    log = logging.getLogger("bridge")
    try:
        if args.command in {"telegram-chats", "telegram-users"}:
            token = os.environ.get("TELEGRAM_TOKEN", "").strip()
            if not token:
                raise ConfigError("Missing environment variable: TELEGRAM_TOKEN.")
            from .discovery import telegram_chats, telegram_users

            if args.command == "telegram-chats":
                asyncio.run(telegram_chats(token))
            else:
                config = load_config(args.config, require_tokens=False)
                asyncio.run(telegram_users(token, config))
            return 0
        config = load_config(args.config, require_tokens=args.command in {"run", "check"})
        if args.command == "mapping":
            from .mappings import add_mapping, prompt_mapping

            if args.mapping_action == "list":
                if not config.user_mappings:
                    print("No user mappings configured.")
                for index, mapping in enumerate(config.user_mappings, 1):
                    print(
                        f"{index}. Telegram ID {mapping.telegram_user_id} ↔ Discord ID "
                        f"{mapping.discord_user_id}; Telegram name={mapping.telegram_name!r}; "
                        f"Discord name={mapping.discord_name!r}"
                    )
                return 0
            with InstanceLock(config.database):
                mapping = prompt_mapping()
                count = add_mapping(args.config, mapping)
            print(f"Mapping saved; configured mappings={count}. Restart the bridge to apply it.")
            return 0
        if args.command == "required-env":
            names = ["DISCORD_TOKEN", "TELEGRAM_TOKEN"]
            names.extend(pair.discord_webhook_env for pair in config.pairs if pair.discord_webhook_env)
            print("\n".join(dict.fromkeys(names)))
            return 0
        if args.command == "check":
            print(f"OK: configuration valid, pairs={len(config.pairs)}. No network connection tested.")
            return 0
        if args.command == "status":
            store = Store(config.database)
            try:
                print(json.dumps({"counts": store.status(), "problems": store.problems()}))
            finally:
                store.close()
            return 0
        with InstanceLock(config.database):
            store = Store(config.database)
            try:
                store.recover()
                store.bind_routes(config.pairs)
                if args.command == "retry":
                    store.retry(args.job_id, allow_uncertain=args.allow_duplicate_risk)
                    print("Job queued. Restart the bridge.")
                elif args.command == "resolve":
                    row = store.db.execute("SELECT pair FROM jobs WHERE id=?", (args.job_id,)).fetchone()
                    pair = next((p for p in config.pairs if row and p.name == row[0]), None)
                    if pair is None:
                        raise ConfigError("The job's pair is missing from the configuration.")
                    store.resolve(args.job_id, args.delivered_id, pair)
                    print("Delivery recorded. Restart the bridge to finish remaining parts.")
                else:
                    from .runtime import run

                    asyncio.run(run(config, store))
            finally:
                store.close()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.info("Stopped; accepted events remain in SQLite")
        return 0
    except Exception as exc:
        # A stack trace / exception string could reveal secrets from request URLs.
        log.error("Stopped: %s. Check tokens, permissions, network and local database.", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
