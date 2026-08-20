"""Dictate v2 — push-to-talk dictation with live streaming overlay + cleanup pass.

Tap the hotkey: an overlay appears and your words stream into it in real time
(nemotron-speech-streaming 160ms — DISPLAY ONLY, its text is never pasted).
Tap again: the full take is re-decoded by parakeet-unified (the accurate one),
the overlay shows the cleaned line, and it's pasted at the cursor.

Design rule (learned from the over-tweaked Dayboard STT): the streaming model is
staging/feedback only. No endpoint detection, no segment accumulators, no tail
races — the pasted text always comes from ONE offline pass over the whole take.

pythonw gotchas: print() is a silent no-op under pythonw (sys.stdout is None),
so log() writes dictate.log — that file is the only way to see what happened.
The v1 "not working" culprit was DUPLICATE INSTANCES: relaunching dictate.cmd
left old hidden pythonw processes alive, every hotkey tap toggled all of them
(starts cancelling stops, double-pastes). The mutex below makes that impossible.

Run:  pythonw dictate.py            (hidden: tray + overlay — normal use)
      python  dictate.py            (console too — handy while tuning)
      python  dictate.py --selftest (prove both models load + transcribe)
"""

import os

# ============================== CONFIG (Claude edits) ==============================
HOTKEY          = "ctrl+alt+d"  # TAP once = start, TAP again = stop+paste. Not hold-to-talk.
                                # (was ctrl+alt+space; suppress-hook for it broke 2026-08-11)
DEBOUNCE_S      = 0.6         # swallow keyboard auto-repeat while the combo is held
SAMPLE_RATE     = 16000
# Model locations. Every one is an env var the launcher `ping-hub install`
# writes, with a derivation from the user's home as the fallback for a
# hand-started daemon. None of them may be a literal home directory: this file
# ships inside the package and runs on machines that are not this one.
_HERE_DIR = os.path.dirname(os.path.abspath(__file__))


def _model(env, *parts):
    v = os.environ.get(env)
    if v:
        return v
    beside = os.path.join(_HERE_DIR, *parts)      # provisioned next to us
    if os.path.isdir(beside):
        return beside
    return os.path.join(os.path.expanduser("~"), ".ping-hub", "stt", *parts)


STREAM_MODEL    = _model("PING_HUB_STREAM_MODEL", "stream")
CLEAN_MODEL     = _model("PING_HUB_STT_MODEL", "model")
PUNCT_MODEL     = _model("PING_HUB_PUNCT_MODEL", "punct")
CLEANUP         = True        # startup default; toggle live from the tray menu.
                              # On  = paste the unified model's accurate punctuated text.
                              # Off = paste the raw streaming text (punctuated) instantly.
HOLD_TO_TALK    = False       # startup default; toggle live from the tray menu.
                              # On  = walkie-talkie: hold the hotkey, release to finish.
                              # Off = tap once to start, talk freely, tap again.
NUM_THREADS     = 4           # measured: 4 threads = RTF 0.70, 8 threads = 0.95 (contention)
# Segmentation + failure rescue for the cleanup pass live in stt_decode.py — the
# SAME ladder the ping-hub mic (cx-ptt.py) walks. Never fork it back in here.
FIXUPS          = True        # apply the hub's canonical replacement list to the paste
FAILED_DIR      = ""       # resolved by _failed_dir() below; see it for why
PASTE           = True        # True = clipboard paste (Ctrl+V). False = type char-by-char.
RESTORE_CLIP    = False       # keep the transcript on the clipboard after pasting —
                              # manual Ctrl+V works as backup if the auto-paste misses.
TRAILING_SPACE  = True        # add a space after each dictation so takes don't run together
CAPITALIZE      = True        # capitalize the first letter of each dictation
BEEP            = True        # audio cue on start/stop
MIN_SECONDS     = 0.3         # ignore accidental sub-0.3s taps
# --- audio source: laptop mic over the network (VBAN), no virtual audio devices ---
VBAN_SOURCE     = False       # True = capture the laptop's VBAN stream directly (bypass mic).
                              # False = default-mic capture. The default mic is "CABLE Output",
                              # which carries the laptop headset via vban-bridge.py -> CABLE Input.
