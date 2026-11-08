"""Run the shipped batch launchers in cmd.exe with an isolated toolchain."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "These entry points use Windows cmd.exe")
class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="maximka-launcher-"))
        self.project = Path(directory) / "Проверка запуска с пробелами"
        self.project.mkdir()
        self.toolchain = Path(directory) / "tools"
        self.toolchain.mkdir()
        (self.project / "webapp" / "node_modules").mkdir(parents=True)
        (self.project / "webapp" / "package.json").write_text("{}", encoding="utf-8")
        # Preserve the exact checkout bytes, including line endings.
        for name in ("start-miniapp-local.cmd", "build-miniapp.cmd", "start.cmd"):
            shutil.copyfile(ROOT / name, self.project / name)
        self.write_cmd(self.toolchain / "node.cmd", "@echo off\nexit /b 0\n")
        self.write_cmd(self.toolchain / "npm.cmd",
                       "@echo off\necho NPM:%*\n"
                       "if not exist package.json exit /b 41\n"
                       "exit /b %MINIAPP_TEST_BUILD_EXIT%\n")
        self.write_cmd(self.project / "start-terminal.cmd",
                       "@echo off\necho TERMINAL:%MAX_LOCAL_MINIAPP%:%HTTP_HOST%:%~1\n"
                       "exit /b 0\n")

    @staticmethod
    def write_cmd(path, text):
        path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))

    def launch(self, *, build_exit=0):
        env = os.environ.copy()
        env.update({"PATH": str(self.toolchain) + os.pathsep + env.get("PATH", ""),
                    "MINIAPP_TEST_BUILD_EXIT": str(build_exit),
                    "MAX_LOCAL_MINIAPP": "0", "HTTP_HOST": "0.0.0.0"})
        command = (f'"{os.environ["COMSPEC"]}" /d /s /c '
                   f'""{self.project / "start-miniapp-local.cmd"}" "session file.txt""')
        return subprocess.run(command, cwd=self.project, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              encoding="utf-8", errors="replace", timeout=20)

    def test_local_launch_builds_and_forwards_flags_from_unicode_path(self):
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("NPM:run build", result.stdout)
        self.assertIn("TERMINAL:1:127.0.0.1:session file.txt", result.stdout)
        self.assertNotIn("is not recognized", result.stdout)

    def test_build_failure_does_not_start_the_bot(self):
        result = self.launch(build_exit=7)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("NPM:run build", result.stdout)
        self.assertNotIn("TERMINAL:", result.stdout)

    @unittest.skipUnless(shutil.which('pyw'), 'The Python Windows launcher is not installed')
    def test_desktop_entry_opens_a_pyw_script_without_a_venv(self):
        marker = self.project / 'launched.txt'
        (self.project / 'launcher.pyw').write_text(
            'from pathlib import Path\nimport os\n'
            'Path(os.environ["LAUNCHER_TEST_MARKER"]).write_text(str(Path(__file__).resolve()), encoding="utf-8")\n',
            encoding='utf-8')
        command = f'"{os.environ["COMSPEC"]}" /d /s /c ""{self.project / "start.cmd"}""'
        result = subprocess.run(command, cwd=self.project, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10,
            env={**os.environ, 'LAUNCHER_TEST_MARKER': str(marker)})
        self.assertEqual(result.returncode, 0, result.stdout)
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(marker.exists(), 'start.cmd did not launch the setup entry point')
        self.assertEqual(Path(marker.read_text('utf-8')), self.project / 'launcher.pyw')
        self.assertFalse((self.project / '.venv').exists())
