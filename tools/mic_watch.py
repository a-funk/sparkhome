#!/usr/bin/env python3
"""Siri Remote mic watch: the remote's voice stream as SparkHome gets it, live on a page that a browser on another
machine opens through ssh -L, like the buttons lab (open-appleremote's, on 8791). Per frame: seq, the Siri bit, the
remote's level, the Opus length (NN), lost frames. Per stream: what SparkHome does with it (nothing for a tap, "Didn't
catch that." when quiet, else whisper), and whisper's transcript, which this asks for itself.

The source is hcitap's socket while it serves (whole frames; deploy/install.sh root installs it; this takes one of its 4
client places, whatever the number of pages), else the remote's hidraw node, read-only, as SparkHome reads it: what
bluetoothd keeps, 13 of each frame's 67-94 Opus bytes, decoded zero-padded (a muffled envelope of the voice). Audio
stays here: a spoken stream's WAV is in $XDG_RUNTIME_DIR until whisper has read it, and the page gets numbers and text.
whisper is SparkHome's asr: its whisper-server when that is up, else whisper-cli (this never starts the server).
Linux, as the user SparkHome runs as:
  python3 -I tools/mic_watch.py [--port 8792] [--config ~/sparkhome/sparkhome.json]
  then on the viewing machine: ssh -N -L 8792:127.0.0.1:8792 SPARK, and open http://localhost:8792
  python3 -I tools/mic_watch.py --selftest     offline: streams, verdicts, the hidraw cut, the page and its events
"""
import argparse
import collections
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sparkhome import agent, shell, siri, siri_audio as sa  # noqa: E402

GAP_S = .5  # a stream with no frame for this long is over (its end marker doesn't always come)
SOURCES = {"tap": "hcitap: whole frames", "hidraw": "hidraw: 13 Opus bytes a frame (the tap isn't serving)"}


def hear(asr, wav):
    """A WAV -> whisper's text, as the agent asks (asr.server when it answers, else asr.cli), never starting a server."""
    u = urllib.parse.urlsplit(asr.get("server") or "")
    if u.hostname and agent._port_open((u.hostname, u.port or 80)):
        return agent._clean(agent._whisper_post(asr["server"], wav))
    if not asr.get("cli"):
        return "(no whisper: settings.asr has neither a running server nor cli)"
    env = {**os.environ, **{k: str(v) for k, v in (asr.get("env") or {}).items()}}
    p = subprocess.run([*asr["cli"], "-f", wav], env=env, capture_output=True, text=True, timeout=60)
    return agent._clean(p.stdout) if p.returncode == 0 else f"(whisper-cli failed: {p.stderr.strip()[-200:]})"


def cut(v):
    """A hidraw voice value (20 bytes: bluetoothd's cut) -> one siri_audio.parse takes: its 13 Opus bytes, then zeros
    to NN. Anything else as it is."""
    return v + bytes(7 + v[6] - len(v)) if len(v) == 20 and v[0] == 0x10 and v[6] > 13 else v