VBAN_NAME       = "LaptopMic" # stream name the laptop's VoiceMeeter sends
VBAN_PORT       = 6980        # laptop sends to 192.168.0.200:6980 -> NETGEAR forwards here
# --- overlay: chat-widget style, bottom-right of the focused monitor ---
OV_WIDTH        = 400
OV_MARGIN       = 16          # gap from the work-area corner
OV_BG           = "#2a221c"   # solid dark warm brown (warm/low-blue for eye comfort)
OV_BORDER       = "#4a3a2c"
OV_STAGED_FG    = "#c9b18f"   # muted tan — live streaming text
OV_FINAL_FG     = "#f2ddb6"   # warm cream — cleaned final text
OV_STATUS_FG    = "#e07b54"   # warm coral — status line
OV_FONT         = ("Segoe UI", 12)
OV_STATUS_FONT  = ("Segoe UI", 9)
OV_TAIL_CHARS   = 700         # how much recent text stays visible while streaming
# ==================================================================================

import sys
import time
import queue
import ctypes
import threading
from ctypes import wintypes

import numpy as np
import sounddevice as sd
import sherpa_onnx
import keyboard
import winsound
import pyperclip

import stt_decode          # the ping-hub decode ladder (shared with cx-ptt.py)
import stt_fixups          # the hub's canonical replacement list
import stt_hubcfg          # hub-governed settings, state and history
from stt_decode import write_wav

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "dictate.log")


def log(msg):
    """pythonw-safe logging: file always, console when one exists."""
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if sys.stdout is not None:
        try:
            print(line)
        except OSError:
            pass


# ------------------------------ models ------------------------------

stream_rec = None   # OnlineRecognizer  (nemotron streaming — display only)
clean_rec = None    # OfflineRecognizer (parakeet unified — the text that pastes)
punct = None        # OnlinePunctuation (cases+punctuates the live display text)
models_ready = threading.Event()
# Live settings. The constants above are only the fallback for a machine
# with no hub store yet — the hub owns these, the tray writes back to it, and
# refresh() re-reads before every take so a change in the app takes effect on
# the next thing said rather than the next restart.
settings = {"cleanup": CLEANUP, "hold": HOLD_TO_TALK, "history": True,
            "hotkey": HOTKEY, "history_keep": 2000}


def refresh_settings():
    """Pull the hub store over the live dict. Never raises: a settings read
    that fails must not be able to stop dictation."""
    try:
        doc = stt_hubcfg.settings()
    except Exception as e:
        log(f"settings unreadable ({e!r}) — keeping current")
        return settings
    settings["cleanup"] = bool(doc.get("cleanup", CLEANUP))
    settings["hold"] = doc.get("mode", "tap") == "hold"
    settings["history"] = bool(doc.get("history", True))
    settings["history_keep"] = int(doc.get("history_keep", 2000))
    settings["hotkey"] = doc.get("hotkey", HOTKEY)
    return settings


def push_settings(**changes):
    """Tray flip -> the hub store, so the app and the tray never disagree."""
    try:
        stt_hubcfg.save_settings(**changes)
    except Exception as e:
        log(f"could not write settings to the hub store: {e!r}")


