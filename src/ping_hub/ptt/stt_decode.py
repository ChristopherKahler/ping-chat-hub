"""stt_decode — the shared parakeet decode ladder.

Imported by cx-ptt.py (live daemon) and stt-fix.py (re-run a saved take), so
recovering a take by hand walks exactly the same path the daemon already
walked. Anything that changes decode behaviour belongs HERE, not in a copy.

parakeet-0.6b fails silently: a piece it chokes on returns "" rather than an
error, and it chokes on long spans, quiet passages and unlucky cuts. So a
single decode attempt is never treated as the answer — an empty result over
audible audio escalates:

    whole take (<=28s)
      -> ~20s pieces cut at silence valleys
        -> failed piece only: 8s -> 4s -> 2.5s pieces
          -> and again, gain-normalised, for takes recorded too quiet

Only the failure path pays for this; a clean take decodes once and returns.
"""
import wave

import numpy as np

SAMPLE_RATE = 16000
LONG_TAKE = 28          # seconds; above this, never try the take whole
CHUNK_S = 20            # seconds per normal piece
RESCUE_WINDOWS = (8, 4, 2.5)   # seconds per piece, tried in order on a failure
LOUD_RMS = 0.005        # above this, an empty decode means LOST, not silence


def load_wav(path):
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0


def write_wav(path, audio):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return path


def split(audio, target_s, look_back_s):
    """Cut into ~target_s pieces, landing each cut in the quietest 200ms window
    of the look_back_s before the nominal boundary, so cuts avoid mid-word."""
    target, win = int(target_s * SAMPLE_RATE), int(0.2 * SAMPLE_RATE)
    out, start, n = [], 0, len(audio)
    while start < n:
        end = min(start + target, n)
        if end < n:
            lo = max(end - int(look_back_s * SAMPLE_RATE), start + win)
            seg = audio[lo:end]
            if len(seg) > 2 * win:
                rms = [float(np.sqrt(np.mean(seg[i:i + win] ** 2)))
                       for i in range(0, len(seg) - win, win)]
                end = lo + int(np.argmin(rms)) * win + win // 2
        out.append(audio[start:end])
        start = end
    return out


def loud(seg):
    return len(seg) > 0 and float(np.sqrt(np.mean(seg ** 2))) > LOUD_RMS


def rescue(decode, seg):
    """Salvage a piece the normal pass returned nothing for. Partial recovery
    still beats a hole in the sentence, so the first rung yielding ANY text
    wins rather than the rung yielding the most."""
    peak = float(np.max(np.abs(seg))) if len(seg) else 0.0
    variants = [seg]
    if 0 < peak < 0.5:                      # too quiet for the model as recorded
        variants.append(np.clip(seg / peak * 0.7, -1.0, 1.0))
    for v in variants:
        for win_s in RESCUE_WINDOWS:
            parts = [t for t in (decode(p) for p in split(v, win_s, 1.0)) if t]
            if parts:
                return " ".join(parts).strip()
    return ""


def transcribe(decode, audio):
    """decode = fn(np.float32 mono @16k) -> str. Returns (text, lost_pieces).

    lost_pieces counts spans that were audible and still would not decode
    after the full ladder — the only case that deserves a marker in the text."""
    if len(audio) <= LONG_TAKE * SAMPLE_RATE:
        text = decode(audio)
        if text:
            return text, 0
        if len(audio) <= 8 * SAMPLE_RATE:
            if not loud(audio):
                return "", 0            # genuinely silent: not a failure
            text = rescue(decode, audio)
            return (text, 0) if text else ("", 1)
    parts, lost = [], 0
    for c in split(audio, CHUNK_S, 6.0):
        p = decode(c)
        if not p and loud(c):
            p = rescue(decode, c)
            if not p:
                lost += 1
                parts.append("[...]")
        if p:
            parts.append(p)
    return " ".join(parts).strip(), lost
