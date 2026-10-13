#!/usr/bin/env python3
"""Derive every shipped icon from the repository images.

Two sources feed the shipped marks:

* ``icondock.png`` (falling back to ``icon圆角.png``, then ``icon.png``) backs
  the Dock, Finder, Windows exe and tray icons through
  ``packaging/assets/app-icon.png``. It carries the white rounded plate macOS
  icons need; its white margin is punched to transparency on the way in.
* ``icon.png`` keeps the transparent artwork for the 18 px menu bar silhouette
  and the 60 px UI logo, so neither shows a white block.

Everything else is generated on the build machine:

* macOS: a full iconset (16-1024) that ``iconutil`` turns into AppIcon.icns,
  plus 18/36 px menu bar silhouettes,
* Windows: a 16/32/48/64/128/256 ICO for the exe files, and a 16/32/48 ICO
  the launcher embeds for the tray,
* UI: a 60x60 logo.png for the sidebar and the favicon.

Only the standard library is used: the Windows package is cross built on macOS
and must never depend on Pillow or any other extra tool. A missing source image
is never fatal, the callers fall back to the system default icon.
"""

from __future__ import annotations

import argparse
import math
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

REPO_IMAGE = PROJECT_ROOT / "icon.png"
REPO_DOCK_IMAGE = PROJECT_ROOT / "icondock.png"
REPO_ROUNDED_IMAGE = PROJECT_ROOT / "icon圆角.png"
SOURCE_IMAGE = ASSETS_DIR / "app-icon.png"
MARK_IMAGE = ASSETS_DIR / "app-mark.png"
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
APP_ICO_SIZES = (16, 32, 48, 64, 128, 256)
TRAY_ICO_SIZES = (16, 32, 48)
STATUS_PX = 18
LOGO_PX = 60
# Big Sur style rounded rectangle: the corner radius macOS icons are drawn
# with, as a fraction of the canvas. ``icon圆角.png`` ships an opaque plate, so
# the four corners have to be punched out before the Dock shows a rounded icon.
ROUNDED_CORNER_RATIO = 0.2237
# The composed app icon: a 1024 px canvas whose white plate bleeds to the
# edges, with the artwork centred at 86% so nothing touches the corners.
APP_ICON_CANVAS = 1024
APP_ICON_CONTENT = 0.86

# Apple draws every macOS app icon inside a 824 px rounded shape on a 1024 px
# canvas, leaving 100 px of transparent margin on each side. Artwork that fills
# its canvas is rendered oversized next to the system icons in the Dock, so the
# finished card is always scaled to this share before it is packed.
MACOS_PLATE_RATIO = 824 / 1024

# ``icondock.png`` is finished artwork: a light rounded card drawn inside a
# white square. The card starts this far from the canvas edge and uses this
# corner radius, so punching that shape to transparency keeps the artwork and
# drops the white margin that would otherwise show up as a square in the Dock.
DOCK_PLATE_INSET_RATIO = 0.024
DOCK_PLATE_CORNER_RATIO = 0.245


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
        raise SystemExit(f"not a png file: {path}")
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
        raise SystemExit(f"png has no IHDR: {path}")
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
            _chunk(b"IDAT", zlib.compress(bytes(raw), 9)),
            _chunk(b"IEND", b""),
        )
    )


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


# ---------------------------------------------------------------- app plate

def mark_box(image: Image, threshold: int = 8) -> tuple[int, int, int, int] | None:
    """Bounding box of the visible pixels of the transparent artwork."""
    stride = image.width * 4
    left, top = image.width, image.height
    right = bottom = -1
    for y in range(image.height):
        alpha = image.pixels[y * stride + 3 : (y + 1) * stride : 4]
        if max(alpha) < threshold:
            continue
        first = next(index for index, value in enumerate(alpha) if value >= threshold)
        last = next(index for index in range(len(alpha) - 1, -1, -1) if alpha[index] >= threshold)
        left = min(left, first)
        right = max(right, last)
        top = min(top, y)
        bottom = max(bottom, y)
    if right < 0:
        return None
    return (left, top, right + 1, bottom + 1)


def plate_coverage(image: Image) -> float:
    """Share of the canvas that is already opaque, i.e. how finished it is."""
    alpha = image.pixels[3::4]
    return sum(1 for value in alpha if value >= 250) / len(alpha)


