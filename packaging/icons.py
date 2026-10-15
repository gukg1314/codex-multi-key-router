#!/usr/bin/env python3
"""Export shipped icons from the approved, frozen PNG masters.

``packaging/assets/app-icon.png`` is the shared 1024 px main icon for Windows
application/tray icons and the UI logo. The macOS iconset resizes the frozen
``app-icon-macos.png`` variant with the system-compatible alpha profile.
The independent 18/36 px white statusbar-source PNGs only change RGB to black,
keeping every alpha value for AppKit template rendering. No export redraws,
crops, adds a plate or derives the status bar mark from the main icon.

Only the standard library is used so both platform builds share one exporter.
"""

from __future__ import annotations

import argparse
import shutil
import struct
import sys
import zlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGING_DIR = PROJECT_ROOT / "packaging"
ASSETS_DIR = PACKAGING_DIR / "assets"
LAUNCHER_ASSETS = PROJECT_ROOT / "launcher" / "assets"
UI_DIR = PROJECT_ROOT / "ui"

SOURCE_IMAGE = ASSETS_DIR / "app-icon.png"
MACOS_SOURCE_IMAGE = ASSETS_DIR / "app-icon-macos.png"
STATUS_SOURCE_PNG = ASSETS_DIR / "statusbar-source.png"
STATUS_SOURCE_PNG_2X = ASSETS_DIR / "statusbar-source@2x.png"
APP_ICO = ASSETS_DIR / "app.ico"
TRAY_ICO = LAUNCHER_ASSETS / "tray.ico"
STATUS_PNG = LAUNCHER_ASSETS / "statusbar.png"
STATUS_PNG_2X = LAUNCHER_ASSETS / "statusbar@2x.png"
UI_LOGO = UI_DIR / "logo.png"

# The macOS iconset covers 16-1024 so Dock, Finder and the app switcher all
# pick a native size instead of scaling a single bitmap. The names are exactly
# the set iconutil accepts; anything else makes iconutil reject the folder.
ICONSET_PLAN = (
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
)
APP_ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
TRAY_ICO_SIZES = (16, 32, 48)
STATUS_PX = 18
LOGO_PX = 60
APP_ICON_CANVAS = 1024


def log_line(message: str) -> None:
    print(message, file=sys.stderr)


class Image:
    """Non premultiplied RGBA8 pixels, top row first."""

    __slots__ = ("width", "height", "pixels")

    def __init__(self, width: int, height: int, pixels: bytes | bytearray) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray(pixels)


# ------------------------------------------------------------------ png read

def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> bytearray:
    stride = width * bpp
    out = bytearray(stride * height)
    pos = 0
    previous = bytearray(stride)
    for row in range(height):
        kind = raw[pos]
        pos += 1
        line = bytearray(raw[pos : pos + stride])
        pos += stride
        if kind == 1:
            for index in range(bpp, stride):
                line[index] = (line[index] + line[index - bpp]) & 0xFF
        elif kind == 2:
            for index in range(stride):
                line[index] = (line[index] + previous[index]) & 0xFF
        elif kind == 3:
            for index in range(stride):
                left = line[index - bpp] if index >= bpp else 0
                line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif kind == 4:
            for index in range(stride):
                left = line[index - bpp] if index >= bpp else 0
                up = previous[index]
                up_left = previous[index - bpp] if index >= bpp else 0
                estimate = left + up - up_left
                da = abs(estimate - left)
                db = abs(estimate - up)
                dc = abs(estimate - up_left)
                if da <= db and da <= dc:
                    predict = left
                elif db <= dc:
                    predict = up
                else:
                    predict = up_left
                line[index] = (line[index] + predict) & 0xFF
        elif kind != 0:
            raise SystemExit(f"unsupported png filter type {kind}")
        out[row * stride : (row + 1) * stride] = line
        previous = line
    return out


