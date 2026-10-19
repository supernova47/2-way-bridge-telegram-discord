# Server Commands

This guide covers an Ubuntu/Debian Linux server with Python 3.11 or newer. The project does not require Docker. Replace `YOUR_USER` and the project path with the values on your server. Never put real tokens in shell commands or Git.

## Install and configure

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
python3 --version
git clone https://github.com/supernova47/2-way-bridge-telegram-discord.git
cd 2-way-bridge-telegram-discord
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
cp config.example.toml config.toml
nano config.toml
```

The Python version must be 3.11 or newer. In `config.toml`, replace the example chat IDs with your actual IDs. Do not put bot tokens or webhook URLs in that file.

Create a private environment file. Enter the real values in the editor; do not replace the placeholders in a command line:

```bash
mkdir -p "$HOME/.config"
chmod 700 "$HOME/.config"
nano "$HOME/.config/discord-telegram-bridge.env"
chmod 600 "$HOME/.config/discord-telegram-bridge.env"
```

The file contents should use the variable names configured for your pair:

```text
DISCORD_TOKEN=replace-with-the-Discord-bot-token
TELEGRAM_TOKEN=replace-with-the-Telegram-bot-token
DISCORD_WEBHOOK_COMMUNITY=replace-with-the-Discord-webhook-URL
```

If `discord_webhook_env` in `config.toml` has a different value, use that exact environment variable name instead of `DISCORD_WEBHOOK_COMMUNITY`.

## Run as a systemd service

Find the Linux account name and project path:

```bash
whoami
pwd
```

Create the unit file:

```bash
sudo nano /etc/systemd/system/discord-telegram-bridge.service
```

Paste this template and replace every `/home/YOUR_USER/2-way-bridge-telegram-discord` path with the absolute project path. Replace `YOUR_USER` with the account from `whoami`:

```ini
[Unit]
Description=Discord Telegram Bridge
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/2-way-bridge-telegram-discord
EnvironmentFile=/home/YOUR_USER/.config/discord-telegram-bridge.env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStartPre=/home/YOUR_USER/2-way-bridge-telegram-discord/.venv/bin/python -m bridge --config config.toml check
ExecStart=/home/YOUR_USER/2-way-bridge-telegram-discord/.venv/bin/python -m bridge --config config.toml run
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/home/YOUR_USER/2-way-bridge-telegram-discord/data

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now discord-telegram-bridge
sudo systemctl status discord-telegram-bridge --no-pager
sudo journalctl -u discord-telegram-bridge -f
```

Useful service commands:

```bash
sudo systemctl restart discord-telegram-bridge
sudo systemctl stop discord-telegram-bridge
sudo systemctl start discord-telegram-bridge
sudo systemctl status discord-telegram-bridge --no-pager
sudo journalctl -u discord-telegram-bridge -n 100 --no-pager
sudo journalctl -u discord-telegram-bridge -f
```

The service log should show `Discord connected`, `Telegram connected`, and `Bridge ready`. The bridge stores its SQLite database under `data/` by default.

## Queue status and recovery

Run these commands from the project directory. Stop the service before retrying or resolving jobs:

```bash
.venv/bin/python -m bridge --config config.toml status
sudo systemctl stop discord-telegram-bridge
.venv/bin/python -m bridge --config config.toml retry JOB_ID
sudo systemctl start discord-telegram-bridge
```

For an `uncertain` delivery, inspect the destination chat first. If the message exists, record its actual destination message ID instead of retrying:

```bash
sudo systemctl stop discord-telegram-bridge
.venv/bin/python -m bridge --config config.toml resolve JOB_ID DELIVERED_MESSAGE_ID
sudo systemctl start discord-telegram-bridge
```

Only if you verified that the message did not arrive, allow a retry that could create a duplicate:

```bash
sudo systemctl stop discord-telegram-bridge
.venv/bin/python -m bridge --config config.toml retry JOB_ID --allow-duplicate-risk
sudo systemctl start discord-telegram-bridge
```

## Update the code

```bash
sudo systemctl stop discord-telegram-bridge
cd /home/YOUR_USER/2-way-bridge-telegram-discord
git pull --ff-only
.venv/bin/python -m pip install -e .
sudo systemctl start discord-telegram-bridge
sudo systemctl status discord-telegram-bridge --no-pager
```

## Windows Server / PowerShell

Run these from the project directory. The launcher prompts for missing tokens and webhook URLs using hidden input:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item -LiteralPath config.example.toml -Destination config.toml
notepad config.toml
.\scripts\start.ps1 -RestartOnFailure
```

Stop with `Ctrl+C`. The PowerShell launcher restarts after failures but does not install a Windows service or configure startup after reboot.

## Run automated tests

Install development tools, then run the full local test and build commands:

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m ruff check bridge tests
.venv/bin/python -m ruff format --check bridge tests
.venv/bin/python -m build --no-isolation
```

The suite uses mocks and does not connect to Discord or Telegram. See [TESTING.md](TESTING.md) for local and live test steps.
