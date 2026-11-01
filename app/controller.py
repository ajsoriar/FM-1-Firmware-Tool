"""Installer flow: which recovery command runs next, and what each event means for the rows.

The controller holds no Tk objects. Row colours change only here, and only in
response to recovery-tool messages (events, exit codes); user actions start recovery
jobs or answer the recovery tool's confirmation requests. Recovery-tool messages arrive
from reader threads through `inbox`; the UI calls pump() from Tk's main loop.
"""

from __future__ import annotations

import datetime
import queue
from dataclasses import dataclass, field
from pathlib import Path

from . import worker

PENDING, RUNNING, WAITING_USER, PASS, FAIL = "PENDING", "RUNNING", "WAITING_USER", "PASS", "FAIL"
STATUS_TEXT = {PENDING: "Pending", RUNNING: "In progress", WAITING_USER: "Waiting for user",
               PASS: "Success", FAIL: "Error"}

ROWS = (
    ("package", "Firmware selected"),
    ("environment", "Environment ready"),
    ("device", "FM-1 connected"),
    ("rom", "ROM mode ready"),
    ("bootloader", "Bootloader detected"),
    ("chip", "Chip and flash checked"),
    ("backup", "Backup verified"),
    ("confirm", "Write confirmation"),
    ("write", "Writing"),
    ("verify", "Read verified"),
    ("boot", "Boot confirmed"),
)
ORDER = [step for step, _ in ROWS]
INSTALL_ROWS = ORDER[ORDER.index("rom"):]          # reset at each installation attempt

RUNNING_TEXT = {
    "package": "Inspecting package (without accessing the FM-1)",
    "environment": "Checking Python, permissions, and local components",
    "device": "Looking for MIDI ports and WL82 UBOOT1.00 disks",
    "rom": "Checking identity over MIDI before sending the ROM key",
    "bootloader": "Looking for exactly one WL82 UBOOT1.00",
    "chip": "Loading the USB loader and reading the chip key and Flash ID",
    "backup": "Reading the full flash (1 MiB) twice",
    "confirm": "Preparing the write summary",
    "write": "Erasing and writing 0x4000..0x92FFF. Do NOT disconnect or power off the FM-1",
    "verify": "Reading the written region again and comparing it byte by byte",
    "boot": "Looking for the FM-1 after the manual reboot",
}

PHASE_TEXT = {"read1": "Read 1/2", "read2": "Read 2/2", "erase": "Erase",
              "write": "Write", "verify": "Post-write read"}

KIND_TEXT = {
    "official-v15": "Official M-VAVE V15 (hash verified)",
    "official-other": "Official M-VAVE package with no known hash (cannot be verified)",
    "felucca-family": "Third-party firmware (Felucca family)",
    "damaged-v15": "Claims to be official V15, but its hash does NOT match",
    "unknown": "Unrecognized identity",
}

CATALOG_TEXT = {
    "match": "local catalog: SHA-256 matches the published hash",
    "mismatch": "local catalog: SHA-256 does NOT match",
    "hash_mismatch": "local catalog: marked hash_mismatch",
    "unavailable": "local catalog: no published hash available to compare",
    "not_in_catalog": "not listed in the local catalog",
    "no_manifest": "not part of a local capture (no manifest.json)",
    "unreadable": "manifest.json is unreadable",
    "not_checked": "catalog not checked",
}
CATALOG_WARN = ("unavailable", "not_in_catalog", "unreadable")

