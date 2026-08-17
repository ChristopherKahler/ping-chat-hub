"""Test-session isolation for ping_hub.

Importing ``ping_hub.daemon`` constructs an ``Engine`` at module level, and an
Engine points at whatever base store the config resolves to. Left alone, that
is the OPERATOR'S LIVE STORE — the suite would create directories in it and
read the journals of a running hub.

So before any test module is imported, the config file is pinned to a
throwaway one whose ``base_gbl`` is a temp dir and whose
``register_standing_title`` is false. This is amendment 3's isolation made
structural rather than remembered: no test can reach the live cockpit even by
accident.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_SCRATCH = Path(tempfile.mkdtemp(prefix="ping-hub-tests-"))
(_SCRATCH / "hub.toml").write_text(
    "[hub]\n"
    "port = 0\n"
    "bind = \"127.0.0.1\"\n"
    "register_standing_title = false\n"
    "\n"
    "[paths]\n"
    f"base_gbl = {str(_SCRATCH / 'base-gbl')!r}\n"
    "\n"
    "[wsl]\n"
    "enabled = false\n",
    encoding="utf-8",
)
os.environ["PING_HUB_CONFIG"] = str(_SCRATCH / "hub.toml")

SCRATCH = _SCRATCH
