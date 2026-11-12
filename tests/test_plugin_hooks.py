import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "hooks" / "hooks.json"


class PluginHookTests(unittest.TestCase):
    def test_manifest_hooks_run_when_only_python3_is_available(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        command = manifest["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

        with tempfile.TemporaryDirectory() as bin_dir:
            Path(bin_dir, "python3").symlink_to(sys.executable)
            env = os.environ.copy()
            env["PATH"] = bin_dir
            env["CLAUDE_PLUGIN_ROOT"] = str(ROOT)
            result = subprocess.run(
                ["/bin/sh", "-c", f"{command} --selfcheck"],
                capture_output=True,
                env=env,
                text=True,
            )

        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