class Watch:
    """Values from either source -> events to every page: each frame, each stream's end with what SparkHome does with
    it, and whisper's transcript of a spoken one (on its own thread, one at a time)."""

    def __init__(self, hear):
        self.hear, self.mu, self.pages_mu, self.ears = hear, threading.Lock(), threading.Lock(), threading.Lock()
        self.pages, self.recent, self.cur, self.n, self.tap = [], collections.deque(maxlen=40), None, 0, False
        self.status = {"kind": "status", "source": "none", "text": "starting"}

    def emit(self, ev):
        with self.pages_mu:
            if ev["kind"] == "status":
                self.status = ev
            elif ev["kind"] in ("stream", "heard"):
                self.recent.append(ev)
            for q in self.pages:
                try:
                    q.put_nowait(ev)
                except queue.Full:  # ponytail: a page that can't keep up misses events
                    pass

    def page(self):
        """A new page's queue, primed with the status and the recent streams."""
        q = queue.Queue(2000)
        with self.pages_mu:
            for ev in (self.status, *self.recent):
                q.put_nowait(ev)
            self.pages.append(q)
        return q

    def value(self, t, v, source):
        """One value at t (time.monotonic): a voice frame (101 bytes, or hidraw's cut()) or an end marker."""
        p = sa.parse(v)
        if p is None:
            return
        with self.mu:
            if self.status["source"] != source:
                self.emit({"kind": "status", "source": source, "text": SOURCES[source]})
            c = self.cur
            if c and not c["s"].add(v):
                self._done()
                c = None
            if c is None:
                s = sa.Stream()
                if not s.add(v):
                    return  # (an end marker with no stream)
                self.n += 1
                c = self.cur = {"s": s, "n": self.n, "source": source, "t0": t, "held": 0, "peak": 0, "seen": 0}
            c["last"] = t
            if p[0] is None:
                self.emit({"kind": "end", "n": c["n"], "marker": v.hex()})
            else:
                lost = len(c["s"].seqs) > 1 and p[0] - c["s"].seqs[-2] - 1
                c["held"] += bool(p[1] & 0x10)
                c["peak"] = max(c["peak"], p[2] if p[0] >= 2 else 0)
                self.emit({"kind": "frame", "n": c["n"], "t": round(t, 3), "seq": p[0], "siri": bool(p[1] & 0x10),
                           "level": p[2], "nn": v[6], "lost": lost or 0, "source": source})
            if c["s"].done:
                self._done()

    def tick(self, now=None):
        """A stream with no frame for GAP_S is over."""
        with self.mu:
            if self.cur and (time.monotonic() if now is None else now) - self.cur["last"] > GAP_S:
                self._done()

    def _done(self):  # (under self.mu)
        c, self.cur = self.cur, None
        pk = c["s"].packets()
        verdict = ("tap" if c["held"] < agent.MIC_HELD else "quiet" if c["peak"] < agent.MIC_QUIET else "whisper")
        self.emit({"kind": "stream", "n": c["n"], "at": time.strftime("%H:%M:%S"), "source": c["source"],
                   "frames": len(pk), "lost": pk.count(None), "held": c["held"], "peak": c["peak"],
                   "secs": round(c["last"] - c["t0"], 2), "verdict": verdict, "ended": c["s"].done})
        if verdict == "whisper":
            threading.Thread(target=self._hear, args=(c["n"], pk), daemon=True).start()

    def _hear(self, n, pk):
        with self.ears:  # one whisper at a time
            d = tempfile.mkdtemp(prefix="sparkhome-mic-watch-", dir=os.environ.get("XDG_RUNTIME_DIR"))  # 0700
            wav = os.path.join(d, "stream.wav")
            try:
                sa.write_wav(wav, sa.decode(pk))
                text = self.hear(wav)
            except Exception as e:  # (no libopus, whisper missing or slow): said on the page
                text = f"({e})"
            finally:
                for f in (wav, wav + ".txt"):
                    agent._rm(f)
                os.rmdir(d)
        self.emit({"kind": "heard", "n": n, "text": text})


def from_tap(w, path):
    """hcitap's socket -> w, reconnecting. A tap at its 4 clients accepts and closes at once: the waits grow."""
    wait = 1
    while True:
        t, held = time.monotonic(), False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as s:
                s.connect(path)
                w.tap = True
                w.emit({"kind": "status", "source": "tap", "text": SOURCES["tap"]})
                while m := s.recv(256):
                    if len(m) in (8 + 2, 8 + 101):
                        w.value(int.from_bytes(m[:8], "little") / 1e9, bytes(m[8:]), "tap")
                held = time.monotonic() - t > 2
        except OSError:
            pass
        if w.tap:
            w.tap = False
            w.emit({"kind": "status", "source": "none", "text": "hcitap closed this client" + (
                "" if held else " at once: it serves 4 clients (SparkHome is one)")})
        wait = 1 if held else min(2 * wait, 10)
        time.sleep(wait)


