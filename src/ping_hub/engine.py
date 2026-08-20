"""ping-chat-hub engine — thread roster + inbox watcher + append-only journals.

The hub is a WINDOW over the relay, never a merger of its stores (H1 §1):
  - roster: who has a thread = global sessions.json (Windows side) plus, later,
    registrations pushed by the WSL bridge. Squad membership tags the thread
    for the Squads tab.
  - messages: relay pings. Inbox files are CONSUMED by base's own delivery,
    so the journal (append-only jsonl per thread) is the only durable history.
    Journal on file-create; tolerate vanish-before-read (falcon, H1 review).
  - watching: the .watching sentinel (wake contract, 15s threshold) drives the
    gray-out state, same constant as `base relay board`.

No third-party deps — stdlib polling, same posture as the claude-chat daemon
this app was forked from.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ping_hub import config, proc

CFG = config.get()

WIN_BASE = CFG.paths.base_store
CX_TOML = CFG.cx_ptt.cx_toml
CX_SLOT = CFG.cx_ptt.cx_slot
CLAUDE_PROJ = CFG.paths.claude_home / "projects"


def wsl_claude_proj() -> Path | None:
    """WSL transcripts as Windows sees them. A FUNCTION, not a constant: it
    derives from the WSL distro and home, which cost a `wsl.exe` call to
    resolve, and an import must never block on that. None = no WSL side."""
    unc = CFG.wsl.home_unc
    return Path(unc) / ".claude" / "projects" if unc else None


def _munge(cwd: str) -> str:
    """Claude Code's project-dir name for a cwd: C:\\code\\api -> C--code-api."""
    return cwd.replace(":", "-").replace("\\", "-").replace("/", "-")
INBOX_ROOT = WIN_BASE / "relay-inbox"
SESSIONS = WIN_BASE / "sessions.json"
SQUADS_DIR = WIN_BASE / "squads"
HUB_DIR = WIN_BASE / "hub"

WATCH_STALE_SECS = 15          # mirror of base's wake contract constant
BRIDGE_PORT = CFG.wsl.bridge_port  # WSL bridge, reachable via localhostForwarding
ROSTER_POLL_SECS = 2.0
INBOX_POLL_SECS = 1.0
# cx-ptt publishes its heartbeat every ~30s and the stale budget is 90s, so
# polling faster than this buys nothing and polling slower loses a whole
# missed-refresh window.
CXPTT_POLL_SECS = 20.0
# how often a card that reads dead may re-ask the recorded pid whether it is
# actually alive. `reap.confirm` is a CIM query on Windows; the roster polls
# every two seconds and must never pay that per card per poll.
ALIVE_RECHECK_SECS = 30.0
HUB_TITLE = CFG.hub.standing_title  # the universal pipe — the hub IS this session


def _read_json(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


# The window in which an echo and the watcher's copy of the SAME send are
# plausibly the same delivery. Ten seconds: the watcher polls once a second and
# the send itself is measured in seconds, so this is the real race width with
# margin. Wider would start swallowing a message Chris deliberately repeated.
ECHO_WINDOW = 10.0

# Backfill is not a race, it is a bounded replay minutes later, so the echo
# window cannot cover it — but the echo has no slug, so backfill cannot dedup
# against it by id either. The entry carries its own dkey instead, and a match
# must ALSO land inside this window: both stamps are send-times, so 60s is
# generous. Without the bound, Chris sending the same short message twice and
# having the second recovered by backfill would DROP it on a dkey hit from the
# first — and a drop is the class we are killing. A miss appends and we eat the
# duplicate, which is the standing rule.
ECHO_BACKFILL_WINDOW = 60.0


def ts_secs(value: str) -> float:
    """Epoch seconds from a journal or ping timestamp. Unparseable returns 0.0,
    which makes every comparison miss — i.e. it appends, never drops."""
    from datetime import datetime
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value)[:24], fmt).timestamp()
        except (ValueError, TypeError):
            continue
    return 0.0


def echo_key(side: str, title: str, sender: str, summary: str) -> str:
    """Weak identity for one outbound send, used at exactly ONE seam.

    base mints the slug inside the ping file, so a send that has just shelled
    out cannot know the id the watcher will later read — a strong dedup is
    impossible across those two paths. This content hash covers only that seam;
    everywhere the real id exists (watcher, replay, backfill) the slug is used
    instead. Ruling 2026-08-19: a visible duplicate always beats a possible
    drop, so a hash MISS appends and we live with the double.
    """
    import hashlib
    raw = f"{side}|{title}|{sender}|{summary}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def epoch_reset(known, seen, cursor: int) -> tuple:
    """Decide the cursor when a bridge response arrives. Pure, so the rule is
    provable without a socket.

    Returns (cursor, epoch). A bridge is a fresh PROCESS after any restart and
    its `seq` starts at zero, so a cursor carried across the reconnect filters
    every new event as already-seen — permanently, and with no error anywhere.
    Measured 2026-08-19: 24 minutes of frozen journaling while /snapshot served
    happily, because roster and events fail independently and only one of them
    is observable from outside.

    An epoch-less response is an older deployed bridge: keep today's behaviour
    rather than resetting on every poll, so a hub newer than its bridge is not
    a new outage.
    """
    if seen is None:
        return cursor, known
    if known is None:
        # FIRST epoch we have ever seen. A cursor we are already carrying was
        # built against a bridge that never identified itself, so its
        # provenance is unknowable and it cannot be trusted against this
        # process — reset rather than assume. This is not hypothetical: it is
        # exactly the hub-upgraded-before-its-bridge window, and it froze the
        # live hub for three minutes on 2026-08-19 until the live proof caught
        # it. Unknown provenance resets; the replay is bounded and deduped.
        return (0 if cursor else cursor), seen
    if seen != known:
        return 0, seen
    return cursor, seen


class _HealBudget:
    """How often the hub may try to restart a bridge it has found down.

    Pure decision logic, kept apart from the shelling out so the budget is
    provable without a machine. A restart loop that fires every five seconds is
    worse than no restart loop at all: it buries the real failure under a wall
    of identical log lines and keeps a sick WSL busy. The first attempt is free
    — a bridge that has just died may only need a nudge — and the interval then
    widens and holds, so a bridge that is never coming back costs one line
    every five minutes.
    """

    STEPS = (30, 60, 120, 300)

    def __init__(self) -> None:
        self.n = 0
        self.last = 0.0

    def wait(self) -> int:
        """Seconds that must pass before the next attempt is allowed."""
        if self.n == 0:
            return 0
        return self.STEPS[min(self.n - 1, len(self.STEPS) - 1)]

    def due(self, now: float) -> bool:
        return (now - self.last) >= self.wait()

    def mark(self, now: float) -> None:
        self.last = now
        self.n += 1

    def reset(self) -> None:
        self.n = 0
        self.last = 0.0


