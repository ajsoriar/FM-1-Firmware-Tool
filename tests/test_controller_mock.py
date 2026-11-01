"""The installer flow without hardware.

ScriptedFlowTests feed the controller scripted recovery-tool output; the
SimulatedRecoveryTests run the real recovery tool with its mock devices
(--dry-run) through the real worker, as `python -m app --simulate` does.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from support import ScriptedLauncher, ev, write_package

from app import controller as c
from app import worker


def drain(ctl):
    while ctl.pump():
        pass


def step_ok(step, **fields):
    return ("event", ev("step_succeeded", step=step, **fields))


def started(step):
    return ("event", ev("step_started", step=step, title=step))


def exit_(code=0):
    return [("event", ev("done", exit_code=code)), ("exit", code)]


def hello(command):
    return ("event", ev("hello", command=command, version="t", log="C:/logs/run.log"))


PACKAGE = dict(name="test.fwsc", identity="FM-1_9530", kind="felucca-family",
               description="third-party", file_size=1000, file_sha256="a" * 64,
               firmware_sha256="b" * 64)


def device(connected=True, mode="midi", port="Felucca", identity="FM-1_9530", disks=0):
    return ("event", ev("device_state", connected=connected, mode=mode, rom_disks=disks,
                        rom_model="WL82 UBOOT1.00", midi_pairs=[port] if port else [],
                        port=port, identity=identity, reason=None if connected else "none"))


class Script:
    """Default happy-path output per recovery command; tests override entries."""

    def __init__(self, **over):
        self.over = over
        self.calls = []

    def __call__(self, argv, job):
        command = argv[2]
        self.calls.append(command)
        if command in self.over:
            return self.over[command](argv)
        return getattr(self, command)(argv)

    def doctor(self, argv):
        return [hello("doctor"), started("environment"),
                ("event", ev("environment", admin=True, checks=[], python="3.14.0")),
                step_ok("environment", python="3.14.0", admin=True, jl_commit="adb3f18")] + exit_()

    def inspect(self, argv):
        return [hello("inspect"), started("package"), ("event", ev("package", **PACKAGE)),
                ("event", ev("catalog", status="match", version="1.7.0")),
                step_ok("package", catalog="match", **PACKAGE)] + exit_()

    def probe(self, argv):
        if "--expect-identity" in argv:
            return [hello("probe"), started("boot"), device(),
                    step_ok("boot", identity="FM-1_9530", port="Felucca")] + exit_()
        return [hello("probe"), started("device"), device(),
                step_ok("device", mode="midi", identity="FM-1_9530", port="Felucca")] + exit_()

    def softkey(self, argv):
        return [hello("softkey"), started("rom"),
                ("event", ev("identity", identity="FM-1_9530", port="Felucca 0")),
                ("event", ev("confirmation_required", id="uboot", phrase="UBOOT",
                             summary={"port": "Felucca 0", "identity": "FM-1_9530"})),
                ("wait", lambda answer: [
                    ("event", ev("confirmation_result", id="uboot", accepted=answer == "UBOOT"))]
                 + ([("event", ev("key_sent", port="Felucca 0")), step_ok("rom", rom_disks=1)]
                    + exit_() if answer == "UBOOT" else
                    [("event", ev("step_failed", step="rom", code="CANCELLED",
                                  message="cancelled"))] + exit_(1)))]

    def find(self, argv):
        return [hello("find"), started("bootloader"),
                step_ok("bootloader", count=1, model="WL82 UBOOT1.00")] + exit_()

    def restore(self, argv):
        head = [hello("restore"), started("package"), step_ok("package", **PACKAGE),
                started("bootloader"), step_ok("bootloader", count=1), started("chip"),
                step_ok("chip", chip_key="0x980F", flash_id="0x856014"), started("backup"),
                step_ok("backup", path="C:/bk/full-flash-before.bin", size=0x100000,
                        sha256="c" * 64, reads_identical=True), started("confirm"),
                ("event", ev("confirmation_required", id="write", phrase="WRITE",
                             summary={"package": "test.fwsc", "start": "0x4000",
                                      "end": "0x92FFF", "size": 0x8F000}))]
        return head + [("wait", self.after_write)]

    def after_write(self, answer):
        if answer != "WRITE":
            return [("event", ev("confirmation_result", id="write", accepted=False)),
                    ("event", ev("step_failed", step="confirm", code="CANCELLED",
                                 message="cancelled; nothing was written"))] + exit_(1)
        return [("event", ev("confirmation_result", id="write", accepted=True)),
                step_ok("confirm"), ("event", ev("write_started", start="0x4000", end="0x92FFF")),
                started("write"), started("verify"), step_ok("write", size=0x8F000),
                step_ok("verify", byte_identical=True, sha256="b" * 64)] + exit_()


class ScriptedFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.pkg = write_package(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, script=None):
        self.script = script or Script()
        self.launcher = ScriptedLauncher(self.script)
        ctl = c.Controller(launcher=self.launcher, paths=c.Paths(self.tmp, self.tmp / "bk"))
        ctl.start(self.pkg)
        drain(ctl)
        return ctl

    def status(self, ctl):
        return {s: ctl.rows[s].status for s in c.ORDER}

    def test_start_runs_only_local_checks(self):
        ctl = self.make()
        self.assertEqual(self.script.calls, ["doctor", "inspect"])
        self.assertEqual(ctl.rows["package"].status, c.PASS)
        self.assertEqual(ctl.rows["environment"].status, c.PASS)
        self.assertEqual(ctl.rows["device"].status, c.PENDING)
        self.assertFalse(ctl.connected)
        self.assertFalse(ctl.can_install())
        self.assertIn("FM-1 not detected", ctl.install_blockers())

    def test_package_chosen_during_local_checks_is_queued(self):
        hold = []
        script = Script(doctor=lambda argv: [hello("doctor"), ("wait", lambda a: hold)])
        launcher = ScriptedLauncher(script)
        ctl = c.Controller(launcher=launcher, paths=c.Paths(self.tmp, self.tmp / "bk"))
        ctl.start()
        drain(ctl)
        self.assertEqual(ctl.job_kind, "doctor")
        self.assertTrue(ctl.choose_package(self.pkg))              # queued, not refused
        launcher.jobs[-1].send_line("")                            # doctor finishes
        launcher.jobs[-1].post("exit", 0)
        drain(ctl)
        self.assertEqual(script.calls, ["doctor", "inspect"])
        self.assertEqual(ctl.package_path, self.pkg)

    def test_package_not_changed_while_the_fm1_is_in_use(self):
        ctl = self.make(Script(find=lambda argv: [hello("find"), started("bootloader")]))
        ctl.detect()
        drain(ctl)
        ctl.start_install()
        drain(ctl)
        ctl.answer_confirmation("UBOOT")
        drain(ctl)
        self.assertEqual(ctl.job_kind, "find")
        self.assertFalse(ctl.choose_package(self.tmp / "other.fwsc"))
        self.assertEqual(ctl.package_path, self.pkg)

    def test_no_hardware(self):
        ctl = self.make(Script(probe=lambda argv: [
            hello("probe"), started("device"), device(connected=False, mode=None, port=None,
                                                      identity=None),
            ("event", ev("step_failed", step="device", code="NO_DEVICE",
                         message="FM-1 not detected"))] + exit_(1)))
        ctl.detect()
        drain(ctl)
        self.assertEqual(ctl.rows["device"].status, c.WAITING_USER)
        self.assertFalse(ctl.connected)
        self.assertFalse(ctl.can_install())
        self.assertIn("Detect FM-1", ctl.rows["device"].detail)
        self.assertIn("C:/logs/run.log", ctl.rows["device"].detail)

    def test_catalog_mismatch_never_reaches_write(self):
        ctl = self.make(Script(inspect=lambda argv: [
            hello("inspect"), started("package"), ("event", ev("package", **PACKAGE)),
            ("event", ev("catalog", status="hash_mismatch")),
            ("event", ev("step_failed", step="package", code="CATALOG_HASH_MISMATCH",
                         message="not intact"))] + exit_(1)))
        ctl.detect()
        drain(ctl)
        self.assertTrue(ctl.connected)
        self.assertEqual(ctl.rows["package"].status, c.FAIL)
        self.assertIn("Download it again", ctl.rows["package"].detail)
        self.assertFalse(ctl.can_install())
        with self.assertRaises(RuntimeError):
            ctl.start_install()
        self.assertNotIn("restore", self.script.calls)

    def test_felucca_port_full_flow(self):
        ctl = self.make()
        ctl.detect()
        drain(ctl)
        self.assertEqual(ctl.device["port"], "Felucca")
        self.assertTrue(ctl.can_install())
        ctl.start_install()
        drain(ctl)
        softkey = self.launcher.argvs[-1]
        self.assertEqual(softkey[2], "softkey")
        self.assertEqual(softkey[softkey.index("--port") + 1], "Felucca")
        self.assertEqual(ctl.rows["rom"].status, c.WAITING_USER)
        self.assertEqual(ctl.pending_confirmation["phrase"], "UBOOT")
        ctl.answer_confirmation("UBOOT")
        drain(ctl)
        self.assertEqual(self.script.calls[-2:], ["find", "restore"])
        self.assertEqual(ctl.rows["rom"].status, c.PASS)
        self.assertEqual(ctl.rows["confirm"].status, c.WAITING_USER)
        self.assertEqual(ctl.rows["write"].status, c.PENDING)
        self.assertTrue(ctl.can_cancel())
        restore = self.launcher.argvs[-1]
        self.assertIn("--backup-dir", restore)
        self.assertNotIn("--verify-v15", restore)        # not the official V15
        ctl.answer_confirmation("WRITE")
        drain(ctl)
        for step in ("bootloader", "chip", "backup", "confirm", "write", "verify"):
            self.assertEqual(ctl.rows[step].status, c.PASS, step)
        self.assertTrue(ctl.written)
        self.assertFalse(ctl.boot_confirmed)
        self.assertEqual(ctl.rows["boot"].status, c.WAITING_USER)
        self.assertIn("5 seconds", ctl.rows["boot"].detail)
        self.assertFalse(ctl.can_install())             # a fresh detection is needed first
        ctl.check_boot()
        drain(ctl)
        probe = self.launcher.argvs[-1]
        self.assertEqual(probe[probe.index("--expect-identity") + 1], "FM-1_9530")
        self.assertEqual(ctl.rows["boot"].status, c.PASS)
        self.assertTrue(ctl.boot_confirmed)

    def test_rom_mode_skips_the_key(self):
        ctl = self.make(Script(probe=lambda argv: [
            hello("probe"), started("device"), device(mode="rom", port=None, identity=None,
                                                      disks=1),
            step_ok("device", mode="rom", rom_disks=1)] + exit_()))
        ctl.detect()
        drain(ctl)
        ctl.start_install()
        drain(ctl)
        self.assertNotIn("softkey", self.script.calls)
        self.assertEqual(ctl.rows["rom"].status, c.PASS)
        self.assertEqual(ctl.pending_confirmation["phrase"], "WRITE")

    def test_several_rom_disks_stop(self):
        ctl = self.make(Script(probe=lambda argv: [
            hello("probe"), started("device"), device(connected=False, mode="rom-multiple",
                                                      port=None, identity=None, disks=2),
            ("event", ev("step_failed", step="device", code="MULTIPLE_DEVICES",
                         message="2 disks"))] + exit_(1)))
        ctl.detect()
        drain(ctl)
        self.assertEqual(ctl.rows["device"].status, c.FAIL)
        self.assertFalse(ctl.can_install())

    def test_unequal_backup_stops_before_confirmation(self):
        def restore(argv):
            return [hello("restore"), started("package"), step_ok("package", **PACKAGE),
                    started("bootloader"), step_ok("bootloader", count=1), started("chip"),
                    step_ok("chip", chip_key="0x980F", flash_id="0x856014"), started("backup"),
                    ("event", ev("progress", step="backup", phase="read2", percent=100)),
                    ("event", ev("step_failed", step="backup", code="BACKUP_READS_DIFFER",
                                 message="the two backup reads DIFFER"))] + exit_(1)
        ctl = self.make(Script(restore=restore))
        ctl.detect()
        drain(ctl)
        ctl.start_install()
        drain(ctl)
        ctl.answer_confirmation("UBOOT")
        drain(ctl)
        self.assertEqual(ctl.rows["backup"].status, c.FAIL)
        self.assertIn("Nothing was written", ctl.rows["backup"].detail)
        self.assertIsNone(ctl.pending_confirmation)
        for step in ("confirm", "write", "verify", "boot"):
            self.assertEqual(ctl.rows[step].status, c.PENDING, step)
        self.assertFalse(ctl.written)
        self.assertIsNone(ctl.flow)

    def test_not_verified_is_danger(self):
        def after_write(answer):
            return [("event", ev("confirmation_result", id="write", accepted=True)),
                    step_ok("confirm"),
                    ("event", ev("write_started", start="0x4000", end="0x92FFF")),
                    started("write"), started("verify"),
                    ("event", ev("step_failed", step="verify", code="WRITE_NOT_VERIFIED",
                                 message="read-back mismatch after two attempts", danger=True,
                                 danger_text="!!! The firmware region could not be verified. !!!"
                                             "\n  * Do NOT power off"))] + exit_(3)
        script = Script()
        script.after_write = after_write
        ctl = self.make(script)
        ctl.detect()
        drain(ctl)
        ctl.start_install()
        drain(ctl)
        ctl.answer_confirmation("UBOOT")
        drain(ctl)
        ctl.answer_confirmation("WRITE")
        drain(ctl)
        self.assertEqual(ctl.rows["verify"].status, c.FAIL)
        self.assertEqual(ctl.rows["write"].status, c.FAIL)          # not left "running"
        self.assertEqual(ctl.rows["boot"].status, c.FAIL)
        self.assertIn("Do NOT power off", ctl.rows["boot"].detail)
        self.assertEqual(ctl.notice[0], "danger")
        self.assertIn("Do NOT power off", ctl.notice[1])     # the recovery tool's own warning
        self.assertFalse(ctl.can_check_boot())
        self.assertFalse(ctl.written)

    def test_write_and_verify_green_only_after_byte_identical(self):
        ctl = self.make()
        ctl.job, ctl.job_kind = object(), "restore"
        seen = []
        for e in [ev("write_started", start="0x4000", end="0x92FFF"),
                  ev("step_started", step="write", title="w"),
                  ev("progress", step="write", phase="erase", percent=50),
                  ev("progress", step="write", phase="write", percent=100),
                  ev("step_started", step="verify", title="v"),
                  ev("progress", step="verify", phase="verify", percent=90)]:
            ctl.handle("event", e)
            seen.append((ctl.rows["write"].status, ctl.rows["verify"].status))
            self.assertFalse(ctl.can_cancel())
            self.assertFalse(ctl.can_close())
        self.assertTrue(all(c.PASS not in pair for pair in seen), seen)
        ctl.handle("event", ev("step_succeeded", step="write"))
        ctl.handle("event", ev("step_succeeded", step="verify", byte_identical=True,
                               sha256="b" * 64))
        self.assertEqual((ctl.rows["write"].status, ctl.rows["verify"].status), (c.PASS, c.PASS))

    def test_cancel_at_write_confirmation_answers_nothing(self):
        ctl = self.make()
        ctl.detect()
        drain(ctl)
        ctl.start_install()
        drain(ctl)
        ctl.answer_confirmation("UBOOT")
        drain(ctl)
        self.assertTrue(ctl.cancel())
        drain(ctl)
        self.assertEqual(self.launcher.jobs[-1].answers, [""])
        self.assertEqual(ctl.rows["confirm"].status, c.FAIL)
        self.assertIn("Cancelled", ctl.rows["confirm"].detail)
        self.assertEqual(ctl.rows["write"].status, c.PENDING)

    def test_cancel_terminates_before_any_write(self):
        ctl = self.make(Script(find=lambda argv: [hello("find"), started("bootloader")]))
        ctl.detect()
        drain(ctl)
        ctl.start_install()
        drain(ctl)
        ctl.answer_confirmation("UBOOT")
        drain(ctl)
        self.assertTrue(ctl.busy)
        self.assertTrue(ctl.cancel())
        drain(ctl)
        self.assertTrue(self.launcher.jobs[-1].terminated)
        self.assertEqual(ctl.rows["bootloader"].status, c.FAIL)
        self.assertFalse(ctl.busy)
        self.assertNotIn("restore", self.script.calls)

    def test_rows_change_only_on_messages(self):
        ctl = self.make()
        before = self.status(ctl)
        for _ in range(5):
            ctl.pump()
            time.sleep(0.01)
        self.assertEqual(self.status(ctl), before)

    def test_messages_of_an_old_job_are_ignored(self):
        ctl = self.make()
        ctl.inbox.put((ctl.job_id - 1, "event", ev("step_failed", step="package", code="X",
                                                   message="stale")))
        drain(ctl)
        self.assertEqual(ctl.rows["package"].status, c.PASS)

    def test_unreported_crash_is_shown(self):
        ctl = self.make(Script(probe=lambda argv: [hello("probe"), started("device"),
                                                   ("stderr", "Traceback ..."), ("exit", 1)]))
        ctl.detect()
        drain(ctl)
        self.assertEqual(ctl.rows["device"].status, c.FAIL)
        self.assertIn("code 1", ctl.rows["device"].detail)

    def test_official_v15_adds_verify_v15(self):
        v15 = dict(PACKAGE, kind="official-v15", identity="FM-1_015")
        ctl = self.make(Script(inspect=lambda argv: [
            hello("inspect"), started("package"), ("event", ev("package", **v15)),
            ("event", ev("catalog", status="no_manifest")),
            step_ok("package", catalog="no_manifest", **v15)] + exit_()))
        ctl.detect()
        drain(ctl)
        ctl.start_install()
        drain(ctl)
        ctl.answer_confirmation("UBOOT")
        drain(ctl)
        self.assertIn("--verify-v15", self.launcher.argvs[-1])

    def test_forbidden_options_never_used(self):
        ctl = self.make()
        ctl.detect()
        drain(ctl)
        ctl.start_install()
        drain(ctl)
        ctl.answer_confirmation("UBOOT")
        drain(ctl)
        ctl.answer_confirmation("WRITE")
        drain(ctl)
        ctl.check_boot()
        drain(ctl)
        for argv in self.launcher.argvs:
            for forbidden in worker.FORBIDDEN_ARGS:
                self.assertNotIn(forbidden, argv)
            self.assertEqual(argv[-1], "--events")


class SimulatedRecoveryTests(unittest.TestCase):
    """The real recovery tool (--dry-run mocks) behind the real worker."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.pkg = write_package(self.tmp, identity="FM-1_5000901")

    def tearDown(self):
        self._tmp.cleanup()

    def run_flow(self, simulation):
        ctl = c.Controller(paths=c.Paths(self.tmp / "data", self.tmp / "bk"),
                           simulation=simulation)
        phrases = []

        def settle(timeout=120):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                ctl.pump()
                if ctl.pending_confirmation is not None:
                    phrase = ctl.pending_confirmation["phrase"]
                    phrases.append(phrase)
                    ctl.answer_confirmation(phrase)
                if not ctl.busy and ctl.inbox.empty():
                    return
                time.sleep(0.01)
            self.fail("recovery tool did not finish: %s" % ctl.log_lines[-20:])

        ctl.start(self.pkg)
        settle()
        ctl.detect()
        settle()
        return ctl, settle, phrases

    def test_full_simulated_installation(self):
        ctl, settle, phrases = self.run_flow(c.Simulation())
        self.assertEqual(ctl.device["port"], "Felucca")             # alias, no "FM-1" port
        self.assertTrue(ctl.can_install(), ctl.install_blockers())
        ctl.start_install()
        settle()
        self.assertEqual(phrases, ["UBOOT", "WRITE"])
        statuses = {s: ctl.rows[s].status for s in c.ORDER}
        self.assertEqual([s for s in c.ORDER if statuses[s] != c.PASS], ["boot"], statuses)
        self.assertEqual(ctl.rows["boot"].status, c.WAITING_USER)
        self.assertTrue(Path(ctl.last_session).is_dir())
        self.assertTrue(str(ctl.last_session).startswith(str(self.tmp / "bk")))
        ctl.check_boot()
        settle()
        self.assertEqual(ctl.rows["boot"].status, c.PASS, ctl.rows["boot"].detail)
        self.assertIn("FM-1_5000901", ctl.rows["boot"].detail)

    def test_simulated_unreliable_usb(self):
        ctl, settle, phrases = self.run_flow(c.Simulation(mode="rom", fault="read"))
        ctl.start_install()
        settle()
        self.assertEqual(phrases, [])                                # never asked for WRITE
        self.assertEqual(ctl.rows["backup"].status, c.FAIL)
        self.assertIn("do NOT match", ctl.rows["backup"].detail)
        self.assertFalse(ctl.written)

    def test_simulated_dead_cell_blocks_power_off(self):
        ctl, settle, phrases = self.run_flow(c.Simulation(mode="rom", fault="write-always"))
        ctl.start_install()
        settle()
        self.assertEqual(phrases, ["WRITE"])
        self.assertEqual(ctl.rows["verify"].status, c.FAIL)
        self.assertEqual(ctl.rows["boot"].status, c.FAIL)
        self.assertEqual(ctl.notice[0], "danger")
        self.assertFalse(ctl.can_check_boot())


if __name__ == "__main__":
    unittest.main()
