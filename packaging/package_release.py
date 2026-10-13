#!/usr/bin/env python3
"""Build and publish the next macOS and Windows release together."""

from __future__ import annotations

from contextlib import contextmanager
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION_FIELDS = (
    ("app/admin.py", r'(?m)^(APP_VERSION\s*=\s*")(\d+\.\d+\.\d+)(")'),
    ("launcher/main.go", r'(?m)^(\s*appVersion\s*=\s*")(\d+\.\d+\.\d+)(")'),
    ("installer/main_windows.go", r'(?m)^(\s*setupVersion\s*=\s*")(\d+\.\d+\.\d+)(")'),
    ("packaging/build_macos.py", r'(?m)^(APP_VERSION\s*=\s*")(\d+\.\d+\.\d+)(")'),
    ("packaging/build_windows.py", r'(?m)^(SETUP_VERSION\s*=\s*")(\d+\.\d+\.\d+)(")'),
    ("packaging/templates/windows-readme.txt", r'(?m)^(Codex 路由 v)(\d+\.\d+\.\d+)( 使用说明)'),
)


def log(message: str) -> None:
    print(f"[打包] {message}", file=sys.stderr, flush=True)


def next_patch_version(version: str) -> str:
    parts = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if parts is None:
        raise ValueError(f"版本号不是三段数字：{version}")
    return f"{parts[1]}.{parts[2]}.{int(parts[3]) + 1}"


@contextmanager
def release_lock(root: Path):
    lock_path = root / "dist-output" / ".package-release.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(f"已有一键打包进程，无法获取锁：{lock_path}") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError(f"已有一键打包进程，无法获取锁：{lock_path}") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def version_sources(root: Path) -> tuple[str, dict[Path, bytes], dict[Path, bytes]]:
    originals: dict[Path, bytes] = {}
    replacements: dict[Path, bytes] = {}
    versions: set[str] = set()
    for relative, expression in VERSION_FIELDS:
        path = root / relative
        original = path.read_bytes()
        source = original.decode("utf-8")
        pattern = re.compile(expression)
        matches = list(pattern.finditer(source))
        if len(matches) != 1:
            raise ValueError(f"版本字段数量不对：{relative}")
        versions.add(matches[0][2])
        originals[path] = original
    if len(versions) != 1:
        raise ValueError("Mac、Windows 和界面版本号不一致")
    version = versions.pop()
    release_root = root / "release"
    candidates = [version]
    if release_root.is_dir():
        candidates.extend(
            path.name
            for path in release_root.iterdir()
            if path.is_dir() and re.fullmatch(r"\d+\.\d+\.\d+", path.name)
        )
    latest = max(candidates, key=lambda value: tuple(map(int, value.split("."))))
    next_version = next_patch_version(latest)
    for relative, expression in VERSION_FIELDS:
        path = root / relative
        source = originals[path].decode("utf-8")
        updated = re.sub(
            expression,
            lambda match: f"{match[1]}{next_version}{match[3]}",
            source,
            count=1,
        )
        replacements[path] = updated.encode("utf-8")
    return next_version, originals, replacements


def artifact_names(version: str) -> tuple[str, str]:
    return (
        f"CodexRouter-{version}-mac-arm64.dmg",
        f"CodexRouter-{version}-win-x64.exe",
    )


def require_build_host() -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise RuntimeError("一键生成 Mac DMG 和 Windows EXE 须在 Apple Silicon Mac 上运行")


def package_release(
    root: Path = PROJECT_ROOT,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    check_host: bool = True,
) -> tuple[Path, Path]:
    root = Path(root)
    if check_host:
        require_build_host()
    with release_lock(root):
        return _package_release_locked(root, runner)


def _package_release_locked(root: Path, runner: Callable[..., subprocess.CompletedProcess]) -> tuple[Path, Path]:
    version, originals, replacements = version_sources(root)
    release_root = root / "release"
    destination = release_root / version
    if destination.exists():
        raise FileExistsError(f"版本目录已存在，拒绝覆盖：{destination}")
    release_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=release_root))
    names = artifact_names(version)
    outputs = (staging / names[0], staging / names[1])
    changed = False
    published = False
    try:
        log(f"准备 {version}，同步 Mac、Windows 和界面版本号")
        changed = True
        for path, data in replacements.items():
            path.write_bytes(data)
        for script, output in zip(("build_macos.py", "build_windows.py"), outputs):
            log(f"构建 {output.name}")
            runner(
                [sys.executable, str(root / "packaging" / script), "--output", str(output)],
                cwd=root,
                check=True,
            )
            if not output.is_file() or output.stat().st_size == 0:
                raise RuntimeError(f"安装包缺失或为空：{output.name}")
            log(f"校验通过 {output.name} bytes={output.stat().st_size}")
        drifted = [str(path.relative_to(root)) for path, data in replacements.items() if path.read_bytes() != data]
        if drifted:
            raise RuntimeError("版本源在打包过程中被修改：" + ", ".join(drifted))
        destination.mkdir()
        published = True
        for output in outputs:
            output.replace(destination / output.name)
        log(f"发布完成：{destination}")
        return tuple(destination / name for name in names)
    except BaseException as error:
        log(f"失败，恢复原版本并清理新产物：{error}")
        if changed:
            for path, data in originals.items():
                path.write_bytes(data)
        if published:
            shutil.rmtree(destination)
        raise
    finally:
        shutil.rmtree(staging)


if __name__ == "__main__":
    try:
        package_release()
    except Exception as error:
        log(str(error))
        raise SystemExit(1) from error
