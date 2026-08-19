"""cx-ptt — multi-terminal push-to-talk -> relay bridge (work-side twin of
ptt-thought.py, which stays Skyrim's).

One mic + one parakeet model (~700MB RAM) serves every configured terminal.
Each [terminals.<codename>] block in cx.toml (see cxpaths.py) gets its
own press-toggle hotkey: press = mic opens (pitched boop identifies which
terminal), press again = transcribe -> `base relay ping --to <codename>`.
The daemon hot-reloads cx.toml (~3s mtime poll) so adding a block while it
runs arms the new hotkey live — that is what makes `cx --new <name>` work
without a restart.

Routing is per-side because the Windows and WSL relay stores are SEPARATE
(neither BASE_RELAY_STORE nor BASE_GLOBAL_DIR redirects the titled-session
registry): side = "wsl" pings through `wsl.exe base`, side = "win" through
base.exe. Replies land in each side's own chris inbox, so BOTH inboxes are
watched and routed by sender into per-codename feed files under
.base-gbl\\cx\\feeds\\ — the cx-chat windows just tail those.

Known crosstalk: ptt-thought.py's reply watcher prints every Windows-side
chris-inbox message unfiltered, so work replies also echo in the Skyrim
console when both daemons run. Harmless.

Run:  python cx-ptt.py              (console — normal use, see Work-Channel.cmd)
      python cx-ptt.py --config     (parse cx.toml, list terminals, exit)
      python cx-ptt.py --selftest   (prove deps + model load)
      python cx-ptt.py --testping <codename> "msg"   (relay plumbing, no mic)
"""
import ctypes
import datetime
import json
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

# ── where everything lives ───────────────────────────────────────────────────
# Vendored into ping-chat-hub 2026-08-19. Every path below used to be an
# operator literal, which is exactly why this daemon only ran on one machine.
# The installer writes them into the launcher's environment; the defaults
# derive, so running this by hand still works on any account.
import os as _os


def _env(name: str, default) -> Path:
    v = (_os.environ.get(name) or "").strip()
    return Path(v) if v else Path(default)


_HOME = Path(_os.environ.get("USERPROFILE") or _os.environ.get("HOME")
             or Path.home())
_BASE_GBL = _env("PING_HUB_BASE_GBL", _HOME / ".base-gbl")
_BASE_STORE = _BASE_GBL / ".base"

CONFIG_PATH = _env("PING_HUB_CX_TOML", _BASE_GBL / "cx.toml")
BASE_WIN = _os.environ.get("PING_HUB_BASE_WIN") or str(
    _HOME / ".local" / "bin" / "base.exe")
# inside WSL the binary is normally on PATH; the installer pins the full path
# when it knows it
BASE_WSL = _os.environ.get("PING_HUB_BASE_WSL") or "base"
CX_DIR = _env("PING_HUB_CX_DIR", _BASE_GBL / "cx")
FEED_DIR = CX_DIR / "feeds"
CHAT_DIR = CX_DIR / "chat"
DEADLETTER_DIR = CX_DIR / "deadletter"
FAILED_DIR = CX_DIR / "stt-failed"
TAKES_DIR = CX_DIR / "stt-takes"
# both sides' standing inboxes; either may not exist yet. The WSL one is a UNC
# path into the distro and only the installer knows the distro name, so it is
# supplied rather than guessed -- a machine with no WSL side simply watches one.
STANDING = _os.environ.get("PING_HUB_STANDING_TITLE") or "chris"
INBOXES = [_BASE_STORE / "relay-inbox" / STANDING]
_wsl_home_unc = (_os.environ.get("PING_HUB_WSL_HOME_UNC") or "").strip()
if _wsl_home_unc:
    INBOXES.append(Path(_wsl_home_unc) / ".base-gbl" / ".base"
                   / "relay-inbox" / STANDING)
SAMPLE_RATE = 16000
# the SAME parakeet the hub's STT server uses -- one 650MB download serves
# both, and `ping-hub install` points this at what it just provisioned
MODEL_DIR = _env("PING_HUB_STT_MODEL",
                 _HOME / ".ping-hub" / "stt" / "model")

SOUND_ON = {
    "start": True,
    "finish": True,
    "send": False,   # error-based: silence = sent fine, buzz = it didn't
    "receive": True,
    "cancel": True,
}
STAGE_OF = {"fail": "cancel"}


def load_config():
    """Terminals = [slots] entries (hotkey from the slot_hotkey template,
    slot number doubles as tone pitch) merged with optional [terminals]
    override blocks (side/pitch always apply; a block hotkey only binds a
    codename that has no slot). Entries with no hotkey from either source
    are dropped."""
    with CONFIG_PATH.open("rb") as f:
        cfg = tomllib.load(f)
    settings = cfg.get("settings", {})
    template = settings.get("slot_hotkey", "shift+num {n}")
    terminals = {}
    for i, (name, block) in enumerate(cfg.get("terminals", {}).items()):
        cn = block.get("codename", name)
        terminals[cn] = {
            "codename": cn,
            "hotkey": block.get("hotkey"),
            "side": block.get("side", "wsl"),
            "pitch": block.get("pitch", i * 2),
        }
    for n, v in cfg.get("slots", {}).items():
        cn = v.get("codename") if isinstance(v, dict) else v
        if not cn:
            continue
        side = v.get("side", "wsl") if isinstance(v, dict) else "wsl"
        t = terminals.setdefault(cn, {"codename": cn, "side": side, "pitch": int(n)})
        t["hotkey"] = template.format(n=n)
        if isinstance(v, dict):
            if "side" in v:
                t["side"] = v["side"]
            if v.get("say"):
                t["say"] = True
    terminals = {cn: t for cn, t in terminals.items() if t.get("hotkey")}
    return settings, terminals


SEED_STARS = {"handoff", "base", "fork", "end"}
_star_cache = {"names": None, "ts": 0.0}