def from_hidraw(w):
    """The remote's hidraw node (siri.find), read-only -> w while the tap isn't serving."""
    told = None
    while True:
        node, err = siri.find(), None
        if node:
            try:
                fd = os.open(node, os.O_RDONLY)
            except OSError as e:
                fd, err = None, e
            if fd is not None:
                try:
                    if not w.tap:
                        w.emit({"kind": "status", "source": "hidraw", "text": SOURCES["hidraw"]})
                    while r := os.read(fd, 64):
                        if not w.tap and r[:2] == b"\xfc\x10":  # report 0xFC, kind 0x10: voice, or its end
                            w.value(time.monotonic(), cut(r[1:]), "hidraw")
                except OSError as e:
                    err = e
                finally:
                    os.close(fd)
        if not w.tap and told != (node, str(err)):
            told = (node, str(err))
            w.emit({"kind": "status", "source": "none", "text": f"{node}: {err}" if err else
                    "the remote is asleep or away: press Menu to wake it" if not node else f"{node} closed"})
        time.sleep(1)


def serve(w, port):
    page = PAGE.encode()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.headers.get("Host", "").rsplit(":", 1)[0] not in ("127.0.0.1", "localhost"):
                return self.send_error(403)  # DNS rebinding: only a page from this machine (or its tunnel)
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif self.path == "/events":
                q = w.page()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    while True:
                        try:
                            self.wfile.write(b"data: " + json.dumps(q.get(timeout=15)).encode() + b"\n\n")
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except OSError:
                    pass
                finally:
                    with w.pages_mu:
                        w.pages.remove(q)
            else:
                self.send_error(404)
    return ThreadingHTTPServer(("127.0.0.1", port), H)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Siri Remote mic</title><style>
