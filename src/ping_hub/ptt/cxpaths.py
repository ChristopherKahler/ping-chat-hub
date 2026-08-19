"""Where the cx tools keep their files — derived, never spelled out.

Every script in this directory used to carry one account's home in a raw
string, which is exactly why the whole thing ran on precisely one machine.
`ping-hub install` writes the PING_HUB_* variables into the launcher's
environment; with none of them set, everything still derives from the account
running the script.

The awkward case is real and is solved here rather than in each caller: some
of these tools run INSIDE WSL and need the WINDOWS store, because cx.toml, the
relay inboxes and the hub's own files live over there. That store is reached
through /mnt/c, and which account owns it is found by looking for the store
itself — never by spelling a name.
"""
from __future__ import annotations

import os
from pathlib import Path

WIN = os.name == "nt"

_MOUNTED_USERS = Path("/mnt/c/Users")


def home() -> Path:
    return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME")
                or Path.home())


def _win_home_from_wsl() -> str:
    """The Windows account that owns the cx store, seen from inside WSL.

    Discovered by the store's own presence. Guessing from $USER would be wrong
    on any machine where the Linux and Windows account names differ, which is
    most of them.
    """
    named = (os.environ.get("PING_HUB_WIN_HOME") or "").strip()
    if named:
        return named
    try:
        for c in sorted(_MOUNTED_USERS.iterdir()):
            if (c / ".base-gbl" / "cx.toml").is_file():
                return str(c)
    except OSError:
        pass
    return ""


def base_gbl() -> Path:
    v = (os.environ.get("PING_HUB_BASE_GBL") or "").strip()
    if v:
        return Path(v)
    if not WIN:
        win = _win_home_from_wsl()
        if win:
            return Path(win) / ".base-gbl"
    return home() / ".base-gbl"


def base_store() -> Path:
    return base_gbl() / ".base"


def cx_toml() -> Path:
    v = (os.environ.get("PING_HUB_CX_TOML") or "").strip()
    return Path(v) if v else base_gbl() / "cx.toml"


def cx_dir() -> Path:
    v = (os.environ.get("PING_HUB_CX_DIR") or "").strip()
    return Path(v) if v else base_gbl() / "cx"


def inbox_root() -> Path:
    return base_store() / "relay-inbox"


def base_bin(side: str = "") -> str:
    """The `base` binary for a side. An empty side means whichever this is."""
    side = side or ("win" if WIN else "wsl")
    if side == "wsl":
        return os.environ.get("PING_HUB_BASE_WSL") or "base"
    v = (os.environ.get("PING_HUB_BASE_WIN") or "").strip()
    if v:
        return v
    if WIN:
        return str(home() / ".local" / "bin" / "base.exe")
    win = _win_home_from_wsl()
    return str(Path(win) / ".local" / "bin" / "base.exe") if win else "base.exe"


def stt_model() -> Path:
    """The parakeet the hub provisions: one 650MB download serves the STT
    server and the hotkey daemon both."""
    v = (os.environ.get("PING_HUB_STT_MODEL") or "").strip()
    return Path(v) if v else home() / ".ping-hub" / "stt" / "model"
