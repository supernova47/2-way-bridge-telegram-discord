from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

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
