"""No console flashes.

The daemon runs windowless, but every child it starts is a console program, so
Windows gave each one its own black rectangle over whatever Chris was doing —
several times a minute, once on every send. The fix is a flag, and the only
hard part is that it has to be on EVERY call: one forgotten site is one flash.

So the interesting test here is structural. It asserts the modules route
through the helper rather than calling `subprocess` directly, because the bug
class is an ABSENT flag, and absence is not something a behavioural test on the
happy path can see.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from ping_hub import proc

SRC = Path(__file__).resolve().parents[1] / "src" / "ping_hub"

# wt.py is exempt on purpose: its child IS the terminal window Chris asked for.
# autostart.py takes `run` as an injectable seam and the caller supplies it.
EXEMPT = {"proc.py", "wt.py", "autostart.py"}


def test_the_flag_is_applied_on_windows(monkeypatch):
    seen = {}
    monkeypatch.setattr(proc.subprocess, "run", lambda *a, **k: seen.update(k))
    monkeypatch.setattr(proc, "HIDDEN", proc.CREATE_NO_WINDOW)
    proc.run(["x"], capture_output=True)
    assert seen["creationflags"] & proc.CREATE_NO_WINDOW
    assert seen["capture_output"] is True      # caller's kwargs survive


def test_an_existing_creationflags_value_is_preserved(monkeypatch):
    seen = {}
    monkeypatch.setattr(proc.subprocess, "run", lambda *a, **k: seen.update(k))
    monkeypatch.setattr(proc, "HIDDEN", proc.CREATE_NO_WINDOW)
    proc.run(["x"], creationflags=0x00000200)
    assert seen["creationflags"] == 0x00000200 | proc.CREATE_NO_WINDOW


def test_no_flag_is_passed_off_windows(monkeypatch):
    """Albert's Mac must not receive a Windows-only creationflags value."""
    seen = {}
    monkeypatch.setattr(proc.subprocess, "run", lambda *a, **k: seen.update(k))
    monkeypatch.setattr(proc, "HIDDEN", 0)
    proc.run(["x"])
    assert "creationflags" not in seen


def test_popen_is_wrapped_too(monkeypatch):
    seen = {}
    monkeypatch.setattr(proc.subprocess, "Popen", lambda *a, **k: seen.update(k))
    monkeypatch.setattr(proc, "HIDDEN", proc.CREATE_NO_WINDOW)
    proc.popen(["x"])
    assert seen["creationflags"] & proc.CREATE_NO_WINDOW


def test_the_constant_is_the_real_windows_value():
    """A wrong constant would silently do nothing, or something else."""
    assert proc.CREATE_NO_WINDOW == 0x08000000


# ── the structural half ──────────────────────────────────────────────────────
def _modules():
    for f in sorted(SRC.rglob("*.py")):
        if f.name in EXEMPT or "__pycache__" in f.parts:
            continue
        # the vendored engines run in their own venvs, not in the daemon.
        # `ptt` joins them for the same reason and one more: cx-spawn-hidden
        # exists precisely to create a console and hide it, so the rule this
        # tripwire enforces is the opposite of its job.
        if {"voice", "bridge", "ptt"} & set(f.parts):
            continue
        yield f


@pytest.mark.parametrize("path", list(_modules()), ids=lambda p: p.name)
def test_no_module_spawns_a_console_directly(path):
    text = path.read_text(encoding="utf-8")
    bad = [f"{n}: {line.strip()[:80]}"
           for n, line in enumerate(text.splitlines(), 1)
           if re.search(r"\bsubprocess\.(run|Popen|call|check_output)\(", line)]
    assert not bad, (f"{path.name} spawns a console directly; use "
                     f"ping_hub.proc so the window stays hidden:\n" + "\n".join(bad))


def test_the_terminal_adapter_is_exempt_on_purpose():
    """If wt.py ever stops launching a window, this exemption is wrong and
    somebody should have to look at it."""
    wt = (SRC / "spawn" / "wt.py").read_text(encoding="utf-8")
    assert "subprocess.Popen(cmd, env=env)" in wt
    assert "proc.popen" not in wt


def test_the_helper_is_importable_without_a_display():
    """It must not reach for ctypes, a window handle, or anything else that
    fails headless — it is imported by every module in the daemon."""
    assert proc.run is not subprocess.run
    assert callable(proc.popen)
