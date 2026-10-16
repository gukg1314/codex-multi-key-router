"""Local management API and UI host for the Codex multi key router.

Listens on 127.0.0.1:8791, serves the desktop UI and a small JSON API, and
owns the 8790 router child process so the UI can start/stop it.

Source in this file stays ASCII on purpose (plan section 5); every user visible
Chinese string lives in the UTF-8 UI file or arrives from user data.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import autostart  # noqa: E402
import keys_store  # noqa: E402
import toolctl  # noqa: E402

# APP_VERSION is the user visible release, and the desktop launcher compares it
# before it reuses a running service (a stale service would serve an old API to
# a new UI). Keep it in sync with launcher/main.go appVersion.
APP_VERSION = "1.2.26"
VERSION = "6"
ADMIN_HOST = "127.0.0.1"
ADMIN_PORT = 8791
UI_STYLES = ("layered", "chaos", "mintlify")
ROUTER_PORT = 8790
MAX_BODY = 1024 * 1024
KEY_PATH = re.compile(r"^/api/keys/([A-Za-z0-9_-]{1,32})(?:/(discover|test|models))?$")

log = logging.getLogger("codex-multikey-admin")


def _build_fingerprint() -> str:
    """Hash of this file so a rebuilt admin.py is never mistaken for the old one.

    Two releases can share APP_VERSION (a hot fix keeps the number), and a
    service started before the fix keeps answering 8791 with the old API. The
    launcher and the --stop-stale path compare this fingerprint with the value
    reported by /healthz, so same-version rebuilds still get replaced.
    """
    try:
        data = Path(__file__).read_bytes()
    except OSError as exc:
        log.warning("admin build fingerprint unreadable error=%s", type(exc).__name__)
        return ""
    return hashlib.sha256(data).hexdigest()[:12]


BUILD = _build_fingerprint()


def configure_logging(log_dir: Path) -> None:
    """Write admin logs as UTF-8 without BOM so Windows never mangles them."""
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "admin.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _child_env() -> dict:
    """Force UTF-8 in child Python processes so Chinese names survive anywhere."""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _tail_lines(path: Path, lines: int, max_bytes: int = 256 * 1024) -> list[str]:
    try:
        size = path.stat().st_size
    except OSError:
        return []
    try:
        with path.open("rb") as handle:
            truncated = size > max_bytes
            if truncated:
                handle.seek(size - max_bytes)
            data = handle.read()
    except OSError:
        return []
    rows = data.decode("utf-8", errors="replace").splitlines()
    if truncated and rows:
        rows = rows[1:]
    return rows[-lines:]


def _listening_pids(port: int = ROUTER_PORT) -> list[int]:
    if toolctl.is_windows():
        return toolctl._windows_listening_pids(port)
    try:
        result = subprocess.run(
            ["lsof", "-tiTCP:%d" % port, "-sTCP:LISTEN"],
            capture_output=True, text=True, errors="replace", timeout=15,
        )
    except OSError as exc:
        log.warning("lsof unavailable error=%s", type(exc).__name__)
        return []
    pids: list[int] = []
    for row in (result.stdout or "").splitlines():
        text = row.strip()
        if text.isdigit():
            pids.append(int(text))
    return pids


def _process_command(pid: int) -> str:
    if toolctl.is_windows():
        return toolctl._windows_process_image(pid)
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, errors="replace", timeout=15,
        )
    except OSError as exc:
        log.warning("ps unavailable error=%s", type(exc).__name__)
        return ""
    return (result.stdout or "").strip()


def _process_full_command(pid: int) -> str:
    """Full command line when the platform exposes it (Windows cannot)."""
    if toolctl.is_windows():
        return ""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, errors="replace", timeout=15,
        )
    except OSError as exc:
        log.warning("ps unavailable error=%s", type(exc).__name__)
        return ""
    return (result.stdout or "").strip()


def _kill_pid(pid: int) -> None:
    """Terminate one process: taskkill on Windows, SIGTERM then SIGKILL elsewhere."""
    if toolctl.is_windows():
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True, text=True, errors="replace", timeout=30,
            **toolctl.hidden_subprocess_kwargs(),
        )
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(20):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _health_payload(port: int, timeout: float = 2.0) -> dict:
    """Read /healthz of a service sitting on the admin port; {} when unreadable."""
    # A proxy must never sit between us and the loopback service, so the opener
    # is built with an empty proxy map instead of the environment defaults.
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open("http://%s:%d/healthz" % (ADMIN_HOST, port), timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        log.info("healthz probe failed port=%d error=%s", port, type(exc).__name__)
        return {}


def _stale_admin_pids(port: int) -> list[int]:
    """Listener pids that are provably an older copy of this admin service.

    Anything that does not answer /healthz as an admin service, or whose process
    is not a Python interpreter, is left alone: the port may belong to an
    unrelated program, and guessing would kill user work.
    """
    payload = _health_payload(port)
    if payload.get("service") != "admin" or "version" not in payload:
        log.warning("stale admin check skipped; not our service port=%d", port)
        return []
    verified: list[int] = []
    for pid in _listening_pids(port):
        image = _process_command(pid).lower()
        full = _process_full_command(pid)
        if "python" not in image and "python" not in full.lower():
            log.warning("stale admin check skipped; pid is not python pid=%s image=%s", pid, image)
            continue
        if full and "admin.py" not in full:
            log.warning("stale admin check skipped; pid runs another script pid=%s", pid)
            continue
        verified.append(pid)
    return verified


def _stop_stale_admin(port: int) -> int:
    """CLI mode: replace an admin service that came from an older release.

    A stale service keeps answering 8791 with the endpoints of its own release,
    so the new UI would silently talk to old code (no model override endpoint,
    no logo route, the old catalog merge). The launcher calls this before it
    spawns the fresh service.
    """
    if not _port_in_use(port):
        log.info("stale admin check: port free port=%d", port)
        print("no admin service on port %d" % port)
        return 0
    payload = _health_payload(port)
    running = str(payload.get("app_version") or "")
    running_build = str(payload.get("build") or "")
    if running == APP_VERSION and running_build == BUILD:
        log.info("admin service already current version=%s build=%s port=%d", running, running_build, port)
        print("admin service is current on port %d" % port)
        return 0
    log.warning(
        "stale admin detected running=%s/%s expected=%s/%s port=%d",
        running or "unknown",
        running_build or "unknown",
        APP_VERSION,
        BUILD,
        port,
    )
    pids = _stale_admin_pids(port)
    if not pids:
        log.warning("stale admin not replaced; port owner unverified port=%d", port)
        print("admin port %d is owned by an unverified process" % port)
        return 1
    for pid in pids:
        log.info("stale admin stopping pid=%s", pid)
        _kill_pid(pid)
    for _ in range(60):
        if not _port_in_use(port):
            log.info("stale admin stopped pids=%s", pids)
            print("stopped stale admin service on port %d" % port)
            return 0
        time.sleep(0.25)
    log.warning("stale admin port still busy after stop attempts port=%d", port)
    print("admin port %d is still in use after stopping it" % port)
    return 1


class RouterProcess:
    """Owns the 8790 router process; also satisfies the toolctl controller API."""

    def __init__(self, p: dict[str, Path]) -> None:
        self.p = p
        self.python = Path(sys.executable)

    # ------------------------------------------------------------- inspection
    def listening(self) -> bool:
        return toolctl._port_listening(ROUTER_PORT)

    def pids(self) -> list[int]:
        return _listening_pids(ROUTER_PORT) if self.listening() else []

    def pid(self) -> Optional[int]:
        pids = self.pids()
        return pids[0] if pids else None

    def _console_less_python(self) -> Path:
        if toolctl.is_windows():
            pythonw = self.python.with_name("pythonw.exe")
            if pythonw.is_file():
                return pythonw
        return self.python

    def retire_legacy_autostart(self) -> list[str]:
        """Remove old tool autostart entries so they cannot fight over port 8790."""
        try:
            service = autostart.Autostart(self.p["package"], home=self.p["home"], log_dir=self.p["logs"])
            return service.retire_legacy()
        except (OSError, subprocess.SubprocessError, autostart.AutostartError) as exc:
            log.warning("legacy autostart cleanup failed error=%s", type(exc).__name__)
            return []

    # ---------------------------------------------------------------- control
    def spawn(self) -> None:
        script = self.p["runtime"] / "router.py"
        if not script.is_file():
            raise ApiError("router runtime is missing; enable the router first", 409, "runtime_missing")
        self.retire_legacy_autostart()
        logs = self.p["logs"]
        logs.mkdir(parents=True, exist_ok=True)
        kwargs: dict = {}
        if toolctl.is_windows():
            kwargs["creationflags"] = toolctl._windows_creation_flags()
        else:
            kwargs["start_new_session"] = True
        with (logs / "router.stdout.log").open("ab") as out, (logs / "router.stderr.log").open("ab") as err:
            process = subprocess.Popen(
                [str(self._console_less_python()), "-X", "utf8", str(script), "--runtime-dir", str(self.p["runtime"])],
                cwd=str(self.p["runtime"]),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                close_fds=True,
                env=_child_env(),
                **kwargs,
            )
        log.info("router spawned pid=%s", process.pid)

    def ensure_running(self) -> None:
        if self.listening():
            log.info("router already listening port=%d", ROUTER_PORT)
            return
        self.spawn()

    def start(self) -> bool:  # toolctl controller interface
        self.ensure_running()
        return True

    def restart(self) -> None:
        self.stop()
        self.ensure_running()

    def health(self, attempts: int = 20) -> dict:  # toolctl controller interface
        return toolctl.http_health(attempts)

    def _kill(self, pid: int) -> None:
        _kill_pid(pid)

    def stop(self) -> None:
        for pid in self.pids():
            command = _process_command(pid)
            if command and "python" not in command.lower():
                raise ApiError(
                    "port %d is owned by another program (%s)" % (ROUTER_PORT, command),
                    409,
                    "port_busy",
                )
            log.info("router stopping pid=%s", pid)
            self._kill(pid)
        for _ in range(40):
            if not self.listening():
                log.info("router stopped")
                return
            time.sleep(0.25)
        log.warning("router still listening after stop attempts")


class TaskRunner:
    """Runs one blocking job (refresh/enable/...) off the HTTP thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._state: dict = {"status": "idle", "kind": None, "message": None,
                             "detail": None, "started": None, "finished": None}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def running(self) -> bool:
        with self._lock:
            return self._state["status"] == "running"

    def submit(self, kind: str, job: Callable[[], Optional[dict]]) -> dict:
        with self._lock:
            if self._state["status"] == "running":
                raise ApiError("another task is already running", 409, "task_running")
            self._state = {
                "status": "running", "kind": kind, "message": None, "detail": None,
                "started": _now_text(), "finished": None,
            }
            thread = threading.Thread(target=self._run, args=(kind, job), name="task-" + kind, daemon=True)
            self._thread = thread
        thread.start()
        return self.snapshot()

    def _run(self, kind: str, job: Callable[[], Optional[dict]]) -> None:
        try:
            detail = job()
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as text
            log.exception("task failed kind=%s type=%s", kind, type(exc).__name__)
            with self._lock:
                self._state.update({
                    "status": "error",
                    "message": "%s: %s" % (type(exc).__name__, exc),
                    "finished": _now_text(),
                })
            return
        with self._lock:
            self._state.update({"status": "done", "detail": detail, "finished": _now_text()})


