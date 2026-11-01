"""Console front end: `python -m app install|check|detect` as a real process.

The recovery tool runs with its mocks (--simulate = --dry-run); answers come
from stdin as a person (or a script) would type them.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import INSTALLER, write_capture, write_package

from app import cli


def fm1(args, stdin="", home=None):
    env = dict(os.environ, PYTHONPATH=str(INSTALLER), PYTHONIOENCODING="utf-8")
    if home:                                   # keeps simulated backups out of Documents
        env["USERPROFILE"] = env["HOME"] = str(home)
    return subprocess.run([sys.executable, "-m", "app"] + args, input=stdin,
                          capture_output=True, text=True, encoding="utf-8", env=env,
                          cwd=home or INSTALLER, timeout=300)


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.pkg = write_package(self.tmp, identity="FM-1_9530")

    def tearDown(self):
        self._tmp.cleanup()

    def install(self, *extra, stdin="UBOOT\nWRITE\n\n"):
        return fm1(["install", str(self.pkg), "--simulate"] + list(extra), stdin, self.tmp)

    def test_install_with_confirmations(self):
        res = self.install()
        self.assertEqual(res.returncode, cli.EXIT_OK, res.stdout + res.stderr)
        out = res.stdout
        # Summary shown before each question, then the rows in order.
        ask_uboot = out.index("Type UBOOT to continue (any other response cancels)")
        ask_write = out.index("Type WRITE to continue (any other response cancels)")
        self.assertLess(out.index("The ROM key will be sent"), ask_uboot)
        self.assertLess(out.index("Target     0x4000..0x92FFF"), ask_write)
        self.assertLess(ask_write, out.index("Do NOT disconnect"))
        self.assertLess(out.index("Do NOT disconnect"), out.index("10. Read verified"))
        self.assertIn("[ OK ] 10. Read verified", out)
        self.assertIn("[ OK ] 11. Boot confirmed", out)
        self.assertTrue((self.tmp / "Documents" / "FM1-Installer").exists()
                        or (self.tmp / "FM1-Installer").exists())

    def test_relative_path_from_the_current_folder(self):
        res = fm1(["check", self.pkg.name, "--simulate"], home=self.tmp)
        self.assertEqual(res.returncode, cli.EXIT_OK, res.stdout + res.stderr)
        self.assertIn("[ OK ]  1. Firmware selected", res.stdout)

    def test_wrong_phrase_cancels(self):
        res = self.install(stdin="UBOOT\nwrite\n")
        self.assertEqual(res.returncode, cli.EXIT_FAIL)
        self.assertIn("Cancelled. Nothing was written", res.stdout)
        self.assertNotIn("[ OK ]  9.", res.stdout)

    def test_no_answers_cancels_before_the_key(self):
        res = self.install(stdin="")
        self.assertEqual(res.returncode, cli.EXIT_FAIL)
        self.assertIn("(no response)", res.stdout)
        self.assertNotIn("Type WRITE", res.stdout)

    def test_powershell_byte_order_mark_is_not_part_of_the_answer(self):
        res = self.install(stdin="﻿UBOOT\nWRITE\n\n")
        self.assertEqual(res.returncode, cli.EXIT_OK, res.stdout)

    def test_unequal_backup_never_asks_write(self):
        res = self.install("--simulate-fault", "read")
        self.assertEqual(res.returncode, cli.EXIT_FAIL)
        self.assertIn("do NOT match", res.stdout)
        self.assertNotIn("Type WRITE", res.stdout)

    def test_unverified_write_is_danger(self):
        res = self.install("--simulate-fault", "write-always", "--simulate-start", "rom",
                           stdin="WRITE\n")
        self.assertEqual(res.returncode, cli.EXIT_DANGER)
        self.assertIn("Do NOT power off", res.stdout)
        self.assertNotIn("Press Enter when", res.stdout)      # no power-cycle prompt

    def test_no_fm1_detected(self):
        res = self.install("--simulate-start", "none")
        self.assertEqual(res.returncode, cli.EXIT_START)
        self.assertIn("FM-1 not detected", res.stdout)

    def test_catalog_mismatch_never_installs(self):
        capture = self.tmp / "capture"
        (capture / "firmwares").mkdir(parents=True)
        pkg = write_package(capture / "firmwares")
        write_capture(capture, pkg, status="hash_mismatch")
        res = fm1(["install", str(pkg), "--simulate"], "UBOOT\nWRITE\n", self.tmp)
        self.assertEqual(res.returncode, cli.EXIT_START)
        self.assertNotIn("FM-1 detection", res.stdout)

    def test_not_a_fwsc(self):
        other = self.tmp / "firmware.bin"
        other.write_bytes(b"x")
        self.assertEqual(fm1(["install", str(other)], home=self.tmp).returncode, cli.EXIT_START)

    def test_skip_boot_check(self):
        res = self.install("--no-boot-check", stdin="UBOOT\nWRITE\n")
        self.assertEqual(res.returncode, cli.EXIT_OK)
        self.assertIn("Manually", res.stdout)
        self.assertNotIn("Boot confirmed: ", res.stdout)

    def test_no_option_skips_a_confirmation(self):
        for flag in ("--yes", "--i-know", "--force-write"):
            with self.subTest(flag=flag):
                res = fm1(["install", str(self.pkg), flag], home=self.tmp)
                self.assertEqual(res.returncode, 2)               # argparse: unknown option
                self.assertIn("unrecognized arguments", res.stderr)


if __name__ == "__main__":
    unittest.main()
