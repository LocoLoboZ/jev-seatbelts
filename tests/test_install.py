from contextlib import redirect_stdout
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import install


class UninstallTests(unittest.TestCase):
    def test_uninstall_removes_retired_hook_and_preserves_foreign_hook(self):
        retired = {
            "matcher": "Bash",
            "hooks": [{
                "type": "command",
                "command": (
                    f'"/old/python" '
                    f'"{install.REPO}/skills/retired/scripts/retired.py"'
                ),
                "statusMessage": "jev-seatbelts retired hook",
            }],
        }
        foreign = {
            "matcher": "Bash",
            "hooks": [{
                "type": "command",
                "command": (
                    f'"{os.path.join(install.REPO, ".venv", "bin", "python")}" '
                    f'"{install.REPO}-backup/custom_hook.py"'
                ),
            }],
        }
        settings = {
            "hooks": {
                "PreToolUse": [install.PRETOOLUSE[0], retired, foreign],
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            settings_path = os.path.join(directory, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as stream:
                json.dump(settings, stream)
            with mock.patch.object(install, "SETTINGS", settings_path):
                with redirect_stdout(io.StringIO()):
                    install.uninstall()
            with open(settings_path, encoding="utf-8") as stream:
                remaining = json.load(stream)["hooks"]["PreToolUse"]

        self.assertEqual(remaining, [foreign])


if __name__ == "__main__":
    unittest.main()