CODE_TEXT = {
    "NO_DEVICE": "FM-1 not detected. Connect it directly with a USB data cable, "
                 "power it on, and click \"Detect FM-1\". You can choose the MIDI port manually.",
    "MULTIPLE_DEVICES": "More than one WL82 UBOOT1.00 is connected. Disconnect the others and "
                        "detect again; one will not be selected at random.",
    "NO_IDENTITY": "The port did not respond to the identity query. Is the FM-1 on its "
                   "normal screen? Try another port.",
    "NOT_FM1": "The returned identity does not match an FM-1. The key will not be sent.",
    "UPDATE_MODE": "The FM-1 is in update mode: power it off and back on.",
    "MIDI_PORT_NOT_FOUND": "That MIDI port was not found. Detect again and choose another.",
    "MIDI_PORT_AMBIGUOUS": "Several ports match that name. Disconnect the other "
                           "FM-1 devices or choose the exact name.",
    "MIDI_PORT_BUSY": "Could not open the MIDI port. Close other programs using it "
                      "(M-VAVE app, browser, or DAW).",
    "MIDI_SEND_FAILED": "Could not send to the MIDI port.",
    "MIDI_LIST_FAILED": "Could not list MIDI ports.",
    "RTMIDI_MISSING": "python-rtmidi is missing: install it manually from "
                      "motor\\requirements-lock.txt.",
    "NO_ROM_DISK": "The WL82 UBOOT1.00 disk did not appear after sending the key. If the screen is "
                   "black, click \"Detect FM-1\" and try installing again.",
    "CHIP_MISMATCH": "Chip key or Flash ID does not match an FM-1 (0x980F / 0x856014). "
                     "Installation stopped; nothing was written.",
    "BACKUP_READS_DIFFER": "The two backup reads do NOT match: the USB connection is "
                           "unreliable. Nothing was written. Try another data cable or port, without a hub.",
    "BACKUP_FILE_BAD": "The backup file could not be read back correctly. Nothing was written. "
                       "Check the destination drive.",
    "CANCELLED": "Cancelled. Nothing was written.",
    "CANCELLED_BY_USER": "Cancelled by the user before writing.",
    "PACKAGE_INVALID": "The file is not a complete FM-1 .fwsc package.",
    "PACKAGE_UNKNOWN_IDENTITY": "The package does not have a recognized FM-1 identity; it "
                                "will not be installed.",
    "PACKAGE_DAMAGED_V15": "The package claims to be official V15, but its hash does not match: "
                           "download it again from M-VAVE.",
    "PACKAGE_BLANK": "The package's firmware region is empty.",
    "NOT_V15": "The package is not the verified official V15.",
    "CATALOG_HASH_MISMATCH": "The local catalog marks this package as hash_mismatch. "
                             "Download it again from the author's page (or "
                             "rerun the downloader) and check that its SHA-256 matches "
                             "the published hash.",
    "CATALOG_MISMATCH": "The file's SHA-256 does not match the local catalog. "
                        "Download it again from the author's page and check that its "
                        "SHA-256 matches the published hash.",
    "NOT_ADMIN": "Administrator privileges are required to access the FM-1 in ROM mode. "
                 "Click \"Relaunch as administrator\".",
    "PYTHON_TOO_OLD": "Python 3.11 or later is required.",
    "JLUB_MISSING": "motor\\jl-uboot-tool is missing. Copy the entire INSTALLER folder again.",
    "JLUB_COMMIT": "motor\\jl-uboot-tool is not at the pinned commit.",
    "JLUB_INCOMPLETE": "motor\\jl-uboot-tool is incomplete.",
    "LOADER_HASH": "The wl82 loader does not have the pinned hash; it will not be used.",
    "PINS_MISMATCH": "A recovery-tool file does not match pins.json; it will not be used.",
    "PINS_MISSING": "motor\\pins.json is missing.",
    "DEPENDENCY_MISSING": "Local dependencies are missing. Install them manually from "
                          "motor\\requirements-lock.txt with the FM-1 disconnected.",
    "WRITE_FAILED": "The write failed. Do NOT power off or disconnect the FM-1.",
    "WRITE_NOT_VERIFIED": "The post-write read does NOT match. Do NOT power off or disconnect the FM-1.",
    "WRITE_INTERRUPTED": "The write was interrupted. Do NOT power off or disconnect the FM-1.",
    "BOOT_STILL_ROM": "The FM-1 is still in ROM mode (black screen): the firmware did not boot.",
    "IDENTITY_MISMATCH": "The FM-1 returned an identity different from the firmware that was written.",
    "FORBIDDEN_OPTION": "The recovery tool rejected a forbidden option.",
    "INTERRUPTED": "The recovery tool was interrupted.",
    "INTERNAL_ERROR": "Internal recovery tool error (see log).",
}

# Failures that mean "waiting for the user to reconnect / act", not a broken step.
WAITING_CODES = ("NO_DEVICE", "NOT_ADMIN", "NO_ROM_DISK")

# A failed command before its first step names the command: the row it belongs to.
COMMAND_ROW = {"inspect": "package", "doctor": "environment", "probe": "device",
               "softkey": "rom", "find": "bootloader", "restore": "package"}
