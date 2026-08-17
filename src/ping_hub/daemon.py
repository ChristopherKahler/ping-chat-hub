"""ping-chat-hub daemon — localhost HTTP + SSE over the engine.

Same stdlib ThreadingHTTPServer posture as the claude-chat daemon this app
forked. Port 7799. Endpoints:

  GET  /                       hub.html
  GET  /api/threads            roster snapshot (both tabs derive from this)
  GET  /api/thread?side&title  journal tail for one thread
  POST /api/send               {side,title,msg} -> base relay ping --from chris
  GET  /api/events             SSE stream (roster + message events)
  POST /bridge/event           WSL bridge push (H3b): {kind:roster|ping,...}

Boot registers the standing `chris` title (synthetic session id) so every
session's `base relay ping --to chris` resolves, and the engine keeps
chris's .watching sentinel fresh — the hub IS chris's wake monitor.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time

_VOICES = None
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ping_hub import config, proc, spawn
from ping_hub.engine import Engine, INBOX_ROOT, HUB_TITLE

CFG = config.get()

PORT = CFG.hub.port
CHRIS_SESSION = CFG.hub.standing_session
HTML = Path(__file__).with_name("hub.html")

engine = Engine(side="win")


# WSL locations are FUNCTIONS, not constants: resolving them costs a `wsl.exe`
# call (the distro name and the home dir are asked of WSL, not spelled out),
# and importing this module must never block on that. Each is cached after the
# first call.
def wsl_home_linux() -> str:
    return CFG.wsl.home_linux


def wsl_home_unc() -> str:
    return CFG.wsl.home_unc


def wsl_unc_root() -> str:
    return CFG.wsl.unc_root


def register_chris() -> None:
    try:
        proc.run(
            [CFG.paths.base_bin, "relay", "register", "--as", HUB_TITLE,
             "--session", CHRIS_SESSION],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def touch_chris_sentinel() -> None:
    d = INBOX_ROOT / HUB_TITLE
    n = 0
    while True:
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / ".watching").touch()
        except OSError:
            pass
        n += 1
        if n % 36 == 0:  # ~3 min: keep chris's heartbeat alive so pings to
            register_chris()  # chris stop warning "may be dead"
        time.sleep(5)


def wsl_profile() -> str:
    """Windows Terminal profile for the WSL side, read from cx.toml [switch].
    Empty means 'let WT pick its default' — a working spawn, not a broken one."""
    return CFG.terminal.wsl_profile


SETTINGS_FILE = None  # set after engine import below


def hub_settings() -> dict:
    from ping_hub.engine import HUB_DIR
    try:
        with open(HUB_DIR / "settings.json", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_hub_settings(s: dict) -> None:
    from ping_hub.engine import HUB_DIR
    with open(HUB_DIR / "settings.json", "w", encoding="utf-8") as fh:
        json.dump(s, fh, indent=1)


GATED_DOC_WIN = CFG.paths.gated_doc
GATED_DOC_WSL = CFG.paths.gated_doc_wsl


def spawn_tab(side: str, claude_args: list[str], cwd: str | None = None,
              title: str | None = None, prompt: str | None = None) -> None:
    """Open a real terminal tab running claude (+args). The mechanics live in
    ping_hub.spawn.<adapter> — which terminal this is, is the one genuinely
    platform-shaped thing in the hub."""
    spawn.spawn(CFG, side, claude_args, cwd, title, prompt)


def pre_trust(side: str, cwd: str) -> None:
    """Seed Claude Code's per-folder trust + global bypass acceptance in that
    side's ~/.claude.json BEFORE the spawn, so the new session boots with zero
    prompts (folder-trust, hooks-trust, bypass warning) in any workspace."""
    if side == "wsl":
        home = wsl_home_unc()
        if not home:
            return   # no WSL side to seed. An empty root would make this a
        # RELATIVE path and write a stray .claude.json into the daemon's cwd
    else:
        home = str(Path.home())
    cfg = Path(home) / ".claude.json"
    try:
        d = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return  # unreadable config (WSL down / first boot): spawn anyway
    d["bypassPermissionsModeAccepted"] = True
    p = d.setdefault("projects", {}).setdefault(cwd, {})
    p["hasTrustDialogAccepted"] = True
    p["hasTrustDialogHooksAccepted"] = True
    p["hasCompletedProjectOnboarding"] = True
    p.setdefault("projectOnboardingSeenCount", 1)
    tmp = cfg.parent / (cfg.name + ".hub-tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    tmp.replace(cfg)


def _workspace_paths(toml_text: str) -> list[str]:
    """path values from base.toml [[workspace]] blocks (base's registry —
    same shape claude-chat's `workspace import` reads)."""
    out, in_block = [], False
    for line in toml_text.splitlines():
        s = line.strip()
        if s == "[[workspace]]":
            in_block = True
        elif s.startswith("["):
            in_block = False
        elif in_block:
            m = re.match(r'path\s*=\s*"(.+)"', s)
            if m:
                out.append(m.group(1))
    return out


def _md_files(*dirs) -> list[dict]:
    """{name, path} for *.md in dirs, newest first. UNC WSL paths are
    translated to native /home/... so the spawned session can read them."""
    out = []
    for d in dirs:
        try:
            files = sorted(Path(d).glob("*.md"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue
        root = wsl_unc_root()
        for p in files:
            path = str(p)
            if root and path.startswith(root):
                path = path[len(root):].replace("\\", "/")
            out.append({"name": p.stem, "path": path})
    return out


def _projects_cli(side: str, ws: str) -> list[dict]:
    """Registered base projects scoped to workspace ws, parsed from
    `base project list`'s markdown table — the CLI answers for whatever
    workspace it runs inside (home = global tier)."""
    if side == "win":
        cmd, run_cwd = [CFG.paths.base_bin, "project", "list"], ws
    else:
        cmd, run_cwd = ["wsl", "-e", "bash", "-lc",
                        f"cd '{ws}' && base project list"], str(Path.home())
    try:
        r = proc.run(cmd, capture_output=True, text=True, timeout=20,
                           cwd=run_cwd)
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in r.stdout.splitlines():
        m = re.match(r"\|\s*([^|]+?)\s*\|\s*(\w+)\s*\|", line)
        if m and m.group(1) not in ("name",) and not set(m.group(1)) <= {"-"}:
            rows.append({"name": m.group(1), "status": m.group(2)})
    return rows


def gated_codename(side: str, fork: str | None) -> str:
    """Builder title pinned at spawn (BASE_RELAY_AS) so the orchestrator ping
    can name the child before it boots. Fork-stem based, collision-checked
    against that side's registry."""
    stem = re.sub(r"[^\w.-]", "-", Path(fork).stem)[:18] if fork else "gated"
    base_name = f"{stem}-builder"
    reg_path = (Path(wsl_home_unc()) / ".base-gbl" / ".base" / "sessions.json"
                if side == "wsl"
                else CFG.paths.base_store / "sessions.json")
    taken: set = set()
    try:
        taken = set(json.loads(reg_path.read_text(encoding="utf-8")).get("sessions") or {})
    except (OSError, ValueError):
        pass
    name, n = base_name, 2
    while name in taken:
        name, n = f"{base_name}-{n}", n + 1
    return name


def set_relation(child_key: str, parent_key: str) -> None:
    """hub/relations.json: child 'side:title' -> parent 'side:title';
    empty parent clears the designation."""
    from ping_hub.engine import HUB_DIR
    rels = {}
    try:
        rels = json.loads((HUB_DIR / "relations.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if parent_key:
        rels[child_key] = parent_key
    else:
        rels.pop(child_key, None)
    (HUB_DIR / "relations.json").write_text(json.dumps(rels, indent=1),
                                            encoding="utf-8")


def spawn_meta(side: str, cwd: str | None) -> dict:
    """Launcher dropdown data SCOPED to the selected workspace (Chris rule:
    picking a workspace drills everything down to it). Home workspace = the
    global tier; any other workspace = its own .base/ tier only."""
    if side == "wsl":
        home = wsl_home_linux()
        ws = (cwd or home).rstrip("/") or home
        is_home = ws == home
        unc = Path(wsl_unc_root() + ws.replace("/", "\\"))
        wslh = Path(wsl_home_unc())
        hdirs = ([wslh / ".base-gbl" / "handoffs", wslh / ".base" / "handoffs"]
                 if is_home else [unc / ".base" / "handoffs"])
        fdirs = ([wslh / ".base-gbl" / "forks", wslh / ".base" / "forks"]
                 if is_home else [unc / ".base" / "forks"])
    else:
        home = Path.home()
        ws = cwd or str(home)
        is_home = Path(ws) == home
        gbl = CFG.paths.base_gbl
        hdirs = ([gbl / "handoffs", home / ".base" / "handoffs"]
                 if is_home else [Path(ws) / ".base" / "handoffs"])
        fdirs = ([gbl / "forks", home / ".base" / "forks"]
                 if is_home else [Path(ws) / ".base" / "forks"])
    return {"projects": _projects_cli(side, ws),
            "handoffs": _md_files(*hdirs),
            "forks": _md_files(*fdirs)}


def list_workspaces() -> dict:
    """Registered base workspaces per side, existence-filtered (drops dead
    /tmp test entries), home first. WSL existence checked over the
    \\\\wsl.localhost share, so this needs WSL up — same as the bridge."""
    sides = {"win": [{"path": str(Path.home()), "name": "~ home"}], "wsl": []}
    if CFG.wsl.enabled and wsl_home_linux():
        sides["wsl"].append({"path": wsl_home_linux(), "name": "~ home"})
    try:
        text = (CFG.paths.base_gbl / "base.toml").read_text(encoding="utf-8")
        for p in _workspace_paths(text):
            if os.path.isdir(p):
                sides["win"].append({"path": p, "name": Path(p).name})
    except OSError:
        pass
    try:
        text = (Path(wsl_home_unc()) / ".base-gbl" / "base.toml").read_text(encoding="utf-8")
        for p in _workspace_paths(text):
            unc = wsl_unc_root() + p.replace("/", "\\")
            if os.path.isdir(unc):
                sides["wsl"].append({"path": p, "name": p.rstrip("/").rsplit("/", 1)[-1]})
    except OSError:
        pass
    return sides


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            body = HTML.read_bytes() if HTML.exists() else b"<h1>hub.html missing</h1>"
            self.send_response(200)
            # stale cached pages silently miss new endpoints (spawn-draft pull)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/threads":
            with engine.lock:
                self._json(sorted(engine.threads.values(), key=lambda t: t["title"]))
        elif u.path == "/api/thread":
            q = parse_qs(u.query)
            title = (q.get("title") or [""])[0]
            side = (q.get("side") or ["win"])[0]
            self._json(engine.tail(title, side=side))
        elif u.path == "/api/escalations":
            self._json(engine.escalations())
        elif u.path == "/api/responses":
            q = parse_qs(u.query)
            self._json(engine.transcript_responses(
                (q.get("title") or [""])[0], (q.get("side") or ["win"])[0]))
        elif u.path == "/api/settings":
            self._json(hub_settings())
        elif u.path == "/api/replacements":
            from ping_hub import replacements
            self._json(replacements.migrate_if_needed(CFG))
        elif u.path == "/api/capabilities":
            # the package is opinionated: voice affordances always render, so
            # the page needs to be able to say WHY there is no audio instead
            # of looking like a dead button (ready|absent|error|off)
            from ping_hub import capabilities
            self._json(capabilities.probe_all(CFG))
        elif u.path == "/api/workspaces":
            self._json(list_workspaces())
        elif u.path == "/api/spawn-meta":
            q = parse_qs(u.query)
            self._json(spawn_meta((q.get("side") or ["win"])[0],
                                  (q.get("cwd") or [""])[0] or None))
        elif u.path == "/api/spawn-draft":
            # hotkey-dictated launcher prompt, held server-side so it waits
            # across devices and modal closes until Launch or Clear
            from ping_hub.engine import HUB_DIR
            try:
                self._json({"text": (HUB_DIR / "spawn-draft.txt")
                            .read_text(encoding="utf-8")})
            except OSError:
                self._json({"text": ""})
        elif u.path in ("/manifest.json", "/sw.js", "/icon-192.png", "/icon-512.png"):
            p = Path(__file__).with_name("assets") / u.path.lstrip("/")
            if p.is_file():
                body = p.read_bytes()
                ct = {"json": "application/manifest+json", "js": "text/javascript",
                      "png": "image/png"}[p.suffix.lstrip(".")]
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"error": "not found"}, 404)
        elif u.path.startswith("/img/"):
            # serve attached images back to the thread view
            from ping_hub.engine import HUB_DIR
            rel = u.path[5:].replace("..", "")
            p = HUB_DIR / "images" / rel
            if p.is_file():
                body = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/" +
                                 (p.suffix.lstrip(".") or "jpeg").replace("jpg", "jpeg"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"error": "not found"}, 404)
        elif u.path == "/api/commands":
            # star-command palette source: parse commands.toml (WSL-owned
            # symlink — read-only here, never written)
            import tomllib
            try:
                with open(CFG.paths.base_gbl / "commands.toml", "rb") as fh:
                    doc = tomllib.load(fh)
                cmds = [{"name": c.get("name", ""), "description": c.get("description", "")}
                        for c in (doc.get("command") or doc.get("commands") or [])
                        if c.get("name")]
                self._json(sorted(cmds, key=lambda c: c["name"]))
            except (OSError, tomllib.TOMLDecodeError) as e:
                self._json({"error": str(e)}, 500)
        elif u.path == "/api/voices":
            # Kokoro voice names via `say --voices`, cached for the process
            global _VOICES
            if _VOICES is None:
                say = CFG.tts.command
                if not say:
                    _VOICES = [CFG.tts.default_voice]   # engine absent
                    self._json(_VOICES)
                    return
                try:
                    r = proc.run(
                        say + ["--voices"],
                        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
                    )
                    _VOICES = re.findall(r"\b([a-z]{2}_[a-z]+)\b", r.stdout or "")
                    _VOICES = sorted(set(_VOICES)) or [CFG.tts.default_voice]
                except (OSError, subprocess.TimeoutExpired):
                    _VOICES = [CFG.tts.default_voice]
            self._json(_VOICES)
        elif u.path == "/api/soundlist":
            media = CFG.paths.sound_dir
            try:
                wavs = sorted(p.name for p in media.glob("*.wav"))[:40]
            except OSError:
                wavs = []
            self._json(wavs)
        elif u.path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            ch: queue.Queue = queue.Queue()
            engine.listeners.append(ch.put)
            try:
                while True:
                    try:
                        ev = ch.get(timeout=15)
                        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                pass
            finally:
                if ch.put in engine.listeners:
                    engine.listeners.remove(ch.put)
        else:
            self._json({"error": "not found"}, 404)

    def _stt(self):
        """Phone mic: browser posts a webm/opus blob; ffmpeg converts to 16k
        mono wav; the local Parakeet bridge (:8973, whisper.cpp convention,
        raw-wav-body fallback) transcribes on this machine."""
        import tempfile
        import urllib.request as ur
        try:
            n = int(self.headers.get("Content-Length") or 0)
            blob = self.rfile.read(n)
            with tempfile.TemporaryDirectory() as td:
                src = Path(td) / "in.webm"
                wav = Path(td) / "out.wav"
                src.write_bytes(blob)
                r = proc.run(
                    [CFG.stt.ffmpeg, "-y", "-i", str(src),
                     "-ar", "16000", "-ac", "1", str(wav)],
                    capture_output=True, timeout=60,
                )
                if r.returncode != 0 or not wav.exists():
                    self._json({"ok": False, "detail": "audio convert failed"}, 500)
                    return
                req = ur.Request(CFG.stt.url,
                                 data=wav.read_bytes(),
                                 headers={"Content-Type": "audio/wav"})
                with ur.urlopen(req, timeout=120) as resp:
                    out = json.loads(resp.read())
            # the same substitution list cx-ptt applies, from the same store:
            # one spoken correction, however the words arrived
            from ping_hub import replacements
            text = replacements.apply_for(CFG, out.get("text", ""))
            self._json({"ok": True, "text": text})
        except Exception as e:  # phone flow must never 500 opaquely
            self._json({"ok": False, "detail": str(e)}, 500)

    def _tts(self, payload):
        """Kokoro speaks server-side to a wav; the page plays it — works on
        the phone over the tailnet, not just desktop speakers."""
        import tempfile
        text = (payload.get("text") or "")[:600]
        voice = payload.get("voice") or CFG.tts.default_voice
        if not text.strip():
            self._json({"ok": False, "detail": "no text"}, 400)
            return
        say = CFG.tts.command
        if not say:
            # absent, not silently-nothing: the page can say WHY there is no
            # audio instead of looking like a dead button
            self._json({"ok": False, "detail": "tts engine not installed"}, 503)
            return
        try:
            with tempfile.TemporaryDirectory() as td:
                wav = Path(td) / "tts.wav"
                r = proc.run(
                    say + ["--out", str(wav), "--voice", voice, text],
                    capture_output=True, timeout=120,
                )
                if r.returncode != 0 or not wav.exists():
                    self._json({"ok": False, "detail": "tts failed"}, 500)
                    return
                body = wav.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._json({"ok": False, "detail": str(e)}, 500)

    def _attach(self, u):
        """Image from the page -> hub/images/<title>/ -> ping with the path in
        refs so the session goes and Reads it; the thread renders it inline."""
        from ping_hub.engine import HUB_DIR
        from urllib.parse import parse_qs
        q = parse_qs(u.query)
        title = (q.get("title") or [""])[0]
        side = (q.get("side") or ["win"])[0]
        note = (q.get("note") or [""])[0]
        given = (q.get("name") or [""])[0]
        ctype = self.headers.get("Content-Type", "")
        images = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
                  "image/gif": "gif"}
        is_image = ctype.split(";")[0] in images
        if is_image:
            ext, bucket, tag = images[ctype.split(";")[0]], "images", "image"
        else:
            # a dropped PDF or log used to be written as .jpg, because the map
            # defaulted everything unknown to an image extension
            ext = Path(given).suffix.lstrip(".").lower() or "bin"
            ext = re.sub(r"[^\w]", "", ext)[:12] or "bin"
            bucket, tag = "files", "file"
        if not title:
            self._json({"ok": False, "detail": "title required"}, 400)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 25_000_000:
                self._json({"ok": False, "detail": "too large"}, 413)
                return
            blob = self.rfile.read(n)
            d = HUB_DIR / bucket / title
            d.mkdir(parents=True, exist_ok=True)
            stem = re.sub(r"[^\w.-]", "-", Path(given).stem)[:40]
            fname = f"att-{int(time.time() * 1000)}{'-' + stem if stem else ''}.{ext}"
            (d / fname).write_bytes(blob)
            path = str(d / fname)
            # a drop stores the file and hands the path back for Chris to
            # compose around; the paperclip pings straight away. Doing both
            # would send the same attachment twice.
            if (q.get("send") or ["1"])[0] == "0":
                self._json({"ok": True, "path": path, "kind": tag,
                            "detail": "stored"})
                return
            msg = f"[{tag}] {path}" + (f" — {note}" if note else "")
            r = proc.run(
                ["base", "relay", "ping", "--to", title, "--from", HUB_TITLE,
                 "--msg", msg, "--refs", path],
                capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
            )
            self._json({"ok": r.returncode == 0, "path": path, "kind": tag,
                        "detail": (r.stdout + r.stderr).strip()[:200]})
        except Exception as e:
            self._json({"ok": False, "detail": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/stt":
            self._stt()
            return
        if u.path == "/api/attach":
            self._attach(u)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "bad json"}, 400)
            return
        if u.path == "/api/tts":
            self._tts(payload)
        elif u.path == "/api/send":
            title = payload.get("title", "")
            msg = payload.get("msg", "")
            if not title or not msg:
                self._json({"error": "title and msg required"}, 400)
                return
            ok, out = engine.send(title, msg, side=payload.get("side", "win"))
            self._json({"ok": ok, "detail": out}, 200 if ok else 502)
        elif u.path == "/api/escalation-reply":
            ok, out = engine.reply_escalation(
                payload.get("id", ""), payload.get("msg", ""),
            )
            self._json({"ok": ok, "detail": out}, 200 if ok else 400)
        elif u.path == "/api/relation":
            try:
                set_relation(payload.get("child", ""), payload.get("parent", ""))
                self._json({"ok": True})
            except OSError as e:
                self._json({"ok": False, "detail": str(e)}, 500)
        elif u.path == "/api/channel":
            ok, out = engine.set_channel(
                payload.get("title", ""), payload.get("side", "win"),
                str(payload.get("slot", "")),
            )
            self._json({"ok": ok, "detail": out}, 200 if ok else 400)
        elif u.path == "/api/spawn":
            # "New chat" = a REAL terminal tab in the user's current Windows
            # Terminal window (Chris directive, H1 §4). -w 0 targets the
            # most-recently-used window. The spawned claude session
            # auto-registers + arms via the shipped wake contract; its thread
            # appears here within one roster poll.
            side = payload.get("side", "win")
            cwd = (payload.get("cwd") or "").strip() or None
            s = hub_settings()
            # AskUserQuestion is invisible to Chris driving from the hub —
            # hard-deny it and teach the relay path instead (Chris 2026-08-17)
            args = ["--dangerously-skip-permissions"]
            if CFG.spawn.disallowed_tools:
                args += ["--disallowedTools", ",".join(CFG.spawn.disallowed_tools)]
            if s.get("spawn_model"):
                m = s["spawn_model"]
                # opus always boots the 1M-context variant (Chris directive)
                args += ["--model",
                         "opus[1m]" if (m == "opus" and CFG.spawn.opus_1m) else m]
            if s.get("spawn_effort"):
                args += ["--effort", s["spawn_effort"]]
            try:
                pre_trust(side, cwd or (wsl_home_linux() if side == "wsl"
                                        else str(Path.home())))
                # boot briefing: selected project/parent/handoff/fork become
                # prompt preamble blocks ahead of Chris's own text
                pieces = ["NO QUESTION DIALOGS: Chris drives from a phone app and "
                          "will NEVER see an AskUserQuestion dialog (the tool is "
                          "denied). Ask him questions as a plain relay ping: "
                          "base relay ping --to chris --msg \"<your question>\" "
                          "— and continue or idle until his ping comes back."]
                proj = (payload.get("project") or "").strip()
                parent = (payload.get("parent") or "").strip()
                handoffs = [str(h).strip() for h in (payload.get("handoffs") or [])
                            if str(h).strip()]
                if not handoffs and (payload.get("handoff") or "").strip():
                    handoffs = [payload["handoff"].strip()]
                forks = [str(f).strip() for f in (payload.get("forks") or [])
                         if str(f).strip()]
                if not forks and (payload.get("fork") or "").strip():
                    forks = [payload["fork"].strip()]
                fork = forks[0] if forks else ""
                # no parent picked + auto-report on -> Chris IS the parent:
                # the boot ack arrives as a ping in his hub (Chris rule)
                if not parent and payload.get("report_boot", True):
                    parent = "chris"
                gated = bool(payload.get("gated")) and bool(parent)
                spawn_title = payload.get("title") or None
                if proj:
                    pieces.append(
                        f"PROJECT: this session serves '{proj}'. Immediately after "
                        f"your codename registers, run: base relay register --as "
                        f"<your-codename> --project {proj} (append-only hub tag).")
                if gated:
                    # falcon-approved shape 2026-08-17: pinned codename, doc-
                    # conformant self-reporting boot; fork folds in here
                    spawn_title = spawn_title or gated_codename(side, fork or None)
                    doc = GATED_DOC_WSL if side == "wsl" else GATED_DOC_WIN
                    pieces.append(
                        f"GATED BUILD: you are builder '{spawn_title}' reporting to "
                        f"orchestrator '{parent}'. Read {doc} — that process governs "
                        f"everything you do. "
                        + (f"Your fork doc{'s' if len(forks) > 1 else ''}, the FULL "
                           f"contract: {'; '.join(forks)}. " if forks else
                           f"No fork assigned yet — your orchestrator briefs you with one. ")
                        + f"Once your wake monitor is armed and .status is set, ping: "
                        f"base relay ping --to {parent} --msg \"{spawn_title}: idle awaiting "
                        f"orders\" — that ping IS the start signal. The fork doc and the "
                        f"process doc are the entire briefing; do not wait for more.")
                elif parent and payload.get("report_boot", True):
                    pieces.append(
                        f"REPORTING: you report to the relay session '{parent}'. "
                        f"As soon as you have registered your codename, ping your parent: "
                        f"base relay ping --to {parent} --msg \"<your-codename>: booted, on task\" "
                        f"— and ping {parent} again at completion, on blockers, and for "
                        f"decisions above your pay grade.")
                elif parent:
                    pieces.append(
                        f"REPORTING: you report to the relay session '{parent}'. Do NOT "
                        f"send a boot ping — a task ping may already be waiting in your "
                        f"inbox; act on it when it lands. Ping {parent} at completion, on "
                        f"blockers, and for decisions above your pay grade.")
                for h in handoffs:
                    pieces.append(f"HANDOFF: read {h} and resume that work "
                                  f"exactly where it left off.")
                if not gated:
                    for f in forks:
                        pieces.append(f"FORK: read {f} — it is your build spec; execute it.")
                if (payload.get("prompt") or "").strip():
                    pieces.append(payload["prompt"].strip())
                prompt = "\n\n".join(pieces)
                if parent and parent != "chris" and spawn_title:
                    set_relation(f"{side}:{spawn_title}", f"{side}:{parent}")
                spawn_tab(side, args, cwd, title=spawn_title, prompt=prompt or None)
                if gated:
                    # the parent expects the child BEFORE it lands — instant ping
                    doc = GATED_DOC_WSL if side == "wsl" else GATED_DOC_WIN
                    engine.send(parent,
                        f"GATED BUILD INBOUND: child '{spawn_title}' booting now — side "
                        f"{side}, workspace {cwd or 'home'}, project {proj or 'untagged'}, "
                        f"fork {'; '.join(forks) or 'UNASSIGNED (assign per doc)'}, style gated. "
                        f"Process: {doc}. It pings you 'idle awaiting orders' when armed — "
                        f"you are its orchestrator; prep G0.", side=side, sender="hub")
                self._json({"ok": True, "title": spawn_title,
                            "detail": f"spawning {side} tab in {cwd or 'home'}"})
            except OSError as e:
                self._json({"ok": False, "detail": str(e)}, 500)
        elif u.path == "/api/replacements":
            from ping_hub import replacements
            try:
                doc = replacements.load(CFG)
                doc["pairs"] = replacements.normalise(payload.get("pairs"))
                doc["imported_from_cx_toml"] = True   # editing here supersedes
                replacements.save(CFG, doc)
                self._json({"ok": True, "count": len(doc["pairs"])})
            except OSError as e:
                self._json({"ok": False, "detail": str(e)}, 500)
        elif u.path == "/api/settings":
            try:
                save_hub_settings(payload)
                self._json({"ok": True})
            except OSError as e:
                self._json({"ok": False, "detail": str(e)}, 500)
        elif u.path == "/api/spawn-draft":
            from ping_hub.engine import HUB_DIR
            df = HUB_DIR / "spawn-draft.txt"
            try:
                if payload.get("clear"):
                    df.unlink(missing_ok=True)
                elif (payload.get("text") or "").strip():
                    old = ""
                    try:
                        old = df.read_text(encoding="utf-8")
                    except OSError:
                        pass
                    df.write_text((old + " " + payload["text"].strip()).strip(),
                                  encoding="utf-8")
                self._json({"ok": True})
            except OSError as e:
                self._json({"ok": False, "detail": str(e)}, 500)
        elif u.path == "/api/title":
            engine.set_title(payload.get("side", "win"), payload.get("title", ""),
                             payload.get("session_id", ""), payload.get("text", "").strip())
            self._json({"ok": True})
        elif u.path == "/api/escalation-dismiss":
            ok = engine.dismiss_escalation(payload.get("id", ""))
            self._json({"ok": ok}, 200 if ok else 404)
        elif u.path == "/api/escalation-resume":
            # resume the asking session in a real WT tab, bypass flag on
            card = next((c for c in engine.escalations()
                         if c["id"] == payload.get("id", "")), None)
            if not card or not card.get("session_id"):
                self._json({"ok": False, "detail": "no session id on card"}, 404)
                return
            try:
                spawn_tab(card.get("side", "win"),
                          ["--dangerously-skip-permissions", "--resume", card["session_id"]])
                self._json({"ok": True, "detail": f"resuming {card['from']} ({card['session_id'][:8]})"})
            except OSError as e:
                self._json({"ok": False, "detail": str(e)}, 500)
        elif u.path == "/api/focus":
            # selection -> hub/state.json; cx-ptt's ctrl+shift+space reads it
            from ping_hub.engine import HUB_DIR
            try:
                with open(HUB_DIR / "state.json", "w", encoding="utf-8") as fh:
                    json.dump({"focused": {
                        "title": payload.get("title", ""),
                        "side": payload.get("side", "win"),
                    }}, fh)
                self._json({"ok": True})
            except OSError as e:
                self._json({"ok": False, "detail": str(e)}, 500)
        elif u.path == "/bridge/event":
            # H3b: WSL bridge pushes {kind: "roster"|"ping", ...} — wire when
            # the bridge daemon lands. Accept now so the bridge can be built
            # against a live endpoint.
            self._json({"ok": True, "note": "bridge ingestion lands at H3b"})
        else:
            self._json({"error": "not found"}, 404)


def main() -> None:
    # a shadow/test instance sets register_standing_title=false: two daemons
    # re-binding the same standing title churn its registration and both touch
    # its sentinel, which would move state under the live cockpit
    if CFG.hub.register_standing_title:
        register_chris()
    # before the loops start: recover anything delivered while we were down
    try:
        engine.backfill()
    except Exception as e:            # never block a boot on a repair step
        print(f"backfill skipped: {e}", flush=True)
    engine.run()
    if CFG.hub.register_standing_title:
        threading.Thread(target=touch_chris_sentinel, daemon=True).start()
    engine.refresh_roster()
    # 0.0.0.0: WSL's tailscale serve proxies phone traffic in via the NAT
    # gateway IP, which cannot reach a 127.0.0.1 bind. Remote access is
    # tailnet-only (the serve is not funneled); LAN exposure is Chris's own
    # network.
    srv = ThreadingHTTPServer((CFG.hub.bind, PORT), Handler)
    print(f"ping-chat-hub on http://127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
