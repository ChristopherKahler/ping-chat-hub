"""stt-fix - re-decode a saved cx-ptt take and print the transcript.

Every take is archived to .base-gbl\\cx\\stt-takes (rolling ring, keep_takes in
cx.toml) and anything the daemon could not fully decode also lands in
.base-gbl\\cx\\stt-failed. Either can be run again here - the point is that a
bad transcript costs a command, not a re-recording and not a session asking
Chris to repeat himself.

    stt-fix                 re-decode the newest take
    stt-fix last            same
    stt-fix -2              the one before that (-N counts back)
    stt-fix take_2026...wav a specific file (bare name or full path)
    stt-fix --list          what is on hand, newest first
    stt-fix --deep <file>   skip the fast path, go straight to the fine ladder
                            (use when the take decoded but decoded WRONG)

Output is the transcript alone, so `! stt-fix last` drops clean text into a
session. Diagnostics go to stderr and stay out of the way.

Shares stt_decode.py with the daemon, so the fast path here reproduces exactly
what the daemon already tried - if that failed, --deep is the thing to reach
for, not a second identical attempt.
"""
import sys
from pathlib import Path
# paths derive rather than being spelled out -- see cxpaths.py, which ships
# beside this file. Vendored into ping-chat-hub 2026-08-19.
import cxpaths


import numpy as np
import sherpa_onnx

import stt_decode
from stt_decode import SAMPLE_RATE, load_wav, rescue, split

CX_DIR = cxpaths.cx_dir()
DIRS = [CX_DIR / "stt-takes", CX_DIR / "stt-failed"]
# the same parakeet the hub provisions for its STT server
MODEL_DIR = cxpaths.stt_model()


def err(msg):
    print(msg, file=sys.stderr)


def takes():
    """Every saved take, newest first."""
    found = [p for d in DIRS if d.is_dir() for p in d.glob("*.wav")]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def resolve(arg):
    all_takes = takes()
    if not all_takes:
        err(f"[stt-fix] nothing saved yet under {' or '.join(str(d) for d in DIRS)}")
        sys.exit(1)
    if arg in (None, "last"):
        return all_takes[0]
    if arg.lstrip("-").isdigit() and arg.startswith("-"):
        i = int(arg.lstrip("-")) - 1
        if i >= len(all_takes):
            err(f"[stt-fix] only {len(all_takes)} takes on hand")
            sys.exit(1)
        return all_takes[i]
    p = Path(arg)
    if p.is_file():
        return p
    for cand in all_takes:                       # bare filename, either dir
        if cand.name == arg or cand.stem == arg:
            return cand
    err(f"[stt-fix] no take matching {arg!r} - try --list")
    sys.exit(1)


def build_decoder():
    rec = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(MODEL_DIR / "encoder.int8.onnx"),
        decoder=str(MODEL_DIR / "decoder.int8.onnx"),
        joiner=str(MODEL_DIR / "joiner.int8.onnx"),
        tokens=str(MODEL_DIR / "tokens.txt"),
        sample_rate=SAMPLE_RATE,
        model_type="nemo_transducer",
    )

    def decode(seg):
        s = rec.create_stream()
        s.accept_waveform(SAMPLE_RATE, seg)
        rec.decode_stream(s)
        return s.result.text.strip()

    return decode


def _words(text):
    return len([w for w in text.split() if w != "[...]"])


def deep(decode, audio):
    """Every piece through the rescue ladder, not just the failed ones - the
    repair for a take that decoded into something wrong rather than nothing."""
    parts = []
    for c in split(audio, 8, 2.0):
        parts.append(decode(c) or rescue(decode, c) or "[...]")
    return " ".join(p for p in parts if p).strip()


def main():
    argv = [a for a in sys.argv[1:]]
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if argv and argv[0] in ("--list", "-l", "list"):
        found = takes()
        if not found:
            err("[stt-fix] nothing saved yet")
            return 1
        for i, p in enumerate(found, 1):
            size = p.stat().st_size / (SAMPLE_RATE * 2)
            tag = "FAILED" if p.parent.name == "stt-failed" else "ok"
            err(f"  -{i:<3} {p.name:<32} {size:5.1f}s  {tag}")
        return 0

    want_deep = "--deep" in argv
    argv = [a for a in argv if a != "--deep"]
    path = resolve(argv[0] if argv else None)
    audio = load_wav(path)
    err(f"[stt-fix] {path.name}  {len(audio) / SAMPLE_RATE:.1f}s"
        f"  peak={float(np.max(np.abs(audio))):.3f}"
        f"{'  (deep)' if want_deep else ''}")

    decode = build_decoder()
    if want_deep:
        text = deep(decode, audio)
    else:
        text, lost = stt_decode.transcribe(decode, audio)
        if lost or not text:
            err(f"[stt-fix] fast path left {lost or 'everything'} unrecovered "
                f"- retrying deep")
            alt = deep(decode, audio)
            # deep is finer, not strictly better - short windows lose the
            # context the model needs, so keep whichever recovered more
            if _words(alt) > _words(text):
                text = alt
            else:
                err("[stt-fix] deep pass recovered no more - keeping the fast result")

    if not text.strip("[.] "):   # nothing but hole markers came back
        err("[stt-fix] nothing decodable in this take (check the mic, not the model)")
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
