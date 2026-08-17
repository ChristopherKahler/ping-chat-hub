---
type: doc
status: active
tags: [claude-chat, hostname, hosts-file, reverse-proxy, apache, nginx, caddy, wsl2, sse, setup, runbook]
relatedTo: [claude-chat, tailscale-setup]
---

# Friendly hostname — turn `127.0.0.1:7788` into `http://chat.go`

**The fastest path: hand this file to Claude Code on the machine that runs the daemon** — *"read
docs/HOSTNAME-SETUP.md and set up my claude-chat hostname"*. It also reads as a manual
walkthrough.

## Two halves, two privileges

| Half | What it does | Needs |
|---|---|---|
| **Name** | a hosts-file entry maps `chat.go` to this machine | root (Linux/macOS) — or nothing, on WSL |
| **Port** | a reverse proxy on `:80` routes by hostname, so the url loses its port | root |

Without the proxy the name still works, with the port: `http://chat.go:7788`.

## The one flag that matters

The chat's live ticker is Server-Sent Events. **A proxy that buffers will make every turn look
frozen** — the reply lands only when the turn ends. Each dialect spells it differently, and
`claude-chat hostname install` writes the right one for you:

| Proxy | The unbuffered flag |
|---|---|
| Apache | `ProxyPass / http://127.0.0.1:7788/ flushpackets=on` |
| nginx | `proxy_buffering off;` (plus `proxy_read_timeout 3600s;`) |
| Caddy | `flush_interval -1` |

## Setup

```sh
claude-chat hostname set chat.go            # name it; links now point at http://chat.go
sudo claude-chat hostname install chat.go   # /etc/hosts + the :80 vhost + reload
claude-chat hostname                        # show state, probe the url
```

`install` detects the proxy you already run (Apache, nginx, or Caddy), writes a vhost fenced in
its own marker block, enables it, and reloads. It targets the daemon's **real** bind address — so
if you later run `claude-chat host tailscale`, re-run `install` or the vhost points at the old
one.

Caddy is the exception: it writes `/etc/caddy/claude-chat.caddy` and asks you to add
`import /etc/caddy/claude-chat.caddy` to your Caddyfile. Editing someone's Caddyfile for them is
not this tool's business.

## The WSL2 case (daemon in WSL, browser on Windows)

Windows can't always reach a WSL service through `127.0.0.1` — the localhost relay won't forward
`:80` when a Windows process already holds it. The way through is the **WSL VM's own IP**, which
**changes on every reboot**. So:

```sh
claude-chat hostname sync    # writes the Windows hosts file, no sudo needed
```

It writes `<current-wsl-ip> chat.go` into `C:\Windows\System32\drivers\etc\hosts`, fenced in a
`claude-chat` marker block that a re-run replaces (it never appends a second line, and never
touches anyone else's entries). It no-ops when the IP hasn't moved, which makes it cheap enough
to hang off your shell rc so the mapping heals itself after every reboot:

```sh
# ~/.bashrc — interactive shells only, backgrounded so it never delays the prompt
case $- in
  *i*) [ -x "$HOME/path/to/.venv/bin/claude-chat" ] && \
       ( "$HOME/path/to/.venv/bin/claude-chat" hostname sync >/dev/null 2>&1 & ) ;;
esac
```

## Verify

Check the **body**, not the status code — an Apache with no matching `ServerName` happily returns
`200` from its default site, which looks like success and is not:

```sh
curl -s -H "Host: chat.go" http://127.0.0.1/ | grep -o '<title>[^<]*</title>'
# → <title>Claude Chat</title>          ✓ the vhost is routing
# → <title>Apache2 Ubuntu Default Page  ✗ no vhost — the default site answered
```

Then open `http://chat.go` in the browser. Send a message and watch the ticker move — that proves
SSE is getting through unbuffered, which a status code never will.

## Undo

```sh
sudo claude-chat hostname uninstall    # drops the vhost + the hosts block, reloads the proxy
```