def star_names():
    """Live star-command catalog from `base commands list`, cached 10 min.
    Falls back to the seed set (and retries soon) if base can't answer —
    e.g. commands.toml is a WSL-backed symlink and WSL is down."""
    now = time.time()
    if _star_cache["names"] is None or now - _star_cache["ts"] > 600:
        names = set(SEED_STARS)
        try:
            r = subprocess.run([BASE_WIN, "commands", "list"],
                               capture_output=True, text=True, timeout=15)
            for ln in (r.stdout or "").splitlines():
                ln = ln.strip()
                if ln.startswith("*"):
                    names.add(ln.split()[0].lstrip("*").lower())
        except Exception:
            pass
        _star_cache["names"] = names
        _star_cache["ts"] = now if len(names) > len(SEED_STARS) else now - 540
    return _star_cache["names"]


def normalize_stt(text):
    """Spoken star-commands become literal ones: 'run star command handoff'
    -> '*handoff'; trailing words ride along as arguments. Resolution is
    fuzzy against the live catalog because STT garbles and Chris misspeaks:
    exact match first (two words fused, then one word — catches 'hand off'),
    then difflib closest-match ('handof' -> handoff, 'dbug' -> debug). No
    match at all still emits *<spoken> — the session-side conduct resolves."""
    import difflib
    import re
    m = re.match(r"^(?:run |execute )?star command[,:]?\s+(.+)$",
                 text.strip(), re.I)
    if not m:
        return text
    words = m.group(1).strip().split()
    clean = [w.strip(".,!?").lower() for w in words]
    names = star_names()
    fused = "".join(clean[:2]) if len(clean) >= 2 else None
    resolved, used = None, 1
    for cand, n in ((fused, 2), (clean[0], 1)):
        if cand and cand in names:
            resolved, used = cand, n
            break
    if not resolved:
        # best score across BOTH forms, not first hit: 'hands off' must fuse
        # to handoff (not hands->handoff + stray arg), 'playing speak' must
        # reach plainspeak (not ping)
        best = (0.0, None, 1)
        for cand, n in ((fused, 2), (clean[0], 1)):
            if not cand:
                continue
            close = difflib.get_close_matches(cand, list(names), n=1, cutoff=0.6)
            if close:
                score = difflib.SequenceMatcher(None, cand, close[0]).ratio()
                if score > best[0]:
                    best = (score, close[0], n)
        if best[1]:
            resolved, used = best[1], best[2]
    if not resolved:
        resolved, used = clean[0], 1
    return ("*" + resolved + " " + " ".join(words[used:])).strip()


HUB_REPLACEMENTS = _BASE_STORE / "hub" / "replacements.json"
_hub_repl = [None, None]  # [(mtime, size), ordered pairs] — (mtime, size)
# keyed, not mtime alone: a one-character correction in the same second keeps
# the byte count moving even when the clock does not (builder's hardening note)


def hub_replacements():
    """Canonical replacement pairs from the ping-hub store (Chris ruling
    2026-08-17: ONE list, edited from the app, applied by every mic). None =
    store absent or unreadable -> the legacy cx.toml [settings.replacements]
    section applies instead. The store REPLACES the toml section entirely
    once it exists, so a pair deleted in the app genuinely stops firing."""
    try:
        st = HUB_REPLACEMENTS.stat()
        key = (st.st_mtime, st.st_size)
    except OSError:
        # store vanished for the atomic-replace instant: serve the last known
        # pairs rather than flapping to the legacy toml for one transcript
        return _hub_repl[1]
    if _hub_repl[0] != key:
        try:
            with open(HUB_REPLACEMENTS, encoding="utf-8") as fh:
                doc = json.load(fh)
            pairs = [(str(p["from"]), str(p.get("to", "")))
                     for p in (doc.get("pairs") or [])
                     if isinstance(p, dict) and str(p.get("from", "")).strip()
                     and p.get("enabled", True)]
        except (OSError, ValueError, KeyError):
            return _hub_repl[1]   # unreadable: last known, never crash dictation
        _hub_repl[0] = key
        _hub_repl[1] = pairs
    return _hub_repl[1]


def fix_transcript(text):
    """Fixed transcriptions: the hub's canonical store when it exists, else
    cx.toml [settings.replacements] (hot-reloaded). Case-insensitive
    whole-word swaps applied in list order — matching kept IDENTICAL to the
    hub's replacements.py, which tests itself against this exact expression."""
    import re
    pairs = hub_replacements()
    if pairs is None:
        pairs = list((settings.get("replacements") or {}).items())
    for wrong, right in pairs:
        text = re.sub(r"(?i)\b" + re.escape(wrong) + r"\b", right, text)
    return text


def base_cmd(side):
    if side == "wsl":
        return ["wsl.exe", "-e", BASE_WSL]
    return [BASE_WIN]


def relay_ping(codename, side, text):
    r = subprocess.run(
        base_cmd(side) + ["relay", "ping", "--to", codename, "--msg", text,
                          "--from", "chris"],
        capture_output=True, text=True, timeout=25)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "base relay ping failed").strip())


def feed(codename, who, text):
    """Append one exchange line: jsonl feed (cx-chat tails it) + daily md journal."""
    now = datetime.datetime.now()
    try:
        FEED_DIR.mkdir(parents=True, exist_ok=True)
        with (FEED_DIR / f"{codename}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now.isoformat(timespec="seconds"),
                                "who": who, "text": text}, ensure_ascii=False) + "\n")
    except Exception:
        pass
    try:
        d = CHAT_DIR / codename
        d.mkdir(parents=True, exist_ok=True)
        md = d / f"chat-{now.strftime('%Y-%m-%d')}.md"
        if not md.exists():
            md.write_text(f"---\ntype: note\nstatus: active\ntags: [cx, relay, chat]\n---\n\n"
                          f"# {codename} relay chat {now.strftime('%Y-%m-%d')}\n\n",
                          encoding="utf-8")
        with md.open("a", encoding="utf-8") as f:
            f.write(f"- **{now.strftime('%H:%M:%S')} {who}:** {text}\n")
    except Exception:
        pass


