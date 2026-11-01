"""Administrator rights on Windows: the recovery tool needs them to open the WL82 disk."""

from __future__ import annotations

import subprocess
import sys

from .worker import INSTALLER_DIR


def is_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def relaunch_as_admin(app_args) -> bool:
    """Start this installer again elevated (UAC prompt); True if it started."""
    if sys.platform != "win32":
        return False
    import ctypes
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, list(app_args)
    else:
        exe, params = sys.executable, ["-m", "app"] + list(app_args)
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", exe, subprocess.list2cmdline(params), str(INSTALLER_DIR), 1)
    return result > 32
