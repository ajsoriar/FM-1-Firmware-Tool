"""Tkinter/ttk window: shows the controller's state and forwards user actions.

Nothing here decides a row's colour: refresh() draws what the controller holds,
and the controller changes it only on recovery-tool events.
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import controller as c
from . import dnd_win, elevation

RESOURCES = Path(__file__).resolve().parent / "resources"
ICON_CONNECTED = RESOURCES / "fm1_connected.ico"
ICON_DISCONNECTED = RESOURCES / "fm1_disconnected.ico"

STATUS_COLOR = {c.PENDING: "#6b7280", c.RUNNING: "#1d4ed8", c.WAITING_USER: "#b45309",
                c.PASS: "#15803d", c.FAIL: "#b91c1c"}
STATUS_SYMBOL = {c.PENDING: "○", c.RUNNING: "▶", c.WAITING_USER: "⚠", c.PASS: "✔", c.FAIL: "✖"}
NOTICE_STYLE = {"danger": ("#b91c1c", "#ffffff"), "critical": ("#f59e0b", "#111827"),
                "warn": ("#fde68a", "#111827"), "ok": ("#dcfce7", "#14532d")}
AUTO_PORT = "Automatic (FM-1 / Felucca)"
POLL_MS = 40


class ConfirmDialog(tk.Toplevel):
    """Shows the recovery tool's summary and asks for the exact phrase it requested."""

    def __init__(self, app, event):
        super().__init__(app.root)
        self.app, self.event = app, event
        phrase = event["phrase"]
        self.title("Confirm %s" % phrase)
        self.transient(app.root)
        self.resizable(False, False)
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        text = "\n".join(app.controller.confirmation_lines(event))
        ttk.Label(body, text=text, justify="left", font=("Consolas", 10)).pack(anchor="w")
        ttk.Label(body, text="Type %s to continue. Any other response cancels."
                  % phrase, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(12, 4))
        self.var = tk.StringVar()
        self.var.trace_add("write", self._typed)
        self.entry = ttk.Entry(body, textvariable=self.var, width=24, font=("Consolas", 12))
        self.entry.pack(anchor="w")
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(14, 0))
        self.ok = ttk.Button(buttons, text="Confirm %s" % phrase, command=self._confirm,
                             state="disabled")
        self.ok.pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="right", padx=8)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Return>", lambda e: self._confirm())
        self.bind("<Escape>", lambda e: self._cancel())
        self.entry.focus_set()
        self.grab_set()

    def _typed(self, *_):
        self.ok.configure(state="normal" if self.var.get().strip() == self.event["phrase"]
                          else "disabled")

    def _confirm(self):
        if str(self.ok["state"]) == "disabled":
            return
        self._answer(self.var.get())

    def _cancel(self):
        self._answer("")

    def _answer(self, text):
        self.app.controller.answer_confirmation(text)
        self.app.dialog = None
        self.grab_release()
        self.destroy()


