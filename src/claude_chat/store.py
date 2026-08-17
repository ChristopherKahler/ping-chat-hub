"""Operator-level state — config, workspaces, threads, conversations.

Everything lives under ``~/.claude-chat/`` (``CLAUDE_CHAT_HOME`` aware), plain
JSON so the operator can read what the daemon believes:

* ``config.json``      — bind, port, permission mode, model, timeouts.
* ``workspaces.json``  — the saved directories the UI quick-selects from.
* ``threads.json``     — conversation ⇄ claude session map.
* ``conversations/``   — one file per conversation (the message history).

Writes are atomic and 0600: tmp file + ``os.replace``, so a killed daemon can
never leave a half-written conversation behind.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

THREAD_MAX_AGE_DAYS = 30

DEFAULT_PORT = 7788

CONFIG_DEFAULTS: dict[str, Any] = {
    "host": "127.0.0.1",          # `claude-chat host tailscale|all` opens the phone path
    "port": DEFAULT_PORT,
    "mode": "approve",            # "approve" | "skip"
    "model": "",                  # --model for turns; "" = account default
    "default_workspace": "",      # workspace id a new conversation falls back to
    "default_cwd": "",            # directory a new conversation falls back to; "" = ~
    "turn_timeout_sec": 1800,     # long turns are normal — 30m before SIGKILL
    "approve_timeout_sec": 300,   # an unanswered Allow/Deny card → deny
    "full_load": False,           # True drops --strict-mcp-config (the session's own MCP servers load)
    "ack_posts": True,            # instant "on it" reply per turn
    "midflight_relay": True,      # steer a live turn via `base relay task` (needs BASE; degrades to queue)
    "updates": True,              # in-turn proactive `say` posts (the rail protocol)
    "hostname": "",               # friendly hostname, e.g. chat.go ("" = none)
    "link_url": "",               # what browsers should open; "" = the daemon's own url
}

WORKSPACE_DEFAULTS: dict[str, Any] = {
    "id": "",
    "name": "",
    "path": "",
    "model": "",     # "" = the daemon's model
    "mode": "",      # "" = the daemon's permission mode
    "boot": "",      # first-turn prefix, e.g. "/resume-session" ("" = the operator's text, raw)
    "dirs": [],      # extra readable roots — one --add-dir each
}


def home() -> Path:
    override = (os.environ.get("CLAUDE_CHAT_HOME") or "").strip()
    path = Path(override).expanduser() if override else Path.home() / ".claude-chat"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def write_private_json(path: Path, data: Any) -> None:
    """Atomic 0600 write — tmp + replace."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    merged = dict(CONFIG_DEFAULTS)
    data = _read_json(home() / "config.json", {})
    if isinstance(data, dict):
        merged.update(data)
    return merged


def save_config(config: dict[str, Any]) -> Path:
    path = home() / "config.json"
    write_private_json(path, config)
    return path


def configured() -> bool:
    """True once `claude-chat setup` has run — the config file exists on disk
    (not just the defaults this module hands out)."""
    return (home() / "config.json").exists()


# ---------------------------------------------------------------------------
# workspaces — the saved directories the UI quick-selects from
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _ID_RE.sub("-", text.strip().lower()).strip("-") or "workspace"


def load_workspaces() -> list[dict[str, Any]]:
    data = _read_json(home() / "workspaces.json", [])
    if not isinstance(data, list):
        return []
    out = []
    for raw in data:
        if not isinstance(raw, dict) or not raw.get("path"):
            continue
        entry = dict(WORKSPACE_DEFAULTS)
        entry.update(raw)
        entry["id"] = entry["id"] or slugify(Path(entry["path"]).name)
        entry["name"] = entry["name"] or Path(entry["path"]).name
        entry["dirs"] = [str(d) for d in (entry.get("dirs") or [])]
        out.append(entry)
    return out


def save_workspaces(workspaces: list[dict[str, Any]]) -> Path:
    path = home() / "workspaces.json"
    write_private_json(path, workspaces)
    return path


def find_workspace(workspace_id: str) -> dict[str, Any] | None:
    if not workspace_id:
        return None
    return next((w for w in load_workspaces() if w["id"] == workspace_id), None)


