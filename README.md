# ping-chat-hub

**Every Claude Code session you are running, in one window — from your phone.**

Sessions register themselves with [BASE](https://chrisai.cv/skool)'s relay under a
codename. The hub is a window over that relay: who is alive, what each one is
doing right now, the full ping history per thread, decision cards that survive
you walking away, and a launcher that opens real terminal tabs. It talks and
listens — speech in from the browser mic, speech out through a bundled voice.

```
┌ threads ────────────┐
│ ● heron      opus   │   watching · "reviewing G2 evidence"
│ ● wslterm3   sonnet │   running Grep
│ ○ falcon            │   gone — history kept
└─────────────────────┘
        │
        ├─ tap a thread ──▶ journal + reply box (your reply is a relay ping)
        ├─ escalations ──▶ decision cards, answered from the phone
        ├─ launcher ─────▶ a real terminal tab, briefed and codenamed
        └─ mic ──────────▶ /api/stt ──▶ Parakeet, on your machine
```

Two Python packages live here:

| | |
|---|---|
| **`ping_hub`** | the hub — relay roster, journals, escalations, launcher, voice |
| **`claude_chat`** | a separate app: headless Claude Code sessions in a browser (see the end of this file) |

---

## Install

Needs **Python 3.11+** (the hub reads TOML with `tomllib`), `base` and `claude`
on PATH, and `ffmpeg`. Windows is the built platform; see **Mac** below.

```sh
python -m venv .venv
.venv/Scripts/pip install .          # .venv/bin/pip on unix
ping-hub install                     # preflight, fetch the engines, write hub.toml
ping-hub serve                       # http://127.0.0.1:7799
```

`ping-hub install` downloads about 800 MB — a Parakeet speech-to-text model and
the Kokoro voice — and builds each one an **isolated venv** under `~/.ping-hub`.
It never installs into the interpreter you ran it with. Then it writes
`hub.toml` pointing at what it just built.

```
ping-hub install --yes             no prompts
                 --no-voice        config only, skip the models
                 --home DIR        somewhere other than ~/.ping-hub
                 --deploy-bridge   copy the WSL bridge into WSL (see below)
ping-hub doctor                    what works here, and why not
ping-hub config                    every resolved value and where it came from
```

### When something is wrong

`ping-hub doctor` is the first thing to run. It reports each capability as one
of four states, and they mean different things:

```
ready    it works
absent   not installed on this machine
error    installed but not responding
off      you wrote enabled = false in hub.toml
```

```
$ ping-hub doctor
config: (none — everything derived)
  ready   stt      http://127.0.0.1:8973/inference
  ready   tts      C:\Users\you\.ping-hub\tts\say.cmd
  absent  cx_ptt   no C:\Users\you\.base-gbl\cx.toml
  ready   wsl      Ubuntu at /home/you
  ready   base     C:\Users\you\.local\bin\base.EXE
```

The same data is served at `GET /api/capabilities`, which is how the page
explains a silent mic instead of showing a dead button.

---

## hub.toml

Machine shape: paths, ports, binaries, which terminal opens tabs. Found at
`$PING_HUB_CONFIG`, else `$BASE_GBL/hub.toml`, else `~/.base-gbl/hub.toml`.

**Having no hub.toml at all is a supported state.** Every key derives from a
platform or BASE root — the WSL home is asked of WSL, the Windows Terminal
profile is read from `cx.toml`, the gated-build doc falls out of your BASE
global tier. Write a key only to override a derivation. An empty string means
"derive this", so a config can name a key without pinning it.

```toml
[hub]
port                    = 7799
bind                    = "0.0.0.0"   # 127.0.0.1 for local-only
register_standing_title = true        # false for a second, non-live instance

[paths]
base_bin  = "base"
base_gbl  = ""        # "" -> ~/.base-gbl
hub_home  = ""        # "" -> ~/.ping-hub
gated_doc = ""        # "" -> <base_gbl>/PROCESS-gated-build.md

[wsl]                 # Windows only; enabled = false collapses to one side
enabled     = true
distro      = ""      # "" -> the default WSL distro
home_linux  = ""      # "" -> asked of WSL
bridge_port = 7798

[terminal]
adapter         = "wt"        # wt | tmux | iterm2 | terminal_app
wsl_profile     = ""          # "" -> from cx.toml [switch], else WT's default
windows_profile = "PowerShell"
restore_focus   = true        # spawn tabs without stealing the screen

[spawn]
default_model    = ""                    # "opus" boots the 1M-context variant
disallowed_tools = ["AskUserQuestion"]   # invisible to someone on a phone

[stt]                 # bundled. enabled = false is an override, not a default
enabled = true
url     = "http://127.0.0.1:8973/inference"
ffmpeg  = "ffmpeg"

[tts]                 # bundled
enabled       = true
default_voice = "af_heart"

[cx_ptt]              # optional push-to-talk module, Windows only
enabled = true
```

Runtime preferences you toggle in the UI — spawn model, effort, voice — live in
`<base_gbl>/.base/hub/settings.json` instead. Different file, different job;
`ping-hub install` never touches it.

### Ports

| | |
|---|---|
| 7799 | the hub |
| 7798 | the WSL bridge, inside WSL |
| 8973 | the bundled speech-to-text server |

`bind = "0.0.0.0"` is the default because a tailnet proxy cannot reach a
loopback bind. **Anyone who reaches this daemon can open terminals on your
machine.** Keep it on localhost or a tailnet; a public interface is unsupported.

## The phone

[docs/PHONE-SETUP.md](docs/PHONE-SETUP.md) is the end-to-end path. The short
version: reach the hub through `tailscale serve`, not a bare tailnet IP. Over
plain HTTP to an IP the page loads but the microphone silently does nothing and
the app will not install, because browsers gate both behind a secure context.
`serve` gives you a real certificate and all of it works.

---

## Two sides: Windows and WSL

Sessions running under WSL register in WSL's own BASE store, which is a
different store. The hub does not read it over `\\wsl.localhost`. Instead a
small bridge runs **inside** WSL, serving its roster and pings on 7798, and the
hub long-polls it — push-shaped delivery over a pull transport.

```sh
ping-hub install --deploy-bridge     # copies the bridge in, writes its config
# then, inside WSL:
python3 ~/.local/share/hub-bridge/wsl-bridge.py
```

That flag is off by default because it overwrites a deployed bridge that may be
running. On a machine with no WSL, set `[wsl] enabled = false`; the hub collapses
to one side, and nothing ever looks for a bridge.

**Known limitation: start WSL before the hub.** With no `[wsl]` block in
hub.toml the distro and home are asked of WSL once, at boot, and cached for the
life of the process. If WSL is down at that moment the hub decides there is no
WSL side and does not start the bridge loop, and it will not notice WSL coming
back later. Restart the hub after starting WSL, or pin the values so the probe
never runs:

```toml
[wsl]
distro     = "Ubuntu"
home_linux = "/home/you"
```

## Mac

Designed, not built. The two-sided model collapses to one side and the bridge
becomes unnecessary, but **no terminal adapter is implemented** — `tmux`,
`iterm2` and `terminal_app` are named in the schema and raise `AdapterNotBuilt`
if selected. Everything else is portable: the daemon and UI are stdlib plus one
HTML file, and the speech engines are cross-platform.

## Starting at login

`ping_hub.autostart` generates a Scheduled Task on Windows and a launchd agent
on Mac, with a dry run that hands you the exact command first. **It is not yet
called by `ping-hub install`** — start the hub yourself, or register the task by
hand, until it is wired in.

## Layout

```
src/ping_hub/
  daemon.py       HTTP + SSE; the endpoints
  engine.py       roster, inbox watcher, append-only journals, escalations
  config.py       hub.toml — resolve, derive, default
  capabilities.py ready / absent / error / off
  install.py      fetch, venv, verify, write config, deploy the bridge
  cli.py          serve · install · doctor · config
  autostart.py    Scheduled Task (Windows) · launchd agent (Mac)
  spawn/          terminal adapters — wt built, others declared
  voice/          the bundled speech engines, copied out at install
  bridge/         the WSL bridge, copied into WSL at install
  hub.html        the UI
tests/            python -m pytest
```

The journal is the durable history: relay inbox files are consumed by BASE's own
delivery, so the hub writes every ping it sees to an append-only JSONL per
thread. Threads are never deleted — a session that goes away is greyed, not
dropped.

---

# claude-chat

A separate app in the same repo: Claude Code sessions in a browser, **in any
directory**. Every conversation is a headless `claude --print` session pinned to
a working directory, with live turn telemetry, mid-flight steering, and
Allow/Deny approval cards.

```sh
claude-chat setup                       # port + permission mode
claude-chat workspace add /path/to/project
claude-chat serve                       # http://127.0.0.1:7788
```

A workspace is a directory you can quick-select plus optional per-directory
defaults — model, permission posture, a boot prompt, extra readable roots.
Sessions inherit that directory's `CLAUDE.md`, hooks and MCP config exactly as a
terminal session there would, and the directory is **pinned at creation**, so a
resumed session can never move.

| | |
|---|---|
| **Activity ticker** | the current tool or latest reasoning line, over SSE |
| **Context meter** | prompt-side tokens against the model's window |
| **Approval cards** | fail-closed: an unreachable daemon and an unanswered card both deny |
| **Mid-flight steering** | a reply landing mid-turn is delivered *into* the live session via BASE's relay; without BASE it queues, which is the honest fallback |
| **Mid-turn voice** | `claude-chat say "found the bug"` posts before the turn ends |

Same warning applies: anyone who reaches it can run code in every registered
workspace. See `docs/HOSTNAME-SETUP.md` and `docs/TAILSCALE-SETUP.md`.

```
src/claude_chat/
  daemon.py · turns.py · store.py · approve.py · service.py · hosts.py · chat.html
```

Depends only on `mcp` (the approval gate is an MCP server). Everything else is
stdlib.
