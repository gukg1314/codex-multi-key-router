#!/usr/bin/env python3
"""Packaging helpers shared by the macOS and Windows builders.

Only the standard library is used. Everything that ends up inside a Windows
package is written as ASCII/UTF-8 on purpose: the previous generation of this
tool failed to start when cmd.exe or PowerShell met a mismatched code page.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import ssl
import sys
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = PROJECT_ROOT / "dist-output"
CACHE_DIR = DIST_DIR / ".cache"

PYTHON_VERSION = "3.12.10"
PYTHON_ARCHIVE = f"python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{PYTHON_ARCHIVE}"
PYTHON_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
ZSTD_ARCHIVE = "zstd-v1.5.7-win64.zip"
ZSTD_URL = f"https://github.com/facebook/zstd/releases/download/v1.5.7/{ZSTD_ARCHIVE}"
ZSTD_SHA256 = "acb4e8111511749dc7a3ebedca9b04190e37a17afeb73f55d4425dbf0b90fad9"
ZSTD_MEMBER = "zstd-v1.5.7-win64/zstd.exe"

# 3.11 起 embed 包的标准库在 python312.zip 内是 .pyc；抽查工具运行必需的模块，
# 防止上游归档换源或裁剪后打出「能解压但启动即失败」的 Windows 包。
EMBEDDED_STDLIB_REQUIRED = (
    "json/__init__.pyc",
    "urllib/request.pyc",
    "http/client.pyc",
    "subprocess.pyc",
    "getpass.pyc",
    "encodings/utf_8.pyc",
    "logging/__init__.pyc",
    "argparse.pyc",
    "hashlib.pyc",
    "socket.pyc",
    "ssl.pyc",
    "pathlib.pyc",
    "shutil.pyc",
    "tempfile.pyc",
    "base64.pyc",
    "re/__init__.pyc",
)


def log_line(message: str) -> None:
    print(message, file=sys.stderr)


def icon_assets() -> dict[str, Path]:
    """Derive every shipped icon from icon.png.

    A missing source image (or a broken generator) degrades to the system
    default icons; it must never stop a package from being built.
    """
    try:
        import icons
    except Exception as exc:  # pragma: no cover - only a broken checkout gets here
        log_line(f"icon module unavailable: {exc}")
        return {}
    try:
        return icons.ensure_assets()
    except Exception as exc:
        log_line(f"icon generation skipped: {exc}")
        return {}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ssl_context() -> ssl.SSLContext:
    """Fall back to the system trust store when the interpreter has none.

    python.org builds on macOS ship without a certificate bundle until
    "Install Certificates.command" has been run, which would otherwise stop any
    packaging run with CERTIFICATE_VERIFY_FAILED.
    """
    candidates = (
        os.environ.get("SSL_CERT_FILE"),
        "/etc/ssl/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            try:
                return ssl.create_default_context(cafile=candidate)
            except ssl.SSLError:
                continue
    return ssl.create_default_context()


def fetch(url: str, archive_name: str, expected_sha256: str, cache_dir: Path) -> Path:
    """从缓存或官网取归档文件，任何一次都要通过 SHA256 校验。"""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / archive_name
    if target.is_file() and sha256_of(target) == expected_sha256:
        log_line(f"cache hit {archive_name}")
        return target
    log_line(f"download {url}")
    context = _ssl_context()
    with urllib.request.urlopen(url, timeout=300, context=context) as response, target.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    actual = sha256_of(target)
    if actual != expected_sha256:
        target.unlink(missing_ok=True)
        raise SystemExit(f"SHA256 校验失败：{archive_name}\n期望 {expected_sha256}\n实际 {actual}")
    return target


def _to_crlf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def ascii_crlf(data: bytes, name: str) -> bytes:
    """Windows 批处理必须纯 ASCII + CRLF，避免 cmd 代码页不一致导致无法启动。"""
    try:
        data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"打包中止：{name} 含非 ASCII 字节（偏移 {exc.start}），cmd 可能无法解析") from exc
    return _to_crlf(data)


def utf8_bom_crlf(data: bytes, name: str) -> bytes:
    """说明文档用 UTF-8 BOM + CRLF，兼容所有 Windows 记事本。"""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"打包中止：{name} 不是合法 UTF-8（偏移 {exc.start}）") from exc
    return b"\xef\xbb\xbf" + _to_crlf(data.removeprefix(b"\xef\xbb\xbf"))


def check_embedded_stdlib(data: bytes, name: str) -> None:
    """内嵌 Python 的标准库归档必须包含运行所需模块，否则 Windows 端无法启动。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as embedded:
            members = set(embedded.namelist())
    except zipfile.BadZipFile as exc:
        raise SystemExit(f"打包自检失败：{name} 不是合法的 zip 归档") from exc
    missing = [member for member in EMBEDDED_STDLIB_REQUIRED if member not in members]
    if missing:
        raise SystemExit(f"打包自检失败：{name} 缺少标准库模块 {', '.join(missing)}")
    log_line(f"embedded stdlib ok archive={name} members={len(members)}")


def read_ascii_crlf(path: Path) -> bytes:
    """打包脚本模板的通用读法：拒绝非 ASCII，统一 CRLF。"""
    path = Path(path)
    return ascii_crlf(path.read_bytes(), path.name)
