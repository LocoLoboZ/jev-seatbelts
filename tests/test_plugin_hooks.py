import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "hooks" / "hooks.json"


class PluginHookTests(unittest.TestCase):
    def _manifest(self):
        return json.loads(MANIFEST.read_text(encoding="utf-8"))

    def _command(self):
        return self._manifest()["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    def _write_executable(self, directory, name, body):
        executable = Path(directory, name)
        executable.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        executable.chmod(0o755)
        return executable

    def _python_wrapper(self, directory, name):
        return self._write_executable(
            directory,
            name,
            f'exec "{sys.executable}" "$@"',
        )

    def _run_hook(self, bin_dir):
        bash = Path(bin_dir, "bash")
        if not bash.exists():
            self._write_executable(
                bin_dir,
                "bash",
                'exec /bin/sh "$@"',
            )
        env = os.environ.copy()
        env["PATH"] = str(bin_dir)
        env["CLAUDE_PLUGIN_ROOT"] = str(ROOT)
        return subprocess.run(
            ["/bin/sh", "-c", f"{self._command()} --selfcheck"],
            capture_output=True,
            env=env,
            text=True,
        )

    def _assert_hook_passed(self, result):
        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_all_manifest_hooks_use_portable_launcher(self):
        commands = [
            hook["command"]
            for entries in self._manifest()["hooks"].values()
            for entry in entries
            for hook in entry["hooks"]
        ]

        self.assertTrue(commands)
        self.assertTrue(all(
            command.startswith(
                'bash "${CLAUDE_PLUGIN_ROOT}/hooks/run-python3.sh" '
            )
            for command in commands
        ))

    def test_manifest_hooks_run_when_only_python3_is_available(self):
        with tempfile.TemporaryDirectory() as bin_dir:
            self._python_wrapper(bin_dir, "python3")
            result = self._run_hook(bin_dir)

        self._assert_hook_passed(result)

    def test_manifest_hooks_run_when_only_python_is_available(self):
        with tempfile.TemporaryDirectory() as bin_dir:
            self._python_wrapper(bin_dir, "python")
            result = self._run_hook(bin_dir)

        self._assert_hook_passed(result)

    def test_manifest_hooks_run_without_launcher_execute_bit(self):
        launcher_ref = self._command().split('"')[1]
        launcher = ROOT / launcher_ref.removeprefix(
            "${CLAUDE_PLUGIN_ROOT}/"
        )
        original_mode = stat.S_IMODE(launcher.stat().st_mode)
        try:
            launcher.chmod(original_mode & ~0o111)
            with tempfile.TemporaryDirectory() as bin_dir:
                self._python_wrapper(bin_dir, "python3")
                result = self._run_hook(bin_dir)
        finally:
            launcher.chmod(original_mode)

        self._assert_hook_passed(result)

    def test_windows_launcher_prefers_py_over_python3_store_stub(self):
        with tempfile.TemporaryDirectory() as bin_dir:
            marker = Path(bin_dir, "store-stub-ran")
            self._write_executable(
                bin_dir,
                "uname",
                "printf '%s\\n' MINGW64_NT-10.0",
            )
            self._write_executable(
                bin_dir,
                "python3",
                f': > "{marker}"\nexit 1',
            )
            self._write_executable(
                bin_dir,
                "py",
                (
                    '[ "$1" = "-3" ] || exit 64\n'
                    'shift\n'
                    f'exec "{sys.executable}" "$@"'
                ),
            )
            result = self._run_hook(bin_dir)
            store_stub_ran = marker.exists()

        self._assert_hook_passed(result)
        self.assertFalse(store_stub_ran, "python3 Store stub was invoked")

    def test_windows_launcher_uses_python_without_running_store_stub(self):
        with tempfile.TemporaryDirectory() as bin_dir:
            marker = Path(bin_dir, "store-stub-ran")
            self._write_executable(
                bin_dir,
                "uname",
                "printf '%s\\n' MINGW64_NT-10.0",
            )
            self._write_executable(
                bin_dir,
                "python3",
                f': > "{marker}"\nexit 1',
            )
            self._python_wrapper(bin_dir, "python")
            result = self._run_hook(bin_dir)
            store_stub_ran = marker.exists()

        self._assert_hook_passed(result)
        self.assertFalse(store_stub_ran, "python3 Store stub was invoked")


if __name__ == "__main__":
    unittest.main()
