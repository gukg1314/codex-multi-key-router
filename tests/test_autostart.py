import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import autostart


def fake_runner(loaded=False):
    """Fake launchctl: the loaded flag drives what launchctl print reports."""
    calls = []

    def run(command):
        calls.append(command)
        code = 0
        if len(command) > 1 and command[0] == "launchctl" and command[1] == "print":
            code = 0 if loaded else 113
        return subprocess.CompletedProcess(command, code, "", "")

    return run, calls


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.package = ROOT / "app"

    def tearDown(self):
        self.tmp.cleanup()

    def test_command_is_absolute_and_forces_utf8(self):
        command = autostart.autostart_command(self.package)
        self.assertEqual(command[1:3], ["-X", "utf8"])
        self.assertTrue(Path(command[0]).is_absolute())
        self.assertEqual(Path(command[3]), self.package / "admin.py")
        self.assertEqual(command[4], "--package")
        self.assertEqual(Path(command[5]), self.package)

    def test_packaged_launcher_is_preferred_over_the_bare_admin_command(self):
        """A login entry must restore the menu bar / tray icon, not just admin."""
        bundle = self.home / "Codex\u8def\u7531.app"
        launcher = bundle / "Contents" / "MacOS" / "CodexMultiKey"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b"binary")
        (bundle / "Contents" / "Info.plist").write_bytes(b"plist")
        package = bundle / "Contents" / "Resources" / "runtime" / "app"
        package.mkdir(parents=True)

        self.assertEqual(autostart.detect_launcher(package, "macos"), launcher)
        self.assertEqual(autostart.launcher_command(launcher), [str(launcher), "--silent"])
        service = autostart.Autostart(package, home=self.home, platform="macos")
        self.assertEqual(service.command(), [str(launcher), "--silent"])
        self.assertEqual(autostart.autostart_command(package), service.command())

    def test_windows_launcher_is_started_directly_in_silent_mode(self):
        root = self.home / "CodexMultiKey"
        launcher = root / "CodexMultiKey.exe"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b"MZ")
        package = root / "runtime" / "app"
        package.mkdir(parents=True)

        self.assertEqual(autostart.detect_launcher(package, "windows"), launcher)
        self.assertEqual(autostart.launcher_command(launcher), [str(launcher), "--silent"])

    def test_repair_rewrites_an_old_entry_without_touching_a_loaded_job(self):
        """The repair runs inside the process the entry started, so it must not
        bootout / bootstrap a job that launchd already holds."""
        bundle = self.home / "Codex\u8def\u7531.app"
        launcher = bundle / "Contents" / "MacOS" / "CodexMultiKey"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b"binary")
        (bundle / "Contents" / "Info.plist").write_bytes(b"plist")
        package = bundle / "Contents" / "Resources" / "runtime" / "app"
        package.mkdir(parents=True)

        run, calls = fake_runner(loaded=True)
        service = autostart.Autostart(package, home=self.home, runner=run, platform="macos")
        service.enable()
        payload = plistlib.loads(service.plist_path.read_bytes())
        payload["ProgramArguments"] = ["/old/pythonw", "admin.py"]
        service.plist_path.write_bytes(plistlib.dumps(payload))
        calls.clear()

        calls.clear()
        self.assertTrue(service.repair())
        self.assertEqual([call[1] for call in calls], ["print"])
        repaired = plistlib.loads(service.plist_path.read_bytes())
        self.assertEqual(repaired["ProgramArguments"], service.command())
        self.assertFalse(service.repair())

    def test_repair_rebootstraps_a_plist_whose_job_vanished(self):
        """A plist without a loaded job spams the login item database; repair it."""
        bundle = self.home / "Codex\u8def\u7531.app"
        launcher = bundle / "Contents" / "MacOS" / "CodexMultiKey"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b"binary")
        (bundle / "Contents" / "Info.plist").write_bytes(b"plist")
        package = bundle / "Contents" / "Resources" / "runtime" / "app"
        package.mkdir(parents=True)

        run, calls = fake_runner(loaded=False)
        service = autostart.Autostart(package, home=self.home, runner=run, platform="macos")
        service.plist_path.parent.mkdir(parents=True, exist_ok=True)
        service.plist_path.write_bytes(plistlib.dumps(service.plist_payload()))
        calls.clear()

        self.assertFalse(service.repair())
        self.assertEqual([call[1] for call in calls], ["print", "bootstrap"])

    def test_windows_repair_rewrites_the_run_value(self):
        root = self.home / "CodexMultiKey"
        launcher = root / "CodexMultiKey.exe"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b"MZ")
        package = root / "runtime" / "app"
        package.mkdir(parents=True)

        service = autostart.Autostart(package, home=self.home, platform="windows")
        written = {}
        service._windows_read = lambda name: "old command"
        service._windows_write = lambda value: written.setdefault("value", value)

        self.assertTrue(service.repair())
        self.assertEqual(written["value"], autostart._quote_windows(service.command()))

    def test_macos_enable_writes_launchagent_and_disable_removes_it(self):
        run, calls = fake_runner(loaded=False)
        service = autostart.Autostart(self.package, home=self.home, runner=run, platform="macos")
        self.assertFalse(service.is_enabled())
        service.enable()
        self.assertTrue(service.is_enabled())
        payload = plistlib.loads(service.plist_path.read_bytes())
        self.assertEqual(payload["Label"], autostart.ADMIN_LABEL)
        self.assertEqual(payload["ProgramArguments"], service.command())
        self.assertEqual(payload["EnvironmentVariables"]["PYTHONUTF8"], "1")
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(
            [call[1] for call in calls if call[0] == "launchctl"],
            ["enable", "print", "bootstrap"],
        )
        service.disable()
        self.assertFalse(service.is_enabled())
        self.assertEqual(
            [call[1] for call in calls if call[0] == "launchctl"],
            ["enable", "print", "bootstrap", "disable"],
        )
        self.assertNotIn("bootout", [call[1] for call in calls if call[0] == "launchctl"])

    def test_enable_keeps_an_already_loaded_job_alive(self):
        """Re-enabling must never bootout the job that hosts this process."""
        run, calls = fake_runner(loaded=True)
        service = autostart.Autostart(self.package, home=self.home, runner=run, platform="macos")

        service.enable()

        self.assertTrue(service.is_enabled())
        self.assertEqual(
            [call[1] for call in calls if call[0] == "launchctl"],
            ["enable", "print"],
        )
        self.assertNotIn("bootout", [call[1] for call in calls if call[0] == "launchctl"])

    def test_disable_never_bootouts_the_job_hosting_the_app(self):
        """1.2.1 booted the login job out and killed the admin service mid request."""
        run, calls = fake_runner(loaded=True)
        service = autostart.Autostart(self.package, home=self.home, runner=run, platform="macos")
        service.enable()
        calls.clear()

        service.disable()

        self.assertFalse(service.is_enabled())
        self.assertEqual(
            [call[1] for call in calls if call[0] == "launchctl"],
            ["disable"],
        )

    def test_bootstrap_failure_rolls_back_the_plist(self):
        def failing_runner(command):
            code = 0
            if command[0] == "launchctl" and command[1] == "print":
                code = 113
            elif command[0] == "launchctl" and command[1] == "bootstrap":
                code = 1
            return subprocess.CompletedProcess(command, code, "", "")

        service = autostart.Autostart(self.package, home=self.home, runner=failing_runner, platform="macos")
        with self.assertRaises(autostart.AutostartError):
            service.enable()
        self.assertFalse(service.plist_path.exists())

    def test_first_run_without_a_loaded_job_still_enables_and_disables(self):
        """launchctl print exits non zero when nothing was loaded yet."""

        def runner(command):
            if command[0] == "launchctl" and command[1] == "print":
                return subprocess.CompletedProcess(command, 3, "", "Boot-out failed: 3: No such process")
            return subprocess.CompletedProcess(command, 0, "", "")

        service = autostart.Autostart(self.package, home=self.home, runner=runner, platform="macos")
        service.enable()
        self.assertTrue(service.is_enabled())
        service.disable()
        self.assertFalse(service.is_enabled())

    def test_default_runner_ignores_missing_job_during_unload(self):
        """The real _run raises on non zero; enable/disable must swallow that."""
        calls = []

        def runner(command, check=True):
            calls.append(command)
            if command[1] in ("enable", "disable", "print"):
                raise autostart.AutostartError("command failed: launchctl")
            return subprocess.CompletedProcess(command, 0, "", "")

        service = autostart.Autostart(self.package, home=self.home, runner=runner, platform="macos")
        service.enable()
        self.assertTrue(service.is_enabled())
        service.disable()
        self.assertFalse(service.is_enabled())
        self.assertEqual([call[1] for call in calls if call[0] == "launchctl"], ["enable", "print", "bootstrap", "disable"])

    def test_windows_command_line_quotes_paths_with_spaces(self):
        quoted = autostart._quote_windows([r"C:\Program Files\Python\pythonw.exe", "-X", "utf8"])
        self.assertEqual(quoted, r'"C:\Program Files\Python\pythonw.exe" -X utf8')
        service = autostart.Autostart(self.package, home=self.home, platform="windows")
        if sys.platform == "win32":
            return
        self.assertFalse(service.is_enabled())
        with self.assertRaises(autostart.AutostartError):
            service.enable()

    def test_macos_legacy_launchagent_is_retired_once(self):
        run, calls = fake_runner()
        service = autostart.Autostart(self.package, home=self.home, runner=run, platform="macos")
        label = autostart.LEGACY_LABELS[0]
        legacy = service.legacy_paths()[0]
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(b"plist")

        self.assertEqual(service.legacy_entries(), ["launchagent:" + label])
        self.assertEqual(service.retire_legacy(), ["launchagent:" + label])
        self.assertFalse(legacy.exists())
        self.assertEqual(service.retire_legacy(), [])
        self.assertEqual([call[:2] for call in calls].count(["launchctl", "bootout"]), 1)

    def test_legacy_cleanup_handles_non_ascii_home(self):
        run, _ = fake_runner()
        home = self.home / "\u5f20\u4e09" / "\u4e2d\u6587\u76ee\u5f55"
        service = autostart.Autostart(self.package, home=home, runner=run, platform="macos")
        legacy = service.legacy_paths()[0]
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(b"plist")

        self.assertEqual(len(service.retire_legacy()), 1)
        self.assertFalse(legacy.exists())

    def test_windows_legacy_run_value_is_retired(self):
        service = autostart.Autostart(self.package, home=self.home, platform="windows")
        legacy_name = autostart.LEGACY_WINDOWS_VALUES[0]
        deleted = []
        service._windows_read = lambda name: "old" if name == legacy_name else None
        service._windows_delete = deleted.append

        self.assertEqual(service.legacy_entries(), ["registry:" + legacy_name])
        self.assertEqual(service.retire_legacy(), ["registry:" + legacy_name])
        self.assertIn(legacy_name, deleted)


if __name__ == "__main__":
    unittest.main()
