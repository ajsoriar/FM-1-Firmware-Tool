"""Files dropped from Explorer onto the window (Windows only, no extra packages).

The top-level window is registered with DragAcceptFiles and its window
procedure is subclassed to catch WM_DROPFILES. The new procedure only stores
the paths: the Tk side picks them up in its own polling loop, so no Tcl call
runs inside the window procedure. When the installer runs elevated, UIPI would
drop the messages from a non-elevated Explorer; ChangeWindowMessageFilterEx lets
exactly the drag-and-drop messages through.
"""

from __future__ import annotations

import sys

WM_DROPFILES = 0x0233
WM_COPYDATA = 0x004A
WM_COPYGLOBALDATA = 0x0049
MSGFLT_ALLOW = 1
GWLP_WNDPROC = -4

_keep = []          # window procedures must outlive the window


def enable_file_drop(toplevel, sink) -> bool:
    """Append the list of dropped paths to `sink` (a list); True if enabled."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32")
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                     wintypes.LPARAM)
        user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_void_p)
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.CallWindowProcW.argtypes = (ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                           wintypes.WPARAM, wintypes.LPARAM)
        user32.CallWindowProcW.restype = LRESULT
        user32.ChangeWindowMessageFilterEx.argtypes = (wintypes.HWND, wintypes.UINT,
                                                       wintypes.DWORD, ctypes.c_void_p)
        user32.ChangeWindowMessageFilterEx.restype = wintypes.BOOL
        shell32.DragAcceptFiles.argtypes = (wintypes.HWND, wintypes.BOOL)
        shell32.DragQueryFileW.argtypes = (ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR,
                                           wintypes.UINT)
        shell32.DragQueryFileW.restype = wintypes.UINT
        shell32.DragFinish.argtypes = (ctypes.c_void_p,)

        toplevel.update_idletasks()
        hwnd = int(toplevel.wm_frame(), 16)
        for msg in (WM_DROPFILES, WM_COPYDATA, WM_COPYGLOBALDATA):
            user32.ChangeWindowMessageFilterEx(hwnd, msg, MSGFLT_ALLOW, None)
        shell32.DragAcceptFiles(hwnd, True)

        old = None

        def proc(h, msg, wparam, lparam):
            if msg == WM_DROPFILES:
                hdrop = ctypes.c_void_p(wparam)
                count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
                paths = []
                for i in range(count):
                    size = shell32.DragQueryFileW(hdrop, i, None, 0) + 1
                    buf = ctypes.create_unicode_buffer(size)
                    shell32.DragQueryFileW(hdrop, i, buf, size)
                    paths.append(buf.value)
                shell32.DragFinish(hdrop)
                sink.append(paths)
                return 0
            return user32.CallWindowProcW(old, h, msg, wparam, lparam)

        new = WNDPROC(proc)
        old = user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.cast(new, ctypes.c_void_p))
        if not old:
            return False
        _keep.append((new, old))
        return True
    except (OSError, AttributeError, ValueError):
        return False
