---
type: doc
status: active
tags: [claude-chat, tailscale, tailnet, phone, pwa, remote-access, security, setup, runbook]
relatedTo: [claude-chat, hostname-setup]
---

# The phone path — reach the chat over tailscale

**The fastest path: hand this file to Claude Code on the machine that runs the daemon** — *"read
docs/TAILSCALE-SETUP.md and set up my phone access"*.

## The security shape first

The daemon binds `127.0.0.1` by default. That is not a token check — it is a **network**
boundary: only processes on this machine can reach it, which is the same trust as your own
terminal.

Move that boundary and you move the trust. **Anyone who can reach this daemon can run code in
every registered workspace, with no password.** Tailscale is the right answer because the tailnet
brings its own device auth and encryption — the boundary becomes "devices I enrolled." A public
interface has no boundary at all, and is deliberately unsupported.

## Setup

On the machine running the daemon:

```sh
tailscale status                  # confirm the tailnet is up
claude-chat host tailscale        # rebind to this machine's 100.x address
claude-chat status                # confirm the new url
```

`host tailscale` finds the machine's `100.64/10` (CGNAT) address — via `tailscale ip -4`, falling
back to an interface scan — and rebinds. It prints a warning naming the exposure, on purpose.

On the phone: install Tailscale, sign into the same tailnet, open `http://100.x.y.z:7788`.

## Add to home screen

The UI ships a PWA manifest, so **Share → Add to Home Screen** gives it a standalone app frame —
no browser chrome, its own icon. The composer is built for it: Enter inserts a newline on a soft
keyboard (SEND sends), the sidebar is a drawer, and the approval cards are thumb-sized.

## Both at once: the friendly url AND the phone

The two paths want different binds, and one bind has to serve both:

| Bind | `chat.go` via the local proxy | Phone over the tailnet |
|---|---|---|
| `local` (127.0.0.1) | ✓ | ✗ |
| `tailscale` (100.x) | ✗ — unless the vhost targets 100.x | ✓ |
| `all` (0.0.0.0) | ✓ | ✓ — **and your whole LAN** |

```sh
claude-chat host all    # both paths; your firewall is now the only thing in the way
```

Pick `all` only on a machine whose LAN you trust (and re-run `sudo claude-chat hostname install`
after any `host` change — the vhost targets the bind that existed when it was written).

## Verify

```sh
claude-chat status | grep url
curl -s http://100.x.y.z:7788/api/state    # from another tailnet device
```

Then send a message from the phone and watch the ticker move. If the reply lands but the ticker
never moves, something between you and the daemon is buffering the event stream — see
[HOSTNAME-SETUP.md](HOSTNAME-SETUP.md).

## Back to local-only

```sh
claude-chat host local
```