def add_workspace(path: Path, *, workspace_id: str = "", name: str = "",
                  model: str = "", mode: str = "", boot: str = "",
                  dirs: list[str] | None = None) -> dict[str, Any]:
    """Register a directory. Re-adding a known id updates it in place — the
    idempotent shape a setup script can run twice."""
    path = path.expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"not a directory: {path}")
    entry = dict(WORKSPACE_DEFAULTS)
    entry.update({
        "id": workspace_id or slugify(path.name),
        "name": name or path.name,
        "path": str(path),
        "model": model,
        "mode": mode,
        "boot": boot,
        "dirs": [str(Path(d).expanduser().resolve()) for d in (dirs or [])],
    })
    workspaces = [w for w in load_workspaces() if w["id"] != entry["id"]]
    workspaces.append(entry)
    workspaces.sort(key=lambda w: w["name"].lower())
    save_workspaces(workspaces)
    return entry


def remove_workspace(workspace_id: str) -> bool:
    workspaces = load_workspaces()
    kept = [w for w in workspaces if w["id"] != workspace_id]
    if len(kept) == len(workspaces):
        return False
    save_workspaces(kept)
    config = load_config()
    if config.get("default_workspace") == workspace_id:
        config["default_workspace"] = ""
        save_config(config)
    return True


def resolve_cwd(workspace: dict[str, Any] | None, cwd: str = "") -> tuple[str, str]:
    """The directory a new conversation boots in, and why — (path, reason).

    Precedence: an explicit path → the named workspace → the default workspace
    → ``default_cwd`` → the operator's home. Validation is the caller's (the
    daemon refuses a path that isn't a directory rather than silently booting
    somewhere else).
    """
    if cwd:
        return str(Path(cwd).expanduser()), "explicit path"
    if workspace:
        return workspace["path"], f"workspace {workspace['id']}"
    config = load_config()
    default = find_workspace(str(config.get("default_workspace") or ""))
    if default:
        return default["path"], f"default workspace {default['id']}"
    if config.get("default_cwd"):
        return str(Path(str(config["default_cwd"])).expanduser()), "default_cwd"
    return str(Path.home()), "home"


# ---------------------------------------------------------------------------
# threads — conversation ⇄ claude session
# ---------------------------------------------------------------------------

def load_threads() -> dict[str, dict[str, Any]]:
    data = _read_json(home() / "threads.json", {})
    return data if isinstance(data, dict) else {}


def save_threads(threads: dict[str, dict[str, Any]]) -> None:
    write_private_json(home() / "threads.json", threads)


def prune_threads(threads: dict[str, dict[str, Any]], *,
                  max_age_days: int = THREAD_MAX_AGE_DAYS,
                  now: float | None = None) -> dict[str, dict[str, Any]]:
    """Drop entries whose last turn is older than *max_age_days*. A pruned
    thread isn't an error later — the daemon opens a fresh session and says so
    in the reply."""
    now = time.time() if now is None else now
    cutoff = now - max_age_days * 86400
    return {cid: entry for cid, entry in threads.items()
            if float(entry.get("last_turn", 0)) >= cutoff}


# ---------------------------------------------------------------------------
# conversations — one JSON file each
# ---------------------------------------------------------------------------

def conversations_dir() -> Path:
    path = home() / "conversations"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def new_conversation_id() -> str:
    return f"c{int(time.time() * 1000):x}{os.urandom(2).hex()}"


def load_conversation(conv_id: str) -> dict[str, Any] | None:
    if not conv_id or "/" in conv_id or "." in conv_id:
        return None   # the id lands in a path — never let it walk out of the dir
    data = _read_json(conversations_dir() / f"{conv_id}.json", None)
    return data if isinstance(data, dict) else None


def save_conversation(conv: dict[str, Any]) -> None:
    write_private_json(conversations_dir() / f"{conv['id']}.json", conv)


def delete_conversation(conv_id: str) -> bool:
    path = conversations_dir() / f"{conv_id}.json"
    if not load_conversation(conv_id):
        return False
    path.unlink(missing_ok=True)
    threads = load_threads()
    if threads.pop(conv_id, None) is not None:
        save_threads(threads)
    return True


def list_conversations() -> list[dict[str, Any]]:
    """Newest-first metadata for the sidebar — never the full message bodies."""
    entries: list[dict[str, Any]] = []
    for path in conversations_dir().glob("c*.json"):
        conv = _read_json(path, None)
        if not isinstance(conv, dict):
            continue
        messages = conv.get("messages", [])
        entries.append({
            "id": conv.get("id", path.stem),
            "title": conv.get("title", ""),
            "created": conv.get("created", 0),
            "last_ts": messages[-1]["ts"] if messages else conv.get("created", 0),
            "message_count": len(messages),
            "workspace": conv.get("workspace", ""),
            "cwd": conv.get("cwd", ""),
        })
    return sorted(entries, key=lambda e: e["last_ts"], reverse=True)
