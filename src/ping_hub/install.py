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
import sys
import tarfile
import tempfile
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
    r = subprocess.run([str(py), "-m", "pip", "install", "-q", *packages],
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


def deploy_bridge(cfg, deploy_unc: str | None = None,
                  config_unc: str | None = None, log=_log) -> dict:
    """Copy the bridge into WSL and drop its config beside it.

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
    return {"script": str(dest), "config": str(conf),
            "linux_path": f"{cfg.wsl.bridge_deploy_linux}/wsl-bridge.py"}


def write_hub_toml(path: Path, sections: dict, log=_log) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(".toml.bak")
        shutil.copyfile(path, backup)
        log(f"existing config backed up to {backup.name}")
    path.write_text(render_hub_toml(sections), encoding="utf-8")
    log(f"wrote {path}")
    return path
