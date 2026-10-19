from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bridge.config import load_config

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def clean_env(self):
        env = os.environ.copy()
        for name in ("DISCORD_TOKEN", "TELEGRAM_TOKEN", "DISCORD_WEBHOOK_COMMUNITY"):
            env.pop(name, None)
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def test_launcher_can_discover_required_variable_names_without_tokens(self):
        result = subprocess.run(
            [sys.executable, "-m", "bridge", "--config", "config.example.toml", "required-env"],
            cwd=ROOT,
            env=self.clean_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(), ["DISCORD_TOKEN", "TELEGRAM_TOKEN", "DISCORD_WEBHOOK_COMMUNITY"]
        )

    def test_mapping_wizard_adds_validated_config_entry_without_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text(
                'database = "state.sqlite3"\n\n[[pairs]]\nname="one"\n'
                "discord_channel_id=123\ntelegram_chat_id=-100\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, "-m", "bridge", "--config", str(config), "mapping", "add"],
                cwd=ROOT,
                env=self.clean_env(),
                input="111\n222\nTelegram Name\nDiscord Name\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(load_config(config, require_tokens=False).user_mappings), 1)
            listed = subprocess.run(
                [sys.executable, "-m", "bridge", "--config", str(config), "mapping", "list"],
                cwd=ROOT,
                env=self.clean_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertIn("Telegram ID 111", listed.stdout)
            self.assertIn("Discord ID 222", listed.stdout)

    def test_missing_tokens_fail_safely_before_network_access(self):
        result = subprocess.run(
            [sys.executable, "-m", "bridge", "--config", "config.example.toml", "check"],
            cwd=ROOT,
            env=self.clean_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("DISCORD_WEBHOOK_COMMUNITY", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows console encoding only")
    def test_help_is_printable_in_cp1251_console(self):
        env = self.clean_env()
        env["PYTHONIOENCODING"] = "cp1251"
        result = subprocess.run(
            [sys.executable, "-m", "bridge", "--help"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="cp1251",
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("telegram-users", result.stdout)

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows launcher only")
    def test_powershell_launcher_checks_configuration_noninteractively(self):
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "start.ps1"),
                "-Config",
                "config.example.toml",
                "-NonInteractive",
            ],
            cwd=ROOT,
            env=self.clean_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("DISCORD_WEBHOOK_COMMUNITY", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
