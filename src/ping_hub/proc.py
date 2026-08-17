"""Run child processes without flashing a console window.

The daemon runs windowless, but every subprocess it starts is a console
program — `base relay ping` on each send, `wsl hostname -I` on each bridge
reconnect, `say`, `ffmpeg`. Windows gives each of those its own console, so a
hub that is supposed to be invisible pops a black rectangle over whatever you
are doing, several times a minute. Chris hit this the moment the packaged
daemon went windowless.

CREATE_NO_WINDOW fixes it, and it has to be on EVERY call — one forgotten site
is one flash. So it lives here rather than as a keyword argument repeated at
fifteen call sites, and the suite asserts the modules use this rather than
`subprocess` directly.

Deliberate exception: `ping_hub.spawn.wt` launches Windows Terminal and calls
`subprocess.Popen` itself. That child's whole purpose is to put a window on
screen, and suppressing it is the one thing it must never do.
"""
from __future__ import annotations

import os
import subprocess

# winbase.h. Named here so the intent survives without a magic number, and so
# non-Windows platforms carry no flag at all.
CREATE_NO_WINDOW = 0x08000000
HIDDEN = CREATE_NO_WINDOW if os.name == "nt" else 0


def _hidden(kwargs: dict) -> dict:
    if HIDDEN:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | HIDDEN
    return kwargs


def run(*args, **kwargs):
    """`subprocess.run` with no console window."""
    return subprocess.run(*args, **_hidden(kwargs))


def popen(*args, **kwargs):
    """`subprocess.Popen` with no console window."""
    return subprocess.Popen(*args, **_hidden(kwargs))