class AdminService:
    """Business logic behind the HTTP API."""

    def __init__(
        self,
        package_dir: Path,
        p: dict[str, Path],
        home: Optional[Path] = None,
        launcher: Optional[Path] = None,
    ) -> None:
        self.package_dir = Path(package_dir)
        self.p = p
        self.home = Path(home or p.get("home") or Path.home())
        self.launcher_path = Path(launcher) if launcher else None
        self.lock = threading.RLock()
        self.events: deque = deque(maxlen=200)
        self.tasks = TaskRunner()
        self.router = RouterProcess(p)
        self.started_at = time.time()
        self._service_quit_hook: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------------ basics
    def add_event(self, message: str, level: str = "info") -> None:
        entry = {"time": time.strftime("%H:%M:%S"), "level": level, "message": message}
        with self.lock:
            self.events.append(entry)
        log.info("event level=%s message=%s", level, message)

    def events_view(self, limit: int = 50) -> list[dict]:
        with self.lock:
            return list(self.events)[-limit:][::-1]

    def _settings_raw(self) -> dict:
        if not self.p["settings"].exists():
            return {}
        try:
            return toolctl._load_json(self.p["settings"], "settings")
        except (toolctl.ValidationError, OSError):
            log.warning("settings.json unreadable; falling back to defaults")
            return {}

    def _write_settings_raw(self, data: dict) -> None:
        toolctl._write_json(self.p["settings"], data, 0o600)

    def router_expected(self) -> bool:
        """Persisted "the router should be running" switch, default on."""
        value = self._settings_raw().get("router_enabled")
        return True if value is None else bool(value)

    def _set_router_enabled(self, value: bool) -> None:
        raw = self._settings_raw()
        if raw.get("router_enabled") is value:
            return
        raw["router_enabled"] = bool(value)
        self._write_settings_raw(raw)
        log.info("router_enabled saved value=%s", bool(value))

    def keys_data(self) -> dict:
        return toolctl.load_runtime_keys(self.p)

    def save_keys(self, data: dict) -> dict:
        return keys_store.save_keys(self.p["keys"], data)

    @property
    def autostart(self) -> autostart.Autostart:
        return autostart.Autostart(
            self.package_dir,
            home=self.home,
            log_dir=self.p["logs"],
            launcher=self.launcher_path,
        )

    def repair_autostart(self) -> dict:
        """Keep an enabled login entry pointed at the desktop launcher.

        Release 1.2.0 registered admin.py, so a reboot restored the service but
        never the menu bar / tray icon. The launcher posts here on every start;
        entries written by an older release are detected from the install
        layout and rewritten for the next login.
        """
        if self.launcher_path is None:
            self.launcher_path = autostart.detect_launcher(self.package_dir)
        if self.launcher_path is None:
            log.info("autostart repair skipped reason=launcher-unknown")
            return {"repaired": False, "reason": "launcher-unknown"}
        try:
            repaired = self.autostart.repair()
        except (OSError, autostart.AutostartError) as exc:
            log.warning("autostart repair failed error=%s", type(exc).__name__)
            return {"repaired": False, "reason": type(exc).__name__}
        if repaired:
            self.add_event("login autostart now opens the desktop launcher")
        return {"repaired": repaired}

    # ------------------------------------------------------------------- state
    def detail_state(self) -> dict:
        keys_data = self.keys_data()
        manifest = self._runtime_manifest()
        official = self._official_models()
        taken_over = toolctl._is_unified(self.p["config"]) if self.p["config"].exists() else False
        return {
            "keys": keys_data,
            "manifest": manifest,
            "official": official,
            "taken_over": taken_over,
        }

    def _runtime_manifest(self) -> Optional[dict]:
        if not self.p["manifest"].exists():
            return None
        try:
            return toolctl.load_manifest(self.p["manifest"])
        except (toolctl.ValidationError, OSError):
            log.warning("runtime manifest unreadable")
            return None

    def _official_models(self) -> list[dict]:
        if not self.p["official_cache"].exists():
            return []
        try:
            cache = toolctl._load_json(self.p["official_cache"], "official cache")
            return toolctl.validate_official_cache(cache)
        except (toolctl.ValidationError, OSError):
            log.warning("official model cache unreadable")
            return []

    def discovery_path(self, key_id: str) -> Path:
        return self.p["runtime"] / "discovery" / (key_id + ".json")

    def read_discovery(self, key_id: str) -> Optional[dict]:
        path = self.discovery_path(key_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("discovery cache unreadable key=%s", key_id)
            return None
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            return None
        return data

    def discover_key(self, key: dict) -> list[str]:
        """Fetch one key's model list; same call the refresh path uses."""
        request_json = toolctl.gateway_requester(key["upstream_base_url"])
        models = toolctl.fetch_gateway_models(key["api_key"], request_json)
        record = {
            "key_id": key["id"],
            "fetched_at": _now_text(),
            "upstream_base_url": key["upstream_base_url"],
            "models": models,
        }
        self.discovery_path(key["id"]).parent.mkdir(parents=True, exist_ok=True)
        toolctl._write_json(self.discovery_path(key["id"]), record)
        log.info("key discovery ok key=%s models=%d", key["id"], len(models))
        return models

    def key_views(self, keys_data: dict, manifest: Optional[dict]) -> list[dict]:
        counts: dict[str, int] = {}
        if manifest:
            for model in manifest["models"]:
                source = model.get("source")
                counts[source] = counts.get(source, 0) + 1
        priority = keys_data.get("model_priority") or {}
        priority_counts: dict[str, int] = {}
        for target in priority.values():
            priority_counts[target] = priority_counts.get(target, 0) + 1
        views = []
        for key in keys_store.ordered_keys(keys_data):
            discovery = self.read_discovery(key["id"])
            views.append({
                "id": key["id"],
                "name": key["name"],
                "masked_key": keys_store.mask_api_key(key["api_key"]),
                "has_key": bool(key["api_key"]),
                "upstream_base_url": key["upstream_base_url"],
                "enabled": key["enabled"],
                "order": key["order"],
                "selected_models": key.get("selected_models"),
                "discovered_count": len(discovery["models"]) if discovery else None,
                "discovered_models": list(discovery["models"]) if discovery else None,
                "discovered_at": discovery.get("fetched_at") if discovery else None,
                "included_count": counts.get(key["id"], 0),
                "priority_count": priority_counts.get(key["id"], 0),
            })
        return views

    def model_views(self, keys_data: dict, manifest: Optional[dict]) -> list[dict]:
        names = {key["id"]: key["name"] for key in keys_data["keys"]}
        rows = []
        if manifest:
            for model in manifest["models"]:
                source = model.get("source") or ""
                rows.append({
                    "slug": model["slug"],
                    "display_name": model["display_name"],
                    "upstream_model": model.get("upstream_model", model["slug"]),
                    "source": source,
                    "source_name": names.get(source, source),
                    "wire_api": model.get("wire_api") or "chat",
                    "priority": model["priority"],
                    "default_reasoning_level": model.get("default_reasoning_level"),
                    "context_window": model.get("context_window"),
                    "input_modalities": list(model.get("input_modalities") or ["text", "image"]),
                })
        return rows

    def conflict_views(self, keys_data: dict, official: list[dict]) -> list[dict]:
        candidates: dict[str, list[str]] = {}
        for key in keys_store.enabled_keys(keys_data):
            discovery = self.read_discovery(key["id"])
            if not discovery:
                continue
            for model in keys_store.apply_selected_models(key, discovery["models"]):
                candidates.setdefault(model, []).append(key["id"])
        # 官方条目按归一化身份匹配，`gpt-5.6-sol` 与 `GPT-5.6 Sol` 视为同一个模型。
        official_identities = {toolctl.model_identity(model["slug"]) for model in official}
        priority = keys_data.get("model_priority") or {}
        rows = []
        for model, ids in sorted(candidates.items()):
            targets = list(ids)
            if toolctl.model_identity(model) in official_identities:
                targets.append(keys_store.OFFICIAL_SOURCE)
            if len(targets) < 2:
                continue
            rows.append({
                "model": model,
                "candidates": targets,
                "chosen": priority.get(model) or ids[0],
            })
        return rows

    def state(self) -> dict:
        keys_data = self.keys_data()
        manifest = self._runtime_manifest()
        official = self._official_models()
        healthy = None
        if self.router.listening():
            try:
                healthy = toolctl.http_health(attempts=1)
            except (toolctl.ControlError, OSError, ValueError):
                healthy = None
        return {
            "ok": True,
            "version": VERSION,
            "app_version": APP_VERSION,
            "platform": "windows" if toolctl.is_windows() else "macos",
            "router": {
                "running": self.router.listening(),
                "port": ROUTER_PORT,
                "pid": self.router.pid(),
                "healthy": healthy,
            },
            "taken_over": toolctl._is_unified(self.p["config"]) if self.p["config"].exists() else False,
            "keys": self.key_views(keys_data, manifest),
            "models": self.model_views(keys_data, manifest),
            "official": {
                "count": len(official),
                "slug_count": len({model["slug"] for model in official}),
                "logged_in": (self.home / ".codex" / "auth.json").exists(),
                "source": "chatgpt-account",
                "models": list(
                    {
                        model["slug"]: {
                            "slug": model["slug"],
                            "display_name": model.get("display_name") or model["slug"],
                        }
                        for model in official
                    }.values()
                ),
            },
            "conflicts": self.conflict_views(keys_data, official),
            "model_priority": keys_data.get("model_priority") or {},
            "settings": self.settings_view(),
            "task": self.tasks.snapshot(),
            "events": self.events_view(),
            "logs": {
                "dir": str(self.p["logs"]),
                "toolctl": str(self.p["logs"] / "toolctl.log"),
            },
        }

    def settings_view(self) -> dict:
        raw = self._settings_raw()
        try:
            autostart_enabled = bool(self.autostart.is_enabled())
        except (OSError, autostart.AutostartError) as exc:
            log.warning("autostart status failed error=%s", type(exc).__name__)
            autostart_enabled = False
        return {
            "admin_port": int(raw.get("admin_port") or ADMIN_PORT),
            "theme": raw.get("theme") if raw.get("theme") in ("light", "dark") else "light",
            "ui_style": raw.get("ui_style") if raw.get("ui_style") in UI_STYLES else "layered",
            "hide_dock_icon": not toolctl.is_windows() and raw.get("hide_dock_icon") is not False,
            "router_port": ROUTER_PORT,
            "router_enabled": self.router_expected(),
            "autostart": autostart_enabled,
            "autostart_command": " ".join(self.autostart.command()),
            "config": str(self.p["config"]),
            "runtime": str(self.p["runtime"]),
            "logs": str(self.p["logs"]),
            "keys_file": str(self.p["keys"]),
            "previous_model": raw.get("previous_model"),
            "previous_reasoning_effort": raw.get("previous_reasoning_effort"),
        }

    # ------------------------------------------------------------------- keys
    def _find_key(self, keys_data: dict, key_id: str) -> dict:
        for key in keys_data["keys"]:
            if key["id"] == key_id:
                return key
        raise ApiError("key not found", 404, "key_not_found")

    def create_key(self, body: dict) -> dict:
        with self.lock:
            data = self.keys_data()
            existing = [key["id"] for key in data["keys"]]
            name = body.get("name") or "Key %d" % (len(existing) + 1)
            entry = keys_store.make_key(
                name,
                body.get("api_key") or "",
                upstream_base_url=body.get("upstream_base_url") or keys_store.DEFAULT_UPSTREAM,
                enabled=bool(body.get("enabled", True)),
                order=len(existing),
                existing=existing,
            )
            data["keys"].append(entry)
            saved = self.save_keys(data)
        self.add_event("key added name=%s id=%s" % (entry["name"], entry["id"]))
        return {"ok": True, "key": keys_store.key_index(saved)[entry["id"]], "keys": saved["keys"]}

    def update_key(self, key_id: str, body: dict) -> dict:
        with self.lock:
            data = self.keys_data()
            key = self._find_key(data, key_id)
            if "name" in body and body["name"] is not None:
                key["name"] = body["name"]
            if "upstream_base_url" in body and body["upstream_base_url"]:
                key["upstream_base_url"] = body["upstream_base_url"]
            if "enabled" in body and body["enabled"] is not None:
                key["enabled"] = bool(body["enabled"])
            api_key = body.get("api_key")
            if isinstance(api_key, str) and api_key and "*" not in api_key:
                key["api_key"] = api_key
            saved = self.save_keys(data)
        self.add_event("key updated id=%s" % key_id)
        return {"ok": True, "keys": saved["keys"]}

    def delete_key(self, key_id: str) -> dict:
        with self.lock:
            data = self.keys_data()
            key = self._find_key(data, key_id)
            data["keys"] = [item for item in data["keys"] if item["id"] != key_id]
            for index, item in enumerate(keys_store.ordered_keys(data)):
                item["order"] = index
            data["model_priority"] = {
                model: target for model, target in (data.get("model_priority") or {}).items()
                if target != key_id
            }
            saved = self.save_keys(data)
            self.discovery_path(key_id).unlink(missing_ok=True)
        self.add_event("key removed name=%s id=%s" % (key["name"], key_id))
        return {"ok": True, "keys": saved["keys"]}

    def reorder_keys(self, ids: list) -> dict:
        with self.lock:
            data = self.keys_data()
            known = {key["id"] for key in data["keys"]}
            if sorted(ids) != sorted(known):
                raise ApiError("order must list every key exactly once", 400, "bad_order")
            index = {key_id: position for position, key_id in enumerate(ids)}
            for key in data["keys"]:
                key["order"] = index[key["id"]]
            saved = self.save_keys(data)
        self.add_event("key order updated")
        return {"ok": True, "keys": saved["keys"]}

    def update_selected_models(self, key_id: str, selected) -> dict:
        with self.lock:
            data = self.keys_data()
            key = self._find_key(data, key_id)
            key["selected_models"] = selected
            saved = self.save_keys(data)
        self.add_event("key model selection saved id=%s count=%s" % (key_id, "all" if selected is None else len(selected)))
        return {"ok": True, "keys": saved["keys"]}

    def update_priority(self, priority: dict) -> dict:
        if not isinstance(priority, dict):
            raise ApiError("model_priority must be an object", 400, "bad_priority")
        with self.lock:
            data = self.keys_data()
            data["model_priority"] = priority
            saved = self.save_keys(data)
        self.add_event("model priority updated entries=%d" % len(priority))
        return {"ok": True, "model_priority": saved["model_priority"]}

    def update_model_override(self, body: dict) -> dict:
        """Save one upstream model's image switches; effective after a refresh."""
        upstream_model = str((body or {}).get("upstream_model") or "").strip()
        if not upstream_model:
            raise ApiError("upstream_model is required", 400, "bad_override")
        modalities = (body or {}).get("input_modalities")
        if (
            not isinstance(modalities, list) or not modalities
            or any(item not in toolctl.MODALITIES for item in modalities)
            or len(modalities) != len(set(modalities))
        ):
            raise ApiError("input_modalities must list text and/or image", 400, "bad_override")
        original = (body or {}).get("supports_image_detail_original")
        if original is not None and not isinstance(original, bool):
            raise ApiError("supports_image_detail_original must be a boolean", 400, "bad_override")
        with self.lock:
            raw = self._settings_raw()
            overrides = raw.get("model_overrides")
            if not isinstance(overrides, dict):
                overrides = {}
            entry = overrides.get(upstream_model)
            if not isinstance(entry, dict):
                entry = {}
            entry["input_modalities"] = list(modalities)
            if original is not None:
                entry["supports_image_detail_original"] = bool(original)
            overrides[upstream_model] = entry
            raw["model_overrides"] = overrides
            self._write_settings_raw(raw)
        self.add_event("model override saved model=%s modalities=%s" % (upstream_model, ",".join(modalities)))
        return {"ok": True, "state": self.state()}

    # ------------------------------------------------------------------ tasks
    def submit_discover(self, key_id: str) -> dict:
        with self.lock:
            key = self._find_key(self.keys_data(), key_id)
        if not key["api_key"]:
            raise ApiError("key has no api key yet", 400, "key_missing")
        def job() -> dict:
            models = self.discover_key(key)
            self.add_event("key model list fetched name=%s count=%d" % (key["name"], len(models)))
            return {"key_id": key_id, "discovered": len(models)}
        return self.tasks.submit("discover", job)

    def submit_refresh(self) -> dict:
        def job() -> dict:
            return self.refresh()
        return self.tasks.submit("refresh", job)

    def submit_discover_official(self) -> dict:
        return self.tasks.submit("discover_official", self.discover_official_models)

    def submit_take_over(self) -> dict:
        def job() -> dict:
            self._set_router_enabled(True)
            return self.take_over()
        return self.tasks.submit("enable", job)

    def submit_stop_router(self) -> dict:
        def job() -> dict:
            self._set_router_enabled(False)
            self.router.stop()
            self.add_event("router stopped")
            return {"router": "stopped"}
        return self.tasks.submit("stop", job)

    def submit_suspend_router(self) -> dict:
        """Stop the router because the user quit the tool window.

        Unlike the stop button this keeps Codex pointed at the local route, so
        the login autostart can bring the same route back on the next boot.
        """
        def job() -> dict:
            self.router.stop()
            log.info("router suspended on tool exit; Codex stays on the local route")
            self.add_event("router suspended on tool exit")
            return {"router": "suspended"}
        return self.tasks.submit("suspend", job)

    def set_service_quit_hook(self, hook: Callable[[], None]) -> None:
        """Register the shutdown callable used by ``/api/service/quit``."""
        self._service_quit_hook = hook

    def submit_service_quit(self) -> dict:
        """End the whole background service because the tool window closed.

        "Close tool" means the user is done with the app: the router process
        stops and the admin process leaves, so a later install or upgrade never
        has to fight over a locked runtime file such as ``python/_bz2.pyd``.
        Codex stays on the local route either way; the next launch (or the
        login autostart) starts a fresh service.
        """
        def job() -> None:
            try:
                self.router.stop()
            except (ApiError, OSError, subprocess.SubprocessError) as exc:
                log.warning("router stop during quit failed error=%s", exc)
            self.add_event("service quit on tool close")
            hook = self._service_quit_hook
            if hook is None:
                log.warning("service quit hook missing; admin keeps running")
                return
            log.info("service quit on tool close")
            hook()

        # The HTTP handler must answer before the server stops, so the teardown
        # runs on its own daemon thread instead of the request thread.
        threading.Thread(target=job, name="service-quit", daemon=True).start()
        return {"kind": "quit", "status": "running"}

    def submit_direct(self) -> dict:
        def job() -> dict:
            model = self.go_direct()
            self.add_event("switched back to official direct model=%s" % model)
            return {"model": model}
        return self.tasks.submit("direct", job)

    # --------------------------------------------------------------- workflow
    def discover_official_models(self) -> dict:
        started = time.monotonic()
        cache = toolctl.fetch_official_models(self.p)
        models = toolctl.validate_official_cache(cache)
        with self.lock:
            manifest = toolctl.load_manifest(self.p["manifest"]) if self.p["manifest"].exists() else {
                "version": 2, "sources": {"official": {"kind": "official"}}, "models": [],
            }
            catalog = toolctl.merge_catalog(cache, manifest)
            snapshot = {
                path: (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
                for path in (self.p["official_cache"], self.p["catalog"])
            }
            try:
                toolctl._write_json(self.p["official_cache"], cache)
                toolctl._write_json(self.p["catalog"], catalog)
            except Exception:
                log.warning("official model publish failed phase=write; restoring cache and catalog")
                toolctl._restore_runtime(snapshot)
                raise
        log.info(
            "official model publish completed official=%d local=%d elapsed_ms=%d",
            len(models), len(manifest["models"]), int((time.monotonic() - started) * 1000),
        )
        self.add_event("official model list fetched count=%d" % len(models))
        result = {"discovered": len(models)}
        # model_catalog_json is read when Codex starts; replacing it is not a hot reload.
        try:
            log.info("official model refresh phase=restart-codex")
            if toolctl._restart_chatgpt() is False:
                result["restart_pending"] = True
        except (toolctl.ControlError, OSError, subprocess.SubprocessError) as exc:
            log.warning("official models updated; Codex restart needed type=%s", type(exc).__name__)
            result["restart_pending"] = True
        if result.get("restart_pending"):
            self.add_event("official models updated; restart Codex manually")
        return result

    def refresh(self) -> dict:
        manifest, report = toolctl._resolve_runtime_manifest(self.package_dir, self.p)
        cache = toolctl._load_json(self.p["official_cache"], "official model cache")
        toolctl.validate_official_cache(cache)
        unified = toolctl._is_unified(self.p["config"]) if self.p["config"].exists() else False
        controller = self.router if unified else None
        snapshot = toolctl._runtime_snapshot(self.p)
        try:
            toolctl._prepare_runtime(self.package_dir, self.p, manifest, cache)
            if controller:
                controller.restart()
                health = controller.health()
                if (
                    health.get("status") != "ok"
                    or health.get("version") != toolctl.VERSION
                    or health.get("transport") != "http-only"
                    or health.get("bridge") != toolctl.BRIDGE_NAME
                    or health.get("local_model_count") != len(manifest["models"])
                ):
                    raise toolctl.ControlError("health check failed after refresh")
        except Exception:
            toolctl._restore_runtime(snapshot)
            if controller:
                with contextlib.suppress(Exception):
                    controller.restart()
            raise
        if unified:
            toolctl._restart_chatgpt()
        self.add_event(
            "refresh done models=%d keys=%d conflicts=%d"
            % (report.get("included", 0), len(report.get("keys", [])), len(report.get("conflicts", [])))
        )
        return {"report": report, "models": len(manifest["models"])}

    def take_over(self) -> dict:
        toolctl._check_zstd_decoder()
        manifest, report = toolctl._resolve_runtime_manifest(self.package_dir, self.p)
        cache = toolctl._load_json(self.p["official_cache"], "official model cache")
        toolctl.validate_official_cache(cache)
        source = self.p["config"].read_text(encoding="utf-8") if self.p["config"].exists() else ""
        current = toolctl.read_managed_config(source) if source else {}
        if current.get("model", "").startswith("local/"):
            current = {}
        raw = self._settings_raw()
        raw["previous_model"] = current.get("model")
        raw["previous_reasoning_effort"] = current.get("model_reasoning_effort")
        self._write_settings_raw(raw)
        model, effort = toolctl.select_fallback(
            cache,
            {"model": current.get("model"), "reasoning_effort": current.get("model_reasoning_effort")},
        )
        snapshot = toolctl._runtime_snapshot(self.p)
        changes = {
            "model": model,
            "model_reasoning_effort": effort,
            "model_provider": toolctl.MODEL_PROVIDER_ID,
            "openai_base_url": None,
            "model_catalog_json": str(self.p["catalog"]),
        }
        try:
            toolctl._prepare_runtime(self.package_dir, self.p, manifest, cache)
            toolctl.enable_transaction(
                self.p["config"], self.p["backups"], self.router, changes,
                len(manifest["models"]), toolctl.PROVIDER_CONFIG,
            )
        except Exception:
            toolctl._restore_runtime(snapshot)
            raise
        toolctl._restart_chatgpt()
        self.add_event("router enabled models=%d" % len(manifest["models"]))
        return {"report": report, "models": len(manifest["models"]), "model": model, "effort": effort}

    def go_direct(self) -> str:
        cache = toolctl._load_json(self.p["official_cache"], "official model cache")
        raw = self._settings_raw()
        model, effort = toolctl.select_fallback(
            cache,
            {"model": raw.get("previous_model"), "reasoning_effort": raw.get("previous_reasoning_effort")},
        )
        changes = {
            "model": model,
            "model_reasoning_effort": effort,
            "model_provider": "openai",
            "openai_base_url": None,
            "model_catalog_json": None,
        }
        toolctl.write_config(self.p["config"], changes, self.p["backups"], None)
        toolctl._restart_chatgpt()
        self.router.stop()
        self._set_router_enabled(False)
        return model

    def update_settings(self, body: dict) -> dict:
        if "ui_style" in body and body["ui_style"] not in UI_STYLES:
            log.warning("界面风格保存被拒绝：风格值不在允许范围内")
            raise ApiError("ui_style must be layered, chaos or mintlify", 400, "bad_ui_style")
        if "hide_dock_icon" in body:
            if toolctl.is_windows():
                raise ApiError("hide_dock_icon is only available on macOS", 400, "macos_only")
            if type(body["hide_dock_icon"]) is not bool:
                raise ApiError("hide_dock_icon must be a boolean", 400, "bad_dock_visibility")
        if "autostart" in body and body["autostart"] is not None:
            service = self.autostart
            if bool(body["autostart"]):
                service.enable()
                self.add_event("autostart enabled")
            else:
                service.disable()
                self.add_event("autostart disabled")
        if "admin_port" in body and body["admin_port"] is not None:
            try:
                port = int(body["admin_port"])
            except (TypeError, ValueError) as exc:
                raise ApiError("admin_port must be a number", 400, "bad_port") from exc
            if not 1024 <= port <= 65535:
                raise ApiError("admin_port out of range", 400, "bad_port")
            raw = self._settings_raw()
            raw["admin_port"] = port
            self._write_settings_raw(raw)
            self.add_event("admin port saved port=%d" % port)
        if "theme" in body and body["theme"] is not None:
            theme = str(body["theme"])
            if theme not in ("light", "dark"):
                raise ApiError("theme must be light or dark", 400, "bad_theme")
            raw = self._settings_raw()
            raw["theme"] = theme
            self._write_settings_raw(raw)
            log.info("theme saved theme=%s", theme)
            self.add_event("theme saved theme=%s" % theme)
        if "ui_style" in body:
            raw = self._settings_raw()
            raw["ui_style"] = body["ui_style"]
            self._write_settings_raw(raw)
            self.add_event("界面风格已保存 style=%s" % body["ui_style"])
        if "hide_dock_icon" in body:
            raw = self._settings_raw()
            raw["hide_dock_icon"] = body["hide_dock_icon"]
            self._write_settings_raw(raw)
            self.add_event("hide_dock_icon saved value=%s" % body["hide_dock_icon"])
        return {"ok": True, "settings": self.settings_view()}

    def open_logs(self) -> None:
        target = str(self.p["logs"])
        self.p["logs"].mkdir(parents=True, exist_ok=True)
        command = ["explorer", target] if toolctl.is_windows() else ["open", target]
        try:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise ApiError("cannot open log folder: %s" % type(exc).__name__, 500, "open_failed") from exc

    def on_start(self) -> None:
        """Silent boot path: bring the router back when Codex is still taken over."""
        self.heal_catalog()
        retired = self.router.retire_legacy_autostart()
        if retired:
            self.add_event("legacy tool autostart retired: %s" % ", ".join(retired))
        self.repair_autostart()
        enabled = self.router_expected()
        config_exists = self.p["config"].exists()
        unified = False
        if config_exists:
            try:
                unified = toolctl._is_unified(self.p["config"])
            except (OSError, toolctl.ValidationError):
                unified = False
        listening = self.router.listening()
        # The boot decision is logged field by field so "the router is stopped
        # after a reboot" can be answered from the log instead of guessed.
        log.info(
            "admin boot decision router_enabled=%s config_exists=%s codex_unified=%s listening=%s",
            enabled, config_exists, unified, listening,
        )
        if not config_exists or not unified:
            return
        if not enabled:
            self.add_event("router auto start skipped: router is switched off in settings")
            return
        if listening:
            log.info("router auto start skipped reason=already-listening")
            return
        try:
            self.router.ensure_running()
            toolctl.http_health(attempts=20)
            self.add_event("router auto started on admin boot")
        except Exception as exc:  # noqa: BLE001 - boot must never crash the UI
            log.exception("router auto start failed type=%s", type(exc).__name__)
            self.add_event("router auto start failed: %s" % type(exc).__name__, "error")

    def heal_catalog(self) -> bool:
        """Rebuild the Codex catalog from cached files with the current merge rules.

        A release upgrade must not leave the old catalog on disk: the previous
        merge kept an official model next to a Key model with the same name, and
        the duplicate rows only disappeared after a manual refresh. Rebuilding
        here is offline, uses the same merge_catalog() as the refresh flow, and
        only rewrites the derived catalog file.
        """
        try:
            manifest = toolctl.load_manifest(self.p["manifest"])
            cache = toolctl._load_json(self.p["official_cache"], "official cache")
            toolctl.validate_official_cache(cache)
            catalog = toolctl.merge_catalog(cache, manifest)
        except Exception as exc:  # noqa: BLE001 - a boot helper must never crash the UI
            log.warning("catalog self heal skipped error=%s", type(exc).__name__)
            return False
        try:
            current = json.loads(self.p["catalog"].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = None
        if current == catalog:
            return False
        try:
            toolctl._write_json(self.p["catalog"], catalog)
        except OSError as exc:
            log.warning("catalog self heal write failed error=%s", type(exc).__name__)
            return False
        log.info("catalog self healed models=%d", len(catalog["models"]))
        self.add_event("catalog rebuilt with the current duplicate rules: models=%d" % len(catalog["models"]))
        return True


class AdminHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    service: AdminService

    def log_message(self, fmt, *args) -> None:  # console stays quiet, file keeps the trail
        log.info("http %s", fmt % args)

    # -------------------------------------------------------------- responses
    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _ui_dir(self) -> Path:
        override = os.environ.get("CODEX_MULTIKEY_UI")
        candidates: list[Path] = []
        if override:
            path = Path(override)
            candidates.append(path.parent if path.is_file() else path)
        package_ui = self.service.package_dir.parent / "ui"
        candidates += [package_ui, self.service.package_dir / "ui"]
        for directory in candidates:
            if (directory / "index.html").is_file():
                return directory
        return package_ui

    def _ui_path(self) -> Path:
        return self._ui_dir() / "index.html"

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise ApiError("invalid content length", 400, "bad_length") from exc
        if length > MAX_BODY:
            raise ApiError("request body too large", 413, "body_too_large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError("request body must be utf-8 json", 400, "bad_json") from exc
        if not isinstance(data, dict):
            raise ApiError("request body must be a json object", 400, "bad_json")
        return data

    def _check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if not origin:
            return
        port = self.server.server_address[1]
        allowed = {"http://127.0.0.1:%d" % port, "http://localhost:%d" % port}
        if origin not in allowed:
            raise ApiError("cross origin request rejected", 403, "forbidden_origin")

    # ---------------------------------------------------------------- routing
    def _handle(self) -> None:
        try:
            self._check_origin()
            self._dispatch()
        except ApiError as exc:
            self._json({"ok": False, "error": str(exc), "code": exc.code}, exc.status)
        except (keys_store.KeysError,) as exc:
            log.warning("key config rejected error=%s", exc)
            self._json({"ok": False, "error": str(exc), "code": "invalid_keys"}, 400)
        except (toolctl.ValidationError, toolctl.ControlError, OSError, ValueError) as exc:
            log.warning("request failed type=%s error=%s", type(exc).__name__, exc)
            self._json({"ok": False, "error": str(exc), "code": "operation_failed"}, 400)
        except Exception as exc:  # noqa: BLE001 - never kill the server thread
            log.exception("unhandled api error type=%s", type(exc).__name__)
            self._json({"ok": False, "error": type(exc).__name__, "code": "internal_error"}, 500)

    def _dispatch(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        method = self.command
        service = self.service

        if path in ("/", "/index.html") and method == "GET":
            ui = self._ui_path()
            if not ui.is_file():
                self._json({"ok": False, "error": "ui file missing", "code": "ui_missing"}, 500)
                return
            self._bytes(ui.read_bytes(), "text/html; charset=utf-8")
            return
        if path in ("/favicon.ico", "/logo.png"):
            logo = self._ui_dir() / "logo.png"
            if logo.is_file():
                self._bytes(logo.read_bytes(), "image/png")
            else:
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            return
        if path == "/healthz" and method == "GET":
            # app_version plus build (the admin.py fingerprint) let the desktop
            # launcher detect a service left over from an older release, or from
            # a same-version hot fix, instead of reusing it.
            self._json({
                "ok": True,
                "version": VERSION,
                "app_version": APP_VERSION,
                "build": BUILD,
                "service": "admin",
            })
            return
        if path == "/api/state" and method == "GET":
            self._json(service.state())
            return
        if path == "/api/settings" and method == "GET":
            self._json({"ok": True, "settings": service.settings_view()})
            return
        if path == "/api/settings" and method == "PUT":
            self._json(service.update_settings(self._read_body()))
            return
        if path == "/api/logs" and method == "GET":
            try:
                lines = int((query.get("lines") or ["200"])[0])
            except ValueError:
                lines = 200
            lines = max(10, min(lines, 2000))
            self._json({
                "ok": True,
                "events": service.events_view(lines),
                "toolctl": _tail_lines(service.p["logs"] / "toolctl.log", lines),
                "router": _tail_lines(service.p["logs"] / "router.stderr.log", max(10, lines // 4)),
            })
            return
        if path == "/api/keys" and method == "POST":
            self._json(service.create_key(self._read_body()))
            return
        if path == "/api/keys/order" and method == "PUT":
            body = self._read_body()
            self._json(service.reorder_keys(body.get("ids") or []))
            return
        if path == "/api/priority" and method == "PUT":
            body = self._read_body()
            self._json(service.update_priority(body.get("model_priority") or {}))
            return
        if path == "/api/models/overrides" and method == "PUT":
            self._json(service.update_model_override(self._read_body()))
            return
        if path == "/api/refresh" and method == "POST":
            self._json(service.submit_refresh(), 202)
            return
        if path == "/api/official/models/discover" and method == "POST":
            self._json(service.submit_discover_official(), 202)
            return
        if path == "/api/router/start" and method == "POST":
            self._json(service.submit_take_over(), 202)
            return
        if path == "/api/router/stop" and method == "POST":
            self._json(service.submit_stop_router(), 202)
            return
        if path == "/api/router/suspend" and method == "POST":
            self._json(service.submit_suspend_router(), 202)
            return
        if path == "/api/service/quit" and method == "POST":
            self._json(service.submit_service_quit(), 202)
            return
        if path == "/api/router/direct" and method == "POST":
            self._json(service.submit_direct(), 202)
            return
        if path == "/api/system/open-logs" and method == "POST":
            service.open_logs()
            self._json({"ok": True})
            return
        if path == "/api/autostart/repair" and method == "POST":
            self._json({"ok": True, **(service.repair_autostart() or {})})
            return
        match = KEY_PATH.match(path)
        if match:
            key_id, action = match.group(1), match.group(2)
            if action is None and method == "PUT":
                self._json(service.update_key(key_id, self._read_body()))
                return
            if action is None and method == "DELETE":
                self._json(service.delete_key(key_id))
                return
            if action in ("discover", "test") and method == "POST":
                self._json(service.submit_discover(key_id), 202)
                return
            if action == "models" and method == "PUT":
                body = self._read_body()
                selected = body["selected_models"] if "selected_models" in body else None
                self._json(service.update_selected_models(key_id, selected))
                return
        self._json({"ok": False, "error": "not found", "code": "not_found"}, 404)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle


def _configure_stdio() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _port_in_use(port: int) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((ADMIN_HOST, port)) == 0


def main(argv: Optional[list[str]] = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="Codex multi key router admin service")
    parser.add_argument("--package", type=Path, default=APP_DIR)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--launcher",
        type=Path,
        help="desktop launcher path, stored in the login autostart entry",
    )
    parser.add_argument("--no-router", action="store_true", help="do not auto start the router")
    parser.add_argument(
        "--print-url",
        action="store_true",
        help="print the admin base url and exit (used by the desktop launcher)",
    )
    parser.add_argument(
        "--stop-stale",
        action="store_true",
        help="stop an admin service from an older release and exit (used by the desktop launcher)",
    )
    args = parser.parse_args(argv)
    package_dir = Path(args.package).resolve()
    p = toolctl.paths(args.home, package_dir)
    toolctl.configure_logging(p["logs"])
    configure_logging(p["logs"])
    service = AdminService(package_dir, p, home=args.home, launcher=args.launcher)
    port = args.port or service.settings_view()["admin_port"]
    if args.print_url:
        url = "http://%s:%d" % (ADMIN_HOST, port)
        log.info("admin url requested url=%s", url)
        print(url)
        return 0
    if args.stop_stale:
        return _stop_stale_admin(port)
    if _port_in_use(port):
        log.info("admin service already running port=%d", port)
        print("admin service already running on port %d" % port)
        return 0
    server = ThreadingHTTPServer((ADMIN_HOST, port), AdminHandler)
    server.daemon_threads = True
    AdminHandler.service = service

    def stop_admin_server() -> None:
        # /api/service/quit answers before the teardown starts, so wait for that
        # response to leave the socket and then leave serve_forever. Closing the
        # service also releases the packaged runtime files (python/_bz2.pyd and
        # friends) that Windows locks while this process is alive.
        time.sleep(0.3)
        log.info("admin service quitting because the tool window closed")
        server.shutdown()

    service.set_service_quit_hook(stop_admin_server)
    log.info(
        "admin startup api_version=%s app_version=%s port=%d package=%s",
        VERSION, APP_VERSION, port, package_dir,
    )
    if not args.no_router:
        service.on_start()
    thread = threading.Thread(target=server.serve_forever, name="admin-http", daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            thread.join(timeout=1)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        log.info("admin shutdown complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
