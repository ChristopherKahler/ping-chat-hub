"""ping-chat-hub daemon — localhost HTTP + SSE over the engine.

Same stdlib ThreadingHTTPServer posture as the claude-chat daemon this app
forked. Port 7799. Endpoints:

  GET  /                       hub.html
  GET  /api/threads            roster snapshot (both tabs derive from this)
  GET  /api/bridge             WSL bridge liveness {up, since, detail, enabled}
  GET  /api/cxptt              cx-ptt daemon liveness {alive, since, detail, enabled}
  GET  /api/resume-preview     can this dead thread be resumed, and why not
  POST /api/resume             {side,title} -> boot `claude --resume <sid>`, codename pinned
  GET  /api/thread?side&title  journal tail for one thread
  POST /api/send               {side,title,msg} -> base relay ping --from chris
  POST /api/end-then-close     {side,title} -> ask the session to close out
                               (handoff first) and then end itself
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


# ── kokoro voice list ────────────────────────────────────────────────────────
# `say --voices` looks cheap and is not: when kokoro's own daemon is down the
# CLI starts it and polls for up to 30s (kokoro/say.py:313) while a 310MB ONNX
# model loads. Measured 163ms with that daemon up, 2.7s forced in-process. A
# request handler must never be the thing that pays for it.
VOICE_REQUEST_TIMEOUT = 2.0     # on the request path: answer, never block
VOICE_WARM_TIMEOUT = 60.0       # off it: wait out the model load, once


def resolve_voices(timeout: float) -> list[str] | None:
    """Voice names, or None meaning "ask again later".

    None is NOT "no voices". Handing back the single default as though it were
    the whole list would make a temporary stall look like a permanently
    one-voice engine, and send Chris hunting a broken install that is fine.
    """
    say = CFG.tts.command
    if not say:
        return [CFG.tts.default_voice]      # engine absent: a real, final answer
    try:
        r = proc.run(say + ["--voices"], capture_output=True, text=True,
                     timeout=timeout, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    names = sorted(set(re.findall(r"\b([a-z]{2}_[a-z]+)\b", r.stdout or "")))
    return names or [CFG.tts.default_voice]


def warm_voices() -> None:
    """Pay the model load once, at boot, off every request path."""
    global _VOICES
    try:
        got = resolve_voices(VOICE_WARM_TIMEOUT)
        if got is not None:
            _VOICES = got
    except Exception:
        pass    # a cosmetic voice list must never take the daemon down with it


def start_voice_warm() -> threading.Thread:
    """Start the warm and RETURN — the bind below does not wait for it.

    A machine with no kokoro is a silent no-op, never a boot error: Albert's
    install must not care whether he owns a speech engine.
    """
    t = threading.Thread(target=warm_voices, daemon=True, name="voice-warm")
    t.start()
    return t


MODELS = ("fable", "opus", "sonnet")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def model_effort_args(settings: dict, model=None, effort=None) -> list[str]:
    """--model/--effort for a spawn: launcher choice first, settings second.

    ONE helper for both sources, because the opus 1M wrap has to apply to
    whichever wins. If it only wrapped the settings value, picking `opus` in
    the launcher would quietly boot a different context window than picking
    `opus` in settings -- same word on screen, different session.

    An unrecognised value is DROPPED rather than forwarded: the CLI would
    reject it and the tab would die on boot with an error Chris never sees,
    since the spawn window closes with it.
    """
    args: list[str] = []
    m = str(model or "").strip() or str(settings.get("spawn_model") or "").strip()
    e = str(effort or "").strip() or str(settings.get("spawn_effort") or "").strip()
    if m in MODELS:
        # opus always boots the 1M-context variant (Chris directive)
        args += ["--model", "opus[1m]" if (m == "opus" and CFG.spawn.opus_1m) else m]
    if e in EFFORTS:
        args += ["--effort", e]
    return args


def read_commands() -> list[dict]:
    """Star commands from the WSL-owned commands.toml. READ ONLY — this file
    belongs to base, and nothing here ever writes it.

    One function, not two parses: the palette and the spoken-fix import must
    agree on what a command is, or an import generates a rule for a command the
    palette cannot send.
    """
    import tomllib
    with open(CFG.paths.base_gbl / "commands.toml", "rb") as fh:
        doc = tomllib.load(fh)
    cmds = [{"name": c.get("name", ""), "description": c.get("description", "")}
            for c in (doc.get("command") or doc.get("commands") or [])
            if c.get("name")]
    return sorted(cmds, key=lambda c: c["name"])


GATED_DOC_WIN = CFG.paths.gated_doc
GATED_DOC_WSL = CFG.paths.gated_doc_wsl


def close_out_request(title: str, side: str, port: int = PORT) -> str:
    """The instruction CLEAR sends a session that has no handoff yet.

    Chris's ruling: when he presses the button he wants the session to close --
    but a session with nothing on disk closes with its work unwritten. So the
    modal offers this instead: the session runs its own close-out ritual,
    registers a handoff, and only then ends itself. The next boot of that
    codename picks the fresh handoff up.

    Two things this message must get right, both learned the hard way:

    1. It NEVER carries the literal star-command trigger. Receiving sessions'
       hooks keyword-match relayed text, and a trigger quoted inside a message
       fires the ritual attributed to Chris -- it happened six-plus times on
       2026-08-18 and nearly closed a working builder. Spelled out, it reads
       the same to a human and matches nothing.
    2. The self-close call is side-shaped. From WSL, 127.0.0.1 is NOT this
       machine: WSL is NAT'd here, so the hub answers on the default gateway
       (measured: gateway 200, loopback refused). A wsl session handed the
       loopback form would write its handoff and then fail to close.
    """
    body = '{"title": "%s", "side": "%s"}' % (title, side)
    if side == "wsl":
        call = ("HOST=$(ip route show default | awk '{print $3}')   # WSL is "
                "NAT'd here; the hub is not on this side's loopback\n"
                "   curl -s -X POST \"http://$HOST:%d/api/close-session\" \\\n"
                "        -H 'Content-Type: application/json' -d '%s'"
                % (port, body))
    else:
        call = ("Invoke-RestMethod -Method Post -ContentType application/json "
                "`\n        -Uri http://127.0.0.1:%d/api/close-session "
                "-Body '%s'" % (port, body))
    return (
        "CLOSE OUT, THEN CLOSE YOURSELF — from the hub, Chris pressed the "
        "button on '%s'.\n\n"
        "You have no handoff on disk, so ending this terminal now would lose "
        "what you know. Do it in this order:\n\n"
        "1. Run your end-of-session ritual. It is the operator command spelled "
        "star-end (spelled, not typed as the trigger, so this message does not "
        "fire anyone's hooks in transit): run `base commands show end` and "
        "execute its canonical steps exactly — handoff written and registered, "
        "decisions logged.\n\n"
        "2. THEN end this terminal yourself:\n\n"
        "   %s\n\n"
        "That call ends this tab and everything running in it, so make it only "
        "after the handoff is on disk. If the close-out cannot finish, say so "
        "by pinging chris and stop — do not call it."
        % (title, call))


def spawn_tab(side: str, claude_args: list[str], cwd: str | None = None,
              title: str | None = None, prompt: str | None = None) -> None:
    """Open a real terminal tab running claude (+args). The mechanics live in
    ping_hub.spawn.<adapter> — which terminal this is, is the one genuinely
    platform-shaped thing in the hub."""
    spawn.spawn(CFG, side, claude_args, cwd, title, prompt)


def _cwd_spellings(cwd: str) -> list[str]:
    """Every string Claude Code might record for ONE folder.

    Claude Code keys `projects` by the raw cwd string it was launched with and
    never canonicalises it. Chris's own ~/.claude.json carries 16 backslash
    keys and 19 forward-slash keys — including both spellings of this very
    repo. Seed one spelling, get read under the other, and the folder-trust
    dialog appears: a spawned session that hangs forever with nobody watching
    it (two spawns lost this way, 2026-08-19).

    POSIX paths keep a single key: flipping their separators would write
    garbage keys into the WSL side's config, so only Windows-shaped paths
    get a second spelling.
    """
    out = [cwd]
    windowsish = "\\" in cwd or (len(cwd) > 1 and cwd[1] == ":")
    if windowsish:
        for alt in (cwd.replace("\\", "/"), cwd.replace("/", "\\")):
            if alt not in out:
                out.append(alt)
    return out


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
    # every spelling, not just the one the hub happens to hold — see
    # _cwd_spellings. Normalising instead would not close this: the hub does
    # not control which spelling Claude Code writes down for itself.
    for key in _cwd_spellings(cwd):
        p = d.setdefault("projects", {}).setdefault(key, {})
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
        elif u.path == "/api/version":
            # the open page polls this and reloads itself when it changes, so a
            # ship reaches a client that never gets closed
            self._json({"version": page_version()})
        elif u.path == "/api/bridge":
            # a SIBLING of /api/threads, not a field on it: that endpoint
            # returns a bare list and reshaping it would break the UI and every
            # probe reading it. Bridge-down has to be answerable even when the
            # roster is empty, which is exactly when it matters most.
            with engine.lock:
                self._json(dict(engine.bridge_state))
        elif u.path == "/api/resume-preview":
            # the mirror image of /api/clear-preview, and deliberately the SAME
            # probe: "can this be cleared" and "should this be resumed" are one
            # question asked from two sides, and two probes could disagree.
            from ping_hub import reap
            q = parse_qs(u.query)
            title = (q.get("title") or [""])[0]
            side = (q.get("side") or ["win"])[0]
            with engine.lock:
                t = dict(engine.threads.get(f"{side}:{title}") or {})
            ok, why = reap.confirm(reap.find_record(INBOX_ROOT, title))
            self._json({"title": title, "side": side,
                        "resumable": (bool(t.get("session_id")) and bool(t.get("cwd"))
                                      and not ok and not _still_reporting(t)),
                        "alive": ok,
                        "session_id": t.get("session_id", ""),
                        "cwd": t.get("cwd", ""),
                        "reason": _resume_reason(t, ok, why)})
        elif u.path == "/api/cxptt":
            # the same shape and the same reason as /api/bridge: a service the
            # hub supervises has to be able to report itself dead even when
            # nothing else on the page would show it
            with engine.lock:
                self._json(dict(engine.cxptt_state))
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
        elif u.path == "/api/clear-preview":
            # everything the confirm modal needs BEFORE anything is reaped:
            # can this session even be cleared, and which handoff would the
            # fresh one resume from
            from ping_hub import handoffs, reap
            q = parse_qs(u.query)
            title = (q.get("title") or [""])[0]
            side = (q.get("side") or ["win"])[0]
            with engine.lock:
                t = dict(engine.threads.get(f"{side}:{title}") or {})
            ok, why = reap.confirm(reap.find_record(INBOX_ROOT, title))
            match = handoffs.for_session(
                handoffs.listing(CFG.paths.base_bin), title, t.get("projects"))
            # The third shape, and the reason CLEAR dead-ended for three days:
            # a session that survived a reboot or a resume keeps a `.pid`
            # naming the process that died with the old one, so `confirm`
            # refuses forever while the session is plainly alive. It is not
            # reapable and it is not dead — it can be ASKED to close itself.
            stale = (not ok) and _still_reporting(t)
            self._json({"title": title, "side": side,
                        "reapable": ok, "reason": "" if ok else why,
                        "stale": stale,
                        "handoff": handoffs.describe(match)})
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
                # the worker itself must never be the stale thing: a cached sw
                # that hoards an old shell cannot be replaced by shipping a new
                # one it will not fetch
                if u.path == "/sw.js":
                    self.send_header("Cache-Control", "no-store")
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
            # star-command palette source: commands.toml (WSL-owned symlink —
            # read-only here, never written)
            import tomllib
            try:
                self._json(read_commands())
            except (OSError, tomllib.TOMLDecodeError) as e:
                self._json({"error": str(e)}, 500)
        elif u.path == "/api/voices":
            global _VOICES
            if _VOICES is None:
                got = resolve_voices(VOICE_REQUEST_TIMEOUT)
                if got is None:
                    # the engine is still waking. Answer NOW with the configured
                    # voice and SAY it is warming, rather than holding the whole
                    # settings modal for up to 30s. "warming" is a third state:
                    # not the real list, and not "this machine has one voice".
                    self._json({"voices": [CFG.tts.default_voice], "warming": True})
                    return
                _VOICES = got
            self._json({"voices": _VOICES, "warming": False})
        elif u.path == "/api/audio":
            from ping_hub import cxptt
            self._json({"devices": cxptt.read_devices(CFG),
                        "daemon": cxptt.status(CFG)})
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
            args += model_effort_args(s, payload.get("model"),
                                      payload.get("effort"))
            try:
                pre_trust(side, cwd or (wsl_home_linux() if side == "wsl"
                                        else str(Path.home())))
                # boot briefing: selected project/parent/handoff/fork become
                # prompt preamble blocks ahead of Chris's own text
                # The boot duties come FIRST and say so, because something
                # else in the child's context tells it to render a lettered
                # handoff menu as the first thing in its reply and it obeys
                # that literally. Measured on `kestrel` 2026-08-19: 28
                # transcript records, ONE assistant turn, ZERO tool calls — it
                # printed the menu and stopped, so it never registered a
                # status, never armed a wake monitor, and never reported in.
                # Two of Chris's pings sat unread in its inbox with nothing
                # watching to deliver them. A session that cannot be woken is
                # a session that is gone, whatever the card says.
                pieces = [
                    "FIRST TURN — do these three BEFORE you render anything "
                    "else, including any \"pick up where you left off\" list "
                    "a hook puts in your context (render that after, if at "
                    "all):\n"
                    "  1. make a tool call — any tool call. Your wake contract "
                    "arrives as a system reminder on the first one, and until "
                    "you have armed it a ping cannot reach you.\n"
                    "  2. arm the relay wake monitor exactly as that reminder "
                    "specifies, and write one line to your relay inbox "
                    "`.status` saying what you are doing.\n"
                    "  3. report in, per the REPORTING block below.\n"
                    "Rendering a menu and stopping leaves you unreachable: no "
                    "status, no waker, pings piling up unread.",
                    "NO QUESTION DIALOGS: Chris drives from a phone app and "
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
        elif u.path == "/api/cx-restart":
            from ping_hub import cxptt
            self._json(cxptt.restart(CFG))
        elif u.path == "/api/audio":
            from ping_hub import cxptt
            out = cxptt.set_device(CFG, str(payload.get("id", "")),
                                   str(payload.get("kind", "")))
            # a mic switch rebinds cx-ptt's input stream, which only happens on
            # restart. Chained here so the page cannot forget, and reported so
            # it can warn BEFORE the daemon goes down under someone dictating.
            if out.get("ok") and out.get("needs_restart"):
                out["restart"] = cxptt.restart(CFG)
            self._json(out)
        elif u.path == "/api/replacements/import":
            # star-commands -> spoken fixes. `names` is REQUIRED and an empty
            # list imports NOTHING: a favourites import with no favourites must
            # import zero, never everything, so absent and empty stay different
            # facts (they are the same falsy value, which is exactly the trap).
            import tomllib
            from ping_hub import replacements
            if not isinstance(payload.get("names"), list):
                self._json({"ok": False,
                            "detail": "names is required (a list; [] imports nothing)"},
                           400)
                return
            try:
                known = {c["name"] for c in read_commands()}
            except (OSError, tomllib.TOMLDecodeError) as e:
                self._json({"ok": False, "detail": f"commands.toml: {e}"}, 500)
                return
            asked = [str(n) for n in payload["names"]]
            # a name the file does not carry is REPORTED, never invented — an
            # import that quietly generates rules for commands that do not
            # exist is worse than one that refuses
            unknown = [n for n in asked if n not in known]
            try:
                out = replacements.import_commands(
                    CFG, payload.get("pairs"), [n for n in asked if n in known])
            except OSError as e:
                self._json({"ok": False, "detail": str(e)}, 500)
                return
            out["unknown"] = unknown
            self._json(out)
        elif u.path == "/api/end-then-close":
            # the third way out of the clear modal: do not end the session,
            # ASK it to close out and then end itself. Confirm first anyway --
            # a session whose record cannot be confirmed would write its
            # handoff and then be refused by /api/close-session, which is a
            # worse outcome than being told up front that it cannot close.
            from ping_hub import reap
            title = payload.get("title", "")
            side = payload.get("side", "win")
            if not title:
                self._json({"ok": False, "detail": "title required"}, 400)
                return
            with engine.lock:
                t = dict(engine.threads.get(f"{side}:{title}") or {})
            ok, why = reap.confirm(reap.find_record(INBOX_ROOT, title))
            # The confirm-first rule stays, with one carve-out it needed all
            # along: a session that is demonstrably REPORTING IN can close
            # itself, because it ends itself and never needs a confirmable
            # record to do it. Without this the button offered on a
            # live-but-stale card would fail every single time — which is the
            # dead end this fork exists to remove, moved one step later.
            if not ok and not _still_reporting(t):
                self._json({"ok": False, "detail": why}, 409)
                return
            sent, detail = engine.send(
                title, close_out_request(title, side, PORT), side=side)
            self._json({"ok": sent, "detail": detail}, 200 if sent else 502)
        elif u.path in ("/api/clear", "/api/close-session"):
            # CLEAR ends the session and boots the same codename back with its
            # handoff attached. CLOSE ends it and leaves it ended. Both refuse
            # unless the recorded process confirms — never a search.
            from ping_hub import handoffs, reap
            title = payload.get("title", "")
            side = payload.get("side", "win")
            if not title:
                self._json({"ok": False, "detail": "title required"}, 400)
                return
            ok, detail = reap.reap(INBOX_ROOT, title)
            if not ok:
                self._json({"ok": False, "detail": detail}, 409)
                return
            if u.path == "/api/close-session":
                self._json({"ok": True, "detail": detail, "rebooted": False})
                return
            with engine.lock:
                t = dict(engine.threads.get(f"{side}:{title}") or {})
            match = handoffs.for_session(
                handoffs.listing(CFG.paths.base_bin), title, t.get("projects"))
            pieces = [f"You are '{title}', rebooted with a clean context from "
                      f"the hub. Register your codename immediately: base relay "
                      f"register --as {title}."]
            if match:
                pieces.append(f"HANDOFF: read the handoff {match['slug']} and "
                              f"resume that work exactly where it left off.")
            args = ["--dangerously-skip-permissions"]
            if CFG.spawn.disallowed_tools:
                args += ["--disallowedTools", ",".join(CFG.spawn.disallowed_tools)]
            try:
                spawn_tab(side, args, t.get("cwd") or None, title=title,
                          prompt="\n\n".join(pieces))
            except OSError as e:
                # the old session is already gone; say so rather than implying
                # nothing happened
                self._json({"ok": False, "rebooted": False,
                            "detail": f"ended, but the reboot failed: {e}"}, 500)
                return
            self._json({"ok": True, "rebooted": True, "detail": detail,
                        "handoff": match["slug"] if match else ""})
        elif u.path == "/api/resume":
            # Boot the SAME conversation back onto the SAME codename. `--resume`
            # mints a new session id (measured: 50 of 1184 transcripts carry a
            # prior one), so what binds the thread is BASE_RELAY_AS, which is
            # why `title` is passed and why the card's session id is expected
            # to change.
            from ping_hub import reap
            title = payload.get("title", "")
            side = payload.get("side", "win")
            if not title:
                self._json({"ok": False, "detail": "title required"}, 400)
                return
            with engine.lock:
                t = dict(engine.threads.get(f"{side}:{title}") or {})
            sid, cwd = t.get("session_id", ""), t.get("cwd", "")
            ok, why = reap.confirm(reap.find_record(INBOX_ROOT, title))
            detail = _resume_reason(t, ok, why)
            if not sid:
                self._json({"ok": False, "detail": detail}, 404)
                return
            if sid and cwd and not ok and not _still_reporting(t) \
                    and engine.transcript_path(sid, side, cwd) is None:
                # `--resume` with no transcript exits instantly and silently.
                # Refusing here turns that into an answer.
                self._json({"ok": False, "detail": (
                    f"no transcript on disk for session {sid[:8]} — there is "
                    f"nothing to resume. It was probably ended before Claude "
                    f"Code wrote one.")}, 409)
                return
            if not cwd or ok or _still_reporting(t):
                # refusing OUT LOUD: a resume that quietly did nothing on a
                # live session would read as the button being broken
                self._json({"ok": False, "detail": detail}, 409)
                return
            args = ["--dangerously-skip-permissions"]
            if CFG.spawn.disallowed_tools:
                args += ["--disallowedTools", ",".join(CFG.spawn.disallowed_tools)]
            args += ["--resume", sid]
            # NO prompt: `claude --resume <sid> "text"` injects a user turn, and
            # the thread has to come back where it left off, not with a message
            # the hub wrote pushed into it.
            try:
                spawn_tab(side, args, cwd, title=title)
            except OSError as e:
                self._json({"ok": False, "detail": f"resume failed to boot: {e}"}, 500)
                return
            # the attempt goes in the JOURNAL, so what happened is legible from
            # the thread itself afterwards even if the tab dies on boot
            engine.append(title, {"from": "hub", "dir": "in", "peer": True,
                                  "slug": f"resume-{sid[:8]}",
                                  "summary": f"resume requested — session {sid[:8]}, "
                                             f"{side}, {cwd}"}, side=side)
            # the thread hears the OUTCOME, not just the request
            threading.Thread(target=_resume_watch, args=(title, side, sid),
                             daemon=True).start()
            self._json({"ok": True, "session_id": sid,
                        "detail": f"resuming {title} ({sid[:8]}) in {cwd}"})
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


# How long a resumed session gets to come back before the thread is told it
# did not. Generous: a cold `claude --resume` on a large transcript is slow,
# and crying failure over a session that is merely still loading would be its
# own lie.
RESUME_WINDOW_SECS = 150.0
RESUME_POLL_SECS = 5.0


def _resume_watch(title: str, side: str, old_sid: str, sleep=None, clock=None) -> str:
    """Say in the THREAD whether the resume came back. Returns what it wrote.

    The gap this closes, found by its own G2 probe on 2026-08-19: the endpoint
    answered 200, wrote "resume requested", and then nothing ever said the
    session had not returned. Only the browser noticed, client-side, and only
    while that page stayed open — so from the phone, later, a failed resume was
    indistinguishable from one that worked. The fork's DoD says a resume that
    fails to boot must surface in the thread and never vanish; this is that.

    Coming back is NOT only a changed session id. `--resume` mints a new one
    here, but that is a fact about today's Claude Code rather than a contract,
    so any sign of the codename reporting in counts.
    """
    sleep = sleep or time.sleep
    clock = clock or time.time
    deadline = clock() + RESUME_WINDOW_SECS
    while clock() < deadline:
        sleep(RESUME_POLL_SECS)
        with engine.lock:
            t = dict(engine.threads.get(f"{side}:{title}") or {})
        sid = t.get("session_id") or ""
        if (sid and sid != old_sid) or t.get("active") or t.get("watching"):
            msg = (f"resumed — {title} is reporting in again"
                   + (f" as {sid[:8]}" if sid and sid != old_sid else ""))
            break
    else:
        msg = (f"resume did NOT come back within {int(RESUME_WINDOW_SECS)}s — "
               f"the tab opened and the session never registered. Its terminal "
               f"may show why.")
    engine.append(title, {"from": "hub", "dir": "in", "peer": True,
                          "slug": f"resume-result-{old_sid[:8]}-{int(clock())}",
                          "summary": msg}, side=side)
    return msg


def _still_reporting(t: dict) -> bool:
    """Is this session showing ANY sign of life besides its recorded pid?

    Load-bearing, and learned the hard way at 14:02 on 2026-08-19: a dead
    RECORD is not a dead SESSION. `zebra` and `toucan` were both working, both
    heartbeating, `toucan` mid-tool-run — and both had a `.pid` naming a
    process that genuinely no longer exists, because a session that gets
    rebooted or reclaims its codename leaves the old record behind. `confirm`
    answered correctly ("process 196849 is not running") and the first cut of
    the preview turned that into "resumable", which would have offered one-tap
    Resume on a live working session and put a SECOND session on its codename.

    That is the identical hazard the ▶ boot button had. Resume is offered only
    when NOTHING claims this session is alive.
    """
    return bool(t.get("active") or t.get("watching") or t.get("idle"))


def _resume_reason(t: dict, alive: bool, why: str) -> str:
    """Why resume is or is not on offer, in words that name the actual state.

    A control that is simply absent teaches nothing. The dead-end this fixes
    was exactly that: on a session that could not be confirmed, the clear modal
    offered Cancel and nothing else, with no account of why.
    """
    if not t.get("session_id"):
        return "no session id on this thread — nothing to resume"
    if not t.get("cwd"):
        return ("no recorded working directory — `--resume` from the wrong "
                "folder does not find the session")
    if alive:
        return f"it looks alive: {why} is running. Clear it first if it is wedged."
    if _still_reporting(t):
        return (f"it is still reporting in, so the RECORD is stale, not the "
                f"session ({why}). Resuming would put a second session on this "
                f"codename.")
    return why


def page_version(path=None) -> str:
    """A stamp that changes whenever the served page changes.

    `Cache-Control: no-store` on / is already set and is still not enough: an
    INSTALLED PWA keeps its own shell copy, so a shipped fix can sit on the
    server while the client happily runs last hour's javascript. Chris hit that
    twice on 2026-08-19 and the second time it read as a regression in work
    that was already correct. A shipped fix that cannot reach an open client is
    indistinguishable from one that does not work.
    """
    import hashlib
    p = Path(path) if path else HTML
    try:
        return hashlib.sha1(p.read_bytes()).hexdigest()[:12]
    except OSError:
        return "unknown"


class SingleInstanceError(RuntimeError):
    """The port already has a listener. Named so the refusal is catchable and
    testable rather than a bare exit."""


def port_listening(port: int, host: str = "127.0.0.1", connect=None) -> bool:
    """Is anything already serving this port?"""
    import socket as _s
    connect = connect or (lambda h, p: _s.create_connection((h, p), 0.6))
    try:
        connect(host, port).close()
        return True
    except (OSError, AttributeError):
        return False


def port_holder_pid(port: int, run=subprocess.run) -> str:
    """Best effort: WHICH process holds it, for the error message.

    Courtesy only — the refusal never depends on this succeeding. Naming the
    pid is the difference between an operator killing the right process and
    an operator guessing, which cost 45 minutes this morning.
    """
    cmd = (["netstat", "-ano"] if os.name == "nt"
           else ["ss", "-ltnp"])
    try:
        r = run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in (r.stdout or "").splitlines():
        if f":{port} " not in line and f":{port}	" not in line:
            continue
        if os.name == "nt":
            if "LISTENING" not in line:
                continue
            parts = line.split()
            if parts and parts[-1].isdigit():
                return parts[-1]
        else:
            m = re.search(r"pid=(\d+)", line)
            if m:
                return m.group(1)
    return ""


def guard_single_instance(port: int, listening=port_listening,
                          holder=port_holder_pid) -> None:
    """Refuse to start a second hub on a live port.

    Windows does NOT refuse this by itself. `HTTPServer.allow_reuse_address`
    is 1, which sets SO_REUSEADDR, and on Windows that does not mean "reuse a
    TIME_WAIT port" — it means "share a live listener". On 2026-08-19 three
    processes held 7799 simultaneously, split Chris's requests between them
    for 45 minutes, and not one of them raised a single error. Two of them
    were debug strays whose operator twice reported them dead because he read
    a log instead of listing processes.
    """
    if not listening(port):
        return
    pid = holder(port)
    raise SingleInstanceError(
        f"port {port} already has a listener"
        + (f" (pid {pid})" if pid else " (holder pid unknown)")
        + ". Refusing to start a second hub: on Windows both would bind and "
          "split requests silently. Stop that process first, or serve on a "
          "different [hub] port.")


class _Server(ThreadingHTTPServer):
    """Exclusive bind. See guard_single_instance for why sharing is the bug."""

    allow_reuse_address = False

    def server_bind(self):
        import socket as _s
        if os.name == "nt" and hasattr(_s, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(_s.SOL_SOCKET, _s.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main() -> None:
    # BEFORE any state is touched: a second hub must not register titles,
    # backfill, or start watchers against a store another hub already owns
    guard_single_instance(PORT)
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
    start_voice_warm()   # background; the bind below does not wait on it
    srv = _Server((CFG.hub.bind, PORT), Handler)
    print(f"ping-chat-hub on http://127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
