# pyright: basic
"""Offline startup contract: a missing BOT_TOKEN must abort fast, naming the
variable — not die deep inside discord.py with a None token."""
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class StartupConfigTests(unittest.TestCase):
    def test_exits_naming_bot_token(self):
        # GSHEET_ID given so the only missing required var is BOT_TOKEN;
        # construction does not fetch, so the run stays offline.
        env = {k: v for k, v in os.environ.items() if k != "BOT_TOKEN"}
        env["GSHEET_ID"] = "startup-test-sheet"
        proc = subprocess.run(
            [sys.executable, "spinnyBoi.py"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Missing required environment variable: BOT_TOKEN", proc.stderr)



if __name__ == "__main__":
    unittest.main()
