"""Versioned JSON Lines protocol spoken by motor/fm1_unbrick.py --events.

Every stdout line of the recovery tool is one JSON object
{"schema": 1, "event": <name>, ...}. Anything else is a protocol error:
the UI never interprets the recovery tool's human text (stderr) as state.
"""

from __future__ import annotations

import json

SCHEMA = 1

# The rows of the window, in order. The recovery tool names them in step_* events.
STEPS = ("package", "environment", "device", "rom", "bootloader", "chip", "backup",
         "confirm", "write", "verify", "boot")

# When the recovery tool fails before its first step_started it names the command instead.
COMMANDS = ("inspect", "doctor", "probe", "softkey", "find", "restore")

# Required fields of each event (more fields may follow).
EVENT_FIELDS = {
    "hello": ("command", "version"),
    "step_started": ("step", "title"),
    "step_succeeded": ("step",),
    "step_failed": ("step", "code", "message"),
    "progress": ("step", "phase", "percent"),
    "package": ("name", "identity", "kind", "file_sha256", "firmware_sha256"),
    "catalog": ("status",),
    "environment": ("admin", "checks"),
    "device_state": ("connected", "mode", "rom_disks"),
    "identity": ("identity", "port"),
    "confirmation_required": ("id", "phrase", "summary"),
    "confirmation_result": ("id", "accepted"),
    "key_sent": ("port",),
    "write_started": ("start", "end"),
    "warning": ("message",),
    "done": ("exit_code",),
}

STEP_EVENTS = ("step_started", "step_succeeded", "step_failed", "progress")


class ProtocolError(ValueError):
    """A stdout line that is not a valid event of this schema."""


def parse_event(line: str) -> dict:
    """Parse and validate one stdout line; raise ProtocolError otherwise."""
    try:
        event = json.loads(line)
    except ValueError as e:
        raise ProtocolError("not JSON: %s" % e) from None
    if not isinstance(event, dict):
        raise ProtocolError("not a JSON object")
    if event.get("schema") != SCHEMA:
        raise ProtocolError("unsupported schema %r (this installer speaks %d)"
                            % (event.get("schema"), SCHEMA))
    name = event.get("event")
    if name not in EVENT_FIELDS:
        raise ProtocolError("unknown event %r" % name)
    missing = [f for f in EVENT_FIELDS[name] if f not in event]
    if missing:
        raise ProtocolError("%s without %s" % (name, ", ".join(missing)))
    if name in STEP_EVENTS:
        step = event["step"]
        allowed = STEPS + COMMANDS if name == "step_failed" else STEPS
        if step not in allowed:
            raise ProtocolError("%s names unknown step %r" % (name, step))
    if name == "progress":
        pct = event["percent"]
        if not isinstance(pct, int) or isinstance(pct, bool) or not 0 <= pct <= 100:
            raise ProtocolError("progress percent %r out of range" % (pct,))
    if name == "confirmation_required" and not isinstance(event["summary"], dict):
        raise ProtocolError("confirmation_required summary is not an object")
    return event
