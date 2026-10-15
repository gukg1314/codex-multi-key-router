#!/usr/bin/env python3
"""Build the Windows installer for the Codex multi key router.

Everything is produced on the build machine, macOS included:

1. download and verify the Python 3.12 embed runtime and zstd,
2. cross compile the Go launcher and install helper,
3. assemble the payload folder (app files, UI, embed runtime, zstd),
4. compile the NSIS installer from the staged folder,
5. verify the installer and smoke run the packaged admin.py.

Source in this file stays ASCII on purpose (plan section 5). Files written for
Windows are UTF-8 without BOM; the readme keeps CRLF for Notepad.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

from packager import (
    CACHE_DIR,
    DIST_DIR,
    PROJECT_ROOT,
    PYTHON_ARCHIVE,
    PYTHON_SHA256,
    PYTHON_URL,
    ZSTD_ARCHIVE,
    ZSTD_MEMBER,
    ZSTD_SHA256,
    ZSTD_URL,
    check_embedded_stdlib,
    fetch,
    icon_assets,
    log_line,
    utf8_bom_crlf,
)

SETUP_VERSION = "1.2.26"
LAUNCHER_EXE = "CodexMultiKey.exe"
HELPER_EXE = "installctl.exe"
SETUP_NAME = f"CodexRouter-Setup-{SETUP_VERSION}.exe"
# Unicode escapes keep this builder ASCII while the installed file stays Chinese.
README_NAME = "\u4f7f\u7528\u8bf4\u660e.txt"

PACKAGING_DIR = PROJECT_ROOT / "packaging"
README_TEMPLATE = PACKAGING_DIR / "templates" / "windows-readme.txt"
NSIS_SCRIPT = PACKAGING_DIR / "templates" / "router-installer.nsi"
# The icon source of truth. go-winres folds it into the Go linker objects, so
# CodexMultiKey.exe carries resource id 1 (the launcher asks WebView2 for that
# id when it builds the window). NSIS applies this icon to setup separately.
APP_ICON = PACKAGING_DIR / "assets" / "app.ico"
UI_LOGO = PROJECT_ROOT / "ui" / "logo.png"
WINRES_TOOL = "github.com/tc-hib/go-winres@v0.3.3"
WORK_DIR = DIST_DIR / ".work" / "windows"
WINRES_SPEC = WORK_DIR / "winres"
PAYLOAD_DIR = WORK_DIR / "payload"
OUTPUT_PATH = DIST_DIR / SETUP_NAME

APP_FILES = (
    "admin.py",
    "autostart.py",
    "keys_store.py",
    "local-models.json",
    "router.py",
    "toolctl.py",
)

# A package that misses any of these would install but never start. NSIS writes
# uninstall.exe during installation, so it is deliberately absent here.
PAYLOAD_REQUIRED = (
    LAUNCHER_EXE,
    README_NAME,
    "runtime/app/admin.py",
    "runtime/app/autostart.py",
    "runtime/app/keys_store.py",
    "runtime/app/local-models.json",
    "runtime/app/router.py",
    "runtime/app/toolctl.py",
    "runtime/app/zstd.exe",
    "runtime/ui/index.html",
    "runtime/python/python.exe",
    "runtime/python/pythonw.exe",
    "runtime/python/python312.zip",
    "runtime/python/python312._pth",
)

GO_TARGET_ENV = {"GOOS": "windows", "GOARCH": "amd64", "CGO_ENABLED": "0"}
GO_LDFLAGS = "-H=windowsgui -s -w"
MIN_SETUP_BYTES = 10 * 1024 * 1024
NSIS_SIGNATURE = b"\xef\xbe\xad\xdeNullsoftInst"
NSIS_CRC_START = 512


def require_go() -> None:
    if shutil.which("go") is None:
        raise SystemExit("go toolchain not found; install Go to cross compile the Windows binaries")


def find_makensis() -> str:
    executable = shutil.which("makensis")
    if executable:
        return executable
    if os.name == "nt":
        for variable in ("ProgramFiles(x86)", "ProgramFiles"):
            program_files = os.environ.get(variable)
            if not program_files:
                continue
            candidate = Path(program_files) / "NSIS" / "makensis.exe"
            if candidate.is_file():
                return str(candidate)
    raise SystemExit("makensis not found; install NSIS to build the Windows installer")


def go_build(package_dir: Path, output: Path, tags: tuple[str, ...] = ()) -> None:
    env = os.environ.copy()
    env.update(GO_TARGET_ENV)
    command = ["go", "build", "-buildvcs=false", "-trimpath", "-ldflags", GO_LDFLAGS]
    if tags:
        command += ["-tags", ",".join(tags)]
    command += ["-o", str(output), "."]
    log_line(f"go build {output.name} tags={','.join(tags) or '-'} cwd={package_dir.name}")
    subprocess.run(command, cwd=package_dir, env=env, check=True)


def winres_spec(spec_dir: Path, icon: Path, original_name: str) -> str:
    """Resource description: icon id 1 (WebView2 asks for it) plus version."""
    payload = {
        "RT_GROUP_ICON": {"#1": {"0000": os.path.relpath(icon, spec_dir)}},
        "RT_VERSION": {
            "#1": {
                "0000": {
                    "fixed": {
                        "file_version": SETUP_VERSION + ".0",
                        "product_version": SETUP_VERSION + ".0",
                    },
                    "info": {
                        "0409": {
                            "CompanyName": "Codex Multi Key Router",
                            "FileDescription": "Codex Multi Key Router",
                            "FileVersion": SETUP_VERSION,
                            "InternalName": original_name,
                            "OriginalFilename": original_name,
                            "ProductName": "Codex Multi Key Router",
                            "ProductVersion": SETUP_VERSION,
                        }
                    },
                }
            }
        },
    }
    return json.dumps(payload, indent=2)


def embed_icon(package_dir: Path, icon: Path, original_name: str) -> Path | None:
    """Stage the icon as a .syso the Go linker merges into the exe.

    go-winres is fetched on demand. When the download or the tool run fails the
    build keeps going with the default Windows icon instead of stopping.
    """
    if not icon.is_file():
        log_line("app icon missing; the exe keeps the default icon")
        return None
    spec_dir = WINRES_SPEC / package_dir.name
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_dir / "winres.json"
    spec.write_text(winres_spec(spec_dir, icon, original_name), encoding="utf-8")
    command = [
        "go", "run", WINRES_TOOL, "make",
        "--in", str(spec),
        "--out", str(package_dir / "rsrc"),
        "--arch", "amd64",
    ]
    try:
        subprocess.run(command, cwd=package_dir, env=os.environ.copy(), check=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        log_line(f"go-winres unavailable ({exc}); the exe keeps the default icon")
        return None
    staged = package_dir / "rsrc_windows_amd64.syso"
    if not staged.is_file():
        log_line("go-winres produced no syso; the exe keeps the default icon")
        return None
    log_line(f"exe icon staged name={original_name} syso={staged.name}")
    return staged


def drop_artifacts(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def pe_group_icon_ids(path: Path) -> list[int]:
    """Resource ids of RT_GROUP_ICON (14) in a PE file."""
    data = path.read_bytes()
    if not is_pe_file(data):
        return []
    coff = int.from_bytes(data[0x3C:0x40], "little") + 4
    sections = int.from_bytes(data[coff + 2 : coff + 4], "little")
    optional_size = int.from_bytes(data[coff + 16 : coff + 18], "little")
    optional = coff + 20
    directories = optional + (112 if int.from_bytes(data[optional : optional + 2], "little") == 0x20B else 96)
    resource_rva = int.from_bytes(data[directories + 16 : directories + 20], "little")
    if resource_rva == 0:
        return []
    root = None
    for index in range(sections):
        header = optional + optional_size + index * 40
        size = max(
            int.from_bytes(data[header + 8 : header + 12], "little"),
            int.from_bytes(data[header + 16 : header + 20], "little"),
        )
        base = int.from_bytes(data[header + 12 : header + 16], "little")
        if base <= resource_rva < base + size:
            root = resource_rva - base + int.from_bytes(data[header + 20 : header + 24], "little")
            break
    if root is None:
        return []
    named = int.from_bytes(data[root + 12 : root + 14], "little")
    total = named + int.from_bytes(data[root + 14 : root + 16], "little")
    for index in range(total):
        entry = root + 16 + index * 8
        type_id = int.from_bytes(data[entry : entry + 4], "little")
        if type_id != 14:
            continue
        child = root + (int.from_bytes(data[entry + 4 : entry + 8], "little") & 0x7FFFFFFF)
        count = int.from_bytes(data[child + 12 : child + 14], "little") + int.from_bytes(
            data[child + 14 : child + 16], "little"
        )
        return [
            int.from_bytes(data[child + 16 + item * 8 : child + 20 + item * 8], "little") for item in range(count)
        ]
    return []


def verify_exe_icon(path: Path) -> bool:
    ids = pe_group_icon_ids(path)
    if 1 in ids:
        log_line(f"exe icon check ok name={path.name} group_ids={ids}")
        return True
    log_line(f"warning: {path.name} has no icon resource 1 (group_ids={ids})")
    return False


def extract_python_runtime(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(archive) as embedded:
        for member in embedded.infolist():
            if member.is_dir():
                continue
            destination = target / member.filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(embedded.read(member))
            count += 1
    log_line(f"embed python extracted files={count} target={target}")


def assemble_payload(launcher: Path, python_archive: Path, zstd_archive: Path, assets: dict[str, Path]) -> None:
    if PAYLOAD_DIR.exists():
        shutil.rmtree(PAYLOAD_DIR)
    app_target = PAYLOAD_DIR / "runtime" / "app"
    ui_target = PAYLOAD_DIR / "runtime" / "ui"
    app_target.mkdir(parents=True)
    ui_target.mkdir(parents=True)

    shutil.copy2(launcher, PAYLOAD_DIR / LAUNCHER_EXE)
    for name in APP_FILES:
        source = PROJECT_ROOT / "app" / name
        if not source.is_file():
            raise SystemExit(f"missing app file: {source}")
        shutil.copy2(source, app_target / name)
    index = PROJECT_ROOT / "ui" / "index.html"
    if not index.is_file():
        raise SystemExit(f"missing ui file: {index}")
    shutil.copy2(index, ui_target / "index.html")
    logo = assets.get("ui_logo")
    if logo and logo.is_file():
        shutil.copy2(logo, ui_target / "logo.png")
    if not README_TEMPLATE.is_file():
        raise SystemExit(f"missing readme template: {README_TEMPLATE}")
    (PAYLOAD_DIR / README_NAME).write_bytes(utf8_bom_crlf(README_TEMPLATE.read_bytes(), README_TEMPLATE.name))

    with zipfile.ZipFile(zstd_archive) as zstd:
        (app_target / "zstd.exe").write_bytes(zstd.read(ZSTD_MEMBER))
    extract_python_runtime(python_archive, PAYLOAD_DIR / "runtime" / "python")
    log_line(f"payload assembled root={PAYLOAD_DIR}")


def is_pe_file(data: bytes) -> bool:
    if len(data) < 0x40 or data[:2] != b"MZ":
        return False
    offset = int.from_bytes(data[0x3C:0x40], "little")
    if offset + 4 > len(data):
        return False
    return data[offset : offset + 4] == b"PE\x00\x00"


def pe_subsystem(data: bytes) -> int:
    """Return the PE subsystem id: 2 is the GUI, 3 would open a console."""
    if not is_pe_file(data):
        raise SystemExit("not a Windows executable; cannot read the subsystem")
    offset = int.from_bytes(data[0x3C:0x40], "little")
    return int.from_bytes(data[offset + 4 + 20 + 68 : offset + 4 + 20 + 70], "little")


def verify_payload_dir(path: Path) -> None:
    missing = [name for name in PAYLOAD_REQUIRED if not (path / name).is_file()]
    if missing:
        raise SystemExit("payload self check failed, missing: " + ", ".join(missing))
    check_embedded_stdlib((path / "runtime/python/python312.zip").read_bytes(), "python312.zip")
    for name in (LAUNCHER_EXE, "runtime/python/python.exe", "runtime/python/pythonw.exe", "runtime/app/zstd.exe"):
        with (path / name).open("rb") as handle:
            if not is_pe_file(handle.read(0x400)):
                raise SystemExit(f"payload entry is not a Windows executable: {name}")
    log_line(f"payload self check ok files={sum(item.is_file() for item in path.rglob('*'))}")


def compile_nsis(output: Path, makensis: str, helper: Path) -> None:
    if not NSIS_SCRIPT.is_file():
        raise SystemExit(f"missing NSIS script: {NSIS_SCRIPT}")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    option = "/" if os.name == "nt" else "-"
    command = [
        makensis,
        option + "WX",
        option + "INPUTCHARSET", "UTF8",
        option + f"DAPP_VERSION={SETUP_VERSION}",
        option + f"DPAYLOAD_DIR={PAYLOAD_DIR.resolve()}",
        option + f"DOUTPUT_FILE={output}",
        option + f"DAPP_ICON={APP_ICON.resolve()}",
        option + f"DHELPER_EXE={helper.resolve()}",
        str(NSIS_SCRIPT),
    ]
    log_line(f"nsis compile version={SETUP_VERSION} payload={PAYLOAD_DIR} output={output}")
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def nsis_header_offset(data: bytes) -> int:
    """Locate the start of the NSIS overlay after all PE sections."""
    if not is_pe_file(data):
        raise SystemExit("setup is not a Windows executable")
    pe = int.from_bytes(data[0x3C:0x40], "little")
    if pe + 24 > len(data):
        raise SystemExit("setup has a truncated PE header")
    section_count = int.from_bytes(data[pe + 6 : pe + 8], "little")
    optional_size = int.from_bytes(data[pe + 20 : pe + 22], "little")
    if optional_size < 60:
        raise SystemExit("setup has a truncated PE optional header")
    optional = pe + 24
    alignment = int.from_bytes(data[optional + 32 : optional + 36], "little")
    image_size = int.from_bytes(data[optional + 56 : optional + 60], "little")
    if alignment == 0 or image_size == 0:
        raise SystemExit("setup has invalid PE image dimensions")
    first = pe + 24 + optional_size
    if section_count == 0 or first + section_count * 40 > len(data):
        raise SystemExit("setup has a truncated PE section table")
    ndata_range = None
    overlay = 0
    for index in range(section_count):
        section = first + index * 40
        name = data[section : section + 8].rstrip(b"\x00")
        virtual_size = int.from_bytes(data[section + 8 : section + 12], "little")
        virtual_address = int.from_bytes(data[section + 12 : section + 16], "little")
        size = int.from_bytes(data[section + 16 : section + 20], "little")
        pointer = int.from_bytes(data[section + 20 : section + 24], "little")
        overlay = max(overlay, pointer + size)
        if name == b".ndata":
            ndata_range = (virtual_address, virtual_size)
    if ndata_range is None:
        raise SystemExit("setup has no NSIS .ndata section")
    start, span = ndata_range
    end = ((start + span + alignment - 1) // alignment) * alignment
    if start == 0 or span == 0 or end > image_size:
        raise SystemExit("setup PE SizeOfImage does not cover the NSIS .ndata section")
    return overlay


def verify_setup(path: Path) -> None:
    size = path.stat().st_size
    if size < MIN_SETUP_BYTES:
        raise SystemExit(f"setup looks too small ({size} bytes); the payload is probably missing")
    with path.open("rb") as handle:
        header = handle.read(0x1000)
        if pe_subsystem(header) != 2:
            raise SystemExit("setup would open a console window")
        overlay = nsis_header_offset(header)
        handle.seek(overlay)
        first_header = handle.read(28)
        if len(first_header) != 28 or first_header[4:20] != NSIS_SIGNATURE:
            raise SystemExit("setup has no NSIS payload header")
        if int.from_bytes(first_header[:4], "little") & 4:
            raise SystemExit("setup NSIS CRC checking is disabled")
        payload_size = int.from_bytes(first_header[24:28], "little")
        if payload_size != size - overlay or payload_size < 32:
            raise SystemExit("setup NSIS payload length does not match the file")
        handle.seek(NSIS_CRC_START)
        remaining = size - NSIS_CRC_START - 4
        checksum = 0
        while remaining:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                raise SystemExit("setup ended before the NSIS CRC")
            checksum = zlib.crc32(chunk, checksum)
            remaining -= len(chunk)
        expected = int.from_bytes(handle.read(4), "little")
        if checksum != expected:
            raise SystemExit(f"setup NSIS CRC mismatch: expected={expected:08x} actual={checksum:08x}")
    log_line(f"NSIS setup self check ok bytes={size} overlay={overlay} crc={checksum:08x} path={path}")


def smoke_run_admin(payload_dir: Path) -> None:
    """Run the packaged admin.py with the build machine Python.

    This proves the shipped file layout and imports work before the installer
    ever reaches a Windows machine. It cannot replace a real Windows run.
    """
    app_dir = payload_dir / "runtime" / "app"
    home = tempfile.mkdtemp(prefix="codexmk-smoke-")
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
    command = [sys.executable, str(app_dir / "admin.py"), "--package", str(app_dir), "--home", home, "--print-url"]
    try:
        result = subprocess.run(command, cwd=app_dir, env=env, capture_output=True, text=True, timeout=180)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if result.returncode != 0 or "http://127.0.0.1:" not in result.stdout:
        raise SystemExit(
            "payload smoke check failed\n"
            f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    log_line("payload smoke check ok (admin.py answered %s)" % result.stdout.strip())


def build(output: Path, cache: Path) -> Path:
    require_go()
    makensis = find_makensis()
    python_archive = fetch(PYTHON_URL, PYTHON_ARCHIVE, PYTHON_SHA256, cache)
    zstd_archive = fetch(ZSTD_URL, ZSTD_ARCHIVE, ZSTD_SHA256, cache)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    assets = icon_assets()

    launcher = WORK_DIR / LAUNCHER_EXE
    helper = WORK_DIR / HELPER_EXE
    staged = [
        path
        for path in (
            embed_icon(PROJECT_ROOT / "launcher", APP_ICON, LAUNCHER_EXE),
        )
        if path
    ]
    try:
        go_build(PROJECT_ROOT / "launcher", launcher)
        go_build(PROJECT_ROOT / "installer" / "installctl", helper)
        with helper.open("rb") as handle:
            if pe_subsystem(handle.read(0x400)) != 2:
                raise SystemExit("install helper would open a console window")
        verify_exe_icon(launcher)
        assemble_payload(launcher, python_archive, zstd_archive, assets)
        verify_payload_dir(PAYLOAD_DIR)
        smoke_run_admin(PAYLOAD_DIR)
        compile_nsis(output, makensis, helper)
    finally:
        drop_artifacts(staged)

    verify_setup(output)
    verify_exe_icon(output)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Windows installer for Codex multi key router")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--cache", type=Path, default=CACHE_DIR)
    args = parser.parse_args(argv)
    output = build(args.output, args.cache)
    print(f"windows installer: {output}")
    print(f"size: {output.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
