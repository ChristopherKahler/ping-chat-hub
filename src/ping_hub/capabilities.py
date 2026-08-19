"""What this machine can actually do, reported honestly.

The package is opinionated (Chris ruling 2026-08-17): voice is bundled and the
UI never hides the mic. That only works if the hub can say WHY audio is not
happening, so every capability answers with one of four states and never with a
falsy blank:

    ready    it works
    absent   not installed on this machine
    error    installed but not responding
    off      a human wrote enabled = false

`absent` and `error` are different facts and are never collapsed. A missing
feed reports "source absent", never a fresh-looking nothing.
"""
from __future__ import annotations

import shutil
import urllib.error
import urllib.request
from pathlib import Path

READY, ABSENT, ERROR, OFF = "ready", "absent", "error", "off"


def _r(state: str, detail: str = "") -> dict:
    return {"state": state, "detail": detail}


def _reachable(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    """A 4xx still proves something is listening — only a transport failure
    means the server is down."""
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True, ""
    except urllib.error.HTTPError as e:
        return True, f"http {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, str(e)[:120]


def stt(cfg, reach=_reachable) -> dict:
    if not cfg.stt.enabled:
        return _r(OFF, "disabled in hub.toml")
    if not shutil.which(cfg.stt.ffmpeg):
        return _r(ABSENT, f"{cfg.stt.ffmpeg} not on PATH")
    root = cfg.stt.url.rsplit("/", 1)[0] + "/"
    ok, why = reach(root)
    return _r(READY, cfg.stt.url) if ok else _r(ERROR, f"no server at {root}: {why}")


def tts(cfg, exists=None) -> dict:
    exists = exists or (lambda p: Path(p).exists())
    if not cfg.tts.enabled:
        return _r(OFF, "disabled in hub.toml")
    cmd = cfg.tts.command
    if not cmd:
        return _r(ABSENT, "no speech engine installed")
    # cmd is an argv prefix; the last element is the script/binary
    target = cmd[-1]
    if not exists(target) and not shutil.which(target):
        return _r(ERROR, f"configured but missing: {target}")
    return _r(READY, target)


def cx_ptt(cfg, exists=None) -> dict:
    exists = exists or (lambda p: Path(p).exists())
    # same ordering rule as wsl(): `enabled` DERIVES from whether cx.toml is
    # there, so asking it first reports a machine that never had cx-ptt as a
    # human decision to turn it off. Caught live 2026-08-17 by a shadow run
    # pointed at a scratch store, where every derived path is legitimately
    # absent and the whole panel read "off".
    if not exists(cfg.cx_ptt.cx_toml):
        return _r(ABSENT, f"no {cfg.cx_ptt.cx_toml}")
    if not cfg.cx_ptt.enabled:
        return _r(OFF, "disabled in hub.toml")
    if not exists(cfg.cx_ptt.cx_slot):
        return _r(ERROR, f"cx.toml present but {cfg.cx_ptt.cx_slot} missing")
    return _r(READY, str(cfg.cx_ptt.cx_toml))


def wsl(cfg) -> dict:
    # order matters: `enabled` DERIVES from whether a distro resolved, so
    # asking it first would report a machine with no WSL as a human decision
    # to switch WSL off. Absent is not off.
    if not cfg.wsl.distro:
        return _r(ABSENT, "no WSL distro resolved")
    if not cfg.wsl.enabled:
        return _r(OFF, "disabled in hub.toml")
    if not cfg.wsl.home_linux:
        return _r(ERROR, f"{cfg.wsl.distro} present but its home did not resolve")
    return _r(READY, f"{cfg.wsl.distro} at {cfg.wsl.home_linux}")


def _wsl_ip(cfg) -> str:
    """WSL's eth0 address — the same authority the engine's bridge client
    uses (`wsl hostname -I`), because this machine's localhost relay does not
    forward the WSL port and the NAT address changes every WSL boot."""
    import subprocess

    from ping_hub import proc
    try:
        r = proc.run(["wsl", "hostname", "-I"], capture_output=True,
                     text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    out = (r.stdout or "").replace("\x00", "").strip()
    return out.split()[0] if out else ""


def bridge(cfg, reach=_reachable, wsl_ip=_wsl_ip) -> dict:
    """Is the WSL bridge answering?

    Same ordering rule this file has now learned four times over (wsl, cx_ptt,
    cx_restart, and now here): absence is checked BEFORE `enabled`, because
    `wsl.enabled` DERIVES from a distro resolving — asking it first reports a
    machine that has no WSL as a human decision to switch the bridge off.

    `absent` and `error` stay different facts: nothing to run over there, vs
    the WSL side is up and nothing answers on the port. The second is the exact
    failure this lifecycle exists to make visible, and collapsing it into a
    blank is how it stayed invisible for a day (2026-08-19).
    """
    if not cfg.wsl.distro:
        return _r(ABSENT, "no WSL distro resolved")
    if not cfg.wsl.enabled:
        return _r(OFF, "disabled in hub.toml")
    host = wsl_ip(cfg)
    if not host:
        return _r(ERROR, f"{cfg.wsl.distro} is present but its IP did not resolve")
    url = f"http://{host}:{cfg.wsl.bridge_port}/snapshot"
    ok, why = reach(url)
    return _r(READY, url) if ok else _r(ERROR, f"no bridge at {url}: {why}")


def base(cfg) -> dict:
    p = shutil.which(cfg.paths.base_bin)
    return _r(READY, p) if p else _r(ABSENT, f"{cfg.paths.base_bin} not on PATH")


def cx_restart(cfg, exists=None) -> dict:
    """Can this machine restart the hotkey daemon from the app?

    Same ordering rule as cx_ptt() and wsl(): absence of the launcher is
    checked BEFORE `enabled`, so a Mac reports "no launcher" rather than
    "a human turned this off".
    """
    exists = exists or (lambda p: Path(p).exists())
    if not exists(cfg.cx_ptt.launcher):
        return _r(ABSENT, f"no launcher at {cfg.cx_ptt.launcher}")
    # cx_ptt.enabled DERIVES from cx.toml existing, so consulting it on a
    # machine that never had cx-ptt turns "not installed" into "a human
    # switched this off". Third time this inversion has surfaced in this file
    # (wsl, cx_ptt, now here) -- check the absence explicitly.
    if not exists(cfg.cx_ptt.cx_toml):
        return _r(ABSENT, f"no {cfg.cx_ptt.cx_toml}")
    if cfg.cx_ptt.enabled_override is False:
        return _r(OFF, "disabled in hub.toml")
    return _r(READY, str(cfg.cx_ptt.launcher))


def audio(cfg, exists=None) -> dict:
    """Audio device switching, gated on cx-ptt's published list.

    The list is the capability: without it there is nothing to show, and the
    switch would be a control with no options. Deliberately does NOT shell out
    to PowerShell to decide -- that call costs 1.2s (measured) and this runs
    when the settings panel opens.
    """
    exists = exists or (lambda p: Path(p).exists())
    if not exists(cfg.cx_ptt.devices_json):
        return _r(ABSENT, f"no device list at {cfg.cx_ptt.devices_json}")
    if not exists(cfg.cx_ptt.cx_toml):
        return _r(ABSENT, f"no {cfg.cx_ptt.cx_toml}")
    if cfg.cx_ptt.enabled_override is False:
        return _r(OFF, "disabled in hub.toml")
    return _r(READY, str(cfg.cx_ptt.devices_json))


def probe_all(cfg, reach=_reachable) -> dict:
    return {"stt": stt(cfg, reach=reach), "tts": tts(cfg), "cx_ptt": cx_ptt(cfg),
            "wsl": wsl(cfg), "bridge": bridge(cfg, reach=reach),
            "base": base(cfg),
            "cx_restart": cx_restart(cfg), "audio": audio(cfg)}
