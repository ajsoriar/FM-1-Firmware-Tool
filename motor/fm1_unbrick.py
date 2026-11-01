#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 MvaveFM1Unbricker contributors
"""
fm1_unbrick.py - restore a soft-bricked M-VAVE FM-1 over plain USB.

A soft-bricked FM-1 shows a black screen, is "not found" by the firmware
installers, and appears on the computer as a USB disk called
"WL82 UBOOT1.00": the JieLi WL82 (AC791N) chip's mask-ROM boot mode.
This script automates the community recovery procedure: it talks to that
boot mode through kagaimiq's jl-uboot-tool (MIT), backs up the whole flash
twice, extracts the firmware region from an FM-1 .fwsc package and writes
it at 0x4000, then reads it back and compares every byte.

Two helpers for a unit that still runs: `softkey` sends the stock firmware's
USB-MIDI key that drops it into that boot mode (any OS), and `repair-settings`
puts the settings areas back to factory state after a custom firmware (stock
V15 reads that firmware's leftovers as its own settings).

Safety rules, enforced in code:
  * nothing is ever erased or written below 0x4000 (the boot head), in
    0xE9000..0xFBFFF (Bluetooth data, factory voices) or 0xFF000..0xFFFFF;
  * "erase chip" is never used;
  * a verified double backup is taken before any write;
  * the chip key (0x980F) and flash ID (0x856014) must match an FM-1.

Run `python fm1_unbrick.py --help` and see README.md.

Installer copy (INSTALLER/motor): `--events` adds JSON Lines events on stdout
for the graphical installer (human output moves to stderr, confirmations are
read as lines from stdin and still compared with the exact phrase), plus the
read-only commands `inspect`, `doctor` and `probe`. Without `--events` the
command line behaves as upstream.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import types
import urllib.request
import zipfile
from pathlib import Path

TOOL_VERSION = "1.2.0+events.1"

# --------------------------------------------------------------------------
# Flash layout of the FM-1 (1 MiB SPI NOR flash)
# --------------------------------------------------------------------------

FLASH_SIZE = 0x100000
SECTOR_SIZE = 0x1000           # smallest erase unit
BLOCK_SIZE = 0x10000           # large erase unit
PROTECTED_END = 0x4000         # 0x0000..0x3FFF = boot head: NEVER erased or written
FW_START = 0x4000              # firmware region: app, SDK files, loader
FW_LEN = 0x8F000
FW_END = FW_START + FW_LEN     # 0x93000 (exclusive)

# Above the firmware, as a factory-fresh stock unit's dump shows it:
VM_START = FW_END              # 0x93000..0xE8FFF: stock V15's settings store (JieLi "VM")
VM_END = 0xE9000
VM_MAGIC = b"\x55\xAA\xAA\x55"  # factory state: only the sector 0x93000 holds data, it starts so
BTIF_START = 0xE9000           # 0xE9000..0xE9FFF: per-unit Bluetooth data
USR_START = 0xEA000            # 0xEA000..0xFBFFF: factory voices / preset resources
GLOBALS_START = 0xFC000        # 0xFC000..0xFEFFF: erased at the factory; Felucca-family globals
GLOBALS_END = 0xFF000
KEY_MAC_START = 0xFF000        # 0xFF000..0xFFFFF: erased at the factory ("key_mac")

# Never erased or written by any command (enforced in _check_mutable).
NEVER_TOUCH = ((BTIF_START, GLOBALS_START, "the Bluetooth data and factory voices"),
               (KEY_MAC_START, FLASH_SIZE, "the key_mac area"))
# The only ranges repair-settings may erase or write (enforced in _check_repairable).
REPAIR_WINDOWS = ((VM_START, VM_END), (GLOBALS_START, GLOBALS_END))
# What lives in those windows, for the report before the repair.
SETTINGS_AREAS = (
    (0x93000, 0x94000, "stock settings, first sector (the factory data)"),
    (0x94000, 0x97000, "stock settings store"),
    (0x97000, 0xE0000, "Felucca-family main store (songs, sounds, settings)"),
    (0xE0000, 0xE5000, "Felucca-family staged update loader"),
    (0xE5000, 0xE9000, "stock settings store"),
    (GLOBALS_START, GLOBALS_END, "Felucca-family globals / superblock"),
)

EXPECTED_CHIP_KEY = 0x980F
EXPECTED_FLASH_ID = 0x856014   # 1 MiB SPI NOR, as reported by the FM-1

# RAM the previous firmware run left behind, readable from the ROM until the
# loader starts. Melodee keeps its crash record (fm1_crash_t, "CRSH") here; its
# own fm1-recover saves the same 512 bytes as retained-ram.bin.
RETAINED_RAM_ADDR = 0x1C7C000
RETAINED_RAM_LEN = 512
MELODEE_CRASH_MAGIC = 0x43525348
MELODEE_CRASH_FIELDS = ("magic", "count", "vec", "pc", "rets", "emu", "dbg", "sp", "psr",
                        "icfg", "uptime_ms", "etm0", "etm1", "etm2", "etm3", "early")

DEVICE_VENDOR = "WL82"
DEVICE_PRODUCT = "UBOOT1.00"
DEVICE_MODEL = "WL82 UBOOT1.00"

# Official M-VAVE V15: identified by the hash of its firmware region 0x4000..0x92FFF of the
# package's flash.bin. V15_FILE_SHA256 is the genuine FM-1.fwsc file (informational only).
V15_IDENTITY = "FM-1_015"
V15_FLASH_FW_SHA256 = "6edf3c37fb5bbbc33607c89375ee024d5477c17914d72221c8c68e58a8255686"
V15_FILE_SHA256 = "db1642b2b6fa5c2cccb11ffd13878068bb28601678d3644049f99dc40e7edb8a"

# .fwsc packaging: the first FWSC_MARKED_BLOCKS blocks of FWSC_BLOCK data
# bytes are each followed by one marker byte. The markers spell the package
# identity: char i = (marker_i - i - 1) & 0xFF; FWSC_NO_CHAR = no character.
FWSC_BLOCK = 0x2F
FWSC_MARKED_BLOCKS = 20
FWSC_NO_CHAR = 0x7D

# jl-uboot-tool, pinned.
JLUB_REPO = "https://github.com/kagaimiq/jl-uboot-tool"
JLUB_COMMIT = "adb3f18889e88ac512ce0a3c4d8cc3d3cb30696a"
JLUB_ZIP_URL = JLUB_REPO + "/archive/" + JLUB_COMMIT + ".zip"
JLUB_MARKER = ".fm1-unbrick-commit"
WL82_LOADER_REL = "data/loaderblobs/usb/wl82loader.bin"
WL82_LOADER_SHA256 = "d41da6126760c9d66660bcc0cac8d27d221806c5e369a8036921efe68dca5376"
LOADER_TARGET_SPI_NOR = 1      # jl-uboot-tool's default loader argument

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_JL_DIR = SCRIPT_DIR / "jl-uboot-tool"

MACOS_MESSAGE = ("not supported: jl-uboot-tool has no macOS SCSI back end; "
                 "use a Windows or Linux machine (a Raspberry Pi works)")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_UNSUPPORTED = 2
EXIT_DANGER = 3                # the firmware region may be incomplete: do not power off

log = logging.getLogger("fm1_unbrick")


class UnbrickError(Exception):
    """A refusal or failure with a message meant for the user.

    event_code names the failure in --events mode (step_failed.code)."""

    def __init__(self, message, code=EXIT_FAIL, event_code=None):
        super().__init__(message)
        self.code = code
        self.event_code = event_code


class SafetyError(UnbrickError):
    """An operation that the safety rules forbid."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def sha256(data) -> str:
    return hashlib.sha256(data).hexdigest()


def timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    path = directory / (stem + suffix)
    n = 2
    while path.exists():
        path = directory / ("%s-%d%s" % (stem, n, suffix))
        n += 1
    return path


def platform_supported() -> bool:
    return sys.platform == "win32" or sys.platform.startswith("linux")


# --------------------------------------------------------------------------
# --events: JSON Lines on stdout for the graphical installer
# --------------------------------------------------------------------------
#
# One JSON object per line, always {"schema": EVENT_SCHEMA, "event": ...}.
# Events carry paths, sizes and hashes only: never flash contents.

EVENT_SCHEMA = 1
_events = None                 # the stream events go to; None: plain command line
_step = None                   # the step named by the last step_started


def events_on() -> bool:
    return _events is not None


def emit(event, **fields):
    if _events is None:
        return
    record = {"schema": EVENT_SCHEMA, "event": event}
    record.update(fields)
    _events.write(json.dumps(record) + "\n")       # ASCII: safe in any console code page
    _events.flush()


def step_started(step, title, **fields):
    global _step
    _step = step
    emit("step_started", step=step, title=title, **fields)


def step_succeeded(step, **fields):
    emit("step_succeeded", step=step, **fields)


def step_failed(step, code, message, **fields):
    emit("step_failed", step=step, code=code, message=message, **fields)


class Progress:
    """A one-line progress display on stderr (only when it is a terminal);
    with --events, a progress event per percent of the current step."""

    def __init__(self, label, total, phase=None):
        self.label, self.total, self.done = label, max(total, 1), 0
        self.phase = phase or label.lower()
        self.tty = hasattr(sys.stderr, "isatty") and sys.stderr.isatty() and not events_on()
        self.last = -1

    def update(self, n):
        self.done += n
        pct = self.done * 100 // self.total
        if pct == self.last:
            return
        self.last = pct
        emit("progress", step=_step, phase=self.phase, percent=pct)
        if self.tty:
            sys.stderr.write("\r  %-24s %3d%%" % (self.label, pct))
            sys.stderr.flush()

    def close(self):
        if self.tty:
            sys.stderr.write("\r  %-24s done\n" % self.label)
            sys.stderr.flush()


def setup_logging(command: str, log_dir: Path, console_stream=None) -> Path:
    log.handlers[:] = []
    log.setLevel(logging.DEBUG)
    log.propagate = False
    console = logging.StreamHandler(console_stream or sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(console)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = unique_path(log_dir, "unbrick-" + timestamp(), ".log")
    fh = logging.FileHandler(str(path), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(fh)
    log.debug("fm1_unbrick %s, python %s, platform %s, command %r",
              TOOL_VERSION, sys.version.split()[0], sys.platform, command)
    return path


def close_logging():
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)


# --------------------------------------------------------------------------
# .fwsc packages
# --------------------------------------------------------------------------

def parse_fwsc(data: bytes):
    """Return (identity, logical_image) of an FM-1 .fwsc package.

    The first FWSC_MARKED_BLOCKS blocks are FWSC_BLOCK data bytes plus one
    marker byte; everything after them is image data as-is.
    """
    stride = FWSC_BLOCK + 1
    head_len = stride * FWSC_MARKED_BLOCKS
    if len(data) < head_len:
        raise UnbrickError("not an FM-1 .fwsc package: only %d bytes" % len(data))
    image = bytearray()
    chars = []
    for i in range(FWSC_MARKED_BLOCKS):
        block = data[i * stride:(i + 1) * stride]
        image += block[:FWSC_BLOCK]
        marker = block[FWSC_BLOCK]
        if marker != FWSC_NO_CHAR:
            chars.append(chr((marker - i - 1) & 0xFF))
    image += data[head_len:]
    return "".join(chars), bytes(image)


def _crc16(data, c=0):
    """firmware/src/ota.c ota_crc16: poly 0x1021, init 0"""
    for b in data:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) if c & 0x8000 else c << 1
        c &= 0xFFFF
    return c


def _jl_enc(buf):
    """firmware/src/ota.c ota_jl_enc: UFW header cipher, key 0xFFFF (its own inverse)"""
    out, k = bytearray(buf), 0xFFFF
    for i in range(len(out)):
        out[i] ^= k & 0xFF
        k = ((k << 1) ^ (0x1021 if k & 0x8000 else 0)) & 0xFFFF
    return bytes(out)


def _u16(p, o):
    return p[o] | p[o + 1] << 8


def _u32(p, o):
    return _u16(p, o) | _u16(p, o + 2) << 16


def ufw_flash(image: bytes):
    """Return (fl_off, fl_len) of the UFW type-0 entry (flash.bin) of a logical image.

    Port of firmware/src/ota.c ota_ufw: the 0x40-byte header and the 0x50-byte
    entries are ciphered; the entry-list CRC is over the still-ciphered entries.
    Flash address X is logical[fl_off + X].
    """
    if len(image) < 0x400:
        raise UnbrickError("not a UFW package (shorter than its 0x400-byte header)")
    hdr = _jl_enc(image[:0x40])
    if _crc16(hdr[2:0x40]) != _u16(hdr, 0):
        raise UnbrickError("UFW header CRC fails: damaged or not an FM-1 package")
    nent = _u16(hdr, 8)
    if nent == 0 or nent > 11:
        raise UnbrickError("UFW header lists %d entries" % nent)
    if _crc16(image[0x40:0x40 + nent * 0x50]) != _u16(hdr, 2):
        raise UnbrickError("UFW entry list CRC fails: damaged package")
    fl = None
    for i in range(nent):
        e = _jl_enc(image[0x40 + i * 0x50:0x90 + i * 0x50])
        if _u16(e, 0) == 0:
            fl = (_u32(e, 8), _u32(e, 12))
    if fl is None:
        raise UnbrickError("no flash.bin (type 0) entry in the UFW header")
    off, length = fl
    if off + length > len(image):
        raise UnbrickError("flash.bin (0x%X bytes at 0x%X) runs past the package (0x%X bytes)"
                           % (length, off, len(image)))
    if length < FW_END:
        raise UnbrickError("flash.bin is only 0x%X bytes, it must reach 0x%X; this is not a "
                           "complete FM-1 package" % (length, FW_END))
    return fl


IDENTITY_RE = re.compile(r"^FM-1_(\d{3,8})$")


