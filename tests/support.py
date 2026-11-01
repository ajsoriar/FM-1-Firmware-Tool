"""Shared test helpers: synthetic .fwsc packages and a scripted recovery job."""

from __future__ import annotations

import importlib.util
import json
import random
import struct
import sys
from pathlib import Path

INSTALLER = Path(__file__).resolve().parent.parent
if str(INSTALLER) not in sys.path:
    sys.path.insert(0, str(INSTALLER))

from app import worker  # noqa: E402

_spec = importlib.util.spec_from_file_location("fm1_unbrick_for_tests", worker.RECOVERY_SCRIPT)
fu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fu)


def make_fwsc(identity: str, flash: bytes) -> bytes:
    """A package laid out as the format is described (inverse of parse_fwsc + ufw_flash)."""
    files = [(0, b"flash.bin", flash), (100, b"ota.bin", b"OTA" * 100)]
    off, ents = 0x400, []
    for i, (typ, name, d) in enumerate(files):
        off = (off + 0xFF) & ~0xFF
        e = bytearray(0x50)
        struct.pack_into("<HHHHIII", e, 0, typ, i, fu._crc16(d), 0, off, len(d),
                         (len(d) + 31) & ~31)
        e[0x40:0x40 + len(name)] = name
        ents.append((e, off, d))
        off += len(d)
    hdr = bytearray(0x40)
    struct.pack_into("<IH", hdr, 4, off, len(files))
    struct.pack_into("<HHH", hdr, 10, 0x0004, 0x0200, 0)
    hdr[16:22] = b"AC791N"
    lst = b"".join(fu._jl_enc(e) for e, _, _ in ents)
    struct.pack_into("<H", hdr, 2, fu._crc16(lst))
    struct.pack_into("<H", hdr, 0, fu._crc16(hdr[2:0x40]))
    image = bytearray(b"\xFF" * off)
    image[0:0x40] = fu._jl_enc(hdr)
    image[0x40:0x40 + len(lst)] = lst
    for _, o, d in ents:
        image[o:o + len(d)] = d
    out = bytearray()
    for i in range(fu.FWSC_MARKED_BLOCKS):
        out += image[i * fu.FWSC_BLOCK:(i + 1) * fu.FWSC_BLOCK]
        out.append((ord(identity[i]) + i + 1) & 0xFF if i < len(identity) else fu.FWSC_NO_CHAR)
    out += image[fu.FWSC_BLOCK * fu.FWSC_MARKED_BLOCKS:]
    return bytes(out)


def write_package(folder: Path, identity="FM-1_9530", seed=1, name="test.fwsc") -> Path:
    path = Path(folder) / name
    path.write_bytes(make_fwsc(identity, random.Random(seed).randbytes(0x94000)))
    return path


def write_capture(folder: Path, package: Path, status="already_present", expected=None):
    """A Firmware Hub capture manifest.json with one record for `package`."""
    sha = fu.sha256(package.read_bytes())
    record = {"name": "Test", "version": "1.0", "identity": "FM-1_9530",
              "relative_path": package.relative_to(folder).as_posix(), "status": status,
              "sha256": sha, "expected_sha256": sha if expected is None else expected,
              "hash_source": "published" if (expected is None or expected) else "calculated_only"}
    (Path(folder) / "manifest.json").write_text(json.dumps(
        {"tool": "FM-1 Firmware Hub Downloader", "records": [record]}), encoding="utf-8")


def ev(_event, **fields):
    return dict({"schema": 1, "event": _event}, **fields)


class ScriptedLauncher:
    """Stands in for worker.RecoveryJob: `script(argv)` returns the messages a run posts.

    Each script item is ("event", dict) / ("stderr", str) / ("exit", code), or
    ("wait",) to stop until the controller answers a confirmation; the answer
    is passed to `on_answer(text)` which returns the rest of the messages.
    """

    def __init__(self, script):
        self.script = script
        self.argvs = []
        self.jobs = []

    def __call__(self, argv, cwd, post):
        job = ScriptedJob(self, argv, post)
        self.argvs.append(argv)
        self.jobs.append(job)
        return job


class ScriptedJob:
    def __init__(self, launcher, argv, post):
        self.launcher, self.argv, self.post = launcher, argv, post
        self.answers = []
        self.terminated = False
        self.rest = None

    def start(self):
        self._play(self.launcher.script(self.argv, self))

    def _play(self, items):
        for i, item in enumerate(items):
            if item[0] == "wait":
                self.rest = item[1]
                return
            self.post(*item)

    def send_line(self, text):
        self.answers.append(text)
        if self.rest is not None:
            rest, self.rest = self.rest, None
            self._play(rest(text))
        return True

    def terminate(self):
        self.terminated = True
        self.post("exit", 1)

    @property
    def running(self):
        return not self.terminated