def load_models():
    global stream_rec, clean_rec
    t0 = time.time()
    stream_rec = sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=os.path.join(STREAM_MODEL, "tokens.txt"),
        encoder=os.path.join(STREAM_MODEL, "encoder.int8.onnx"),
        decoder=os.path.join(STREAM_MODEL, "decoder.int8.onnx"),
        joiner=os.path.join(STREAM_MODEL, "joiner.int8.onnx"),
        num_threads=NUM_THREADS,
        sample_rate=SAMPLE_RATE,
        feature_dim=80,
    )
    log(f"streaming model loaded in {time.time()-t0:.1f}s")
    t0 = time.time()
    clean_rec = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=os.path.join(CLEAN_MODEL, "encoder.int8.onnx"),
        decoder=os.path.join(CLEAN_MODEL, "decoder.int8.onnx"),
        joiner=os.path.join(CLEAN_MODEL, "joiner.int8.onnx"),
        tokens=os.path.join(CLEAN_MODEL, "tokens.txt"),
        num_threads=NUM_THREADS,
        model_type="nemo_transducer",
    )
    log(f"cleanup model loaded in {time.time()-t0:.1f}s")
    global punct
    try:
        mc = sherpa_onnx.OnlinePunctuationModelConfig(
            cnn_bilstm=os.path.join(PUNCT_MODEL, "model.int8.onnx"),
            bpe_vocab=os.path.join(PUNCT_MODEL, "bpe.vocab"),
            num_threads=1)
        punct = sherpa_onnx.OnlinePunctuation(
            sherpa_onnx.OnlinePunctuationConfig(model_config=mc))
        log("punctuation model loaded")
    except Exception as e:
        log(f"punctuation model unavailable ({e!r}) — live text stays raw")
    models_ready.set()


def punct_text(t):
    if punct is None or not t:
        return t
    try:
        return punct.add_punctuation_with_case(t)
    except Exception:
        return t


def _decode(seg):
    """One raw pass at the unified model. stt_decode owns everything above it."""
    s = clean_rec.create_stream()
    s.accept_waveform(SAMPLE_RATE, seg)
    clean_rec.decode_stream(s)
    return s.result.text.strip()


def clean_decode(audio):
    """Accurate pass over the whole take via the ping-hub ladder.

    parakeet-0.6b fails SILENTLY - a piece it chokes on returns "" rather than
    an error, and it chokes on long spans, quiet passages and unlucky cuts.
    The old local splitter dropped those empty pieces without a word, which is
    how a 46.5s paragraph pasted as one sentence (dictate.log, 2026-08-20).
    stt_decode escalates instead: whole take -> ~20s silence-cut pieces ->
    8/4/2.5s rescue windows -> gain-normalised retry. Returns (text, lost).
    """
    return stt_decode.transcribe(_decode, audio)


# ------------------------------ paste ------------------------------

def emit(text):
    if not text:
        return
    if CAPITALIZE:
        text = text[0].upper() + text[1:]
    if TRAILING_SPACE:
        text += " "
    if PASTE:
        prev = ""
        if RESTORE_CLIP:
            try:
                prev = pyperclip.paste()
            except Exception:
                prev = ""
        pyperclip.copy(text)
        time.sleep(0.15)   # let the clipboard set + focus settle (Moonlight round-trip)
        for mod in ("alt", "shift", "windows"):
            try:
                keyboard.release(mod)   # fingers may still be on the hotkey combo
            except Exception:
                pass
        try:
            u32 = ctypes.windll.user32
            buf = ctypes.create_unicode_buffer(128)
            u32.GetWindowTextW(u32.GetForegroundWindow(), buf, 128)
            log(f"pasting into {buf.value!r}")
        except Exception:
            pass
        keyboard.send("ctrl+v")
        if RESTORE_CLIP:
            time.sleep(0.6)  # let the paste land before restoring
            try:
                pyperclip.copy(prev)
            except Exception:
                pass
    else:
        keyboard.write(text, delay=0.005)


def foreground_title():
    """Whatever window is about to receive the paste, for the history row."""
    try:
        u32 = ctypes.windll.user32
        buf = ctypes.create_unicode_buffer(128)
        u32.GetWindowTextW(u32.GetForegroundWindow(), buf, 128)
        return buf.value or ""
    except Exception:
        return ""


def record_history(text, seconds, lost):
    """File the transcript with the hub. Wrapped whole: a failed history write
    must never cost Chris the paste that already succeeded."""
    if not settings.get("history", True):
        return
    try:
        stt_hubcfg.append_history(text, seconds, lost=lost,
                                  target=foreground_title())
        keep = int(settings.get("history_keep", 2000))
        # rotate rarely, not on every take: it rewrites the whole file
        if _takes[0] % 50 == 0:
            dropped = stt_hubcfg.rotate_history(keep)
            if dropped:
                log(f"history rotated: dropped {dropped} oldest")
        _takes[0] += 1
    except Exception as e:
        log(f"history write failed: {e!r}")


