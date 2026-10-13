"""Login autostart registration for the Codex multi key router UI host.

One registration per platform, both launching the same ascii-only command:

* macOS: LaunchAgent plist that runs ``admin.py``.
* Windows: ``HKCU\\...\\Run`` value that runs ``admin.py``.

No ``.bat`` file is generated anywhere (see the encoding rules in the plan).
Source in this file stays ASCII; user visible Chinese copy lives in the UI.
"""

from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

try:  # pragma: no cover - only importable on Windows
    import winreg
except ImportError:  # pragma: no cover - macOS and Linux
    winreg = None

log = logging.getLogger("codex-multikey-autostart")

ADMIN_LABEL = "com.codexmultikey.router.admin"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_VALUE_NAME = "CodexMultiKeyRouter"
LEGACY_LABELS = (f"com.{Path.home().name}.codex-official-local-router",)
LEGACY_WINDOWS_VALUES = ("CodexOfficialLocalRouter",)
LAUNCHER_NAMES = {"windows": "CodexMultiKey.exe", "macos": "CodexMultiKey"}


class AutostartError(RuntimeError):
    pass


def console_less_python(python: Optional[Path] = None) -> Path:
    """Return pythonw.exe on Windows when it exists, else the given interpreter."""
    interpreter = Path(python or sys.executable)
    if os.name == "nt":
        candidate = interpreter.with_name("pythonw.exe")
        if candidate.is_file():
            return candidate
    return interpreter


def autostart_command(package_dir: Path, python: Optional[Path] = None) -> list[str]:
    """Absolute, quote free argv used by LaunchAgent and the registry Run value.

    ``-X utf8`` forces UTF-8 mode so Chinese user names or Chinese key names can
    never trip the Windows ANSI code page, and no environment block is needed.

    The packaged desktop launcher is preferred whenever it can be found: a
    login entry that starts the bare admin service leaves the user without the
    menu bar / tray icon, which is exactly the "app is not running" report from
    release 1.2.0. The python fallback keeps development checkouts working.
    """
    launcher = detect_launcher(Path(package_dir))
    if launcher is not None:
        return launcher_command(launcher)
    return [
        str(console_less_python(python)),
        "-X",
        "utf8",
        str(Path(package_dir) / "admin.py"),
        "--package",
        str(Path(package_dir)),
    ]


def launcher_command(launcher: Path) -> list[str]:
    """Argv that boots the packaged desktop launcher in silent mode.

    Both platforms start the executable directly. Going through macOS ``open``
    would make LaunchServices wake an already running copy with a reopen event,
    which pops the main window on every login; the launcher's own single
    instance channel handles that case by exiting quietly instead.
    """
    launcher = Path(launcher)
    return [str(launcher), "--silent"]


