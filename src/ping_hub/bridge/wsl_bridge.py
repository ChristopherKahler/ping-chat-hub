#!/usr/bin/env python3
"""ping-chat-hub WSL bridge — the hub's window onto the WSL relay store.

Ships INSIDE the hub package (`ping_hub/bridge/wsl_bridge.py`) and is COPIED
into WSL by `ping-hub install --deploy-bridge`, which also writes
~/.config/hub-bridge.toml beside it. The hub never reads the WSL base store
itself; this daemon is the only thing that does, and it SERVES on
127.0.0.1:7798 — WSL2 localhostForwarding makes that reachable from Windows,
while the reverse direction would need the per-boot NAT gateway IP. The hub
long-polls /events, so delivery is push-shaped over a pull transport.

  GET  /snapshot          {sessions: {...}, watching: {title: bool}}
  GET  /events?cursor=N   events after N; holds up to 25s when empty
  POST /send              {title, msg} -> base relay ping --from chris (WSL binary)

Registers the standing `chris` title WSL-side and keeps BOTH halves fresh — the
registration on a cadence and the sentinel every tick. The hub is chris's
monitor on both sides.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

def _conf() -> dict:
    """The bridge lives INSIDE WSL, where the hub's own hub.toml is not
    visible, so the deploy step drops these three keys next to it. Absent file
    = the defaults below, which is how it ran before the key existed."""
    try:
        import tomllib
        with open(Path.home() / ".config" / "hub-bridge.toml", "rb") as fh:
            return tomllib.load(fh).get("bridge") or {}
    except (OSError, ValueError):
        return {}


_C = _conf()

BASE = Path(_C.get("base_gbl") or (Path.home() / ".base-gbl")) / ".base"
# absolute path: the daemon's inherited PATH may contain broken /mnt/c drvfs
# entries that raise EIO during exec's PATH walk (seen live 2026-08-17)
BASE_BIN = str(_C.get("base_bin") or (Path.home() / ".local" / "bin" / "base"))
INBOX = BASE / "relay-inbox"
SESSIONS = BASE / "sessions.json"
PORT = int(_C.get("port") or 7798)   # must match the hub's [wsl].bridge_port
WATCH_STALE_SECS = 15
TICK_SECS = 5
KEEPALIVE_TICKS = 12        # x TICK_SECS = re-register once a minute
HUB_TITLE = str(_C.get("standing_title") or "chris")

# Process identity. `seq` restarts at zero in every fresh bridge, so a hub
# that carries its cursor across the reconnect filters every new event as
# already-seen — silently, forever. The epoch lets the hub notice it is
# talking to a NEW bridge and start over. Measured 2026-08-19: 24 minutes of
# frozen journaling that looked perfectly healthy from outside.
EPOCH = time.time()

events: list[dict] = []          # ring of {seq, kind, ...}
seq_lock = threading.Lock()
cond = threading.Condition()
_seq = 0
_snapshot: dict = {"sessions": {}, "watching": {}}
_inbox_seen: dict[str, set] = {}


def emit(kind: str, **payload) -> None:
    global _seq
    with cond:
        _seq += 1
        events.append({"seq": _seq, "kind": kind, **payload})
        del events[:-500]
        cond.notify_all()


def read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


CLAUDE_PROJ = Path.home() / ".claude" / "projects"
_meta_cache: dict[str, tuple] = {}
_idx_cache: dict[str, tuple] = {}
_path_cache: dict[str, Path] = {}


def find_transcript(sid: str):
    """Board cwd tracks the last register; the transcript lives under the
    BOOT cwd's project dir — find it anywhere (mirrors the hub's win logic)."""
    hit = _path_cache.get(sid)
    if hit is not None and hit.exists():
        return hit
    try:
        for d in CLAUDE_PROJ.iterdir():
            p = d / f"{sid}.jsonl"
            if p.exists():
                _path_cache[sid] = p
                return p
    except OSError:
        pass
    return None


def doing_of(d: dict) -> str:
    """One live verb from a transcript entry (hub working-dots snippet)."""
    t = d.get("type")
    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
    content = msg.get("content")
    if t == "assistant" and isinstance(content, list) and content:
        b = content[-1] if isinstance(content[-1], dict) else {}
        bt = b.get("type")
        if bt == "tool_use":
            n = b.get("name", "tool")
            return "running " + (n.split("__")[-1] if n.startswith("mcp__") else n)
        if bt == "thinking":
            return "thinking"
        if bt == "text":
            return "writing"
    if t == "user":
        if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return "thinking"
        return "reading the prompt"
    return ""


def session_meta(sid: str, cwd: str) -> dict:
    """label (Claude Code's session summary = the terminal tab title) +
    model/effort from the transcript tail. Mirrors the hub's win-side logic."""
    proj = CLAUDE_PROJ / cwd.replace("/", "-")
    out = {"label": "", "model": "", "effort": "", "ctx": 0, "doing": "",
           "active": False}
    idx = proj / "sessions-index.json"
    try:
        imt = idx.stat().st_mtime
        cached = _idx_cache.get(str(idx))
        if not cached or cached[0] != imt:
            m = {}
            for e in (read_json(idx) or {}).get("entries", []):
                m[e.get("sessionId", "")] = e.get("summary") or e.get("firstPrompt", "")
            cached = (imt, m)
            _idx_cache[str(idx)] = cached
        out["label"] = cached[1].get(sid, "")
    except OSError:
        pass
    if not out["label"]:
        out["label"] = history_label(sid)
    jp = proj / f"{sid}.jsonl"
    if not jp.exists():
        jp = find_transcript(sid)
        if jp is None:
            return out
    try:
        jmt = jp.stat().st_mtime
    except OSError:
        return out
    # active = the transcript moved in the last 90s (WSL has no hook-events
    # feed the hub can read; transcript freshness is the honest signal)
    out["active"] = (time.time() - jmt) < 90
    cached = _meta_cache.get(sid)
    if cached and cached[0] == jmt:
        out.update(cached[1])
        return out
    model = effort = doing = ""
    ctx = 0
    try:
        with open(jp, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 65536))
            tail = fh.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = d.get("message")
            if not doing:
                doing = doing_of(d)
            if not model and isinstance(msg, dict) and msg.get("model"):
                model = msg["model"]
            if not effort and d.get("effort"):
                effort = str(d["effort"])
            if not ctx and isinstance(msg, dict):
                u = msg.get("usage")
                if isinstance(u, dict) and (u.get("input_tokens") or u.get("cache_read_input_tokens")):
                    ctx = (u.get("input_tokens", 0)
                           + u.get("cache_read_input_tokens", 0)
                           + u.get("cache_creation_input_tokens", 0))
            if model and effort and ctx:
                break
    except OSError:
        pass
    meta = {"model": model.replace("claude-", "").split("-")[0] if model else "",
            "effort": effort, "ctx": ctx, "doing": doing}
    _meta_cache[sid] = (jmt, meta)
    out.update(meta)
    return out


