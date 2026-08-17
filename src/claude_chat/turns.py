"""The turn core — everything the rail needs that isn't its transport.

Every turn does the same four things: compose a prompt, spawn ``claude
--print`` in the conversation's directory, tap the stream-json output as it
flows, and steer a live turn when a reply lands mid-flight. That machinery
lives here; :mod:`claude_chat.daemon` owns only the HTTP + SSE surface.

Nothing in this module knows a transport — surface text (the rail's name, its
``say`` command) arrives as parameters.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

APPROVE_TOOL = "mcp__claude-chat__approve"

_SCOPE_RE = re.compile(r"^@([A-Za-z0-9][A-Za-z0-9_-]*)\s+(.+)$", re.DOTALL)


# ---------------------------------------------------------------------------
# Binary resolution (a systemd unit carries a bare PATH — resolve explicitly)
# ---------------------------------------------------------------------------

def find_claude() -> str | None:
    """``CLAUDE_CHAT_CLAUDE_BIN`` → PATH → ``~/.local/bin`` → newest nvm node."""
    env_bin = os.environ.get("CLAUDE_CHAT_CLAUDE_BIN")
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for cand in (home / ".local" / "bin" / "claude",
                 *sorted(home.glob(".nvm/versions/node/*/bin/claude"), reverse=True)):
        if cand.exists():
            return str(cand)
    return None


def find_base() -> str | None:
    """The BASE cli, if the operator has one — mid-flight steering rides it.
    None is a normal answer: without BASE a mid-turn reply queues instead."""
    found = shutil.which("base")
    if found:
        return found
    cand = Path.home() / ".local" / "bin" / "base"
    return str(cand) if cand.exists() else None


# ---------------------------------------------------------------------------
# Turn composition — prompt, argv, stream parse
# ---------------------------------------------------------------------------

def parse_scope(text: str) -> tuple[str, str]:
    """``@toolbox fix the tests`` → ("toolbox", "fix the tests").

    Scope only means something on a fresh conversation, where it names the
    workspace to boot in. The daemon resolves it against the registry and
    ignores it when nothing matches (so an ``@name`` that happens to open a
    message is not swallowed).
    """
    match = _SCOPE_RE.match(text.strip())
    if match:
        return match.group(1), match.group(2).strip()
    return "", text.strip()


def rail_protocol(*, surface: str, say_cmd: str) -> str:
    """The in-turn emission protocol, appended to every fresh turn.

    Over a rail the session's narration goes nowhere — without explicit
    emissions a twenty-minute turn is indistinguishable from a dead daemon.
    Event-driven, not time-driven, so it informs instead of paging.
    """
    return (
        "---\n"
        f"{surface} rail protocol — you are talking to the operator over "
        f"{surface}, not a terminal. Your normal output reaches them ONLY when "
        f"this turn ends. Your mid-turn voice is: {say_cmd} \"<message>\" "
        "(conversation routing is already in your env). Emit:\n"
        "1. On accepting this turn: one line — what you're about to do.\n"
        "2. The moment a finding changes the plan or direction.\n"
        "3. Before anything long or expensive.\n"
        "4. If ~5 minutes pass with nothing emitted: one still-working line "
        "with where you are.\n"
        "Your final message posts to the conversation automatically — do NOT "
        "`say` it too."
    )


def compose_prompt(
    text: str,
    *,
    resumed: bool,
    updates: bool = True,
    boot: str = "",
    surface: str,
    say_cmd: str,
) -> str:
    """The prompt for one turn. A resumed session gets the operator's words
    verbatim; a fresh one gets the workspace's *boot* prefix (a slash command,
    a standing brief, or nothing at all) and the rail protocol on top."""
    text = text.strip()
    if resumed:
        return text
    prompt = text
    if boot.strip():
        prompt = f"{boot.strip()}\n\n{text}" if text else boot.strip()
    if updates:   # quiet operators get one answer per turn, nothing between
        prompt += "\n\n" + rail_protocol(surface=surface, say_cmd=say_cmd)
    return prompt


def build_cmd(
    claude_bin: str,
    *,
    mode: str,
    prompt: str,
    resume: str | None = None,
    mcp_config: str | None = None,
    full_load: bool = False,
    model: str | None = None,
    add_dirs: list[str] | None = None,
) -> list[str]:
    """The exact argv for one turn."""
    cmd = [claude_bin, "--print", "--output-format", "stream-json", "--verbose"]
    if not full_load:
        cmd.append("--strict-mcp-config")
    if mode == "skip":
        cmd.append("--dangerously-skip-permissions")
    else:
        cmd += ["--permission-prompt-tool", APPROVE_TOOL]
    if mcp_config:
        cmd += ["--mcp-config", mcp_config]
    if model:
        cmd += ["--model", model]
    for extra in add_dirs or []:
        cmd += ["--add-dir", extra]
    if resume:
        cmd += ["--resume", resume]
    cmd += ["-p", prompt]
    return cmd


def parse_stream(stdout: str) -> tuple[str | None, str, bool]:
    """(session_id, final_text, is_error) from a stream-json transcript.

    The session id is taken from any event carrying one (init races exist); the
    result event is authoritative for text and error state. No result event at
    all = error.
    """
    session_id: str | None = None
    text = ""
    is_error = True
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            sid = obj.get("session_id")
            if sid:
                session_id = str(sid)
            if obj.get("type") == "result":
                subtype = obj.get("subtype", "")
                is_error = bool(obj.get("is_error", subtype != "success"))
                text = str(obj.get("result") or "")
                if not text and is_error:
                    text = f"turn ended without a result ({subtype or 'unknown'})"
    return session_id, text, is_error


def activity_line(event: dict[str, Any]) -> str | None:
    """One human line for a stream-json event, or None if it isn't telemetry
    worth showing. Deliberately terse — this feeds a live ticker, not a log."""
    if event.get("type") != "assistant":
        return None
    content = ((event.get("message") or {}).get("content")) or []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            return f"⚙ {block.get('name', 'tool')}"
        if block.get("type") == "text":
            text = " ".join(str(block.get("text", "")).split())
            if text:
                return text[:160]
    return None


def context_window_for(model: str) -> int:
    """The token window the configured model runs with — ``[1m]`` models get
    the 1M window, everything else the standard 200k."""
    return 1_000_000 if "[1m]" in (model or "") else 200_000


@dataclasses.dataclass
class TurnResult:
    ok: bool
    session_id: str | None
    text: str
    detail: str = ""


def spawn_turn(
    cmd: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout_sec: int,
    on_session: Callable[[str], None] | None = None,
    on_activity: Callable[[str], None] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> TurnResult:
    """One turn's process: spawn → stream-tap → parse. Pure runner — the caller
    composed the argv and env; nothing here knows a transport.

    *on_session* fires the moment the child announces its session id (the init
    event, seconds in) — the daemon records it immediately so a reply arriving
    MID-turn can be steered into the live session instead of waiting out a long
    turn. *on_activity* fires with one terse line per tap-worthy event (see
    :func:`activity_line`) — the live ticker. *on_event* fires with EVERY
    parsed stream object, so the daemon can derive its own telemetry (context
    usage) without this module knowing what it looks for. A callback that
    raises must not kill the tap, so failures are swallowed here.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
        )
    except OSError as exc:
        return TurnResult(False, None, "", f"claude failed to exec: {exc}")

    stderr_box: list[str] = []
    drain = threading.Thread(
        target=lambda: stderr_box.append(proc.stderr.read() if proc.stderr else ""),
        daemon=True,
    )
    drain.start()
    killed = threading.Event()

    def _timeout_kill() -> None:
        killed.set()
        proc.kill()

    watchdog = threading.Timer(timeout_sec, _timeout_kill)
    watchdog.start()

    lines: list[str] = []
    announced = False
    try:
        for line in proc.stdout or []:
            lines.append(line)
            try:
                obj = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if not announced and on_session is not None:
                sid = obj.get("session_id")
                if sid:
                    announced = True
                    on_session(str(sid))
            if on_activity is not None:
                note = activity_line(obj)
                if note:
                    on_activity(note)
            if on_event is not None:
                try:
                    on_event(obj)
                except Exception:
                    pass   # telemetry must never kill the tap
        proc.wait()
    finally:
        watchdog.cancel()
        drain.join(timeout=2)

    session_id, result_text, is_error = parse_stream("".join(lines))
    if killed.is_set():
        return TurnResult(False, session_id, "", "turn timed out")
    if proc.returncode != 0 and not result_text:
        stderr = (stderr_box[0] if stderr_box else "").strip()
        stderr_tail = stderr.splitlines()[-1:] or ["no stderr"]
        return TurnResult(False, session_id, "", f"exit {proc.returncode}: {stderr_tail[0]}")
    return TurnResult(not is_error, session_id, result_text,
                      "" if not is_error else "session reported an error")


