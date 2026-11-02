"""Stdlib Chrome DevTools Protocol client for the SparkHome kiosk.

One persistent browser-level WebSocket with flattened sessions (page commands carry a sessionId), reconnecting forever,
and the browser's targets kept current from Target.* events. It sends no Origin header, so Chrome needs no
--remote-allow-origins and web pages still can't reach the port. Grown from the scratch prototype tested on Chrome 154/155.
"""
import base64
import http.client
import itertools
import json
import logging
import os
import queue
import socket
import struct
import threading
import time
import urllib.parse

log = logging.getLogger("sparkhome.cdp")


def _mask(data, key):
    n = len(data)
    return (int.from_bytes(data, "big") ^ int.from_bytes((key * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")


def ws_frame(payload, op=1, mask=None):
    """Client→server WebSocket frame: FIN set and always masked (RFC 6455 §5.3)."""
    n, mask = len(payload), mask or os.urandom(4)
    size = bytes([0x80 | n]) if n < 126 else b"\xfe" + struct.pack(">H", n) if n < 65536 else b"\xff" + struct.pack(">Q", n)
    return bytes([0x80 | op]) + size + mask + _mask(payload, mask)


def ws_read(read):
    """One frame via read(n) (exactly n bytes) → (fin, opcode, payload). Unmasks if the peer masked."""
    b0, b1 = read(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", read(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", read(8))[0]
    key = read(4) if b1 & 0x80 else None
    data = read(n)
    return bool(b0 & 0x80), b0 & 0x0F, _mask(data, key) if key else data


class CDP:
    """Browser-level CDP connection. call() is thread-safe. Events and on_connect run in order on one dispatch
    thread (not the socket reader), so handlers may call() back into Chrome."""

    def __init__(self, port, on_event=None, on_connect=None):
        self.port, self.on_event, self.on_connect = port, on_event, on_connect
        self.targets = {}                    # targetId → targetInfo (after Target.setDiscoverTargets)
        self.connected = threading.Event()
        self.version = {}
        self._ids, self._send_lock, self._waiting = itertools.count(1), threading.Lock(), {}
        self._sock = self._rf = None
        self._events = queue.Queue()

    def start(self):
        threading.Thread(target=self._run, name="cdp", daemon=True).start()
        threading.Thread(target=self._dispatch, name="cdp-events", daemon=True).start()
        return self

    def call(self, method, session=None, timeout=10, **params):
        """Send a command (to a flattened session if given) and wait for its result. Raises ConnectionError,
        TimeoutError or RuntimeError (Chrome's error message)."""
        if not self.connected.is_set():
            raise ConnectionError("browser not connected")
        i, w = next(self._ids), [threading.Event(), None, session]
        self._waiting[i] = w
        msg = {"id": i, "method": method, "params": params}
        if session:
            msg["sessionId"] = session
        try:
            self._send(json.dumps(msg).encode())
            if not w[0].wait(timeout):
                raise TimeoutError(f"{method}: no reply in {timeout} s")
        finally:
            self._waiting.pop(i, None)
        if w[1] is None:
            raise ConnectionError(f"{method}: browser connection lost")
        if "error" in w[1]:
            raise RuntimeError(f"{method}: {w[1]['error'].get('message')}")
        return w[1].get("result", {})

    def _send(self, payload, op=1):
        try:
            with self._send_lock:
                self._sock.sendall(ws_frame(payload, op))
        except (OSError, AttributeError) as e:
            raise ConnectionError(f"send failed: {e}") from None

    def _open(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)   # not urllib: no proxy env surprises
        try:
            c.request("GET", "/json/version")
            self.version = json.loads(c.getresponse().read())
        finally:
            c.close()
        u = urllib.parse.urlsplit(self.version["webSocketDebuggerUrl"])
        sock = socket.create_connection((u.hostname, u.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        rf = sock.makefile("rb")
        status = rf.readline()
        if b" 101 " not in status:
            sock.close()
            raise ConnectionError(status.decode(errors="replace").strip())
        while rf.readline().strip():         # rest of the handshake headers
            pass
        sock.settimeout(None)
        self._sock, self._rf = sock, rf

    def _exact(self, n):
        data = self._rf.read(n)
        if len(data) < n:
            raise ConnectionError("closed")
        return data

    def _run(self):
        delay, down = 0.5, False
        while True:
            try:
                self._open()
            except (OSError, ValueError, KeyError, http.client.HTTPException):
                if not down:
                    log.info("waiting for Chrome's DevTools on 127.0.0.1:%s", self.port)
                down = True
                time.sleep(delay)
                delay = min(delay * 2, 5)
                continue
            log.info("connected to %s", self.version.get("Browser", "Chrome"))
            delay, down = 0.5, False
            self.targets.clear()
            self.connected.set()
            self._events.put(None)           # None = "connected": on_connect runs on the dispatch thread
            try:
                self._read_loop()
            except (OSError, ValueError, ConnectionError):
                pass
            self.connected.clear()
            for w in list(self._waiting.values()):
                w[0].set()                   # wake callers; their result stays None → ConnectionError
            try:
                self._sock.close()
            except OSError:
                pass
            log.warning("browser connection lost")

    def _read_loop(self):
        buf = b""
        while True:
            fin, op, data = ws_read(self._exact)
            if op == 9:
                self._send(data, 0xA)        # ping → pong
            elif op == 8:
                return
            elif op in (0, 1, 2):
                buf += data
                if not fin:
                    continue
                m, buf = json.loads(buf), b""
                if "id" in m:                # a reply (late ones for timed-out calls are dropped)
                    w = self._waiting.get(m["id"])
                    if w:
                        w[1] = m
                        w[0].set()
                    continue
                if "sessionId" not in m:     # browser-level target bookkeeping
                    p = m.get("params", {})
                    if m.get("method") in ("Target.targetCreated", "Target.targetInfoChanged"):
                        self.targets[p["targetInfo"]["targetId"]] = p["targetInfo"]
                    elif m.get("method") == "Target.targetDestroyed":
                        self.targets.pop(p["targetId"], None)
                    elif m.get("method") == "Target.detachedFromTarget":   # (its calls in flight never get a
                        for w in list(self._waiting.values()):            # reply: measured 10 s stalls)
                            if w[2] and w[2] == p.get("sessionId") and w[1] is None:  # (a reply in hand stands)
                                w[1] = {"error": {"message": "target closed"}}
                                w[0].set()
                self._events.put(m)

    def _dispatch(self):
        while True:
            m = self._events.get()
            try:
                if m is None:
                    if self.on_connect and self.connected.is_set():
                        self.on_connect()
                elif self.on_event:
                    self.on_event(m)
            except Exception:                # a handler bug must not stop event delivery
                log.exception("CDP handler failed")


def _selftest():
    import io
    for n in (0, 5, 125, 126, 65535, 70000):          # 7-, 16- and 64-bit lengths; masked round trip
        p = os.urandom(n)
        fin, op, data = ws_read(io.BytesIO(ws_frame(p, 1, b"\x01\x02\x03\x04")).read)
        assert fin and op == 1 and data == p, n

    def frame(p, op=1, fin=True):                    # server→client: unmasked
        n = len(p)
        size = bytes([n]) if n < 126 else b"\x7e" + struct.pack(">H", n) if n < 65536 else b"\x7f" + struct.pack(">Q", n)
        return bytes([(0x80 if fin else 0) | op]) + size + p

    a, b = socket.socketpair()
    c = CDP(0)
    c._sock, c._rf = a, a.makefile("rb")
    c.connected.set()
    threading.Thread(target=lambda: _raises(c._read_loop), daemon=True).start()      # ends when the test closes
    peer = b.makefile("rb")
    ev = json.dumps({"method": "Target.targetCreated",
                     "params": {"targetInfo": {"targetId": "T1", "type": "page", "url": "about:blank"}}}).encode()
    b.sendall(frame(ev[:10], 1, False) + frame(ev[10:], 0, True))       # fragmented text message
    assert c._events.get(timeout=2)["method"] == "Target.targetCreated" and c.targets["T1"]["type"] == "page"
    b.sendall(frame(b"hb", 9))                                          # ping → masked pong
    assert ws_read(peer.read)[1:] == (0xA, b"hb")
    out = {}
    t = threading.Thread(target=lambda: out.update(r=c.call("Browser.getVersion", session="S1", timeout=2)))
    t.start()
    req = json.loads(ws_read(peer.read)[2])
    assert req["method"] == "Browser.getVersion" and req["sessionId"] == "S1"
    b.sendall(frame(json.dumps({"id": req["id"], "result": {"product": "Chrome/155"}}).encode()))
    t.join(2)
    assert out["r"] == {"product": "Chrome/155"}
    t = threading.Thread(target=lambda: out.update(e=_raises(lambda: c.call("Nope.nope", timeout=2))))
    t.start()
    req = json.loads(ws_read(peer.read)[2])
    b.sendall(frame(json.dumps({"id": req["id"], "error": {"message": "not found"}}).encode()))
    t.join(2)
    assert isinstance(out["e"], RuntimeError) and "not found" in str(out["e"])
    big = json.dumps({"method": "Page.x", "params": {"data": "x" * 70000}}).encode()
    b.sendall(frame(big))                                               # 64-bit length from the server
    assert len(c._events.get(timeout=2)["params"]["data"]) == 70000
    b.sendall(frame(json.dumps({"method": "Target.targetDestroyed", "params": {"targetId": "T1"}}).encode()))
    c._events.get(timeout=2)
    assert "T1" not in c.targets
    # a page that closes while a call to its session is in flight: Chrome sends no reply, only the detach
    t = threading.Thread(target=lambda: out.update(d=_raises(lambda: c.call("Input.dispatchKeyEvent", session="S1",
                                                                         timeout=3))))
    t.start()
    ws_read(peer.read)
    b.sendall(frame(json.dumps({"method": "Target.detachedFromTarget", "params": {"sessionId": "S1"}}).encode()))
    t.join(1)
    assert isinstance(out.get("d"), RuntimeError) and "target closed" in str(out["d"]), out.get("d")
    # ...but a reply that came in before the detach stands (one read: both arrive before the caller wakes)
    t = threading.Thread(target=lambda: out.update(kept=c.call("Runtime.evaluate", session="S2", timeout=3)))
    t.start()
    req = json.loads(ws_read(peer.read)[2])
    b.sendall(frame(json.dumps({"id": req["id"], "result": {"v": 1}}).encode()) +
              frame(json.dumps({"method": "Target.detachedFromTarget", "params": {"sessionId": "S2"}}).encode()))
    t.join(2)
    assert out.get("kept") == {"v": 1}, out.get("kept")
    a.close()
    b.close()


def _raises(fn):
    try:
        fn()
    except Exception as e:
        return e
