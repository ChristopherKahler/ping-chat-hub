"""Friendly hostname wiring — turn ``http://127.0.0.1:7788`` into ``http://chat.go``.

Two independent halves, because they need different privileges:

* **name resolution** — a hosts-file entry. On Linux/macOS that's ``/etc/hosts``
  (root). Under WSL2 the browser lives on Windows, so the *Windows* hosts file
  is the one that matters — and it must point at the WSL VM's IP, which changes
  every reboot. That file is writable from WSL without sudo, so
  :func:`sync_hosts` is a cheap idempotent call you can hang off your shell rc
  and forget.
* **the port** — a reverse proxy on :80 routing by hostname. Apache, nginx, and
  Caddy all do it; the config we emit is the same three lines in three dialects.
  The one flag that matters is the **unbuffered** one (Apache
  ``flushpackets=on``, nginx ``proxy_buffering off``, Caddy ``flush_interval
  -1``): without it the proxy sits on the SSE stream and the chat looks frozen.

Every write is fenced in a marker block, so a re-run replaces its own lines and
touches nothing else in the file.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

MARKER = "claude-chat"
WINDOWS_HOSTS = Path("/mnt/c/Windows/System32/drivers/etc/hosts")
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


def valid_hostname(name: str) -> bool:
    return bool(_HOSTNAME_RE.match(name.strip().lower())) and len(name) <= 253


def is_wsl() -> bool:
    if "microsoft" in os.uname().release.lower():
        return True
    return Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists()


def wsl_ip() -> str:
    """This WSL VM's address on the Windows host network. Windows cannot always
    reach a WSL service through 127.0.0.1 (the localhost relay refuses :80 when
    a Windows process already holds it), and this IP moves on every reboot —
    which is exactly why :func:`sync_hosts` exists."""
    out = subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                         timeout=10).stdout
    return out.split()[0] if out.split() else ""


def _fence(names: list[str], ip: str, *, crlf: bool) -> str:
    eol = "\r\n" if crlf else "\n"
    return (f"# >>> {MARKER} (auto — re-run `claude-chat hostname sync` after a reboot) >>>{eol}"
            f"{ip} {' '.join(names)}{eol}"
            f"# <<< {MARKER} <<<{eol}")


def _rewrite(path: Path, names: list[str], ip: str, *, crlf: bool) -> bool:
    """Replace this tool's marker block in *path* — an empty *names* removes
    the block entirely. Everything outside the fence is preserved byte for
    byte. Returns False when the file already says exactly this (the no-op fast
    path that makes this cheap to call from a shell rc)."""
    try:
        current = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        current = ""
    kept: list[str] = []
    inside = False
    for line in current.splitlines(keepends=True):
        if f">>> {MARKER}" in line:
            inside = True
            continue
        if f"<<< {MARKER}" in line:
            inside = False
            continue
        if not inside:
            kept.append(line)
    rebuilt = "".join(kept)
    if names:
        if rebuilt and not rebuilt.endswith(("\n", "\r")):
            rebuilt += "\r\n" if crlf else "\n"
        rebuilt += _fence(names, ip, crlf=crlf)
    if rebuilt == current:
        return False
    path.write_text(rebuilt, encoding="utf-8", newline="")
    return True


def sync_hosts(names: list[str]) -> dict[str, Any]:
    """Point *names* at this machine, in whichever hosts files the browser
    actually reads. No sudo: under WSL that's the Windows file (writable from
    Linux); everywhere else the local /etc/hosts already needs root, so it is
    :func:`install_proxy`'s job and this call reports what's missing instead of
    failing."""
    if not is_wsl():
        return {"ok": True, "changed": False, "note": "not WSL — /etc/hosts is "
                "handled by `sudo claude-chat hostname install`"}
    if not WINDOWS_HOSTS.exists():
        return {"ok": False, "reason": f"{WINDOWS_HOSTS} not found — is /mnt/c mounted?"}
    ip = wsl_ip()
    if not ip:
        return {"ok": False, "reason": "could not read this VM's IP (hostname -I)"}
    try:
        changed = _rewrite(WINDOWS_HOSTS, names, ip, crlf=True)
    except OSError as exc:
        return {"ok": False, "reason": f"cannot write {WINDOWS_HOSTS}: {exc}"}
    return {"ok": True, "changed": changed, "ip": ip, "names": names,
            "file": str(WINDOWS_HOSTS)}


# ---------------------------------------------------------------------------
# the reverse proxy — root's half
# ---------------------------------------------------------------------------

def detect_proxy() -> str:
    """The proxy this machine actually has, preferring one that's already
    running. ``none`` is a normal answer: the name still works, with the port."""
    for name, marker in (("apache2", "/etc/apache2/sites-available"),
                         ("nginx", "/etc/nginx/sites-available"),
                         ("caddy", "/etc/caddy")):
        if Path(marker).is_dir():
            return name
    for binary in ("apache2", "nginx", "caddy"):
        if shutil.which(binary):
            return binary
    return "none"


def vhost_conf(proxy: str, names: list[str], target: str) -> str:
    """The proxy config for *names* → *target*. The unbuffered flag in each
    dialect is load-bearing: SSE is the chat's live ticker."""
    primary, *aliases = names
    if proxy == "apache2":
        alias = f"    ServerAlias {' '.join(aliases)}\n" if aliases else ""
        return (f"# {MARKER} — generated by `claude-chat hostname install`\n"
                f"<VirtualHost *:80>\n"
                f"    ServerName {primary}\n"
                f"{alias}"
                f"    ProxyPreserveHost On\n"
                f"    ProxyPass        / {target}/ flushpackets=on\n"
                f"    ProxyPassReverse / {target}/\n"
                f"</VirtualHost>\n")
    if proxy == "nginx":
        return (f"# {MARKER} — generated by `claude-chat hostname install`\n"
                f"server {{\n"
                f"    listen 80;\n"
                f"    server_name {' '.join(names)};\n"
                f"    location / {{\n"
                f"        proxy_pass {target};\n"
                f"        proxy_http_version 1.1;\n"
                f"        proxy_set_header Host $host;\n"
                f"        proxy_set_header Connection '';\n"
                f"        proxy_buffering off;\n"
                f"        proxy_read_timeout 3600s;\n"
                f"    }}\n"
                f"}}\n")
    if proxy == "caddy":
        return (f"# {MARKER} — import this from your Caddyfile\n"
                f"http://{', http://'.join(names)} {{\n"
                f"    reverse_proxy {target} {{\n"
                f"        flush_interval -1\n"
                f"    }}\n"
                f"}}\n")
    return ""