:root{--bg:#0b0b0d;--card:#18181b;--line:#2a2a2f;--dim:#8e8e96;--acc:#0a84ff;--hot:#30d158;--bad:#ff453a;--warn:#ffd60a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#f2f2f7;font:15px/1.4 system-ui,-apple-system,sans-serif;
padding:20px 16px;max-width:1100px}h1{font-size:22px;margin:0 0 4px}.sub{color:var(--dim);margin:0 0 14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px;margin-bottom:16px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin:0 0 10px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.pill{padding:4px 10px;border-radius:999px;background:#232328;color:var(--dim);font-size:13px}
.pill.ok{color:var(--hot)}.pill.warn{color:var(--warn)}.pill.on{background:var(--acc);color:#fff}
canvas{width:100%;height:160px;display:block}#now{font:13px ui-monospace,monospace;color:var(--dim);margin-top:8px}
.s{border-top:1px solid var(--line);padding:10px 0}.s:first-child{border:0;padding-top:0}.meta{color:var(--dim);font-size:13px}
.say{font-size:20px;margin-top:4px}.say.dim{color:var(--dim);font-size:15px}.v{font-weight:600}
</style></head><body>
<h1>Siri Remote mic</h1>
<p class="sub">The remote's voice as SparkHome gets it. Hold Siri and speak: each 20 ms frame is a bar (blue while Siri
is held, grey for the remote's tail after you let go, red where frames were lost). When a stream ends, it shows what
SparkHome does with it and what whisper hears. The audio stays on the Spark.</p>
<div class="row"><span class="pill" id="src">connecting…</span><span class="pill" id="rate">0 frames/s</span>
<span class="pill" id="siri">Siri up</span></div>
<div class="card"><h2>Live stream, last 10 s</h2><canvas id="cv" width="1600" height="320"></canvas>
<div id="now">waiting for Siri</div></div>
<div class="card"><h2>Streams</h2><div id="list"><div class="meta" id="none">none yet</div></div></div>
<script>
'use strict';
const $=id=>document.getElementById(id), cv=$('cv'), g=cv.getContext('2d'), esc=s=>String(s).replace(/[&<>]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let bars=[], at=[], tmax=0, seen=0;  // bars sit at the Spark's time of their frame: a burst still spreads out
const SRC={tap:'ok',hidraw:'warn',none:''}, VIA={tap:'hcitap',hidraw:'hidraw, 13 bytes a frame'};
const VERDICT={tap:'a tap: SparkHome does nothing',quiet:'too quiet: SparkHome says “Didn\\u2019t catch that.”',
  whisper:'speech: SparkHome sends it to whisper'};
function draw(){const W=cv.width,H=cv.height,now=tmax+(performance.now()-seen)/1000,w=W/500;g.clearRect(0,0,W,H);
  bars=bars.filter(b=>now-b.t<10);
  for(const b of bars){const x=W-(now-b.t)/10*W;
    if(b.edge){g.fillStyle='#ffffff40';g.fillRect(x,0,2,H);continue;}
    const h=b.lost?H:Math.max(3,Math.sqrt(b.level/32768)*H);
    g.fillStyle=b.lost?'#ff453a':b.siri?'#0a84ff':'#5a5a62';g.fillRect(x,H-h,Math.max(2,w-1),h);}
  requestAnimationFrame(draw);}
requestAnimationFrame(draw);
function card(ev){let d=$('s'+ev.n);if(!d){d=document.createElement('div');d.className='s';d.id='s'+ev.n;
  $('list').prepend(d);const n=$('none');if(n)n.remove();}return d;}
function on(ev){const now=performance.now();
  if(ev.kind==='status'){$('src').textContent=ev.text;$('src').className='pill '+SRC[ev.source];}
  else if(ev.kind==='frame'){if(ev.t>tmax){tmax=ev.t;seen=now;}
    at.push(ev.t);at=at.filter(t=>ev.t-t<1);$('rate').textContent=at.length+' frames/s';
    if(ev.seq===0)bars.push({t:ev.t,edge:1});for(let i=1;i<=ev.lost;i++)bars.push({t:ev.t-.02*i,lost:1});
    bars.push({t:ev.t,level:ev.level,siri:ev.siri});$('siri').textContent=ev.siri?'Siri held':'Siri up';
    $('siri').className='pill'+(ev.siri?' on':'');
    $('now').textContent=`stream ${ev.n} · seq ${ev.seq} · level ${ev.level} · NN ${ev.nn} · ${VIA[ev.source]}`+
      (ev.lost?` · ${ev.lost} lost`:'');}
  else if(ev.kind==='end'){$('now').textContent+=` · end ${ev.marker}`;$('siri').textContent='Siri up';$('siri').className='pill';}
  else if(ev.kind==='stream'){const d=card(ev);d.innerHTML=`<div><span class="v">${esc(VERDICT[ev.verdict])}</span></div>
    <div class="meta">stream ${ev.n} · ${ev.at} · ${VIA[ev.source]} · ${ev.frames} frames
    (${ev.held} with Siri held, ${ev.lost} lost) · ${ev.secs} s · peak level ${ev.peak}${ev.ended?'':' · no end marker'}
    </div>`+(ev.verdict==='whisper'?`<div class="say dim" id="h${ev.n}">transcribing…</div>`:'');}
  else if(ev.kind==='heard'){card(ev);let h=$('h'+ev.n);if(!h){h=document.createElement('div');h.id='h'+ev.n;
    $('s'+ev.n).append(h);}h.className='say'+(ev.text?'':' dim');h.textContent=ev.text?`“${ev.text}”`:'(nothing heard)';}}
const es=new EventSource('/events');es.onmessage=m=>on(JSON.parse(m.data));
es.onerror=()=>{$('src').textContent='reconnecting to the Spark…';$('src').className='pill';};
</script></body></html>
"""


def _selftest():
    import http.client
    asked, evs = [], []
    w = Watch(lambda wav: asked.append(os.path.getsize(wav)) or "Open YouTube.")
    q = w.page()

    def take():  # every event so far
        while True:
            try:
                evs.append(q.get(timeout=.01))
            except queue.Empty:
                return [e for e in evs if e["kind"] != "status"]

    fr = lambda s, held=True, level=20000: sa.value(s, bytes([sa.TOC]) + bytes(70), 0x10 if held else 0, level)
    sa.decode, decode = (lambda pk: bytes(640 * len(pk))), sa.decode  # (libopus: siri_audio's selftest)
    try:
        t = 100.0
        for s in [*range(20), *range(21, 25)]:  # spoken: Siri held 0.4 s, one frame lost, then the tail and its end
            w.value(t + s * .02, fr(s, s < 20), "tap")
        w.value(t + .5, b"\x10\x00", "tap")
        for i in range(200):
            if any(e["kind"] == "heard" for e in evs):
                break
            take()
            time.sleep(.01)
        ev = take()
        assert [e["seq"] for e in ev if e["kind"] == "frame"] == [*range(20), *range(21, 25)], ev
        assert [e["lost"] for e in ev if e["kind"] == "frame"][20] == 1 and ev[24] == {"kind": "end", "n": 1,
                                                                                        "marker": "1000"}
        st = ev[25]
        assert (st["kind"], st["frames"], st["lost"], st["held"], st["peak"], st["verdict"], st["ended"]) == \
            ("stream", 25, 1, 20, 20000, "whisper", True), st
        assert ev[26] == {"kind": "heard", "n": 1, "text": "Open YouTube."} and asked == [44 + 640 * 25], (ev, asked)
        evs.clear()
        for s in range(5):  # a tap; then held but quiet, cut off by a restart (no end marker between) ...
            w.value(t + s * .02, fr(s), "tap")
        w.value(t + 1, b"\x10\x10", "tap")
        for s in range(20):
            w.value(t + 2 + s * .02, fr(s, level=2000), "tap")
        w.value(t + 3, fr(0), "tap")
        w.tick(t + 3.4)  # ... and a stream that breaks off (its end marker lost): over GAP_S after its last frame
        assert [e["verdict"] for e in take() if e["kind"] == "stream"] == ["tap", "quiet"] and w.cur["n"] == 4
        w.tick(t + 3 + GAP_S + .01)
        assert [(e["n"], e["verdict"], e["ended"]) for e in take() if e["kind"] == "stream"][-1] == (4, "tap", False)
        assert len(asked) == 1  # only speech goes to whisper
        w.value(t + 9, b"\x10\x00", "tap")  # (an end marker with no stream: nothing)
        assert w.cur is None
        # the hidraw cut: 20 bytes keep the TOC and 12 more of NN; zero-padded, they decode as a whole packet would
        v = fr(7)[:20]
        assert sa.parse(v) is None and sa.parse(cut(v)) == (7, 0x10, 20000, v[7:20] + bytes(71 - 13))
        assert cut(b"\x10\x00") == b"\x10\x00" and cut(b"\x01" * 20) == b"\x01" * 20
        # the page and its events, from this machine (or a tunnel) only
        srv = serve(w, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/")
        r = c.getresponse()
        assert r.status == 200 and b"EventSource('/events')" in r.read()
        c.request("GET", "/", headers={"Host": "evil.example:8792"})
        r = c.getresponse()
        assert r.status == 403
        r.read()
        c.request("GET", "/events", headers={"Host": f"localhost:{port}"})
        r = c.getresponse()
        lines = [r.fp.readline() for _ in range(4)]  # the status, then the recent streams, each "data: ...", ""
        assert r.status == 200 and [json.loads(x[6:])["kind"] for x in lines[::2]] == ["status", "stream"], lines
        c.close()
        srv.shutdown()
    finally:
        sa.decode = decode


def main(argv=None):
    p = argparse.ArgumentParser(prog="tools/mic_watch.py", description=__doc__.split("\n")[0])
    p.add_argument("--port", type=int, default=8792, help="port on 127.0.0.1 (default 8792; the buttons lab has 8791)")
    p.add_argument("--config", help="sparkhome.json for asr (default: as SparkHome finds it)")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args(argv)
    if a.selftest:
        _selftest()
        return print("mic_watch selftest OK")
    if not sys.platform.startswith("linux"):
        p.exit(2, "mic_watch reads the remote on Linux (the Spark); view it from here through ssh -L\n")
    asr = shell.expand(shell.load_config(a.config)["settings"]).get("asr") or {}
    w = Watch(lambda wav: hear(asr, wav))
    run = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    for target, args in ((from_tap, (w, os.path.join(run, agent.MIC_SOCK))), (from_hidraw, (w,))):
        threading.Thread(target=target, args=args, daemon=True).start()

    def ticker():
        while True:
            time.sleep(.1)
            w.tick()
    threading.Thread(target=ticker, daemon=True).start()
    srv = serve(w, a.port)
    print(f"mic_watch: http://127.0.0.1:{a.port} (Ctrl-C to stop)", file=sys.stderr, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