JOB_ROW = {"inspect": "package", "doctor": "environment", "detect": "device", "softkey": "rom",
           "find": "bootloader", "restore": "package", "boot": "boot"}

BOOT_INSTRUCTIONS = ("Firmware written and verified. Now manually: 1) disconnect the USB cable; "
                     "2) power off the FM-1; 3) wait 5 seconds; 4) power it on and "
                     "reconnect it. Then click \"Check boot\".")


@dataclass
class Row:
    step: str
    title: str
    status: str = PENDING
    detail: str = ""
    time: str = ""
    progress: int = None

    @property
    def status_text(self):
        return STATUS_TEXT[self.status]


@dataclass
class Paths:
    data_root: Path
    backup_dir: Path

    @property
    def log_dir(self) -> Path:
        return self.data_root / "logs"

    @classmethod
    def default(cls, simulate=False):
        docs = Path.home() / "Documents"
        root = (docs if docs.is_dir() else Path.home()) / "FM1-Installer"
        if simulate:
            root = root / "simulation"
        return cls(root, root / "backups")


@dataclass
class Simulation:
    """A dry run of the whole flow with the recovery tool's mocks (--dry-run): what the mock
    device looks like to each command. The power cycle is simulated by check_boot()."""
    mode: str = "midi"                  # midi | rom | none
    ports: tuple = ("Felucca 0",)       # a Felucca-family unit: no port named FM-1
    identity: str = "FM-1_9530"
    fault: str = None                   # read | write | write-always | chip | multi | noanswer
    pending_boot: bool = False

    def args(self, command):
        if command == "inspect":
            return []
        args = ["--dry-run"]
        if command in ("probe", "find", "softkey"):
            disks = 2 if self.fault == "multi" else (1 if self.mode == "rom" else 0)
            midi = self.mode == "midi"
            args += ["--mock-rom-disks", str(disks),
                     "--mock-ports", ",".join(self.ports) if midi else "",
                     "--mock-identity", self.identity if midi and self.fault != "noanswer"
                     else "none"]
        if command == "restore":
            if self.fault in ("read", "write", "write-always"):
                args += ["--mock-flaky", self.fault]
            if self.fault == "chip":
                args += ["--mock-chip-key", "0x1234"]
        return args


def _short(sha):
    return (sha or "")[:16] + "…" if sha else "-"


def _now():
    return datetime.datetime.now()


