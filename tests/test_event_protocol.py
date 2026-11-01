"""The JSON Lines protocol: parsing, the argv guard, and the real recovery tool's output.

The process tests run motor/fm1_unbrick.py --events --dry-run (mock devices):
no hardware is opened.
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from support import INSTALLER, write_capture, write_package

from app import events, worker


class ParseTests(unittest.TestCase):
    def test_valid_event(self):
        e = events.parse_event('{"schema":1,"event":"progress","step":"write","phase":"erase",'
                               '"percent":55}')
        self.assertEqual(e["percent"], 55)

    def test_rejects(self):
        bad = [
            "Backing up the whole flash",                                   # human text
            "[1, 2]",
            '{"schema":2,"event":"done","exit_code":0}',                    # other schema
            '{"event":"done","exit_code":0}',                               # no schema
            '{"schema":1,"event":"reboot"}',                                # unknown event
            '{"schema":1,"event":"step_failed","step":"backup","code":"X"}',  # no message
            '{"schema":1,"event":"step_started","step":"flash","title":"x"}',  # unknown step
            '{"schema":1,"event":"progress","step":"write","phase":"w","percent":101}',
            '{"schema":1,"event":"progress","step":"write","phase":"w","percent":true}',
            '{"schema":1,"event":"confirmation_required","id":"write","phrase":"WRITE",'
            '"summary":"x"}',
        ]
        for line in bad:
            with self.subTest(line=line), self.assertRaises(events.ProtocolError):
                events.parse_event(line)

    def test_step_failed_may_name_the_command(self):
        events.parse_event('{"schema":1,"event":"step_failed","step":"restore",'
                           '"code":"FORBIDDEN_OPTION","message":"x"}')


class ArgvGuardTests(unittest.TestCase):
    def test_forbidden_options_never_reach_the_recovery_tool(self):
        for args in (["--yes"], ["--i-know"], ["--force-write"],
                     ["--device", r"\\.\PhysicalDrive1"], ["--device=x"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                worker.recovery_command("restore", ["pkg.fwsc"] + args)
        for command in ("setup", "repair-settings", "backup", "info"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                worker.recovery_command(command)

    def test_argv(self):
        argv = worker.recovery_command("probe", ["--port", "Felucca"])
        self.assertEqual(argv[1], str(worker.RECOVERY_SCRIPT))
        self.assertEqual(argv[-4:], ["probe", "--port", "Felucca", "--events"])
        self.assertTrue(worker.RECOVERY_SCRIPT.is_relative_to(INSTALLER))


def run_recovery(command, args, cwd, answers=None, timeout=120):
    """Run one recovery-tool process through worker.RecoveryJob; return (events, messages)."""
    q = queue.Queue()
    job = worker.RecoveryJob(worker.recovery_command(command, list(args) + [
        "--log-dir", str(Path(cwd) / "logs")]), cwd, lambda k, p: q.put((k, p)))
    job.start()
    messages, deadline = [], time.monotonic() + timeout
    while True:
        kind, payload = q.get(timeout=max(1, deadline - time.monotonic()))
        messages.append((kind, payload))
        if kind == "event" and payload["event"] == "confirmation_required":
            job.send_line((answers or {}).get(payload["phrase"], ""))
        if kind == "exit":
            break
    evs = [p for k, p in messages if k == "event"]
    return evs, messages


def names(evs):
    return [e["event"] for e in evs]


def first(evs, name, **match):
    for e in evs:
        if e["event"] == name and all(e.get(k) == v for k, v in match.items()):
            return e
    raise AssertionError("no %s %r in %s" % (name, match, names(evs)))


class RecoveryProcessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_ok(self, command, args, answers=None, code=0):
        evs, messages = run_recovery(command, args, self.tmp, answers)
        self.assertEqual([p for k, p in messages if k == "protocol_error"], [])
        self.assertEqual(evs[0]["event"], "hello")
        self.assertEqual(evs[-1]["event"], "done")
        self.assertEqual(messages[-1], ("exit", code), [p for k, p in messages if k == "stderr"])
        self.assertEqual(evs[-1]["exit_code"], code)
        for e in evs:                       # paths, sizes and hashes only: never flash data
            self.assertLess(len(json.dumps(e)), 4096, e["event"])
        return evs

    def test_inspect(self):
        pkg = write_package(self.tmp, identity="FM-1_5000901")
        evs = self.run_ok("inspect", [str(pkg)])
        done = first(evs, "step_succeeded", step="package")
        self.assertEqual(done["identity"], "FM-1_5000901")
        self.assertEqual(done["kind"], "felucca-family")
        self.assertEqual(done["catalog"], "no_manifest")
        self.assertEqual(done["firmware_start"], "0x4000")
        self.assertEqual(done["firmware_end"], "0x92FFF")

    def test_inspect_catalog(self):
        cases = [("already_present", None, 0, "match", None),
                 ("hash_mismatch", None, 1, "hash_mismatch", "CATALOG_HASH_MISMATCH"),
                 ("already_present", "0" * 64, 1, "mismatch", "CATALOG_MISMATCH"),
                 ("already_present", "", 0, "unavailable", None)]
        for status, expected, code, catalog, failure in cases:
            with self.subTest(status=status, expected=expected):
                capture = Path(tempfile.mkdtemp(dir=self.tmp))
                (capture / "firmwares" / "Test" / "1.0").mkdir(parents=True)
                pkg = write_package(capture / "firmwares" / "Test" / "1.0")
                write_capture(capture, pkg, status=status, expected=expected)
                evs = self.run_ok("inspect", [str(pkg)], code=code)
                self.assertEqual(first(evs, "catalog")["status"], catalog)
                if failure:
                    self.assertEqual(first(evs, "step_failed")["code"], failure)
                    self.assertNotIn("step_succeeded", names(evs))

    def test_inspect_refuses_unknown_identity(self):
        pkg = write_package(self.tmp, identity="SOMETHING")
        evs = self.run_ok("inspect", [str(pkg)], code=1)
        self.assertEqual(first(evs, "step_failed")["code"], "PACKAGE_UNKNOWN_IDENTITY")

    def test_inspect_not_a_package(self):
        bad = self.tmp / "bad.fwsc"
        bad.write_bytes(b"\x00" * 5000)
        evs = self.run_ok("inspect", [str(bad)], code=1)
        self.assertEqual(first(evs, "step_failed")["code"], "PACKAGE_INVALID")

    def test_doctor(self):
        evs = self.run_ok("doctor", ["--dry-run"])
        env = first(evs, "environment")
        self.assertTrue(all(c["ok"] for c in env["checks"] if c["name"] != "admin"
                            and c["name"] != "python-rtmidi"), env["checks"])
        first(evs, "step_succeeded", step="environment")

    def test_probe_felucca_alias(self):
        evs = self.run_ok("probe", ["--dry-run", "--mock-rom-disks", "0", "--mock-ports",
                                    "Microsoft GS Wavetable Synth 0,Felucca 1",
                                    "--mock-identity", "FM-1_9530"])
        state = first(evs, "device_state")
        self.assertTrue(state["connected"])
        self.assertEqual(state["port"], "Felucca")
        self.assertEqual(state["identity"], "FM-1_9530")

    def test_probe_numeric_identity_on_chosen_port(self):
        evs = self.run_ok("probe", ["--dry-run", "--mock-rom-disks", "0", "--mock-ports",
                                    "MIDIIN2 (Device) 0", "--mock-identity", "FM-1_5000901",
                                    "--port", "MIDIIN2 (Device)"])
        self.assertEqual(first(evs, "device_state")["identity"], "FM-1_5000901")

    def test_probe_without_device(self):
        evs = self.run_ok("probe", ["--dry-run", "--mock-rom-disks", "0", "--mock-ports", ""],
                          code=1)
        self.assertFalse(first(evs, "device_state")["connected"])
        self.assertEqual(first(evs, "step_failed")["code"], "NO_DEVICE")

    def test_probe_several_rom_disks(self):
        evs = self.run_ok("probe", ["--dry-run", "--mock-rom-disks", "2"], code=1)
        self.assertEqual(first(evs, "step_failed")["code"], "MULTIPLE_DEVICES")

    def test_find_counts(self):
        self.assertEqual(first(self.run_ok("find", ["--dry-run"]), "step_succeeded")["count"], 1)
        for n, code in ((0, "NO_DEVICE"), (2, "MULTIPLE_DEVICES")):
            evs = self.run_ok("find", ["--dry-run", "--mock-rom-disks", str(n)], code=1)
            self.assertEqual(first(evs, "step_failed")["code"], code)

    def test_softkey_needs_uboot(self):
        args = ["--dry-run", "--port", "Felucca", "--mock-ports", "Felucca 0",
                "--mock-identity", "FM-1_9530"]
        evs = self.run_ok("softkey", args, answers={"UBOOT": "UBOOT"})
        self.assertEqual(first(evs, "identity")["identity"], "FM-1_9530")
        self.assertIn("key_sent", names(evs))
        evs = self.run_ok("softkey", args, answers={"UBOOT": "uboot"}, code=1)
        self.assertNotIn("key_sent", names(evs))
        self.assertEqual(first(evs, "step_failed")["code"], "CANCELLED")

    def test_restore(self):
        pkg = write_package(self.tmp)
        evs = self.run_ok("restore", [str(pkg), "--dry-run", "--backup-dir",
                                      str(self.tmp / "bk")], answers={"WRITE": "WRITE"})
        order = [(e["event"], e.get("step")) for e in evs if e["event"].startswith("step_")]
        succeeded = [s for ev_name, s in order if ev_name == "step_succeeded"]
        self.assertEqual(succeeded, ["package", "bootloader", "chip", "backup", "confirm",
                                     "write", "verify"])
        req = first(evs, "confirmation_required")
        self.assertEqual(req["summary"]["start"], "0x4000")
        self.assertEqual(req["summary"]["end"], "0x92FFF")
        verify = first(evs, "step_succeeded", step="verify")
        self.assertTrue(verify["byte_identical"])
        backup = first(evs, "step_succeeded", step="backup")
        self.assertTrue(backup["reads_identical"])
        self.assertTrue(Path(backup["path"]).is_file())
        phases = {e["phase"] for e in evs if e["event"] == "progress"}
        self.assertEqual(phases, {"read1", "read2", "erase", "write", "verify"})
        # write_started comes after the confirmation, never before.
        self.assertLess(names(evs).index("confirmation_result"), names(evs).index("write_started"))

    def test_restore_unequal_backup_never_asks_write(self):
        pkg = write_package(self.tmp)
        evs = self.run_ok("restore", [str(pkg), "--dry-run", "--mock-flaky", "read",
                                      "--backup-dir", str(self.tmp / "bk")], code=1)
        self.assertEqual(first(evs, "step_failed")["code"], "BACKUP_READS_DIFFER")
        self.assertNotIn("confirmation_required", names(evs))
        self.assertNotIn("write_started", names(evs))

    def test_restore_wrong_chip(self):
        pkg = write_package(self.tmp)
        evs = self.run_ok("restore", [str(pkg), "--dry-run", "--mock-chip-key", "0x1234",
                                      "--backup-dir", str(self.tmp / "bk")], code=1)
        self.assertEqual(first(evs, "step_failed")["code"], "CHIP_MISMATCH")
        self.assertNotIn("backup", [e.get("step") for e in evs])

    def test_restore_not_verified_is_danger(self):
        pkg = write_package(self.tmp)
        evs = self.run_ok("restore", [str(pkg), "--dry-run", "--mock-flaky", "write-always",
                                      "--backup-dir", str(self.tmp / "bk")],
                          answers={"WRITE": "WRITE"}, code=3)
        fail = first(evs, "step_failed")
        self.assertEqual((fail["step"], fail["code"]), ("verify", "WRITE_NOT_VERIFIED"))
        self.assertTrue(fail["danger"])
        self.assertIn("Do NOT power off", fail["danger_text"])
        self.assertNotIn(("step_succeeded", "verify"),
                         [(e["event"], e.get("step")) for e in evs])

    def test_events_mode_refuses_overrides(self):
        pkg = write_package(self.tmp)
        for extra in (["--yes"], ["--i-know"], ["--force-write"],
                      ["--device", r"\\.\PhysicalDrive1"]):
            with self.subTest(extra=extra):
                res = subprocess.run([sys.executable, str(worker.RECOVERY_SCRIPT), "restore",
                                      str(pkg), "--dry-run", "--events", "--log-dir",
                                      str(self.tmp / "logs")] + extra,
                                     capture_output=True, text=True, cwd=self.tmp)
                self.assertEqual(res.returncode, 2)
                evs = [events.parse_event(l) for l in res.stdout.splitlines()]
                self.assertEqual(first(evs, "step_failed")["code"], "FORBIDDEN_OPTION")


class PortabilityTests(unittest.TestCase):
    def test_installer_folder_runs_from_a_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "somewhere else" / "INSTALLER"
            ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
            for part in ("app", "motor"):
                shutil.copytree(INSTALLER / part, copy / part, ignore=ignore)
            res = subprocess.run([sys.executable, str(copy / "motor" / "fm1_unbrick.py"),
                                  "doctor", "--dry-run", "--events", "--log-dir",
                                  str(Path(tmp) / "logs")],
                                 capture_output=True, text=True, cwd=tmp)
            evs = [events.parse_event(l) for l in res.stdout.splitlines()]
            self.assertEqual(res.returncode, 0, res.stderr)
            # pins.json is checked against the files next to it: those of the copy.
            checks = {c["name"]: c for c in first(evs, "environment")["checks"]}
            self.assertTrue(checks["pins"]["ok"])
            self.assertTrue(checks["jl-uboot-tool"]["ok"])


if __name__ == "__main__":
    unittest.main()
