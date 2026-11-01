#!/usr/bin/env python3
"""Draw fm1_connected.ico and fm1_disconnected.ico (standard library only).

A USB plug with a round badge: green with a tick (connected) or red with an
X (not detected). Run again only to change the drawing:

    py INSTALLER\\app\\resources\\make_icons.py
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIZES = (16, 24, 32, 48, 64)
SAMPLES = 4                    # supersampling per axis

DARK = (55, 65, 81)
METAL = (156, 163, 175)
WHITE = (255, 255, 255)
GREEN = (21, 128, 61)
RED = (185, 28, 28)


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def shade(x, y, connected):
    """Colour (r, g, b, a) at unit coordinates, last shape drawn wins."""
    color = None
    if 0.02 <= x <= 0.30 and abs(y - 0.42) <= 0.05:
        color = DARK + (255,)                                   # cable
    if 0.28 <= x <= 0.60 and 0.24 <= y <= 0.60:
        color = DARK + (255,)                                   # plug body
    if 0.60 <= x <= 0.86 and 0.30 <= y <= 0.54:
        color = METAL + (255,)                                  # connector
    cx, cy, r = 0.70, 0.70, 0.29
    d = math.hypot(x - cx, y - cy)
    if d <= r + 0.04:
        color = WHITE + (255,)                                  # badge ring
    if d <= r:
        color = (GREEN if connected else RED) + (255,)
        if connected:
            mark = min(seg_dist(x, y, 0.57, 0.71, 0.66, 0.80),
                       seg_dist(x, y, 0.66, 0.80, 0.83, 0.60))
        else:
            mark = min(seg_dist(x, y, 0.60, 0.60, 0.80, 0.80),
                       seg_dist(x, y, 0.80, 0.60, 0.60, 0.80))
        if mark <= 0.055:
            color = WHITE + (255,)
    return color


def render(size, connected):
    """Rows of BGRA pixels, bottom-up, as the ICO bitmap wants them."""
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    c = shade((px + (sx + 0.5) / SAMPLES) / size,
                              (py + (sy + 0.5) / SAMPLES) / size, connected)
                    if c:
                        for i in range(3):
                            acc[i] += c[i] * c[3]
                        acc[3] += c[3]
            n = SAMPLES * SAMPLES
            alpha = acc[3] / n
            if acc[3]:
                r, g, b = (acc[i] / acc[3] for i in range(3))
            else:
                r = g = b = 0
            row += bytes((round(b), round(g), round(r), round(alpha)))
        rows.append(bytes(row))
    return b"".join(reversed(rows))


def ico(connected):
    images = []
    for size in SIZES:
        pixels = render(size, connected)
        mask = b"\x00" * (((size + 31) // 32) * 4 * size)      # alpha does the masking
        header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                             len(pixels) + len(mask), 0, 0, 0, 0)
        images.append((size, header + pixels + mask))
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for size, data in images:
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(data for _, data in images)


def main():
    (HERE / "fm1_connected.ico").write_bytes(ico(True))
    (HERE / "fm1_disconnected.ico").write_bytes(ico(False))
    print("fm1_connected.ico, fm1_disconnected.ico written")


if __name__ == "__main__":
    main()
