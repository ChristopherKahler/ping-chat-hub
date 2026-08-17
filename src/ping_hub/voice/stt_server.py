"""Parakeet STT bridge — whisper.cpp-compatible speech-to-text, bundled.

Vendored into the hub package (Chris ruling 2026-08-17: one package). Wraps a
local sherpa-onnx nemo parakeet transducer behind the whisper.cpp server HTTP
convention:

    POST /inference   multipart with a wav in field "file", or a raw wav body
    ->  {"text": "<transcript>"}
    GET  /            health check

Model loads ONCE at boot (~3s, ~700 MB RAM). Bad input never crashes the
server; it returns {"text": ""}. Stdlib + numpy + sherpa-onnx only.

Paths come from the environment so `ping-hub install` can place the model
anywhere:  PING_HUB_STT_MODEL, PING_HUB_STT_HOST, PING_HUB_STT_PORT.
"""
import io
import json
import os
import re
import struct
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import sherpa_onnx

HOST = os.environ.get("PING_HUB_STT_HOST", "127.0.0.1")
PORT = int(os.environ.get("PING_HUB_STT_PORT", "8973"))
TARGET_RATE = 16000
MODEL_DIR = Path(os.environ.get("PING_HUB_STT_MODEL")
                 or Path(__file__).resolve().parent / "model")

recognizer = None
_decode_lock = threading.Lock()   # one decode at a time; requests serialize


def load_model():
    global recognizer
    need = ["encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx",
            "tokens.txt"]
    missing = [n for n in need if not (MODEL_DIR / n).is_file()]
    if missing:
        # absent, said out loud — not a stack trace three layers down
        raise SystemExit(f"[stt-server] model incomplete in {MODEL_DIR}: "
                         f"missing {missing}. Run `ping-hub install`.")
    print(f"[stt-server] loading {MODEL_DIR.name} (one-time, ~700MB RAM)...",
          flush=True)
    t0 = time.time()
    recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(MODEL_DIR / "encoder.int8.onnx"),
        decoder=str(MODEL_DIR / "decoder.int8.onnx"),
        joiner=str(MODEL_DIR / "joiner.int8.onnx"),
        tokens=str(MODEL_DIR / "tokens.txt"),
        num_threads=4,
        model_type="nemo_transducer",
    )
    print(f"[stt-server] model loaded in {time.time() - t0:.1f}s", flush=True)


# ---------------------------------------------------------------- wav decoding

def _pcm_to_float(raw: bytes, sampwidth: int) -> np.ndarray:
    if sampwidth == 1:                                   # unsigned 8-bit
        return (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if sampwidth == 2:                                   # signed 16-bit
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if sampwidth == 3:                                   # signed 24-bit
        b = np.frombuffer(raw[: len(raw) - len(raw) % 3], dtype=np.uint8).reshape(-1, 3)
        vals = (b[:, 0].astype(np.int32)
                | (b[:, 1].astype(np.int32) << 8)
                | (b[:, 2].astype(np.int32) << 16))
        vals -= (vals & 0x800000) << 1                   # sign-extend
        return vals.astype(np.float32) / 8388608.0
    if sampwidth == 4:                                   # signed 32-bit
        return np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    raise ValueError(f"unsupported sample width {sampwidth}")


def _decode_wav_stdlib(data: bytes):
    with wave.open(io.BytesIO(data), "rb") as wf:
        nch, rate = wf.getnchannels(), wf.getframerate()
        samples = _pcm_to_float(wf.readframes(wf.getnframes()), wf.getsampwidth())
    return samples, nch, rate


def _decode_wav_manual(data: bytes):
    """Minimal RIFF parser (handles IEEE-float wavs the wave module rejects)."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    pos, fmt, raw = 12, None, None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4:pos + 8])
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            raw = body
        pos += 8 + size + (size & 1)
    if fmt is None or raw is None:
        raise ValueError("missing fmt/data chunk")
    audio_fmt, nch, rate, _, _, bits = fmt
    if audio_fmt == 0xFFFE and len(data) >= 24:          # WAVE_FORMAT_EXTENSIBLE
        audio_fmt = 1 if bits in (8, 16, 24, 32) else 3
    if audio_fmt == 3 and bits == 32:                    # IEEE float32
        samples = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    elif audio_fmt == 1:
        samples = _pcm_to_float(raw, bits // 8)
    else:
        raise ValueError(f"unsupported wav format {audio_fmt}/{bits}bit")
    return samples, nch, rate


def wav_to_16k_mono_f32(data: bytes) -> np.ndarray:
    """Any wav bytes -> 16kHz mono float32 (linear resample if needed)."""
    try:
        samples, nch, rate = _decode_wav_stdlib(data)
    except Exception:
        samples, nch, rate = _decode_wav_manual(data)
    if nch > 1:
        samples = samples[: len(samples) - len(samples) % nch]
        samples = samples.reshape(-1, nch).mean(axis=1)
    samples = np.ascontiguousarray(samples, dtype=np.float32)
    if rate != TARGET_RATE and len(samples) > 1:
        n_out = max(1, int(round(len(samples) * TARGET_RATE / rate)))
        x_old = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        samples = np.interp(x_new, x_old, samples).astype(np.float32)
    return samples


# ------------------------------------------------------------ multipart parse

def extract_wav_bytes(body: bytes, content_type: str) -> bytes:
    """Pull the audio out of a multipart body; raw-wav body as fallback."""
    if content_type and "multipart/form-data" in content_type.lower():
        m = re.search(r'boundary="?([^";]+)"?', content_type, re.IGNORECASE)
        if m:
            boundary = m.group(1).encode("latin-1")
            preferred, fallback = None, None
            for part in body.split(b"--" + boundary):
                head, sep, content = part.partition(b"\r\n\r\n")
                if not sep:
                    continue
                if content.endswith(b"\r\n"):
                    content = content[:-2]   # exactly one CRLF before boundary
                head_l = head.lower()
                if b'name="file"' in head_l:
                    preferred = content
                elif b"filename=" in head_l or content[:4] == b"RIFF":
                    fallback = fallback or content
            if preferred is not None:
                return preferred
            if fallback is not None:
                return fallback
    return body


# -------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, obj: dict, status: int = 200) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._send_json({"status": "ok", "model": MODEL_DIR.name})

    def do_POST(self):   # any POST path is treated as /inference (lenient)
        t_req = time.time()
        text, dur = "", 0.0
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            wav = extract_wav_bytes(body, self.headers.get("Content-Type") or "")
            samples = wav_to_16k_mono_f32(wav)
            dur = len(samples) / TARGET_RATE
            with _decode_lock:
                stream = recognizer.create_stream()
                stream.accept_waveform(TARGET_RATE, samples)
                recognizer.decode_stream(stream)
                text = stream.result.text.strip()
        except Exception as exc:
            print(f"[stt-server] request failed ({type(exc).__name__}: {exc})"
                  f" -> returning empty text", flush=True)
        print(f"[stt-server] {dur:.2f}s audio -> {time.time() - t_req:.2f}s "
              f"latency | {text or '(empty)'}", flush=True)
        try:
            self._send_json({"text": text})
        except Exception:
            pass   # client hung up; never crash


    def log_message(self, fmt, *args):
        pass


def main() -> None:
    load_model()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"[stt-server] listening on http://{HOST}:{PORT}/inference "
          f"(whisper.cpp-compatible)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[stt-server] shutting down.", flush=True)
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