def read_png(path: Path) -> Image:
    """Decode an 8 bit, non interlaced PNG (RGB, RGBA, grey or grey+alpha)."""
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"not a png file: {Path(path).name}")
    pos = 8
    header = None
    chunks: list[bytes] = []
    palette = b""
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"IDAT":
            chunks.append(body)
        elif kind == b"IEND":
            break
    if header is None:
        raise SystemExit(f"png has no IHDR: {Path(path).name}")
    width, height, depth, color, _, _, interlace = header
    if depth != 8 or interlace != 0 or color not in (0, 2, 4, 6, 3):
        raise SystemExit(
            f"{Path(path).name}: need an 8 bit non interlaced png, got depth={depth} color={color} interlace={interlace}"
        )
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    raw = zlib.decompress(b"".join(chunks))
    flat = _unfilter(raw, width, height, channels)
    rgba = bytearray(width * height * 4)
    for index in range(width * height):
        source = index * channels
        target = index * 4
        if color == 0:
            value = flat[source]
            rgba[target : target + 4] = bytes((value, value, value, 255))
        elif color == 2:
            rgba[target : target + 4] = bytes(flat[source : source + 3]) + b"\xff"
        elif color == 3:
            offset = flat[source] * 3
            rgba[target : target + 4] = bytes(palette[offset : offset + 3]) + b"\xff"
        elif color == 4:
            value = flat[source]
            rgba[target : target + 4] = bytes((value, value, value, flat[source + 1]))
        else:
            rgba[target : target + 4] = flat[source : source + 4]
    return Image(width, height, rgba)


# ----------------------------------------------------------------- png write

def write_png(path: Path, image: Image) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_png(image))


def encode_png(image: Image) -> bytes:
    width, height = image.width, image.height
    stride = width * 4
    raw = bytearray()
    for row in range(height):
        raw.append(0)
        raw += image.pixels[row * stride : (row + 1) * stride]
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
            _chunk(b"sRGB", b"\x00"),
            _chunk(b"IDAT", zlib.compress(bytes(raw), 9)),
            _chunk(b"IEND", b""),
        )
    )


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


# --------------------------------------------------------------- frozen masters

def _read_master(path: Path, size: int) -> Image:
    image = read_png(path)
    if (image.width, image.height) != (size, size):
        raise SystemExit(f"图标母版尺寸不符 source={path.name} expected={size}x{size} actual={image.width}x{image.height}")
    log_line(f"图标母版已读取 source={path.name} px={size}x{size}")
    return image


def app_icon_image(app_path: Path) -> Image:
    """Read the approved main icon without changing its artwork or alpha."""
    return _read_master(app_path, APP_ICON_CANVAS)


def statusbar_template(path: Path, size: int) -> Image:
    """Convert approved white artwork to black without changing its alpha."""
    image = _read_master(path, size)
    for index in range(0, len(image.pixels), 4):
        if image.pixels[index + 3] and image.pixels[index : index + 3] != b"\xff\xff\xff":
            raise SystemExit(f"状态栏母版必须为纯白线条 source={path.name}")
        image.pixels[index : index + 3] = b"\x00\x00\x00"
    return image


# -------------------------------------------------------------------- resize

def _premultiply(image: Image) -> bytearray:
    pixels = bytearray(image.pixels)
    for index in range(0, len(pixels), 4):
        alpha = pixels[index + 3]
        if alpha == 255:
            continue
        pixels[index] = pixels[index] * alpha // 255
        pixels[index + 1] = pixels[index + 1] * alpha // 255
        pixels[index + 2] = pixels[index + 2] * alpha // 255
    return pixels


