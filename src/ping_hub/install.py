"""Provisioning — voice works out of the box, on any machine.

The package is opinionated and VENDORED (Chris rulings 2026-08-17): the speech
engines are part of the hub, not something the operator is told to go install.
This module fetches them, builds them isolated venvs, and writes a hub.toml
that points at what it just built.

Four findings from Chris's own Parakeet-installer run (2026-08-15) are load-
bearing here and are not rediscovered:

1. Fetch at install time, do not pre-bake. The package stays ~1 MB; the models
   are ~800 MB and most machines want the same ones.
2. NEVER shell out to `tar.exe` for a .tar.bz2. Windows bsdtar has no built-in
   bzip2 and always spawns an external `bzip2 -d`, which exists on the author's
   box only because Git ships it. Stdlib `tarfile` decompresses in-process.
3. NEVER install into the operator's global interpreter. On this machine
   C:\\Python312 also runs the STT server, and a pip step there once uninstalled
   a live `sherpa-onnx`. Every install goes into a venv this module creates.
4. Verify what came out of the archive. A download that 200s and extracts to
   the wrong layout is a failure that only shows up as a mystery at runtime.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from ping_hub import proc
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import venv
from pathlib import Path

# Verified live 2026-08-17 by HEAD request. The kokoro sizes match the bytes
# already on this machine exactly (325,532,387 and 28,214,398), which is how we
# know these are the same artifacts, not a lookalike release.
STT_MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                 "asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2")
TTS_MODEL_URL = ("https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
                 "model-files-v1.0/kokoro-v1.0.onnx")
TTS_VOICES_URL = ("https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
                  "model-files-v1.0/voices-v1.0.bin")

STT_MEMBERS = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx",
               "tokens.txt")
STT_PIP = ["sherpa-onnx", "numpy"]
TTS_PIP = ["kokoro-onnx", "soundfile", "onnxruntime"]

VOICE_SRC = Path(__file__).with_name("voice")
BRIDGE_SRC = Path(__file__).with_name("bridge") / "wsl_bridge.py"
# the cx-ptt hotkey daemon and its siblings, vendored 2026-08-19. It was the
# last piece of this app a human had to start by hand, and it lived in another
# repo with one account's paths baked into it -- so a recipient of this package
# got none of it. Copied from the LIVE files rather than that repo: the repo
# had drifted 5.5KB behind on the daemon and 9KB behind on the waker, and
# shipping the copy that does not run is not shipping it.
CXPTT_SRC = Path(__file__).with_name("ptt")
# on TOP of STT_PIP, into the SAME venv: one app means one interpreter, and
# cx-ptt loads the very parakeet the STT server already downloaded
CXPTT_PIP = ["sounddevice", "keyboard"]


class InstallError(RuntimeError):
    pass


def _log(msg: str) -> None:
    print(f"[ping-hub install] {msg}", flush=True)


# ── fetching ─────────────────────────────────────────────────────────────────
def download(url: str, dest: Path, log=_log) -> Path:
    """Stream to <dest>.part, then rename. A half-written model that looks
    complete is worse than no model."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        log(f"have {dest.name} ({dest.stat().st_size / 1e6:.0f} MB), skipping")
        return dest
    part = dest.with_suffix(dest.suffix + ".part")
    log(f"fetching {dest.name}")
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as fh:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            step = max(total // 10, 1) if total else 0
            nxt = step
            while chunk := r.read(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if step and done >= nxt:
                    log(f"  {done / 1e6:.0f}/{total / 1e6:.0f} MB")
                    nxt += step
    except OSError as e:
        part.unlink(missing_ok=True)
        raise InstallError(f"download failed: {url}\n  {e}") from e
    part.replace(dest)
    return dest


def extract_bz2(archive: Path, into: Path, log=_log) -> Path:
    """Stdlib only. Shelling out to tar.exe here works on the author's machine
    and fails on a user's — bsdtar cannot decompress bzip2 without an external
    binary that ships with Git, not with Windows."""
    into.mkdir(parents=True, exist_ok=True)
    log(f"extracting {archive.name}")
    with tarfile.open(archive, "r:bz2") as tf:
        members = tf.getmembers()
        roots = {Path(m.name).parts[0] for m in members if m.name.strip("./")}
        for m in members:
            if m.name.startswith(("/", "..")) or ".." in Path(m.name).parts:
                raise InstallError(f"refusing unsafe archive path: {m.name}")
        try:
            tf.extractall(into, filter="data")   # 3.12+ default in 3.14
        except TypeError:
            tf.extractall(into)
    return into / sorted(roots)[0] if len(roots) == 1 else into


def verify_members(d: Path, names=STT_MEMBERS) -> None:
    missing = [n for n in names if not (d / n).is_file()]
    if missing:
        have = sorted(p.name for p in d.iterdir())[:12] if d.is_dir() else []
        raise InstallError(
            f"model layout wrong in {d}: missing {missing}; found {have}")


# ── venvs ────────────────────────────────────────────────────────────────────
def make_venv(path: Path, packages: list[str], log=_log) -> Path:
    """An isolated interpreter, never the operator's. Returns its python."""
    py = (path / "Scripts" / "python.exe") if os.name == "nt" else (path / "bin" / "python")
    if not py.exists():
        log(f"creating venv {path.name}")
        venv.EnvBuilder(with_pip=True, clear=False).create(path)
    if not py.exists():
        raise InstallError(f"venv build produced no interpreter at {py}")
    log(f"installing {' '.join(packages)}")
    r = proc.run([str(py), "-m", "pip", "install", "-q", *packages],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise InstallError(f"pip failed in {path.name}:\n{(r.stdout + r.stderr)[-800:]}")
    return py


def _launcher(path: Path, py: Path, script: Path, env: dict | None = None) -> Path:
    """A one-line shim so hub.toml can point at a stable path regardless of
    where the venv put its interpreter, and so the model location travels with
    the launcher instead of being a second thing to configure."""
    env = env or {}
    if os.name == "nt":
        path = path.with_suffix(".cmd")
        sets = "".join(f'set "{k}={v}"\r\n' for k, v in env.items())
        path.write_text(f'@echo off\r\nsetlocal\r\n{sets}"{py}" "{script}" %*\r\n',
                        encoding="utf-8")
    else:
        sets = "".join(f'export {k}="{v}"\n' for k, v in env.items())
        path.write_text(f'#!/bin/sh\n{sets}exec "{py}" "{script}" "$@"\n',
                        encoding="utf-8")
        path.chmod(0o755)
    return path


# ── the two engines ──────────────────────────────────────────────────────────
def provision_stt(home: Path, log=_log) -> dict:
    root = home / "stt"
    py = make_venv(root / "venv", STT_PIP, log)
    with tempfile.TemporaryDirectory() as td:
        arc = download(STT_MODEL_URL, Path(td) / "parakeet.tar.bz2", log)
        got = extract_bz2(arc, root / "extract", log)
        verify_members(got)
        model = root / "model"
        if model.exists():
            shutil.rmtree(model)
        shutil.move(str(got), str(model))
    shutil.rmtree(root / "extract", ignore_errors=True)
    server = root / "stt-server.py"
    shutil.copyfile(VOICE_SRC / "stt_server.py", server)
    launcher = _launcher(root / "start-stt", py, server,
                         {"PING_HUB_STT_MODEL": str(model)})
    return {"launcher": [str(launcher)], "model_dir": str(model),
            "python": str(py), "server": str(server)}


def cxptt_launcher_text(py: Path, script: Path, log_path: Path,
                        env: dict) -> str:
    """The .cmd that starts the hotkey daemon with its output tee'd.

    Not `_launcher()`: the tee and the window title are load-bearing here.
    cxptt.restart() always relaunches THROUGH this file precisely so a daemon
    that restarted itself bare (which it does on any mic change) gets its log
    and its title back — a button that repairs a degraded daemon rather than
    re-running it degraded.
    """
    sets = "".join(f'set "{k}={v}"\r\n' for k, v in env.items() if v)
    return (
        "@echo off\r\n"
        "rem Work Channel - cx-ptt hotkey daemon. Written by `ping-hub install`;\r\n"
        "rem every path below came from provisioning, not from an operator.\r\n"
        "title Work Channel - cx-ptt (hotkeys from cx.toml)\r\n"
        "setlocal\r\n"
        f"{sets}"
        f'powershell -NoProfile -Command "& {{ \'{py}\' -u \'{script}\' 2>&1 '
        f"| Tee-Object -FilePath '{log_path}' }}\"\r\n"
        "echo.\r\n"
        "echo cx-ptt exited. Press any key to close.\r\n"
        "pause >nul\r\n")


def provision_cxptt(home: Path, cfg, stt: dict, log=_log) -> dict:
    """Install the hotkey daemon the way the speech engines are installed.

    Two things this deliberately does NOT do. It does not download a second
    parakeet: cx-ptt loads `encoder/decoder/joiner.int8.onnx` + `tokens.txt`,
    which is exactly the layout `provision_stt` already fetched and verified,
    so one 650MB download serves both. And it does not build a third venv: the
    extra requirements over STT_PIP are two packages, and the ruling was one
    app, one interpreter.

    Never the operator's global python — install.py finding 3, and the
    hand-written launcher this replaces ran the daemon on C:\\Python312, which
    is the same interpreter a pip step there once broke.
    """
    if not CXPTT_SRC.is_dir():
        raise InstallError(f"cx-ptt sources missing from the package "
                           f"({CXPTT_SRC}); reinstall it")
    root = home / "cxptt"
    root.mkdir(parents=True, exist_ok=True)
    for f in sorted(CXPTT_SRC.glob("*.py")):
        shutil.copyfile(f, root / f.name)
    log(f"copied {len(list(CXPTT_SRC.glob('*.py')))} cx-ptt scripts to {root}")
    venv_dir = Path(stt["python"]).parent.parent
    py = make_venv(venv_dir, STT_PIP + CXPTT_PIP, log)
    cx_dir = cfg.cx_ptt.devices_json.parent
    env = {
        "PING_HUB_BASE_GBL": str(cfg.paths.base_gbl),
        "PING_HUB_CX_TOML": str(cfg.cx_ptt.cx_toml),
        "PING_HUB_CX_DIR": str(cx_dir),
        "PING_HUB_STT_MODEL": stt["model_dir"],
        "PING_HUB_BASE_WIN": cfg.paths.base_bin,
        "PING_HUB_WSL_HOME_UNC": cfg.wsl.home_unc or "",
        "PING_HUB_STANDING_TITLE": cfg.hub.standing_title,
    }
    launcher = root / "start-cxptt.cmd"
    launcher.write_text(
        cxptt_launcher_text(py, root / "cx-ptt.py",
                            cx_dir / "ptt-daemon.log", env),
        encoding="utf-8")
    log(f"wrote {launcher}")
    return {"launcher": str(launcher), "dir": str(root), "python": str(py)}


def provision_tts(home: Path, log=_log) -> dict:
    root = home / "tts"
    py = make_venv(root / "venv", TTS_PIP, log)
    download(TTS_MODEL_URL, root / "model" / "kokoro-v1.0.onnx", log)
    download(TTS_VOICES_URL, root / "model" / "voices-v1.0.bin", log)
    script = root / "say.py"
    shutil.copyfile(VOICE_SRC / "say.py", script)
    launcher = _launcher(root / "say", py, script,
                         {"PING_HUB_TTS_MODEL": str(root / "model")})
    return {"command": ([str(launcher)] if os.name != "nt"
                        else ["cmd", "/c", str(launcher)]),
            "model_dir": str(root / "model"), "python": str(py)}


# ── config ───────────────────────────────────────────────────────────────────
def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    return json.dumps(str(v))


def render_hub_toml(sections: dict) -> str:
    """Only what provisioning DECIDED gets written. Everything else stays
    absent so it keeps deriving — a config full of restated defaults is a
    config that goes stale silently."""
    out = ["# ping-chat-hub machine config, written by `ping-hub install`.",
           "# Keys absent here derive at load; delete a key to go back to",
           "# derivation. Not a preferences file: the UI writes those to",
           "# hub/settings.json.", ""]
    for name, keys in sections.items():
        if not keys:
            continue
        out.append(f"[{name}]")
        out += [f"{k} = {_toml(v)}" for k, v in keys.items()]
        out.append("")
    return "\n".join(out)


def installed_source(read_direct_url=None, list_dists=None) -> str:
    """Where this package was installed FROM, so `ping-hub update` can reinstall
    from the same place instead of guessing.

    pip records it in PEP 610 `direct_url.json` for path, VCS and URL installs.
    The distribution is found by which one provides `ping_hub`, not by name —
    the distribution is currently called something else, and hardcoding that
    would break the day it is renamed.
    """
    import importlib.metadata as md
    if read_direct_url is None:
        def read_direct_url(dist_name: str) -> str:
            try:
                return md.distribution(dist_name).read_text("direct_url.json") or ""
            except Exception:
                return ""
    # injectable: enumerating every installed distribution is slow enough to
    # dominate a test run, and a unit test has no business reading the real
    # environment to answer a question about parsing
    if list_dists is None:
        def list_dists() -> list[str]:
            try:
                return md.packages_distributions().get("ping_hub") or []
            except Exception:
                return []
    names = list_dists()
    for name in names:
        raw = read_direct_url(name)
        if not raw:
            continue
        try:
            url = (json.loads(raw) or {}).get("url", "")
        except ValueError:
            continue
        if not url:
            continue
        if url.startswith("file:"):
            from urllib.parse import urlparse
            from urllib.request import url2pathname
            return url2pathname(urlparse(url).path)
        return url
    return ""


def deploy_bridge(cfg, deploy_unc: str | None = None,
                  config_unc: str | None = None, log=_log,
                  python: str = "") -> dict:
    """Copy the bridge into WSL, drop its config beside it, and lay down the
    systemd user unit that keeps it running.

    The bridge runs INSIDE WSL against WSL's own base store; the hub never
    reads that store over the share. Both paths are written from Windows over
    `\\\\wsl.localhost`, which is the one direction that works — the reverse
    would need the per-boot NAT gateway IP.

    Newlines are forced to LF: the file is executed by WSL's Python and read by
    a Linux shell, and Windows text mode would put CRLF in both.
    """
    deploy = Path(deploy_unc or cfg.wsl.bridge_deploy_unc)
    if not str(deploy) or str(deploy) == ".":
        raise InstallError("no WSL side on this machine: nothing to deploy to")
    home = Path(config_unc or cfg.wsl.home_unc)
    if not str(home) or str(home) == ".":
        raise InstallError("WSL home did not resolve; cannot place bridge config")

    deploy.mkdir(parents=True, exist_ok=True)
    dest = deploy / "wsl-bridge.py"
    dest.write_text(BRIDGE_SRC.read_text(encoding="utf-8"),
                    encoding="utf-8", newline="\n")
    log(f"deployed {dest}")

    conf_dir = home / ".config"
    conf_dir.mkdir(parents=True, exist_ok=True)
    conf = conf_dir / "hub-bridge.toml"
    # only the keys the bridge cannot work out for itself from inside WSL;
    # base_gbl and base_bin are left to its own Path.home() derivation
    body = ("# written by `ping-hub install --deploy-bridge`\n"
            "[bridge]\n"
            f"port = {cfg.wsl.bridge_port}\n"
            f"standing_title = {json.dumps(cfg.hub.standing_title)}\n")
    conf.write_text(body, encoding="utf-8", newline="\n")
    log(f"wrote {conf}")

    # the unit is written here, beside the two files it refers to, and ENABLED
    # separately by autostart.register_bridge — writing a file over the share
    # is this module's job, talking to systemd is not
    from ping_hub import autostart
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / autostart.BRIDGE_UNIT
    unit.write_text(autostart.bridge_unit_text(cfg, python),
                    encoding="utf-8", newline="\n")
    log(f"wrote {unit}")
    return {"script": str(dest), "config": str(conf), "unit": str(unit),
            "linux_path": f"{cfg.wsl.bridge_deploy_linux}/wsl-bridge.py"}


def merge_hub_toml(existing: str, sections: dict) -> str:
    """Fold what provisioning decided INTO the config that is already there.

    Re-running the installer on a configured machine used to replace the file
    with only the four sections `cmd_install` builds, so every hand-pinned key
    in any other section was gone from the live config (the .bak kept it, which
    is not the same as keeping it). On this machine that was `[wsl] distro` and
    `home_linux` — pinned precisely because deriving them had already failed
    once — plus `[terminal]`, `[cx_ptt]` and `[hub]`.

    Line surgery, not a re-render, because the comments are the point. Chris's
    hub.toml explains WHY each key is pinned; a value-perfect rewrite that
    dropped those paragraphs would hand the next operator the same bug with the
    reasoning deleted. So an existing key has its line replaced in place, a new
    key is appended inside its section, and a new section goes on the end.
    Everything else — comments, blank lines, ordering — is untouched.

    A key provisioning just resolved WINS: it is the fresher fact, and it is
    pointing at something that was created seconds ago.
    """
    lines = existing.splitlines()
    # where each section's body starts and ends, by line index
    bounds: dict[str, list[int]] = {}
    current = ""
    for i, line in enumerate(lines):
        st = line.strip()
        if st.startswith("[") and st.endswith("]") and not st.startswith("[["):
            current = st[1:-1].strip()
            bounds[current] = [i + 1, i + 1]
        elif current:
            if st:
                bounds[current][1] = i + 1
    edits: dict[int, str] = {}          # line index -> replacement
    appends: dict[str, list[str]] = {}  # section -> lines to insert at its end
    tail: list[str] = []                # whole sections that do not exist yet
    for name, keys in sections.items():
        if not keys:
            continue
        if name not in bounds:
            tail.append(f"[{name}]")
            tail += [f"{k} = {_toml(v)}" for k, v in keys.items()]
            tail.append("")
            continue
        start, end = bounds[name]
        for k, v in keys.items():
            rendered = f"{k} = {_toml(v)}"
            for i in range(start, end):
                st = lines[i].strip()
                if st.startswith("#") or "=" not in st:
                    continue
                if st.partition("=")[0].strip() == k:
                    edits[i] = rendered
                    break
            else:
                appends.setdefault(name, []).append(rendered)
    out: list[str] = []
    for i, line in enumerate(lines):
        out.append(edits.get(i, line))
        for name, (start, end) in bounds.items():
            if i + 1 == end and name in appends:
                out += appends.pop(name)
    for name, extra in appends.items():   # section with an empty body
        out += extra
    if tail:
        if out and out[-1].strip():
            out.append("")
        out += tail
    return "\n".join(out) + "\n"


def bridge_decision(cfg, force: bool = False, skip: bool = False,
                    exists=None) -> tuple[bool, str]:
    """Should this install deploy the WSL bridge? Decision only, no side effects.

    "One app" means a fresh clone plus `ping-hub install` yields the WHOLE
    thing, and the bridge was the other half missing from that promise — it sat
    behind an opt-in flag, so a recipient's install wrote no bridge and
    registered no unit. So the default flips to ON.

    It does NOT flip to "always": the original flag's warning is still true,
    this writes into a live WSL home and the bridge it replaces may be running
    right now. That is a real risk on a configured machine and no risk at all
    on a bare one, and the two are distinguishable — so ask.
    """
    exists = exists or (lambda p: Path(p).exists())
    if skip:
        return False, "skipped (--no-deploy-bridge)"
    if not cfg.wsl.enabled or not cfg.wsl.bridge_deploy_unc:
        return False, "no WSL side on this machine"
    if force:
        return True, "forced (--deploy-bridge)"
    deployed = Path(cfg.wsl.bridge_deploy_unc) / "wsl-bridge.py"
    if exists(deployed):
        return False, (f"a bridge is already deployed at {deployed} and may be "
                       f"running; pass --deploy-bridge to replace it")
    return True, "no bridge deployed yet"


def write_hub_toml(path: Path, sections: dict, log=_log) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(render_hub_toml(sections), encoding="utf-8")
        log(f"wrote {path}")
        return path
    backup = path.with_suffix(".toml.bak")
    shutil.copyfile(path, backup)
    log(f"existing config backed up to {backup.name}")
    existing = path.read_text(encoding="utf-8")
    try:
        tomllib.loads(existing)
    except ValueError as e:
        # louder than a missing file, same as `config.load`: someone edited it
        # and got it wrong, and merging into a broken file would bury that
        raise InstallError(f"{path} is not valid TOML ({e}); fix it or move it "
                           f"aside, then re-run") from e
    text = merge_hub_toml(existing, sections)
    # never ship a config this function could not read back. The whole value of
    # the merge is that the file survives a re-run; a merge that corrupted it
    # would be strictly worse than the clobber it replaces.
    try:
        doc = tomllib.loads(text)
    except ValueError as e:
        raise InstallError(f"merging into {path} produced invalid TOML ({e}); "
                           f"the original is untouched at {backup.name}") from e
    for name, keys in sections.items():
        for k in keys:
            if k not in (doc.get(name) or {}):
                raise InstallError(f"merge lost [{name}] {k}; the original is "
                                   f"untouched at {backup.name}")
    path.write_text(text, encoding="utf-8")
    log(f"merged {len(sections)} section(s) into {path}")
    return path
