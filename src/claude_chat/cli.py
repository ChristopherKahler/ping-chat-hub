"""``claude-chat`` — the operator surface.

End to end, no external app and no tokens::

    claude-chat setup                    # port + permission mode — 20 seconds
    claude-chat workspace add ~/dev      # a directory you can quick-select
    claude-chat serve                    # run the daemon in the foreground
    claude-chat open                     # print (and try to launch) the UI url
    claude-chat enable                   # background service, restart on failure
    claude-chat hostname set chat.go     # the friendly url
    claude-chat status                   # service state + config + counts
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from claude_chat import hosts, service, store
from claude_chat.daemon import daemon_url, local_call
from claude_chat.turns import find_claude


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2))


def _sched(unit_dir: Path | None = None):
    return service.resolve_scheduler(unit_dir)


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------

def run_setup() -> int:
    print("claude-chat setup — Claude Code sessions, in a browser, in any directory.\n")
    config = store.load_config()

    default_port = int(config.get("port", store.DEFAULT_PORT))
    raw = input(f"Port [{default_port}]: ").strip()
    if raw:
        try:
            port = int(raw)
            if not 1024 <= port <= 65535:
                raise ValueError
        except ValueError:
            print("port must be a number between 1024 and 65535")
            return 1
    else:
        port = default_port

    mode = ""
    while mode not in ("approve", "skip"):
        mode = (input("Permission mode — approve (Allow/Deny card per action, "
                      "default) or skip (full trust)? [approve/skip] ")
                .strip().lower() or "approve")

    config.update({
        "host": "127.0.0.1",   # `claude-chat host tailscale` opens the phone path
        "port": port,
        "mode": mode,
    })
    path = store.save_config(config)
    print(f"✓ config written — {path}")
    print(f"\nNext:\n"
          "  claude-chat workspace add <path>   # a directory to quick-select\n"
          "  claude-chat serve                  # run it (foreground)\n"
          "  claude-chat open                   # open the UI\n"
          "  claude-chat enable                 # run it as a service\n"
          "  claude-chat hostname set chat.go   # friendly url (see docs/HOSTNAME-SETUP.md)\n"
          "  claude-chat host tailscale         # reach it from your phone\n"
          f"UI: http://127.0.0.1:{port} — pick a workspace (or any path) and talk.")
    return 0


# ---------------------------------------------------------------------------
# workspaces — the saved directories the UI quick-selects from
# ---------------------------------------------------------------------------

def run_workspace_list() -> int:
    workspaces = store.load_workspaces()
    default = store.load_config().get("default_workspace", "")
    if not workspaces:
        _emit({"ok": True, "workspaces": [],
               "hint": "add one: claude-chat workspace add <path>"})
        return 0
    _emit({"ok": True, "default": default, "workspaces": [
        {**w, "default": w["id"] == default, "exists": Path(w["path"]).is_dir()}
        for w in workspaces]})
    return 0


def run_workspace_add(path: Path, *, workspace_id: str, name: str, model: str,
                      mode: str, boot: str, dirs: list[str],
                      make_default: bool) -> int:
    if mode and mode not in ("approve", "skip"):
        _emit({"ok": False, "reason": "--mode must be approve or skip"})
        return 1
    try:
        entry = store.add_workspace(path, workspace_id=workspace_id, name=name,
                                    model=model, mode=mode, boot=boot, dirs=dirs)
    except ValueError as exc:
        _emit({"ok": False, "reason": str(exc)})
        return 1
    if make_default:
        config = store.load_config()
        config["default_workspace"] = entry["id"]
        store.save_config(config)
    _emit({"ok": True, "workspace": entry, "default": make_default})
    return 0


def run_workspace_remove(workspace_id: str) -> int:
    if not store.remove_workspace(workspace_id):
        _emit({"ok": False, "reason": f"unknown workspace {workspace_id!r}"})
        return 1
    _emit({"ok": True, "removed": workspace_id,
           "note": "conversations already open in it keep their directory"})
    return 0


def run_workspace_default(workspace_id: str | None) -> int:
    config = store.load_config()
    if workspace_id is None:
        _emit({"ok": True, "default_workspace": config.get("default_workspace", "")})
        return 0
    if workspace_id and not store.find_workspace(workspace_id):
        _emit({"ok": False, "reason": f"unknown workspace {workspace_id!r}"})
        return 1
    config["default_workspace"] = workspace_id
    store.save_config(config)
    _emit({"ok": True, "default_workspace": workspace_id})
    return 0


_TOML_PATH_RE = re.compile(r'^\s*path\s*=\s*"([^"]+)"')


def run_workspace_import(source: Path) -> int:
    """Seed the registry from a TOML file that lists directories in
    ``[[workspace]] path = "…"`` blocks (BASE's registry is one such file).
    Directories that no longer exist are skipped, not registered — a stale
    entry in someone else's file is not this tool's problem to carry."""
    source = source.expanduser()
    if not source.is_file():
        _emit({"ok": False, "reason": f"{source} not found"})
        return 1
    added, skipped = [], []
    in_block = False
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("[["):
            in_block = stripped == "[[workspace]]"
            continue
        if stripped.startswith("["):
            in_block = False
            continue
        match = _TOML_PATH_RE.match(line) if in_block else None
        if not match:
            continue
        path = Path(match.group(1)).expanduser()
        if not path.is_dir():
            skipped.append(str(path))
            continue
        added.append(store.add_workspace(path)["id"])
    _emit({"ok": True, "source": str(source), "added": added, "skipped": skipped,
           "hint": "claude-chat workspace list"})
    return 0


# ---------------------------------------------------------------------------
# serve / open / test / say
# ---------------------------------------------------------------------------

def run_serve() -> int:
    from claude_chat.daemon import run_serve as serve
    return serve()


def run_open() -> int:
    """Print the UI url and best-effort launch a browser (WSL-aware)."""
    config = store.load_config()
    url = str(config.get("link_url") or "") or daemon_url(config)
    state = local_call(f"{daemon_url(config)}/api/state")
    print(url)
    if not state.get("ok"):
        print("(daemon not responding — start it: claude-chat serve, "
              "or claude-chat enable)", file=sys.stderr)
    for launcher in (["wslview", url],
                     ["cmd.exe", "/c", "start", url.replace("&", "^&")],
                     ["xdg-open", url]):
        try:
            subprocess.run(launcher, capture_output=True, timeout=10)
            break
        except (OSError, subprocess.TimeoutExpired):
            continue
    return 0


def run_test() -> int:
    if not store.configured():
        _emit({"ok": False, "reason": "not configured — run: claude-chat setup"})
        return 1
    config = store.load_config()
    state = local_call(f"{daemon_url(config)}/api/state")
    if not state.get("ok"):
        _emit({"ok": False, "reason": state.get("reason", "daemon unreachable"),
               "hint": "start it: claude-chat serve"})
        return 1
    _emit({"ok": True, "url": daemon_url(config),
           "conversations": len(state.get("conversations", [])),
           "workspaces": len(state.get("workspaces", [])),
           "mode": state.get("mode"), "claude_bin": find_claude()})
    return 0


def run_say(text: str, conversation: str | None) -> int:
    """Post *text* into a conversation — the voice a running session uses to
    answer mid-turn (its spawn env carries the routing)."""
    url = os.environ.get("CLAUDE_CHAT_URL") or daemon_url(store.load_config())
    conv_id = conversation or os.environ.get("CLAUDE_CHAT_CONVERSATION")
    if not conv_id:
        _emit({"ok": False, "reason": "no conversation — pass --conversation "
                                      "or set CLAUDE_CHAT_CONVERSATION"})
        return 1
    result = local_call(f"{url}/api/say",
                        {"conversation_id": conv_id, "text": text})
    _emit(result)
    return 0 if result.get("ok") else 1


# ---------------------------------------------------------------------------
# enable / disable / status
# ---------------------------------------------------------------------------

def run_enable(*, unit_dir: Path | None = None) -> int:
    if not store.configured():
        _emit({"ok": False, "reason": "not configured — run: claude-chat setup"})
        return 1
    config = store.load_config()
    claude_bin = find_claude()
    if not claude_bin:
        _emit({"ok": False, "reason": "claude binary not found — set CLAUDE_CHAT_CLAUDE_BIN"})
        return 1
    sched = _sched(unit_dir)
    try:
        installed = sched.install_service(
            description="claude-chat — Claude Code sessions in a browser",
            workdir=Path.home(),
            env={"CLAUDE_CHAT_CLAUDE_BIN": claude_bin},
            argv=[sys.executable, "-m", "claude_chat", "serve"],
        )
    except service.SchedulerError as exc:
        _emit({"ok": False, "reason": str(exc)})
        return 1
    _emit({"ok": True, "unit": installed.get("unit", service.UNIT_NAME),
           "scheduler": sched.name, "unit_dir": installed.get("unit_dir", ""),
           "url": daemon_url(config), "claude_bin": claude_bin,
           "mode": config.get("mode")})
    return 0


def run_disable(*, unit_dir: Path | None = None) -> int:
    sched = _sched(unit_dir)
    if not sched.status().get("installed"):
        _emit({"ok": False, "reason": "the service is not installed"})
        return 1
    out = sched.remove()
    _emit({"ok": True, "removed": out.get("removed") or service.UNIT_NAME})
    return 0


def _restart_if_active(result: dict[str, Any]) -> None:
    """Config edits apply at daemon start — bounce the service when it runs."""
    sched = _sched()
    if sched.status().get("state") == "active":
        ok, out = sched.restart()
        result["service"] = "restarted" if ok else f"restart failed: {out}"
    else:
        result["note"] = ("takes effect next daemon start — restart your "
                          "foreground `claude-chat serve` if one is running")


def apply_setting(key: str, value: Any) -> dict[str, Any]:
    """The write seam — mutate ONE option and bounce the service if it's
    running. Settable: mode (approve|skip), model (str, '' = account default),
    port (int), host (str), default_cwd (str), link_url (str), and the
    updates / ack_posts / midflight_relay / full_load booleans."""
    if not store.configured():
        return {"ok": False, "reason": "not configured — run: claude-chat setup"}
    config = store.load_config()
    if key == "mode":
        if value not in ("approve", "skip"):
            return {"ok": False, "reason": "mode must be approve or skip"}
        config["mode"] = value
    elif key == "model":
        config["model"] = str(value or "").strip()
    elif key == "port":
        try:
            config["port"] = int(value)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "port must be a number"}
    elif key in ("host", "link_url", "default_cwd", "hostname"):
        config[key] = str(value or "").strip()
    elif key in ("updates", "ack_posts", "midflight_relay", "full_load"):
        config[key] = bool(value)
    else:
        return {"ok": False, "reason": f"unknown setting {key!r}"}
    store.save_config(config)
    result: dict[str, Any] = {"ok": True, key: config[key]}
    _restart_if_active(result)
    return result


def run_mode(mode: str | None) -> int:
    if mode is None:
        _emit({"ok": True, "mode": store.load_config().get("mode", "approve")})
        return 0
    result = apply_setting("mode", mode)
    _emit(result)
    return 0 if result.get("ok") else 1


def run_model(model: str | None) -> int:
    if model is None:
        _emit({"ok": True,
               "model": store.load_config().get("model") or "(account default)"})
        return 0
    value = "" if model.strip().lower() == "default" else model.strip()
    result = apply_setting("model", value)
    if result.get("ok"):
        result["model"] = result["model"] or "(account default)"
    _emit(result)
    return 0 if result.get("ok") else 1


def run_updates(value: str | None) -> int:
    if value is None:
        _emit({"ok": True,
               "updates": "on" if store.load_config().get("updates", True) else "off"})
        return 0
    result = apply_setting("updates", value == "on")
    if result.get("ok"):
        result["note_scope"] = ("applies to new conversations — existing "
                                "sessions keep the rules they were born with")
    _emit(result)
    return 0 if result.get("ok") else 1


def _tailscale_ip() -> str | None:
    """This machine's tailscale IPv4 — `tailscale ip` first, then a CGNAT
    (100.64/10) interface scan for setups where the CLI isn't on PATH."""
    try:
        proc = subprocess.run(["tailscale", "ip", "-4"],
                              capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            for line in proc.stdout.strip().splitlines():
                if line.strip():
                    return line.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        proc = subprocess.run(["ip", "-4", "addr"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(
        r"inet (100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+)", proc.stdout)
    return match.group(1) if match else None


def run_host(value: str | None) -> int:
    """Show or set the bind address. ``local`` = 127.0.0.1 (default),
    ``tailscale`` = this machine's tailnet address (the phone path — the
    tailnet's device auth and encryption is the boundary), ``all`` = 0.0.0.0
    (both the local proxy and the phone reach it; your firewall is then the
    only thing between this daemon and your LAN). A raw IPv4 is accepted for
    unusual setups. A public interface is on the operator."""
    config = store.load_config()
    if value is None:
        _emit({"ok": True, "host": config.get("host", "127.0.0.1"),
               "url": daemon_url(config)})
        return 0
    value = value.strip().lower()
    if value == "local":
        host = "127.0.0.1"
    elif value == "all":
        host = "0.0.0.0"
    elif value == "tailscale":
        host = _tailscale_ip() or ""
        if not host:
            _emit({"ok": False,
                   "reason": "no tailscale interface found — is tailscaled up? "
                             "(pass the 100.x address directly if you know it)"})
            return 1
    elif re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", value):
        host = value
    else:
        _emit({"ok": False, "reason": "pass local, tailscale, all, or an IPv4 address"})
        return 1
    result = apply_setting("host", host)
    if result.get("ok") and host != "127.0.0.1":
        result["url"] = daemon_url(store.load_config())
        result["warning"] = (
            f"every device that can reach {host}:{store.load_config().get('port')} "
            "can run code in every registered workspace — keep this on the "
            "tailnet, never a public interface")
        if config.get("hostname"):
            result["hostname_note"] = (
                f"re-run `sudo claude-chat hostname install {config['hostname']}` "
                "— the vhost points at the old bind address")
    _emit(result)
    return 0 if result.get("ok") else 1


def status_payload() -> dict[str, Any]:
    """The daemon's state as one dict — shared by `claude-chat status` and any
    dashboard that wants to render it (import this, don't re-derive)."""
    config = store.load_config()
    state = _sched().status().get("state", "unknown")
    entry: dict[str, Any] = {
        "ok": True,
        "service": state,
        "configured": store.configured(),
        "url": daemon_url(config),
        "link_url": str(config.get("link_url") or "") or daemon_url(config),
        "hostname": config.get("hostname", ""),
        "host": config.get("host", ""),
        "mode": config.get("mode", ""),
        "model": config.get("model") or "",
        "ack_posts": bool(config.get("ack_posts", True)),
        "updates": bool(config.get("updates", True)),
        "workspaces": len(store.load_workspaces()),
        "default_workspace": config.get("default_workspace", ""),
        "conversations": len(store.list_conversations()),
        "home": str(store.home()),
    }
    health = store.home() / "health.json"
    if health.exists():
        try:
            last = json.loads(health.read_text()).get("last_activity")
            entry["last_activity_age_sec"] = int(time.time() - float(last))
        except (OSError, ValueError, TypeError):
            pass
    return entry


def run_status() -> int:
    _emit(status_payload())
    return 0


# ---------------------------------------------------------------------------
# hostname — the friendly url (chat.go)
# ---------------------------------------------------------------------------

def _proxy_target(config: dict[str, Any]) -> str:
    """What the vhost proxies to — the daemon's real bind, not an assumption.
    A wildcard bind is reachable at 127.0.0.1; a tailnet bind is not."""
    host = str(config.get("host") or "127.0.0.1")
    if host == "0.0.0.0":
        host = "127.0.0.1"
    return f"http://{host}:{config.get('port', store.DEFAULT_PORT)}"


def run_hostname_show() -> int:
    config = store.load_config()
    name = str(config.get("hostname") or "")
    payload: dict[str, Any] = {
        "ok": True,
        "hostname": name or "(none)",
        "proxy": hosts.detect_proxy(),
        "target": _proxy_target(config),
        "wsl": hosts.is_wsl(),
    }
    if name:
        payload["url"] = f"http://{name}"
        payload["http_status"] = hosts.probe(f"http://{name}/api/state")
        payload["healthy"] = payload["http_status"] == 200
    if hosts.is_wsl():
        payload["wsl_ip"] = hosts.wsl_ip()
    _emit(payload)
    return 0


def run_hostname_set(name: str) -> int:
    name = name.strip().lower()
    if not hosts.valid_hostname(name):
        _emit({"ok": False, "reason": f"{name!r} is not a valid hostname"})
        return 1
    config = store.load_config()
    config["hostname"] = name
    config["link_url"] = f"http://{name}"
    store.save_config(config)
    result: dict[str, Any] = {"ok": True, "hostname": name, "url": f"http://{name}"}
    result["next"] = [
        f"sudo claude-chat hostname install {name}   # hosts entry + :80 vhost",
        "claude-chat hostname sync                   # (WSL) point Windows at this VM",
    ]
    _restart_if_active(result)
    _emit(result)
    return 0


def run_hostname_sync() -> int:
    config = store.load_config()
    name = str(config.get("hostname") or "")
    if not name:
        _emit({"ok": False, "reason": "no hostname set — claude-chat hostname set chat.go"})
        return 1
    result = hosts.sync_hosts([name])
    _emit(result)
    return 0 if result.get("ok") else 1


def run_hostname_install(name: str | None, proxy: str) -> int:
    config = store.load_config()
    name = (name or str(config.get("hostname") or "")).strip().lower()
    if not name:
        _emit({"ok": False, "reason": "no hostname — pass one or run: "
                                      "claude-chat hostname set chat.go"})
        return 1
    if not hosts.valid_hostname(name):
        _emit({"ok": False, "reason": f"{name!r} is not a valid hostname"})
        return 1
    result = hosts.install_proxy([name], _proxy_target(config), proxy=proxy)
    if result.get("ok") and config.get("hostname") != name:
        config["hostname"] = name
        config["link_url"] = f"http://{name}"
        store.save_config(config)
    if result.get("ok") and hosts.is_wsl():
        result["wsl"] = ("run `claude-chat hostname sync` as your normal user "
                         "(not root) to point Windows at this VM")
    _emit(result)
    return 0 if result.get("ok") else 1


def run_hostname_uninstall(proxy: str) -> int:
    result = hosts.uninstall_proxy(proxy)
    if result.get("ok"):
        config = store.load_config()
        config["hostname"] = ""
        config["link_url"] = ""
        store.save_config(config)
    _emit(result)
    return 0 if result.get("ok") else 1
