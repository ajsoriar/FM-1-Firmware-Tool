"""What the window shows for each state (needs a display; skipped without Tk)."""

from __future__ import annotations

import ctypes
import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path

from support import ScriptedLauncher, write_package
from test_controller_mock import Script, drain

from app import __version__, controller as c, ui


def post_drop(hwnd, paths):
    """Post the WM_DROPFILES that Explorer sends: a DROPFILES block in global memory."""
    from ctypes import wintypes
    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = k32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    u32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    names = ("\0".join(paths) + "\0\0").encode("utf-16-le")
    data = (20).to_bytes(4, "little") + bytes(12) + (1).to_bytes(4, "little") + names
    handle = k32.GlobalAlloc(0x0042, len(data))                 # GMEM_MOVEABLE | ZEROINIT
    ctypes.memmove(k32.GlobalLock(handle), data, len(data))
    k32.GlobalUnlock(handle)
    assert u32.PostMessageW(hwnd, 0x0233, handle, 0)            # WM_DROPFILES


class UiStateTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest("no display for Tk: %s" % e)
        self.root.withdraw()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.script = Script()
        self.launcher = ScriptedLauncher(self.script)
        self.ctl = c.Controller(launcher=self.launcher, paths=c.Paths(self.tmp, self.tmp / "bk"))
        self.app = ui.InstallerApp(self.root, self.ctl, drop_enabled=False)
        self.ctl.start(write_package(self.tmp))
        drain(self.ctl)

    def tearDown(self):
        if hasattr(self, "app"):
            if self.app.dialog is not None:
                self.app.dialog.destroy()
            self.root.after_cancel(self.app._after)
            self.root.destroy()
            self._tmp.cleanup()

    def text(self, widget):
        return str(widget.cget("text"))

    def state(self, button):
        return str(button.cget("state"))

    def test_without_hardware(self):
        self.assertIn("FM-1 not detected", self.text(self.app.hw_text))
        self.assertIn("✖", self.text(self.app.hw_text))
        self.assertEqual(self.state(self.app.btn_install), "disabled")
        self.assertIn("FM-1 not detected", self.text(self.app.blockers))
        sym, status, _, _ = self.app.row_widgets["device"]
        self.assertEqual(self.text(status), "Pending")
        self.assertEqual(self.text(sym), "○")

    def test_application_version_is_shown_in_window_title(self):
        self.assertIn(__version__, self.root.title())

    def test_detected_through_felucca_alias(self):
        self.ctl.detect()
        drain(self.ctl)
        self.assertIn("FM-1 connected", self.text(self.app.hw_text))
        uboot_summary = self.text(self.app.uboot_summary)
        self.assertIn('MIDI port "Felucca"', uboot_summary)
        self.assertIn("FM-1_9530", uboot_summary)
        self.assertIn("WL82 UBOOT1.00", uboot_summary)
        self.assertIn("Power-cycling returns", uboot_summary)
        sub = self.text(self.app.hw_sub)
        self.assertIn("Felucca", sub)
        self.assertIn("FM-1_9530", sub)
        self.assertIn("checked at", sub)
        self.assertEqual(self.state(self.app.btn_install), "normal")
        self.assertIn("Felucca", self.app.port_box.cget("values"))

    def test_every_status_has_text_symbol_and_colour(self):
        for status in (c.PENDING, c.RUNNING, c.WAITING_USER, c.PASS, c.FAIL):
            with self.subTest(status=status):
                self.ctl.rows["backup"].status = status
                self.app.refresh()
                sym, label, _, _ = self.app.row_widgets["backup"]
                self.assertEqual(self.text(label), c.STATUS_TEXT[status])
                self.assertEqual(self.text(sym), ui.STATUS_SYMBOL[status])
                self.assertEqual(str(label.cget("fg")), ui.STATUS_COLOR[status])

    def test_write_confirmation_dialog(self):
        self.ctl.detect()
        drain(self.ctl)
        self.app.uboot_auto_var.set(False)
        self.ctl.start_install()
        drain(self.ctl)
        self.assertIsNotNone(self.app.dialog)                         # UBOOT
        self.app.dialog.var.set("UBOOT")
        self.app.dialog._confirm()
        drain(self.ctl)
        dialog = self.app.dialog                                      # WRITE
        self.assertIsNotNone(dialog)
        self.assertEqual(dialog.event["phrase"], "WRITE")
        dialog.var.set("write")
        self.assertEqual(str(dialog.ok.cget("state")), "disabled")
        dialog.var.set("WRITE")
        self.assertEqual(str(dialog.ok.cget("state")), "normal")
        dialog._cancel()
        drain(self.ctl)
        self.assertEqual(self.launcher.jobs[-1].answers, [""])
        self.assertIsNone(self.app.dialog)
        _, status, _, detail = self.app.row_widgets["confirm"]
        self.assertEqual(self.text(status), "Error")
        self.assertIn("Nothing was written", self.text(detail))

    def test_uboot_confirmation_is_sent_automatically_by_default(self):
        self.ctl.detect()
        drain(self.ctl)
        self.ctl.start_install()
        drain(self.ctl)
        self.assertTrue(self.app.uboot_auto_var.get())
        softkey_job = next(job for job in self.launcher.jobs if "softkey" in job.argv)
        self.assertEqual(softkey_job.answers, ["UBOOT"])
        self.assertIsNotNone(self.app.dialog)                         # WRITE remains explicit
        self.assertEqual(self.app.dialog.event["phrase"], "WRITE")
        self.assertTrue(self.app.uboot_frame.winfo_manager())
        self.assertIn("does not write firmware", self.text(self.app.uboot_warning))

    def test_write_confirmation_is_manual_by_default(self):
        self.ctl.detect()
        drain(self.ctl)
        self.assertFalse(self.app.write_auto_var.get())
        self.assertTrue(self.app.write_frame.winfo_manager())
        preview = self.text(self.app.write_summary)
        self.assertIn("test.fwsc", preview)
        self.assertIn("boot head 0x0000-0x3FFF is untouched", preview)
        self.assertIn("WITHOUT asking", self.text(self.app.write_warning))
        self.ctl.start_install()
        drain(self.ctl)
        self.assertIsNotNone(self.app.dialog)
        self.assertEqual(self.app.dialog.event["phrase"], "WRITE")
        self.assertEqual(self.launcher.jobs[-1].answers, [])

    def test_write_confirmation_is_sent_automatically_when_checked(self):
        self.ctl.detect()
        drain(self.ctl)
        self.app.write_auto_var.set(True)
        self.ctl.start_install()
        drain(self.ctl)
        restore_job = next(job for job in self.launcher.jobs if "restore" in job.argv)
        self.assertEqual(restore_job.answers, ["WRITE"])
        self.assertIsNone(self.app.dialog)
        summary = self.text(self.app.write_summary)                   # full technical summary
        self.assertIn("about to be WRITTEN", summary)
        self.assertIn("0x4000..0x92FFF", summary)
        self.assertIn("Do not disconnect", summary)
        _, status, _, _ = self.app.row_widgets["boot"]
        self.assertEqual(self.text(status), "Waiting for user")

    def test_during_write_cancel_is_disabled_and_warning_shown(self):
        self.ctl.job, self.ctl.job_kind = object(), "restore"
        self.ctl.handle("event", {"schema": 1, "event": "write_started", "start": "0x4000",
                                  "end": "0x92FFF"})
        self.assertEqual(self.state(self.app.btn_cancel), "disabled")
        self.assertIn("Do NOT disconnect", self.text(self.app.notice))
        self.assertTrue(self.app.notice.winfo_manager())

    @unittest.skipUnless(sys.platform == "win32", "WM_DROPFILES is Windows only")
    def test_file_dropped_on_the_window_is_chosen(self):
        root = tk.Toplevel(self.root)
        try:
            ctl = c.Controller(launcher=ScriptedLauncher(Script()),
                               paths=c.Paths(self.tmp, self.tmp / "bk"))
            app = ui.InstallerApp(root, ctl)
            self.assertTrue(app.drop_enabled)
            dropped = write_package(self.tmp, name="dropped.fwsc")
            post_drop(int(root.wm_frame(), 16), [str(dropped)])
            deadline = time.monotonic() + 5
            while ctl.package_path is None and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            drain(ctl)
            self.assertEqual(ctl.package_path, dropped)
            self.assertEqual(ctl.rows["package"].status, c.PASS)
            self.assertEqual(app.pkg_var.get(), str(dropped))
            root.after_cancel(app._after)
        finally:
            root.destroy()

    def test_boot_not_confirmed_until_checked(self):
        self.ctl.detect()
        drain(self.ctl)
        self.ctl.start_install()
        drain(self.ctl)
        self.app.dialog.var.set("WRITE")
        self.app.dialog._confirm()
        drain(self.ctl)
        _, status, _, _ = self.app.row_widgets["boot"]
        self.assertEqual(self.text(status), "Waiting for user")
        self.assertEqual(self.state(self.app.btn_boot), "normal")
        self.assertIn("Boot is not confirmed yet", self.text(self.app.notice))
        self.app.check_boot()
        drain(self.ctl)
        self.assertEqual(self.text(status), "Success")


if __name__ == "__main__":
    unittest.main()
