"""
whisper_worker — persistent local STT server for Chat·hache (B40).

Serves a single OpenAI-compatible-ish endpoint on 127.0.0.1:8499 used by the
bridge as the OFFLINE fallback for /v1/audio/transcriptions (Groq primary):

  POST /transcribe  (multipart: file=<audio>, optional language=<code>)
       -> {"text": "..."}
  GET  /health      -> {"status": "ok", "model": ..., "loaded": true}

Runs faster-whisper (CTranslate2, int8, no torch) with the model loaded once;
first start downloads ~1.7 GB from HuggingFace to ~/.cache/huggingface.

Launchd user agent org.hache.chat.whisper (TCC-safe: env/bin/python).
"""

import argparse
import json
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL = "large-v3-turbo"
COMPUTE = "int8"
HOST = "127.0.0.1"
PORT = 8499

_model = None
_model_lock = threading.Lock()


def get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel  # lazy import: keep startup fast

            _model = WhisperModel(MODEL, device="cpu", compute_type=COMPUTE, cpu_threads=8)
        return _model


def _parse_multipart(body: bytes, content_type: str):
    m = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not m:
        return None, None, None
    boundary = ("--" + m.group(1)).encode()
    parts = body.split(boundary)
    for part in parts:
        if not part.strip(b"\r\n-"):
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        if b'name="file"' not in head:
            continue
        data = data.rstrip(b"\r\n")
        fn = re.search(rb'filename="([^"]*)"', head)
        name = fn.group(1).decode() if fn else "audio.bin"
        return name, data, None
    return None, None, "no file part found"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep stdout clean
        pass

    def _send(self, code: int, obj: dict, ctype: str = "application/json"):
        payload = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, {"status": "ok", "model": MODEL, "compute": COMPUTE, "loaded": _model is not None})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/transcribe"):
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            self._send(400, {"error": "empty body"})
            return
        body = self.rfile.read(length)
        fname, audio, err = _parse_multipart(body, self.headers.get("Content-Type", ""))
        if err or audio is None:
            self._send(400, {"error": err or "missing file part"})
            return
        lang = None
        lm = re.search(rb'name="language"\r\n\r\n([a-zA-Z-]{2,10})', body)
        if lm:
            lang = lm.group(1).decode()
        try:
            model = get_model()
            with tempfile.NamedTemporaryFile(suffix=Path(fname).suffix or ".wav", delete=False) as tf:
                tf.write(audio)
                tmp = tf.name
            try:
                segments, info = model.transcribe(tmp, language=lang or None, vad_filter=True)
                text = "".join(seg.text for seg in segments).strip()
            finally:
                Path(tmp).unlink(missing_ok=True)
            if not text:
                self._send(200, {"text": ""})
            else:
                self._send(200, {"text": text})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": f"transcription failed: {e}"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--preload", action="store_true", help="load the model at startup (first run downloads it)")
    args = ap.parse_args()

    if args.preload:
        print(f"[whisper_worker] loading {MODEL} ({COMPUTE}) …", flush=True)
        get_model()
        print("[whisper_worker] model ready", flush=True)

    srv = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"[whisper_worker] listening on http://{HOST}:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