_takes = [1]


def _failed_dir():
    """Where a take the model could not read is kept.

    Beside cx-ptt's own salvage folder on purpose: `stt-fix <name>` recovers
    either mic's take and there is no reason for two piles. Env first, then
    derived — never a literal home.
    """
    from pathlib import Path
    if FAILED_DIR:
        return Path(FAILED_DIR)
    cx = os.environ.get("PING_HUB_CX_DIR")
    if cx:
        return Path(cx) / "stt-failed"
    gbl = os.environ.get("PING_HUB_BASE_GBL")
    root = Path(gbl) if gbl else Path.home() / ".base-gbl"
    return root / "cx" / "stt-failed"


def salvage(audio):
    """Keep a take the model could not read, so no breath is ever spent twice."""
    name = f"take_{time.strftime('%Y%m%d_%H%M%S')}.wav"
    return str(write_wav(_failed_dir() / name, audio))


# ------------------------------ recording pipeline ------------------------------

ui_q = queue.Queue()        # messages to the tk overlay
audio_q = queue.Queue()     # mic chunks -> decoder thread
_recording = threading.Event()
_busy = threading.Event()   # whole take in flight (record + cleanup + paste)


def audio_callback(indata, _frames, _time, _status):
    if _recording.is_set():
        audio_q.put(indata[:, 0].copy())


# VBAN sample-rate index table (protocol spec order)
_VBAN_SR = (6000, 12000, 24000, 48000, 96000, 192000, 384000,
            8000, 16000, 32000, 64000, 128000, 256000, 512000,
            11025, 22050, 44100, 88200, 176400, 352800, 705600)


