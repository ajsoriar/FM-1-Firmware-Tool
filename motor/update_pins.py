#!/usr/bin/env python3
"""Regenerate pins.json: the sha256 of every file the installer's recovery tool runs or loads.

`fm1_unbrick.py doctor` compares these files with pins.json before any device
is touched. Run this only after reviewing a deliberate change to the recovery tool:

    py INSTALLER\\motor\\update_pins.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
JLUB_COMMIT = "adb3f18889e88ac512ce0a3c4d8cc3d3cb30696a"
SKIP_DIRS = {"__pycache__", ".git"}


def pinned_files():
    yield HERE / "fm1_unbrick.py"
    yield HERE / "LICENSE-MvaveFM1Unbricker"
    for path in sorted((HERE / "jl-uboot-tool").rglob("*")):
        if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(HERE).parts):
            yield path


def main():
    files = {p.relative_to(HERE).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in pinned_files()}
    pins = {
        "fm1_unbrick_upstream": "MvaveFM1Unbricker 707470a521d49c9791b373ff522653dd41dd6f4a "
                                "+ identity 3..8 digits + --events (INSTALLER copy)",
        "jl_uboot_tool_commit": JLUB_COMMIT,
        "jl_uboot_tool_source": "https://github.com/kagaimiq/jl-uboot-tool (MIT, see "
                                "jl-uboot-tool/LICENSE)",
        "files": files,
    }
    (HERE / "pins.json").write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")
    print("pins.json: %d files" % len(files))


if __name__ == "__main__":
    main()