def classify(identity: str, fw_sha: str):
    """Return (kind, description) of a package (fw_sha: flash.bin region hash)."""
    m = IDENTITY_RE.match(identity)
    if fw_sha == V15_FLASH_FW_SHA256:
        return "official-v15", "official M-VAVE V15 (sha256 verified)"
    if identity == V15_IDENTITY:
        return "damaged-v15", ("claims to be official V15 but the firmware hash does "
                               "NOT match: damaged or not genuine")
    if not m:
        return "unknown", "unrecognised package identity %r" % identity
    num = m.group(1)
    if num.startswith("0"):
        return "official-other", ("official-style M-VAVE package %s (no known hash: "
                                  "cannot be verified)" % identity)
    if identity == "FM-1_920":
        return "felucca-family", "ChoralRoot (Felucca-family firmware)"
    return "felucca-family", "third-party Felucca-family firmware %s" % identity


class Package:
    def __init__(self, path, data):
        self.path = Path(path)
        self.file_sha256 = sha256(data)
        self.file_size = len(data)
        self.identity, self.image = parse_fwsc(data)
        if len(self.image) < FW_END:
            raise UnbrickError(
                "%s: the flash image is only 0x%X bytes, it must reach 0x%X; "
                "this is not a complete FM-1 package" % (self.path.name, len(self.image), FW_END))
        try:
            self.fl_off, self.fl_len = ufw_flash(self.image)
        except UnbrickError as e:
            raise UnbrickError("%s: %s" % (self.path.name, e))
        self.flash = self.image[self.fl_off:self.fl_off + self.fl_len]
        self.head = self.flash[:PROTECTED_END]
        self.firmware = self.flash[FW_START:FW_END]
        self.firmware_sha256 = sha256(self.firmware)
        self.head_sha256 = sha256(self.head)
        self.kind, self.description = classify(self.identity, self.firmware_sha256)

    @classmethod
    def load(cls, path):
        path = Path(path)
        try:
            data = path.read_bytes()
        except OSError as e:
            raise UnbrickError("cannot read package %s: %s" % (path, e))
        return cls(path, data)

    def log_summary(self):
        log.info("Package     %s", self.path)
        log.info("  file sha256      %s", self.file_sha256)
        log.info("  identity         %s  = %s", self.identity or "(none)", self.description)
        log.info("  firmware region  0x%X..0x%X (0x%X bytes)", FW_START, FW_END - 1, FW_LEN)
        log.info("  flash.bin        offset 0x%X in the logical image, 0x%X bytes", self.fl_off,
                 self.fl_len)
        log.info("  firmware sha256  %s", self.firmware_sha256)
        if self.kind == "official-v15":
            if self.file_sha256 == V15_FILE_SHA256:
                log.info("  file sha256 = the genuine V15 FM-1.fwsc (informational)")
            else:
                log.warning("  NOTE: firmware region is V15, but the file differs from the "
                            "genuine FM-1.fwsc (sha256 %s); informational only", V15_FILE_SHA256)
        log.debug("  package head sha256 %s", self.head_sha256)

    def event_fields(self):
        """What the package event reports (no image bytes)."""
        return {"name": self.path.name, "identity": self.identity, "kind": self.kind,
                "description": self.description, "file_size": self.file_size,
                "file_sha256": self.file_sha256, "firmware_sha256": self.firmware_sha256,
                "firmware_start": "0x%X" % FW_START, "firmware_end": "0x%X" % (FW_END - 1),
                "firmware_size": FW_LEN,
                "genuine_v15_file": self.file_sha256 == V15_FILE_SHA256}

    def check_writable(self, i_know=False):
        """Refuse packages that must not be written."""
        if self.firmware.count(0xFF) == len(self.firmware) or not any(self.firmware):
            raise SafetyError("the firmware region of %s is blank; refusing" % self.path.name,
                              event_code="PACKAGE_BLANK")
        if self.kind == "damaged-v15":
            raise SafetyError(
                "%s says it is official V15 but its firmware hash is %s, not %s. "
                "Download FM-1.fwsc again from M-VAVE; refusing to write it."
                % (self.path.name, self.firmware_sha256, V15_FLASH_FW_SHA256),
                event_code="PACKAGE_DAMAGED_V15")
        if self.kind == "unknown":
            if not i_know:
                raise SafetyError("%s has no recognisable FM-1 identity (%r); refusing "
                                  "(--i-know overrides)" % (self.path.name, self.identity),
                                  event_code="PACKAGE_UNKNOWN_IDENTITY")
            log.warning("WARNING: unrecognised package written because of --i-know")
        if self.kind == "official-other":
            log.warning("NOTE: this package cannot be hash-verified; official V15 is the "
                        "recommended path.")


# --------------------------------------------------------------------------
# Device discovery (never opens or touches any disk)
# --------------------------------------------------------------------------

WIN_DISK_QUERY = ("Get-CimInstance Win32_DiskDrive | "
                  "Select-Object Index,Model,InterfaceType,PNPDeviceID,Size | "
                  "ConvertTo-Json -Compress")
WIN_PATH_RE = re.compile(r"^\\\\\.\\PhysicalDrive\d+$", re.IGNORECASE)
LINUX_PATH_RE = re.compile(r"^/dev/sg\d+$")


def parse_windows_disks(json_text: str):
    """Candidates from `Get-CimInstance Win32_DiskDrive | ... | ConvertTo-Json`."""
    text = (json_text or "").strip()
    if not text:
        return []
    items = json.loads(text)
    if isinstance(items, dict):
        items = [items]
    found = []
    for d in items:
        model = str(d.get("Model") or "")
        iface = str(d.get("InterfaceType") or "")
        index = d.get("Index")
        if index is None:
            continue
        if DEVICE_MODEL.lower() in model.lower() and iface.upper() == "USB":
            found.append({"path": "\\\\.\\PhysicalDrive%d" % int(index),
                          "description": "%s (disk %d, %s)" % (model, int(index), iface)})
    return found


def find_windows(run=subprocess.run):
    try:
        res = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", WIN_DISK_QUERY],
                  capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise UnbrickError("could not run PowerShell to list disks: %s" % e)
    if res.returncode != 0:
        raise UnbrickError("Get-CimInstance Win32_DiskDrive failed: %s" % res.stderr.strip())
    return parse_windows_disks(res.stdout)