class Engine:
    def __init__(self, side: str = "win") -> None:
        self.side = side
        self.threads: dict[str, dict] = {}      # title -> roster entry
        self.lock = threading.Lock()
        self.listeners: list[Callable[[dict], None]] = []   # SSE fan-out
        self._seen: dict[str, set[str]] = {}    # title -> slugs already journaled
        self._inbox_snapshot: dict[str, set[str]] = {}
        self._last: dict[str, dict] = {}         # "side:title" -> latest ping preview
        self._meta_cache: dict[str, tuple] = {}  # sid -> (jsonl mtime, {model, effort})
        self._alive_cache: dict[str, tuple] = {}  # title -> (checked_at, ok)
        self._path_cache: dict[str, Path] = {}   # sid -> transcript found off-cwd
        self._rel_cache: list = [0.0, {}]        # relations.json mtime cache
        self._idx_cache: dict[str, tuple] = {}   # index path -> (mtime, {sid: summary})
        self._stop = threading.Event()
        # bridge liveness as a TOP-LEVEL fact, not only the per-thread flag: a
        # hub that starts while the bridge is already down has no wsl threads
        # to stamp, and an empty list must never read as "no sessions over
        # there". Absent is not empty.
        # `probed` is the difference between "down" and "not asked yet", and
        # they are not the same fact. Without it the hub announces an outage
        # for the first few seconds of every boot, which is the exact species
        # of lie this fork exists to remove.
        # cx-ptt gets the same treatment for the same reason: it died at
        # 2026-08-18 20:36 and nothing noticed for ~16 hours while every
        # hub-owned service came back by itself. `probed` again separates
        # "down" from "not asked yet".
        self.cxptt_state: dict = {"alive": False, "probed": False,
                                  "enabled": bool(CFG.cx_ptt.enabled),
                                  "since": "", "detail": "not probed yet"}
        self._cx_heal = _HealBudget()
        self.bridge_state: dict = {"up": False, "probed": False, "since": "",
                                   "detail": "not probed yet",
                                   "enabled": bool(CFG.wsl.enabled)}
        self._heal = _HealBudget()
        # echo_key -> ts, the fast in-memory path for the echo-vs-watcher race
        self._echoes: dict[str, float] = {}
        # the DURABLE form of the same fact, seeded from the journals at boot.
        # In-memory alone is not enough: a hub restart between the send and the
        # watcher's delivery loses the echo and the watcher writes a second
        # copy — measured live 2026-08-19, echo at 11:09:27, duplicate at
        # 11:09:33 across a restart. The dkey on the entry exists to survive
        # exactly that, so it has to be consulted here too, not only in
        # backfill.
        self._dkeys: dict = {}
        # "side:title" -> every project that EVER touched the session (Chris
        # spec 2026-08-17: append on change, never remove — filter keywords)
        self._projects: dict[str, list] = _read_json(HUB_DIR / "projects.json") or {}
        HUB_DIR.joinpath("threads", self.side).mkdir(parents=True, exist_ok=True)
        self._load_seen()
        self._dkeys = self._echo_index()

    def _project_note(self, key: str, project: str) -> None:
        if not project or project in self._projects.get(key, []):
            return
        self._projects.setdefault(key, []).append(project)
        try:
            with open(HUB_DIR / "projects.json", "w", encoding="utf-8") as fh:
                json.dump(self._projects, fh, indent=1)
        except OSError:
            pass

    # ── journal (side-aware: threads/win/*.jsonl, threads/wsl/*.jsonl) ───
    def journal_path(self, title: str, side: str | None = None) -> Path:
        return HUB_DIR / "threads" / (side or self.side) / f"{title}.jsonl"

    def _load_seen(self) -> None:
        for side in ("win", "wsl"):
            HUB_DIR.joinpath("threads", side).mkdir(parents=True, exist_ok=True)
            for jp in (HUB_DIR / "threads" / side).glob("*.jsonl"):
                slugs = set()
                last = None
                try:
                    with open(jp, encoding="utf-8") as fh:
                        for line in fh:
                            try:
                                d = json.loads(line)
                                slugs.add(d["slug"])
                                last = d
                            except (json.JSONDecodeError, KeyError):
                                continue
                except OSError:
                    continue
                self._seen[f"{side}:{jp.stem}"] = slugs
                if last:
                    self._last[f"{side}:{jp.stem}"] = {
                        "from": last.get("from", ""),
                        "text": (last.get("summary") or "")[:140],
                    }

    def append(self, title: str, entry: dict, side: str | None = None) -> None:
        side = side or self.side
        entry.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        with self.lock:
            self._last[f"{side}:{title}"] = {
                "from": entry.get("from", ""),
                "text": (entry.get("summary") or "")[:140],
            }
            self._seen.setdefault(f"{side}:{title}", set()).add(entry.get("slug", ""))
            # EVERY outbound write registers its content key, whichever path
            # wrote it. The race runs both ways: measured 2026-08-19, the echo
            # won it once (11:09:27) and the watcher won it two minutes later
            # (11:13:13), so a guard on only one side just moves the duplicate.
            if entry.get("dir") == "out":
                self._dkeys.setdefault(f"{side}:{title}", {})[
                    echo_key(side, title, entry.get("from", ""),
                             entry.get("summary", ""))] = ts_secs(
                    entry.get("created") or entry.get("ts") or "") or time.time()
            with open(self.journal_path(title, side), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        self._emit({"event": "message", "side": side, "title": title, **entry})

    def tail(self, title: str, n: int = 200, side: str | None = None) -> list[dict]:
        try:
            with open(self.journal_path(title, side), encoding="utf-8") as fh:
                lines = fh.readlines()[-n:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # ── events ───────────────────────────────────────────────────────────
    def _emit(self, event: dict) -> None:
        for cb in list(self.listeners):
            try:
                cb(event)
            except Exception:
                self.listeners.remove(cb)

    # ── roster ───────────────────────────────────────────────────────────
    def _squad_titles(self) -> dict[str, str]:
        """title -> squad name, from each squad's squad.json members list.
        Leaders stay Terminals — falcon appearing in worldsys' relay registry
        misclassified him; role=='spawn' is the membership test (falcon's
        correction, H3 review)."""
        out: dict[str, str] = {}
        try:
            squads = [d for d in SQUADS_DIR.iterdir() if d.is_dir()]
        except OSError:
            return out
        for sq in squads:
            doc = _read_json(sq / "squad.json") or {}
            if doc.get("status") not in (None, "active"):
                continue
            for m in doc.get("members") or []:
                if m.get("role") != "leader" and m.get("codename"):
                    out[m["codename"]] = sq.name
        return out

    @staticmethod
    def has_evidence(t: dict) -> bool:
        """Is there a real Claude Code session behind this card?

        A live session leaves traces the roster already collects: a model name
        and a context size read from its transcript tail, a label, a live verb.
        A relay mirror has a registration and nothing else, because there is no
        transcript on that side to read.
        """
        return bool(t.get("model") or t.get("ctx") or t.get("doing")
                    or t.get("label"))

    def mark_ghosts(self) -> None:
        """Flag cross-side relay mirrors.

        One session id registered on BOTH sides means one of the two cards is
        plumbing: the session registered a title over there so pings can route,
        but it does not run there. The ghost is the side with no evidence
        behind it, and it should read as infrastructure rather than as a
        session that has gone quiet — nothing about it deserves to worry
        anyone.

        Ambiguity is resolved by NOT flagging. If both sides have evidence they
        are two real sessions sharing an id, and if neither does there is
        nothing to distinguish them; guessing would gray out a real card.
        """
        by_sid: dict[str, list[str]] = {}
        for key, t in self.threads.items():
            t["ghost"] = False      # EVERY card carries it: a missing field
            sid = t.get("session_id") or ""   # reads as undefined in the UI
            if sid:
                by_sid.setdefault(sid, []).append(key)
        for sid, keys in by_sid.items():
            if len(keys) < 2:
                continue
            sides = {self.threads[k].get("side") for k in keys}
            if len(sides) < 2:
                continue            # duplicates on ONE side are not mirrors
            real = [k for k in keys if self.has_evidence(self.threads[k])]
            if len(real) != 1:
                continue            # ambiguous: leave every card alone
            for key in keys:
                if key not in real:
                    self.threads[key]["ghost"] = True

    def _relations(self) -> dict:
        """{child "side:title" -> parent "side:title"} from hub/relations.json
        — Chris's parent/child (orchestrator/builder) designations."""
        try:
            mt = (HUB_DIR / "relations.json").stat().st_mtime
        except OSError:
            return {}
        if self._rel_cache[0] != mt:
            self._rel_cache = [mt, _read_json(HUB_DIR / "relations.json") or {}]
        return self._rel_cache[1]

    @staticmethod
    def transcript_path(sid: str, side: str, cwd: str) -> Path | None:
        """Where this session's transcript actually is, or None.

        Load-bearing for resume: `claude --resume <sid>` with no transcript to
        resume exits immediately and takes its own error message off the screen
        with it. Measured 2026-08-19 on the scratch session `resume-probe` —
        killed 25s in, before Claude Code had written the file, so the resume
        booted, found nothing, and died silently. Better to refuse up front.

        The transcript lives under the BOOT cwd's project dir, so the direct
        path is tried first and the whole root searched second — a session that
        registered from a subfolder is still findable.
        """
        if not sid:
            return None
        root = wsl_claude_proj() if side == "wsl" else CLAUDE_PROJ
        if root is None:
            return None
        jp = root / _munge(cwd or "") / f"{sid}.jsonl"
        if jp.exists():
            return jp
        try:
            for d in root.iterdir():
                c = d / f"{sid}.jsonl"
                if c.exists():
                    return c
        except OSError:
            pass
        return None

    def transcript_responses(self, title: str, side: str, n: int = 10) -> list[dict]:
        """Last n assistant TEXT replies from the session's transcript — the
        subtle terminal mirror behind the thread accordion (Chris 2026-08-17).
        WSL transcripts read over \\\\wsl.localhost."""
        with self.lock:
            t = dict(self.threads.get(f"{side}:{title}") or {})
        sid, cwd = t.get("session_id", ""), t.get("cwd", "")
        if not sid:
            return []
        root = wsl_claude_proj() if side == "wsl" else CLAUDE_PROJ
        if root is None:
            return []          # no WSL side on this machine: absent, not empty
        jp = root / _munge(cwd) / f"{sid}.jsonl"
        if not jp.exists():
            jp = None
            try:
                for d in root.iterdir():
                    c = d / f"{sid}.jsonl"
                    if c.exists():
                        jp = c
                        break
            except OSError:
                pass
        if jp is None:
            return []
        try:
            with open(jp, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 512 * 1024))
                tail = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return []
        out = []
        for line in tail.splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "assistant":
                continue
            content = (d.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            text = "\n".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text").strip()
            if text:
                out.append({"ts": d.get("timestamp", ""), "text": text[:4000]})
        return out[-n:]

    @staticmethod
    def _doing_of(d: dict) -> str:
        """One live verb from a transcript entry — what the session is doing
        RIGHT NOW (shown beside the working dots, Chris ask 2026-08-17)."""
        t = d.get("type")
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        content = msg.get("content")
        if t == "assistant" and isinstance(content, list) and content:
            b = content[-1] if isinstance(content[-1], dict) else {}
            bt = b.get("type")
            if bt == "tool_use":
                n = b.get("name", "tool")
                if n.startswith("mcp__"):
                    n = n.split("__")[-1]
                return f"running {n}"
            if bt == "thinking":
                return "thinking"
            if bt == "text":
                return "writing"
        if t == "user":
            if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content):
                return "thinking"  # tool output absorbed, model computing
            return "reading the prompt"
        return ""

    def _find_transcript(self, sid: str) -> Path | None:
        hit = self._path_cache.get(sid)
        if hit is not None and hit.exists():
            return hit
        try:
            for d in CLAUDE_PROJ.iterdir():
                p = d / f"{sid}.jsonl"
                if p.exists():
                    self._path_cache[sid] = p
                    return p
        except OSError:
            pass
        return None

    # ── session meta: tab-title summary + model/effort (Chris ask 2026-08-17) ──
    def _session_meta(self, sid: str, cwd: str) -> dict:
        """{label, model, effort} for a session, from Claude Code's own
        artifacts: summary from the project's sessions-index.json, model +
        effort from the transcript tail. Both mtime-cached — 19 threads on a
        2s poll must not reread transcripts."""
        proj = CLAUDE_PROJ / _munge(cwd)
        out = {"label": "", "model": "", "effort": "", "ctx": 0}
        # summary from the per-project index
        idx_path = proj / "sessions-index.json"
        try:
            imt = idx_path.stat().st_mtime
        except OSError:
            imt = None
        if imt is not None:
            cached = self._idx_cache.get(str(idx_path))
            if not cached or cached[0] != imt:
                m = {}
                for e in (_read_json(idx_path) or {}).get("entries", []):
                    m[e.get("sessionId", "")] = e.get("summary") or e.get("firstPrompt", "")
                cached = (imt, m)
                self._idx_cache[str(idx_path)] = cached
            out["label"] = cached[1].get(sid, "")
        if not out["label"]:
            # the index goes stale for months; history.jsonl is live — the
            # session's FIRST prompt is what Windows Terminal shows as the tab
            # name, which is exactly the line Chris asked to see stacked here
            out["label"] = self._history_label(sid)
        # model/effort from the transcript tail (last 64KB)
        jp = proj / f"{sid}.jsonl"
        if not jp.exists():
            # board cwd tracks wherever the session last REGISTERED from; the
            # transcript lives under the BOOT cwd's project dir. Find it
            # anywhere so a subfolder register never blanks model/effort/ctx
            # (falcon in tools/claude-coop, 2026-08-17).
            jp = self._find_transcript(sid)
            if jp is None:
                return out
        try:
            jmt = jp.stat().st_mtime
        except OSError:
            return out
        # the verb travels with the age of the thing it was read from. Without
        # it the UI can only render "thinking" as though it were happening now,
        # which is the whole silent-lie class: `hawk`, dead 21h, was still
        # reporting "reading the prompt".
        out["doing_at"] = jmt
        cached = self._meta_cache.get(sid)
        if cached and cached[0] == jmt:
            out.update(cached[1])
            return out
        model = effort = doing = ""
        ctx = 0
        try:
            with open(jp, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 65536))
                tail = fh.read().decode("utf-8", errors="replace")
            for line in reversed(tail.splitlines()):
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = d.get("message")
                if not doing:
                    doing = self._doing_of(d)
                if not model and isinstance(msg, dict) and msg.get("model"):
                    model = msg["model"]
                if not effort and d.get("effort"):
                    effort = str(d["effort"])
                if not ctx and isinstance(msg, dict):
                    u = msg.get("usage")
                    if isinstance(u, dict) and (u.get("input_tokens") or u.get("cache_read_input_tokens")):
                        # context in the window right now = everything the last
                        # turn actually carried in
                        ctx = (u.get("input_tokens", 0)
                               + u.get("cache_read_input_tokens", 0)
                               + u.get("cache_creation_input_tokens", 0))
                if model and effort and ctx:
                    break
        except OSError:
            pass
        # "claude-fable-5" -> "fable"
        short = model.replace("claude-", "").split("-")[0] if model else ""
        meta = {"model": short, "effort": effort, "ctx": ctx, "doing": doing}
        self._meta_cache[sid] = (jmt, meta)
        out.update(meta)
        return out

    def _titles(self) -> dict[str, str]:
        """Chris's manual card titles (hub/titles.json) — they beat every
        fetched label; keyed by session_id first, side:title as fallback."""
        p = HUB_DIR / "titles.json"
        try:
            mt = p.stat().st_mtime
        except OSError:
            return {}
        cached = self._idx_cache.get("titles")
        if not cached or cached[0] != mt:
            cached = (mt, _read_json(p) or {})
            self._idx_cache["titles"] = cached
        return cached[1]

    def set_title(self, side: str, title: str, sid: str, text: str) -> None:
        p = HUB_DIR / "titles.json"
        with self.lock:
            data = _read_json(p) or {}
            for key in ([sid] if sid else []) + [f"{side}:{title}"]:
                if text:
                    data[key] = text
                else:
                    data.pop(key, None)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
        self._emit({"event": "roster", "side": side})

    def _history_label(self, sid: str) -> str:
        hist = CLAUDE_PROJ.parent / "history.jsonl"
        try:
            hmt = hist.stat().st_mtime
        except OSError:
            return ""
        cached = self._idx_cache.get("history")
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
                        disp = (d.get("display") or "").strip()
                        # paste placeholders make useless titles — take the
                        # first prompt the human actually typed (Chris, live)
                        if (s and disp and s not in m
                                and not disp.startswith("[Pasted text")
                                and not disp.startswith("[Image")
                                and not disp.startswith("⚠")):
                            m[s] = disp[:80]
            except OSError:
                return ""
            cached = (hmt, m)
            self._idx_cache["history"] = cached
        return cached[1].get(sid, "")

    def _activity_map(self) -> dict[str, float]:
        """session_id -> last hook-event timestamp, from the workspace
        hook-events.jsonl tail. A session mid-turn emits a line per tool call;
        silence = idle. Cached by file size."""
        log = CFG.paths.hook_events
        try:
            size = log.stat().st_size
        except OSError:
            return {}
        cached = self._idx_cache.get("hookev")
        if cached and cached[0] == size:
            return cached[1]
        m: dict[str, float] = {}
        try:
            with open(log, "rb") as fh:
                fh.seek(max(0, size - 131072))
                for line in fh.read().decode("utf-8", errors="replace").splitlines():
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid = d.get("session_id") or ""
                    ts = d.get("ts") or ""
                    if sid and ts:
                        try:
                            import datetime
                            m[sid] = datetime.datetime.fromisoformat(ts).timestamp()
                        except ValueError:
                            continue
        except OSError:
            return {}
        self._idx_cache["hookev"] = (size, m)
        return m

    def _confirmed_alive(self, title: str, now: float) -> bool | None:
        """Is this session's recorded process still that process? None = no
        record, so the question cannot be answered.

        This is the strong proof, and until 2026-08-19 the card did not ask.
        Liveness was inferred from two weak proxies — a wake-monitor sentinel
        and transcript churn — and an idle session has neither. Measured on
        `quokka`: pid 34243 alive with its start-time anchor matching, while
        the card read dead and offered to boot a fresh session onto its
        codename. That offer would have put TWO sessions on one title.

        Asked only for cards that would otherwise render dead, and rechecked at
        most every 30s: `confirm` costs a CIM query per Windows session and the
        roster polls every two seconds.
        """
        hit = self._alive_cache.get(title)
        if hit and now - hit[0] < ALIVE_RECHECK_SECS:
            return hit[1]
        from ping_hub import reap
        rec = reap.find_record(INBOX_ROOT, title)
        ans = None if not rec else reap.confirm(rec)[0]
        self._alive_cache[title] = (now, ans)
        return ans

    def _status(self, title: str) -> str:
        """Session-written live status: relay-inbox/<title>/.status (dotfile,
        invisible to inbox scans). Convention: a session updates it with one
        line whenever what it is working on changes."""
        try:
            with open(INBOX_ROOT / title / ".status", encoding="utf-8") as fh:
                return fh.read().strip()[:120]
        except OSError:
            return ""

    def _watching(self, title: str) -> bool:
        try:
            age = time.time() - (INBOX_ROOT / title / ".watching").stat().st_mtime
        except OSError:
            return False
        return age < WATCH_STALE_SECS

    def _slots(self) -> dict[str, str]:
        """(side, codename) -> slot digit, from cx.toml [slots]. The slot IS
        the channel: ctrl+alt+num <digit> talks to that codename."""
        try:
            import tomllib
            with open(CX_TOML, "rb") as fh:
                slots = tomllib.load(fh).get("slots") or {}
        except (OSError, ValueError):
            return {}
        out = {}
        for digit, entry in slots.items():
            if isinstance(entry, dict) and entry.get("codename"):
                out[f"{entry.get('side', 'wsl')}:{entry['codename']}"] = str(digit)
        return out

    def set_channel(self, title: str, side: str, slot: str) -> tuple[bool, str]:
        """Bind a slot digit to a thread via cx-slot.py (the canonical writer —
        one codename per slot, steal/move semantics preserved)."""
        import subprocess
        if slot not in tuple("0123456789"):
            return False, "slot must be 0-9"
        try:
            r = proc.run(
                [CFG.cx_ptt.python, str(CX_SLOT), title, slot, "--side", side],
                capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
            )
            return r.returncode == 0, (r.stdout + r.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)

    def channel_of(self, title: str, side: str) -> str:
        """The slot digit bound to one codename, or "".

        Needed because the roster cannot answer it for the standing title: the
        roster skips that title on purpose, so anything reading a channel off
        the roster reads nothing for the hub's own inbox and reports an
        unbound channel that is in fact bound.
        """
        return self._slots().get(f"{side}:{title}", "")

    def refresh_roster(self) -> None:
        reg = _read_json(SESSIONS) or {}
        squads = self._squad_titles()
        channels = self._slots()
        activity = self._activity_map()
        pending_esc = {(c["side"], c["from"]) for c in self._esc_load()
                       if c.get("status") == "pending"}
        now = time.time()
        with self.lock:
            for title, e in (reg.get("sessions") or {}).items():
                if title == HUB_TITLE:
                    continue
                t = self.threads.setdefault(f"{self.side}:{title}", {"title": title, "side": self.side})
                t.update(
                    session_id=e.get("session_id", ""),
                    cwd=e.get("cwd", ""),
                    workspace=e.get("workspace", ""),
                    auto=bool(e.get("auto")),
                    heartbeat=e.get("last_heartbeat", ""),
                    watching=self._watching(title),
                    squad=squads.get(title, ""),
                    channel=channels.get(f"{self.side}:{title}", ""),
                    seen_at=now,
                    last=self._last.get(f"{self.side}:{title}"),
                    active=(now - activity.get(e.get("session_id", ""), 0)) < 90,
                    esc=(self.side, title) in pending_esc,
                    gone=False,   # present in the registry = NOT gone; the
                    # flag must heal after a torn/empty read marked it
                    **self._session_meta(e.get("session_id", ""), e.get("cwd", "")),
                )
                custom = self._titles()
                # display name: Chris's rename of the HEADING (codename stays
                # the routing key); the label line stays session-owned (status
                # first — it moves as the session works — then first prompt)
                t["display"] = (custom.get(e.get("session_id", ""))
                                or custom.get(f"{self.side}:{title}") or "")
                t["label"] = self._status(title) or t.get("label", "")
                for p in (e.get("projects") or []):
                    self._project_note(f"{self.side}:{title}", p.strip())
                t["projects"] = self._projects.get(f"{self.side}:{title}", [])
                rel = self._relations()
                key = f"{self.side}:{title}"
                t["parent"] = (rel.get(key) or "").split(":")[-1]
                t["children"] = sum(1 for v in rel.values() if v == key)
            # A card with no wake monitor and a quiet transcript is not dead —
            # that is the normal state of a session waiting for a ping. Ask the
            # recorded pid before letting the UI offer to replace it.
            for title, t in self.threads.items():
                if not title.startswith(f"{self.side}:") or t.get("seen_at") != now:
                    continue
                t["idle"] = False
                if t.get("active") or t.get("watching"):
                    continue
                if self._confirmed_alive(t["title"], now):
                    t["idle"] = True
            # registry pruned a title -> keep the thread, mark it gone (history
            # is never deleted; gray-out is the UI's job)
            for key, t in self.threads.items():
                if key.startswith(f"{self.side}:") and t.get("seen_at") != now:
                    t["watching"] = False
                    t["gone"] = True
            self.mark_ghosts()
        self._emit({"event": "roster", "side": self.side})

    # ── inbox watch ──────────────────────────────────────────────────────
    def scan_inboxes(self) -> None:
        try:
            dirs = [d for d in INBOX_ROOT.iterdir() if d.is_dir()]
        except OSError:
            return
        for d in dirs:
            title = d.name
            try:
                cur = {p.name for p in d.glob("*.json")}
            except OSError:
                continue
            prev = self._inbox_snapshot.get(title, set())
            self._inbox_snapshot[title] = cur
            for fname in cur - prev:
                slug = fname[:-5]
                ping = _read_json(d / fname)  # may vanish mid-read: returns None
                if ping is None:
                    ping = {"slug": slug, "summary": "(consumed before read)", "from": "?"}
                # thread key: the SESSION side of the conversation. Pings in a
                # session's inbox FROM chris are outbound; pings in chris's
                # inbox are inbound from that session. Peer pings journal to
                # the receiving session's thread flagged peer=true (UI filters).
                src = ping.get("from", "?")
                if title == HUB_TITLE:
                    tkey, direction, peer = src, "in", False
                    self.maybe_escalation(self.side, ping)
                elif src == HUB_TITLE:
                    tkey, direction, peer = title, "out", False
                else:
                    tkey, direction, peer = title, "in", True
                # dedupe by the THREAD key the entry will land under — the
                # inbox dir name differs from it for chris-inbox files, and a
                # restart re-offers every unconsumed file (first screenshot
                # caught exactly that double).
                if slug in self._seen.get(f"{self.side}:{tkey}", set()):
                    continue
                # our own echo already journaled this send seconds ago; the
                # slug cannot match it because base minted the slug after we
                # had already returned
                if direction == "out" and (
                        self._echo_recent(echo_key(self.side, tkey, src,
                                                   ping.get("summary", "")))
                        or self._echoed_already(self._dkeys, self.side, tkey, {
                            "from": src, "summary": ping.get("summary", ""),
                            "created": ping.get("created", "")})):
                    continue
                self.append(tkey, {
                    "slug": ping.get("slug", slug),
                    "dir": direction,
                    "peer": peer,
                    "from": src,
                    "to": title,
                    "kind": ping.get("kind", "ping"),
                    "summary": ping.get("summary", ""),
                    "created": ping.get("created", ""),
                })

    # ── escalations (Chris directive 2026-08-17: decision cards that survive
    #    him walking away; convention = ping to chris starting ESCALATION[topic]:) ──
    def _esc_path(self) -> Path:
        return HUB_DIR / "escalations.json"

    def _esc_load(self) -> list[dict]:
        return _read_json(self._esc_path()) or []

    def _esc_save(self, cards: list[dict]) -> None:
        tmp = self._esc_path().with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cards, fh, indent=1)
        os.replace(tmp, self._esc_path())

    def maybe_escalation(self, side: str, ping: dict) -> None:
        """Called for every ping landing on the chris pipe (either side).
        Cards are keyed by ping slug — idempotent across restarts; they
        archive on answer and never vanish."""
        summary = ping.get("summary", "")
        if not summary.startswith("ESCALATION["):
            return
        topic, _, question = summary[len("ESCALATION["):].partition("]")
        question = question.lstrip(": ").strip()
        asker = ping.get("from", "?")
        # asker's session id, for the resume-session button: win from the
        # global registry, wsl from the bridge-fed roster
        sid = ""
        if side == "win":
            reg = _read_json(SESSIONS) or {}
            sid = ((reg.get("sessions") or {}).get(asker) or {}).get("session_id", "")
        else:
            with self.lock:
                sid = (self.threads.get(f"wsl:{asker}") or {}).get("session_id", "")
        card = {
            "id": ping.get("slug", ""),
            "ts": ping.get("created", ""),
            "side": side,
            "from": asker,
            "session_id": sid,
            "topic": topic.strip(),
            "question": question or summary,
            "status": "pending",
        }
        with self.lock:
            cards = self._esc_load()
            if any(c["id"] == card["id"] for c in cards):
                return
            cards.append(card)
            self._esc_save(cards)
        self._emit({"event": "escalation", "id": card["id"]})

    def escalations(self) -> list[dict]:
        with self.lock:
            return self._esc_load()

    def dismiss_escalation(self, eid: str) -> bool:
        """Archive a card without answering — visible, never deleted."""
        with self.lock:
            cards = self._esc_load()
            hit = False
            for c in cards:
                if c["id"] == eid and c["status"] == "pending":
                    c["status"] = "dismissed"
                    c["answered_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    hit = True
            if hit:
                self._esc_save(cards)
        if hit:
            self._emit({"event": "escalation", "id": eid})
        return hit

    def reply_escalation(self, eid: str, msg: str) -> tuple[bool, str]:
        """Answer a card: ping the asker back as chris (base's reply semantics
        clear the raw inbox file), then archive the card as answered."""
        with self.lock:
            cards = self._esc_load()
            card = next((c for c in cards if c["id"] == eid), None)
        if card is None:
            return False, "no such escalation"
        ok, detail = self.send(card["from"], msg, side=card["side"])
        if not ok:
            return False, detail
        with self.lock:
            cards = self._esc_load()
            for c in cards:
                if c["id"] == eid:
                    c["status"] = "answered"
                    c["reply"] = msg
                    c["answered_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            self._esc_save(cards)
        self._emit({"event": "escalation", "id": eid})
        return True, detail

    def _echo_recent(self, key: str, now: float | None = None) -> bool:
        """Did WE just journal this same outbound? Prunes as it goes."""
        now = time.time() if now is None else now
        with self.lock:
            for k, ts in list(self._echoes.items()):
                if now - ts > ECHO_WINDOW:
                    del self._echoes[k]
            return key in self._echoes

    def _echo_out(self, title: str, msg: str, side: str, sender: str) -> None:
        """Journal our own outbound AT SEND TIME and push it to the open thread.

        Before this, an outbound entry only existed if the watcher happened to
        see the inbox file before the receiving session consumed it — a race the
        hub usually loses — so Chris's own messages appeared only after a hub
        restart's backfill, wearing a boot timestamp instead of a send time.
        """
        key = echo_key(side, title, sender, msg)
        # the watcher may already have journaled this send: base writes the
        # inbox file before our subprocess even returns, so on a fast delivery
        # it wins the race and we must not add a second copy
        if self._echoed_already(self._dkeys, side, title,
                                {"from": sender, "summary": msg,
                                 "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")}):
            return
        with self.lock:
            self._echoes[key] = time.time()
        self.append(title, {
            "slug": "", "dir": "out", "peer": False, "from": sender,
            "to": title, "kind": "ping", "summary": msg,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "echo": True,
            # carried so backfill can recognise its own echo later: it has no
            # slug to compare against, because base had not minted one yet
            "dkey": echo_key(side, title, sender, msg),
        }, side=side)

    # ── send ─────────────────────────────────────────────────────────────
    def send(self, title: str, msg: str, side: str = "win",
             sender: str | None = None) -> tuple[bool, str]:
        """Ping a session through the target side's own base binary (win:
        directly; wsl: via the bridge). sender defaults to chris (the hub IS
        chris); system notices pass sender='hub' so they stop wearing Chris's
        name (his catch, 2026-08-17). On success it journals its own entry AT
        SEND TIME and emits it, so the open thread renders immediately; the
        watcher's later copy of the same ping is suppressed by echo_key."""
        import subprocess
        sender = sender or HUB_TITLE
        if side == "wsl":
            import urllib.request
            host = self._bridge_host()
            if not host:
                return False, "wsl ip unresolvable"
            try:
                req = urllib.request.Request(
                    f"http://{host}:{BRIDGE_PORT}/send",
                    data=json.dumps({"title": title, "msg": msg,
                                     "sender": sender}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=20) as r:
                    body = json.loads(r.read())
                ok = bool(body.get("ok"))
                if ok:
                    self._echo_out(title, msg, side, sender)
                return ok, body.get("detail", "")
            except OSError as e:
                return False, f"wsl bridge unreachable: {e}"
        try:
            r = proc.run(
                [CFG.paths.base_bin, "relay", "ping", "--to", title,
                 "--from", sender, "--msg", msg],
                # 60s, not 15: base relay ping measured 4-40s on 2026-08-18
                # (machine-wide base slowdown, cause in the base repo, under
                # investigation). At 15s the hub reported failure on sends
                # that had ALREADY delivered - a false failure is worse than
                # a slow one.
                capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
            )
            ok = r.returncode == 0
            if ok:
                self._echo_out(title, msg, side, sender)
            return ok, (r.stdout + r.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)

    # ── WSL bridge client (hub long-polls the bridge over the WSL NAT IP) ──
    def _bridge_host(self) -> str:
        """WSL eth0 IP, resolved fresh on each reconnect — it changes per WSL
        boot, and this machine's localhost relay does not forward WSL ports
        (Docker Desktop config). `wsl hostname -I` is the authority."""
        import subprocess
        try:
            r = proc.run(
                ["wsl", "hostname", "-I"], capture_output=True, text=True, timeout=10,
            )
            out = (r.stdout or "").replace("\x00", "").strip()
            return out.split()[0] if out else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def _cxptt_mark(self, alive: bool, detail: str = "") -> None:
        """`since` moves only on a CHANGE, so the UI can say how long it has
        been down rather than how long ago the last poll was."""
        with self.lock:
            if self.cxptt_state.get("alive") is not alive:
                self.cxptt_state["since"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self.cxptt_state.update(alive=alive, detail=detail, probed=True,
                                    enabled=bool(CFG.cx_ptt.enabled))

    def _cxptt_loop(self) -> None:
        """Notice cx-ptt dead, and bring it back.

        Two instruments, deliberately. The POLL asks the heartbeat file, which
        costs a stat and a small read — `cxptt.status()` enumerates every
        Win32_Process through CIM and cannot run on a loop, and a supervisor
        that cannot look often cannot notice. Only once the decision to act is
        made does the expensive path run.

        The budget is the bridge's, for the bridge's reason: a restart loop
        firing every twenty seconds is worse than none at all, because it
        buries the real failure under identical log lines and keeps a sick
        machine busy. First attempt free, then 30/60/120/300.
        """
        from ping_hub import cxptt
        was = None
        while not self._stop.wait(CXPTT_POLL_SECS):
            beat = cxptt.heartbeat(CFG)
            alive = bool(beat.get("alive"))
            self._cxptt_mark(alive, beat.get("detail", ""))
            if alive != was:      # one honest line per TRANSITION, not per poll
                print(f"cx-ptt {'up' if alive else 'DOWN'}: "
                      f"{beat.get('detail', '')}", flush=True)
                was = alive
            if alive:
                self._cx_heal.reset()
                continue
            now = time.time()
            if not self._cx_heal.due(now):
                continue
            self._cx_heal.mark(now)
            try:
                out = cxptt.restart(CFG)
            except OSError as e:
                # a supervisor that dies of the thing it supervises is worse
                # than no supervisor: it takes the reporting down with it
                print(f"cx-ptt restart failed: {e}", flush=True)
                continue
            print(f"cx-ptt restart: {out.get('detail', '')}", flush=True)

    def _bridge_mark(self, up: bool, detail: str = "") -> None:
        """Record bridge liveness. `since` moves only on a CHANGE of state, so
        the UI can say how long it has been down rather than how long ago the
        last poll was."""
        with self.lock:
            if self.bridge_state.get("up") is not up:
                self.bridge_state["since"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self.bridge_state.update(up=up, detail=detail, probed=True,
                                     enabled=bool(CFG.wsl.enabled))

    def _bridge_heal(self) -> None:
        """Try to bring the bridge back from this side.

        The systemd unit is the primary mechanism; this is the backstop for the
        one case the unit cannot cover — WSL itself was not running when the
        bridge died, so nothing inside it was alive to restart anything. Every
        failure here is expected and none of them may kill the loop: a hub that
        falls over because WSL is down is worse than a hub with no WSL side.
        """
        import subprocess

        from ping_hub import autostart
        if not CFG.wsl.enabled or not CFG.wsl.distro:
            return
        unit = f"systemctl --user restart {autostart.BRIDGE_UNIT}"
        try:
            r = proc.run(autostart.wsl_sh(unit), capture_output=True,
                         text=True, timeout=30)
            if r.returncode == 0:
                print(f"bridge down: ran `{unit}` in {CFG.wsl.distro}")
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
        # no systemd, or the unit was never installed. NOT a backgrounded shell
        # job: WSL kills those when the interop session exits, silently
        if autostart.start_bridge_detached(CFG):
            print(f"bridge down: started it directly in {CFG.wsl.distro}")
            return
        print(f"bridge down: could not restart it in {CFG.wsl.distro}")

    def _bridge_loop(self) -> None:
        import urllib.request
        cursor = 0
        epoch = None
        primed = False
        host = ""
        while not self._stop.is_set():
            try:
                if not host:
                    host = self._bridge_host()
                    if not host:
                        raise OSError("wsl ip unresolvable")
                if not primed:
                    with urllib.request.urlopen(
                        f"http://{host}:{BRIDGE_PORT}/snapshot", timeout=5
                    ) as r:
                        snap = json.loads(r.read())
                    cursor, epoch = epoch_reset(epoch, snap.get("epoch"), cursor)
                    self._bridge_roster(snap)
                    primed = True
                    self._bridge_mark(True, f"{host}:{BRIDGE_PORT}")
                    self._heal.reset()
                with urllib.request.urlopen(
                    f"http://{host}:{BRIDGE_PORT}/events?cursor={cursor}", timeout=35
                ) as r:
                    body = json.loads(r.read())
                fresh_cursor, new_epoch = epoch_reset(epoch, body.get("epoch"), cursor)
                if new_epoch != epoch and epoch is not None:
                    # a different bridge process answered: this response was
                    # filtered against a cursor that means nothing to it, so
                    # discard it, re-prime, and poll again from zero
                    print(f"bridge restarted: cursor reset (was {cursor})", flush=True)
                    cursor, epoch, primed = 0, new_epoch, False
                    continue
                epoch = new_epoch
                cursor = body.get("cursor", cursor)
                self._bridge_mark(True, f"{host}:{BRIDGE_PORT}")
                self._heal.reset()
                for ev in body.get("events", []):
                    if ev["kind"] == "roster":
                        self._bridge_roster(ev)
                    elif ev["kind"] == "ping":
                        self._bridge_ping(ev.get("inbox", "?"), ev.get("ping") or {})
            except OSError as e:
                # WSL down or bridge not running: gray the wsl threads, retry.
                primed = False
                host = ""
                self._bridge_mark(False, str(e)[:160])
                with self.lock:
                    for key, t in self.threads.items():
                        if key.startswith("wsl:"):
                            t["watching"] = False
                            t["bridge_down"] = True
                            # active and doing were left ALONE here until
                            # 2026-08-19, so every wsl card kept its last
                            # `active: true` and its last verb for as long as
                            # the bridge stayed down — pulsing dots and a verb
                            # frozen at the instant the transport died. After a
                            # reboot that is a whole board of live-looking work
                            # that stopped minutes ago.
                            t["active"] = False
                            t["idle"] = False
                self._emit({"event": "roster", "side": "wsl"})
                now = time.time()
                if self._heal.due(now):
                    self._heal.mark(now)
                    self._bridge_heal()
                if self._stop.wait(5):
                    return

    def _bridge_roster(self, snap: dict) -> None:
        watching = snap.get("watching") or {}
        with self.lock:
            for title, e in (snap.get("sessions") or {}).items():
                if title == HUB_TITLE:
                    continue
                t = self.threads.setdefault(f"wsl:{title}", {"title": title, "side": "wsl"})
                t.update(
                    session_id=e.get("session_id", ""),
                    cwd=e.get("cwd", ""),
                    workspace=e.get("workspace", ""),
                    auto=bool(e.get("auto")),
                    heartbeat=e.get("last_heartbeat", ""),
                    watching=bool(watching.get(title)),
                    squad="",
                    channel=self._slots().get(f"wsl:{title}", ""),
                    bridge_down=False,
                    gone=False,
                    last=self._last.get(f"wsl:{title}"),
                    display=(self._titles().get(e.get("session_id", ""))
                             or self._titles().get(f"wsl:{title}") or ""),
                    label=e.get("status", "") or e.get("label", ""),
                    model=e.get("model", ""),
                    effort=e.get("effort", ""),
                    active=bool(e.get("active")),
                    ctx=e.get("ctx", 0) or 0,
                    doing=e.get("doing", ""),
                    doing_at=e.get("doing_at", 0) or 0,
                )
                if not t.get("active") and not t.get("watching"):
                    t["idle"] = bool(self._confirmed_alive(title, time.time()))
                else:
                    t["idle"] = False
                for p in (e.get("projects") or []):
                    self._project_note(f"wsl:{title}", p.strip())
                t["projects"] = self._projects.get(f"wsl:{title}", [])
                rel = self._relations()
                key = f"wsl:{title}"
                t["parent"] = (rel.get(key) or "").split(":")[-1]
                t["children"] = sum(1 for v in rel.values() if v == key)
            self.mark_ghosts()
        self._emit({"event": "roster", "side": "wsl"})

    def _bridge_ping(self, inbox: str, ping: dict) -> None:
        src = ping.get("from", "?")
        slug = ping.get("slug", "")
        if inbox == HUB_TITLE:
            tkey, direction, peer = src, "in", False
            self.maybe_escalation("wsl", ping)
        elif src == HUB_TITLE:
            tkey, direction, peer = inbox, "out", False
        else:
            tkey, direction, peer = inbox, "in", True
        if slug and slug in self._seen.get(f"wsl:{tkey}", set()):
            return
        if direction == "out" and (
                self._echo_recent(echo_key("wsl", tkey, src, ping.get("summary", "")))
                or self._echoed_already(self._dkeys, "wsl", tkey, {
                    "from": src, "summary": ping.get("summary", ""),
                    "created": ping.get("created", "")})):
            return
        self.append(tkey, {
            "slug": slug,
            "dir": direction,
            "peer": peer,
            "from": src,
            "to": inbox,
            "kind": ping.get("kind", "ping"),
            "summary": ping.get("summary", ""),
            "created": ping.get("created", ""),
        }, side="wsl")

    # ── backfill (pings delivered while the hub was not running) ─────────
    def _marks(self) -> tuple[dict[str, set], dict[str, str], str]:
        """Per-thread slugs and newest timestamp, plus a global floor.

        The floor is the newest entry anywhere in the journals: a thread the
        hub has never journaled must not drag in months of pre-hub history
        just because its own mark is empty.
        """
        newest: dict[str, str] = {}
        floor = ""
        for side in ("win", "wsl"):
            for jp in (HUB_DIR / "threads" / side).glob("*.jsonl"):
                key = f"{side}:{jp.stem}"
                try:
                    with open(jp, encoding="utf-8") as fh:
                        for line in fh:
                            try:
                                created = json.loads(line).get("created", "")
                            except (json.JSONDecodeError, AttributeError):
                                continue
                            if created > newest.get(key, ""):
                                newest[key] = created
                            if created > floor:
                                floor = created
                except OSError:
                    continue
        return self._seen, newest, floor

    def backfill(self) -> int:
        """Append pings base recorded but the hub never saw. Runs once at boot.

        Never prunes and never rewrites: base's graph is missing records the
        journal still holds, so anything two-way would destroy real history.
        """
        from ping_hub import backfill as bf
        seen, newest, floor = self._marks()
        echoes = self._echo_index()
        written = 0
        stores = bf.graph_stores(CFG)
        for side, store in zip(("win", "wsl"), stores):
            records = bf.read_pings(store)
            if not records:
                continue
            for entry in bf.plan(CFG, seen, newest, side, records, floor):
                thread = entry.pop("_thread")
                if entry.get("dir") == "out" and self._echoed_already(
                        echoes, side, thread, entry):
                    continue
                self.append(thread, entry, side=side)
                written += 1
        if written:
            print(f"backfilled {written} ping(s) the hub was down for",
                  flush=True)
        return written

    def _echo_index(self) -> dict:
        """{"side:thread" -> {dkey: ts_secs}} for entries WE echoed."""
        idx: dict[str, dict[str, float]] = {}
        for side in ("win", "wsl"):
            d = HUB_DIR / "threads" / side
            try:
                files = list(d.glob("*.jsonl"))
            except OSError:
                continue
            for f in files:
                key = f"{side}:{f.stem}"
                try:
                    with open(f, encoding="utf-8") as fh:
                        for line in fh:
                            try:
                                e = json.loads(line)
                            except ValueError:
                                continue
                            if e.get("dkey"):
                                idx.setdefault(key, {})[e["dkey"]] = ts_secs(
                                    e.get("ts") or "")
                except OSError:
                    continue
        return idx

    def _echoed_already(self, idx: dict, side: str, thread: str,
                        entry: dict) -> bool:
        """Did we already journal this outbound ourselves, at send time?"""
        k = echo_key(side, thread, entry.get("from", ""), entry.get("summary", ""))
        when = idx.get(f"{side}:{thread}", {}).get(k)
        if not when:
            return False
        theirs = ts_secs(entry.get("created") or entry.get("ts") or "")
        return bool(theirs) and abs(theirs - when) <= ECHO_BACKFILL_WINDOW

    # ── loops ────────────────────────────────────────────────────────────
    def run(self) -> None:
        threading.Thread(target=self._roster_loop, daemon=True).start()
        threading.Thread(target=self._inbox_loop, daemon=True).start()
        # one-sided machines (Mac, or Windows without WSL) have no bridge to
        # long-poll; starting the loop there would spend forever failing to
        # resolve a NAT IP and graying threads that never existed
        if CFG.wsl.enabled:
            threading.Thread(target=self._bridge_loop, daemon=True).start()
        # cx-ptt is Windows-and-cx-ptt-only. On a machine that never had it,
        # `enabled` derives False from a missing cx.toml and the supervisor
        # never runs — absent is not a service that needs restarting.
        if CFG.cx_ptt.enabled and os.name == "nt":
            threading.Thread(target=self._cxptt_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _roster_loop(self) -> None:
        while not self._stop.wait(ROSTER_POLL_SECS):
            self.refresh_roster()

    def _inbox_loop(self) -> None:
        # first pass primes the snapshot so a hub restart doesn't re-journal
        # files that were already sitting in inboxes before boot — except ones
        # never journaled at all (fresh _seen check handles those).
        while not self._stop.wait(INBOX_POLL_SECS):
            self.scan_inboxes()