def history_label(sid: str) -> str:
    hist = Path.home() / ".claude" / "history.jsonl"
    try:
        hmt = hist.stat().st_mtime
    except OSError:
        return ""
    cached = _idx_cache.get("history")
    if not cached or cached[0] != hmt:
        m: dict[str, str] = {}
        try:
            with open(hist, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    s = d.get("sessionId", "")
                    if s and s not in m:
                        m[s] = (d.get("display") or "")[:80]
        except OSError:
            return ""
        cached = (hmt, m)
        _idx_cache["history"] = cached
    return cached[1].get(sid, "")


def watcher() -> None:
    last_roster = ""
    while True:
        reg = read_json(SESSIONS) or {}
        def status_of(t):
            try:
                return (INBOX / t / ".status").read_text(encoding="utf-8").strip()[:120]
            except OSError:
                return ""
        sessions = {t: {**e, "status": status_of(t),
                        **session_meta(e.get("session_id", ""), e.get("cwd", ""))}
                    for t, e in (reg.get("sessions") or {}).items()}
        watching = {}
        for t in sessions:
            try:
                watching[t] = (time.time() - (INBOX / t / ".watching").stat().st_mtime) < WATCH_STALE_SECS
            except OSError:
                watching[t] = False
        snap = {"sessions": sessions, "watching": watching}
        blob = json.dumps(snap, sort_keys=True)
        _snapshot.update(snap)
        if blob != last_roster:
            last_roster = blob
            emit("roster", **snap)
        try:
            dirs = [d for d in INBOX.iterdir() if d.is_dir()]
        except OSError:
            dirs = []
        for d in dirs:
            try:
                cur = {p.name for p in d.glob("*.json")}
            except OSError:
                continue
            prev = _inbox_seen.get(d.name, set())
            _inbox_seen[d.name] = cur
            for fname in cur - prev:
                ping = read_json(d / fname) or {"slug": fname[:-5], "summary": "(consumed before read)", "from": "?"}
                emit("ping", inbox=d.name, ping=ping)
        time.sleep(1)


def register_chris(run=subprocess.run) -> bool:
    """Refresh the standing title. Returns success, and never raises.

    The registry's live/DEAD state reads the REGISTRATION timestamp, not the
    sentinel. Registering only at boot therefore let `chris` decay to DEAD
    about half an hour after every bridge start, while the bridge was up and
    serving — and every `base relay ping --to chris` began warning that he may
    be dead. Measured 2026-08-19: his last_heartbeat matched the unit's start
    timestamp to the second, half an hour later.

    It never raises because it shares a daemon thread with the sentinel touch.
    The original call had no handler at all, so a slow or missing `base` binary
    at boot killed BOTH halves at once, with nothing printed.
    """
    try:
        r = run([BASE_BIN, "relay", "register", "--as", HUB_TITLE,
                 "--session", "hub-chris-standing-wsl"],
                capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return getattr(r, "returncode", 1) == 0


def chris_keeper(run=subprocess.run, sleep=time.sleep, stop=None) -> None:
    """Keep the standing title alive: re-register on a cadence, touch the
    sentinel every tick.

    Ticks are COUNTED rather than clocks read, so the cadence is provable
    without injecting a clock or sleeping for real.
    """
    d = INBOX / HUB_TITLE
    tick = 0
    healthy = True
    while not (stop and stop(tick)):
        if tick % KEEPALIVE_TICKS == 0:
            fresh = register_chris(run)
            # One line each way and nothing in between: silence IS the healthy
            # state. A keeper that has been failing for an hour leaving no
            # trace is the exact shape of the outage this was written for.
            if fresh != healthy:
                print(f"chris keeper: registration "
                      f"{'recovered' if fresh else 'FAILING'}", flush=True)
                healthy = fresh
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / ".watching").touch()
        except OSError:
            pass
        tick += 1
        sleep(TICK_SECS)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
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
        if u.path == "/snapshot":
            self._json({**_snapshot, "epoch": EPOCH})
        elif u.path == "/events":
            cursor = int((parse_qs(u.query).get("cursor") or ["0"])[0])
            deadline = time.time() + 25
            with cond:
                while True:
                    fresh = [e for e in events if e["seq"] > cursor]
                    if fresh or time.time() >= deadline:
                        break
                    cond.wait(timeout=max(0.1, deadline - time.time()))
            self._json({"events": fresh, "epoch": EPOCH,
                        "cursor": fresh[-1]["seq"] if fresh else cursor})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "bad json"}, 400)
            return
        if u.path == "/send":
            title, msg = payload.get("title", ""), payload.get("msg", "")
            if not title or not msg:
                self._json({"error": "title and msg required"}, 400)
                return
            sender = payload.get("sender") or HUB_TITLE
            try:
                r = subprocess.run(
                    [BASE_BIN, "relay", "ping", "--to", title, "--from", sender, "--msg", msg],
                    capture_output=True, text=True, timeout=15,
                )
                self._json({"ok": r.returncode == 0, "detail": (r.stdout + r.stderr).strip()})
            except (OSError, subprocess.TimeoutExpired) as e:
                self._json({"ok": False, "detail": str(e)}, 502)
        else:
            self._json({"error": "not found"}, 404)


def main() -> None:
    threading.Thread(target=watcher, daemon=True).start()
    threading.Thread(target=chris_keeper, daemon=True).start()
    # 0.0.0.0, not 127.0.0.1: WSL2's localhost relay only proxies wildcard-
    # bound ports to Windows. The NAT subnet keeps this machine-private.
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"hub wsl-bridge on 0.0.0.0:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
