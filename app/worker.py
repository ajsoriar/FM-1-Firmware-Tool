"""Runs motor/fm1_unbrick.py --events in a child process.

Two reader threads turn stdout lines into validated events and stderr lines
into log text; everything goes through one callback (the controller puts it
on a queue.Queue that Tkinter drains with after()). Nothing here touches Tk.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from . import events

INSTALLER_DIR = Path(__file__).resolve().parent.parent
RECOVERY_SCRIPT = INSTALLER_DIR / "motor" / "fm1_unbrick.py"

ALLOWED_COMMANDS = ("inspect", "doctor", "probe", "find", "softkey", "restore")
# The installer never skips a confirmation or a check, and never picks the disk itself.
FORBIDDEN_ARGS = ("--yes", "--i-know", "--force-write", "--device", "setup", "repair-settings")

CREATE_NO_WINDOW = 0x08000000
# Its own process group, without the console: a Ctrl+C typed in the console that runs
# `fm1 install` never reaches the recovery tool (it would interrupt a write).
CREATE_NEW_PROCESS_GROUP = 0x00000200


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def recovery_python() -> str:
    """python.exe next to pythonw.exe: the recovery tool needs real stdio handles."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        return str(exe.parent / "python.exe")
    return str(exe)


def recovery_command(command: str, args=()) -> list:
    """The argv of one recovery-tool run; refuses what the installer never uses."""
    if command not in ALLOWED_COMMANDS:
        raise ValueError("recovery command %r is not used by the installer" % command)
    args = [str(a) for a in args]
    for a in args:
        if a.split("=", 1)[0] in FORBIDDEN_ARGS:
            raise ValueError("the installer never passes %s to the recovery tool" % a)
    if frozen():
        base = [sys.executable, "--run-recovery"]
    else:
        base = [recovery_python(), str(RECOVERY_SCRIPT)]
    return base + [command] + args + ["--events"]


class RecoveryJob:
    """One recovery-tool process. post(kind, payload) receives, from reader threads:

    ("event", dict)           a validated event from stdout
    ("protocol_error", str)   a stdout line that is not a valid event
    ("stderr", str)           a line of human text / diagnostics
    ("exit", int)             the exit code, after every line above
    """

    def __init__(self, argv, cwd, post):
        self.argv, self.cwd, self.post = list(argv), cwd, post
        self.proc = None
        self._readers = []

    def start(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        Path(self.cwd).mkdir(parents=True, exist_ok=True)
        try:
            self.proc = subprocess.Popen(
                self.argv, cwd=str(self.cwd), env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", bufsize=1,
                creationflags=(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
                if sys.platform == "win32" else 0,
                start_new_session=sys.platform != "win32")
        except OSError as e:
            self.post("stderr", "Could not start the recovery tool: %s" % e)
            self.post("exit", -1)
            return
        self._readers = [threading.Thread(target=self._read_stdout, daemon=True),
                         threading.Thread(target=self._read_stderr, daemon=True)]
        for t in self._readers:
            t.start()
        threading.Thread(target=self._wait, daemon=True).start()

    def _read_stdout(self):
        for line in self.proc.stdout:
            line = line.rstrip("\r\n")
            if not line:
                continue
            try:
                self.post("event", events.parse_event(line))
            except events.ProtocolError as e:
                self.post("protocol_error", "%s: %s" % (e, line[:200]))

    def _read_stderr(self):
        for line in self.proc.stderr:
            self.post("stderr", line.rstrip("\r\n"))

    def _wait(self):
        code = self.proc.wait()
        for t in self._readers:
            t.join()
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass
        self.post("exit", code)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def send_line(self, text: str) -> bool:
        """Answer a confirmation_required event; False if the process is gone."""
        try:
            self.proc.stdin.write(text + "\n")
            self.proc.stdin.flush()
            return True
        except (OSError, ValueError, AttributeError):
            return False            # the process ended (pipe closed)

    def terminate(self):
        if self.running:
            self.proc.terminate()