class InstallerApp:
    def __init__(self, root, controller, app_args=(), drop_enabled=True):
        self.root, self.controller = root, controller
        self.app_args = list(app_args)
        self.dialog = None
        self._dialog_for = None
        self._log_shown = 0
        self._icon = None
        self._dropped = []
        root.title("FM-1 Firmware Tool" + (" (simulation)" if controller.simulation else ""))
        scale = root.winfo_fpixels("1i") / 96.0          # geometry is in pixels, fonts in points
        height = min(int(900 * scale), root.winfo_screenheight() - int(80 * scale))
        root.geometry("%dx%d" % (int(1060 * scale), height))
        root.minsize(int(860 * scale), int(600 * scale))
        root.configure(background="#f3f4f6")
        self._style()
        self._build()
        controller.subscribe(self.refresh)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.drop_enabled = drop_enabled and dnd_win.enable_file_drop(root, self._dropped)
        self.drop_hint.configure(text="or drag a .fwsc file into this window"
                                 if self.drop_enabled else
                                 "or drop it onto run_gui.bat")
        self.refresh()
        self._after = root.after(POLL_MS, self._poll)

    # ------------------------------------------------------------------ layout

    def _style(self):
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Hw.TLabel", font=("Segoe UI", 13, "bold"))
        style.configure("Row.TLabel", font=("Segoe UI", 10))
        style.configure("RowTitle.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Head.TLabel", font=("Segoe UI", 9, "bold"), foreground="#374151")
        style.configure("Install.TButton", font=("Segoe UI", 11, "bold"))

    def _build(self):
        root = self.root
        outer = ttk.Frame(root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        head = ttk.Frame(outer)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(1, weight=1)
        ttk.Label(head, text="FM-1 Firmware Tool", style="Title.TLabel").grid(
            row=0, column=0, sticky="w")
        if self.controller.simulation:
            tk.Label(head, text="SIMULATION: mock recovery device (--dry-run); "
                                "no real hardware is opened", bg="#7c3aed", fg="white",
                     font=("Segoe UI", 9, "bold"), padx=8, pady=2).grid(row=0, column=1,
                                                                          sticky="e")

        hw = ttk.Frame(outer, padding=(0, 10))
        hw.grid(row=1, column=0, sticky="ew")
        self.hw_canvas = tk.Canvas(hw, width=56, height=56, highlightthickness=0,
                                   background="#f3f4f6")
        self.hw_canvas.grid(row=0, column=0, rowspan=2, padx=(0, 10))
        self.hw_text = ttk.Label(hw, style="Hw.TLabel")
        self.hw_text.grid(row=0, column=1, sticky="w")
        self.hw_sub = ttk.Label(hw, foreground="#374151")
        self.hw_sub.grid(row=1, column=1, sticky="w")

        fw = ttk.LabelFrame(outer, text="Firmware", padding=8)
        fw.grid(row=2, column=0, sticky="ew")
        fw.columnconfigure(0, weight=1)
        self.pkg_var = tk.StringVar()
        ttk.Entry(fw, textvariable=self.pkg_var, state="readonly").grid(row=0, column=0,
                                                                         sticky="ew")
        self.btn_pkg = ttk.Button(fw, text="Choose .fwsc...", command=self.choose_package)
        self.btn_pkg.grid(row=0, column=1, padx=(8, 0))
        self.drop_hint = ttk.Label(fw, foreground="#6b7280")
        self.drop_hint.grid(row=1, column=0, sticky="w")
        self.pkg_warn = tk.Label(fw, fg="#92400e", bg="#fef3c7", justify="left", anchor="w")
        self.pkg_warn.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        conn = ttk.LabelFrame(outer, text="Connection and backups", padding=8)
        conn.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        conn.columnconfigure(5, weight=1)
        ttk.Label(conn, text="MIDI port:").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar(value=AUTO_PORT)
        self.port_box = ttk.Combobox(conn, textvariable=self.port_var, width=34,
                                     state="readonly", values=[AUTO_PORT])
        self.port_box.grid(row=0, column=1, padx=(4, 8))
        self.btn_detect = ttk.Button(conn, text="Detect FM-1", command=self.detect)
        self.btn_detect.grid(row=0, column=2)
        self.ports_info = ttk.Label(conn, foreground="#374151")
        self.ports_info.grid(row=0, column=3, columnspan=3, sticky="w", padx=(10, 0))
        ttk.Label(conn, text="Backups:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.backup_var = tk.StringVar()
        ttk.Entry(conn, textvariable=self.backup_var, state="readonly").grid(
            row=1, column=1, columnspan=5, sticky="ew", padx=(4, 8), pady=(6, 0))
        self.btn_backup = ttk.Button(conn, text="Change...", command=self.choose_backup_dir)
        self.btn_backup.grid(row=1, column=6, pady=(6, 0))
        ttk.Button(conn, text="Open", command=lambda: self._open(
            self.controller.paths.backup_dir)).grid(row=1, column=7, padx=(4, 0), pady=(6, 0))

        steps = ttk.LabelFrame(outer, text="Steps", padding=(8, 4))
        steps.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        outer.rowconfigure(4, weight=1)
        steps.columnconfigure(4, weight=1)
        self.steps_frame = steps
        for col, text in enumerate(("", "Step", "Status", "Time", "Details")):
            ttk.Label(steps, text=text, style="Head.TLabel").grid(row=0, column=col, sticky="w",
                                                                  padx=4)
        self.row_widgets = {}
        for i, (step, title) in enumerate(c.ROWS, start=1):
            sym = tk.Label(steps, font=("Segoe UI Symbol", 12), width=2)
            name = ttk.Label(steps, text="%d. %s" % (i, title), style="RowTitle.TLabel")
            status = tk.Label(steps, font=("Segoe UI", 10, "bold"), anchor="w", width=20)
            when = ttk.Label(steps, style="Row.TLabel", width=9)
            detail = ttk.Label(steps, style="Row.TLabel", wraplength=500, justify="left")
            for col, w in enumerate((sym, name, status, when, detail)):
                w.grid(row=i, column=col, sticky="nw", padx=4, pady=1)
            self.row_widgets[step] = (sym, status, when, detail)
        steps.bind("<Configure>", lambda e: self.root.after_idle(self._wrap_details))
        self.progress = ttk.Progressbar(steps, maximum=100)
        self.progress.grid(row=len(c.ROWS) + 1, column=0, columnspan=5, sticky="ew",
                           pady=(8, 0))
        self.progress_label = ttk.Label(steps, foreground="#374151")
        self.progress_label.grid(row=len(c.ROWS) + 2, column=0, columnspan=5, sticky="w")

        self.notice = tk.Label(outer, justify="left", anchor="w", padx=10, pady=6,
                               font=("Segoe UI", 10, "bold"), wraplength=980)
        self.notice.grid(row=5, column=0, sticky="ew", pady=(8, 0))

        actions = ttk.Frame(outer)
        actions.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        actions.columnconfigure(5, weight=1)
        self.btn_install = ttk.Button(actions, text="Install firmware", style="Install.TButton",
                                      command=self.install)
        self.btn_install.grid(row=0, column=0)
        self.btn_cancel = ttk.Button(actions, text="Cancel", command=self.cancel)
        self.btn_cancel.grid(row=0, column=1, padx=(8, 0))
        self.btn_boot = ttk.Button(actions, text="Check boot", command=self.check_boot)
        self.btn_boot.grid(row=0, column=2, padx=(8, 0))
        self.btn_admin = ttk.Button(actions, text="Relaunch as administrator",
                                    command=self.relaunch_admin)
        self.btn_admin.grid(row=0, column=3, padx=(8, 0))
        ttk.Button(actions, text="Open log", command=self.open_log).grid(row=0, column=4,
                                                                          padx=(8, 0))
        self.blockers = ttk.Label(actions, foreground="#6b7280")
        self.blockers.grid(row=0, column=5, sticky="w", padx=(12, 0))
        self.show_log = tk.BooleanVar(value=False)
        ttk.Checkbutton(actions, text="Show log", variable=self.show_log,
                        command=self._toggle_log).grid(row=0, column=6, sticky="e")

        self.log_frame = ttk.LabelFrame(outer, text="Recovery log", padding=4)
        self.log_frame.grid(row=7, column=0, sticky="ew", pady=(8, 0))
        self.log = ScrolledText(self.log_frame, height=8, font=("Consolas", 9),
                                state="disabled", wrap="none")
        self.log.pack(fill="both", expand=True)
        self.log_frame.grid_remove()

    def _toggle_log(self):
        if self.show_log.get():
            self.log_frame.grid()
        else:
            self.log_frame.grid_remove()

    def _wrap_details(self):
        """Wrap the detail column at the width it actually has (after layout)."""
        _, _, _, first_detail = self.row_widgets["package"]
        frame_width, x = self.steps_frame.winfo_width(), first_detail.winfo_x()
        if frame_width <= 1 or not x:
            return
        width = max(200, frame_width - x - 24)
        if width != int(str(first_detail.cget("wraplength")) or 0):
            for _, _, _, detail in self.row_widgets.values():
                detail.configure(wraplength=width)
        self.notice.configure(wraplength=max(300, frame_width - 20))

    # ------------------------------------------------------------------ drawing

    def _draw_hw(self, connected):
        cv = self.hw_canvas
        cv.delete("all")
        cv.create_line(4, 30, 16, 30, width=4, fill="#374151")                 # cable
        cv.create_rectangle(16, 20, 34, 40, fill="#374151", outline="")         # plug body
        cv.create_rectangle(34, 23, 46, 37, fill="#9ca3af", outline="")         # connector
        cv.create_rectangle(37, 26, 40, 29, fill="#f3f4f6", outline="")
        cv.create_rectangle(41, 26, 44, 29, fill="#f3f4f6", outline="")
        color = "#15803d" if connected else "#b91c1c"
        cv.create_oval(30, 30, 54, 54, fill=color, outline="white", width=2)    # badge
        if connected:
            cv.create_line(36, 42, 41, 47, 48, 37, fill="white", width=3)
        else:
            cv.create_line(37, 37, 47, 47, fill="white", width=3)
            cv.create_line(47, 37, 37, 47, fill="white", width=3)

    def _set_window_icon(self, connected):
        path = ICON_CONNECTED if connected else ICON_DISCONNECTED
        if self._icon == path or sys.platform != "win32" or not path.is_file():
            return
        try:
            self.root.iconbitmap(default=str(path))
            self._icon = path
        except tk.TclError:
            pass

    def refresh(self):
        ctl = self.controller
        dev = ctl.device or {}
        connected = ctl.connected
        self._draw_hw(connected)
        self._set_window_icon(connected)
        when = ctl.last_check.strftime("%H:%M:%S") if ctl.last_check else None
        if connected:
            self.hw_text.configure(text="✔ FM-1 connected", foreground="#15803d")
            what = ("ROM mode, %d %s disk" % (dev["rom_disks"], dev.get("rom_model"))
                    if dev.get("mode") == "rom"
                    else '%s on port "%s"' % (dev.get("identity"), dev.get("port")))
            self.hw_sub.configure(text="%s · checked at %s" % (what, when))
        else:
            self.hw_text.configure(text="✖ FM-1 not detected", foreground="#b91c1c")
            self.hw_sub.configure(text=("%s · checked at %s" % (dev.get("reason") or "",
                                                                     when)) if when else
                                  'Connect the FM-1 and click "Detect FM-1".')

        self.pkg_var.set(str(ctl.package_path) if ctl.package_path else "")
        warns = ctl.package_warnings()
        if warns:
            self.pkg_warn.configure(text="⚠ " + "\n⚠ ".join(warns))
            self.pkg_warn.grid()
        else:
            self.pkg_warn.grid_remove()

        pairs = list(dev.get("midi_pairs") or [])
        self.port_box.configure(values=[AUTO_PORT] + pairs)
        if dev:
            self.ports_info.configure(text="WL82 UBOOT1.00 disks: %d | MIDI ports: %s"
                                      % (dev.get("rom_disks", 0), ", ".join(pairs) or "none"))
        self.backup_var.set(str(ctl.paths.backup_dir))

        running = None
        for step, (sym, status, when_lbl, detail) in self.row_widgets.items():
            row = ctl.rows[step]
            color = STATUS_COLOR[row.status]
            sym.configure(text=STATUS_SYMBOL[row.status], fg=color)
            status.configure(text=row.status_text, fg=color)
            when_lbl.configure(text=row.time)
            detail.configure(text=row.detail)
            if row.status == c.RUNNING and row.progress is not None:
                running = row
        if running:
            self.progress.configure(value=running.progress)
            self.progress_label.configure(text="%s: %d %%" % (running.title, running.progress))
        else:
            self.progress.configure(value=0)
            self.progress_label.configure(text="")

        if ctl.notice:
            bg, fg = NOTICE_STYLE[ctl.notice[0]]
            self.notice.configure(text=ctl.notice[1], bg=bg, fg=fg)
            self.notice.grid()
        else:
            self.notice.grid_remove()

        idle = not ctl.busy
        self.btn_install.configure(state="normal" if ctl.can_install() else "disabled")
        self.btn_cancel.configure(state="normal" if ctl.can_cancel() else "disabled")
        self.btn_boot.configure(state="normal" if ctl.can_check_boot() else "disabled")
        for b in (self.btn_pkg, self.btn_detect, self.btn_backup):
            b.configure(state="normal" if idle else "disabled")
        self.port_box.configure(state="readonly" if idle else "disabled")
        env = ctl.environment or {}
        need_admin = (ctl.simulation is None and env and not env.get("admin")
                      and sys.platform == "win32")
        if need_admin:
            self.btn_admin.grid()
        else:
            self.btn_admin.grid_remove()
        blockers = ctl.install_blockers() if not ctl.can_install() else []
        self.blockers.configure(text=("Installation disabled: " + "; ".join(blockers))
                                if blockers else "")

        if len(ctl.log_lines) > self._log_shown:
            self.log.configure(state="normal")
            self.log.insert("end", "\n".join(ctl.log_lines[self._log_shown:]) + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
            self._log_shown = len(ctl.log_lines)

        pending = ctl.pending_confirmation
        if pending is not None and self.dialog is None and self._dialog_for is not pending:
            self._dialog_for = pending
            self.dialog = ConfirmDialog(self, pending)
        elif pending is None and self.dialog is not None:
            self.dialog.grab_release()
            self.dialog.destroy()
            self.dialog = None

    # ------------------------------------------------------------------ actions

    def _poll(self):
        if self._dropped:
            batches, self._dropped[:] = list(self._dropped), []
            self.on_drop([p for batch in batches for p in batch])
        self.controller.pump()
        self._after = self.root.after(POLL_MS, self._poll)

    def on_drop(self, paths):
        fwsc = [p for p in paths if p.lower().endswith(".fwsc")]
        if len(fwsc) != 1 or len(paths) != 1:
            messagebox.showwarning("FM-1 Firmware Tool", "Drop exactly one .fwsc file.")
            return
        self._use_package(fwsc[0])

    def choose_package(self):
        path = filedialog.askopenfilename(title="Choose FM-1 firmware",
                                          filetypes=[("Firmware FM-1", "*.fwsc")])
        if path:
            self._use_package(path)

    def _use_package(self, path):
        if not self.controller.choose_package(path):
            messagebox.showinfo("FM-1 Firmware Tool", "A task is running for the FM-1. "
                                "Choose firmware when it finishes.")

    def choose_backup_dir(self):
        path = filedialog.askdirectory(title="Backup folder",
                                       initialdir=str(self.controller.paths.backup_dir))
        if path:
            self.controller.set_backup_dir(path)

    def detect(self):
        port = self.port_var.get()
        self.controller.detect(None if port == AUTO_PORT else port)

    def install(self):
        if self.controller.can_install():
            self.controller.start_install()

    def cancel(self):
        if self.dialog is not None:
            self.dialog._cancel()
        else:
            self.controller.cancel()

    def check_boot(self):
        if self.controller.can_check_boot():
            self.controller.check_boot()

    def relaunch_admin(self):
        if self.controller.busy:
            return
        if elevation.relaunch_as_admin(self.app_args):
            self.root.destroy()
        else:
            messagebox.showerror("FM-1 Firmware Tool", "Could not relaunch as administrator.")

    def open_log(self):
        log = self.controller.last_log or self.controller.job_log
        self._open(Path(log) if log else self.controller.paths.log_dir)

    def _open(self, path):
        path = Path(path)
        if not path.exists():
            path = path.parent
        if not path.exists():
            messagebox.showinfo("FM-1 Firmware Tool", "Does not exist yet: %s" % path)
            return
        if sys.platform == "win32":
            os.startfile(str(path))

    def on_close(self):
        ctl = self.controller
        if not ctl.can_close():
            messagebox.showwarning("FM-1 Firmware Tool", "Write in progress: the window cannot be closed. "
                                   "Do not disconnect or power off the FM-1.")
            return
        if ctl.danger:
            if not messagebox.askyesno("FM-1 Firmware Tool", "The firmware region has NOT been "
                                       "verified. Do NOT power off or disconnect the FM-1. "
                                       "Close the window anyway?"):
                return
        if ctl.busy:
            if not messagebox.askyesno("FM-1 Firmware Tool", "A task is running but has not started writing. "
                                       "Cancel it and close?"):
                return
            ctl.cancel()
        self.root.after_cancel(self._after)
        self.root.destroy()