def _reload(proxy: str) -> tuple[bool, str]:
    cmd = {"apache2": ["systemctl", "reload", "apache2"],
           "nginx": ["systemctl", "reload", "nginx"],
           "caddy": ["systemctl", "reload", "caddy"]}.get(proxy)
    if not cmd:
        return False, f"no reload command for {proxy}"
    if not shutil.which("systemctl"):
        cmd = {"apache2": ["apache2ctl", "graceful"],
               "nginx": ["nginx", "-s", "reload"],
               "caddy": ["caddy", "reload"]}[proxy]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def install_proxy(names: list[str], target: str, *,
                  proxy: str = "") -> dict[str, Any]:
    """Root's half: the local hosts entry plus the :80 vhost. Requires root and
    says so rather than half-doing it."""
    if os.geteuid() != 0:
        return {"ok": False, "reason": "needs root — run: sudo claude-chat "
                                       f"hostname install {names[0]}"}
    proxy = proxy or detect_proxy()
    steps: list[str] = []

    try:
        if _rewrite(Path("/etc/hosts"), names, "127.0.0.1", crlf=False):
            steps.append("/etc/hosts updated")
    except OSError as exc:
        return {"ok": False, "reason": f"cannot write /etc/hosts: {exc}"}

    if proxy == "none":
        return {"ok": True, "proxy": "none", "steps": steps,
                "note": "no reverse proxy found — the names work, with the port "
                        f"({target}). Install apache2, nginx, or caddy for the "
                        "portless url."}

    conf = vhost_conf(proxy, names, target)
    if proxy == "caddy":
        path = Path("/etc/caddy") / f"{MARKER}.caddy"
        path.write_text(conf, encoding="utf-8")
        return {"ok": True, "proxy": proxy, "steps": steps, "config": str(path),
                "note": f"add `import {path}` to /etc/caddy/Caddyfile, then "
                        "`sudo systemctl reload caddy` — we don't edit your "
                        "Caddyfile for you"}

    available = Path(f"/etc/{proxy}/sites-available") / f"{MARKER}.conf"
    enabled = Path(f"/etc/{proxy}/sites-enabled") / f"{MARKER}.conf"
    available.parent.mkdir(parents=True, exist_ok=True)
    available.write_text(conf, encoding="utf-8")
    steps.append(f"{available} written")
    enabled.parent.mkdir(parents=True, exist_ok=True)
    if enabled.is_symlink() or enabled.exists():
        enabled.unlink()
    enabled.symlink_to(available)
    steps.append(f"{enabled} enabled")

    if proxy == "apache2":
        # proxy + proxy_http are what ProxyPass rides on; a2enmod is a no-op
        # when they're already on.
        subprocess.run(["a2enmod", "proxy", "proxy_http"],
                       capture_output=True, text=True, timeout=30)

    ok, out = _reload(proxy)
    steps.append(f"{proxy} reloaded" if ok else f"{proxy} reload FAILED: {out}")
    return {"ok": ok, "proxy": proxy, "steps": steps, "config": str(available),
            "url": f"http://{names[0]}"}


def uninstall_proxy(proxy: str = "") -> dict[str, Any]:
    """Undo :func:`install_proxy` — drop the vhost and our hosts block."""
    if os.geteuid() != 0:
        return {"ok": False, "reason": "needs root — run: sudo claude-chat "
                                       "hostname uninstall"}
    proxy = proxy or detect_proxy()
    removed: list[str] = []
    for path in (Path(f"/etc/{proxy}/sites-enabled") / f"{MARKER}.conf",
                 Path(f"/etc/{proxy}/sites-available") / f"{MARKER}.conf",
                 Path("/etc/caddy") / f"{MARKER}.caddy"):
        if path.is_symlink() or path.exists():
            path.unlink()
            removed.append(str(path))
    if _rewrite(Path("/etc/hosts"), [], "", crlf=False):
        removed.append("/etc/hosts entry")
    if proxy != "none":
        _reload(proxy)
    return {"ok": True, "removed": removed}


def probe(url: str, timeout: float = 5.0) -> int:
    """HTTP status the friendly url answers with — 0 when it doesn't answer."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0