def deadletter(codename, text, err):
    DEADLETTER_DIR.mkdir(parents=True, exist_ok=True)
    with (DEADLETTER_DIR / f"{codename}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now(datetime.timezone.utc)
                  .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "to": codename, "text": text, "error": err,
        }, ensure_ascii=False) + "\n")


if "--testnorm" in sys.argv:
    i = sys.argv.index("--testnorm")
    print(normalize_stt(sys.argv[i + 1] if len(sys.argv) > i + 1 else ""))
    sys.exit(0)

if "--config" in sys.argv:
    settings, terminals = load_config()
    print(f"cx.toml OK — cancel={settings.get('cancel_hotkey', 'ctrl+alt+x')}, "
          f"auto_send={settings.get('auto_send_seconds', 120)}s")
    for t in terminals.values():
        tts = ", say ON" if t.get("say") else ""
        print(f"  {t['hotkey']:<16} -> {t['codename']}  (side={t['side']}, pitch=+{t['pitch']}{tts})")
    sys.exit(0)

if "--testping" in sys.argv:
    i = sys.argv.index("--testping")
    if len(sys.argv) < i + 3:
        print("usage: cx-ptt.py --testping <codename> <msg>")
        sys.exit(1)
    cn, msg = sys.argv[i + 1], sys.argv[i + 2]
    _, terminals = load_config()
    side = terminals.get(cn, {}).get("side", "wsl")
    try:
        relay_ping(cn, side, msg)
        feed(cn, "CHRIS", msg)
        print(f"TESTPING OK -> ({side}) base relay ping --to {cn}: {msg}")
        sys.exit(0)
    except Exception as e:
        deadletter(cn, msg, str(e))
        print(f"TESTPING FAILED (dead-lettered -> {DEADLETTER_DIR}\\{cn}.jsonl): {e}")
        sys.exit(1)

import numpy as np
import sounddevice as sd
import sherpa_onnx
import keyboard
import winsound

# down here with the rest of the heavy imports: it pulls numpy, and --config /
# --testping must stay instant
import stt_decode
from stt_decode import write_wav

ctypes.windll.kernel32.CreateMutexW(None, False, "CxPTT_singleton")
if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    print("another cx-ptt is already running — exiting")
    sys.exit(1)

print("Loading parakeet-unified-en-0.6b (one-time, ~700MB RAM)...")
t0 = time.time()
recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
    encoder=str(MODEL_DIR / "encoder.int8.onnx"),
    decoder=str(MODEL_DIR / "decoder.int8.onnx"),
    joiner=str(MODEL_DIR / "joiner.int8.onnx"),
    tokens=str(MODEL_DIR / "tokens.txt"),
    num_threads=4,
    model_type="nemo_transducer",
)
print(f"Model loaded in {time.time() - t0:.1f}s")