def has_punched_corners(image: Image) -> bool:
    """True when the artwork already carries the macOS rounded outline.

    Four transparent corners plus opaque points just inside each edge is
    exactly what ``dock_plate`` leaves behind and what a finished dock icon
    ships. A plain opaque square keeps its corners and fails the test, and a
    transparent mark that never reaches the edges fails it too, so neither is
    mistaken for finished artwork. The coverage ratio alone cannot tell them
    apart: dock artwork with a narrow white margin lands just under any such
    threshold and would silently be rebuilt from the flat mark instead.
    """
    if image.width != image.height or image.width < 4:
        return False
    size = image.width
    inset = max(2, size // 12)

    def alpha(x: int, y: int) -> int:
        return image.pixels[(y * size + x) * 4 + 3]

    corners = ((0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1))
    if any(alpha(x, y) > 7 for x, y in corners):
        return False
    inside = (
        (size // 2, inset),
        (size // 2, size - 1 - inset),
        (inset, size // 2),
        (size - 1 - inset, size // 2),
    )
    return all(alpha(x, y) > 200 for x, y in inside)


def square_crop(image: Image, box: tuple[int, int, int, int]) -> Image:
    """Copy the largest centred square out of ``box``."""
    left, top, right, bottom = box
    side = min(right - left, bottom - top)
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2
    left = max(0, min(image.width - side, center_x - side // 2))
    top = max(0, min(image.height - side, center_y - side // 2))
    pixels = bytearray(side * side * 4)
    stride = side * 4
    for row in range(side):
        source = ((top + row) * image.width + left) * 4
        pixels[row * stride : (row + 1) * stride] = image.pixels[source : source + stride]
    return Image(side, side, pixels)


def rounded_plate(image: Image) -> Image:
    """Punch the four corners out of an opaque square plate.

    Artwork that already carries transparency (or is not square) is returned
    untouched, so a hand drawn rounded png keeps its own outline.
    """
    if image.width != image.height:
        return image
    pixels = image.pixels
    if min(bytes(pixels[3::4])) < 255:
        return image
    size = image.width
    radius = size * ROUNDED_CORNER_RATIO
    limit = min(size, int(radius) + 2)
    out = bytearray(pixels)
    for y in range(limit):
        for x in range(limit):
            for cx, cy in ((x, y), (size - 1 - x, y), (x, size - 1 - y), (size - 1 - x, size - 1 - y)):
                dx = radius - (cx + 0.5)
                dy = radius - (cy + 0.5)
                distance = (dx * dx + dy * dy) ** 0.5
                coverage = radius + 0.5 - distance
                if coverage >= 1:
                    continue
                offset = (cy * size + cx) * 4 + 3
                out[offset] = 0 if coverage <= 0 else int(out[offset] * coverage)
    return Image(size, size, out)


def _plate_alpha(x: int, y: int, inset: float, edge: float, radius: float) -> int:
    """Antialiased coverage of a rounded rectangle at pixel ``x, y``."""
    center = inset + radius
    span = edge - radius
    nearest_x = min(max(x + 0.5, center), span)
    nearest_y = min(max(y + 0.5, center), span)
    distance = math.hypot(x + 0.5 - nearest_x, y + 0.5 - nearest_y) - radius
    coverage = 0.5 - distance
    if coverage <= 0:
        return 0
    if coverage >= 1:
        return 255
    return int(coverage * 255)


def dock_plate(image: Image) -> Image:
    """Punch the white margin of a hand drawn dock icon to transparency.

    Square artwork that is already transparent is returned untouched, so a
    source with its own outline keeps every pixel.
    """
    if image.width != image.height or min(bytes(image.pixels[3::4])) < 255:
        return image
    size = image.width
    inset = size * DOCK_PLATE_INSET_RATIO
    edge = size - inset
    radius = size * DOCK_PLATE_CORNER_RATIO
    out = bytearray(image.pixels)
    for y in range(size):
        for x in range(size):
            alpha = _plate_alpha(x, y, inset, edge, radius)
            if alpha >= 255:
                continue
            offset = (y * size + x) * 4 + 3
            out[offset] = 0 if alpha <= 0 else out[offset] * alpha // 255
    return Image(size, size, out)


def compose_app_icon(mark: Image, size: int = APP_ICON_CANVAS) -> Image:
    """Draw the transparent artwork centred on a white rounded plate."""
    box = mark_box(mark)
    cropped = square_crop(mark, box) if box else mark
    content_size = max(1, min(size, int(size * APP_ICON_CONTENT)))
    content = Resizer(cropped).resize(content_size)
    plate = rounded_plate(Image(size, size, b"\xff\xff\xff\xff" * (size * size)))
    offset = (size - content_size) // 2
    for y in range(content_size):
        for x in range(content_size):
            source = (y * content_size + x) * 4
            alpha = content.pixels[source + 3]
            if alpha == 0:
                continue
            target = ((y + offset) * size + x + offset) * 4
            if alpha == 255:
                plate.pixels[target : target + 4] = content.pixels[source : source + 4]
                continue
            rest = 255 - alpha
            for channel in range(3):
                plate.pixels[target + channel] = (content.pixels[source + channel] * alpha + 255 * rest) // 255
    return plate


def fit_macos_canvas(image: Image, size: int = APP_ICON_CANVAS) -> Image:
    """Scale a finished rounded card to the official macOS share of the canvas.

    The visible bounds are measured first, so a transparent margin the source
    already carries is not counted twice: only the card itself ends up at
    ``MACOS_PLATE_RATIO`` of the canvas, which is what the Dock expects.
    """
    box = mark_box(image)
    cropped = square_crop(image, box) if box else image
    plate_size = max(1, int(round(size * MACOS_PLATE_RATIO)))
    content = Resizer(cropped).resize(plate_size)
    canvas = Image(size, size, bytes(size * size * 4))
    offset = (size - plate_size) // 2
    for y in range(plate_size):
        source = (y * plate_size) * 4
        target = ((y + offset) * size + offset) * 4
        canvas.pixels[target : target + plate_size * 4] = content.pixels[source : source + plate_size * 4]
    log_line(f"app icon fitted canvas={size} plate={plate_size}")
    return canvas


def app_icon_image(app_path: Path, mark_path: Path | None = None) -> Image:
    """Build the shipped app icon: transparent artwork on a white rounded plate.

    ``icon圆角.png`` draws its own plate, but the artwork overflows that plate,
    so cropping it would bite the keys. Composing the transparent artwork on a
    freshly drawn plate keeps every pixel and matches the macOS icon shape. A
    source that already ships transparent rounded corners is used untouched.

    Whatever branch builds the card, the result is scaled into the official
    824/1024 macOS canvas before it is handed to the icns and ico writers.
    """
    app_source = read_png(app_path)
    if has_punched_corners(app_source):
        log_line(f"app icon source kept as drawn path={app_path}")
        return fit_macos_canvas(app_source)
    if mark_path is not None and Path(mark_path).is_file() and Path(mark_path).resolve() != Path(app_path).resolve():
        log_line(f"app icon composed from mark path={mark_path}")
        return fit_macos_canvas(compose_app_icon(read_png(mark_path)))
    if plate_coverage(app_source) >= 0.9:
        log_line(f"app icon plate corners punched path={app_path}")
        return fit_macos_canvas(rounded_plate(app_source))
    log_line(f"app icon composed from source path={app_path}")
    return fit_macos_canvas(compose_app_icon(app_source))


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


def _box_mask(src: bytearray, src_size: int, target: int) -> bytearray:
    """Area average a single channel mask down to ``target`` pixels (0-255)."""
    out = bytearray(target * target)
    scale = src_size / target
    for y in range(target):
        y0 = int(y * scale)
        y1 = max(y0 + 1, int((y + 1) * scale))
        for x in range(target):
            x0 = int(x * scale)
            x1 = max(x0 + 1, int((x + 1) * scale))
            total = 0
            for sy in range(y0, y1):
                base = sy * src_size
                for sx in range(x0, x1):
                    total += src[base + sx]
            out[y * target + x] = total // ((y1 - y0) * (x1 - x0))
    return out


class Resizer:
    """Halving pyramid so every size is built from a close smaller buffer."""

    def __init__(self, image: Image) -> None:
        self.source = image
        self.premultiplied = _premultiply(image)
        self.levels: list[tuple[int, bytearray]] = [(image.width, self.premultiplied)]
        self.masks: dict[str, bytearray] = {}

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
        level_size, level = self._level_for(size)
        pixels = level if level_size == size else _box(level, level_size, size)
        return Image(size, size, _unpremultiply(bytearray(pixels)))

    def _mask(self, name: str, hole: bool) -> bytearray:
        if name in self.masks:
            return self.masks[name]
        mask = bytearray(self.source.width * self.source.height)
        pixels = self.source.pixels
        for index in range(len(mask)):
            offset = index * 4
            if pixels[offset + 3] < 128:
                continue
            if hole:
                luminance = (
                    299 * pixels[offset] + 587 * pixels[offset + 1] + 114 * pixels[offset + 2]
                ) // 1000
                if luminance < 240:
                    continue
            mask[index] = 255
        self.masks[name] = mask
        return mask

    def silhouette(self, size: int, body_keep: int = 140, hole_keep: int = 150) -> Image:
        """Solid menu bar mark: opaque body, bright highlights punched out.

        The body and the highlight holes are thresholded at full resolution and
        only then scaled down, so the three nodes and the code chevrons survive
        at 18 px instead of blurring into one blob.
        """
        body = _box_mask(self._mask("body", False), self.source.width, size)
        hole = _box_mask(self._mask("hole", True), self.source.width, size)
        out = bytearray(size * size * 4)
        for index in range(size * size):
            if body[index] >= body_keep and hole[index] < hole_keep:
                offset = index * 4
                out[offset : offset + 4] = b"\x00\x00\x00\xff"
        return Image(size, size, out)


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
    """A drawing change in this file always wins over an old derived icon."""
    if not target.is_file():
        return False
    newest = max(source.stat().st_mtime, Path(__file__).stat().st_mtime)
    return target.stat().st_mtime >= newest


def repo_app_image() -> Path:
    """The app icon source: the dock artwork first, then the plates, then the mark."""
    if REPO_DOCK_IMAGE.is_file():
        return REPO_DOCK_IMAGE
    return REPO_ROUNDED_IMAGE if REPO_ROUNDED_IMAGE.is_file() else REPO_IMAGE


def source_image_bytes(path: Path) -> bytes:
    """The bytes written for a source image; dock artwork loses its white margin."""
    if path == REPO_DOCK_IMAGE and path.is_file():
        return encode_png(dock_plate(read_png(path)))
    return path.read_bytes()


def copy_source(force: bool = False) -> Path:
    """Keep packaging/assets/app-icon.png in sync with the app icon source."""
    repo_image = repo_app_image()
    if not repo_image.is_file():
        return SOURCE_IMAGE
    payload = source_image_bytes(repo_image)
    if force or not SOURCE_IMAGE.is_file() or payload != SOURCE_IMAGE.read_bytes():
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        SOURCE_IMAGE.write_bytes(payload)
        if repo_image == REPO_DOCK_IMAGE:
            log_line(f"icon source dock plate corners punched path={SOURCE_IMAGE}")
        if repo_image == REPO_IMAGE:
            log_line(f"icon source fallback missing={REPO_ROUNDED_IMAGE.name} path={repo_image}")
        log_line(f"icon source copied path={SOURCE_IMAGE}")
    return SOURCE_IMAGE


def copy_mark_source(force: bool = False) -> Path:
    """Keep app-mark.png in sync with the transparent icon.png."""
    if not REPO_IMAGE.is_file():
        return copy_source(force)
    if force or not MARK_IMAGE.is_file() or REPO_IMAGE.read_bytes() != MARK_IMAGE.read_bytes():
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_IMAGE, MARK_IMAGE)
        log_line(f"mark source copied path={MARK_IMAGE}")
    return MARK_IMAGE


def ensure_assets(force: bool = False) -> dict[str, Path]:
    """Generate every derived icon; returns the files that exist afterwards."""
    source = copy_source(force)
    mark_source = copy_mark_source(force)
    if not source.is_file():
        log_line("icon source missing; keeping the default icons")
        return {}
    if not mark_source.is_file():
        mark_source = source
    stale = [
        target
        for target in (APP_ICO, TRAY_ICO)
        if force or not _is_fresh(target, source)
    ]
    stale_marks = [
        target
        for target in (STATUS_PNG, STATUS_PNG_2X, UI_LOGO)
        if force or not _is_fresh(target, mark_source)
    ]
    if not stale and not stale_marks:
        log_line("icons already up to date")
        return {
            "source": source,
            "mark": mark_source,
            "app_ico": APP_ICO,
            "tray_ico": TRAY_ICO,
            "status_png": STATUS_PNG,
            "status_png_2x": STATUS_PNG_2X,
            "ui_logo": UI_LOGO,
        }
    if stale:
        resizer = Resizer(app_icon_image(source, mark_source))
        write_ico(APP_ICO, [resizer.resize(size) for size in APP_ICO_SIZES])
        log_line(f"app ico written sizes={','.join(str(size) for size in APP_ICO_SIZES)} path={APP_ICO}")
        write_ico(TRAY_ICO, [resizer.resize(size) for size in TRAY_ICO_SIZES])
        log_line(f"tray ico written sizes={','.join(str(size) for size in TRAY_ICO_SIZES)} path={TRAY_ICO}")
    if stale_marks:
        mark = Resizer(read_png(mark_source))
        write_png(STATUS_PNG, mark.silhouette(STATUS_PX))
        write_png(STATUS_PNG_2X, mark.silhouette(STATUS_PX * 2))
        log_line(f"status bar mark written px={STATUS_PX} path={STATUS_PNG}")
        write_png(UI_LOGO, mark.resize(LOGO_PX))
        log_line(f"ui logo written px={LOGO_PX} path={UI_LOGO}")
    return {
        "source": source,
        "mark": mark_source,
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
    log_line(f"iconset written files={len(written)} path={target_dir}")
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
    log_line(f"icns written bytes={target.stat().st_size} path={target}")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Derive the app, tray and UI icons from the repository images")
    parser.add_argument("--force", action="store_true", help="rebuild even when the outputs look fresh")
    args = parser.parse_args(argv)
    produced = ensure_assets(force=args.force)
    if not produced:
        print("icon source missing: nothing generated")
        return 1
    for name, path in sorted(produced.items()):
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
