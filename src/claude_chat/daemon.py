"""claude-chat — a chat surface for headless Claude Code sessions.

A localhost HTTP daemon serving a single-page chat UI (``chat.html``) and a
small JSON API. Every conversation is a headless ``claude --print`` session
pinned to a **directory**: pick a saved workspace or type any path, and the
session boots there. Replies ``--resume`` it; a reply landing mid-turn is
steered into the live session via ``base relay task`` instead of queueing; and
approve mode gates every action behind an Allow/Deny card.

What this surface adds over a terminal: the live turn is visible from
anywhere. The stream tap that announces the session id also feeds an activity
ticker (current tool / latest reasoning line) over SSE, so a long turn never
looks like a dead daemon. Ticker lines are ephemeral by design — pushed, never
persisted; the conversation file holds only real messages.

Security boundary: the daemon binds ``127.0.0.1`` by default — the OS session
is the allowlist, the same trust as the operator's own terminal. No token, no
pairing. ``claude-chat host tailscale`` rebinds to the machine's tailscale
interface (100.64/10) so a phone reaches it over the tailnet's own device auth
and encryption. Binding a public interface is deliberately unsupported: anyone
who reaches this daemon can run code in every registered workspace.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from claude_chat import store, turns
from claude_chat.turns import TurnResult, build_cmd, context_window_for, parse_scope

SURFACE = "claude-chat"
_UI_FILE = Path(__file__).resolve().parent / "chat.html"


def _log(message: str) -> None:
    """Narrate to stdout — the serve terminal and journald are the operator's
    window into the daemon."""
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def daemon_url(config: dict[str, Any]) -> str:
    host = str(config.get("host") or "127.0.0.1")
    if host == "0.0.0.0":   # a wildcard bind is not a reachable url
        host = "127.0.0.1"
    return f"http://{host}:{config.get('port', store.DEFAULT_PORT)}"


def say_command() -> str:
    """The absolute ``claude-chat say`` invocation for THIS install — baked
    into steer messages so the receiving session needs zero PATH luck."""
    return f"{Path(sys.executable).parent / 'claude-chat'} say"


def compose_prompt(text: str, *, resumed: bool, updates: bool = True,
                   boot: str = "") -> str:
    return turns.compose_prompt(text, resumed=resumed, updates=updates, boot=boot,
                                surface=SURFACE, say_cmd=say_command())


def relay_steer(session_id: str, text: str) -> str | None:
    """Steer a live turn; returns the relay task slug (the receipt handle)."""
    return turns.relay_steer(session_id, text,
                             from_name="claude-chat",
                             slug_prefix="chat-steer",
                             say_cmd=say_command())


# ---------------------------------------------------------------------------
# Event bus — one process-wide sequence the SSE clients follow
# ---------------------------------------------------------------------------

class EventBus:
    def __init__(self, maxlen: int = 500) -> None:
        self._cond = threading.Condition()
        self._events: deque[tuple[int, str, dict[str, Any]]] = deque(maxlen=maxlen)
        self.seq = 0

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        with self._cond:
            self.seq += 1
            self._events.append((self.seq, kind, data))
            self._cond.notify_all()

    def wait_since(self, since: int, timeout: float = 20.0) -> list[tuple[int, str, dict[str, Any]]]:
        """Events newer than *since*; blocks up to *timeout* when none yet."""
        with self._cond:
            fresh = [e for e in self._events if e[0] > since]
            if fresh:
                return fresh
            self._cond.wait(timeout)
            return [e for e in self._events if e[0] > since]


# ---------------------------------------------------------------------------
# The turn — this surface's parameters over the shared core
# ---------------------------------------------------------------------------

def _write_turn_mcp_config(path: Path, *, env: dict[str, str]) -> str:
    """The approve-mode ``--mcp-config``: exactly one server, the Allow/Deny
    gate (``claude_chat.approve``). The server key IS the tool namespace —
    ``claude-chat`` here, matching :data:`turns.APPROVE_TOOL`."""
    config = {
        "mcpServers": {
            "claude-chat": {
                "command": sys.executable,
                "args": ["-m", "claude_chat.approve"],
                "env": env,
            }
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return str(path)


def run_turn(
    config: dict[str, Any],
    *,
    text: str,
    conv_id: str,
    cwd: str,
    workspace: dict[str, Any] | None,
    resume: str | None,
    claude_bin: str,
    on_session: Callable[[str], None] | None = None,
    on_activity: Callable[[str], None] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> TurnResult:
    """One turn: compose → spawn in *cwd* → stream-tap.

    The workspace's own model / mode / boot prefix / extra dirs win over the
    daemon's defaults; an empty string on the workspace means "inherit".
    """
    workspace = workspace or {}
    mode = str(workspace.get("mode") or config.get("mode", "approve"))
    model = str(workspace.get("model") or config.get("model") or "")
    prompt = compose_prompt(text, resumed=bool(resume),
                            updates=bool(config.get("updates", True)),
                            boot=str(workspace.get("boot") or ""))
    mcp_config: str | None = None
    if mode != "skip":
        mcp_config = _write_turn_mcp_config(
            store.home() / "turns" / f"{conv_id}.mcp.json",
            env={
                "CLAUDE_CHAT_URL": daemon_url(config),
                "CLAUDE_CHAT_CONVERSATION": conv_id,
                "CLAUDE_CHAT_APPROVE_TIMEOUT": str(int(config.get("approve_timeout_sec", 300))),
            },
        )
    cmd = build_cmd(
        claude_bin,
        mode=mode,
        prompt=prompt,
        resume=resume,
        mcp_config=mcp_config,
        full_load=bool(config.get("full_load")),
        model=model or None,
        add_dirs=[str(d) for d in (workspace.get("dirs") or [])],
    )
    # Explicit env for the child: inherit the daemon's, add the routing that
    # `claude-chat say` needs for the session's mid-turn voice.
    env = dict(os.environ)
    env["CLAUDE_CHAT_URL"] = daemon_url(config)
    env["CLAUDE_CHAT_CONVERSATION"] = conv_id
    return turns.spawn_turn(
        cmd,
        cwd=cwd,
        env=env,
        timeout_sec=int(config.get("turn_timeout_sec", 1800)),
        on_session=on_session,
        on_activity=on_activity,
        on_event=on_event,
    )


# ---------------------------------------------------------------------------
# The rail — store + dispatch + approvals, transport-agnostic (the HTTP
# handler calls these methods; the tests drive them directly)
# ---------------------------------------------------------------------------

class ChatRail:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        claude_bin: str,
        turn_runner: Callable[..., TurnResult] = run_turn,
        max_workers: int = 4,
    ) -> None:
        self.config = config
        self.claude_bin = claude_bin
        self.bus = EventBus()
        self._turn_runner = turn_runner
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="rail-turn")
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._threads_guard = threading.Lock()
        self._store_guard = threading.Lock()
        self.threads = store.prune_threads(store.load_threads())
        # Live turn state, authoritative for the UI: the threads map only says
        # "running" once the session announces itself (seconds in) — this
        # covers the whole turn span, dispatch to release.
        self.live: dict[str, str] = {}
        self.approvals: dict[str, dict[str, Any]] = {}
        self._approvals_guard = threading.Lock()

    # -- store -------------------------------------------------------------

    def _append(self, conv_id: str, role: str, text: str,
                kind: str = "chat", extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Append one message. Returns None when the conversation is gone —
        the operator deleted it mid-turn, and a live turn must not write its
        file back into existence."""
        with self._store_guard:
            conv = store.load_conversation(conv_id)
            if conv is None:
                return None
            message = {
                "id": len(conv["messages"]) + 1,
                "role": role,
                "kind": kind,
                "text": text,
                "ts": time.time(),
            }
            if extra:
                message.update(extra)
            conv["messages"].append(message)
            store.save_conversation(conv)
        self.bus.publish("message", {"conversation_id": conv_id, "message": message})
        return message

    def _update_message(self, conv_id: str, message_id: int,
                        patch: dict[str, Any]) -> None:
        with self._store_guard:
            conv = store.load_conversation(conv_id)
            if conv is None:
                return
            for message in conv["messages"]:
                if message["id"] == message_id:
                    message.update(patch)
                    break
            store.save_conversation(conv)

    def _set_status(self, conv_id: str, status: str) -> None:
        self.live[conv_id] = status
        self.bus.publish("status", {"conversation_id": conv_id, "status": status})

    def status_of(self, conv_id: str) -> str:
        return self.live.get(conv_id, "idle")

    def _note_context(self, conv_id: str, usage: dict[str, Any], model: str) -> None:
        """Record the turn's context footprint (prompt-side tokens: fresh +
        cache reads + cache writes) so the operator can see a long
        conversation closing in on the window."""
        tokens = 0
        for key in ("input_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens"):
            try:
                tokens += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                pass
        if tokens <= 0:
            return
        window = context_window_for(model)
        with self._store_guard:
            conv = store.load_conversation(conv_id)
            if conv is None:
                return
            conv["context_tokens"] = tokens
            conv["context_window"] = window
            store.save_conversation(conv)
        self.bus.publish("context", {"conversation_id": conv_id,
                                     "tokens": tokens, "window": window})

    # -- inbound -----------------------------------------------------------

    def post_operator_message(self, conv_id: str | None, text: str, *,
                              workspace: str = "", cwd: str = "") -> dict[str, Any]:
        """The single inbound door — create/extend a conversation, dispatch the
        turn. Returns ``{"ok": True, "conversation_id": …}`` immediately;
        everything after is SSE.

        *workspace* and *cwd* only mean something on a FRESH conversation: they
        choose the directory the session boots in, and that choice is pinned
        for the conversation's life (a resumed session cannot move).
        """
        text = text.strip()
        if not text:
            return {"ok": False, "reason": "empty message"}
        if conv_id is None:
            return self._open_conversation(text, workspace=workspace, cwd=cwd)
        conv = store.load_conversation(conv_id)
        if conv is None:
            return {"ok": False, "reason": f"unknown conversation {conv_id}"}
        self._append(conv_id, "operator", text)
        self._dispatch(text, conv_id)
        return {"ok": True, "conversation_id": conv_id}

    def _dispatch(self, text: str, conv_id: str) -> None:
        """Hand the turn to the pool. Status flips to running HERE, not inside
        the worker: between the POST answering and a pool thread getting
        scheduled, ``status_of`` would otherwise still say idle — and an idle
        conversation is one the operator may delete."""
        self._set_status(conv_id, "running")
        self._pool.submit(self._work, text, conv_id)

    def _open_conversation(self, text: str, *, workspace: str,
                           cwd: str) -> dict[str, Any]:
        record = store.find_workspace(workspace) if workspace else None
        if workspace and record is None:
            return {"ok": False, "reason": f"unknown workspace {workspace!r}"}
        if record is None and not cwd:
            # `@toolbox fix the tests` on a fresh conversation names a saved
            # workspace. An @word that matches nothing is left in the message.
            scope, agenda = parse_scope(text)
            record = store.find_workspace(scope) if scope else None
            if record is not None:
                text = agenda
        path, why = store.resolve_cwd(record, cwd)
        directory = Path(path).expanduser()
        if not directory.is_dir():
            return {"ok": False,
                    "reason": f"{directory} is not a directory ({why}) — "
                              "pick a workspace or give a path that exists"}
        conv_id = store.new_conversation_id()
        conv = {
            "id": conv_id,
            "title": text[:64],
            "created": time.time(),
            "cwd": str(directory.resolve()),
            "workspace": record["id"] if record else "",
            "messages": [],
        }
        store.save_conversation(conv)
        self.bus.publish("conversation", {
            "id": conv_id, "title": conv["title"], "created": conv["created"],
            "last_ts": conv["created"], "message_count": 0,
            "workspace": conv["workspace"], "cwd": conv["cwd"],
        })
        _log(f"✦ new conversation {conv_id} in {conv['cwd']}"
             + (f" ({record['name']})" if record else ""))
        self._append(conv_id, "operator", text)
        self._dispatch(text, conv_id)
        return {"ok": True, "conversation_id": conv_id}

    def say(self, conv_id: str, text: str) -> dict[str, Any]:
        """The session's mid-turn voice (``claude-chat say``) — and any other
        service-layer post into a conversation."""
        if store.load_conversation(conv_id) is None:
            return {"ok": False, "reason": f"unknown conversation {conv_id}"}
        self._append(conv_id, "assistant", text, kind="say")
        return {"ok": True, "conversation_id": conv_id}

    # The settings the UI may write live (no restart). host/port/hostname are
    # deliberately absent: rebinding the network from a page reachable over
    # that network is a footgun, and a port change kills the daemon serving the
    # page. Those stay CLI-only. Everything here is read from self.config per
    # turn, so a change lands on the next turn across every conversation.
    _UI_SETTINGS = ("mode", "model", "updates", "ack_posts", "midflight_relay",
                    "full_load", "default_workspace", "default_cwd")
    _BOOL_SETTINGS = ("updates", "ack_posts", "midflight_relay", "full_load")

    def _settings_snapshot(self) -> dict[str, Any]:
        snap = {k: self.config.get(k, store.CONFIG_DEFAULTS.get(k))
                for k in self._UI_SETTINGS}
        snap["model"] = snap.get("model") or ""
        snap["default_workspace"] = snap.get("default_workspace") or ""
        snap["default_cwd"] = snap.get("default_cwd") or ""
        return snap

    def update_setting(self, key: str, value: Any) -> dict[str, Any]:
        """Change one daemon-wide setting live, persist it, broadcast it. The
        CLI's ``apply_setting`` is the other door — it bounces the service;
        this one doesn't, because it holds the live config in hand."""
        if key == "mode":
            if value not in ("approve", "skip"):
                return {"ok": False, "reason": "mode must be approve or skip"}
            self.config["mode"] = value
        elif key == "model":
            self.config["model"] = str(value or "").strip()
        elif key in self._BOOL_SETTINGS:
            self.config[key] = bool(value)
        elif key == "default_workspace":
            wid = str(value or "").strip()
            if wid and not store.find_workspace(wid):
                return {"ok": False, "reason": f"unknown workspace {wid!r}"}
            self.config["default_workspace"] = wid
        elif key == "default_cwd":
            path = str(value or "").strip()
            if path and not Path(path).expanduser().is_dir():
                return {"ok": False, "reason": f"{path} is not a directory"}
            self.config["default_cwd"] = path
        else:
            return {"ok": False, "reason": f"{key!r} is not settable from the UI"}
        store.save_config(self.config)
        self.bus.publish("settings", self._settings_snapshot())
        _log(f"⚙ {key} → {self.config.get(key)!r}")
        return {"ok": True, key: self.config.get(key)}

    def _broadcast_workspaces(self) -> None:
        self.bus.publish("workspaces", {
            "workspaces": store.load_workspaces(),
            "default_workspace": str(self.config.get("default_workspace") or ""),
        })

    def save_workspace(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Add or update a workspace (idempotent by id). ``spec`` mirrors the
        CLI's ``workspace add`` flags: path, id, name, model, mode, boot, dirs,
        default. Empty model/mode mean "inherit the daemon default"."""
        path = str(spec.get("path") or "").strip()
        if not path:
            return {"ok": False, "reason": "a path is required"}
        mode = str(spec.get("mode") or "").strip()
        if mode and mode not in ("approve", "skip"):
            return {"ok": False, "reason": "mode must be approve, skip, or blank"}
        dirs = [str(d).strip() for d in (spec.get("dirs") or []) if str(d).strip()]
        try:
            entry = store.add_workspace(
                Path(path), workspace_id=str(spec.get("id") or "").strip(),
                name=str(spec.get("name") or "").strip(),
                model=str(spec.get("model") or "").strip(), mode=mode,
                boot=str(spec.get("boot") or "").strip(), dirs=dirs)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}
        if spec.get("default"):
            self.config["default_workspace"] = entry["id"]
            store.save_config(self.config)
            self.bus.publish("settings", self._settings_snapshot())
        self._broadcast_workspaces()
        _log(f"🗂 workspace {entry['id']} saved ({entry['path']})")
        return {"ok": True, "workspace": entry}

    def remove_workspace(self, workspace_id: str) -> dict[str, Any]:
        if not store.remove_workspace(workspace_id):
            return {"ok": False, "reason": f"unknown workspace {workspace_id!r}"}
        # store.remove_workspace clears the default on disk if it pointed here;
        # re-sync the live copy so both agree.
        self.config["default_workspace"] = store.load_config().get(
            "default_workspace", "")
        self._broadcast_workspaces()
        self.bus.publish("settings", self._settings_snapshot())
        _log(f"🗂 workspace {workspace_id} removed")
        return {"ok": True, "removed": workspace_id,
                "note": "conversations already open in it keep their directory"}

    def delete(self, conv_id: str) -> dict[str, Any]:
        # Under the same guard the appends take: otherwise the unlink can land
        # inside a live turn's read-modify-write and the save writes the file
        # back. A turn still running against a deleted conversation finds it
        # gone on its next append and stops narrating.
        with self._store_guard:
            if not store.delete_conversation(conv_id):
                return {"ok": False, "reason": f"unknown conversation {conv_id}"}
        with self._threads_guard:
            self.threads.pop(conv_id, None)
        self.live.pop(conv_id, None)
        self.bus.publish("deleted", {"conversation_id": conv_id})
        _log(f"🗑 conversation {conv_id} deleted")
        return {"ok": True}

    def close(self) -> None:
        """Drain the turn pool — nothing outlives the rail that owns it."""
        self._pool.shutdown(wait=True)

    # -- approvals ---------------------------------------------------------

    def create_approval(self, conv_id: str, tool_name: str,
                        tool_input: dict[str, Any], timeout_sec: int) -> dict[str, Any]:
        if store.load_conversation(conv_id) is None:
            return {"ok": False, "reason": f"unknown conversation {conv_id}"}
        request_id = f"a{int(time.time() * 1000):x}{os.urandom(2).hex()}"
        detail = json.dumps(tool_input, indent=2, sort_keys=True)
        if len(detail) > 600:
            detail = detail[:600] + "\n… (truncated)"
        with self._approvals_guard:
            self.approvals[request_id] = {
                "conversation_id": conv_id,
                "tool_name": tool_name,
                "verdict": None,
                "created": time.time(),
                "timeout_sec": timeout_sec,
            }
        message = self._append(
            conv_id, "assistant",
            f"wants to run: {tool_name}",
            kind="approval",
            extra={"approval_id": request_id, "tool_name": tool_name,
                   "detail": detail, "timeout_sec": timeout_sec, "verdict": None},
        )
        if message is None:
            # The conversation was deleted between the check and the card. No
            # card, no operator, no approval — the gate reads this as a deny.
            with self._approvals_guard:
                self.approvals.pop(request_id, None)
            return {"ok": False, "reason": f"unknown conversation {conv_id}"}
        with self._approvals_guard:
            self.approvals[request_id]["message_id"] = message["id"]
        _log(f"🛡 approval requested — {tool_name} (conversation {conv_id})")
        return {"ok": True, "id": request_id}

    def approval_state(self, request_id: str) -> dict[str, Any]:
        with self._approvals_guard:
            entry = self.approvals.get(request_id)
            if entry is None:
                return {"ok": False, "reason": "unknown approval"}
            return {"ok": True, "verdict": entry["verdict"]}

    def set_verdict(self, request_id: str, verdict: str) -> dict[str, Any]:
        if verdict not in ("allow", "deny", "timeout"):
            return {"ok": False, "reason": "verdict must be allow, deny, or timeout"}
        with self._approvals_guard:
            entry = self.approvals.get(request_id)
            if entry is None:
                return {"ok": False, "reason": "unknown approval"}
            if entry["verdict"] is None:
                entry["verdict"] = verdict
            verdict = entry["verdict"]   # first answer wins — no flip-flops
        conv_id = entry["conversation_id"]
        with self._store_guard:
            conv = store.load_conversation(conv_id)
            if conv is not None:
                for message in conv["messages"]:
                    if message.get("approval_id") == request_id:
                        message["verdict"] = verdict
                        break
                store.save_conversation(conv)
        self.bus.publish("approval", {"conversation_id": conv_id,
                                      "id": request_id, "verdict": verdict})
        _log(f"🛡 approval {request_id[:8]}… → {verdict}")
        return {"ok": True, "verdict": verdict}

    # -- the turn ----------------------------------------------------------

    def _lock_for(self, conv_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(conv_id, threading.Lock())

    def _note_session(self, conv_id: str, session_id: str) -> None:
        with self._threads_guard:
            self.threads[conv_id] = {"session_id": session_id,
                                     "status": "running",
                                     "last_turn": time.time()}
            store.save_threads(self.threads)
        # Bind the announced session to a stable relay title immediately —
        # headless sessions never self-register, and an unbound session is
        # invisible to relay_steer, which is why mid-flight replies queued
        # instead of steering with ✓/✓✓ receipts.
        if turns.relay_register(session_id, f"cc-{conv_id}"):
            _log(f"  relay bind cc-{conv_id} ← session {session_id[:8]}…")
        _log(f"  session {session_id[:8]}… announced — conversation {conv_id}")

    def _work(self, text: str, conv_id: str) -> None:
        lock = self._lock_for(conv_id)
        if not lock.acquire(blocking=False):
            entry = self.threads.get(conv_id) or {}
            live = (entry.get("session_id")
                    if entry.get("status") == "running" else None)
            slug = (relay_steer(live, text)
                    if live and self.config.get("midflight_relay", True) else None)
            if slug:
                _log(f"📨 relayed mid-flight into session {(live or '')[:8]}… ({slug})")
                message = self._append(
                    conv_id, "system",
                    "steered into the live turn — its answer folds this in",
                    kind="steer", extra={"receipt": "sent", "slug": slug})
                if message is None:
                    return   # deleted out from under us — nothing to receipt
                threading.Thread(
                    target=self._watch_receipt,
                    args=(conv_id, message["id"], slug),
                    daemon=True, name="steer-receipt",
                ).start()
                return
            _log(f"🕐 queued behind the in-flight turn — conversation {conv_id}")
            self._append(conv_id, "system", "queued behind the running turn",
                         kind="queued")
            lock.acquire()   # queue behind the in-flight turn
        started = time.monotonic()
        try:
            conv = store.load_conversation(conv_id)
            if conv is None:   # deleted while this turn waited for the lock
                _log(f"↩ conversation {conv_id} is gone — dropping the turn")
                return
            cwd = str(conv.get("cwd") or Path.home())
            # Re-read the workspace each turn (its model/boot may have been
            # edited); the cwd stays pinned to the conversation either way, so
            # a deleted workspace degrades to daemon defaults, never to a
            # session booting somewhere the operator didn't choose.
            workspace = store.find_workspace(str(conv.get("workspace") or ""))
            model = str((workspace or {}).get("model")
                        or self.config.get("model") or "")
            self._set_status(conv_id, "running")
            entry = self.threads.get(conv_id) or {}
            resume = entry.get("session_id")
            if self.config.get("ack_posts", True):
                where = (workspace or {}).get("name") or Path(cwd).name
                ack = ("⚙️ resuming the session…" if resume else
                       f"⚙️ on it — opening a session in {where}. The first turn "
                       "can take a minute; watch the ticker.")
                self._append(conv_id, "system", ack, kind="ack")
            on_session = lambda sid: self._note_session(conv_id, sid)  # noqa: E731
            on_activity = lambda line: self.bus.publish(               # noqa: E731
                "activity", {"conversation_id": conv_id, "line": line})

            def on_event(obj: dict[str, Any]) -> None:
                if obj.get("type") == "result" and isinstance(obj.get("usage"), dict):
                    self._note_context(conv_id, obj["usage"], model)

            result = self._turn_runner(
                self.config,
                text=text,
                conv_id=conv_id,
                cwd=cwd,
                workspace=workspace,
                resume=resume,
                claude_bin=self.claude_bin,
                on_session=on_session,
                on_activity=on_activity,
                on_event=on_event,
            )
            if resume and not result.ok and not result.session_id:
                # The resume target is gone (pruned elsewhere / another
                # machine) — open a fresh session and say so, don't fake it.
                result = self._turn_runner(
                    self.config, text=text, conv_id=conv_id, cwd=cwd,
                    workspace=workspace, resume=None, claude_bin=self.claude_bin,
                    on_session=on_session, on_activity=on_activity,
                    on_event=on_event,
                )
                if result.ok:
                    result.text = ("previous session was gone — this is a "
                                   "fresh one.\n\n" + result.text)
            with self._threads_guard:
                sid = result.session_id or (
                    (self.threads.get(conv_id) or {}).get("session_id"))
                if sid:
                    self.threads[conv_id] = {
                        "session_id": sid,
                        "status": "idle",
                        "last_turn": time.time(),
                    }
                    store.save_threads(self.threads)
            elapsed = int(time.monotonic() - started)
            if result.ok:
                self._append(conv_id, "assistant", result.text)
                _log(f"✓ turn done in {elapsed}s — {len(result.text)} chars "
                     f"→ conversation {conv_id}")
            else:
                reason = result.detail or "turn failed"
                self._append(conv_id, "system",
                             reason + (f"\n\n{result.text}" if result.text else ""),
                             kind="error")
                _log(f"✗ turn failed in {elapsed}s — {reason}")
        except Exception as exc:   # a broken turn must not kill the daemon
            try:
                self._append(conv_id, "system", f"rail error: {exc}", kind="error")
            except Exception:
                pass
            _log(f"✗ rail error: {exc}")
        finally:
            self._touch_health()
            if store.load_conversation(conv_id) is None:
                self.live.pop(conv_id, None)   # deleted — leave no residue
            else:
                self._set_status(conv_id, "idle")
            lock.release()

    def _watch_receipt(self, conv_id: str, message_id: int, slug: str,
                       *, poll_sec: float = 3.0, max_wait_sec: float = 1800.0) -> None:
        """The second check of the iMessage mechanic: poll the relay until the
        steer task reports delivered (fired inside the session's hooks — it's
        in their system messages now) or cleared (the session ran ``relay
        done``). Either way the operator can expect a reply. Gives up silently
        when the relay is unavailable or the window closes — the mark just
        stays at ✓, which is the honest state."""
        deadline = time.monotonic() + max_wait_sec
        while time.monotonic() < deadline:
            state = turns.relay_task_state(slug)
            if state is None:
                return
            if state in ("delivered", "cleared"):
                self._update_message(conv_id, message_id, {"receipt": "delivered"})
                self.bus.publish("receipt", {"conversation_id": conv_id,
                                             "message_id": message_id,
                                             "receipt": "delivered"})
                _log(f"✓✓ steer {slug} landed in the session")
                return
            time.sleep(poll_sec)

    def _touch_health(self) -> None:
        try:
            (store.home() / "health.json").write_text(
                json.dumps({"last_activity": time.time()}), encoding="utf-8")
        except OSError:
            pass

    # -- read models -------------------------------------------------------

    def state_payload(self) -> dict[str, Any]:
        conversations = store.list_conversations()
        for conv in conversations:
            conv["status"] = self.status_of(conv["id"])
        return {
            "ok": True,
            "conversations": conversations,
            "workspaces": store.load_workspaces(),
            "default_workspace": str(self.config.get("default_workspace") or ""),
            "default_cwd": store.resolve_cwd(None)[0],
            "mode": self.config.get("mode", "approve"),
            "model": self.config.get("model") or "",
            "updates": bool(self.config.get("updates", True)),
            "settings": self._settings_snapshot(),
            "connection": {
                "host": self.config.get("host", "127.0.0.1"),
                "port": self.config.get("port", store.DEFAULT_PORT),
                "hostname": self.config.get("hostname", ""),
                "url": daemon_url(self.config),
            },
            "seq": self.bus.seq,
        }

    def conversation_payload(self, conv_id: str) -> dict[str, Any]:
        conv = store.load_conversation(conv_id)
        if conv is None:
            return {"ok": False, "reason": "unknown conversation"}
        workspace = store.find_workspace(str(conv.get("workspace") or ""))
        return {
            "ok": True,
            "conversation": conv,
            "status": self.status_of(conv["id"]),
            "workspace": workspace,
            "cwd": conv.get("cwd", ""),
            "context": {
                "tokens": conv.get("context_tokens", 0),
                "window": conv.get("context_window", 0),
            },
        }


# ---------------------------------------------------------------------------
# HTTP — stdlib ThreadingHTTPServer, JSON API + SSE + the single-page UI
# ---------------------------------------------------------------------------

def make_handler(rail: ChatRail) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # quiet the default per-request stderr lines; the rail narrates itself
        def log_message(self, format: str, *args: Any) -> None:   # noqa: A002
            pass

        def _json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _bytes(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                return data if isinstance(data, dict) else {}
            except (ValueError, json.JSONDecodeError):
                return {}

        def do_GET(self) -> None:   # noqa: N802 (http.server API)
            path, _, query = self.path.partition("?")
            if path in ("/", "/index.html"):
                self._bytes(_UI_FILE.read_bytes(), "text/html; charset=utf-8")
                return
            if path == "/manifest.json":
                # PWA manifest — "add to home screen" on the phone gives the
                # chat a standalone app frame over tailscale.
                self._json({
                    "name": "Claude Chat",
                    "short_name": "Chat",
                    "start_url": "/",
                    "display": "standalone",
                    "background_color": "#0f1211",
                    "theme_color": "#0f1211",
                    "icons": [{"src": "/icon.svg", "sizes": "any",
                               "type": "image/svg+xml"}],
                })
                return
            if path == "/icon.svg":
                self._bytes((
                    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96">'
                    '<rect width="96" height="96" rx="20" fill="#0f1211"/>'
                    '<rect x="8" y="8" width="80" height="80" rx="14" fill="none" '
                    'stroke="#d97757" stroke-opacity=".35" stroke-width="2"/>'
                    '<text x="48" y="62" text-anchor="middle" font-family="monospace" '
                    'font-size="40" font-weight="bold" fill="#d97757">*</text>'
                    "</svg>"
                ).encode("utf-8"), "image/svg+xml")
                return
            if path == "/api/state":
                self._json(rail.state_payload())
                return
            if path.startswith("/api/conversations/"):
                payload = rail.conversation_payload(path.rsplit("/", 1)[1])
                self._json(payload, 200 if payload.get("ok") else 404)
                return
            if path.startswith("/api/approve/"):
                self._json(rail.approval_state(path.rsplit("/", 1)[1]))
                return
            if path == "/api/events":
                self._serve_events(query)
                return
            self._json({"ok": False, "reason": "not found"}, 404)

        def do_POST(self) -> None:   # noqa: N802 (http.server API)
            path = self.path.partition("?")[0]
            body = self._read_body()
            if path == "/api/messages":
                self._json(rail.post_operator_message(
                    body.get("conversation_id") or None,
                    str(body.get("text", "")),
                    workspace=str(body.get("workspace") or ""),
                    cwd=str(body.get("cwd") or "")))
                return
            if path == "/api/say":
                self._json(rail.say(str(body.get("conversation_id", "")),
                                    str(body.get("text", ""))))
                return
            if path == "/api/settings":
                self._json(rail.update_setting(str(body.get("key", "")),
                                               body.get("value")))
                return
            if path == "/api/workspaces":
                self._json(rail.save_workspace(body))
                return
            if path.startswith("/api/workspaces/") and path.endswith("/delete"):
                self._json(rail.remove_workspace(path.split("/")[3]))
                return
            if path == "/api/approve":
                self._json(rail.create_approval(
                    str(body.get("conversation_id", "")),
                    str(body.get("tool_name", "")),
                    body.get("tool_input") or {},
                    int(body.get("timeout_sec") or 300)))
                return
            if path.startswith("/api/approve/") and path.endswith("/verdict"):
                request_id = path.split("/")[3]
                self._json(rail.set_verdict(request_id, str(body.get("verdict", ""))))
                return
            if path.startswith("/api/conversations/") and path.endswith("/delete"):
                self._json(rail.delete(path.split("/")[3]))
                return
            self._json({"ok": False, "reason": "not found"}, 404)

        def _serve_events(self, query: str) -> None:
            """SSE — replay from ``since``, then follow the bus. One comment
            heartbeat per idle wait keeps proxies and EventSource alive."""
            since = 0
            for part in query.split("&"):
                if part.startswith("since="):
                    try:
                        since = int(part.split("=", 1)[1])
                    except ValueError:
                        pass
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")   # tell a proxy not to buffer us
            self.end_headers()
            try:
                while True:
                    events = rail.bus.wait_since(since, timeout=20.0)
                    if not events:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    for seq, kind, data in events:
                        since = max(since, seq)
                        payload = json.dumps(data)
                        self.wfile.write(
                            f"id: {seq}\nevent: {kind}\ndata: {payload}\n\n"
                            .encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return   # client went away — normal SSE lifecycle

    return Handler


def run_serve() -> int:
    """Foreground daemon — config + UI + API, blocks until killed."""
    config = store.load_config()
    if not store.configured():
        print("not configured — run: claude-chat setup", file=sys.stderr)
        return 1
    claude_bin = turns.find_claude()
    if not claude_bin:
        print("claude binary not found — set CLAUDE_CHAT_CLAUDE_BIN", file=sys.stderr)
        return 1
    if not _UI_FILE.exists():
        print(f"chat UI missing from the package ({_UI_FILE}) — reinstall claude-chat",
              file=sys.stderr)
        return 1

    rail = ChatRail(config, claude_bin=claude_bin)
    host = str(config.get("host", "127.0.0.1"))
    port = int(config.get("port", store.DEFAULT_PORT))
    server = ThreadingHTTPServer((host, port), make_handler(rail))
    server.daemon_threads = True   # hung SSE clients must not block shutdown
    workspaces = store.load_workspaces()
    print(f"claude-chat up — {daemon_url(config)} (bind {host}:{port}), "
          f"mode {config.get('mode')}, {len(workspaces)} workspace(s)")
    if config.get("hostname"):
        print(f"  friendly url: http://{config['hostname']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def local_call(url: str, payload: dict[str, Any] | None = None,
               timeout: float = 10.0) -> dict[str, Any]:
    """One call to the running daemon (the CLI's helpers: say, test). Never
    raises — an unreachable daemon comes back as ``{"ok": False, …}``."""
    try:
        if payload is None:
            req = urllib.request.Request(url, method="GET")
        else:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        return body if isinstance(body, dict) else {"ok": False, "reason": "bad response"}
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": f"daemon unreachable: {exc}"}