# ── Tones ────────────────────────────────────────────────────────────────────
def _tone_wav(parts, vol=0.4, decay=False):
    """parts = [(freq_hz, ms), ...] -> one wav byte-string (16-bit mono).

    Leads with 200ms of silence: the G733 wireless link clips the first
    fraction of a new audio session, so short unpadded beeps vanish."""
    import io
    import wave
    samples = [np.zeros(int(SAMPLE_RATE * 0.2))]
    for freq, ms in parts:
        t = np.arange(int(SAMPLE_RATE * ms / 1000)) / SAMPLE_RATE
        note = (np.sin(2 * np.pi * freq * t)
                + 0.4 * np.sin(2 * np.pi * freq * 2 * t)) * vol
        if decay:
            note = note * np.exp(-t * 7)
        samples.append(note)
        samples.append(np.zeros(int(SAMPLE_RATE * 0.02)))
    pcm = (np.concatenate(samples) * 32767).astype(np.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


import tempfile

TONE_DIR = Path(tempfile.gettempdir()) / "cx-ptt-tones"
TONE_DIR.mkdir(exist_ok=True)


def build_tones(terminals):
    """Global tones once + per-terminal pitched start/finish/receive so the ear
    can tell which terminal's mic opened or which session replied."""
    (TONE_DIR / "send.wav").write_bytes(
        _tone_wav([(523, 200), (659, 200), (784, 280)], vol=0.22))
    (TONE_DIR / "cancel.wav").write_bytes(_tone_wav([(220, 300)]))
    (TONE_DIR / "fail.wav").write_bytes(_tone_wav([(220, 500)]))
    for t in terminals.values():
        f = 2 ** (t["pitch"] / 12)
        cn = t["codename"]
        (TONE_DIR / f"start-{cn}.wav").write_bytes(_tone_wav([(440 * f, 150)]))
        # END = a rising plucked triad (a "success" chime), unmistakably
        # different from the single-note START beep so the ear never confuses
        # open-mic with mic-closed. Still ×f per terminal, keeping the
        # which-terminal cue in the pitch.
        (TONE_DIR / f"finish-{cn}.wav").write_bytes(
            _tone_wav([(523 * f, 80), (659 * f, 80), (880 * f, 160)],
                      vol=0.28, decay=True))
        (TONE_DIR / f"receive-{cn}.wav").write_bytes(
            _tone_wav([(1319 * f, 450)], vol=0.3, decay=True))


HUB_SETTINGS = _BASE_STORE / "hub" / "settings.json"
_hub_sounds = [{}, 0.0]  # [dict, mtime] — hub UI settings modal writes this file


def hub_sounds():
    try:
        mt = HUB_SETTINGS.stat().st_mtime
    except OSError:
        return {}
    if _hub_sounds[1] != mt:
        try:
            with open(HUB_SETTINGS, encoding="utf-8") as fh:
                _hub_sounds[0] = json.load(fh).get("sounds", {})
        except (OSError, ValueError):
            _hub_sounds[0] = {}
        _hub_sounds[1] = mt
    return _hub_sounds[0]


def play(name, codename=None):
    snd = hub_sounds()
    stage = STAGE_OF.get(name, name)
    if not snd.get(stage, SOUND_ON.get(stage, True)):
        return
    # per-action wav override from the hub settings modal
    override = snd.get(stage + "_wav")
    if override and override != "default" and Path(override).exists():
        try:
            winsound.PlaySound(override,
                               winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            return
        except Exception:
            pass
    path = TONE_DIR / (f"{name}-{codename}.wav" if codename else f"{name}.wav")
    if not path.exists():
        path = TONE_DIR / f"{name}.wav"
    try:
        winsound.PlaySound(str(path),
                           winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:
        try:
            winsound.Beep(440, 80)
        except Exception:
            pass


if "--selftest" in sys.argv:
    s = recognizer.create_stream()
    s.accept_waveform(SAMPLE_RATE, np.zeros(SAMPLE_RATE, dtype=np.float32))
    recognizer.decode_stream(s)
    settings, terminals = load_config()
    print(f"SELFTEST OK (silence -> '{s.result.text}', {len(terminals)} terminal(s) configured)")
    sys.exit(0)


# ── Recording state ──────────────────────────────────────────────────────────
frames = []
recording = threading.Event()
active = [None]          # codename holding the mic right now
send_timer = [None]
last_toggle = {}         # per-codename debounce vs key auto-repeat
settings, terminals = load_config()


def audio_callback(indata, _frames, _time, _status):
    if recording.is_set():
        frames.append(indata.copy())


def start_recording(codename):
    active[0] = codename
    frames.clear()
    recording.set()
    play("start", codename)
    secs = settings.get("auto_send_seconds", 120)
    t = threading.Timer(secs, lambda: stop_and_dispatch(codename))
    t.daemon = True
    send_timer[0] = t
    t.start()
    w = threading.Timer(1.5, lambda: _dead_stream_check(codename))
    w.daemon = True
    w.start()
    print(f"[{codename}] mic open — press again to send")


def _dead_stream_check(codename):
    # A slept-and-woken headset (G733 does this daily) re-enumerates the
    # device, and the process-lifetime InputStream silently stops delivering.
    # If the mic has been open 1.5s and the callback has produced nothing,
    # the stream is dead — recover NOW, not after a full take is lost.
    if recording.is_set() and active[0] == codename and not frames:
        play("fail")
        print("(mic stream is dead — headset slept/rebound; auto-restarting "
              "to rebind. Re-press your hotkey in ~3s.)")
        restart_self()


def toggle(codename):
    now = time.time()
    if now - last_toggle.get(codename, 0.0) < 0.5:
        return  # key auto-repeat while held
    last_toggle[codename] = now
    if recording.is_set():
        if active[0] == codename:
            stop_and_dispatch(codename)
        else:
            print(f"[{codename}] mic busy — {active[0]} is recording "
                  f"(cancel: {settings.get('cancel_hotkey', 'ctrl+alt+x')})")
    else:
        start_recording(codename)


_last_unbound = {}


def unbound_slot(digit):
    """A claimed-but-unbound slot chord. The daemon console is hidden in the
    tray, so the buzz IS the feedback — silence here would read as 'the ping
    went out' when nothing did."""
    now = time.time()
    if now - _last_unbound.get(digit, 0.0) < 1.0:
        return
    _last_unbound[digit] = now
    play("fail")
    print(f"(slot {digit} is not bound to any session — key swallowed, nothing "
          f"sent. Bind it with: cx <codename> {digit})")


def stray_numpad(name):
    """Silent on purpose — no buzz. These are mis-hits, not failed pings."""
    print(f"(shift+numpad '{name}' swallowed — not a slot key, and letting it "
          f"reach the terminal is destructive)")


# ── Focused-thread PTT (ping-chat-hub, Chris sign-off 2026-08-17) ────────────
# ctrl+shift+space talks to whatever thread is selected in the hub UI — no
# slot digit needed. The hub daemon writes the selection to hub/state.json on
# every thread click; unknown titles get a synthetic terminal entry so tones
# and side resolution work like any slotted session.
HUB_STATE = _BASE_STORE / "hub" / "state.json"


def focused_ptt():
    try:
        with open(HUB_STATE, encoding="utf-8") as fh:
            focus = (json.load(fh).get("focused") or {})
    except Exception:
        focus = {}
    cn, side = focus.get("title"), focus.get("side", "win")
    if not cn:
        play("fail")
        print("(ctrl+shift+space: no focused hub thread — select one in the hub UI)")
        return
    if cn not in terminals:
        terminals[cn] = {"codename": cn, "side": side, "pitch": 0,
                         "say": False, "hotkey": None}
        try:
            build_tones(terminals)
        except Exception:
            pass
    toggle(cn)


SPAWN_DRAFT = "@spawn-draft"


def spawn_draft_ptt():
    """Toggle a recording whose transcript goes to the hub's launcher-prompt
    draft (POST /api/spawn-draft) instead of a relay ping."""
    if SPAWN_DRAFT not in terminals:
        terminals[SPAWN_DRAFT] = {"codename": SPAWN_DRAFT, "side": "win",
                                  "pitch": 8, "say": False, "hotkey": None}
        try:
            build_tones(terminals)
        except Exception:
            pass
    toggle(SPAWN_DRAFT)


def cancel_recording():
    if not recording.is_set():
        return
    cn = active[0]
    recording.clear()
    active[0] = None
    if send_timer[0]:
        send_timer[0].cancel()
        send_timer[0] = None
    frames.clear()
    play("cancel")
    print(f"\n(cancelled {cn} recording — nothing sent)")


# ── Transcription (proven recipe from ptt-thought.py) ────────────────────────
def _decode(seg):
    s = recognizer.create_stream()
    s.accept_waveform(SAMPLE_RATE, seg)
    recognizer.decode_stream(s)
    return s.result.text.strip()


def transcribe(audio):
    """Escalating decode — see stt_decode.py for the ladder (shared with
    stt-fix so a hand re-run walks exactly this path). Returns (text, lost)."""
    return stt_decode.transcribe(_decode, audio)


def save_failed_take(audio):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return write_wav(FAILED_DIR / f"take_{stamp}.wav", audio)


def archive_take(audio, codename):
    """Rolling ring of the last `keep_takes` takes, so ANY take — not just a
    failed one — can be re-run with `stt-fix` instead of being spoken again.
    Oldest are pruned; keep_takes = 0 in [settings] turns the ring off."""
    keep = int(settings.get("keep_takes", 30) or 0)
    if keep <= 0:
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = write_wav(TAKES_DIR / f"{stamp}_{codename}.wav", audio)
    for old in sorted(TAKES_DIR.glob("*.wav"))[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass
    return path


def stop_and_dispatch(codename):
    if not recording.is_set() or active[0] != codename:
        return
    recording.clear()
    active[0] = None
    if send_timer[0]:
        send_timer[0].cancel()
        send_timer[0] = None
    play("finish", codename)
    if not frames:
        play("fail")
        print("(no audio captured — mic stream delivered zero frames; "
              "auto-restarting to rebind. Re-press your hotkey in ~3s.)")
        restart_self()
        return
    audio = np.concatenate(frames)[:, 0].astype(np.float32)
    dur = len(audio) / SAMPLE_RATE
    if dur < 0.4:
        print(f"(too short: {dur:.2f}s, ignored)")
        return
    t = time.time()
    archive_take(audio, codename)
    text, lost = transcribe(audio)
    if lost:
        # the rescue ladder already tried and failed; keep the marker terse and
        # the path out of the message — the session can't act on either
        wav = save_failed_take(audio)
        text += f" [{lost} piece{'s' if lost > 1 else ''} of this take would not decode]"
        print(f"({lost} piece(s) unrecoverable — audio at {wav.name}; "
              f"retry by hand with: stt-fix {wav.name})")
    if not text:
        sec = SAMPLE_RATE
        peaks = [float(np.sqrt(np.mean(audio[i:i + sec] ** 2)))
                 for i in range(0, max(len(audio) - sec, 1), sec)]
        if not peaks or max(peaks) < 0.02:
            play("fail")
            print(f"(MIC DEAD: {dur:.0f}s at noise floor, max rms {max(peaks or [0]):.4f} — "
                  f"no speech reached the stream. Check boom mute / mic grab.)")
            text = (f"[MIC WAS DEAD during a {dur:.0f}s take — noise floor only, "
                    f"nothing transcribable ever reached the stream. FYI.]")
        else:
            wav = save_failed_take(audio)
            play("fail")
            # no "go transcribe this" instruction: the daemon has already run
            # the full ladder, so the session would only burn tokens failing too
            text = (f"[STT recovered nothing from a {dur:.0f}s take — "
                    f"nothing to act on, I'll repeat it.]")
            print(f"(decode + rescue both empty — audio salvaged to {wav.name}; "
                  f"recover with: stt-fix {wav.name})")
    text = fix_transcript(text)
    text = normalize_stt(text)
    if codename == SPAWN_DRAFT:
        # launcher-prompt dictation: hub-side draft, not a relay ping
        import urllib.request
        try:
            urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:7799/api/spawn-draft",
                data=json.dumps({"text": text}).encode(),
                headers={"Content-Type": "application/json"}), timeout=5)
            play("send")
            print(f"\n>> [spawn-draft] {text}\n   ({dur:.1f}s audio — waiting "
                  f"behind the hub's + button)")
        except Exception as e:
            play("fail")
            print(f"\n[spawn-draft] hub unreachable, text NOT saved: {e}\n   {text}")
        return
    term = terminals.get(codename)
    side = term["side"] if term else "wsl"
    try:
        relay_ping(codename, side, text)
    except Exception as e:
        deadletter(codename, text, str(e))
        play("fail")
        feed(codename, "SYSTEM", f"send FAILED (dead-lettered): {text}")
        print(f"\n[{codename}] RELAY FAILED, dead-lettered: {e}")
        return
    play("send")
    feed(codename, "CHRIS", text)
    print(f"\n>> [{codename}] CHRIS: {text}\n   [{dur:.1f}s audio, {time.time() - t:.1f}s stt]")


# ── Say-back: speak replies for say-enabled terminals via kokoro ─────────────
SAY_CMD = _os.environ.get("PING_HUB_TTS_SAY") or str(
    _HOME / ".ping-hub" / "tts" / "say.cmd")
SAY_MAX_CHARS = 600
import queue

say_q = queue.Queue()


def say_worker():
    """Serialized so overlapping replies never talk over each other. The say
    daemon releases its client with ~2s of audio still buffered and a NEW
    `say` interrupts current playback (say.py line 19), so a gap after each
    utterance keeps back-to-back replies from clipping each other's tails."""
    while True:
        text = say_q.get()
        if len(text) > SAY_MAX_CHARS:
            text = text[:SAY_MAX_CHARS].rsplit(" ", 1)[0] + \
                " ... full message in the chat window."
        try:
            subprocess.run([SAY_CMD, text], capture_output=True, timeout=180)
            time.sleep(2.5)
        except Exception:
            pass


threading.Thread(target=say_worker, daemon=True).start()


# ── Chat guard: every live session keeps a chat window, always ───────────────
PIDS_DIR = CX_DIR / "pids"
# it ships beside this file now, so there is nothing left to configure
SPAWNER = str(Path(__file__).resolve().with_name("cx-spawn-hidden.py"))
# never the operator's global interpreter (install.py finding 3): the one
# running this daemon is the venv the installer built for it
PYW = _os.environ.get("PING_HUB_PYTHONW") or str(
    Path(sys.executable).with_name("pythonw.exe"))
NO_WINDOW = 0x08000000


def _session_alive(ref):
    if ref.startswith("win:"):
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, int(ref[4:]))  # SYNCHRONIZE
        if not h:
            return False
        try:
            return k.WaitForSingleObject(h, 0) != 0
        finally:
            k.CloseHandle(h)
    if ref.startswith("wsl:"):
        try:
            r = subprocess.run(["wsl.exe", "-e", "test", "-e", f"/proc/{ref[4:]}"],
                               capture_output=True, timeout=20,
                               creationflags=NO_WINDOW)
            return r.returncode == 0
        except Exception:
            return False
    return False


def chat_guard():
    """X-ing (or otherwise losing) a chat window must never orphan a live
    session: any registered session without a window gets one respawned,
    hidden, within seconds — so 'close' degrades to 'back to the tray'."""
    while True:
        time.sleep(8)
        if not settings.get("tray", True):
            continue
        try:
            for pf in PIDS_DIR.glob("*"):
                cn = pf.name
                try:
                    ref = pf.read_text(encoding="utf-8").strip()
                except Exception:
                    continue
                if not _session_alive(ref):
                    continue
                if not ctypes.windll.user32.FindWindowW(None, f"cx-chat {cn}"):
                    subprocess.Popen([PYW, SPAWNER, cn], creationflags=NO_WINDOW)
                    print(f"(chat window for live session '{cn}' was gone — respawned to tray)")
        except Exception:
            pass


# ── Reply watcher: both sides' chris inboxes -> per-codename feeds ───────────
def reply_watcher():
    seen = {}
    for box in INBOXES:
        try:
            seen[box] = {p.name for p in box.glob("*.json")}
        except Exception:
            seen[box] = set()
    while True:
        time.sleep(2)
        for box in INBOXES:
            try:
                for p in sorted(box.glob("*.json")):
                    if p.name in seen[box]:
                        continue
                    seen[box].add(p.name)
                    try:
                        data = json.loads(p.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    msg = data.get("summary") or data.get("doc") or "(empty)"
                    who = data.get("from", "?")
                    if who in terminals:
                        feed(who, who.upper(), msg)
                        play("receive", who)
                        if terminals[who].get("say"):
                            say_q.put(msg)
                        print(f"\n<< [{who}]: {msg}\n")
                    elif who not in ("skyrim", "companion", "calgor", "jokir"):
                        # not ours, not the game's — keep a trace, never lose it
                        feed("_unrouted", who.upper(), msg)
            except Exception:
                pass


# ── Hotkeys + config hot-reload ──────────────────────────────────────────────
# Slot keys are matched by a low-level FILTER hook, not add_hotkey: a physical
# Shift+Numpad press under NumLock arrives shift-STRIPPED as a bare nav key
# (Left/End/PgUp… with the keypad flag set), which add_hotkey's suppress
# matcher neither fires on nor swallows — the nav key then leaks into the
# focused window (Claude Code's TUI reacts to it). The filter recognises the
# clean form (Razer synthetic sends) AND the stripped form, consumes EVERY
# shift+numpad digit 0-9 — bound or not, since a leak is worse than a no-op —
# and passes every other keystroke through untouched.
# While a mic is hot, DELETE cancels the take (cancel tone confirms); when no
# mic is hot Delete passes through (it belongs to the Skyrim thought channel).
# suppress_hotkeys=false in [settings] reverts to plain leaky add_hotkey.
hotkey_handles = []
_KEYPAD_NAV_DIGIT = {"end": "1", "down": "2", "page down": "3", "left": "4",
                     "clear": "5", "right": "6", "home": "7", "up": "8",
                     "page up": "9", "insert": "0"}
# Shift+top-row digits arrive named as their SHIFTED SYMBOL ('!', '$'…), so a
# macro/mouse profile sending TOP-ROW combos needs this map (US layout). Only
# consulted when numpad_only = false — see _digit_of.
_SHIFT_SYMBOL_DIGIT = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
                       "^": "6", "&": "7", "*": "8", "(": "9", ")": "0"}
# Every keypad key that arrives wearing a navigation name. The digit map stops
# at 0-9; numpad-. adds 'delete'. Used only to spot the Shift+Numpad override.
_KEYPAD_NAV_NAMES = set(_KEYPAD_NAV_DIGIT) | {"delete"}
_mods = {"shift": False, "ctrl": False, "alt": False}
_last_shift_up = [0.0]
_slot_of_digit = {}          # digit -> codename, rebuilt on config (re)load
_filter_installed = [False]


VK_NUMLOCK = 0x90


def _numlock_on():
    return bool(ctypes.windll.user32.GetKeyState(VK_NUMLOCK) & 1)


def _nav_form(event):
    """True when this is a keypad key wearing its navigation name. 'delete' is
    numpad-. 's twin — it has no slot digit but Windows strips shift for it just
    the same, so the NumLock test has to recognise it or it slips the chord."""
    return (bool(getattr(event, "is_keypad", False))
            and (event.name or "") in _KEYPAD_NAV_NAMES)


def _digit_of(event):
    """Slot digit for this key event, or None.

    NUMPAD ONLY by default (numpad_only in [settings], hot-reloaded). Only a
    physical numpad key carries the keypad flag — the keyboard lib sets it from
    (scan_code, vk, is_extended), and the real nav cluster is the EXTENDED form
    of the same scan codes, so the flag separates them cleanly. The flag rides
    along in every guise the numpad shows up in: NumLock on -> 'num 1', NumLock
    off or shift-stripped -> the nav name ('end', 'clear', 'page up'…).

    Top-row digits never set it, so shift+1..0 stay free to type ! @ # $ %
    instead of being swallowed by the suppressing filter. Set numpad_only =
    false only if a macro/mouse profile has to send the top row — that costs
    you those symbols while the daemon runs."""
    if getattr(event, "is_keypad", False):
        name = (event.name or "").replace("num ", "")
        return _KEYPAD_NAV_DIGIT.get(name, name if name.isdigit() else None)
    if settings.get("numpad_only", True):
        return None
    name = event.name or ""
    if name.isdigit():
        return name
    return _SHIFT_SYMBOL_DIGIT.get(name)


def _key_filter(event):
    """Blocking filter: return False = swallow the event system-wide.
    Must stay FAST — real work is handed to a thread, never run inline."""
    name = event.name or ""
    if settings.get("debug_keys"):
        print(f"KEY {event.event_type:<4} {name!r:<14} "
              f"kp={int(bool(getattr(event, 'is_keypad', False)))} "
              f"sc={event.scan_code} mods={_mods}")
    if "shift" in name:
        _mods["shift"] = event.event_type == "down"
        if not _mods["shift"]:
            _last_shift_up[0] = time.time()
        return True
    if "ctrl" in name:
        _mods["ctrl"] = event.event_type == "down"
        return True
    if "alt" in name:
        _mods["alt"] = event.event_type == "down"
        return True
    if event.event_type != "down":
        return True
    if name == "delete" and recording.is_set():
        threading.Thread(target=cancel_recording, daemon=True).start()
        return False
    # Channel PTT chord (Chris sign-off 2026-08-17, ping-chat-hub H1):
    # ctrl+alt+numpad digit. shift+num is RETIRED — Chris felt the
    # interference (shifted numpad sends nav keys under NumLock), so shifted
    # numpad now passes through untouched, back to normal typing/nav.
    # cx-switch's chords both include shift (ctrl+shift+num / alt+shift+num),
    # so the claim is exactly ctrl AND alt AND NOT shift. Plain alt+num stays
    # free for Alt-code unicode entry.
    if _mods["ctrl"] and _mods["alt"] and not _mods["shift"]:
        if not getattr(event, "is_keypad", False):
            return True   # ctrl+alt + top-row/letters: not ours
        if (event.name or "").replace("num ", "") == "enter":
            # ctrl+alt+numpad-Enter: dictate the hub launcher's initial prompt
            # from anywhere — text lands in the hub's spawn draft and waits
            # behind the + button (Chris ask 2026-08-17)
            threading.Thread(target=spawn_draft_ptt, daemon=True).start()
            return False
        d = _digit_of(event)
        if d is None:
            return True   # ctrl+alt + numpad +/-/etc: not claimed
        cn = _slot_of_digit.get(d)
        if not cn:
            # The bound-or-not lesson survives the chord change: an unbound
            # digit inside OUR chord swallows + buzzes, never leaks a nav twin
            # into the focused TUI (stray keys mid-tool arm Claude Code's
            # "Backgrounding after the current tool finishes…").
            threading.Thread(target=unbound_slot, args=(d,), daemon=True).start()
            return False
        threading.Thread(target=toggle, args=(cn,), daemon=True).start()
        return False
    return True


def register_hotkeys():
    global hotkey_handles
    for h in hotkey_handles:
        try:
            keyboard.remove_hotkey(h)
        except Exception:
            pass
    hotkey_handles = []
    use_filter = settings.get("suppress_hotkeys", True)
    template = settings.get("slot_hotkey", "shift+num {n}")
    slot_keys = {template.format(n=d): d for d in "0123456789"}
    _slot_of_digit.clear()
    for t in terminals.values():
        cn = t["codename"]
        d = slot_keys.get(t["hotkey"])
        if use_filter and d is not None:
            _slot_of_digit[d] = cn
        else:
            hotkey_handles.append(
                keyboard.add_hotkey(t["hotkey"], toggle, args=(cn,),
                                    suppress=False))
    hotkey_handles.append(
        keyboard.add_hotkey(settings.get("cancel_hotkey", "ctrl+alt+x"),
                            cancel_recording, suppress=False))
    # focused-thread PTT — plain add_hotkey: space has no NumLock guises
    hotkey_handles.append(
        keyboard.add_hotkey("ctrl+shift+space", focused_ptt, suppress=False))
    # installed even with ZERO slots bound: an empty [slots] must mean "the
    # numpad does nothing", never "the numpad leaks into whatever has focus"
    if use_filter and not _filter_installed[0]:
        keyboard.hook(_key_filter, suppress=True)
        _filter_installed[0] = True
    elif not use_filter and _filter_installed[0]:
        try:
            keyboard.unhook(_key_filter)
        except Exception:
            pass
        _filter_installed[0] = False


def config_watcher():
    global settings, terminals
    last_mtime = CONFIG_PATH.stat().st_mtime
    while True:
        time.sleep(1)  # 1s so hot swaps retarget the PTT key near-instantly
        try:
            m = CONFIG_PATH.stat().st_mtime
            if m == last_mtime or recording.is_set():
                continue
            last_mtime = m
            new_settings, new_terminals = load_config()
            settings, terminals = new_settings, new_terminals
            build_tones(terminals)
            register_hotkeys()
            print(f"\n(cx.toml reloaded: "
                  + ", ".join(f"{t['hotkey']}->{t['codename']}" for t in terminals.values())
                  + ")")
        except Exception as e:
            print(f"\n(cx.toml reload FAILED, keeping previous config: {e})")


build_tones(terminals)
register_hotkeys()
threading.Thread(target=reply_watcher, daemon=True).start()
threading.Thread(target=config_watcher, daemon=True).start()
threading.Thread(target=chat_guard, daemon=True).start()


# ── Audio device picker (tray): swap mic/speaker from the menu any time ──────
AUDIO_DEVICES = {"Playback": [], "Recording": []}   # [{name, id, default}]
_PS_EXE = [None]   # resolved lazily: pwsh 7 first (has AudioDeviceCmdlets), then 5.1


def _ps_run(script, timeout=25):
    for exe in ([_PS_EXE[0]] if _PS_EXE[0] else ["pwsh", "powershell"]):
        try:
            r = subprocess.run(
                [exe, "-NoProfile", "-NonInteractive", "-Command",
                 "Import-Module AudioDeviceCmdlets; " + script],
                capture_output=True, text=True, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode == 0:
                _PS_EXE[0] = exe
                return r.stdout
        except Exception:
            continue
    return None


def refresh_audio_devices():
    out = _ps_run("Get-AudioDevice -List | Select-Object Type,Name,ID,Default"
                  " | ConvertTo-Json -Compress")
    if not out:
        return
    try:
        devs = json.loads(out)
        if isinstance(devs, dict):
            devs = [devs]
        fresh = {"Playback": [], "Recording": []}
        for d in devs:
            fresh.setdefault(d["Type"], []).append(
                {"name": d["Name"], "id": d["ID"], "default": bool(d["Default"])})
        AUDIO_DEVICES.update(fresh)
        _publish_audio_devices()
    except Exception as e:
        print(f"(audio device list failed: {e})")


def _publish_audio_devices():
    # The hub's settings panel reads this instead of paying its own 1.1s
    # pwsh Get-AudioDevice call (heron ruling 2026-08-18, fork
    # hub-settings-daemon-audio). Atomic replace so a mid-write read never
    # sees a torn file; stamped so a consumer can spot a dead publisher.
    try:
        out = CONFIG_PATH.parent / "cx" / "audio-devices.json"
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(
            {"ts": datetime.datetime.now().astimezone().isoformat(),
             "devices": AUDIO_DEVICES}), encoding="utf-8")
        tmp.replace(out)
    except Exception as e:
        print(f"(audio device publish failed: {e})")


def _audio_device_watcher():
    while True:
        refresh_audio_devices()
        time.sleep(30)


def restart_self():
    """Mic changed: the input stream binds the default device once at open,
    so die and respawn (the singleton mutex frees on process exit; the cmd
    helper waits 2s before starting the successor)."""
    print("(mic changed — restarting cx-ptt to rebind the input stream)")
    subprocess.Popen(
        'cmd /c timeout /t 2 /nobreak >nul & start "" "%s" "%s"'
        % (sys.executable, Path(__file__).resolve()),
        creationflags=subprocess.CREATE_NO_WINDOW
        | subprocess.CREATE_NEW_PROCESS_GROUP)
    os._exit(0)


def set_audio_device(dev, is_mic):
    if _ps_run("Set-AudioDevice -ID '%s' | Out-Null" % dev["id"]) is None:
        print(f"(failed to switch audio device to {dev['name']})")
        return
    print(f"({'mic' if is_mic else 'speaker'} -> {dev['name']})")
    if is_mic:
        restart_self()
    refresh_audio_devices()


threading.Thread(target=_audio_device_watcher, daemon=True).start()


# ── Tray: consoles live hidden, shown on demand from the tray icon ───────────
def _toggle_hwnd(hwnd):
    u = ctypes.windll.user32
    if u.IsWindowVisible(hwnd):
        u.ShowWindow(hwnd, 0)   # SW_HIDE
    else:
        u.ShowWindow(hwnd, 9)   # SW_RESTORE
        u.SetForegroundWindow(hwnd)


def start_tray():
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception as e:
        print(f"(no tray: {e} — consoles stay visible)")
        return False

    img = Image.new("RGB", (32, 32), (20, 14, 8))
    ImageDraw.Draw(img).ellipse((4, 4, 28, 28), fill=(255, 176, 59))

    def toggle_chat(cn):
        def cb(icon, item):
            h = ctypes.windll.user32.FindWindowW(None, f"cx-chat {cn}")
            if h:
                _toggle_hwnd(h)
        return cb

    def toggle_daemon(icon, item):
        h = ctypes.windll.kernel32.GetConsoleWindow()
        if h:
            _toggle_hwnd(h)

    def do_exit(icon, item):
        icon.stop()
        os._exit(0)

    def pick_device(dev, is_mic):
        def cb(icon, item):
            # threaded: a mic pick blocks on PowerShell then kills the process
            threading.Thread(target=set_audio_device, args=(dev, is_mic),
                             daemon=True).start()
        return cb

    def device_submenu(kind, is_mic):
        def items():
            if not AUDIO_DEVICES[kind]:
                yield pystray.MenuItem("(scanning devices…)", None, enabled=False)
            for d in AUDIO_DEVICES[kind]:
                yield pystray.MenuItem(d["name"], pick_device(d, is_mic),
                                       radio=True,
                                       checked=lambda item, _d=d: _d["default"])
        return pystray.Menu(items)

    def menu_items():
        # Chat entries removed 2026-08-17 (Chris): the hub is the chat surface
        # now, so the tray stays minimal - mic, speaker, console, exit.
        cur_mic = next((d["name"] for d in AUDIO_DEVICES["Recording"] if d["default"]), "?")
        cur_spk = next((d["name"] for d in AUDIO_DEVICES["Playback"] if d["default"]), "?")
        yield pystray.MenuItem(f"Mic: {cur_mic[:30]}", device_submenu("Recording", True))
        yield pystray.MenuItem(f"Speaker: {cur_spk[:30]}", device_submenu("Playback", False))
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Daemon console", toggle_daemon)
        yield pystray.MenuItem("Exit cx-ptt", do_exit)

    icon = pystray.Icon("cx", img, "cx work channel",
                        menu=pystray.Menu(menu_items))
    threading.Thread(target=icon.run, daemon=True).start()
    return True


import os

if settings.get("tray", True) and start_tray():
    time.sleep(0.5)
    _own = ctypes.windll.kernel32.GetConsoleWindow()
    if _own:
        ctypes.windll.user32.ShowWindow(_own, 0)

with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                    callback=audio_callback):
    print("Work channel live — press-toggle hotkeys per terminal:")
    for t in terminals.values():
        print(f"  {t['hotkey']:<16} -> {t['codename']}  (side={t['side']})")
    print(f"  {settings.get('cancel_hotkey', 'ctrl+alt+x'):<16} -> cancel recording")
    print(f"Config hot-reloads from {CONFIG_PATH} (~3s).")
    print(f"Feeds -> {FEED_DIR}\\<codename>.jsonl (cx-chat windows tail these)")
    keyboard.wait()