def vban_listener():
    """Feed audio_q from the laptop's VBAN mic stream instead of a local mic.

    Accepts int16 PCM audio packets named VBAN_NAME on VBAN_PORT, downmixes to
    mono, resamples to SAMPLE_RATE, and pushes ~100 ms float32 chunks while a
    take is recording — identical contract to audio_callback.
    """
    import socket
    want = VBAN_NAME.encode().ljust(16, b"\x00")
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            sock.bind(("0.0.0.0", VBAN_PORT))
            log(f"vban source: udp {VBAN_PORT}, stream '{VBAN_NAME}'")
            buf = np.zeros(0, np.float32)
            while True:
                data, _addr = sock.recvfrom(2048)
                if len(data) < 28 or data[:4] != b"VBAN" or data[8:24] != want:
                    continue
                if (data[4] & 0xE0) != 0 or (data[7] & 0x07) != 1:
                    continue                       # not int16 PCM audio
                if not _recording.is_set():
                    buf = buf[:0]
                    continue
                sr = _VBAN_SR[data[4] & 0x1F]
                nbc = data[6] + 1
                pcm = np.frombuffer(data, np.int16, offset=28)
                if nbc > 1:
                    pcm = pcm[: (len(pcm) // nbc) * nbc].reshape(-1, nbc).mean(axis=1)
                f = pcm.astype(np.float32) / 32768.0
                if sr != SAMPLE_RATE:
                    if sr % SAMPLE_RATE == 0:      # e.g. 48000 -> 16000: average groups
                        step = sr // SAMPLE_RATE
                        n = (len(f) // step) * step
                        f = f[:n].reshape(-1, step).mean(axis=1)
                    else:                          # non-integer ratio: nearest-sample
                        idx = (np.arange(int(len(f) * SAMPLE_RATE / sr)) * sr / SAMPLE_RATE)
                        f = f[idx.astype(np.int64)]
                buf = np.concatenate([buf, f])
                if len(buf) >= SAMPLE_RATE // 10:
                    audio_q.put(buf)
                    buf = np.zeros(0, np.float32)
        except Exception as e:
            log(f"vban source error: {e!r}; retrying in 3s")
            time.sleep(3)


def decoder_loop(tray):
    """Per-take thread: live partials while recording, then cleanup + paste."""
    capture = []
    s = stream_rec.create_stream()
    last = ""
    try:
        while _recording.is_set() or not audio_q.empty():
            try:
                chunk = audio_q.get(timeout=0.1)
            except queue.Empty:
                continue
            capture.append(chunk)
            s.accept_waveform(SAMPLE_RATE, chunk)
            while stream_rec.is_ready(s):
                stream_rec.decode_stream(s)
            p = stream_rec.get_result(s)
            if p and p != last:
                last = p
                # punctuate only the visible tail — keeps the call ~15ms
                ui_q.put(("text", punct_text(p[-2 * OV_TAIL_CHARS:])))
        # flush the streaming tail so the last words show while cleanup runs
        s.accept_waveform(SAMPLE_RATE, np.zeros(int(SAMPLE_RATE * 0.8), np.float32))
        s.input_finished()
        while stream_rec.is_ready(s):
            stream_rec.decode_stream(s)
        p = stream_rec.get_result(s)
        if p:
            last = p
            ui_q.put(("text", punct_text(p[-2 * OV_TAIL_CHARS:])))

        if not capture:
            ui_q.put(("hide", None))
            return
        audio = np.concatenate(capture)
        dur = len(audio) / SAMPLE_RATE
        if dur < MIN_SECONDS:
            log(f"(too short: {dur:.2f}s)")
            ui_q.put(("hide", None))
            return
        lost = 0
        if settings["cleanup"]:
            ui_q.put(("state", "cleaning"))
            if tray:
                tray.set_state("cleaning")
            t0 = time.time()
            text, lost = clean_decode(audio)
            log(f"[{dur:.1f}s audio -> {time.time()-t0:.2f}s cleanup] {text!r}")
            if lost:
                # the ladder already tried everything: say so in the paste
                # rather than letting the words vanish, and keep the audio
                wav = salvage(audio)
                text += f" [{lost} piece{'s' if lost > 1 else ''} would not decode]"
                log(f"({lost} piece(s) unrecoverable - audio at {wav}; "
                    f"retry by hand with: stt-fix {os.path.basename(wav)})")
            elif not text:
                wav = salvage(audio)
                text = f"[STT recovered nothing from a {dur:.0f}s take]"
                log(f"(decode + rescue both empty - audio salvaged to {wav})")
        else:
            text = punct_text(last.strip())
            log(f"[{dur:.1f}s audio, cleanup off] {text!r}")
        rule = None
        if FIXUPS and text:
            try:
                rule = stt_fixups.consume_rule(text)
            except Exception as e:
                log(f"inline rule failed: {e!r}")
            if rule:
                log(f"word fix {rule['action']}: {rule['from']!r} -> {rule['to']!r}")
                ui_q.put(("rule", f"{rule['action']}: {rule['from']} \u2192 {rule['to']}"))
            text = stt_fixups.fix_transcript(text)
        if text:
            if not rule:
                ui_q.put(("final", text))
            emit(text)
            record_history(text, dur, lost)
        else:
            ui_q.put(("hide", None))
    except Exception as e:
        log(f"decoder error: {e!r}")
        ui_q.put(("hide", None))
    finally:
        if tray:
            tray.set_state("idle")
        _busy.clear()


def start_take(tray):
    if not models_ready.is_set():
        if BEEP:
            winsound.Beep(220, 120)
        ui_q.put(("flash", "models still loading\u2026"))
        return
    if _busy.is_set():
        if BEEP:
            winsound.Beep(220, 120)
        return
    _busy.set()
    refresh_settings()           # the app may have changed them since the last take
    while not audio_q.empty():   # drop anything stale
        audio_q.get_nowait()
    _recording.set()
    if BEEP:
        winsound.Beep(880, 90)
    if tray:
        tray.set_state("listening")
    ui_q.put(("state", "listening"))
    threading.Thread(target=decoder_loop, args=(tray,), daemon=True).start()
    log("[listening...]")


def stop_take():
    _recording.clear()
    if BEEP:
        winsound.Beep(440, 90)


def make_toggle(tray):
    last_evt = [0.0]
    # the combo's non-modifier key, from whatever is actually bound
    release_key = (settings.get("hotkey") or HOTKEY).split("+")[-1]

    def watch_release():
        # hold mode: the release of the main key ends the take
        time.sleep(0.05)
        while keyboard.is_pressed(release_key):
            time.sleep(0.03)
        if _recording.is_set():
            stop_take()

    def toggle():
        log("hotkey received")
        # Holding the combo streams auto-repeat events every ~30ms; each one
        # refreshes the timestamp, so only the first press of a hold fires.
        now = time.monotonic()
        prev = last_evt[0]
        last_evt[0] = now
        if now - prev < DEBOUNCE_S:
            return
        if _recording.is_set():
            if not settings["hold"]:
                stop_take()   # hold mode: watch_release owns stopping
        elif not _busy.is_set():
            start_take(tray)
            if settings["hold"]:
                threading.Thread(target=watch_release, daemon=True).start()
        else:
            if BEEP:
                winsound.Beep(220, 120)   # still cleaning the previous take
    return toggle


# ------------------------------ tray ------------------------------

def build_tray():
    import pystray
    from PIL import Image, ImageDraw

    COLORS = {
        "loading":   (120, 120, 120, 255),
        "idle":      (170, 150, 120, 255),
        "listening": (224, 123, 84, 255),
        "cleaning":  (217, 164, 65, 255),
    }

    def icon_img(state):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        c = COLORS.get(state, COLORS["idle"])
        d.ellipse((14, 8, 50, 44), fill=c)       # mic head
        d.rectangle((28, 40, 36, 52), fill=c)    # stem
        d.rectangle((22, 52, 42, 56), fill=c)    # base
        return img

    icon = pystray.Icon("dictate", icon_img("loading"), "Dictate (loading models\u2026)")

    class Tray:
        def set_state(self, state):
            icon.icon = icon_img(state)
            icon.title = {
                "loading": "Dictate (loading models\u2026)",
                "idle": f"Dictate — {HOTKEY} to talk",
                "listening": "Dictate (LISTENING)",
                "cleaning": "Dictate (cleaning up\u2026)",
            }.get(state, "Dictate")

    tray = Tray()

    def flip_cleanup(_):
        settings["cleanup"] = not settings["cleanup"]
        push_settings(cleanup=settings["cleanup"])
        log(f"cleanup pass {'ON' if settings['cleanup'] else 'OFF'}")

    def flip_hold(_):
        settings["hold"] = not settings["hold"]
        log(f"hold-to-talk {'ON' if settings['hold'] else 'OFF (tap-toggle)'}")

    icon.menu = pystray.Menu(
        pystray.MenuItem(lambda i: "\u25a0 Stop" if _recording.is_set() else "\u25cf Start",
                         lambda i: make_toggle(tray)()),
        pystray.MenuItem("Cleanup pass", flip_cleanup,
                         checked=lambda i: settings["cleanup"]),
        pystray.MenuItem("Hold-to-talk", flip_hold,
                         checked=lambda i: settings["hold"]),
        pystray.MenuItem("Quit", lambda i: ui_q.put(("quit", None))),
    )
    return icon, tray


# ------------------------------ overlay (tk, main thread) ------------------------------

def run_ui(icon, tray):
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()

    win = tk.Toplevel(root)
    win.withdraw()
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg=OV_BORDER)
    inner = tk.Frame(win, bg=OV_BG)
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    status = tk.Label(inner, text="", font=OV_STATUS_FONT, fg=OV_STATUS_FG,
                      bg=OV_BG, anchor="w")
    status.pack(fill="x", padx=14, pady=(8, 0))
    label = tk.Label(inner, text="", font=OV_FONT, fg=OV_STAGED_FG, bg=OV_BG,
                     wraplength=OV_WIDTH - 60, justify="left", anchor="w")
    label.pack(fill="x", padx=14, pady=(2, 10))

    hide_job = [None]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    def work_area():
        """Work area (taskbar excluded) of the monitor holding the focused
        window — that's where the paste lands, so the overlay goes there."""
        try:
            u32 = ctypes.windll.user32
            mon = u32.MonitorFromWindow(u32.GetForegroundWindow(), 2)  # DEFAULTTONEAREST
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if u32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                r = mi.rcWork
                return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
        return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()

    def place():
        win.update_idletasks()
        h = win.winfo_reqheight()
        _left, _top, right, bottom = work_area()
        win.geometry(f"{OV_WIDTH}x{h}+{right - OV_WIDTH - OV_MARGIN}+{bottom - h - OV_MARGIN}")

    def no_activate():
        """WS_EX_NOACTIVATE: the widget can NEVER take focus — without this,
        deiconify could steal focus and Ctrl+V pasted into the overlay itself."""
        try:
            u32 = ctypes.windll.user32
            h = u32.GetParent(win.winfo_id()) or win.winfo_id()
            GWL_EXSTYLE = -20
            ex = u32.GetWindowLongW(h, GWL_EXSTYLE)
            u32.SetWindowLongW(h, GWL_EXSTYLE, ex | 0x08000000 | 0x00000080)  # NOACTIVATE|TOOLWINDOW
        except Exception:
            pass

    def show():
        if hide_job[0]:
            root.after_cancel(hide_job[0])
            hide_job[0] = None
        place()
        no_activate()
        win.deiconify()

    def hide():
        win.withdraw()
        label.config(text="")

    def poll():
        try:
            while True:
                kind, val = ui_q.get_nowait()
                if kind == "state" and val == "listening":
                    hint = ("release" if settings["hold"]
                            else settings.get("hotkey") or HOTKEY)
                    status.config(text=f"\u25cf listening \u2014 {hint} to finish")
                    label.config(text="", fg=OV_STAGED_FG)
                    show()
                elif kind == "state" and val == "cleaning":
                    status.config(text="\u25cc cleaning up\u2026")
                    show()
                elif kind == "text":
                    t = val
                    if len(t) > OV_TAIL_CHARS:
                        t = "\u2026" + t[-OV_TAIL_CHARS:]
                    label.config(text=t, fg=OV_STAGED_FG)
                    show()
                elif kind == "final":
                    t = val
                    if len(t) > OV_TAIL_CHARS:
                        t = "\u2026" + t[-OV_TAIL_CHARS:]
                    status.config(text="\u2713 pasted")
                    label.config(text=t, fg=OV_FINAL_FG)
                    show()
                    hide_job[0] = root.after(1600, hide)
                elif kind == "rule":
                    status.config(text="\u2713 word fix saved")
                    label.config(text=val, fg=OV_FINAL_FG)
                    show()
                    hide_job[0] = root.after(2600, hide)
                elif kind == "flash":
                    status.config(text=val)
                    label.config(text="", fg=OV_STAGED_FG)
                    show()
                    hide_job[0] = root.after(1200, hide)
                elif kind == "hide":
                    hide()
                elif kind == "quit":
                    root.destroy()
                    return
        except queue.Empty:
            pass
        root.after(40, poll)

    root.after(40, poll)
    root.mainloop()
    if icon:
        icon.stop()


# ------------------------------ selftest ------------------------------

def selftest():
    load_models()
    wav = os.path.join(STREAM_MODEL, "0.wav")
    if os.path.exists(wav):
        import wave
        with wave.open(wav) as w:
            sr = w.getframerate()
            audio = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0
        if sr != SAMPLE_RATE:
            log(f"note: test wav is {sr}Hz")
    else:
        sr, audio = SAMPLE_RATE, np.zeros(SAMPLE_RATE * 3, np.float32)
    dur = len(audio) / sr
    s = stream_rec.create_stream()
    chunk = int(sr * 0.16)
    t0 = time.time()
    for i in range(0, len(audio), chunk):
        s.accept_waveform(sr, audio[i:i + chunk])
        while stream_rec.is_ready(s):
            stream_rec.decode_stream(s)
    s.input_finished()
    while stream_rec.is_ready(s):
        stream_rec.decode_stream(s)
    log(f"SELFTEST streaming: {dur:.1f}s in {time.time()-t0:.2f}s -> {stream_rec.get_result(s)!r}")
    t0 = time.time()
    text = clean_decode(audio)[0] if sr == SAMPLE_RATE else "(skipped: wav rate)"
    log(f"SELFTEST cleanup:   {dur:.1f}s in {time.time()-t0:.2f}s -> {text!r}")
    log("SELFTEST OK")


# ------------------------------ main ------------------------------

def main():
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, "DictateSTT_singleton")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        log("another Dictate is already running — exiting (kill pythonw to restart)")
        try:
            winsound.Beep(220, 250)
        except Exception:
            pass
        sys.exit(0)
    try:
        os.remove(LOG_PATH)
    except OSError:
        pass
    refresh_settings()
    log(f"dictate v2 starting (hotkey {settings['hotkey']}, "
        f"mode {'hold' if settings['hold'] else 'tap'})")

    icon, tray = None, None
    try:
        icon, tray = build_tray()
        icon.run_detached()
    except Exception as e:
        log(f"tray unavailable ({e!r}) — overlay only")

    def boot():
        try:
            load_models()
            if tray:
                tray.set_state("idle")
            log(f"ready — {settings['hotkey']}")
        except Exception as e:
            log(f"MODEL LOAD FAILED: {e!r}")
            ui_q.put(("flash", f"model load failed: {e}"))

    threading.Thread(target=boot, daemon=True).start()

    # suppress=True: without it Alt+Space reaches the focused window and pops
    # its system menu (the "right-click menu in the top-left corner").
    # Native RegisterHotKey (OS-level, reliable) with keyboard-lib fallback.
    def native_hotkey_loop(cb):
        """Bind whatever combo the hub store names, and report what happened.

        The hotkey used to be three constants inlined here (ctrl, alt, 0x44),
        so `HOTKEY` above was only ever a log string and the fallback's
        argument — changing it changed neither of the two paths that matter.
        It is parsed now, and the result is written to the hub state file
        because a REQUESTED hotkey and a REGISTERED one are different facts:
        RegisterHotKey fails when another program already owns the combo.
        """
        combo = settings.get("hotkey") or HOTKEY
        hk = stt_hubcfg.parse_hotkey(combo)
        if hk is None:
            log(f"unbindable hotkey {combo!r} — falling back to {HOTKEY}")
            combo, hk = HOTKEY, stt_hubcfg.parse_hotkey(HOTKEY)
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, hk["flags"], hk["vk"]):
            log(f"RegisterHotKey FAILED for {combo} (combo owned elsewhere?) "
                f"— keyboard-lib fallback")
            try:
                keyboard.add_hotkey(combo, cb, suppress=False)
                stt_hubcfg.write_state(hotkey=combo, registered=True,
                                       method="keyboard-lib",
                                       mode=settings.get("hold") and "hold" or "tap",
                                       cleanup=settings.get("cleanup", True))
            except Exception as e:
                log(f"fallback hotkey ALSO failed: {e!r} — no hotkey is live")
                stt_hubcfg.write_state(hotkey=combo, registered=False,
                                       method="none", detail=repr(e))
            return
        log(f"native hotkey registered: {combo}")
        stt_hubcfg.write_state(hotkey=combo, registered=True, method="native",
                               mode=settings.get("hold") and "hold" or "tap",
                               cleanup=settings.get("cleanup", True))
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                cb()
    threading.Thread(target=native_hotkey_loop, args=(make_toggle(tray),), daemon=True).start()
    if VBAN_SOURCE:
        threading.Thread(target=vban_listener, daemon=True).start()
        stream = None
        log("audio source: VBAN network stream (laptop headset)")
    else:
        stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                callback=audio_callback)
        stream.start()
    try:
        run_ui(icon, tray)
    finally:
        _recording.clear()
        if stream:
            stream.stop()
            stream.close()
        keyboard.unhook_all()
        log("dictate stopped")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    main()