def detect_launcher(package_dir: Path, platform: Optional[str] = None) -> Optional[Path]:
    """Best effort lookup of the packaged desktop launcher beside the app files.

    An autostart entry written by an older release starts ``admin.py`` directly,
    so that process never received the launcher path as an argument. Reading the
    path from the installed folder layout keeps the very next reboot on the
    launcher instead of the bare background service.
    """
    current = platform or ("windows" if os.name == "nt" else "macos")
    name = LAUNCHER_NAMES.get(current)
    if name is None:
        return None
    package = Path(package_dir)
    candidates: list[Path] = []
    if current == "windows":
        # <root>/runtime/app -> <root>/CodexMultiKey.exe
        candidates.append(package.parent.parent / name)
    else:
        # <bundle>/Contents/Resources/runtime/app -> <bundle>/Contents/MacOS/...
        if len(package.parents) > 2:
            candidates.append(package.parents[2] / "MacOS" / name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _quote_windows(argv: list[str]) -> str:
    """Quote argv for a Windows command line (subprocess list2cmdline rules)."""
    return subprocess.list2cmdline(argv)


def _run(command: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if check and result.returncode:
        raise AutostartError("command failed: %s" % command[0])
    return result


class Autostart:
    """Register/remove the login autostart entry for the UI host."""

    def __init__(
        self,
        package_dir: Path,
        home: Optional[Path] = None,
        log_dir: Optional[Path] = None,
        python: Optional[Path] = None,
        launcher: Optional[Path] = None,
        runner: Callable[[list[str]], subprocess.CompletedProcess] = _run,
        platform: Optional[str] = None,
    ) -> None:
        self.package_dir = Path(package_dir)
        self.home = Path(home or Path.home())
        self.log_dir = Path(log_dir) if log_dir else self.package_dir / "runtime" / "logs"
        self.python = Path(python) if python else None
        self.runner = runner
        self.platform = platform or ("windows" if os.name == "nt" else "macos")
        if launcher is not None and Path(launcher).is_file():
            self.launcher = Path(launcher)
        else:
            self.launcher = detect_launcher(self.package_dir, self.platform)

    # ---------------------------------------------------------------- helpers
    def command(self) -> list[str]:
        if self.launcher is not None:
            return launcher_command(self.launcher)
        return autostart_command(self.package_dir, self.python)

    def repair(self) -> bool:
        """Point an existing login entry at the packaged desktop launcher.

        Only the stored command is rewritten. The loaded job is left alone, so
        this can run safely from inside the process that the entry started
        (a bootout/bootstrap round trip would kill the running launcher).
        """
        if self.launcher is None or not self.is_enabled():
            return False
        desired = self.command()
        if self.platform == "windows":
            quoted = _quote_windows(desired)
            if self._windows_read(WINDOWS_VALUE_NAME) == quoted:
                return False
            self._windows_write(quoted)
            log.info("autostart entry repaired name=%s command=%s", WINDOWS_VALUE_NAME, desired[0])
            return True
        if self.plist_path.exists():
            try:
                payload = plistlib.loads(self.plist_path.read_bytes())
            except (OSError, ValueError):
                payload = {}
            if payload.get("ProgramArguments") == desired:
                uid = os.getuid() if hasattr(os, "getuid") else 0
                domain = "gui/%d" % uid
                self._rebootstrap_if_missing(domain, "%s/%s" % (domain, ADMIN_LABEL))
                return False
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.plist_path.write_bytes(plistlib.dumps(self.plist_payload()))
        log.info("autostart entry repaired label=%s command=%s", ADMIN_LABEL, desired[0])
        uid = os.getuid() if hasattr(os, "getuid") else 0
        domain = "gui/%d" % uid
        self._rebootstrap_if_missing(domain, "%s/%s" % (domain, ADMIN_LABEL))
        return True

    def _unload(self, command: list[str]) -> None:
        """Best effort unload: launchctl exits non zero when no job was loaded."""
        try:
            self.runner(command)
        except (AutostartError, OSError, subprocess.SubprocessError) as exc:
            log.info("autostart unload skipped command=%s error=%s", command[0], type(exc).__name__)

    def _service_loaded(self, target: str) -> bool:
        """True when launchd already holds this job in the given domain."""
        try:
            result = self.runner(["launchctl", "print", target])
        except (AutostartError, OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def _rebootstrap_if_missing(self, domain: str, target: str) -> bool:
        """Re-register a plist that exists but has no loaded launchd job.

        macOS keeps a stale entry in the login item database when the plist and
        the loaded job disagree, which spams "Could not find specified service"
        and leaves the settings switch stuck. Bootstrapping the plist again makes
        both sides agree. RunAtLoad may start a second launcher, but the single
        instance channel makes that copy exit without opening a window.
        """
        if self._service_loaded(target):
            return False
        boot = self.runner(["launchctl", "bootstrap", domain, str(self.plist_path)])
        if boot.returncode:
            log.info("autostart job rebootstrap skipped label=%s", ADMIN_LABEL)
            return False
        log.info("autostart job rebootstrap label=%s", ADMIN_LABEL)
        return True

    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / (ADMIN_LABEL + ".plist")

    def plist_payload(self) -> dict:
        return {
            "Label": ADMIN_LABEL,
            "ProgramArguments": self.command(),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 10,
            "StandardOutPath": str(self.log_dir / "admin.stdout.log"),
            "StandardErrorPath": str(self.log_dir / "admin.stderr.log"),
            "EnvironmentVariables": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            "ProcessType": "Background",
        }

    # ------------------------------------------------------------------ state
    def is_enabled(self) -> bool:
        if self.platform == "windows":
            return self._windows_read(WINDOWS_VALUE_NAME) is not None
        return self.plist_path.exists()

    def enable(self) -> None:
        if self.platform == "windows":
            self._windows_write(_quote_windows(self.command()))
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.plist_path.write_bytes(plistlib.dumps(self.plist_payload()))
        lint = self.runner(["plutil", "-lint", str(self.plist_path)])
        if lint.returncode:
            self.plist_path.unlink(missing_ok=True)
            raise AutostartError("plist lint failed")
        uid = os.getuid() if hasattr(os, "getuid") else 0
        domain = "gui/%d" % uid
        target = "%s/%s" % (domain, ADMIN_LABEL)
        # Clear a previous disable first, then leave an already loaded job alone:
        # a bootout/bootstrap round trip would kill the process tree that hosts
        # the running launcher and admin service.
        self._unload(["launchctl", "enable", target])
        if self._service_loaded(target):
            log.info("autostart enabled label=%s mode=already-loaded", ADMIN_LABEL)
            return
        boot = self.runner(["launchctl", "bootstrap", domain, str(self.plist_path)])
        if boot.returncode:
            self.plist_path.unlink(missing_ok=True)
            raise AutostartError("launchctl bootstrap failed")
        log.info("autostart enabled label=%s", ADMIN_LABEL)

    def disable(self) -> None:
        if self.platform == "windows":
            self._windows_delete(WINDOWS_VALUE_NAME)
            return
        uid = os.getuid() if hasattr(os, "getuid") else 0
        domain = "gui/%d" % uid
        # Never bootout here: the login entry owns the running launcher and
        # admin service, so booting it out kills the process answering this
        # request (the 1.2.1 "Load failed" bug). Disabling the label keeps the
        # next login from starting it, and dropping the plist removes the entry
        # from the login item database; the current process keeps serving until
        # the user quits.
        self._unload(["launchctl", "disable", "%s/%s" % (domain, ADMIN_LABEL)])
        self.plist_path.unlink(missing_ok=True)
        log.info("autostart disabled label=%s mode=disable-without-bootout", ADMIN_LABEL)

    # ---------------------------------------------------------------- legacy
    def legacy_paths(self) -> list[Path]:
        return [self.home / "Library" / "LaunchAgents" / (label + ".plist") for label in LEGACY_LABELS]

    def legacy_entries(self) -> list[str]:
        """Old tool autostart entries that would fight this one over port 8790."""
        if self.platform == "windows":
            return ["registry:" + name for name in LEGACY_WINDOWS_VALUES
                    if self._windows_read(name) is not None]
        return ["launchagent:" + path.stem for path in self.legacy_paths() if path.exists()]

    def retire_legacy(self) -> list[str]:
        """Remove old tool autostart entries; the new tool owns port 8790."""
        entries = self.legacy_entries()
        if not entries:
            return []
        if self.platform == "windows":
            for name in LEGACY_WINDOWS_VALUES:
                self._windows_delete(name)
            return entries
        uid = os.getuid() if hasattr(os, "getuid") else 0
        for path in self.legacy_paths():
            if not path.exists():
                continue
            self._unload(["launchctl", "bootout", "gui/%d" % uid, str(path)])
            path.unlink(missing_ok=True)
        log.info("legacy autostart retired entries=%s", ",".join(entries))
        return entries

    # --------------------------------------------------------------- windows
    def _windows_read(self, name: str) -> Optional[str]:
        if winreg is None:
            return None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
                value, _ = winreg.QueryValueEx(key, name)
                return str(value)
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.warning("autostart query failed error=%s", type(exc).__name__)
            return None

    def _windows_write(self, command: str) -> None:
        if winreg is None:
            raise AutostartError("windows registry module unavailable")
        try:
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.SetValueEx(key, WINDOWS_VALUE_NAME, 0, winreg.REG_SZ, command)
        except OSError as exc:
            raise AutostartError("windows autostart write failed: %s" % type(exc).__name__) from exc
        log.info("autostart enabled name=%s", WINDOWS_VALUE_NAME)

    def _windows_delete(self, name: str) -> None:
        if winreg is None:
            return
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("autostart removal failed error=%s", type(exc).__name__)
            return
        log.info("autostart removed name=%s", name)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Codex multi key router autostart")
    parser.add_argument("action", choices=("status", "enable", "disable"))
    parser.add_argument("--package", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--home", type=Path)
    args = parser.parse_args(argv)
    service = Autostart(args.package, home=args.home)
    try:
        if args.action == "status":
            print("enabled" if service.is_enabled() else "disabled")
        elif args.action == "enable":
            service.enable()
            print("enabled")
        else:
            service.disable()
            print("disabled")
    except AutostartError as exc:
        print("autostart failed: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
