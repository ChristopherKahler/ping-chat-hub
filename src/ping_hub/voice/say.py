"""Kokoro TTS speaker — bundled.

Vendored into the hub package (Chris ruling 2026-08-17: one package). Exactly
the surface the daemon uses, and nothing else:

    say --voices                 list voice names, one per line
    say --out FILE.wav [text]    synthesise to a wav (what the hub calls)
    say [text]                   speak it on this machine's default output

Model paths come from the environment so `ping-hub install` can place them
anywhere:  PING_HUB_TTS_MODEL (a directory holding kokoro-v1.0.onnx and
voices-v1.0.bin).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

MODEL_DIR = Path(os.environ.get("PING_HUB_TTS_MODEL")
                 or Path(__file__).resolve().parent / "model")
MODEL = MODEL_DIR / "kokoro-v1.0.onnx"
VOICES = MODEL_DIR / "voices-v1.0.bin"
DEFAULT_VOICE = "af_heart"


def _load():
    missing = [p.name for p in (MODEL, VOICES) if not p.is_file()]
    if missing:
        # absent, said plainly, with the fix — not an ImportError traceback
        raise SystemExit(f"[say] model incomplete in {MODEL_DIR}: missing "
                         f"{missing}. Run `ping-hub install`.")
    from kokoro_onnx import Kokoro
    return Kokoro(str(MODEL), str(VOICES))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="say", description="Speak text with Kokoro")
    p.add_argument("text", nargs="*", help="text to speak; omit to read stdin")
    p.add_argument("--voice", default=DEFAULT_VOICE)
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--out", metavar="FILE.wav", help="write a wav instead of playing")
    p.add_argument("--voices", action="store_true", help="list voices and exit")
    a = p.parse_args(argv)

    kokoro = _load()
    if a.voices:
        print("\n".join(sorted(kokoro.get_voices())))
        return 0

    text = " ".join(a.text) or sys.stdin.read()
    if not text.strip():
        print("[say] nothing to speak", file=sys.stderr)
        return 1
    samples, rate = kokoro.create(text, voice=a.voice, speed=a.speed, lang="en-us")

    if a.out:
        import soundfile as sf
        sf.write(a.out, samples, rate)
        return 0
    try:
        import sounddevice as sd
        sd.play(samples, rate)
        sd.wait()
    except Exception as e:
        # the hub always passes --out, so playback is the optional half:
        # report why rather than exiting 0 having made no sound
        print(f"[say] no audio output device ({e})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