def _unpremultiply(pixels: bytearray) -> bytearray:
    for index in range(0, len(pixels), 4):
        alpha = pixels[index + 3]
        if alpha in (0, 255):
            if alpha == 0:
                pixels[index : index + 3] = b"\x00\x00\x00"
            continue
        pixels[index] = min(255, pixels[index] * 255 // alpha)
        pixels[index + 1] = min(255, pixels[index + 1] * 255 // alpha)
        pixels[index + 2] = min(255, pixels[index + 2] * 255 // alpha)
    return pixels


def _box(src: bytearray, src_size: int, target: int) -> bytearray:
    """Area average a square premultiplied buffer down to ``target`` pixels."""
    out = bytearray(target * target * 4)
    scale = src_size / target
    for y in range(target):
        y0 = int(y * scale)
        y1 = max(y0 + 1, int((y + 1) * scale))
        for x in range(target):
            x0 = int(x * scale)
            x1 = max(x0 + 1, int((x + 1) * scale))
            red = green = blue = alpha = 0
            for sy in range(y0, y1):
                base = (sy * src_size + x0) * 4
                for sx in range(x1 - x0):
                    offset = base + sx * 4
                    red += src[offset]
                    green += src[offset + 1]
                    blue += src[offset + 2]
                    alpha += src[offset + 3]
            count = (y1 - y0) * (x1 - x0)
            target_offset = (y * target + x) * 4
            out[target_offset] = red // count
            out[target_offset + 1] = green // count
            out[target_offset + 2] = blue // count
            out[target_offset + 3] = alpha // count
    return out


class Resizer:
    """Halving pyramid so every size is built from a close smaller buffer."""

    def __init__(self, image: Image) -> None:
        self.source = image
        self.premultiplied = _premultiply(image)
        self.levels: list[tuple[int, bytearray]] = [(image.width, self.premultiplied)]

    def _level_for(self, size: int) -> tuple[int, bytearray]:
        while self.levels[-1][0] > size * 2:
            current_size, current = self.levels[-1]
            smaller = (current_size + 1) // 2
            self.levels.append((smaller, _box(current, current_size, smaller)))
        best = self.levels[0]
        for level in self.levels:
            if level[0] >= size:
                best = level
            else:
                break
        return best

    def resize(self, size: int) -> Image:
        if size == self.source.width:
            return Image(size, size, self.source.pixels)
        level_size, level = self._level_for(size)
        pixels = level if level_size == size else _box(level, level_size, size)
        return Image(size, size, _unpremultiply(bytearray(pixels)))


# ----------------------------------------------------------------------- ico

def _dib(image: Image) -> bytes:
    width = height = image.width
    mask_stride = ((width + 31) // 32) * 4
    header = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        height * 2,
        1,
        32,
        0,
        width * height * 4 + mask_stride * height,
        0,
        0,
        0,
        0,
    )
    xor = bytearray()
    mask = bytearray(mask_stride * height)
    for y in range(height):
        row = (height - 1 - y) * width * 4
        mask_row = y * mask_stride
        for x in range(width):
            offset = row + x * 4
            xor += bytes(
                (image.pixels[offset + 2], image.pixels[offset + 1], image.pixels[offset], image.pixels[offset + 3])
            )
            if image.pixels[offset + 3] == 0:
                mask[mask_row + (x >> 3)] |= 0x80 >> (x & 7)
    return header + bytes(xor) + bytes(mask)


def write_ico(path: Path, images: list[Image]) -> None:
    payloads = [_dib(image) for image in images]
    offset = 6 + 16 * len(payloads)
    header = struct.pack("<HHH", 0, 1, len(payloads))
    directory = bytearray()
    body = bytearray()
    for image, payload in zip(images, payloads):
        size = image.width
        directory += struct.pack("<BBBBHHII", size if size < 256 else 0, size if size < 256 else 0, 0, 0, 1, 32, len(payload), offset)
        body += payload
        offset += len(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + bytes(directory) + bytes(body))


# --------------------------------------------------------------------- build

def _is_fresh(target: Path, source: Path) -> bool:
    """Regenerate when either the frozen master or export code changes."""
    if not target.is_file():
        return False
    newest = max(source.stat().st_mtime, Path(__file__).stat().st_mtime)
    return target.stat().st_mtime >= newest


def ensure_assets(force: bool = False) -> dict[str, Path]:
    """Export from approved masters; missing or invalid masters stop the build."""
    missing = [path.name for path in (SOURCE_IMAGE, STATUS_SOURCE_PNG, STATUS_SOURCE_PNG_2X) if not path.is_file()]
    if missing:
        raise SystemExit(f"图标母版缺失，停止导出 sources={','.join(missing)}")
    main = app_icon_image(SOURCE_IMAGE)
    status_sources = (
        (STATUS_PNG, STATUS_SOURCE_PNG, STATUS_PX),
        (STATUS_PNG_2X, STATUS_SOURCE_PNG_2X, STATUS_PX * 2),
    )
    # 先验证两个密度的母版，再写入任何生产资源。
    status_images = [statusbar_template(source, size) for _, source, size in status_sources]
    if force or any(not _is_fresh(target, SOURCE_IMAGE) for target in (APP_ICO, TRAY_ICO, UI_LOGO)):
        resizer = Resizer(main)
        write_ico(APP_ICO, [resizer.resize(size) for size in APP_ICO_SIZES])
        log_line(f"应用图标已导出 source={SOURCE_IMAGE.name} sizes={','.join(str(size) for size in APP_ICO_SIZES)} target={APP_ICO.name}")
        write_ico(TRAY_ICO, [resizer.resize(size) for size in TRAY_ICO_SIZES])
        log_line(f"托盘图标已导出 source={SOURCE_IMAGE.name} sizes={','.join(str(size) for size in TRAY_ICO_SIZES)} target={TRAY_ICO.name}")
        write_png(UI_LOGO, resizer.resize(LOGO_PX))
        log_line(f"界面图标已导出 source={SOURCE_IMAGE.name} px={LOGO_PX}x{LOGO_PX} target={UI_LOGO.name}")
    for (target, source, size), image in zip(status_sources, status_images):
        if force or not _is_fresh(target, source):
            write_png(target, image)
            log_line(f"状态栏模板已导出 source={source.name} px={size}x{size} target={target.name} alpha=原样保留")
    return {
        "source": SOURCE_IMAGE,
        "macos_source": MACOS_SOURCE_IMAGE,
        "app_ico": APP_ICO,
        "tray_ico": TRAY_ICO,
        "status_png": STATUS_PNG,
        "status_png_2x": STATUS_PNG_2X,
        "ui_logo": UI_LOGO,
    }


def write_iconset(resizer: Resizer, target_dir: Path, force: bool = False, source: Path | None = None) -> list[Path]:
    """Write the 16-1024 iconset iconutil turns into AppIcon.icns."""
    reference = source or SOURCE_IMAGE
    written: list[Path] = []
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, size in ICONSET_PLAN:
        path = target_dir / name
        if not force and reference.is_file() and _is_fresh(path, reference):
            written.append(path)
            continue
        write_png(path, resizer.resize(size))
        written.append(path)
    log_line(f"macOS图标组已导出 source={reference.name} files={len(written)} sizes={','.join(str(size) for _, size in ICONSET_PLAN)} target={target_dir.name}")
    return written


def build_icns(iconset_dir: Path, target: Path) -> Path:
    """Use Apple's iconutil when it is available."""
    iconutil = shutil.which("iconutil")
    if iconutil is None:
        raise SystemExit("iconutil not found; the macOS icon must be built on macOS")
    import subprocess

    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    subprocess.run([iconutil, "-c", "icns", str(iconset_dir), "-o", str(target)], check=True)
    log_line(f"macOS图标已编码 source={iconset_dir.name} bytes={target.stat().st_size} target={target.name}")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export app, tray and UI icons from the approved PNG masters")
    parser.add_argument("--force", action="store_true", help="rebuild even when the outputs look fresh")
    args = parser.parse_args(argv)
    produced = ensure_assets(force=args.force)
    for name, path in sorted(produced.items()):
        print(f"{name}: {path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
