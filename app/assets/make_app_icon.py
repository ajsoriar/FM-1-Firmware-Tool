"""Generate the FM-1 Firmware Tool multi-resolution Windows icon (stdlib only)."""

from __future__ import annotations

import math
import struct
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIZES = (16, 24, 32, 48, 64, 128, 256)
SAMPLES = 4


def rounded_rect(x, y, left, top, right, bottom, radius):
    cx = min(max(x, left + radius), right - radius)
    cy = min(max(y, top + radius), bottom - radius)
    return math.hypot(x - cx, y - cy) <= radius


def pixel(x, y):
    """Return an RGBA pixel in unit-square coordinates."""
    color = (0, 0, 0, 0)
    if rounded_rect(x, y, .08, .08, .92, .92, .19):
        color = (17, 34, 64, 255)  # navy instrument panel
    if rounded_rect(x, y, .19, .19, .81, .42, .045):
        color = (34, 211, 238, 255)  # bright display
    if rounded_rect(x, y, .25, .25, .58, .36, .02):
        color = (8, 47, 73, 255)  # display readout
    # White keyboard bed and seven key divisions.
    if rounded_rect(x, y, .16, .52, .84, .79, .035):
        color = (248, 250, 252, 255)
    if .19 <= x <= .81 and .55 <= y <= .76:
        for i in range(1, 7):
            if abs(x - (.19 + i * .62 / 7)) <= .006:
                color = (148, 163, 184, 255)
    # Five raised black keys.
    for i in (0, 1, 3, 4, 5):
        center = .19 + (i + 1) * .62 / 7
        if center - .028 <= x <= center + .028 and .54 <= y <= .67:
            color = (15, 23, 42, 255)
    # Small green ready indicator.
    if math.hypot(x - .72, y - .305) <= .035:
        color = (22, 163, 74, 255)
    return color


def render(size):
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            sums = [0, 0, 0, 0]
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    rgba = pixel((px + (sx + .5) / SAMPLES) / size,
                                 (py + (sy + .5) / SAMPLES) / size)
                    for n, value in enumerate(rgba):
                        sums[n] += value
            row.extend(round(value / (SAMPLES * SAMPLES)) for value in sums[2::-1])
            row.append(round(sums[3] / (SAMPLES * SAMPLES)))
        rows.append(bytes(row))
    return b"".join(reversed(rows))


def make_ico():
    images = []
    for size in SIZES:
        pixels = render(size)
        mask = b"\0" * (((size + 31) // 32) * 4 * size)
        header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                             len(pixels) + len(mask), 0, 0, 0, 0)
        images.append((size, header + pixels + mask))
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for size, data in images:
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0,
                           1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(data for _, data in images)


if __name__ == "__main__":
    target = HERE / "fm1_installer.ico"
    target.write_bytes(make_ico())
    print(target)
