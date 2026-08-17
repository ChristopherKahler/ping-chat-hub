---
type: doc
status: active
tags: [ping-chat-hub, tailscale, phone, pwa, mobile, remote-access, security, runbook]
relatedTo: [ping-chat-hub]
---

# The phone path

The point of the hub is reading your sessions from the couch and talking back to
them. That needs three things working at once: the phone can reach the daemon,
the browser trusts the page enough to give it a microphone, and the page
installs to your home screen like an app.

All three come from one decision, so get this part right and the rest follows:

> **Reach the hub over `tailscale serve`, not over a bare tailnet IP.**

## Why HTTPS is the whole answer

A tailnet IP works — `http://100.x.y.z:7799` loads the page. Then the microphone
button does nothing, the app will not install, and nothing explains why.

Browsers gate the interesting APIs behind a **secure context**. Over plain HTTP
to an IP address you lose:

| | |
|---|---|
| `getUserMedia` | no microphone, so no voice |
| service worker | no `sw.js`, so no install prompt |
| add to home screen | no standalone app frame |

`localhost` is exempt, which is why everything works on the machine itself and
then quietly stops working from the phone.

`tailscale serve` terminates TLS with a real certificate for your `.ts.net`
name. That makes the phone a secure context, and all three come back.

## Setup

Both topologies below assume the hub is already running and answering on
`http://127.0.0.1:7799` on its own machine.

### If tailscale runs on the same OS as the hub

This is the simple case. Use it if you can.

```sh
tailscale serve --bg --https=443 http://127.0.0.1:7799
tailscale serve status
```

Open `https://<node>.<tailnet>.ts.net` on the phone, signed into the same
tailnet. Done, and nothing here goes stale.

### If tailscale runs inside WSL and the hub runs on Windows

WSL reaches Windows through its default gateway; Windows cannot reach WSL's
loopback. So the hub binds `0.0.0.0` (its default) and serve points at the
gateway address rather than at localhost:

```sh
# inside WSL
tailscale serve --bg --https=8443 "http://$(ip route show default | awk '{print $3}'):7799"
```

Open `https://<node>.<tailnet>.ts.net:8443`.

> **This one goes stale.** That gateway address is handed out fresh on every WSL
> boot. After a WSL restart the mapping points at an address that no longer
> routes, and the phone shows a proxy error while the hub itself is perfectly
> healthy. **Symptom: the page worked yesterday and today it does not, from the
> phone only.** Re-run the same command — it recomputes the address.

Pin the values if you would rather not think about it: put an explicit
`[wsl] distro` and `home_linux` in `hub.toml` (see the README) and re-run serve
after each WSL restart, or drive it from a login script.

## Add it to the home screen

With HTTPS in place the hub is an installable PWA: it ships a manifest, an icon
and a service worker.

- **Android / Chrome** — menu → *Install app* (or *Add to Home screen*).
- **iOS / Safari** — Share → *Add to Home Screen*.

You get an app icon, its own window with no browser furniture, and a standalone
frame that keeps the composer above the keyboard.

## Check it end to end

```sh
tailscale serve status                 # the mapping, and what it points at
tailscale status                       # is this node up, is the phone enrolled
curl -s http://127.0.0.1:7799/api/capabilities   # on the hub machine
```

Then from the phone: load the page, confirm threads appear, tap the microphone
and speak. `stt: ready` in capabilities plus a silent microphone on the phone
means the secure-context problem above, not a broken speech server.

## The security shape

**Anyone who reaches this daemon can open terminals and run code on that
machine.** There is no password, because the network boundary *is* the
authentication.

That makes tailscale the right answer rather than a convenient one: a tailnet
brings device authentication and encryption of its own, so the boundary becomes
"devices I enrolled." Keep the hub on loopback or a tailnet. Do not funnel it to
the public internet, and do not bind it to a LAN you share with anyone you would
not hand a terminal to.
