#!/usr/bin/env python3
"""Build the macOS dmg for the Codex multi key router.

Steps:

1. download and verify the python-build-standalone runtime and the zstd source,
2. build the Go launcher (cgo + WKWebView) and compile zstd for arm64,
3. assemble the .app bundle (info.plist, launcher, app files, UI, runtime),
4. ad-hoc sign every Mach-O file and the bundle so arm64 macOS will run it,
5. create the dmg, then mount it and run the embedded python from it.

Source in this file stays ASCII on purpose (plan section 5); the app name is
written with unicode escapes and lands in the bundle through trove/plist tools.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from packager import CACHE_DIR, DIST_DIR, PROJECT_ROOT, fetch, icon_assets, log_line

APP_VERSION = "1.2.21"
APP_NAME = "Codex \u8def\u7531"
APP_BUNDLE_NAME = APP_NAME + ".app"
LAUNCHER_NAME = "CodexMultiKey"
BUNDLE_ID = "com.codexmultikey.router"
DMG_NAME = f"CodexRouter-{APP_VERSION}-arm64.dmg"

PACKAGING_DIR = PROJECT_ROOT / "packaging"
WORK_DIR = ".work" / Path("macos")
WORK_ROOT = DIST_DIR / WORK_DIR
DMG_ROOT = WORK_ROOT / "dmg-root"
APP_BUNDLE = DMG_ROOT / APP_BUNDLE_NAME

PYTHON_TAG = "20260901"
PYTHON_VERSION = "3.12.14"
PYTHON_ASSET = f"cpython-{PYTHON_VERSION}+{PYTHON_TAG}-aarch64-apple-darwin-install_only.tar.gz"
PYTHON_URL = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PYTHON_TAG}/{PYTHON_ASSET}"
PYTHON_SHA256 = "3ee3ee547cedfeb7c2b16b2b7156039f7b470bb8f857e226fd3d2eb11db83c76"

ZSTD_VERSION = "1.5.7"
ZSTD_ASSET = f"zstd-{ZSTD_VERSION}.tar.gz"
ZSTD_URL = f"https://github.com/facebook/zstd/releases/download/v{ZSTD_VERSION}/{ZSTD_ASSET}"
ZSTD_SHA256 = "eb33e51f49a15e023950cd7825ca74a4a2b43db8354825ac24fc1b7ee09e6fa3"

APP_FILES = (
    "admin.py",
    "autostart.py",
    "keys_store.py",
    "local-models.json",
    "router.py",
    "toolctl.py",
)

MACHO_MAGICS = {0xFEEDFACE, 0xFEEDFACF, 0xCEFAEDFE, 0xCFFAEDFE, 0xCAFEBABE, 0xBEBAFECA}


def require_build_host() -> None:
    if sys.platform != "darwin":
        raise SystemExit("the macOS dmg must be built on macOS")
    for tool in ("go", "make", "hdiutil", "codesign"):
        if shutil.which(tool) is None:
            raise SystemExit(f"missing build tool: {tool}")
    if os.uname().machine != "arm64":
        raise SystemExit("this build targets Apple Silicon; run it on an arm64 mac")


def is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(4)
    except OSError:
        return False
    if len(head) < 4:
        return False
    return int.from_bytes(head, "big") in MACHO_MAGICS


def go_build(output: Path) -> None:
    env = os.environ.copy()
    env.update({"GOOS": "darwin", "GOARCH": "arm64", "CGO_ENABLED": "1"})
    command = ["go", "build", "-buildvcs=false", "-trimpath", "-ldflags", "-s -w", "-o", str(output), "."]
    log_line(f"go build {output.name} cwd=launcher")
    subprocess.run(command, cwd=PROJECT_ROOT / "launcher", env=env, check=True)


def extract_python_runtime(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if len(parts) < 2 or parts[0] != "python":
                continue
            member.name = str(Path(*parts[1:]))
            tar.extract(member, target, filter="tar")
            count += 1
    log_line(f"embed python extracted members={count} target={target}")


def build_zstd(archive: Path) -> Path:
    source_root = WORK_ROOT / "zstd-src"
    if source_root.exists():
        shutil.rmtree(source_root)
    source_root.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(source_root, filter="tar")
    source = source_root / f"zstd-{ZSTD_VERSION}"
    jobs = max(2, min(4, os.cpu_count() or 2))
    subprocess.run(["make", f"-j{jobs}", "-C", str(source / "programs"), "zstd"], check=True)
    binary = source / "programs" / "zstd"
    if not binary.is_file() or not is_macho(binary):
        raise SystemExit("zstd build did not produce a Mach-O binary")
    log_line(f"zstd built arch=arm64 version={ZSTD_VERSION}")
    return binary


def install_bundle_icons(contents: Path, ui_target: Path, assets: dict[str, Path]) -> str | None:
    """Copy the packaged marks and build AppIcon.icns from the source image."""
    resources = contents / "Resources"
    resources.mkdir(parents=True, exist_ok=True)
    status = assets.get("status_png"), assets.get("status_png_2x")
    if all(item and item.is_file() for item in status):
        shutil.copy2(status[0], resources / "statusbar.png")
        shutil.copy2(status[1], resources / "statusbar@2x.png")
        log_line("menu bar icons copied")
    logo = assets.get("ui_logo")
    if logo and logo.is_file():
        shutil.copy2(logo, ui_target / "logo.png")
    source = assets.get("source")
    if not source or not source.is_file():
        log_line("app icon skipped: no source image")
        return None
    try:
        import icons

        resizer = icons.Resizer(icons.app_icon_image(source, assets.get("mark")))
        iconset = WORK_ROOT / "AppIcon.iconset"
        icons.write_iconset(resizer, iconset, source=source)
        icons.build_icns(iconset, resources / "AppIcon.icns")
    except Exception as exc:
        log_line(f"app icon skipped: {exc}")
        return None
    return "AppIcon"


def assemble_bundle(launcher: Path, zstd_binary: Path, python_archive: Path, assets: dict[str, Path]) -> None:
    if DMG_ROOT.exists():
        shutil.rmtree(DMG_ROOT)
    contents = APP_BUNDLE / "Contents"
    macos_dir = contents / "MacOS"
    resources = contents / "Resources" / "runtime"
    app_target = resources / "app"
    ui_target = resources / "ui"
    for directory in (macos_dir, app_target, ui_target):
        directory.mkdir(parents=True)

    shutil.copy2(launcher, macos_dir / LAUNCHER_NAME)
    for name in APP_FILES:
        source = PROJECT_ROOT / "app" / name
        if not source.is_file():
            raise SystemExit(f"missing app file: {source}")
        shutil.copy2(source, app_target / name)
    index = PROJECT_ROOT / "ui" / "index.html"
    if not index.is_file():
        raise SystemExit(f"missing ui file: {index}")
    shutil.copy2(index, ui_target / "index.html")
    shutil.copy2(zstd_binary, app_target / "zstd")
    (app_target / "zstd").chmod(0o755)
    extract_python_runtime(python_archive, resources / "python")
    icon_file = install_bundle_icons(contents, ui_target, assets)
    write_info_plist(contents / "Info.plist", icon_file)
    (DMG_ROOT / "Applications").symlink_to("/Applications")
    log_line(f"app assembled path={APP_BUNDLE}")


def write_info_plist(target: Path, icon_file: str | None = None) -> None:
    payload = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": LAUNCHER_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        # Start without a Dock tile; the launcher restores Regular mode when requested.
        "LSUIElement": True,
        "LSApplicationCategoryType": "public.app-category.developer-tools",
    }
    if icon_file:
        payload["CFBundleIconFile"] = icon_file
    with target.open("wb") as handle:
        plistlib.dump(payload, handle)
    log_line(f"info plist written path={target}")


def sign_bundle() -> None:
    signed = 0
    for path in sorted(APP_BUNDLE.rglob("*")):
        if path.is_symlink() or not path.is_file() or not is_macho(path):
            continue
        subprocess.run(
            ["codesign", "--force", "--sign", "-", "--timestamp=none", str(path)],
            check=True,
            capture_output=True,
        )
        signed += 1
    subprocess.run(
        ["codesign", "--force", "--sign", "-", "--timestamp=none", str(APP_BUNDLE)],
        check=True,
        capture_output=True,
    )
    subprocess.run(["codesign", "--verify", "--verbose=2", str(APP_BUNDLE)], check=True, capture_output=True)
    log_line(f"ad-hoc signature ok macho_files={signed}")


def verify_embedded_python(app: Path) -> None:
    python = app / "Contents" / "Resources" / "runtime" / "python" / "bin" / "python3"
    if not python.is_file():
        raise SystemExit("the app bundle has no embedded python")
    version = subprocess.run([str(python), "--version"], capture_output=True, text=True)
    if version.returncode != 0:
        raise SystemExit(f"embedded python failed: {version.stderr.strip()}")
    app_dir = app / "Contents" / "Resources" / "runtime" / "app"
    home = tempfile.mkdtemp(prefix="codexmk-smoke-")
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    try:
        result = subprocess.run(
            [str(python), str(app_dir / "admin.py"), "--package", str(app_dir), "--home", home, "--print-url"],
            cwd=app_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if result.returncode != 0 or "http://127.0.0.1:" not in result.stdout:
        raise SystemExit(
            "embedded python could not start the admin service\n"
            f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    log_line(f"embedded python ok {version.stdout.strip()} admin={result.stdout.strip()}")


def create_dmg(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "hdiutil", "create",
            "-volname", APP_NAME,
            "-srcfolder", str(DMG_ROOT),
            "-ov", "-format", "UDZO",
            str(output),
        ],
        check=True,
    )
    subprocess.run(["hdiutil", "verify", str(output)], check=True, capture_output=True)
    log_line(f"dmg created bytes={output.stat().st_size} path={output}")


def verify_dmg(output: Path) -> None:
    mount = Path(tempfile.mkdtemp(prefix="codexmk-dmg-"))
    attached = False
    try:
        subprocess.run(
            ["hdiutil", "attach", str(output), "-nobrowse", "-readonly", "-mountpoint", str(mount)],
            check=True,
            capture_output=True,
        )
        attached = True
        app = mount / APP_BUNDLE_NAME
        binary = app / "Contents" / "MacOS" / LAUNCHER_NAME
        if not binary.is_file():
            raise SystemExit("the dmg does not contain the launcher")
        python = app / "Contents" / "Resources" / "runtime" / "python" / "bin" / "python3"
        result = subprocess.run([str(python), "--version"], capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(f"embedded python cannot run from the mounted dmg: {result.stderr.strip()}")
        log_line(f"dmg mount check ok python={result.stdout.strip()}")
    finally:
        if attached:
            subprocess.run(["hdiutil", "detach", str(mount)], capture_output=True)
        shutil.rmtree(mount, ignore_errors=True)


def build(output: Path, cache: Path) -> Path:
    require_build_host()
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    python_archive = fetch(PYTHON_URL, PYTHON_ASSET, PYTHON_SHA256, cache)
    zstd_archive = fetch(ZSTD_URL, ZSTD_ASSET, ZSTD_SHA256, cache)

    launcher = WORK_ROOT / LAUNCHER_NAME
    go_build(launcher)
    zstd_binary = build_zstd(zstd_archive)
    assemble_bundle(launcher, zstd_binary, python_archive, icon_assets())
    sign_bundle()
    verify_embedded_python(APP_BUNDLE)
    create_dmg(output)
    verify_dmg(output)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the macOS dmg for Codex multi key router")
    parser.add_argument("--output", type=Path, default=DIST_DIR / DMG_NAME)
    parser.add_argument("--cache", type=Path, default=CACHE_DIR)
    args = parser.parse_args(argv)
    output = build(args.output, args.cache)
    print(f"macos dmg: {output}")
    print(f"size: {output.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