def _read_attr(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def scan_linux_sysfs(sysroot="/sys", devroot="/dev"):
    """Candidates from /sys/class/scsi_generic/sgN/device/{vendor,model}."""
    base = Path(sysroot) / "class" / "scsi_generic"
    found = []
    if not base.is_dir():
        return found
    for entry in sorted(base.iterdir(), key=lambda p: (len(p.name), p.name)):
        if not re.match(r"^sg\d+$", entry.name):
            continue
        dev = entry / "device"
        vendor = _read_attr(dev / "vendor")
        model = _read_attr(dev / "model")
        if vendor.upper() != DEVICE_VENDOR or not model.upper().startswith(DEVICE_PRODUCT):
            continue
        real = os.path.realpath(str(dev))
        if "/usb" not in real.replace("\\", "/"):
            continue
        found.append({"path": str(Path(devroot) / entry.name),
                      "description": "%s %s (%s, USB)" % (vendor, model, entry.name)})
    return found


def find_devices():
    if sys.platform == "win32":
        return find_windows()
    if sys.platform.startswith("linux"):
        if not Path("/sys/class/scsi_generic").is_dir():
            log.info("No /sys/class/scsi_generic: load the SCSI generic driver with "
                     "`sudo modprobe sg` and try again.")
        return scan_linux_sysfs()
    raise UnbrickError(MACOS_MESSAGE, EXIT_UNSUPPORTED)


def select_device(explicit, candidates, i_know=False):
    """Pick the one device to use; refuse anything ambiguous."""
    if explicit:
        if sys.platform == "win32" and not WIN_PATH_RE.match(explicit):
            raise SafetyError("--device must look like \\\\.\\PhysicalDriveN on Windows")
        if sys.platform.startswith("linux") and not LINUX_PATH_RE.match(explicit):
            raise SafetyError("--device must look like /dev/sgN on Linux")
        if candidates is not None and explicit not in [c["path"] for c in candidates]:
            if not i_know:
                raise SafetyError(
                    "%s is not listed as a %s USB disk; refusing (--i-know overrides, "
                    "the device must still answer as %s)" % (explicit, DEVICE_MODEL, DEVICE_VENDOR))
            log.warning("WARNING: %s not in the scan, used because of --i-know", explicit)
        return explicit
    if not candidates:
        raise UnbrickError(
            "No %s device found. Check: the FM-1 is switched on and plugged in directly with "
            "a data cable; it shows a black screen; on Windows run from an administrator "
            "PowerShell; on Linux run with sudo (and `sudo modprobe sg`). If it never appears "
            "as %s, see the FM-1 Transporter (README)." % (DEVICE_MODEL, DEVICE_MODEL),
            event_code="NO_DEVICE")
    if len(candidates) > 1:
        listing = "\n".join("  %s  %s" % (c["path"], c["description"]) for c in candidates)
        raise SafetyError("%d candidate devices found; refusing to guess. Unplug the others "
                          "or pass --device explicitly:\n%s" % (len(candidates), listing),
                          event_code="MULTIPLE_DEVICES")
    return candidates[0]["path"]


# --------------------------------------------------------------------------
# The device: jl-uboot-tool's loader protocol, or a mock
# --------------------------------------------------------------------------

class Fm1Device:
    """A WL82 in UBOOT mode with the USB loader running.

    `ldr` offers jl-uboot-tool's JL_LoaderV2 methods: chip_key(),
    online_device(), flash_read(), flash_write(), flash_erase_sector(),
    flash_erase_block(). Mutating access goes only through erase_range()
    and write_range() below, which enforce the 0x4000 rule.
    """

    def __init__(self, path, ldr, vendor=DEVICE_VENDOR, product=DEVICE_PRODUCT,
                 io_max=512, closer=None, mock=False, retained_ram=None):
        self.path, self.ldr, self.vendor, self.product = path, ldr, vendor, product
        self.retained_ram = retained_ram
        self.session = None        # this run's folder (session_dir)
        self.io_max = io_max
        self.mock = mock
        self._closer = closer

    def chip_key(self):
        return self.ldr.chip_key()

    def flash_id(self):
        return self.ldr.online_device()["id"]

    def online_type(self):
        return self.ldr.online_device()["type"]

    def close(self):
        if self._closer:
            self._closer()
            self._closer = None


def read_flash(dev: Fm1Device, addr: int, length: int, label="Reading", phase=None) -> bytes:
    if addr < 0 or length < 0 or addr + length > FLASH_SIZE:
        raise SafetyError("read 0x%X+0x%X is outside the flash" % (addr, length))
    out = bytearray()
    prog = Progress(label, length, phase) if label else None   # label None: no progress line
    try:
        while length > 0:
            n = min(length, dev.io_max)
            data = dev.ldr.flash_read(addr, n)
            if len(data) != n:
                raise UnbrickError("short read at 0x%X: %d of %d bytes" % (addr, len(data), n))
            out += data
            addr += n
            length -= n
            if prog:
                prog.update(n)
    finally:
        if prog:
            prog.close()
    return bytes(out)


def _check_mutable(addr: int, length: int):
    """The single gate for every erase and write."""
    if length <= 0:
        raise SafetyError("empty erase/write")
    if addr < PROTECTED_END:
        raise SafetyError("refusing to touch 0x%X: nothing below 0x%X is ever erased or "
                          "written (boot head)" % (addr, PROTECTED_END))
    if addr + length > FLASH_SIZE:
        raise SafetyError("0x%X+0x%X runs past the end of the flash" % (addr, length))
    for start, end, what in NEVER_TOUCH:
        if addr < end and addr + length > start:
            raise SafetyError("refusing to touch 0x%X+0x%X: 0x%X..0x%X (%s) is never erased "
                              "or written" % (addr, length, start, end - 1, what))


def _check_repairable(addr: int, length: int):
    """The gate of repair-settings: whole 4 KiB sectors inside the two settings windows."""
    _check_mutable(addr, length)
    if addr % SECTOR_SIZE or length % SECTOR_SIZE:
        raise SafetyError("repair 0x%X+0x%X is not whole 4 KiB sectors; refusing" % (addr, length))
    if not any(start <= addr and addr + length <= end for start, end in REPAIR_WINDOWS):
        raise SafetyError("refusing to repair 0x%X+0x%X: only 0x%X..0x%X and 0x%X..0x%X are "
                          "settings areas" % (addr, length, VM_START, VM_END - 1, GLOBALS_START,
                                              GLOBALS_END - 1))


def erase_range(dev: Fm1Device, addr: int, length: int):
    """Erase exactly [addr, addr+length), like jl-uboot-tool's flash_erase()
    (64 KiB blocks where aligned, 4 KiB sectors elsewhere), but only on
    sector boundaries and never below 0x4000."""
    _check_mutable(addr, length)
    if addr % SECTOR_SIZE or length % SECTOR_SIZE:
        raise SafetyError("erase 0x%X+0x%X is not 4 KiB aligned; refusing (it would "
                          "destroy neighbouring data)" % (addr, length))
    end = addr + length
    prog = Progress("Erasing", length, "erase")
    try:
        while addr < end:
            if addr % BLOCK_SIZE == 0 and end - addr >= BLOCK_SIZE:
                _check_mutable(addr, BLOCK_SIZE)
                dev.ldr.flash_erase_block(addr)
                step = BLOCK_SIZE
            else:
                _check_mutable(addr, SECTOR_SIZE)
                dev.ldr.flash_erase_sector(addr)
                step = SECTOR_SIZE
            addr += step
            prog.update(step)
    finally:
        prog.close()


def write_range(dev: Fm1Device, addr: int, data: bytes):
    """Program already-erased flash in io_max chunks (as jl-uboot-tool does)."""
    _check_mutable(addr, len(data))
    prog = Progress("Writing", len(data), "write")
    try:
        off = 0
        while off < len(data):
            chunk = data[off:off + dev.io_max]
            _check_mutable(addr + off, len(chunk))
            dev.ldr.flash_write(addr + off, chunk)
            off += len(chunk)
            prog.update(len(chunk))
    finally:
        prog.close()


class MockUboot:
    """Stands in for jl-uboot-tool's loader on an FM-1 (JL_LoaderV2 API).

    The flash is a bytearray with NOR semantics: erase sets 0xFF in whole
    4 KiB sectors / 64 KiB blocks, programming can only clear bits.
    Fault injection: flaky_read=N corrupts one byte of the N-th read call,
    flaky_write=N corrupts one byte of the N-th write call, bad_cell=ADDR
    makes that byte never program correctly.
    """

    def __init__(self, image=None, chip_key=EXPECTED_CHIP_KEY, flash_id=EXPECTED_FLASH_ID,
                 io_max=512, flaky_read=None, flaky_write=None, bad_cell=None):
        if image is None:
            image = synthetic_flash()
        if len(image) != FLASH_SIZE:
            raise ValueError("mock image must be exactly 0x%X bytes" % FLASH_SIZE)
        self.flash = bytearray(image)
        self.key, self.id, self.io_max = chip_key, flash_id, io_max
        self.flaky_read, self.flaky_write, self.bad_cell = flaky_read, flaky_write, bad_cell
        self.reads = 0
        self.writes = 0
        self.ops = []          # ("erase_sector"|"erase_block"|"write", addr, length)

    def chip_key(self, arg=0xAC6900):
        return self.key

    def online_device(self):
        return {"type": 0x03, "id": self.id}

    def read_id(self):
        return self.id

    def usb_buffer_size(self):
        return self.io_max

    def flash_read(self, addr, length):
        self.reads += 1
        data = bytearray(self.flash[addr:addr + length])
        if self.flaky_read is not None and self.reads == self.flaky_read and data:
            data[len(data) // 2] ^= 0x5A
        return bytes(data)

    def flash_erase_sector(self, addr):
        assert addr % SECTOR_SIZE == 0, "unaligned sector erase"
        self.ops.append(("erase_sector", addr, SECTOR_SIZE))
        self.flash[addr:addr + SECTOR_SIZE] = b"\xff" * SECTOR_SIZE

    def flash_erase_block(self, addr):
        assert addr % BLOCK_SIZE == 0, "unaligned block erase"
        self.ops.append(("erase_block", addr, BLOCK_SIZE))
        self.flash[addr:addr + BLOCK_SIZE] = b"\xff" * BLOCK_SIZE
        return 0

    def flash_write(self, addr, data):
        assert len(data) <= self.io_max, "write larger than the USB buffer"
        assert addr + len(data) <= FLASH_SIZE
        self.writes += 1
        self.ops.append(("write", addr, len(data)))
        data = bytearray(data)
        if self.flaky_write is not None and self.writes == self.flaky_write:
            data[0] ^= 0x01
        for i, b in enumerate(data):
            a = addr + i
            if a == self.bad_cell:
                b ^= 0x80
            self.flash[a] &= b

    def flash_erase_chip(self):
        raise AssertionError("erase chip must never be used")

    def lowest_touched(self):
        return min((op[1] for op in self.ops), default=None)


def synthetic_flash(seed=b"fm1-mock") -> bytes:
    """A deterministic 'bricked' flash image for dry runs."""
    out = bytearray()
    counter = 0
    while len(out) < FW_END:
        out += hashlib.sha256(seed + counter.to_bytes(4, "little")).digest()
        counter += 1
    out = out[:FW_END] + b"\xff" * (FLASH_SIZE - FW_END)
    return bytes(out)


def mock_device(args) -> Fm1Device:
    image = None
    if getattr(args, "mock_image", None):
        image = Path(args.mock_image).read_bytes()
    flaky = getattr(args, "mock_flaky", None)
    mock = MockUboot(image=image,
                     chip_key=getattr(args, "mock_chip_key", None) or EXPECTED_CHIP_KEY,
                     flaky_read=2 if flaky == "read" else None,
                     flaky_write=3 if flaky == "write" else None,
                     bad_cell=0x50000 if flaky == "write-always" else None)
    log.info("DRY RUN: using a MOCK FM-1 (no real device is opened, nothing is written to "
             "hardware)")
    return Fm1Device("mock://fm1-uboot", mock, io_max=mock.usb_buffer_size(), mock=True)


# --------------------------------------------------------------------------
# jl-uboot-tool: setup, import, real device
# --------------------------------------------------------------------------

def _git(*args, cwd=None):
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True)


def jlub_commit(jl_dir: Path):
    """The commit of an existing checkout (git) or extracted zip (marker), or None."""
    if (jl_dir / ".git").exists() and shutil.which("git"):
        res = _git("-C", str(jl_dir), "rev-parse", "HEAD")
        if res.returncode == 0:
            return res.stdout.strip()
    marker = jl_dir / JLUB_MARKER
    if marker.is_file():
        return marker.read_text().strip()
    return None


def verify_jlub(jl_dir: Path):
    """Raise unless jl_dir is jl-uboot-tool at the pinned commit with an intact wl82 loader."""
    if not jl_dir.is_dir():
        raise UnbrickError("jl-uboot-tool not found at %s; run `python fm1_unbrick.py setup` "
                           "first" % jl_dir, event_code="JLUB_MISSING")
    commit = jlub_commit(jl_dir)
    if commit != JLUB_COMMIT:
        raise UnbrickError("%s is at commit %s, expected %s; delete the folder and run setup "
                           "again" % (jl_dir, commit, JLUB_COMMIT), event_code="JLUB_COMMIT")
    for rel in ("jltech/uboot.py", "jltech/cipher.py", "scsiio/__init__.py",
                "data/chips.yaml", "data/usb-loaders.yaml", WL82_LOADER_REL):
        if not (jl_dir / rel).is_file():
            raise UnbrickError("%s is incomplete: %s missing; run setup again" % (jl_dir, rel),
                               event_code="JLUB_INCOMPLETE")
    got = sha256((jl_dir / WL82_LOADER_REL).read_bytes())
    if got != WL82_LOADER_SHA256:
        raise UnbrickError("the wl82 loader blob has sha256 %s, expected %s; refusing"
                           % (got, WL82_LOADER_SHA256), event_code="LOADER_HASH")
    return commit


def cmd_setup(args):
    jl_dir = Path(args.jl_dir)
    source = args.source or JLUB_REPO
    if jl_dir.exists():
        log.info("Found %s, verifying it", jl_dir)
    elif args.dry_run:
        log.info("DRY RUN: would fetch %s at %s into %s", source, JLUB_COMMIT, jl_dir)
    elif shutil.which("git"):
        log.info("Cloning %s into %s", source, jl_dir)
        res = _git("clone", "--quiet", source, str(jl_dir))
        if res.returncode != 0:
            raise UnbrickError("git clone failed: %s" % res.stderr.strip())
        res = _git("-C", str(jl_dir), "checkout", "--quiet", JLUB_COMMIT)
        if res.returncode != 0:
            raise UnbrickError("git checkout %s failed: %s" % (JLUB_COMMIT, res.stderr.strip()))
    else:
        if args.source:
            raise UnbrickError("--source needs git")
        log.info("git not found; downloading %s", JLUB_ZIP_URL)
        with urllib.request.urlopen(JLUB_ZIP_URL, timeout=120) as r:
            blob = r.read()
        log.debug("zip sha256 %s", sha256(blob))
        tmp = jl_dir.with_name(jl_dir.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(str(tmp))
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            z.extractall(str(tmp))
        inner = [p for p in tmp.iterdir() if p.is_dir()]
        if len(inner) != 1:
            raise UnbrickError("unexpected zip layout from GitHub")
        inner[0].rename(jl_dir)
        shutil.rmtree(str(tmp))
        (jl_dir / JLUB_MARKER).write_text(JLUB_COMMIT + "\n")

    if args.dry_run and not jl_dir.exists():
        log.info("DRY RUN: would run %s -m pip install -r %s", sys.executable,
                 jl_dir / "requirements.txt")
        install_rtmidi(args)
        return EXIT_OK
    commit = verify_jlub(jl_dir)
    log.info("jl-uboot-tool OK: commit %s, wl82 loader sha256 %s", commit, WL82_LOADER_SHA256)

    req = jl_dir / "requirements.txt"
    pip = [sys.executable, "-m", "pip", "install", "-r", str(req)]
    if args.no_pip:
        log.info("Skipping pip (--no-pip). Requirements: %s", req)
    elif args.dry_run:
        log.info("DRY RUN: would run %s", " ".join(pip))
    else:
        log.info("Installing jl-uboot-tool's requirements: %s", " ".join(pip))
        res = subprocess.run(pip)
        if res.returncode != 0:
            raise UnbrickError("pip install failed (exit %d)" % res.returncode)
    install_rtmidi(args)
    if not platform_supported():
        log.info("Note: %s", MACOS_MESSAGE)
    log.info("Setup complete.")
    return EXIT_OK


def install_rtmidi(args):
    """python-rtmidi, for `softkey` only: a failure is a warning, not a failed setup."""
    pip = [sys.executable, "-m", "pip", "install", "python-rtmidi"]
    if args.no_pip:
        log.info("Skipping pip (--no-pip) for python-rtmidi too (only `softkey` needs it)")
    elif args.dry_run:
        log.info("DRY RUN: would run %s", " ".join(pip))
    else:
        log.info("Installing python-rtmidi (for `softkey`): %s", " ".join(pip))
        res = subprocess.run(pip)
        if res.returncode != 0:
            log.warning("WARNING: python-rtmidi could not be installed (exit %d). Everything "
                        "but `softkey` works without it%s.", res.returncode,
                        "; on Linux `softkey` uses raw ALSA instead"
                        if sys.platform.startswith("linux") else "")


def load_jlub(jl_dir: Path):
    """Import jl-uboot-tool's modules from its folder (nothing is vendored)."""
    path = str(Path(jl_dir).resolve())
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        import yaml
        from jltech.uboot import JL_UBOOT, JL_LoaderV2
        from jltech.cipher import cipher_bytes, jl_crc_cipher
        from scsiio.common import SCSIException
    except ImportError as e:
        raise UnbrickError("cannot import jl-uboot-tool (%s); run `python fm1_unbrick.py "
                           "setup`" % e, event_code="DEPENDENCY_MISSING")
    try:
        from scsiio import SCSIDev
    except ImportError:
        SCSIDev = None
    return types.SimpleNamespace(yaml=yaml, JL_UBOOT=JL_UBOOT, JL_LoaderV2=JL_LoaderV2,
                                 cipher_bytes=cipher_bytes, jl_crc_cipher=jl_crc_cipher,
                                 SCSIException=SCSIException, SCSIDev=SCSIDev,
                                 dataroot=Path(path) / "data")


def scsi_inquiry(scsi):
    data = bytearray(36)
    scsi.execute(b"\x12\x00\x00" + len(data).to_bytes(2, "big") + b"\x00", None, data)
    return (data[8:16].decode("ascii", "replace").strip(),
            data[16:32].decode("ascii", "replace").strip(),
            data[32:36].decode("ascii", "replace").strip())


def upload_loader(jl, scsi, chipname="wl82"):
    """Upload and start the USB loader, exactly as jluboottool.py does."""
    chips = jl.yaml.safe_load((jl.dataroot / "chips.yaml").read_text())
    loaders = jl.yaml.safe_load((jl.dataroot / "usb-loaders.yaml").read_text())
    if chipname not in chips or chipname not in loaders:
        raise UnbrickError("jl-uboot-tool has no USB loader for %r" % chipname)
    spec, chipspec = loaders[chipname], chips[chipname]
    blob = (jl.dataroot / spec["file"]).read_bytes()
    if chipname == "wl82" and sha256(blob) != WL82_LOADER_SHA256:
        raise UnbrickError("unexpected wl82 loader blob; refusing")
    block_size = spec.get("blocksize", 512)
    cipher = spec.get("encryption", "none")
    quirks = (chipspec.get("uboot1.00") or {}).get("quirks") or {}
    rom_crypt = quirks.get("memory-rw-mengli-crypt") is True
    crypt = (not rom_crypt and cipher == "MengLi") or (rom_crypt and cipher != "MengLi")
    if cipher == "RxGp":
        raise UnbrickError("RxGp loaders are not supported here")
    uboot = jl.JL_UBOOT(scsi)
    address = spec["address"]
    log.debug("loader %s: %d bytes at 0x%X, block %d, cipher %s, host scramble %s",
              spec["file"], len(blob), address, block_size, cipher, crypt)
    for off in range(0, len(blob), block_size):
        block = blob[off:off + block_size]
        if crypt:
            block = jl.cipher_bytes(jl.jl_crc_cipher, block)
        uboot.mem_write(address + off, block)
    try:
        uboot.mem_jump(spec["address"], LOADER_TARGET_SPI_NOR)
    except jl.SCSIException as e:
        raise UnbrickError("failed to start the USB loader: %s" % e)
    log.info("USB loader uploaded and running (%s, 0x%X)", spec["file"], spec["address"])


def read_retained_ram(jl, scsi):
    """Read the retained RAM from the ROM, before the loader is uploaded.

    Diagnostic only (nothing is written): a failure is logged and ignored.
    The WL82 ROM returns memory MengLi-scrambled (chips.yaml,
    memory-rw-mengli-crypt), as it expects the loader upload.
    """
    try:
        data = bytes(jl.JL_UBOOT(scsi).mem_read(RETAINED_RAM_ADDR, RETAINED_RAM_LEN))
        if len(data) != RETAINED_RAM_LEN:
            raise UnbrickError("short read: %d bytes" % len(data))
        return bytes(jl.cipher_bytes(jl.jl_crc_cipher, data))
    except Exception as e:
        log.warning("Could not read the retained RAM (%s); continuing without it", e)
        return None


_session = None                # the folder of the current main() run, for the log copy


def session_dir(dev: Fm1Device, outdir: Path) -> Path:
    """This run's folder under outdir: everything worth sending lands here.

    The file names are those of Melodee's fm1-recover (retained-ram.bin,
    full-flash-before.bin, backup.json, recovery-result.json), plus unbrick.log.
    """
    global _session
    if dev.session is None:
        outdir.mkdir(parents=True, exist_ok=True)
        stem = "fm1-recovery-" + timestamp() + ("-dryrun" if dev.mock else "")
        dev.session = unique_path(outdir, stem, "")
        dev.session.mkdir()
        _session = dev.session
    return dev.session


def durable_write(path: Path, data: bytes):
    with open(str(path), "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def write_report(dev: Fm1Device, name: str, report: dict):
    durable_write(dev.session / name, (json.dumps(report, indent=2) + "\n").encode())


def save_retained_ram(dev: Fm1Device, outdir: Path):
    """Save what read_retained_ram() got, for whoever investigates the brick."""
    data = dev.retained_ram
    if data is None:
        return None
    try:
        path = session_dir(dev, outdir) / "retained-ram.bin"
        durable_write(path, data)
    except OSError as e:
        log.warning("Could not save the retained RAM (%s); continuing", e)
        return None
    dev.retained_ram = None
    log.info("Retained RAM 0x%X+0x%X saved: %s", RETAINED_RAM_ADDR, RETAINED_RAM_LEN, path)
    log.info("  sha256 %s", sha256(data))
    words = struct.unpack_from("<%dI" % len(MELODEE_CRASH_FIELDS), data)
    if words[0] == MELODEE_CRASH_MAGIC:
        log.info("  Melodee crash record: %s", " ".join(
            "%s=%s" % (n, v if n in ("count", "vec", "uptime_ms", "early") else "0x%08X" % v)
            for n, v in zip(MELODEE_CRASH_FIELDS[1:], words[1:])))
    else:
        log.info("  no Melodee crash record (no CRSH magic)")
    return path


def open_real_device(path, jl, scsi=None, wait=10.0) -> Fm1Device:
    if scsi is None:
        if jl.SCSIDev is None:
            raise UnbrickError(MACOS_MESSAGE, EXIT_UNSUPPORTED)
        deadline = time.monotonic() + wait
        while True:
            try:
                scsi = jl.SCSIDev(path)
                break
            except PermissionError:
                raise UnbrickError("permission denied opening %s: run from an administrator "
                                   "PowerShell (Windows) or with sudo (Linux)" % path)
            except OSError as e:
                if time.monotonic() > deadline:
                    raise UnbrickError("cannot open %s: %s" % (path, e))
                time.sleep(0.5)
    try:
        vendor, product, rev = scsi_inquiry(scsi)
        log.info("Device      %s: %s %s (%s)", path, vendor, product, rev)
        if vendor.upper() != DEVICE_VENDOR:
            raise SafetyError("%s answers as %r, not %s; this is not an FM-1 in UBOOT mode. "
                              "Not touching it." % (path, vendor, DEVICE_VENDOR))
        retained = None
        if product == DEVICE_PRODUCT:
            retained = read_retained_ram(jl, scsi)
            upload_loader(jl, scsi, vendor.lower())
        else:
            log.info("Device does not report %s; checking whether the loader already runs",
                     DEVICE_PRODUCT)
            try:
                jl.JL_LoaderV2(scsi).online_device()
            except Exception as e:
                raise SafetyError("%s reports %r and does not answer the loader protocol "
                                  "(%s); refusing" % (path, product, e))
        ldr = jl.JL_LoaderV2(scsi)
        try:
            io_max = int(ldr.usb_buffer_size())
        except Exception:
            io_max = 512
        if not 16 <= io_max <= 0xFFFF:
            io_max = 512
        return Fm1Device(path, ldr, vendor, product, io_max, closer=scsi.close,
                         retained_ram=retained)
    except BaseException:
        scsi.close()
        raise


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

def ensure_platform(args):
    if args.dry_run or platform_supported():
        return
    raise UnbrickError(MACOS_MESSAGE, EXIT_UNSUPPORTED)


def check_privileges(path):
    if sys.platform == "win32":
        try:
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                raise UnbrickError("run this from an administrator PowerShell (right-click "
                                   "Start > Terminal (Admin))")
        except (AttributeError, OSError):
            pass
    elif sys.platform.startswith("linux") and os.path.exists(path):
        if not os.access(path, os.R_OK | os.W_OK):
            raise UnbrickError("no read/write access to %s: run with sudo" % path)


def connect(args) -> Fm1Device:
    """find + open + loader. Real hardware unless --dry-run."""
    step_started("bootloader", "Looking for bootloader %s" % DEVICE_MODEL)
    if args.dry_run:
        dev = mock_device(args)
        step_succeeded("bootloader", count=1, model=DEVICE_MODEL, mock=True)
        step_started("chip", "Cargador USB, chip key y Flash ID")
        return dev
    ensure_platform(args)
    jl_dir = Path(args.jl_dir)
    verify_jlub(jl_dir)
    jl = load_jlub(jl_dir)
    candidates = find_devices()
    for c in candidates:
        log.info("Candidate   %s  %s", c["path"], c["description"])
    path = select_device(args.device, candidates, args.i_know)
    step_succeeded("bootloader", count=len(candidates), model=DEVICE_MODEL)
    check_privileges(path)
    step_started("chip", "Cargador USB, chip key y Flash ID")
    return open_real_device(path, jl)


def check_identity(dev: Fm1Device, i_know=False):
    key = dev.chip_key()
    ondev = dev.ldr.online_device()
    fid = ondev["id"]
    log.info("Chip key    0x%04X (expected 0x%04X)", key, EXPECTED_CHIP_KEY)
    log.info("Flash ID    0x%06X (expected 0x%06X, 1 MiB), type 0x%02X", fid,
             EXPECTED_FLASH_ID, ondev.get("type", 0))
    log.info("USB buffer  %d bytes", dev.io_max)
    problems = []
    if key != EXPECTED_CHIP_KEY:
        problems.append("chip key 0x%04X is not 0x%04X" % (key, EXPECTED_CHIP_KEY))
    if fid != EXPECTED_FLASH_ID:
        problems.append("flash ID 0x%06X is not 0x%06X" % (fid, EXPECTED_FLASH_ID))
    if problems:
        if not i_know:
            raise SafetyError("this does not look like an FM-1: %s. Stopping; nothing was "
                              "written (--i-know overrides)." % "; ".join(problems),
                              event_code="CHIP_MISMATCH")
        log.warning("WARNING: %s; continuing because of --i-know", "; ".join(problems))
    step_succeeded("chip", chip_key="0x%04X" % key, flash_id="0x%06X" % fid,
                   expected_chip_key="0x%04X" % EXPECTED_CHIP_KEY,
                   expected_flash_id="0x%06X" % EXPECTED_FLASH_ID, usb_buffer=dev.io_max)


MARKER_PATTERNS = [
    (re.compile(rb"\[FELUCCA\]"), "Felucca-family config tag"),
    (re.compile(rb"FELUCCA-LOADER-\d+"), "Felucca-family loader"),
    (re.compile(rb"FM-1_\d{3}"), "FM-1 package identity string"),
]


def analyse_dump(data: bytes, packages=()):
    """Report what is in a full flash dump."""
    head, fw = data[:PROTECTED_END], data[FW_START:FW_END]
    fw_sha = sha256(fw)
    log.info("Analysis")
    log.info("  boot head 0x0000..0x3FFF sha256  %s", sha256(head))
    log.info("  firmware  0x4000..0x92FFF sha256 %s", fw_sha)
    if fw_sha == V15_FLASH_FW_SHA256:
        log.info("  firmware region = official M-VAVE V15 (exact)")
    sectors = FW_LEN // SECTOR_SIZE
    blank = sum(1 for i in range(sectors)
                if fw[i * SECTOR_SIZE:(i + 1) * SECTOR_SIZE] == b"\xff" * SECTOR_SIZE)
    log.info("  firmware sectors erased (all 0xFF): %d of %d%s", blank, sectors,
             "  <- looks like an interrupted install" if blank > 8 else "")
    for rx, what in MARKER_PATTERNS:
        hits = [(m.start(), m.group(0).decode("ascii", "replace")) for m in rx.finditer(data)]
        if hits:
            log.info("  marker: %s %s", what,
                     ", ".join("%r at 0x%05X" % (t, o) for o, t in hits[:4]))
    for pkg in packages:
        same = sum(1 for i in range(sectors)
                   if fw[i * SECTOR_SIZE:(i + 1) * SECTOR_SIZE]
                   == pkg.firmware[i * SECTOR_SIZE:(i + 1) * SECTOR_SIZE])
        log.info("  vs %s (%s): firmware sectors identical %d/%d%s", pkg.path.name,
                 pkg.identity, same, sectors, "  = exact copy" if same == sectors else "")
        if pkg.kind == "official-v15":
            if head == pkg.head:
                log.info("  boot head matches the official V15 package's head")
            else:
                log.warning("  boot head DIFFERS from the official V15 package's head. This "
                            "tool never writes the boot head; see README, Troubleshooting.")
        else:
            log.info("  boot head %s %s's head (informational: third-party packages may "
                     "carry their own head)", "matches" if head == pkg.head else "differs from",
                     pkg.path.name)


def take_backup(dev: Fm1Device, outdir: Path, packages=()):
    """Read the whole flash twice, compare, save, re-check the saved file."""
    outdir.mkdir(parents=True, exist_ok=True)
    step_started("backup", "Full flash backup (two reads)")
    log.info("Backing up the whole flash (0x0..0x%X), twice", FLASH_SIZE - 1)
    a = read_flash(dev, 0, FLASH_SIZE, "Backup read 1/2", "read1")
    log.info("  read 1 sha256 %s", sha256(a))
    b = read_flash(dev, 0, FLASH_SIZE, "Backup read 2/2", "read2")
    log.info("  read 2 sha256 %s", sha256(b))
    if a != b:
        diffs = [i for i in range(FLASH_SIZE) if a[i] != b[i]]
        raise UnbrickError(
            "the two backup reads DIFFER (%d bytes, first at 0x%X): the USB connection is "
            "not reliable. Nothing was written. Use another (data) cable or port, plug the "
            "FM-1 in directly (no hub) and try again." % (len(diffs), diffs[0]),
            event_code="BACKUP_READS_DIFFER")
    path = session_dir(dev, outdir) / "full-flash-before.bin"
    durable_write(path, a)
    if path.read_bytes() != a:
        raise UnbrickError("the backup file %s does not read back correctly; check the disk"
                           % path, event_code="BACKUP_FILE_BAD")
    log.info("Backup OK   %s", path)
    log.info("  sha256 %s (both reads identical)", sha256(a))
    analyse_dump(a, packages)
    ram = dev.session / "retained-ram.bin"
    sectors = range(0, FW_LEN, SECTOR_SIZE)
    dev.report = {
        "tool": "MvaveFM1Unbricker fm1_unbrick.py " + TOOL_VERSION,
        "device": {"vendor": dev.vendor, "product": dev.product},
        "chip_key": "0x%04x" % dev.chip_key(),
        "flash_id": "0x%06x" % dev.flash_id(),
        "full_backup": path.name,
        "backup_sha256": sha256(a),
        "retained_ram": ram.name if ram.is_file() else None,
        "retained_ram_sha256": sha256(ram.read_bytes()) if ram.is_file() else None,
        "packages": [{
            "package": pkg.path.name, "product": pkg.identity, "kind": pkg.kind,
            "package_sha256": pkg.file_sha256,
            "application_sectors_differ": sum(
                1 for i in sectors if a[FW_START + i:FW_START + i + SECTOR_SIZE]
                != pkg.firmware[i:i + SECTOR_SIZE]),
        } for pkg in packages],
    }
    write_report(dev, "backup.json", dev.report)
    step_succeeded("backup", path=str(path.resolve()), size=len(a), sha256=sha256(a),
                   reads_identical=True, session=str(dev.session.resolve()))
    return path, a


def confirm(args, lines, phrase, summary=None):
    for line in lines:
        log.info(line)
    if args.yes:
        log.info("Confirmation skipped (--yes)")
        return
    if events_on():
        # The front end shows `summary`, asks for the phrase and sends back one line;
        # it is compared here exactly as the typed answer is.
        emit("confirmation_required", id=phrase.lower(), phrase=phrase, summary=summary or {})
        line = sys.stdin.readline() if sys.stdin is not None else ""
        answer = line.rstrip("\r\n")
        log.debug("confirmation answer %r", answer)
        accepted = answer.strip() == phrase
        emit("confirmation_result", id=phrase.lower(), accepted=accepted)
        if not accepted:
            raise UnbrickError("cancelled; nothing was written", event_code="CANCELLED")
        return
    if not stdin_is_tty():
        raise UnbrickError("no terminal to confirm on; re-run interactively or pass --yes")
    answer = ask("Type %s to continue (anything else cancels): " % phrase)
    log.debug("confirmation answer %r", answer)
    if answer.strip() != phrase:
        raise UnbrickError("cancelled; nothing was written")


def stdin_is_tty():
    return sys.stdin is not None and sys.stdin.isatty()


def ask(prompt):
    return input(prompt)


def write_and_verify(dev: Fm1Device, data: bytes):
    """Erase+write FW region once, verify, retry once. Returns True on success."""
    assert len(data) == FW_LEN
    for attempt in (1, 2):
        step_started("write", "Erase and write 0x%X..0x%X" % (FW_START, FW_END - 1),
                     attempt=attempt)
        log.info("Write attempt %d: erase+write 0x%X..0x%X", attempt, FW_START, FW_END - 1)
        erase_range(dev, FW_START, FW_LEN)
        write_range(dev, FW_START, data)
        step_started("verify", "Post-write read and byte-by-byte comparison", attempt=attempt)
        log.info("Verifying: reading back 0x%X bytes from 0x%X", FW_LEN, FW_START)
        back = read_flash(dev, FW_START, FW_LEN, "Verify", "verify")
        log.info("  read-back sha256 %s", sha256(back))
        if back == data:
            log.info("  byte-for-byte identical")
            return True
        diffs = [i for i in range(FW_LEN) if back[i] != data[i]]
        log.error("  MISMATCH: %d bytes differ, first at 0x%X", len(diffs), FW_START + diffs[0])
        if attempt == 1:
            log.info("Retrying the write once")
            emit("warning", step="verify", code="VERIFY_MISMATCH_RETRY",
                 message="read-back mismatch (%d bytes, first at 0x%X); the recovery tool "
                         "retries the write once" % (len(diffs), FW_START + diffs[0]))
    return False


DANGER_TEXT = """
!!! The firmware region could not be verified. !!!
  * Do NOT power off, unplug or restart the FM-1.
  * The boot head (0x0000-0x3FFF) was not touched.
  * Re-run the same restore command (it backs up again and rewrites), if possible
    with another cable or USB port. Your backup: %s
  * If it keeps failing, follow UNBRICK-GUIDE.md (manual write) and ask for help.
"""

DONE_TEXT = """
Done. Now:
  1. Unplug the USB cable.
  2. Switch the FM-1 off.
  3. Wait 5 seconds.
  4. Switch it on.
"""


def mock_rom_disks(args):
    """Dry run: the WL82 disks the mock reports (--mock-rom-disks, default 1)."""
    n = getattr(args, "mock_rom_disks", None)
    return [{"path": "mock://fm1-uboot-%d" % i, "description": DEVICE_MODEL + " (MOCK)"}
            for i in range(1 if n is None else n)]


def cmd_find(args):
    step_started("bootloader", "Looking for bootloader %s" % DEVICE_MODEL)
    if args.dry_run:
        cands = mock_rom_disks(args)
    else:
        ensure_platform(args)
        cands = find_devices()
    if not cands:
        log.info("No %s device found.", DEVICE_MODEL)
    for c in cands:
        log.info("%s  %s", c["path"], c["description"])
    if args.dry_run and not events_on():
        return EXIT_OK
    if args.dry_run:
        select_device(None, cands)          # the same zero/one/several rule as real hardware
    else:
        select_device(args.device, cands, args.i_know)
    log.info("Exactly one device: OK")
    step_succeeded("bootloader", count=len(cands), model=DEVICE_MODEL, mock=bool(args.dry_run))
    return EXIT_OK


def with_device(args, fn, device=None):
    dev = device or connect(args)
    try:
        # Saved by every command: once the loader runs, the ROM read is gone.
        save_retained_ram(dev, Path(getattr(args, "dir", None)
                                    or getattr(args, "backup_dir", None) or "backups"))
        return fn(dev)
    finally:
        dev.close()


def cmd_info(args, device=None):
    def run(dev):
        check_identity(dev, args.i_know)
        log.info("This is an FM-1 in UBOOT mode: OK")
        return EXIT_OK
    return with_device(args, run, device)


def cmd_backup(args, device=None):
    packages = [Package.load(p) for p in (args.package or [])]

    def run(dev):
        check_identity(dev, args.i_know)
        take_backup(dev, Path(args.dir), packages)
        return EXIT_OK
    return with_device(args, run, device)


def cmd_extract(args):
    pkg = Package.load(args.package)
    pkg.log_summary()
    if args.verify_v15 and pkg.kind != "official-v15":
        log.error("NOT the official V15 firmware: expected sha256 %s. Stop here.",
                  V15_FLASH_FW_SHA256)
        return EXIT_FAIL
    if args.verify_v15:
        log.info("V15 hash verified.")
    out = Path(args.out)
    out.write_bytes(pkg.firmware)
    log.info("Wrote %s (0x%X bytes); write it at 0x%X only.", out, len(pkg.firmware), FW_START)
    return EXIT_OK


def load_package_step(args):
    """Load and describe the package as the `package` step (vetting is the caller's)."""
    step_started("package", "Inspecting the package")
    try:
        pkg = Package.load(args.package)
    except SafetyError:
        raise
    except UnbrickError as e:
        raise UnbrickError(str(e), e.code, e.event_code or "PACKAGE_INVALID")
    pkg.log_summary()
    emit("package", **pkg.event_fields())
    return pkg


def cmd_restore(args, device=None):
    log.info("Step 1/6: package")
    pkg = load_package_step(args)
    pkg.check_writable(args.i_know)
    if args.verify_v15 and pkg.kind != "official-v15":
        raise SafetyError("--verify-v15 given but this is not the verified official V15",
                          event_code="NOT_V15")
    catalog = vet_catalog(pkg, args.manifest)
    step_succeeded("package", catalog=catalog["status"], **pkg.event_fields())

    def run(dev):
        log.info("Step 2/6: device")
        check_identity(dev, args.i_know)
        log.info("Step 3/6: backup (mandatory)")
        backup_path, dump = take_backup(dev, Path(args.backup_dir), [pkg])
        if dump[FW_START:FW_END] == pkg.firmware and not args.force_write:
            log.info("The firmware region already holds exactly this firmware; nothing to "
                     "write. (If it still does not boot, the cause is elsewhere: see README, "
                     "Troubleshooting. --force-write rewrites anyway.)")
            # Evidence: both backup reads hold exactly this firmware region.
            for step in ("confirm", "write"):
                step_succeeded(step, skipped=True, reason="already_installed")
            step_succeeded("verify", byte_identical=True, already_installed=True,
                           sha256=pkg.firmware_sha256, size=FW_LEN)
            log.info(DONE_TEXT)
            return EXIT_OK
        log.info("Step 4/6: confirmation")
        step_started("confirm", "Write confirmation")
        confirm(args, [
            "",
            "About to WRITE the FM-1 flash:",
            "  device    %s (%s %s)%s" % (dev.path, dev.vendor, dev.product,
                                          "  [MOCK]" if dev.mock else ""),
            "  package   %s" % pkg.path.name,
            "  identity  %s = %s" % (pkg.identity or "(none)", pkg.description),
            "  size      0x%X bytes (%d)" % (FW_LEN, FW_LEN),
            "  address   0x%X .. 0x%X (boot head 0x0000-0x3FFF is never touched)"
            % (FW_START, FW_END - 1),
            "  backup    %s" % backup_path,
            "",
        ], "WRITE", summary={
            "device": "%s %s%s" % (dev.vendor, dev.product, " [MOCK]" if dev.mock else ""),
            "package": pkg.path.name, "identity": pkg.identity, "kind": pkg.kind,
            "description": pkg.description, "firmware_sha256": pkg.firmware_sha256,
            "size": FW_LEN, "start": "0x%X" % FW_START, "end": "0x%X" % (FW_END - 1),
            "backup": str(Path(backup_path).resolve()),
        })
        step_succeeded("confirm", phrase="WRITE")
        log.info("Step 5/6: write + verify. Do not unplug anything now.")
        emit("write_started", start="0x%X" % FW_START, end="0x%X" % (FW_END - 1), size=FW_LEN)
        def result(verified, error=None):
            write_report(dev, "recovery-result.json", dict(
                dev.report, written_package=pkg.path.name, written_range="0x%x..0x%x"
                % (FW_START, FW_END - 1), firmware_region_verified=verified, error=error))

        def danger(code, message):
            log.error(DANGER_TEXT % backup_path)
            step_failed(_step or "write", code, message, danger=True,
                        danger_text=(DANGER_TEXT % backup_path).strip(),
                        backup=str(Path(backup_path).resolve()),
                        session=str(dev.session.resolve()))
        try:
            ok = write_and_verify(dev, pkg.firmware)
        except KeyboardInterrupt:
            log.error("Interrupted during the write.")
            result(False, "interrupted")
            danger("WRITE_INTERRUPTED", "interrupted during the write")
            return EXIT_DANGER
        except Exception as e:
            log.error("Write failed: %s", e)
            result(False, str(e))
            danger("WRITE_FAILED", "write failed: %s" % e)
            return EXIT_DANGER
        if not ok:
            result(False, "read-back mismatch after two attempts")
            danger("WRITE_NOT_VERIFIED", "read-back mismatch after two attempts")
            return EXIT_DANGER
        result(True)
        log.info("Step 6/6: restored %s (%s), verified byte for byte.", pkg.identity,
                 pkg.description)
        step_succeeded("write", size=FW_LEN)
        step_succeeded("verify", byte_identical=True, sha256=pkg.firmware_sha256, size=FW_LEN,
                       identity=pkg.identity, report=str((dev.session / "recovery-result.json")
                                                         .resolve()))
        log.info(DONE_TEXT)
        return EXIT_OK
    return with_device(args, run, device)


# --------------------------------------------------------------------------
# repair-settings: the settings areas back to factory state
# --------------------------------------------------------------------------
#
# Felucca-family firmwares keep their store at 0x97000..0xDFFFF and stage their
# update loader at 0xE0000..0xE4FFF, inside stock V15's settings store (VM,
# 0x93000..0xE8FFF), and their globals at 0xFC000..0xFEFFF. The installers only
# rewrite 0x4000..0x92FFF, so after going back to stock V15 it reads those
# leftovers as its own settings. The repair: the VM as in a factory dump (only
# its sector 0x93000 holds data), 0xFC000..0xFEFFF erased, everything else as is.

BLANK_SECTOR = b"\xff" * SECTOR_SIZE


def load_factory_dump(path):
    """Read and vet a clean factory dump; return its bytes."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise UnbrickError("cannot read the factory dump %s: %s" % (path, e))
    problems = []
    if len(data) != FLASH_SIZE:
        problems.append("it is %d bytes, a full FM-1 dump is exactly %d (1 MiB)"
                        % (len(data), FLASH_SIZE))
    else:
        if data[VM_START:VM_START + 4] != VM_MAGIC:
            problems.append("its sector 0x%X does not start with %s (got %s)"
                            % (VM_START, VM_MAGIC.hex(" ").upper(),
                               data[VM_START:VM_START + 4].hex(" ").upper()))
        for start, end in ((VM_START + SECTOR_SIZE, VM_END), (GLOBALS_START, GLOBALS_END)):
            used = used_sectors(data, start, end)
            if used:
                problems.append("0x%X..0x%X is not erased (%d sectors hold data: %s)"
                                % (start, end - 1, len(used), ranges_text(used)))
    if problems:
        raise UnbrickError("%s is not a clean factory dump: %s. It must be the backup of a "
                           "factory-fresh FM-1; refusing." % (path.name, "; ".join(problems)))
    log.info("Factory     %s", path)
    log.info("  sha256                   %s", sha256(data))
    log.info("  settings sector 0x%X sha256 %s", VM_START,
             sha256(data[VM_START:VM_START + SECTOR_SIZE]))
    log.info("  clean: 0x%X..0x%X and 0x%X..0x%X erased", VM_START + SECTOR_SIZE, VM_END - 1,
             GLOBALS_START, GLOBALS_END - 1)
    return data


def used_sectors(data, start, end):
    """Addresses of the 4 KiB sectors in [start, end) that are not all 0xFF."""
    return [a for a in range(start, end, SECTOR_SIZE) if data[a:a + SECTOR_SIZE] != BLANK_SECTOR]


def ranges_text(addrs):
    """'0x93000..0x93FFF, 0x97000..0xDFFFF' for a sorted list of sector addresses."""
    runs = []
    for a in addrs:
        if runs and runs[-1][1] == a:
            runs[-1][1] = a + SECTOR_SIZE
        else:
            runs.append([a, a + SECTOR_SIZE])
    return ", ".join("0x%05X..0x%05X" % (a, b - 1) for a, b in runs) or "none"


def factory_settings_image(backup: bytes, factory: bytes) -> bytes:
    """The backup with the VM taken from the factory dump and the globals erased."""
    img = bytearray(backup)
    img[VM_START:VM_END] = factory[VM_START:VM_END]
    img[GLOBALS_START:GLOBALS_END] = b"\xff" * (GLOBALS_END - GLOBALS_START)
    return bytes(img)


def report_settings_areas(dump: bytes):
    """What the two windows hold now, area by area, and the markers found there."""
    lines = []
    for start, end, what in SETTINGS_AREAS:
        used = used_sectors(dump, start, end)
        lines.append("    0x%05X..0x%05X %-52s %2d of %2d sectors in use%s"
                     % (start, end - 1, what, len(used), (end - start) // SECTOR_SIZE,
                        (": " + ranges_text(used)) if used and len(used) < 4 else ""))
    for rx, what in MARKER_PATTERNS:
        hits = [(m.start(), m.group(0).decode("ascii", "replace"))
                for start, end in REPAIR_WINDOWS for m in rx.finditer(dump, start, end)]
        if hits:
            lines.append("    marker: %s %s" % (what, ", ".join("%r at 0x%05X" % (t, o)
                                                            for o, t in hits[:4])))
    return lines


def erase_repair_sector(dev: Fm1Device, addr: int):
    """One 4 KiB sector erase (never a block erase) inside the settings windows."""
    _check_repairable(addr, SECTOR_SIZE)
    dev.ldr.flash_erase_sector(addr)


def write_repair_sector(dev: Fm1Device, addr: int, data: bytes):
    """Program one erased 4 KiB sector of the settings windows in io_max chunks."""
    _check_repairable(addr, len(data))
    for off in range(0, len(data), dev.io_max):
        chunk = data[off:off + dev.io_max]
        _check_mutable(addr + off, len(chunk))
        dev.ldr.flash_write(addr + off, chunk)


def repair_sectors(dev: Fm1Device, expected: bytes, sectors):
    """Erase, write (unless blank) and read back each sector; retry a sector once."""
    prog = Progress("Repairing", len(sectors) * SECTOR_SIZE)
    try:
        for addr in sectors:
            target = expected[addr:addr + SECTOR_SIZE]
            for attempt in (1, 2):
                erase_repair_sector(dev, addr)
                if target != BLANK_SECTOR:
                    write_repair_sector(dev, addr, target)
                back = read_flash(dev, addr, SECTOR_SIZE, None)
                if back == target:
                    break
                first = next(i for i in range(SECTOR_SIZE) if back[i] != target[i])
                log.error("  sector 0x%05X: read-back MISMATCH, first at 0x%05X", addr,
                          addr + first)
                if attempt == 1:
                    log.info("  Retrying sector 0x%05X once", addr)
            else:
                raise UnbrickError("sector 0x%05X still reads back wrong after two attempts"
                                   % addr, EXIT_DANGER)
            log.debug("sector 0x%05X %s and verified", addr,
                      "erased" if target == BLANK_SECTOR else "erased, written")
            prog.update(SECTOR_SIZE)
    finally:
        prog.close()


REPAIR_DANGER_TEXT = """
!!! The settings areas could not be verified. !!!
  * Do not use the FM-1 before a successful repair-settings run.
  * The firmware itself (0x0000-0x92FFF) was not touched, nor 0xE9000-0xFBFFF
    (Bluetooth data, factory voices) and 0xFF000-0xFFFFF.
  * Run the same repair-settings command again, if possible with another cable
    or USB port. Your backup: %s
"""

REPAIR_DONE_TEXT = """
Done. Now:
  1. Unplug the USB cable.
  2. Switch the FM-1 off.
  3. Wait 5 seconds.
  4. Switch it on: stock V15 starts with factory settings.
  The preset banks can then be reloaded with stock's own System > Reset Patches.
"""


def cmd_repair_settings(args, device=None):
    log.info("Step 1/7: factory dump")
    factory = load_factory_dump(args.factory)
    pkg = None
    if args.package:
        pkg = Package.load(args.package)
        pkg.log_summary()

    def run(dev):
        log.info("Step 2/7: device")
        check_identity(dev, args.i_know)
        log.info("Step 3/7: backup (mandatory)")
        backup_path, dump = take_backup(dev, Path("backups"), [pkg] if pkg else [])
        if pkg and dump[FW_START:FW_END] != pkg.firmware:
            log.warning("NOTE: the firmware region differs from %s (factory units carry a "
                        "firmware image that differs from the public package); continuing.",
                        pkg.path.name)
        fw = dump[FW_START:FW_END]
        if any(rx.search(fw) for rx, _ in MARKER_PATTERNS[:2]):
            log.warning("NOTE: the firmware region holds Felucca-family markers: the unit "
                        "may run a custom firmware, not stock. This repair is meant for a unit "
                        "back on stock V15; it deletes that firmware's store too.")

        log.info("Step 4/7: plan")
        expected = factory_settings_image(dump, factory)
        sectors = [a for a in range(0, FLASH_SIZE, SECTOR_SIZE)
                   if dump[a:a + SECTOR_SIZE] != expected[a:a + SECTOR_SIZE]]
        for a in sectors:                       # by construction; checked anyway
            _check_repairable(a, SECTOR_SIZE)
        written = [a for a in sectors if expected[a:a + SECTOR_SIZE] != BLANK_SECTOR]
        log.info("  %d sectors differ from the factory state", len(sectors))

        def result(ok, final_sha=None, error=None):
            write_report(dev, "repair-result.json", dict(
                dev.report, factory_dump=Path(args.factory).name,
                factory_sha256=sha256(factory),
                factory_settings_sector_sha256=sha256(factory[VM_START:VM_START + SECTOR_SIZE]),
                sectors_changed=["0x%05x" % a for a in sectors],
                sectors_written=["0x%05x" % a for a in written],
                final_sha256=final_sha, ok=ok, error=error))

        if not sectors:
            log.info("The settings areas 0x%X..0x%X and 0x%X..0x%X are already factory-clean; "
                     "nothing to write.", VM_START, VM_END - 1, GLOBALS_START, GLOBALS_END - 1)
            result(True, sha256(dump))
            return EXIT_OK

        log.info("Step 5/7: confirmation")
        confirm(args, [
            "",
            "About to REPAIR the FM-1 settings areas:",
            "  device    %s (%s %s)%s" % (dev.path, dev.vendor, dev.product,
                                          "  [MOCK]" if dev.mock else ""),
            "  factory   %s (sha256 %s)" % (Path(args.factory).name, sha256(factory)[:16]),
            "  erase     %d sectors of 4 KiB: %s" % (len(sectors), ranges_text(sectors)),
            "  write     the factory settings into %s" % ranges_text(written),
            "  in use on the unit now:",
        ] + report_settings_areas(dump) + [
            "  DELETED:  songs, sounds and settings that stock V15 or a custom firmware",
            "            (Felucca, ChoralRoot, ...) keeps in these areas. The backup keeps them:",
            "            %s" % backup_path,
            "  NOT touched: 0x0..0x92FFF (boot head, firmware), 0xE9000..0xFBFFF (Bluetooth "
            "data, factory voices), 0xFF000..0xFFFFF",
            "",
        ], "REPAIR")

        log.info("Step 6/7: erase + write + verify, sector by sector. Do not unplug anything now.")
        try:
            repair_sectors(dev, expected, sectors)
            log.info("Final check: reading back the whole flash")
            final = read_flash(dev, 0, FLASH_SIZE, "Final read")
            log.info("  read-back sha256 %s", sha256(final))
            if final != expected:
                first = next(i for i in range(FLASH_SIZE) if final[i] != expected[i])
                raise UnbrickError("the final read differs from the expected image, first at "
                                   "0x%05X" % first, EXIT_DANGER)
        except KeyboardInterrupt:
            log.error("Interrupted during the repair.")
            log.error(REPAIR_DANGER_TEXT % backup_path)
            result(False, error="interrupted")
            return EXIT_DANGER
        except Exception as e:
            log.error("Repair failed: %s", e)
            log.error(REPAIR_DANGER_TEXT % backup_path)
            result(False, error=str(e))
            return EXIT_DANGER
        result(True, sha256(final))
        log.info("  equals the expected image byte for byte")
        log.info("Step 7/7: settings areas repaired: %d sectors erased, %d written, verified.",
                 len(sectors), len(written))
        log.info(REPAIR_DONE_TEXT)
        return EXIT_OK
    return with_device(args, run, device)


# --------------------------------------------------------------------------
# softkey: the USB-MIDI key into ROM boot mode
# --------------------------------------------------------------------------
#
# Stock V15 (and the Felucca family) jumps into the mask-ROM UBOOT when its
# USB-MIDI port receives F0 22 24 35 7D F7. On USB that is the 8-byte transfer
# 04 F0 22 24 07 35 7D F7, and stock compares whole transfers: the six bytes go
# out as ONE SysEx message, in one send call. The unit leaves the bus once it
# takes the key, so a later send may fail. Only a power cycle leaves UBOOT.

SOFT_KEY = bytes([0xF0, 0x22, 0x24, 0x35, 0x7D, 0xF7])
SOFT_KEY_SENDS = 3
SOFT_KEY_GAP = 0.02            # seconds between the sends
SOFT_KEY_WAIT = 15.0           # seconds to wait for the WL82 UBOOT1.00 disk
# The identity query and its answer, after ChoralRoot's tools/fm1_install.py
# (HS_QUERY, unpack7, parse_identity, handshake).
HS_QUERY = bytes([0xF0, 0x00, 0x32, 0x45, 0x00, 0x00, 0x00, 0x40, 0x7F, 0xF7])
HS_TIMEOUT = 1.0
HS_TRIES = 3
FM1_PORT_TAG = "fm-1"          # an FM-1's MIDI port names contain this (any case)


def pack7(data: bytes) -> bytes:
    """8-bit bytes into 7-bit SysEx data, low bits first (fm1_install.py pack7)."""
    out, acc, nb = bytearray(), 0, 0
    for b in data:
        acc |= b << nb
        nb += 8
        while nb >= 7:
            out.append(acc & 0x7F)
            acc >>= 7
            nb -= 7
    if nb:
        out.append(acc & 0x7F)
    return bytes(out)


def unpack7(data: bytes) -> bytes:
    out, acc, nb = bytearray(), 0, 0
    for b in data:
        acc |= (b & 0x7F) << nb
        nb += 7
        while nb >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nb -= 8
    return bytes(out)


def parse_identity(pkt: bytes):
    """The identity text of an answer to HS_QUERY ("FM-1_015"), or None."""
    if len(pkt) < 2 or pkt[0] != 0xF0 or pkt[-1] != 0xF7:
        return None
    d = unpack7(pkt[1:-1])
    if len(d) != 34 or d[:3] != b"\x00\x59\x11":
        return None
    return d[6:33].split(b"\0")[0].decode("latin-1")


def identity_answer(text: str) -> bytes:
    """What an FM-1 answers to HS_QUERY (for the mock)."""
    body = b"\x00\x59\x11\x00\x00\x00" + text.encode("latin-1")[:26].ljust(27, b"\0") + b"\0"
    return b"\xF0" + pack7(body) + b"\xF7"


def describe_identity(text: str) -> str:
    m = re.fullmatch(r"FM-1_(\d+)", text)
    if not m:
        return "not an FM-1 firmware identity"
    if m.group(1).startswith("0"):
        return "M-VAVE stock firmware V%d" % int(m.group(1))
    if text == "FM-1_920":
        return "ChoralRoot (Felucca family)"
    return "Felucca-family firmware"


class MidiBackend:
    """The MIDI layer of softkey: port names, one open input/output pair, SysEx.

    list_ports() -> (input names, output names); open(in_name, out_name);
    send(msg) sends one whole message in one call; read(timeout) returns the
    next SysEx (bytes) or None; drain() drops what came in; close().
    """

    name = "?"

    def drain(self):
        while self.read(0) is not None:
            pass


class RtMidiBackend(MidiBackend):
    """python-rtmidi. The input is polled (no callback): fm1_install.py found that a
    callback can deadlock the interpreter when a port closes on CoreMIDI."""

    name = "python-rtmidi"

    def __init__(self, rtmidi):
        self.rt, self.inp, self.out = rtmidi, None, None

    def list_ports(self):
        names = []
        for cls in (self.rt.MidiIn, self.rt.MidiOut):
            p = cls()
            try:
                names.append(list(p.get_ports()))
            finally:
                p.delete()
        return names[0], names[1]

    def open(self, in_name, out_name):
        if in_name is not None:
            self.inp = self.rt.MidiIn()
            self.inp.ignore_types(sysex=False, timing=True, active_sense=True)
            self.inp.open_port(self.inp.get_ports().index(in_name))
        self.out = self.rt.MidiOut()
        self.out.open_port(self.out.get_ports().index(out_name))

    def send(self, msg):
        self.out.send_message(list(msg))

    def read(self, timeout):
        end = time.monotonic() + timeout
        while self.inp is not None:
            m = self.inp.get_message()             # (message, delta) or None
            if m:
                if m[0] and m[0][0] == 0xF0:
                    return bytes(m[0])
                continue                            # notes and the like: not for us
            if time.monotonic() >= end:
                break
            time.sleep(0.005)
        return None

    def close(self):
        for p in (self.inp, self.out):
            if p is not None:
                try:
                    p.close_port()
                    p.delete()
                except Exception as e:        # the unit may have left the bus already
                    log.debug("closing a MIDI port: %s", e)
        self.inp = self.out = None


class AlsaRawBackend(MidiBackend):
    """Linux without python-rtmidi: the sound card's raw MIDI device /dev/snd/midiCxDy
    (one device for both directions), named after /proc/asound/cards."""

    name = "ALSA raw MIDI"

    def __init__(self, proc="/proc/asound/cards", devdir="/dev/snd"):
        self.proc, self.devdir, self.fd, self.buf = proc, Path(devdir), None, bytearray()
        self.paths = {}

    def list_ports(self):
        cards = {}
        try:
            for line in Path(self.proc).read_text(errors="replace").splitlines():
                m = re.match(r"\s*(\d+)\s+\[(.*?)\s*\]:\s*(.*)$", line)
                if m:
                    cards[int(m.group(1))] = "%s: %s" % (m.group(2), m.group(3).strip())
        except OSError:
            pass
        self.paths = {}
        for p in sorted(self.devdir.glob("midiC*D*")):
            m = re.match(r"midiC(\d+)D(\d+)$", p.name)
            if m:
                c, d = int(m.group(1)), int(m.group(2))
                self.paths["%s (hw:%d,%d)" % (cards.get(c, "card %d" % c), c, d)] = p
        names = list(self.paths)
        return names, names

    def open(self, in_name, out_name):
        self.fd = os.open(str(self.paths[out_name]), os.O_RDWR | os.O_NONBLOCK)

    def send(self, msg):
        if os.write(self.fd, bytes(msg)) != len(msg):
            raise OSError("short write to the raw MIDI device")

    def read(self, timeout):
        import select
        end = time.monotonic() + timeout
        while True:
            start = self.buf.find(b"\xF0")
            stop = self.buf.find(b"\xF7", start + 1) if start >= 0 else -1
            if stop >= 0:
                pkt = bytes(b for b in self.buf[start:stop + 1] if b < 0xF8)   # drop real-time bytes
                del self.buf[:stop + 1]
                return pkt
            left = end - time.monotonic()
            if left < 0 or not select.select([self.fd], [], [], max(left, 0))[0]:
                return None
            try:
                self.buf += os.read(self.fd, 4096)
            except BlockingIOError:
                pass

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError as e:
                log.debug("closing the raw MIDI device: %s", e)
            self.fd = None


class MockMidi(MidiBackend):
    """A dry run's FM-1 on USB-MIDI: answers HS_QUERY with `identity` (None: no
    answer), records every message sent, and leaves the bus once it has the key
    (later sends fail, as on the unit)."""

    name = "MOCK MIDI"

    def __init__(self, ports=("FM-1",), identity=V15_IDENTITY):
        self.ports, self.identity = list(ports), identity
        self.sent, self.inbox, self.opened, self.closed, self.gone = [], [], None, False, False

    def list_ports(self):
        return list(self.ports), list(self.ports)

    def open(self, in_name, out_name):
        self.opened = (in_name, out_name)

    def send(self, msg):
        if self.gone:
            raise OSError("the device left the bus")
        self.sent.append(bytes(msg))
        if bytes(msg) == HS_QUERY and self.identity is not None:
            self.inbox.append(identity_answer(self.identity))
        if bytes(msg) == SOFT_KEY:
            self.gone = True

    def read(self, timeout):
        return self.inbox.pop(0) if self.inbox else None

    def close(self):
        self.closed = True


def midi_backend(args) -> MidiBackend:
    if args.dry_run:
        log.info("DRY RUN: using a MOCK FM-1 on MIDI (no real MIDI port is opened)")
        ports = getattr(args, "mock_ports", None)
        identity = getattr(args, "mock_identity", None) or V15_IDENTITY
        return MockMidi(ports=[p for p in ports.split(",") if p] if ports is not None
                        else ("FM-1",), identity=None if identity == "none" else identity)
    try:
        import rtmidi
        return RtMidiBackend(rtmidi)
    except ImportError:
        pass
    if sys.platform.startswith("linux"):
        log.info("python-rtmidi is not installed; using raw ALSA MIDI (/dev/snd/midiC*D*)")
        return AlsaRawBackend()
    raise UnbrickError("softkey needs python-rtmidi: run `%s -m pip install python-rtmidi` "
                       "(or `python fm1_unbrick.py setup`) and try again"
                       % Path(sys.executable).name, event_code="RTMIDI_MISSING")


def select_midi_port(names, explicit, what):
    """The one port named explicitly (exact, or the only one containing it), or the
    one FM-1 port. Refuses none and several."""
    if explicit:
        found = [n for n in names if n == explicit] or [n for n in names if explicit in n]
    else:
        found = [n for n in names if FM1_PORT_TAG in n.lower()]
    listing = "\n".join("  " + n for n in names) or "  (none)"
    if not found:
        raise UnbrickError("no MIDI %s port %s. Plug the FM-1 in with a data cable, switch it "
                           "on and wait for its screen. MIDI %s ports here:\n%s"
                           % (what, "matches %r" % explicit if explicit else "named FM-1",
                              what, listing), event_code="MIDI_PORT_NOT_FOUND")
    if len(found) > 1:
        raise SafetyError("%d MIDI %s ports match; refusing to guess. Unplug the other FM-1s "
                          "or pass --port with the exact name:\n%s"
                          % (len(found), what, "\n".join("  " + n for n in found)),
                          event_code="MIDI_PORT_AMBIGUOUS")
    return found[0]


def midi_handshake(midi: MidiBackend, tries=HS_TRIES, timeout=HS_TIMEOUT):
    """Ask the identity, up to `tries` times; its text or None (fm1_install.py handshake)."""
    midi.drain()
    for _ in range(tries):
        try:
            midi.send(HS_QUERY)
        except Exception as e:
            raise UnbrickError("could not send to the FM-1's MIDI port: %s" % e,
                               event_code="MIDI_SEND_FAILED")
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            pkt = midi.read(left)
            if pkt is None:
                break
            text = parse_identity(pkt)
            if text is not None:
                return text
    return None


def wait_for_uboot(timeout=SOFT_KEY_WAIT):
    """Poll find_devices() until a WL82 UBOOT1.00 disk shows up or `timeout` passes."""
    if sys.platform.startswith("linux") and not Path("/sys/class/scsi_generic").is_dir():
        log.info("No /sys/class/scsi_generic: load the SCSI generic driver with `sudo modprobe "
                 "sg`, then run `sudo python3 fm1_unbrick.py find`.")
        return []
    deadline = time.monotonic() + timeout
    while True:
        try:
            found = find_devices()
        except UnbrickError as e:
            log.debug("find: %s", e)
            found = []
        if found or time.monotonic() > deadline:
            return found
        time.sleep(1.0)


def cmd_softkey(args, midi=None):
    step_started("rom", "ROM key via USB-MIDI")
    midi = midi or midi_backend(args)
    log.info("MIDI        %s", midi.name)
    try:
        ins, outs = midi.list_ports()
    except Exception as e:
        raise UnbrickError("cannot list this computer's MIDI ports: %s" % e,
                           event_code="MIDI_LIST_FAILED")
    log.debug("MIDI inputs %r, outputs %r", ins, outs)
    out_name = select_midi_port(outs, args.port, "output")
    in_name = select_midi_port(ins, args.port, "input")
    log.info("FM-1 port   out %r, in %r", out_name, in_name)
    try:
        midi.open(in_name, out_name)
    except Exception as e:
        raise UnbrickError("cannot open the FM-1's MIDI port (%s). Close other programs that "
                           "use it (the M-VAVE app, a browser page, a DAW) and try again." % e,
                           event_code="MIDI_PORT_BUSY")
    try:
        ident = midi_handshake(midi)
        if ident is None:
            if not args.i_know:
                raise SafetyError("the FM-1 does not answer the identity query; refusing (is it "
                                  "on its normal screen? --i-know sends the key anyway)",
                                  event_code="NO_IDENTITY")
            log.warning("WARNING: no identity answer; sending the key because of --i-know")
        else:
            log.info("Identity    %s = %s", ident, describe_identity(ident))
            emit("identity", identity=ident, description=describe_identity(ident), port=out_name,
                 input_port=in_name)
            if ident.lower().startswith("ota-"):
                raise SafetyError("the FM-1 is in update mode (%s): finish or cancel the update "
                                  "first (switch it off and on); refusing" % ident,
                                  event_code="UPDATE_MODE")
            if not re.fullmatch(r"FM-1_\d+", ident):
                raise SafetyError("%r is not an FM-1; refusing" % ident, event_code="NOT_FM1")
        confirm(args, [
            "",
            "About to send the ROM boot key to the FM-1 on %r:" % out_name,
            "  the FM-1 drops into its ROM boot mode (USB disk %s), the screen goes" % DEVICE_MODEL,
            "  dark. Nothing is written to it. Switching it off and on brings it back.",
            "",
        ], "UBOOT", summary={"port": out_name, "identity": ident,
                             "description": describe_identity(ident) if ident else None})
        for i in range(SOFT_KEY_SENDS):
            if i:
                time.sleep(SOFT_KEY_GAP)
            try:
                midi.send(SOFT_KEY)
            except Exception as e:
                if i == 0:
                    raise UnbrickError("could not send the key: %s" % e,
                                       event_code="MIDI_SEND_FAILED")
                log.debug("send %d of the key: %s (the unit has left the bus: expected)", i + 1, e)
                break
        log.info("Key sent    %s", SOFT_KEY.hex(" ").upper())
        emit("key_sent", port=out_name)
    finally:
        midi.close()

    if args.dry_run:
        log.info("DRY RUN: a real FM-1 would now be in ROM boot mode (%s); not waiting for it.",
                 DEVICE_MODEL)
        step_succeeded("rom", rom_disks=1, mock=True)
        return EXIT_OK
    if not platform_supported():
        log.info("The FM-1 should now be in ROM boot mode (dark screen, USB disk %s). This Mac "
                 "cannot continue (jl-uboot-tool has no macOS SCSI back end): move the cable to a "
                 "Windows or Linux machine; the FM-1 stays in ROM boot mode while it is powered."
                 % DEVICE_MODEL)
        return EXIT_OK
    log.info("Waiting up to %d s for the %s disk", SOFT_KEY_WAIT, DEVICE_MODEL)
    found = wait_for_uboot()
    for c in found:
        log.info("Found       %s  %s", c["path"], c["description"])
    if found:
        log.info("The FM-1 is in ROM boot mode: run repair-settings or restore now (Windows: an "
                 "administrator PowerShell; Linux: sudo).")
        step_succeeded("rom", rom_disks=len(found))
        return EXIT_OK
    log.error("No %s disk appeared within %d s. The FM-1 may still be in ROM boot mode (dark "
              "screen): on Windows run `find` from an administrator PowerShell, on Linux with "
              "sudo after `sudo modprobe sg`.", DEVICE_MODEL, SOFT_KEY_WAIT)
    step_failed("rom", "NO_ROM_DISK", "no %s disk appeared within %d s after the key; the FM-1 "
                "may still be in ROM boot mode (dark screen): detect it again"
                % (DEVICE_MODEL, SOFT_KEY_WAIT))
    return EXIT_FAIL


# --------------------------------------------------------------------------
# Read-only commands of the graphical installer: inspect, doctor, probe
# --------------------------------------------------------------------------
#
# None of them opens the WL82 disk, uploads the loader or writes anything.
# probe sends only the identity query (HS_QUERY) to one MIDI port, never the key.

MANIFEST_NAME = "manifest.json"
MANIFEST_SEARCH_DEPTH = 6      # package folder and its parents
CATALOG_BLOCKING = ("hash_mismatch", "mismatch")


def find_manifest(package: Path):
    """The manifest.json of a local Firmware Hub capture holding `package`, or None."""
    for d in list(Path(package).resolve().parents)[:MANIFEST_SEARCH_DEPTH]:
        if (d / MANIFEST_NAME).is_file():
            return d / MANIFEST_NAME
    return None


def check_catalog(pkg: Package, manifest="auto"):
    """Compare the package with its record in a local capture's manifest.json.

    status: match | mismatch | hash_mismatch | unavailable (no published hash) |
    not_in_catalog | no_manifest | unreadable | not_checked.
    """
    if manifest in (None, "none"):
        return {"status": "not_checked"}
    path = find_manifest(pkg.path) if manifest == "auto" else Path(manifest)
    if path is None:
        return {"status": "no_manifest"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        records = data["records"]
        if not isinstance(records, list):
            raise ValueError("records is not a list")
    except (OSError, ValueError, KeyError, TypeError) as e:
        return {"status": "unreadable", "manifest": str(path), "message": str(e)}
    me = os.path.normcase(str(pkg.path.resolve()))
    rec, matched_by = None, None
    for r in records:
        rel = r.get("relative_path") if isinstance(r, dict) else None
        if rel and os.path.normcase(str((path.parent / rel).resolve())) == me:
            rec, matched_by = r, "path"
            break
    if rec is None:
        for r in records:
            if isinstance(r, dict) and pkg.file_sha256 in ((r.get("sha256") or "").lower(),
                                                            (r.get("expected_sha256") or "").lower()):
                rec, matched_by = r, "sha256"
                break
    if rec is None:
        return {"status": "not_in_catalog", "manifest": str(path)}
    expected = (rec.get("expected_sha256") or "").lower()
    recorded = (rec.get("sha256") or "").lower()
    if rec.get("status") == "hash_mismatch":
        status = "hash_mismatch"
    elif expected:
        status = "match" if expected == pkg.file_sha256 else "mismatch"
    elif recorded and recorded != pkg.file_sha256:
        status = "mismatch"            # the file changed since the capture
    else:
        status = "unavailable"
    return {"status": status, "manifest": str(path), "matched_by": matched_by,
            "name": rec.get("name"), "version": rec.get("version"),
            "record_status": rec.get("status"), "hash_source": rec.get("hash_source"),
            "expected_sha256": expected or None, "recorded_sha256": recorded or None,
            "file_sha256": pkg.file_sha256, "record_error": rec.get("error") or None,
            "catalog_identity": rec.get("identity") or None,
            "identity_matches": (rec.get("identity") or pkg.identity) == pkg.identity}


def vet_catalog(pkg: Package, manifest):
    """check_catalog, reported; refuses what the manifest marks as not intact."""
    catalog = check_catalog(pkg, manifest)
    emit("catalog", **catalog)
    status = catalog["status"]
    if status != "not_checked":
        log.info("  catalog          %s%s", status,
                 " (%s)" % catalog["manifest"] if catalog.get("manifest") else "")
    if status in CATALOG_BLOCKING:
        raise SafetyError(
            "%s: the local catalog marks this package as not intact (%s; expected sha256 %s, "
            "file sha256 %s). Download it again from the author's page or run the downloader "
            "again, and check that the sha256 matches the published one; refusing."
            % (pkg.path.name, status, catalog.get("expected_sha256") or "-", pkg.file_sha256),
            event_code="CATALOG_" + status.upper())
    return catalog


def cmd_inspect(args):
    pkg = load_package_step(args)
    catalog = vet_catalog(pkg, args.manifest)
    pkg.check_writable(args.i_know)
    step_succeeded("package", catalog=catalog["status"], **pkg.event_fields())
    return EXIT_OK


def is_admin() -> bool:
    if sys.platform == "win32":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


PINS_FILE = SCRIPT_DIR / "pins.json"


def verify_pins(pins_path: Path):
    """Files listed in pins.json (sha256 of each, relative to its folder) that differ."""
    pins = json.loads(pins_path.read_text(encoding="utf-8"))
    bad = []
    for rel, digest in sorted(pins["files"].items()):
        f = pins_path.parent / rel
        if not f.is_file():
            bad.append(rel + " (missing)")
        elif sha256(f.read_bytes()) != digest:
            bad.append(rel)
    return pins, bad


def cmd_doctor(args):
    step_started("environment", "Checking the local environment")
    checks = []

    def check(name, ok, detail, code, required=True):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "code": code,
                       "required": required})
        log.info("  %-14s %s  %s", name, "OK  " if ok else ("FAIL" if required else "WARN"),
                 detail)

    log.info("Environment")
    check("python", sys.version_info >= (3, 11), "%s (%s)" % (sys.version.split()[0],
                                                             sys.executable), "PYTHON_TOO_OLD")
    admin = is_admin()
    check("admin", admin, "administrator" if admin else "not an administrator", "NOT_ADMIN",
          required=not args.dry_run)
    jl_dir = Path(args.jl_dir)
    commit = None
    try:
        commit = verify_jlub(jl_dir)
        check("jl-uboot-tool", True, "commit %s, wl82 loader sha256 %s"
              % (commit, WL82_LOADER_SHA256), "JLUB")
    except UnbrickError as e:
        check("jl-uboot-tool", False, str(e), e.event_code or "JLUB")
    if PINS_FILE.is_file():
        try:
            pins, bad = verify_pins(PINS_FILE)
            check("pins", not bad, "%d pinned files intact" % len(pins["files"]) if not bad
                  else "changed: " + ", ".join(bad[:5]), "PINS_MISMATCH")
        except (OSError, ValueError, KeyError) as e:
            check("pins", False, "pins.json unreadable: %s" % e, "PINS_MISMATCH")
    else:
        check("pins", False, "%s not found" % PINS_FILE, "PINS_MISSING")
    try:
        load_jlub(jl_dir)
        check("dependencies", True, "jl-uboot-tool imports (PyYAML, crcmod)", "DEPENDENCY_MISSING")
    except UnbrickError as e:
        check("dependencies", False, str(e), "DEPENDENCY_MISSING")
    try:
        import rtmidi
        check("python-rtmidi", True, getattr(rtmidi, "__version__", "installed"), "RTMIDI_MISSING",
              required=False)
    except ImportError:
        check("python-rtmidi", False, "not installed: MIDI detection and the ROM key are "
              "unavailable", "RTMIDI_MISSING", required=False)
    emit("environment", python=sys.version.split()[0], admin=admin, jl_commit=commit,
         checks=checks)
    failed = [c for c in checks if c["required"] and not c["ok"]]
    if failed:
        raise UnbrickError("; ".join("%s: %s" % (c["name"], c["detail"]) for c in failed),
                           event_code=failed[0]["code"])
    step_succeeded("environment", python=sys.version.split()[0], admin=admin, jl_commit=commit,
                   warnings=[c["detail"] for c in checks if not c["ok"]])
    return EXIT_OK


KNOWN_PORT_ALIASES = ("fm-1", "felucca")   # auto-selection only; any port can be chosen


def midi_port_pairs(ins, outs):
    """Port names present as both input and output, without Windows' trailing index."""
    def base(name):
        return re.sub(r"\s+\d+$", "", name).strip()
    return sorted({base(n) for n in ins} & {base(n) for n in outs})


def cmd_probe(args, midi=None):
    expect = args.expect_identity
    step = "boot" if expect else "device"
    step_started(step, "Checking boot" if expect else "Detecting the FM-1")
    if args.dry_run:
        disks = mock_rom_disks(args)
    else:
        ensure_platform(args)
        disks = find_devices()
    ins, outs, midi_error = [], [], None
    try:
        midi = midi or midi_backend(args)
        ins, outs = midi.list_ports()
    except Exception as e:
        midi, midi_error = None, str(e)
    pairs = midi_port_pairs(ins, outs)
    target, auto = args.port, False
    if target is None:
        known = [p for p in pairs if any(a in p.lower() for a in KNOWN_PORT_ALIASES)]
        if len(known) == 1:
            target, auto = known[0], True
    identity = identity_error = None
    if target and midi is not None and not disks:
        try:
            out_name = select_midi_port(outs, target, "output")
            in_name = select_midi_port(ins, target, "input")
            midi.open(in_name, out_name)
            try:
                identity = midi_handshake(midi)
            finally:
                midi.close()
            if identity is None:
                identity_error = "no identity answer on %r" % target
            else:
                log.info("Identity    %s = %s on %r", identity, describe_identity(identity),
                         out_name)
                emit("identity", identity=identity, description=describe_identity(identity),
                     port=out_name, input_port=in_name)
        except UnbrickError as e:
            identity_error = str(e)
        except Exception as e:
            identity_error = "cannot open the MIDI port %r: %s" % (target, e)

    if len(disks) == 1:
        mode = "rom"
    elif len(disks) > 1:
        mode = "rom-multiple"
    elif identity and identity.lower().startswith("ota-"):
        mode = "update"
    elif identity and re.fullmatch(r"FM-1_\d+", identity):
        mode = "midi"
    else:
        mode = None
    connected = mode in ("rom", "midi")
    if connected:
        reason = None
    elif mode == "rom-multiple":
        reason = "%d %s disks: unplug the others" % (len(disks), DEVICE_MODEL)
    elif mode == "update":
        reason = "the FM-1 is in update mode (%s): switch it off and on" % identity
    elif identity:
        reason = "%r is not an FM-1 identity" % identity
    elif identity_error:
        reason = identity_error
    elif midi_error:
        reason = "no %s disk; MIDI unavailable: %s" % (DEVICE_MODEL, midi_error)
    elif not pairs:
        reason = "no %s disk and no MIDI port" % DEVICE_MODEL
    else:
        reason = "no %s disk; choose the FM-1's MIDI port and check its identity" % DEVICE_MODEL
    log.info("ROM disks   %d", len(disks))
    log.info("MIDI ports  %s", ", ".join(pairs) or "(none)")
    log.info("FM-1        %s", "connected (%s)" % mode if connected else "not detected: " + reason)
    emit("device_state", connected=connected, mode=mode, rom_disks=len(disks),
         rom_model=DEVICE_MODEL, midi_inputs=ins, midi_outputs=outs, midi_pairs=pairs,
         port=target, port_auto=auto, identity=identity,
         description=describe_identity(identity) if identity else None,
         midi_error=midi_error, identity_error=identity_error, reason=reason,
         mock=bool(args.dry_run))

    if expect:
        if mode == "midi" and identity == expect:
            step_succeeded("boot", identity=identity, port=target)
            return EXIT_OK
        if mode in ("rom", "rom-multiple"):
            raise UnbrickError("the FM-1 is still in ROM boot mode (%s): it did not start the "
                               "firmware" % DEVICE_MODEL, event_code="BOOT_STILL_ROM")
        if mode == "midi":
            raise UnbrickError("the FM-1 answers %s, expected %s" % (identity, expect),
                               event_code="IDENTITY_MISMATCH")
        raise UnbrickError("FM-1 not detected: %s" % reason, event_code="NO_DEVICE")
    if mode == "rom-multiple":
        raise SafetyError(reason, event_code="MULTIPLE_DEVICES")
    if mode == "update":
        raise SafetyError(reason, event_code="UPDATE_MODE")
    if not connected:
        raise UnbrickError("FM-1 not detected: %s" % reason, event_code="NO_DEVICE")
    step_succeeded("device", mode=mode, identity=identity, port=target, rom_disks=len(disks))
    return EXIT_OK


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def int0(s):
    return int(s, 0)


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true",
                        help="use a MOCK device; never open or write real hardware")
    common.add_argument("--device", help=r"device path (\\.\PhysicalDriveN or /dev/sgN); "
                                         "default: the single one found")
    common.add_argument("--i-know", action="store_true",
                        help="override the chip key / flash ID / identity checks")
    common.add_argument("--jl-dir", default=str(DEFAULT_JL_DIR),
                        help="jl-uboot-tool folder (default: %(default)s)")
    common.add_argument("--log-dir", default="logs", help="log folder (default: ./logs)")
    common.add_argument("--mock-image", help="dry run: seed the mock flash from a 1 MiB file")
    common.add_argument("--mock-flaky", choices=["read", "write", "write-always"],
                        help="dry run: inject a fault (one bad read / one bad write / a dead cell)")
    common.add_argument("--mock-chip-key", type=int0, help=argparse.SUPPRESS)
    common.add_argument("--mock-rom-disks", type=int, help=argparse.SUPPRESS)
    common.add_argument("--mock-ports", help=argparse.SUPPRESS)        # comma-separated
    common.add_argument("--mock-identity", help=argparse.SUPPRESS)     # "none": no answer
    common.add_argument("--events", action="store_true",
                        help="JSON Lines events on stdout for the graphical installer (human "
                             "output on stderr, confirmations read from stdin)")

    ap = argparse.ArgumentParser(
        prog="fm1_unbrick.py",
        description="Restore a soft-bricked M-VAVE FM-1 (USB disk 'WL82 UBOOT1.00') by "
                    "writing the firmware region at 0x4000 through jl-uboot-tool.")
    ap.add_argument("--version", action="version", version=TOOL_VERSION)
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("setup", parents=[common],
                       help="fetch jl-uboot-tool (pinned) and install its requirements")
    p.add_argument("--source", help="clone from this URL/path instead of GitHub")
    p.add_argument("--no-pip", action="store_true", help="do not run pip")

    sub.add_parser("find", parents=[common], help="list WL82 UBOOT1.00 devices")
    sub.add_parser("info", parents=[common], help="upload the loader, show chip key and flash ID")

    p = sub.add_parser("backup", parents=[common], help="read the whole flash twice into DIR")
    p.add_argument("dir")
    p.add_argument("--package", action="append",
                   help="also compare the dump with this .fwsc (repeatable)")

    p = sub.add_parser("extract", parents=[common],
                       help="extract the firmware region 0x4000..0x92FFF from a .fwsc")
    p.add_argument("package")
    p.add_argument("out")
    p.add_argument("--verify-v15", action="store_true",
                   help="fail unless it is the official V15 firmware")

    p = sub.add_parser("restore", parents=[common],
                       help="find + info + backup + write at 0x4000 + verify")
    p.add_argument("package")
    p.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    p.add_argument("--verify-v15", action="store_true",
                   help="refuse unless the package is the verified official V15")
    p.add_argument("--force-write", action="store_true",
                   help="write even if the device already holds this exact firmware")
    p.add_argument("--backup-dir", default="backups",
                   help="folder for this run's backup folder (default: ./backups)")
    p.add_argument("--manifest", default="none",
                   help="also refuse a package that this Firmware Hub capture manifest.json "
                        "marks as not intact ('auto': search the package's folders; "
                        "default: none)")

    p = sub.add_parser("softkey", parents=[common],
                       help="send a running FM-1 the USB-MIDI key into ROM boot mode (any OS)")
    p.add_argument("--port", help="the FM-1's MIDI port name (default: the single one named FM-1)")
    p.add_argument("--yes", action="store_true", help="skip the typed confirmation")

    p = sub.add_parser("repair-settings", parents=[common],
                       help="backup + settings areas 0x93000..0xE8FFF and 0xFC000..0xFEFFF "
                            "back to factory state + verify")
    p.add_argument("--factory", required=True, metavar="FACTORY_DUMP.bin",
                   help="full 1 MiB backup of a factory-fresh FM-1")
    p.add_argument("--package", help="also compare the unit's firmware with this .fwsc")
    p.add_argument("--yes", action="store_true", help="skip the typed confirmation")

    p = sub.add_parser("inspect", parents=[common],
                       help="describe and vet a .fwsc without touching any device")
    p.add_argument("package")
    p.add_argument("--manifest", default="auto",
                   help="Firmware Hub capture manifest.json to compare with ('auto': search "
                        "the package's folders, 'none': skip; default: auto)")

    sub.add_parser("doctor", parents=[common],
                   help="check Python, administrator rights, jl-uboot-tool and dependencies "
                        "(never downloads anything)")

    p = sub.add_parser("probe", parents=[common],
                       help="list WL82 disks and MIDI ports; ask one port for its identity "
                            "(never sends the ROM key)")
    p.add_argument("--port", help="MIDI port to ask (exact name or unique part of it)")
    p.add_argument("--expect-identity",
                   help="boot check: succeed only if the FM-1 answers this identity")
    return ap


FORBIDDEN_WITH_EVENTS = ("setup", "repair-settings")


def events_refusal(args):
    """What --events refuses: the installer never skips a check or picks the disk itself."""
    flags = [name for name, on in (("--yes", getattr(args, "yes", False)),
                                   ("--i-know", args.i_know),
                                   ("--force-write", getattr(args, "force_write", False)),
                                   ("--device", args.device)) if on]
    if args.command in FORBIDDEN_WITH_EVENTS:
        flags.append("the %s command" % args.command)
    return flags


def run_command(args, device, midi):
    if args.command == "setup":
        return cmd_setup(args)
    if args.command == "find":
        return cmd_find(args)
    if args.command == "info":
        return cmd_info(args, device)
    if args.command == "backup":
        return cmd_backup(args, device)
    if args.command == "extract":
        return cmd_extract(args)
    if args.command == "restore":
        return cmd_restore(args, device)
    if args.command == "softkey":
        return cmd_softkey(args, midi)
    if args.command == "repair-settings":
        return cmd_repair_settings(args, device)
    if args.command == "inspect":
        return cmd_inspect(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "probe":
        return cmd_probe(args, midi)
    return EXIT_UNSUPPORTED


def main(argv=None, device=None, midi=None):
    global _session, _events, _step
    _session = None
    _step = None
    args = build_parser().parse_args(argv)
    saved_stdout = sys.stdout
    if args.events:
        # stdout carries only events: the human text, and anything a library prints
        # (jl-uboot-tool prints while it waits for the disk), goes to stderr.
        _events, sys.stdout = sys.stdout, sys.stderr
    log_path = setup_logging(args.command, Path(args.log_dir),
                             sys.stderr if args.events else None)
    log.debug("args %r", vars(args))
    rc = EXIT_FAIL
    emit("hello", tool="fm1_unbrick.py", version=TOOL_VERSION, command=args.command,
         dry_run=bool(args.dry_run), log=str(log_path.resolve()))
    try:
        refused = events_refusal(args) if args.events else []
        if refused:
            raise SafetyError("--events never uses %s; refusing" % ", ".join(refused),
                              EXIT_UNSUPPORTED, "FORBIDDEN_OPTION")
        rc = run_command(args, device, midi)
        return rc
    except UnbrickError as e:
        log.error("ERROR: %s", e)
        step_failed(_step or args.command, e.event_code or "ERROR", str(e),
                    log=str(log_path.resolve()))
        rc = e.code
        return rc
    except KeyboardInterrupt:
        log.error("Interrupted.")
        step_failed(_step or args.command, "INTERRUPTED", "interrupted",
                    log=str(log_path.resolve()))
        return EXIT_FAIL
    except Exception as e:
        if not events_on():
            raise
        log.exception("Internal error: %s", e)
        step_failed(_step or args.command, "INTERNAL_ERROR", "%s: %s" % (type(e).__name__, e),
                    log=str(log_path.resolve()))
        return EXIT_FAIL
    finally:
        log.info("Log: %s", log_path)
        if _session is not None:
            log.info("Everything from this run is in one folder: %s", _session.resolve())
            log.info("To report a brick, zip that folder and send it (it holds your flash "
                     "contents, including on-device songs and sounds).")
        close_logging()
        if _session is not None:
            try:
                shutil.copyfile(str(log_path), str(_session / "unbrick.log"))
            except OSError:
                pass
        emit("done", exit_code=rc, log=str(log_path.resolve()),
             session=str(_session.resolve()) if _session is not None else None)
        _events, sys.stdout = None, saved_stdout


if __name__ == "__main__":
    sys.exit(main())
