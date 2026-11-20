#!/usr/bin/env python3
"""Genereerib kvoot.ico ikooni ainult Pythoni standardteegiga (PNG-in-ICO)."""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _encode_rgba_png(size: int, pixels: list[tuple[int, int, int, int]]) -> bytes:
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        row_start = y * size
        for x in range(size):
            r, g, b, a = pixels[row_start + x]
            raw.extend((r, g, b, a))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), level=9)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")


def _blend(dst: tuple[float, float, float, float], src: tuple[int, int, int], alpha: float) -> tuple[float, float, float, float]:
    a = max(0.0, min(1.0, alpha))
    r = dst[0] * (1.0 - a) + src[0] * a
    g = dst[1] * (1.0 - a) + src[1] * a
    b = dst[2] * (1.0 - a) + src[2] * a
    return (r, g, b, max(dst[3], a * 255.0))


def _dist_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _render_icon_size(size: int) -> bytes:
    bg = (13, 17, 23)       # #0d1117 tume taust
    border = (42, 50, 61)   # #2a323d õhuke ääris
    track = (42, 52, 66)    # #2a3442 tume mõõdikukaar
    mint = (46, 229, 157)   # #2ee59d heleroheline kuma-detail
    glow = (64, 245, 178)   # pehme kuma
    hub = (230, 237, 243)   # neutraalne hele keskpunkt

    cx = (size - 1) / 2.0
    cy = (size - 1) / 2.0
    rad = size * 0.46
    corner_r = size * 0.20
    gauge_cy = size * 0.56
    gauge_r = size * 0.30
    stroke = max(1.3, size * 0.068)

    # Kaar -215 kraadist +35 kraadini (kokku 250 kraadi), roheline osa 60% ulatuses
    ang_start = math.radians(-215.0)
    ang_end = math.radians(35.0)
    ang_needle = math.radians(-70.0)
    nx2 = cx + math.cos(ang_needle) * (gauge_r * 0.86)
    ny2 = gauge_cy + math.sin(ang_needle) * (gauge_r * 0.86)

    pixels: list[tuple[int, int, int, int]] = []
    for y in range(size):
        for x in range(size):
            # Ümar ruut SDF
            qx = abs(x - cx) - (rad - corner_r)
            qy = abs(y - cy) - (rad - corner_r)
            sdf = math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - corner_r
            if sdf > 1.0:
                pixels.append((0, 0, 0, 0))
                continue
            aa = max(0.0, min(1.0, 1.0 - sdf))
            col: tuple[float, float, float, float] = (float(bg[0]), float(bg[1]), float(bg[2]), aa * 255.0)

            # Õhuke ääris
            if -1.6 <= sdf <= 0.6:
                b_alpha = max(0.0, min(1.0, 1.0 - abs(sdf + 0.5))) * 0.75 * aa
                col = _blend(col, border, b_alpha)

            # Mõõdikukaare kaugus
            dx, dy = x - cx, y - gauge_cy
            dist_r = abs(math.hypot(dx, dy) - gauge_r)
            ang = math.atan2(dy, dx)
            # Normaliseeri [-215°, +35°] vahemikku: nihe +215° -> [0, 250°]
            rel_deg = (math.degrees(ang) + 215.0) % 360.0
            on_arc = rel_deg <= 250.0

            if on_arc:
                if dist_r < stroke * 0.65:
                    t_alpha = max(0.0, min(1.0, 1.0 - (dist_r - stroke * 0.35) / (stroke * 0.35))) * aa
                    if rel_deg <= 145.0:
                        # Heleroheline aktiivne kaar + kuma
                        col = _blend(col, mint, t_alpha)
                    else:
                        col = _blend(col, track, t_alpha)
                elif rel_deg <= 145.0 and dist_r < stroke * 1.9:
                    # Kuma ainult rohelise kaare ümber
                    g_alpha = max(0.0, 1.0 - dist_r / (stroke * 1.9)) * 0.34 * aa
                    col = _blend(col, glow, g_alpha)

            # Mõõdiku nõel (roheline kuma)
            d_needle = _dist_segment(x, y, cx, gauge_cy, nx2, ny2)
            n_w = max(1.0, size * 0.042)
            if d_needle < n_w * 2.2:
                if d_needle < n_w:
                    n_alpha = max(0.0, min(1.0, 1.0 - max(0.0, d_needle - n_w * 0.5) / (n_w * 0.5))) * aa
                    col = _blend(col, mint, n_alpha)
                else:
                    g_alpha = max(0.0, 1.0 - d_needle / (n_w * 2.2)) * 0.35 * aa
                    col = _blend(col, glow, g_alpha)

            # Keskpunkt
            d_hub = math.hypot(dx, dy)
            hub_r = max(1.2, size * 0.055)
            if d_hub < hub_r + 0.8:
                h_alpha = max(0.0, min(1.0, 1.0 - (d_hub - hub_r * 0.6) / (hub_r * 0.6))) * aa
                col = _blend(col, hub, h_alpha)

            pixels.append((int(col[0]), int(col[1]), int(col[2]), int(col[3])))

    return _encode_rgba_png(size, pixels)


def build_ico(out_path: Path, sizes: tuple[int, ...] = (16, 32, 48, 64)) -> Path:
    """Kirjutab mitme suurusega .ico faili (PNG-kujutised ICO konteineris)."""
    images = [(s, _render_icon_size(s)) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = bytearray()
    payload = bytearray()
    for s, png in images:
        dim = 0 if s >= 256 else s
        entries.extend(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset))
        payload.extend(png)
        offset += len(png)
    out_path.write_bytes(header + bytes(entries) + bytes(payload))
    return out_path


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "kvoot.ico"
    build_ico(target)
    print(f"Genereeritud: {target} ({target.stat().st_size} baiti)")