# ---------------------------------------------------------------------------
# Mid-flight steering — optional, rides BASE's relay when it's installed
# ---------------------------------------------------------------------------

def relay_register(session_id: str, title: str) -> bool:
    """Bind *session_id* to a stable relay *title* (re-binding is safe).

    Rail-spawned ``--print`` sessions never self-register — no operator ran
    ``base relay register`` inside them — so :func:`relay_steer`'s title
    lookup always missed and every mid-flight message silently fell back to
    queueing. The daemon holds both halves (the announced session id and its
    own stable name for the conversation), so it binds them the moment the
    session announces itself; the session's own hook activity keeps the
    binding live for the rest of the turn. Best-effort: no BASE, no bind —
    the queue fallback stays honest."""
    base = find_base()
    if not base:
        return False
    try:
        done = subprocess.run(
            [base, "relay", "register", "--as", title, "--session", session_id],
            capture_output=True, text=True, timeout=10,
        )
        return done.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def relay_steer(
    session_id: str,
    text: str,
    *,
    from_name: str,
    slug_prefix: str,
    say_cmd: str,
) -> str | None:
    """Deliver *text* into a LIVE session via ``base relay task``.

    A ``--print`` session can't be resumed while its process is alive — but a
    relay task fires inside its hooks on its next tool call, mid-run. Task, not
    ping, on purpose: the receiver clears a task itself (``base relay done
    <slug>``), while a ping only clears on a reply to the sender — and the
    daemon is not a registered session, so a ping's alert would re-fire
    forever. The message carries the reply path: the session posts into its own
    conversation via *say_cmd* (its env already holds the routing).

    Returns the task slug on success (the caller watches its delivery state —
    see :func:`relay_task_state`), None on any failure. Best-effort: BASE is
    optional, and None means "fall back to queueing".
    """
    base = find_base()
    if not base:
        return None
    try:
        listing = subprocess.run(
            [base, "relay", "sessions"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        title = next(
            (line.split()[0] for line in listing.splitlines()
             if f"session:{session_id}" in line and "[live" in line),
            None,
        )
        if not title:
            return None
        slug = f"{slug_prefix}-{int(time.time())}"
        summary = (
            f"OPERATOR (mid-turn, via the {from_name}): {text} "
            f"— ACT ON THIS NOW within your running turn. Reply directly into "
            f"your conversation: {say_cmd} \"<your reply>\" (routing is already "
            f"in your env). Then clear this alert: {base} relay done {slug}"
        )
        sent = subprocess.run(
            [base, "relay", "task", "--to", title, "--from", from_name,
             "--slug", slug, "--summary", summary],
            capture_output=True, text=True, timeout=10,
        )
        return slug if sent.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def relay_task_state(slug: str) -> str | None:
    """Delivery state of a steer task: ``pending`` (in the inbox, not yet
    fired), ``delivered`` (fired inside the session's hooks — it's in their
    system messages now), ``cleared`` (the session ran ``relay done`` on it),
    or None when the relay itself is unavailable. This is what turns a steer
    into an iMessage-style receipt: sent ✓, landed ✓✓."""
    base = find_base()
    if not base:
        return None
    try:
        listing = subprocess.run(
            [base, "relay", "tasks"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in listing.splitlines():
        if slug in line:
            return "delivered" if "[delivered" in line else "pending"
    return "cleared"
