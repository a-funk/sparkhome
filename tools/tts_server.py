#!/usr/bin/env python3
"""SparkHome's speech server: Kokoro-82M (kokoro-onnx, int8) on the CPU, resident, streamed one sentence at a time.

settings.tts.server_cmd starts it on the first spoken reply (agent.py _tts_ready), with the python of Kokoro's own venv,
so SparkHome itself stays stdlib-only; then it stays up, like whisper-server. The port opens only once the model is
loaded and warm, so an open port means ready.

  ~/.local/share/sparkhome/tts/kokoro/venv/bin/python -I tools/tts_server.py --port 8782 \
      --model ~/.local/share/sparkhome/tts/kokoro/kokoro-v1.0.int8.onnx \
      --voices ~/.local/share/sparkhome/tts/kokoro/voices-v1.0.bin --voice af_heart --threads 4

POST /synthesize {"text": "...", "voice"?: "bf_emma"} -> 200 audio/wav: 16-bit mono PCM at 24 kHz, streamed. The header
goes out at once, its sizes saying "unknown" (read to the end); then each sentence as soon as it is synthesized, with
0.25 s of silence between them (kokoro-onnx's own sentence_pause). "voice" (SparkHome's Settings › Voice) is one of the
voices file's; without it, --voice. Piper's `python -m piper.http_server` takes the same request (and answers a whole
WAV), so tts.server can point at either. 400: no text, or a voice the file hasn't; 404: any other path. Loopback only.

Self-check (stdlib only, a fake voice): python3 -I tools/tts_server.py --selftest
"""
import argparse
import json
import os
import re
import struct
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

GAP_S = 0.25  # silence between sentences synthesized one at a time (= kokoro-onnx's sentence_pause)
MAX_BODY = 16384  # bytes of JSON: replies are a sentence or two, say's text at most 200 characters
KOKORO = "~/.local/share/sparkhome/tts/kokoro/"  # where the README's install puts the venv, model and voices


def sentences(text):
    """Split after . ! or ? and a space. ponytail: "Dr. No" splits too: a short pause, no harm."""
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def wav_header(rate):
    """16-bit mono WAV header with both sizes 0xFFFFFFFF (unknown): the PCM after it is streamed to the end."""
    return b"RIFF" + struct.pack("<I4s4sIHHIIHH4sI", 0xFFFFFFFF, b"WAVE", b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16,
                                 b"data", 0xFFFFFFFF)


class Handler(BaseHTTPRequestHandler):
    timeout = 10  # a client that connects and says nothing can't hold this one-request-at-a-time server

    def do_POST(self):
        try:
            raw = self.rfile.read(min(max(int(self.headers.get("Content-Length") or 0), 0), MAX_BODY + 1))
            body = json.loads(raw) if 0 < len(raw) <= MAX_BODY else None
        except ValueError:
            body = None
        text, voice = (body.get("text"), body.get("voice")) if isinstance(body, dict) else (None, None)
        parts = sentences(text) if isinstance(text, str) else []
        if self.path != "/synthesize" or not parts or voice is not None and not (isinstance(voice, str) and
                                                                                voice in self.server.names):
            return self.send_error(404 if self.path != "/synthesize" else 400)
        rate, synth = self.server.voice
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.end_headers()
        gap = bytes(2 * int(GAP_S * rate))
        try:
            self.wfile.write(wav_header(rate))  # at once: the player starts while the first sentence is made
            for i, s in enumerate(parts):
                try:
                    pcm = synth(s, voice)
                except ValueError:  # no phonemes kokoro-onnx knows in it: the rest still is (emoji are spoken by name)
                    continue
                self.wfile.write(pcm + (gap if i < len(parts) - 1 else b""))  # the gap now: more for the player
        except ConnectionError:
            pass  # the listener went away (SparkHome stops speaking when a game comes to the front)

    def log_message(self, *a):
        pass


def serve(rate, synth, port, names=()):
    """synth(sentence, voice or None) -> PCM; names: the voices a request may ask for."""
    srv = HTTPServer(("127.0.0.1", port), Handler)
    srv.voice, srv.names = (rate, synth), set(names)
    srv.serve_forever()


def kokoro(model, voices, voice, threads):
    """-> (sample rate, (sentence, voice or None for --voice) -> 16-bit PCM bytes): Kokoro on the CPU, as BENCH.md
    measured it (int8, af_heart)."""
    import numpy as np
    import onnxruntime as ort
    from kokoro_onnx import SAMPLE_RATE, Kokoro
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    k = Kokoro.from_session(ort.InferenceSession(model, so, providers=["CPUExecutionProvider"]), voices)

    def synth(s, v=None):
        v = v or voice  # Kokoro's voice names: a… American, b… British English (its phonemes too)
        return (np.clip(k.create(s, voice=v, lang="en-gb" if v.startswith("b") else "en-us")[0], -1, 1) * 32767
                ).astype("<i2").tobytes()
    return SAMPLE_RATE, synth


