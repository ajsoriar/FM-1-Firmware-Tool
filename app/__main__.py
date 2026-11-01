"""FM-1 installer entry point (fm1.bat runs it as `py -m app`):

    fm1                              window
    fm1 PACKAGE.fwsc                 window with that package chosen
    fm1 install PACKAGE.fwsc         install from the console (see app/cli.py)
    fm1 check PACKAGE.fwsc           analyse package and environment, no device access
    fm1 detect                       detect the FM-1 (read-only)

`--run-recovery ARGS` runs the recovery tool itself; it is how a packaged EXE
starts it as a child process.
"""

from __future__ import annotations

import argparse
import sys

from .cli import COMMANDS, add_simulation_args, simulation_from


def parse_gui_args(argv):
    ap = argparse.ArgumentParser(
        prog="fm1", description="FM-1 Firmware Tool. For the console, run: fm1 install "
                                "PACKAGE.fwsc (ver fm1 install --help).")
    ap.add_argument("package", nargs="?", help="optional .fwsc package")
    ap.add_argument("--no-elevate", action="store_true",
                    help="do not request administrator privileges on launch")
    ap.add_argument("--backup-dir", help="backup folder (default Documents\\"
                                         "FM1-Installer\\backups)")
    add_simulation_args(ap)
    return ap.parse_args(argv)


def run_recovery(argv):
    from pathlib import Path
    from .worker import RECOVERY_SCRIPT
    sys.path.insert(0, str(Path(RECOVERY_SCRIPT).parent))
    import fm1_unbrick
    return fm1_unbrick.main(argv)


def run_gui(argv):
    args = parse_gui_args(argv)

    from . import elevation
    if (sys.platform == "win32" and not args.simulate and not args.no_elevate
            and not elevation.is_admin()):
        # One UAC prompt up front: the recovery tool needs it to open the WL82 disk.
        # Declined: the window still opens and shows what is missing.
        if elevation.relaunch_as_admin(argv + ["--no-elevate"]):
            return 0

    import tkinter as tk
    from pathlib import Path
    from .controller import Controller, Paths
    from .ui import InstallerApp

    paths = Paths.default(simulate=args.simulate)
    if args.backup_dir:
        paths.backup_dir = Path(args.backup_dir)
    controller = Controller(paths=paths, simulation=simulation_from(args))
    if sys.platform == "win32":
        try:                                    # crisp text on scaled (125 %, 150 %) screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    InstallerApp(root, controller, app_args=argv)
    controller.start(Path(args.package).resolve() if args.package else None)
    root.mainloop()
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--run-recovery"]:
        return run_recovery(argv[1:])
    if argv[:1] and argv[0] in COMMANDS:
        from .cli import main as cli_main
        return cli_main(argv)
    return run_gui(argv)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                      # pythonw has no console: show it
        try:
            import tkinter.messagebox as mb
            mb.showerror("FM-1 Firmware Tool", "The installer could not start:\n%s" % e)
        finally:
            raise