class Controller:
    def __init__(self, launcher=worker.RecoveryJob, paths=None, simulation=None, clock=_now):
        self.launcher = launcher
        self.simulation = simulation
        self.paths = paths or Paths.default(simulate=simulation is not None)
        self.clock = clock
        self.inbox = queue.Queue()
        self.rows = {step: Row(step, title) for step, title in ROWS}
        self.listeners = []
        self.log_lines = []

        self.package_path = None
        self.package = None             # last "package" event
        self.catalog = None             # last "catalog" event
        self.environment = None         # last "environment" event
        self.device = None              # last "device_state" event
        self.last_check = None          # time of that event
        self.detection_fresh = False    # a detection since the last installation attempt
        self.port_choice = None         # MIDI port base name chosen by the user; None: automatic

        self.job = None
        self.job_id = 0
        self.job_kind = None
        self.job_failed = False         # the current job reported step_failed
        self.job_log = None
        self.pending_confirmation = None
        self.critical = False           # between write_started and the end of restore
        self.flow = None                # "install" while softkey -> find -> restore chain runs
        self.written = False            # restore verified the firmware region
        self.boot_confirmed = False
        self.danger = None              # step_failed event with danger=true
        self.notice = None              # (level, text) shown above the buttons
        self.last_log = None
        self.last_session = None
        self._queued = []
        self._user_cancelled = False

    # ------------------------------------------------------------------ observers

    def subscribe(self, fn):
        self.listeners.append(fn)

    def _changed(self):
        for fn in list(self.listeners):
            fn()

    # ------------------------------------------------------------------ state queries

    @property
    def busy(self):
        return self.job is not None

    @property
    def connected(self):
        return bool(self.device and self.device.get("connected"))

    def package_blocked(self):
        return self.rows["package"].status != PASS

    def can_install(self):
        return (not self.busy and self.rows["package"].status == PASS
                and self.rows["environment"].status == PASS
                and self.rows["device"].status == PASS and self.connected
                and self.detection_fresh and self.device.get("mode") in ("rom", "midi"))

    def install_blockers(self):
        """Why the install button is disabled, in the order the user should fix it."""
        if self.busy:
            return ["a task is running"]
        out = []
        if self.rows["package"].status != PASS:
            out.append("choose a valid firmware file")
        if self.rows["environment"].status != PASS:
            out.append("the environment is not ready")
        if not self.connected:
            out.append("FM-1 not detected")
        elif not self.detection_fresh:
            out.append('click "Detect FM-1" again')
        return out

    def can_cancel(self):
        return self.busy and not self.critical

    def can_close(self):
        return not self.critical

    def can_check_boot(self):
        return (not self.busy and self.written and not self.danger
                and self.rows["boot"].status in (WAITING_USER, FAIL))

    def package_warnings(self):
        out = []
        status = (self.catalog or {}).get("status")
        if status in CATALOG_WARN:
            out.append(CATALOG_TEXT[status].capitalize())
        if self.package and self.package.get("kind") == "official-other":
            out.append("Official package has no known hash; it cannot be verified.")
        if self.catalog and self.catalog.get("identity_matches") is False:
            out.append("The catalog identity (%s) does not match the package."
                       % self.catalog.get("catalog_identity"))
        return out

    # ------------------------------------------------------------------ rows

    def _stamp(self):
        return self.clock().strftime("%H:%M:%S")

    def _set(self, step, status, detail=None, progress=None):
        row = self.rows[step]
        row.status = status
        if detail is not None:
            row.detail = detail
        row.progress = progress
        row.time = self._stamp()

    def _reset(self, steps):
        for step in steps:
            row = self.rows[step]
            row.status, row.detail, row.time, row.progress = PENDING, "", "", None

    def _fail(self, step, code, message, log=None):
        explanation = CODE_TEXT.get(code, "")
        parts = [p for p in (explanation, "Recuperador: %s" % message if message else "",
                             "Log: %s" % log if log else "") if p]
        status = WAITING_USER if code in WAITING_CODES else FAIL
        self._set(step, status, "\n".join(parts))
        # Later steps of the installation are no longer valid.
        self._reset([s for s in INSTALL_ROWS if ORDER.index(s) > ORDER.index(step)])

    # ------------------------------------------------------------------ jobs

    def _start(self, kind, command, args=()):
        if self.busy:
            raise RuntimeError("a recovery job is already running")
        args = list(args)
        if self.simulation is not None:
            args += self.simulation.args(command)
        args += ["--log-dir", str(self.paths.log_dir)]
        argv = worker.recovery_command(command, args)
        self.job_id += 1
        job_id = self.job_id
        self.job_kind = kind
        self.job_failed = False
        self.job_log = None
        self.job = self.launcher(argv, self.paths.data_root,
                                 lambda k, p: self.inbox.put((job_id, k, p)))
        self.log_lines.append("> %s %s" % (command, " ".join(args)))
        self.job.start()
        self._changed()

    def _enqueue(self, fn):
        if self.busy:
            self._queued.append(fn)
        else:
            fn()

    def pump(self, limit=500):
        """Handle what the reader threads posted. Call from the UI thread."""
        handled = 0
        while handled < limit:
            try:
                job_id, kind, payload = self.inbox.get_nowait()
            except queue.Empty:
                break
            handled += 1
            if job_id == self.job_id and self.job is not None:
                self.handle(kind, payload, notify=False)
        if handled:
            self._changed()
        return handled

    def handle(self, kind, payload, notify=True):
        if kind == "event":
            self.apply_event(payload)
        elif kind == "stderr":
            self.log_lines.append(payload)
        elif kind == "protocol_error":
            self.log_lines.append("[protocol] %s" % payload)
            self.notice = ("warn", "The recovery tool sent a line that is not a valid event "
                                   "(see log).")
        elif kind == "exit":
            self._job_exit(payload)
        if notify:
            self._changed()

    # ------------------------------------------------------------------ user actions

    def start(self, package=None):
        """At start-up: only local checks (environment, package). Hardware waits for the user."""
        self._enqueue(self.check_environment)
        if package:
            self._enqueue(lambda: self.set_package(package))

    def check_environment(self):
        self._start("doctor", "doctor")

    def set_package(self, path):
        self.package_path = Path(path)
        self.package = self.catalog = None
        self.written = self.boot_confirmed = False
        self.danger = None
        self._reset(["package"] + INSTALL_ROWS)
        self._start("inspect", "inspect", [str(self.package_path), "--manifest", "auto"])

    def choose_package(self, path):
        """A package picked or dropped by the user: analysed now, or right after the local
        checks that may be running. Refused while a job touches the FM-1."""
        if self.busy and self.job_kind not in ("doctor", "inspect"):
            return False
        self._enqueue(lambda: self.set_package(path))
        return True

    def set_backup_dir(self, path):
        self.paths.backup_dir = Path(path)
        self._changed()

    def detect(self, port=None):
        """Read-only detection: WL82 disks, MIDI ports, identity of one port."""
        self.port_choice = port or None
        args = ["--port", port] if port else []
        self._start("detect", "probe", args)

    def start_install(self):
        if not self.can_install():
            raise RuntimeError("installation not allowed now: " + ", ".join(self.install_blockers()))
        self._reset(INSTALL_ROWS)
        self.written = self.boot_confirmed = False
        self.danger = None
        self.notice = None
        self.flow = "install"
        self.detection_fresh = False
        if self.device["mode"] == "rom":
            self._set("rom", PASS, "The FM-1 is already in ROM mode (%d %s disk; black screen is "
                                   "normal)" % (self.device["rom_disks"], self.device.get(
                                       "rom_model", "WL82 UBOOT1.00")))
            self._start("find", "find")
        else:
            self._start("softkey", "softkey", ["--port", self.device["port"]])

    def answer_confirmation(self, text):
        """Send the typed phrase; the recovery tool compares it with the exact phrase."""
        if self.pending_confirmation is None or self.job is None:
            return False
        self.log_lines.append("> confirmation %s sent" % self.pending_confirmation["phrase"]
                              if text.strip() == self.pending_confirmation["phrase"]
                              else "> confirmation rejected by user")
        ok = self.job.send_line(text)
        self._changed()
        return ok

    def cancel(self):
        """Cancel what can be cancelled safely; never once the write has started."""
        if not self.busy or self.critical:
            return False
        if self.pending_confirmation is not None:
            return self.answer_confirmation("")
        self._user_cancelled = True
        self.job.terminate()
        return True

    def check_boot(self):
        if not self.can_check_boot():
            raise RuntimeError("boot check not available now")
        if self.simulation is not None and self.simulation.pending_boot:
            self.simulation.mode = "midi"          # the simulated manual power cycle
        args = ["--expect-identity", self.package["identity"]]
        if self.port_choice:
            args += ["--port", self.port_choice]
        self._start("boot", "probe", args)

    # ------------------------------------------------------------------ events

    def apply_event(self, ev):
        name = ev["event"]
        handler = getattr(self, "_on_" + name, None)
        if handler is not None:
            handler(ev)

    def _row_for(self, step):
        if step in self.rows:
            if step == "device" and self.job_kind == "boot":
                return "boot"
            return step
        return JOB_ROW.get(self.job_kind) or COMMAND_ROW.get(step, "package")

    def _on_hello(self, ev):
        self.job_log = ev.get("log")

    def _on_step_started(self, ev):
        step = self._row_for(ev["step"])
        detail = RUNNING_TEXT.get(step, ev["title"])
        if ev.get("attempt", 1) > 1:
            detail += " (recovery tool retry %d/2)" % ev["attempt"]
        self._set(step, RUNNING, detail, progress=0 if step in ("backup", "write", "verify")
                  else None)

    def _on_progress(self, ev):
        step = self._row_for(ev["step"])
        row = self.rows[step]
        phase = PHASE_TEXT.get(ev["phase"], ev["phase"])
        row.progress = ev["percent"]
        row.detail = "%s: %d %%" % (phase, ev["percent"])
        if step == "write":
            row.detail += ". Do NOT disconnect or power off the FM-1"
        if step == "verify":
            # The read-back is part of the write as the user sees it.
            self.rows["write"].detail = "Written; post-write read: %d %%" % ev["percent"]

    def _on_step_succeeded(self, ev):
        step = self._row_for(ev["step"])
        self._set(step, PASS, self._success_text(step, ev))

    def _on_step_failed(self, ev):
        self.job_failed = True
        step = self._row_for(ev["step"])
        if ev.get("danger"):
            self.danger = ev
        self._fail(step, ev["code"], ev["message"], ev.get("log") or self.job_log)
        if ev.get("danger"):
            self._set("boot", FAIL, "Blocked: Do NOT power off, disconnect, or reboot the FM-1. "
                                    "Follow the recovery tool's safety warning.")
            self.notice = ("danger", ev.get("danger_text") or ev["message"])
        if step == "device" and self.job_kind == "detect":
            self.detection_fresh = False

    def _on_package(self, ev):
        self.package = ev

    def _on_catalog(self, ev):
        self.catalog = ev

    def _on_environment(self, ev):
        self.environment = ev

    def _on_device_state(self, ev):
        self.device = ev
        self.last_check = self.clock()
        if self.job_kind == "detect":
            self.detection_fresh = bool(ev.get("connected"))

    def _on_identity(self, ev):
        pass                    # reported again in device_state / the UBOOT summary

    def _on_confirmation_required(self, ev):
        self.pending_confirmation = ev
        step = "rom" if ev["id"] == "uboot" else "confirm"
        self._set(step, WAITING_USER, "Type %s to continue (or cancel)." % ev["phrase"])

    def _on_confirmation_result(self, ev):
        self.pending_confirmation = None
        step = "rom" if ev["id"] == "uboot" else "confirm"
        if ev["accepted"]:
            self._set(step, RUNNING, "%s confirmed" % ev["id"].upper())

    def _on_key_sent(self, ev):
        self.rows["rom"].detail = ('Key sent via "%s". Waiting for the WL82 UBOOT1.00 '
                                   '(black screen is normal)' % ev["port"])

    def _on_write_started(self, ev):
        self.critical = True
        self.notice = ("critical", "Writing %s..%s. Do NOT disconnect, power off, or close the app. "
                                   "Cancel is disabled." % (ev["start"], ev["end"]))

    def _on_warning(self, ev):
        self.log_lines.append("[warning] %s" % ev["message"])
        step = ev.get("step")
        if step in self.rows:
            self.rows[step].detail = "Recovery tool warning: %s" % ev["message"]

    def _on_done(self, ev):
        self.last_log = ev.get("log")
        self.last_session = ev.get("session") or self.last_session

    # ------------------------------------------------------------------ texts

    def _success_text(self, step, ev):
        if step == "package":
            kind = KIND_TEXT.get(ev.get("kind"), ev.get("kind"))
            version = (self.catalog or {}).get("version")
            parts = [ev.get("name"), "version %s" % version if version else "version: -",
                     ev.get("identity") or "(no identity)", kind,
                     "%d bytes" % ev.get("file_size", 0), "SHA-256 %s" % ev.get("file_sha256"),
                     CATALOG_TEXT.get(ev.get("catalog"), "")]
            return " · ".join(p for p in parts if p)
        if step == "environment":
            warns = ev.get("warnings") or []
            return "Python %s · %s · jl-uboot-tool %s · verified wl82 loader%s" % (
                ev.get("python"), "administrator" if ev.get("admin") else "not an administrator",
                (ev.get("jl_commit") or "")[:7], (" · warning: " + "; ".join(warns)) if warns else "")
        if step == "device":
            if ev.get("mode") == "rom":
                return "ROM mode: 1 WL82 UBOOT1.00 disk"
            return '%s on port "%s"' % (ev.get("identity"), ev.get("port"))
        if step == "rom":
            return "WL82 UBOOT1.00 disk present (black screen is normal)"
        if step == "bootloader":
            return "Exactly one %s" % ev.get("model", "WL82 UBOOT1.00")
        if step == "chip":
            return "Chip key %s · Flash ID %s" % (ev.get("chip_key"), ev.get("flash_id"))
        if step == "backup":
            return "Two identical reads of %d bytes · SHA-256 %s\n%s" % (
                ev.get("size", 0), _short(ev.get("sha256")), ev.get("path"))
        if step in ("confirm", "write") and ev.get("skipped"):
            return "Not needed: the FM-1 already contains this exact firmware"
        if step == "confirm":
            return "WRITE confirmed by the user"
        if step == "write":
            return "0x4000..0x92FFF written (%d bytes)" % ev.get("size", 0)
        if step == "verify":
            if ev.get("already_installed"):
                return "Identical byte for byte (confirmed in both backup reads)"
            return "Identical byte for byte | SHA-256 %s" % _short(ev.get("sha256"))
        if step == "boot":
            return 'Boot confirmed: %s on "%s"' % (ev.get("identity"), ev.get("port"))
        return ""

    def confirmation_lines(self, ev=None):
        """The dialog text of a confirmation_required event, in English."""
        ev = ev or self.pending_confirmation
        s = ev["summary"]
        if ev["id"] == "uboot":
            return [
                'The ROM key will be sent to the FM-1 via port "%s".' % s.get("port"),
                "Identity verified: %s (%s)" % (s.get("identity"), s.get("description")),
                "The FM-1 will enter ROM mode (WL82 UBOOT1.00 disk): the screen will go "
                "black; this is normal.",
                "Nothing is written. Power-cycling returns it to its firmware.",
            ]
        return [
            "The FM-1 flash is about to be WRITTEN:",
            "  Firmware   %s" % s.get("package"),
            "  Identity   %s (%s)" % (s.get("identity"), KIND_TEXT.get(s.get("kind"),
                                                                       s.get("description"))),
            "  SHA-256    %s" % s.get("firmware_sha256"),
            "  Target     %s..%s (%d bytes); boot head 0x0000-0x3FFF is untouched"
            % (s.get("start"), s.get("end"), s.get("size", 0)),
            "  Device     %s" % s.get("device"),
            "  Backup     %s" % s.get("backup"),
            "",
            "Do not disconnect or power off the FM-1 during erase, write, or verification. "
            "This cannot be cancelled.",
        ]

    # ------------------------------------------------------------------ job end

    def _job_exit(self, code):
        kind = self.job_kind
        cancelled, self._user_cancelled = self._user_cancelled, False
        # Rows this job left open: running ones, or the one waiting for its confirmation.
        active = [s for s in ORDER if self.rows[s].status == RUNNING]
        if self.pending_confirmation is not None:
            active.append("rom" if self.pending_confirmation["id"] == "uboot" else "confirm")
        if cancelled:
            for s in active:
                self._fail(s, "CANCELLED_BY_USER", "", self.job_log)
        elif code != 0 and not self.job_failed:
            self._fail(active[0] if active else JOB_ROW[kind], "EXIT_%s" % code,
                       "the recovery tool exited with code %s without reporting an error" % code,
                       self.job_log)
        # Nothing stays "running" once the process is gone (e.g. the write row when the
        # read-back fails): it did not complete.
        for s in ORDER:
            if self.rows[s].status == RUNNING:
                self._set(s, FAIL, "Not completed (exit code %s); see the failed step."
                          % code)
        self.job = None
        self.critical = False
        self.pending_confirmation = None
        if self.notice and self.notice[0] == "critical":
            self.notice = None
        if kind == "restore":
            self.detection_fresh = False

        if self.simulation is not None and code == 0:
            if kind == "softkey":
                self.simulation.mode = "rom"
            if kind == "restore" and self.package:
                self.simulation.identity = self.package["identity"]
                self.simulation.pending_boot = True

        if kind == "boot" and code == 0:
            self.boot_confirmed = True
            if self.simulation is not None:
                self.simulation.pending_boot = False
            self.notice = ("ok", "Boot confirmed: the FM-1 started with the installed firmware.")

        if self.flow == "install":
            if code != 0:
                self.flow = None
            elif kind == "softkey":
                self._start("find", "find")
                return
            elif kind == "find":
                self._start("restore", "restore", self._restore_args())
                return
            elif kind == "restore":
                self.flow = None
                self.written = self.rows["verify"].status == PASS
                if self.written:
                    self._set("boot", WAITING_USER, BOOT_INSTRUCTIONS)
                    self.notice = ("ok", "Firmware written and verified byte for byte. Boot "
                                         "is not confirmed yet.")
        if code == 3 and self.danger is None:
            # The recovery tool's danger exit without its event: never hide it.
            self.danger = {"message": "exit code 3"}
            self._set("boot", FAIL, "Blocked: the recovery tool reports that the region may be "
                                    "incomplete. Do NOT power off or disconnect the FM-1.")
            self.notice = ("danger", "The recovery tool exited with code 3: the firmware region "
                                     "may be incomplete. Do NOT power off or disconnect the FM-1.")
        if self._queued and self.job is None:
            self._queued.pop(0)()

    def _restore_args(self):
        args = [str(self.package_path), "--backup-dir", str(self.paths.backup_dir),
                "--manifest", "auto"]
        if self.package and self.package.get("kind") == "official-v15":
            args.append("--verify-v15")
        return args
