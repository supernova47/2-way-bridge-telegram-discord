# Testing the Bridge

## Automated tests

Automated tests run locally with mocked Discord and Telegram APIs. They do not need real bot tokens, chat IDs, or network access.

On Windows PowerShell, from the project directory:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\scripts\check.ps1
```

`check.ps1` runs Ruff linting, formatting checks, all unit tests, and a package build. The first command installs the optional development tools if needed.

On Linux, from the project directory:

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m ruff check bridge tests
.venv/bin/python -m ruff format --check bridge tests
.venv/bin/python -m build --no-isolation
```

The test suite covers both message directions, bot and command filters, duplicate events, retries, uncertain sends, replies, edits, deletes, media previews and fallbacks, size limits, safe formatting, and secret-safe logs. A successful test run does not prove that live credentials or chat permissions are correct.

## Live smoke test

Use a test Discord channel and a test Telegram group. Run only one bridge process for the Telegram bot.

1. Validate the configuration. On Windows, run `.\.venv\Scripts\python.exe -m bridge --config config.toml check`. On Linux, run `.venv/bin/python -m bridge --config config.toml check`.
2. Start the bridge. On Windows, run `.\scripts\start.ps1 -RestartOnFailure`; on Linux, run `sudo systemctl start discord-telegram-bridge`.
3. Confirm the logs show `Discord connected`, `Telegram connected`, and `Bridge ready`.
4. Send a short text in Discord. In Telegram, it should appear as `[Discord] Author:` followed by a line break and the message. Send a message from Telegram and confirm it appears in Discord under `Author · Telegram` when a webhook is configured.
5. Send a photo, GIF, video, and ordinary document in both directions. Check that images and videos show previews when supported. A media-only Discord message should have `[Discord] Author:` in its Telegram media caption.
6. Reply to a bridged message in both platforms. The destination should use a native reply or a readable quote.
7. Edit a text message and an attachment in Discord, then confirm the known copy updates. Delete a Discord original and confirm its known Telegram copy is removed. Telegram deletion events are not available to this bridge.
8. Restart the process and send another message. Confirm the bridge reconnects and does not duplicate already delivered items.

Stop the Windows process with `Ctrl+C`; on Linux, run `sudo systemctl stop discord-telegram-bridge`. Follow Linux service logs with `sudo journalctl -u discord-telegram-bridge -f`.

Do not run the live test in a busy production chat. Do not paste tokens or webhook URLs into logs, screenshots, or support messages.
