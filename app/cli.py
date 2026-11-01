"""Console front end:  fm1 install PACKAGE.fwsc  |  fm1 check PACKAGE.fwsc  |  fm1 detect

The same Controller as the window drives the same recovery-tool runs, so the
console gets the same checks, the same order and the same confirmations: UBOOT
and WRITE are read from stdin after the summary is printed, and the recovery
tool compares them with the exact phrase. There is no option to skip them;
without an answer (end of input) the installation is cancelled.

Exit codes: 0 firmware written and verified (or check/detect passed);
1 failed or cancelled, nothing left half-written; 2 could not start (usage,
package, environment, FM-1 not detected); 3 DANGER: the firmware region may be
incomplete, do not power off; 4 written and verified, but the boot check failed.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import controller as c

EXIT_OK, EXIT_FAIL, EXIT_START, EXIT_DANGER, EXIT_BOOT = 0, 1, 2, 3, 4
COMMANDS = ("install", "check", "detect")
FAULTS = ("read", "write", "write-always", "chip", "multi", "noanswer")
MARK = {c.PENDING: "[    ]", c.RUNNING: "[ .. ]", c.WAITING_USER: "[ ?? ]", c.PASS: "[ OK ]",
        c.FAIL: "[FAIL]"}


def add_simulation_args(ap):
    g = ap.add_argument_group("simulation (no hardware)")
    g.add_argument("--simulate", action="store_true",
                   help="full workflow using the recovery tool's mock device (--dry-run)")
    g.add_argument("--simulate-start", choices=("midi", "rom", "none"), default="midi",
                   help="FM-1 in normal firmware (MIDI), ROM mode, or absent")
    g.add_argument("--simulate-port", default="Felucca 0",
                   help="MIDI port name (defaults to the Felucca alias)")
    g.add_argument("--simulate-identity", default="FM-1_9530",
                   help="identity returned by the FM-1 before installation")
    g.add_argument("--simulate-fault", choices=FAULTS,
                   help="faulty read/write, foreign chip, multiple disks, or no response")


def simulation_from(args):
    if not args.simulate:
        return None
    return c.Simulation(mode=args.simulate_start, ports=(args.simulate_port,),
                        identity=args.simulate_identity, fault=args.simulate_fault)


def build_parser():
    ap = argparse.ArgumentParser(
        prog="fm1", description="FM-1 Firmware Tool console interface. With no subcommand "
        "(or with only a .fwsc file), the window opens.",
        epilog="Exit codes: 0 successful; 1 failed or cancelled; 2 could not start; "
               "3 DANGER, do not power off the FM-1; 4 written and verified but boot not "
               "confirmed.")
    sub = ap.add_subparsers(dest="command", metavar="COMMAND", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", help="FM-1 MIDI port (default: the only one named "
                                       "FM-1 or Felucca)")
    common.add_argument("--verbose", "-v", action="store_true",
                        help="also show recovery tool output")
    add_simulation_args(common)

    p = sub.add_parser("install", parents=[common], help="install a .fwsc on the FM-1")
    p.add_argument("package", help="path to the .fwsc file")
    p.add_argument("--backup-dir", help="backup folder (default "
                                        "Documents\\FM1-Installer\\backups)")
    p.add_argument("--no-boot-check", action="store_true",
                   help="exit after verification without waiting for the manual reboot")
    p = sub.add_parser("check", parents=[common],
                       help="inspect a .fwsc and the environment without accessing the FM-1")
    p.add_argument("package", help="path to the .fwsc file")
    sub.add_parser("detect", parents=[common], help="detect the FM-1 (read-only)")
    return ap


class ConsoleView:
    """Prints each row when its status or detail changes, and the notices."""

    def __init__(self, ctl, out, verbose=False):
        self.ctl, self.out, self.verbose = ctl, out, verbose
        self.seen = {}
        self.progress = {}
        self.log_shown = 0
        self.notice = None
        self.tty = hasattr(out, "isatty") and out.isatty()
        ctl.subscribe(self.render)

    def say(self, text=""):
        self.out.write(text + "\n")
        self.out.flush()

    def render(self):
        ctl = self.ctl
        if self.verbose:
            for line in ctl.log_lines[self.log_shown:]:
                self.say("    | " + line)
        self.log_shown = len(ctl.log_lines)
        for i, (step, title) in enumerate(c.ROWS, start=1):
            row = ctl.rows[step]
            if row.status == c.RUNNING and row.progress is not None:
                decile = row.progress // 10
                if self.progress.get(step) != decile:
                    self.progress[step] = decile
                    self.say("%s %2d. %-27s %s" % (MARK[row.status], i, title, row.detail))
                    self.seen[step] = (row.status, row.detail)
                continue
            key = (row.status, row.detail)
            if self.seen.get(step, (c.PENDING, "")) != key:
                self.seen[step] = key
                if row.status == c.PENDING:
                    continue
                detail = row.detail
                if step == "boot" and row.status == c.WAITING_USER:
                    detail = "Waiting for manual reboot (see instructions below)"
                lines = detail.splitlines() or [""]
                self.say("%s %2d. %-27s %s" % (MARK[row.status], i, title, lines[0]))
                for extra in lines[1:]:
                    self.say(" " * 38 + extra)
        if ctl.notice != self.notice:
            self.notice = ctl.notice
            if ctl.notice:
                level, text = ctl.notice
                if level == "danger":
                    bar = "!" * 72
                    self.say("\n" + bar + "\n" + text + "\n" + bar)
                else:
                    self.say(">>> " + text)


class ConsoleApp:
    def __init__(self, args, out=sys.stdout, inp=sys.stdin, controller=None):
        self.args, self.out, self.inp = args, out, inp
        paths = c.Paths.default(simulate=args.simulate)
        if getattr(args, "backup_dir", None):
            paths.backup_dir = Path(args.backup_dir)
        self.ctl = controller or c.Controller(paths=paths, simulation=simulation_from(args))
        self.view = ConsoleView(self.ctl, out, args.verbose)
        self._answered = None

    def say(self, text=""):
        self.view.say(text)

    def ask(self, prompt):
        """One line from stdin; None at end of input."""
        self.out.write(prompt)
        self.out.flush()
        try:
            line = self.inp.readline()
        except KeyboardInterrupt:
            self.say("")
            return None
        if not line:
            self.say("(no response)")
            return None
        if not self.inp.isatty():
            self.say(line.rstrip("\r\n").lstrip("﻿"))      # echo what was piped in
        # PowerShell pipes text with a UTF-8 byte-order mark: it is not part of the answer.
        return line.rstrip("\r\n").lstrip("﻿")

    def wait(self):
        """Run until the current job chain ends, answering confirmations from stdin."""
        ctl = self.ctl
        while True:
            try:
                # One message at a time: the console prints in the order things happen.
                while ctl.pump(limit=1):
                    if ctl.pending_confirmation is not None:
                        break
                pending = ctl.pending_confirmation
                if pending is not None and pending is not self._answered:
                    self._answered = pending
                    self.say("")
                    for line in ctl.confirmation_lines(pending):
                        self.say("    " + line)
                    answer = self.ask("\nType %s to continue (any other response cancels): "
                                      % pending["phrase"])
                    ctl.answer_confirmation(answer or "")
                    continue
                if not ctl.busy and ctl.inbox.empty():
                    return
                time.sleep(0.05)
            except KeyboardInterrupt:
                if ctl.critical:
                    self.say("\nWrite in progress: it cannot be cancelled. Do NOT disconnect or "
                             "power off the FM-1.")
                elif ctl.cancel():
                    self.say("\nCancelling (nothing has been written)...")

    def local_checks(self, package=None):
        """Environment and package, like the window does at start-up."""
        self.say("Local checks (without accessing the FM-1)")
        self.ctl.start(Path(package).resolve() if package else None)
        self.wait()
        env = self.ctl.rows["environment"]
        if env.status != c.PASS:
            if self.ctl.environment and not self.ctl.environment.get("admin"):
                self.say("\nOpen the console as administrator (right-click > Run as "
                         "administrator) and run the command again.")
            return False
        return package is None or self.ctl.rows["package"].status == c.PASS

    def detect(self):
        self.say("\nFM-1 detection (read-only)")
        self.ctl.detect(self.args.port)
        self.wait()
        return self.ctl.connected

    # ------------------------------------------------------------------ commands

    def run(self):
        command = self.args.command
        if command in ("install", "check"):
            package = Path(self.args.package)
            if not package.is_file() or package.suffix.lower() != ".fwsc":
                self.say("Not a .fwsc file: %s" % package)
                return EXIT_START
        if self.ctl.simulation is not None:
            self.say("SIMULATION: mock recovery device; no real hardware is "
                     "real.\n")
        if command == "check":
            return EXIT_OK if self.local_checks(self.args.package) else EXIT_START
        if command == "detect":
            if not self.local_checks():
                return EXIT_START
            return EXIT_OK if self.detect() else EXIT_START
        return self.install()

    def install(self):
        ctl = self.ctl
        if not self.local_checks(self.args.package) or not self.detect():
            self.say("\nCannot install: %s." % "; ".join(ctl.install_blockers()))
            return EXIT_START
        if not ctl.can_install():
            self.say("\nCannot install: %s." % "; ".join(ctl.install_blockers()))
            return EXIT_START
        self.say("\nInstalling %s" % ctl.package_path.name)
        ctl.start_install()
        self.wait()
        if ctl.danger:
            return EXIT_DANGER
        if not ctl.written:
            return EXIT_FAIL
        if self.args.no_boot_check:
            self.say("Manually disconnect USB, power off the FM-1, wait 5 seconds, power it on, and "
                     "reconnect it.")
            return EXIT_OK
        while True:
            self.say("\nManually: 1) disconnect the USB cable; 2) power off the FM-1; 3) wait "
                     "5 seconds; 4) power it on and reconnect it.")
            answer = self.ask("Press Enter when it is powered on and connected (or type "
                              "'skip'): ")
            if answer is None or answer.strip().lower() == "skip":
                self.say("Boot check skipped.")
                return EXIT_OK
            ctl.check_boot()
            self.wait()
            if ctl.boot_confirmed:
                self.say("\nInstallation complete: boot confirmed.")
                return EXIT_OK
            again = self.ask("Check again? [y/N]: ")
            if not again or again.strip().lower() not in ("y", "yes"):
                return EXIT_BOOT


def main(argv):
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        return ConsoleApp(args).run()
    except KeyboardInterrupt:
        return EXIT_FAIL