def main(argv=None):
    p = argparse.ArgumentParser(description="SparkHome's speech server: Kokoro-82M on the CPU, streamed per sentence")
    p.add_argument("--port", type=int, default=8782)
    p.add_argument("--model", default=KOKORO + "kokoro-v1.0.int8.onnx")
    p.add_argument("--voices", default=KOKORO + "voices-v1.0.bin")
    p.add_argument("--voice", default="af_heart")
    p.add_argument("--threads", type=int, default=4, help="onnxruntime intra-op threads (8: first audio 10-27%% "
                   "sooner for 1.5x the CPU, BENCH.md)")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args(argv)
    if a.selftest:
        return _selftest()
    rate, synth = kokoro(os.path.expanduser(a.model), os.path.expanduser(a.voices), a.voice, a.threads)
    synth("Ready.")  # warm: the first run allocates (and a wrong voice fails here, before the port opens)
    with zipfile.ZipFile(os.path.expanduser(a.voices)) as z:  # (a zip of <name>.npy: the voices a request may name)
        names = {n[:-4] for n in z.namelist() if n.endswith(".npy")}
    serve(rate, synth, a.port, names)


def _selftest():
    import socket
    import threading
    import urllib.error
    import urllib.request
    import wave
    assert sentences(" Cyberpunk 2077 is a 2020 game.  You last played it 3 days ago. ") == [
        "Cyberpunk 2077 is a 2020 game.", "You last played it 3 days ago."]
    assert sentences("It costs $2.50. OK!") == ["It costs $2.50.", "OK!"] and sentences("Wait... what?") == [
        "Wait...", "what?"]
    assert sentences("Disney+ is open.") == ["Disney+ is open."] and sentences(" \n ") == []
    go = threading.Event()

    voices = []

    def synth(s, voice):  # one sample per character; the second sentence waits until the client has heard the first
        voices.append(voice)
        if s == "🙂.":
            raise ValueError("Nothing to synthesize")
        if s.startswith("Second"):
            assert go.wait(5)
        return b"\x01\x00" * len(s)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    threading.Thread(target=serve, args=(24000, synth, port, {"bf_emma", "af_heart"}), daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.1).close()
            break
        except OSError:
            threading.Event().wait(0.05)

    def post(body, path="/synthesize"):
        req = urllib.request.Request(url + path, body if isinstance(body, bytes) else json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=5)

    with post({"text": "First one. Second one."}) as r:
        assert r.headers["Content-Type"] == "audio/wav"
        w = wave.open(r)  # the streamed header: sizes unknown
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (24000, 1, 2)
        assert w.readframes(len("First one.")) == b"\x01\x00" * 10  # here while the second is still being made
        go.set()
        assert w.readframes(10 ** 6) == bytes(2 * 6000) + b"\x01\x00" * len("Second one.")  # 0.25 s, then it
    with post({"text": "🙂. Done."}) as r:  # an unspeakable sentence is skipped, not the reply
        assert r.read()[44:] == b"\x01\x00" * 5
    big = {"text": "x" * (MAX_BODY - 11)}  # one byte over MAX_BODY, all of it read: no reset before the 400
    for body, path, code in (({}, "/synthesize", 400), (b"not json", "/synthesize", 400), ({"text": " "}, "/synthesize",
                             400), ({"text": 5}, "/synthesize", 400), (big, "/synthesize", 400),
                             ({"text": "Hi."}, "/tts", 404)):
        try:
            post(body, path)
            raise AssertionError((body, path))
        except urllib.error.HTTPError as e:
            assert e.code == code, (body, path, e.code)
    with socket.create_connection(("127.0.0.1", port)) as c:  # a listener that hangs up mid-reply...
        b = json.dumps({"text": "Gone. Second one."}).encode()
        c.sendall(b"POST /synthesize HTTP/1.0\r\nContent-Length: %d\r\n\r\n" % len(b) + b)
        c.recv(1)
    with post({"text": "Still here."}) as r:  # ...leaves the server serving
        assert r.read()[44:] == b"\x01\x00" * 11
    voices.clear()  # a voice the request names (a voices-file name) reaches synth; none: None (synth's --voice)
    with post({"text": "Hi.", "voice": "bf_emma"}) as r, post({"text": "Hi."}) as r2:
        assert r.read()[44:] == r2.read()[44:] == b"\x01\x00" * 3 and voices == ["bf_emma", None], voices
    for bad in ("nope", "", 5, ["bf_emma"]):  # (a voice the file hasn't: 400, never Kokoro's KeyError mid-stream)
        try:
            post({"text": "Hi.", "voice": bad})
            raise AssertionError(bad)
        except urllib.error.HTTPError as e:
            assert e.code == 400, (bad, e.code)
    with post({"text": "Hi.", "voice": None}) as r:  # (null: none, the default)
        assert r.read()[44:] == b"\x01\x00" * 3 and voices[-1] is None
    print("tts_server selftest OK: sentences, streamed WAV (header first, sentence by sentence, 0.25 s gaps), bad "
          "requests, a listener hanging up, a voice per request")


if __name__ == "__main__":
    main()
