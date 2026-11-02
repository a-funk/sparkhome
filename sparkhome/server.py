"""SparkHome daemon: the launcher's HTTP server — UI files, REST API, SSE events, MCP passthrough, Steam/demo art.

Binds 127.0.0.1, or 0.0.0.0 with --lan (then everything except GET /remote and POST /api/pair needs a paired token).
A POST whose Origin isn't ours gets 403, so pages in the kiosk browser can't drive the daemon; unknown Host headers get
403 (DNS rebinding); /mcp is loopback-only (remote agents use an ssh tunnel). A POST with our Origin is the person at the
TV (our own pages send it); without one, a local agent: Settings' display mode, power and Sleep Now are the person's.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import queue
import secrets
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from . import settings as settings_mod
from . import shell as shell_mod

log = logging.getLogger("sparkhome.server")
UI = shell_mod.REPO / "ui"
MAX_BODY = 1 << 20
TOKENS = Path("~/.local/state/sparkhome/tokens.json").expanduser()
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
         ".jpeg": "image/jpeg", ".webp": "image/webp", ".ico": "image/x-icon", ".woff2": "font/woff2"}
VOICE = {"listening": "listening", "thinking": "thinking"}       # SSE voice phase → State.voice (others → idle)
PRESSES = ("navigate", "select", "back", "home", "media", "volume", "app_switcher")  # the phone's buttons (/api/action)
PERSON = "the TV's own pages only: the person at the TV (agents: get_settings, brightness, sleep)"


def sse(typ, data):
    return f"event: {typ}\ndata: {json.dumps(data, separators=(',', ':'), default=str)}\n\n".encode()


class Hub:
    """SSE fan-out. emit() never blocks: each client has a bounded queue, and one that falls 256 events behind
    (a stalled phone, a dead socket) is dropped."""

    def __init__(self):
        self.lock, self.clients = threading.Lock(), set()

    def add(self):
        q = queue.Queue(256)
        q.dead = False
        with self.lock:
            self.clients.add(q)
        return q

    def remove(self, q):
        with self.lock:
            self.clients.discard(q)

    def emit(self, typ, data):
        msg = sse(typ, data)
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    q.dead = True
                    self.clients.discard(q)


class Pairing:
    """Phone pairing: a 6-digit PIN per daemon start → long-lived tokens (stored as SHA-256). Every 5 wrong PINs lock
    pairing, twice as long each time (60 s … 24 h): a LAN guesser gets ~55 tries on day one, then 5 a day. The right
    PIN (shown on the TV) resets it."""

    def __init__(self, path=TOKENS):
        self.pin, self.path, self.lock = f"{secrets.randbelow(10 ** 6):06d}", path, threading.Lock()
        self.fails, self.locks, self.locked_until = 0, 0, 0.0
        try:
            self.hashes = set(json.loads(path.read_text()))
        except (OSError, ValueError):
            self.hashes = set()

    def check(self, token):
        h = hashlib.sha256(token.encode()).hexdigest() if token else ""
        return bool(token) and any(hmac.compare_digest(h, x) for x in self.hashes)

    def pair(self, pin):
        with self.lock:
            now = time.monotonic()
            if now < self.locked_until:
                return 429, {"ok": False, "error": f"Too many wrong PINs. Try again in {int(self.locked_until - now) + 1} s."}
            if not hmac.compare_digest(pin, self.pin):
                self.fails += 1
                if self.fails >= 5:
                    self.locks += 1
                    self.fails, self.locked_until = 0, now + min(60 * 2 ** (self.locks - 1), 86400)
                return 403, {"ok": False, "error": "Wrong PIN"}
            self.fails = self.locks = 0
            token = secrets.token_urlsafe(32)
            self.hashes.add(hashlib.sha256(token.encode()).hexdigest())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(sorted(self.hashes), f)
            os.replace(tmp, self.path)
            return 200, {"ok": True, "token": token}


def _sleep(shell, fn, *a, **kw):
    """The Shell's sleep (wake, sleep_now, sleep_state: shell.py), else None on a Shell without it."""
    f = getattr(shell, fn, None)
    return f(*a, **kw) if f else None


def loopback(ip):
    try:
        return ipaddress.ip_address(ip.removeprefix("::ffff:")).is_loopback
    except ValueError:
        return False


def gate(method, path, client, host, origin, token_ok, lan, port, hosts):
    """Request policy → None to proceed, else (status, message). Pure, so the selftest can table-test it."""
    try:
        name = urlsplit(f"//{host}").hostname if host else None
    except ValueError:
        return 400, "bad Host header"
    if host and name not in hosts:
        return 403, "unknown Host header"                            # DNS rebinding
    if method == "POST" and origin and origin not in (f"http://{host}", f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
        return 403, "cross-origin request refused"                    # pages in the kiosk browser
    if path == "/mcp" and not loopback(client):
        return 403, "/mcp is loopback-only: use an ssh tunnel"
    if lan and not loopback(client) and not token_ok and (method, path) not in (("GET", "/remote"), ("POST", "/api/pair")):
        return 401, "pair this device first (PIN in Settings on the TV)"
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "SparkHome/0.1"
    timeout = 30                                 # idle/stuck sockets (SSE writes included) error out instead of pinning a thread

    def log_message(self, fmt, *a):
        log.debug("%s " + fmt, self.client_address[0], *a)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def _token(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sh_token":
                return v
        return None

    def _handle(self, method):
        app = self.server.app
        path = urlsplit(self.path).path
        try:
            client = self.client_address[0]
            hosts = app.hosts | {self.connection.getsockname()[0]}
            g = gate(method, path, client, self.headers.get("Host"), self.headers.get("Origin"),
                     app.lan and app.pairing.check(self._token()), app.lan, app.port, hosts)
            if g:
                return self._json(g[0], {"ok": False, "error": g[1]})
            if method == "GET":
                return self._get(path, loopback(client))
            if path == "/mcp" and method == "DELETE":
                return self._json(405, {"ok": False, "error": "POST only"}, [("Allow", "POST")])
            if method == "POST":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n < 0:
                        raise ValueError
                except ValueError:
                    return self._json(400, {"ok": False, "error": "bad Content-Length"})
                if n > MAX_BODY:
                    self.close_connection = True
                    return self._json(413, {"ok": False, "error": "body over 1 MB"})
                return self._post(path, self.rfile.read(n))
            return self._json(404, {"ok": False, "error": "not found"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            log.exception("%s %s", method, path)
            try:
                self._json(500, {"ok": False, "error": "internal error"})
            except OSError:
                pass

    def _get(self, path, local):
        app = self.server.app
        if path == "/":
            return self._file(UI / "index.html")
        if path == "/remote":
            return self._file(UI / "remote.html")
        if path.startswith("/ui/"):
            rel = unquote(path[4:])
            p = (UI / rel).resolve()
            ok = ".." not in Path(rel).parts and not rel.startswith("/") and p.is_relative_to(UI.resolve())
            return self._file(p) if ok else self._json(404, {"ok": False, "error": "not found"})
        if path == "/api/tiles":
            return self._json(200, {**app.shell.tiles(), "pin": app.pairing.pin if local else None, "lan_url": app.lan_url})
        if path == "/api/state":
            return self._json(200, app.shell.state())
        if path == "/api/stats":
            return self._json(200, app.shell.stats())
        if path == "/api/events":
            return self._events()
        if path == "/api/tools":
            return self._json(200, app.agent.tools()) if app.agent else self._no_agent()
        if path == "/api/settings":              # Settings (settings.py): every value, one section, the TV's modes
            return self._json(200, {"ok": True, "values": app.shell.prefs.values()})
        if path.startswith("/api/settings/"):    # (the phone's PIN only to a loopback client, as /api/tiles)
            if path[14:] not in settings_mod.SECTIONS:
                return self._json(404, {"ok": False, "error": f"no settings section {path[14:]!r}"})
            return self._json(200, app.shell.prefs.section(path[14:], app, local))
        if path == "/api/display/modes":
            return self._json(200, app.shell.prefs.modes())
        if path == "/api/build":                 # the builder (builder.py): what it is doing, or idle
            b = getattr(app.agent, "builder", None)
            return self._json(200, {"ok": True, **b.status()}) if b else \
                self._json(404, {"ok": False, "error": "no builder (settings.builder)"})
        if path == "/api/sleep":
            st = _sleep(app.shell, "sleep_state", full=True)
            return self._json(200, {**st, "ok": True}) if st is not None else \
                self._json(503, {"ok": False, "error": "sleep unavailable"})
        if path == "/mcp":
            return self._json(405, {"ok": False, "error": "POST only"}, [("Allow", "POST")])
        if path.startswith("/api/snap/"):        # an app switcher card's snapshot (its URL carries ?v=)
            png = app.shell.snap(unquote(path[10:]))
            return self._send(200, png, TYPES[".png"], "max-age=86400") if png else \
                self._json(404, {"ok": False, "error": "no snapshot"})
        parts = path.split("/")                  # /art/<steam|demo>/<id>/<kind>
        if len(parts) == 5 and parts[1] == "art":
            if parts[2] == "steam":
                f = app.shell.art_file(parts[3], parts[4])
                if f:
                    return self._send(200, Path(f).read_bytes(), TYPES.get(Path(f).suffix.lower(), "image/jpeg"), "max-age=86400")
            elif parts[2] == "demo" and app.shell.demo:
                svg = shell_mod.demo_art(unquote(parts[3]), parts[4])
                if svg:
                    return self._send(200, svg.encode(), TYPES[".svg"], "max-age=86400")
        return self._json(404, {"ok": False, "error": "not found"})

    def _post(self, path, raw):
        app = self.server.app
        if path == "/mcp":
            if not app.agent:
                return self._no_agent()
            status, obj = app.agent.mcp(raw, self.headers)
            return self._json(status, obj)
        try:
            data = json.loads(raw)
        except ValueError:
            return self._json(400, {"ok": False, "error": "bad JSON"})
        if not isinstance(data, dict):
            return self._json(400, {"ok": False, "error": "expected a JSON object"})
        bad = lambda why: self._json(400, {"ok": False, "error": why})
        if path == "/api/pair":
            pin = data.get("pin")
            if not (isinstance(pin, str) and len(pin) == 6 and pin.isascii() and pin.isdigit()):
                return bad("pin must be 6 digits")
            status, res = app.pairing.pair(pin)
            cookie = [("Set-Cookie", f"sh_token={res['token']}; HttpOnly; SameSite=Strict; Max-Age=31536000; Path=/")] \
                if status == 200 else []
            return self._json(status, res, cookie)
        if path == "/api/ui":
            vals = [data.get(k) for k in ("focused", "row", "screen", "launch")]
            if not all(v is None or isinstance(v, str) and len(v) <= 200 for v in vals):
                return bad("focused/row/screen/launch must be short strings or null")
            app.shell.ui_report(*vals)
            return self._json(200, {"ok": True})
        if path == "/api/launch":
            tid = data.get("id")
            if not (isinstance(tid, str) and 0 < len(tid) <= 200):
                return bad("id must be a tile id")
            return self._json(200, app.shell.open(tid))
        if path == "/api/close":                 # the app switcher: a person at the TV. Our own pages only (gate() has
            if not self.headers.get("Origin"):   # refused other Origins): a bare loopback POST is an agent, not a person
                return self._json(403, {"ok": False, "error": "the app switcher's own page only (agents: close_app)"})
            key, force = data.get("key"), data.get("force", False)
            if not (isinstance(key, str) and 0 < len(key) <= 200 and isinstance(force, bool)):
                return bad("expected {key: an open app's key, force?: true|false}")
            return self._json(200, app.shell.close_app(key, force, person=True))
        if path in ("/api/build/keep", "/api/build/discard", "/api/build/stop"):   # the Keep sheet: a person at
            if not self.headers.get("Origin"):   # the TV (our own pages; gate() refused other Origins), never an agent
                return self._json(403, {"ok": False, "error": "the TV's own Keep sheet only (agents: the build tool)"})
            b = getattr(app.agent, "builder", None)
            if not b:
                return self._json(404, {"ok": False, "error": "no builder (settings.builder)"})
            return self._json(200, b.stop() if path.endswith("stop") else b.answer(path.endswith("keep")))
        if path == "/api/tilt":                  # a remote's resting finger leans the focused tile (Siri Remote, tvOS-style)
            x, y = data.get("x"), data.get("y")
            if not all(type(v) in (int, float) and -1 <= v <= 1 for v in (x, y)):
                return bad("x and y must be numbers from -1 to 1")
            app.shell.emit("tilt", {"x": round(x, 3), "y": round(y, 3)})
            return self._json(200, {"ok": True})
        if path == "/api/pointer":               # the phone trackpad: user input, never an agent tool
            v = {k: data.get(k, 0) for k in ("dx", "dy", "scroll_x", "scroll_y")}
            if not all(type(n) is int and abs(n) <= (4000 if k[0] == "d" else 40) for k, n in v.items()) \
                    or data.get("click") not in (None, "left", "right"):
                return bad('expected integers dx/dy (within 4000) and scroll_x/scroll_y (within 40), click "left"/"right"')
            if _sleep(app.shell, "wake", "phone"):   # a press that wakes the TV only wakes it (and so do its echoes)
                return self._json(200, {"ok": True, "woke": True})
            return self._json(200, app.shell.pointer(click=data.get("click"), **v))
        if path == "/api/action":
            name, args = data.get("name"), data.get("args") or {}
            if not (isinstance(name, str) and 0 < len(name) <= 64 and isinstance(args, dict)):
                return bad("expected {name, args}")
            if self.headers.get("Origin") and name in PRESSES and _sleep(app.shell, "wake", "phone"):
                return self._json(200, {"ok": True, "woke": True})   # (the phone's button: an agent's acts, via _action)
            if not app.agent:
                return self._no_agent()
            # our own pages (the phone remote) may drive an open app switcher (navigate, select) as the person; every
            # other call here is an agent's, close_app included (never a game, never force). ponytail: Origin marks our
            # pages (gate() refused others), it isn't auth: a local process can send one
            person = bool(self.headers.get("Origin")) and name in ("navigate", "select")
            res = app.agent.call(name, args, person=person)
            if isinstance(res, dict) and isinstance(res.get("image"), (bytes, bytearray)):
                res = {**res, "image": base64.b64encode(res["image"]).decode()}
            return self._json(200, res)
        if path == "/api/voice":
            if not app.agent:
                return self._no_agent()
            text, action = data.get("text"), data.get("action")
            if text is not None:
                if not (isinstance(text, str) and text.strip() and len(text) <= 1000):
                    return bad("text must be 1-1000 characters")
                shell_mod._bg(app.agent.handle_text, text.strip())
            elif action in ("start", "stop", "cancel"):
                shell_mod._bg(app.agent.voice, action)
            else:
                return bad('expected {"action": "start"|"stop"|"cancel"} or {"text": "..."}')
            return self._json(200, {"ok": True})
        return self._settings(path, data)

    def _settings(self, path, data):
        """Settings' POSTs (settings.py). An agent (no Origin) may set AGENTS' keys only; the display mode, power and
        Sleep Now take the person's own pages (gate() refused other Origins). ponytail: a policy, not a boundary, as
        for the app switcher: any local process can send our Origin."""
        app, person = self.server.app, bool(self.headers.get("Origin"))
        bad = lambda why: self._json(400, {"ok": False, "error": why})
        if path.startswith("/api/settings/"):
            key = path[14:]
            if key not in settings_mod.KEYS:
                return self._json(404, {"ok": False, "error": f"no setting {key!r}"})
            if "value" not in data or not isinstance(data.get("preview", False), bool):
                return bad('expected {"value": …, "preview"?: true|false}')
            r = app.shell.prefs.change(key, data["value"], person=person, preview=data.get("preview", False))
            return self._json(403 if not person and key not in settings_mod.AGENTS else
                              400 if key == "display.mode" else 200, r)
        if path == "/api/sleep/wake":            # the launcher page saw its own input (or its keepalive): any caller
            return self._json(200, {"ok": True, "woke": bool(_sleep(app.shell, "wake", "page", press=False))})
        power = path.startswith("/api/power/")
        if power and path[11:] not in settings_mod.POWER or not power and path not in (
                "/api/display/apply", "/api/display/confirm", "/api/display/revert", "/api/sleep/now"):
            return self._json(404, {"ok": False, "error": "not found"})
        if not person:
            return self._json(403, {"ok": False, "error": PERSON})
        prefs = app.shell.prefs
        if power:
            return self._json(200, prefs.power(path[11:], person=True))
        if path == "/api/display/apply":
            if not (isinstance(data.get("mode"), str) and 0 < len(data["mode"]) <= 64):
                return bad("expected {mode: a mode id from /api/display/modes}")
            return self._json(200, prefs.apply_mode(data["mode"], person=True))
        if path == "/api/display/confirm":
            return self._json(200, prefs.keep_mode())
        if path == "/api/display/revert":
            return self._json(200, prefs.revert_mode("asked"))
        r = _sleep(app.shell, "sleep_now", "person")
        return self._json(200, r) if r is not None else self._json(503, {"ok": False, "error": "sleep unavailable"})

    def _events(self):
        app = self.server.app
        q = app.hub.add()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(sse("state", app.shell.state()))
            st = _sleep(app.shell, "sleep_state")
            if st is not None:                   # (a page that connects to a dark TV starts dark)
                self.wfile.write(sse("sleep", st))
            while not q.dead:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    msg = b": ping\n\n"
                self.wfile.write(msg)
        except OSError:
            pass
        finally:
            app.hub.remove(q)
            self.close_connection = True

    def _no_agent(self):
        return self._json(503, {"ok": False, "error": "agent unavailable (see the daemon log)"})

    def _file(self, p):
        if not p.is_file():
            return self._json(404, {"ok": False, "error": "not found"})
        return self._send(200, p.read_bytes(), TYPES.get(p.suffix.lower(), "application/octet-stream"), "no-cache")

    def _json(self, status, obj, headers=()):
        body = b"" if obj is None else json.dumps(obj, default=str).encode()
        return self._send(status, body, "application/json" if obj is not None else None, "no-store", headers)

    def _send(self, status, body, ctype, cache, headers=()):
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


def make_server(app, host, port):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.app = app
    return srv


def load_agent(shell, emit):
    try:
        from .agent import Agent
        return Agent(shell, shell.settings, emit)
    except Exception as e:                       # a broken agent must not take the launcher down
        (log.warning if isinstance(e, ImportError) else log.exception)(
            "agent unavailable (%s): /api/action, /api/voice, /api/tools and /mcp answer 503", e)
        return None


def _ticker(app):
    while True:
        time.sleep(2)                            # (none while the TV sleeps: no nvidia-smi every 2 s all night)
        if app.hub.clients and (_sleep(app.shell, "sleep_state") or {}).get("state") != "asleep":
            try:
                app.hub.emit("stats", app.shell.stats())
            except Exception:
                log.exception("stats")


def load_shell(path, demo, port, emit, user=shell_mod.USER_CONFIG):
    """Shell for --config PATH, else the user's override, else the repo default. A broken user override (bad JSON, or a
    shape Shell can't use: a row without an id, a non-numeric port...) falls back to the repo default with an error
    log, so the TV always gets a launcher; an explicit --config fails hard instead."""
    def make(config):
        if port:
            config["settings"]["port"] = port
        return shell_mod.Shell(config, demo, emit)
    try:
        return make(shell_mod.load_config(path, user))
    except Exception as e:
        if path:
            sys.exit(f"sparkhome: bad config {path}: {e!r}")
        log.error("bad config %s (%r); using the repo default", user, e)
        return make(shell_mod.load_config(shell_mod.REPO / "sparkhome.json"))


def main(args):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    hub, ref = Hub(), {}

    def emit(typ, data):                         # thread-safe; also mirrors voice phases into State.voice
        if typ == "voice" and "shell" in ref:
            ref["shell"].voice = VOICE.get((data or {}).get("phase"), "idle")
        hub.emit(typ, data)

    shell = ref["shell"] = load_shell(args.config, args.demo, args.port, emit)
    prefs = shell.prefs = settings_mod.Prefs(shell)
    prefs.load()                                 # the owner's settings over the config, before anyone reads it
    shell.on_dim = lambda on: prefs.brightness(dim=on)   # the sleep warning's half-dim, through the gamma ramp
    settings = shell.settings
    port, lan = shell.port, bool(args.lan or settings.get("lan"))
    shell.launch_chrome = not args.no_browser
    agent = prefs.agent = shell.agent = load_agent(shell, emit)   # (shell.agent: plugins' Home.ask and Home.say)
    if agent and isinstance(shell.settings.get("builder"), dict):   # a bigger model builds integrations (builder.py)
        from .builder import Builder
        agent.builder = Builder(shell, agent, emit, shell.settings)
    shell.on_voice = getattr(agent, "voice_key", None)
    host = socket.gethostname().lower()
    lan_url = f"http://{host if '.' in host else host + '.local'}:{port}/remote" if lan else None
    app = SimpleNamespace(shell=shell, agent=agent, hub=hub, pairing=Pairing(), lan=lan, port=port, lan_url=lan_url,
                          hosts={"127.0.0.1", "localhost", "::1", host, host.split(".")[0] + ".local"})
    try:
        srv = make_server(app, "0.0.0.0" if lan else "127.0.0.1", port)
    except OSError as e:
        sys.exit(f"sparkhome: can't listen on port {port}: {e}")
    shell.start()
    prefs.start()                                # the brightness once X answers, then the display guard
    if getattr(agent, "builder", None):         # a build that restarted SparkHome to load its plugin goes on, once the
        threading.Timer(8, agent.builder.resume).start()   # kiosk is back (it drives the TV)
    if agent and not args.demo:
        agent.start_mic()                        # the Siri Remote's own mic: a standing connection to its tap
    threading.Thread(target=_ticker, args=(app,), name="stats", daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    log.info("SparkHome%s on %s%s", " (demo)" if args.demo else "", url, f", phones: {lan_url}" if lan else "")
    if args.demo and not args.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- selftest

class _FakeAgent:
    def __init__(self):
        self.calls = []

    def tools(self):
        return [{"name": "home", "description": "Go to the home screen.", "inputSchema": {"type": "object"}}]

    def call(self, name, args, person=False):
        self.calls.append((name, args))
        return {"ok": True, "image": b"\x89PNG"} if name == "screenshot" else {"ok": True, "name": name, "person": person}

    def mcp(self, body, headers):
        m = json.loads(body)
        return (202, None) if "id" not in m else (200, {"jsonrpc": "2.0", "id": m["id"], "result": {}})

    def voice(self, action):
        self.calls.append(("voice", action))

    def handle_text(self, text):
        self.calls.append(("text", text))


def _selftest():
    import http.client
    import tempfile
    H = {"127.0.0.1", "localhost", "::1", "192.168.1.5", "spark", "spark.local"}
    ok = lambda *a: gate(*a, 8780, H) is None
    code = lambda *a: gate(*a, 8780, H)[0]
    assert ok("GET", "/api/state", "127.0.0.1", "127.0.0.1:8780", None, False, False)
    assert ok("POST", "/api/launch", "127.0.0.1", "127.0.0.1:8780", "http://127.0.0.1:8780", False, False)
    assert ok("POST", "/api/launch", "127.0.0.1", "localhost:8780", "http://127.0.0.1:8780", False, False)
    assert code("POST", "/api/launch", "127.0.0.1", "127.0.0.1:8780", "https://www.netflix.com", False, False) == 403
    assert code("POST", "/api/launch", "127.0.0.1", "127.0.0.1:8780", "null", False, False) == 403
    assert code("GET", "/api/tiles", "127.0.0.1", "evil.example:8780", None, False, False) == 403      # rebinding
    assert ok("GET", "/api/tiles", "127.0.0.1", None, None, False, False)                               # HTTP/1.0, no Host
    assert code("POST", "/mcp", "192.168.1.9", "192.168.1.5:8780", None, True, True) == 403                  # never over LAN
    assert ok("POST", "/mcp", "::1", "[::1]:8780", None, False, True)
    assert code("GET", "/api/state", "192.168.1.9", "spark.local:8780", None, False, True) == 401
    assert ok("GET", "/remote", "192.168.1.9", "spark.local:8780", None, False, True)
    assert ok("POST", "/api/pair", "192.168.1.9", "spark.local:8780", "http://spark.local:8780", False, True)
    assert ok("POST", "/api/launch", "192.168.1.9", "192.168.1.5:8780", "http://192.168.1.5:8780", True, True)
    assert code("POST", "/api/launch", "192.168.1.9", "spark.local:8780", "http://spark.local:8780", False, True) == 401
    assert ok("GET", "/api/state", "192.168.1.9", "spark.local:8780", None, False, False) and loopback("::ffff:127.0.0.1")
    # pairing: lockout after 5 wrong PINs, twice as long each time until the right PIN; tokens stored hashed
    tmp = Path(tempfile.mkdtemp(prefix="sparkhome-test-"))
    p = Pairing(tmp / "tokens.json")
    wrong = f"{(int(p.pin) + 1) % 10 ** 6:06d}"
    assert [p.pair(wrong)[0] for _ in range(5)] == [403] * 5 and p.pair(p.pin)[0] == 429
    p.locked_until = 0
    assert [p.pair(wrong)[0] for _ in range(5)] == [403] * 5 and 110 < p.locked_until - time.monotonic() <= 120
    p.locks, p.locked_until = 20, 0                                                    # capped at a day
    assert [p.pair(wrong)[0] for _ in range(5)] == [403] * 5 and 86000 < p.locked_until - time.monotonic() <= 86400
    p.locked_until = 0
    status, res = p.pair(p.pin)
    assert status == 200 and p.locks == 0 and p.check(res["token"]) and not p.check("x") and not p.check(None)
    assert res["token"] not in (tmp / "tokens.json").read_text() and Pairing(tmp / "tokens.json").check(res["token"])
    assert oct((tmp / "tokens.json").stat().st_mode & 0o777) == "0o600"
    # hub: a client that stops reading is dropped, emit never blocks
    hub = Hub()
    q = hub.add()
    for i in range(300):
        hub.emit("stats", {"i": i})
    assert q.dead and q not in hub.clients
    # a broken user override (bad JSON, a shape Shell can't use, a bad nav) → the repo default; an explicit --config
    # fails hard
    user, repo = tmp / "user.json", shell_mod.load_config(shell_mod.REPO / "sparkhome.json")
    for bad in ("{nope", json.dumps({"settings": {"port": "abc"}, "rows": [], "tiles": {}}),
                json.dumps({"settings": {}, "rows": ["top", {"tiles": []}], "tiles": {}}),
                json.dumps({"settings": {}, "rows": [], "tiles": {"x": {"nav": "mouse"}}})):
        user.write_text(bad)
        assert load_shell(None, True, 0, hub.emit, user).config == repo
    try:
        load_shell(str(user), True, 0, hub.emit)
        raise AssertionError("an explicit --config must fail hard")
    except SystemExit:
        pass
    assert load_shell(None, True, 8799, hub.emit, tmp / "absent.json").port == 8799
    # the real handler over loopback, with a demo shell (pinned config: users edit the repo one) and a fake agent
    shell = shell_mod.Shell({"settings": {}, "rows": [{"id": "top", "tiles": ["netflix"]}, {"id": "g", "auto": "steam"}],
                             "tiles": {"netflix": {"type": "web", "title": "Netflix",
                                                   "url": "https://www.netflix.com/browse"}}}, True, hub.emit)
    app = SimpleNamespace(shell=shell, agent=_FakeAgent(), hub=hub, pairing=p, lan=False, port=0, lan_url=None,
                          hosts={"127.0.0.1", "localhost"})
    shell.prefs = settings_mod.Prefs(shell, tmp / "settings.json", tmp / "monitors.xml", keep_s=30)   # (demo: in memory)
    shell.prefs.load()
    # the Shell's sleep (shell.py), stubbed on the instance: what the routes pass on and call is what's checked
    woke, nap = [], {"state": "awake", "minutes": 0, "game_minutes": 0, "signal": "off", "idle_s": 3, "in_s": None,
                     "held": None}
    shell.wake = lambda src, press=True: woke.append((src, press)) or woke[-1] in wakes
    shell.sleep_state = lambda full=False: {**nap, "full": full}
    shell.sleep_now = lambda src="person": woke.append(("now", src)) or {"ok": True, "state": "asleep"}
    wakes = set()                                                   # (src, press) a stub wake answers True for
    srv = make_server(app, "127.0.0.1", 0)
    port = app.port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def req(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request(method, path, body=body if body is None or isinstance(body, bytes) else json.dumps(body).encode(),
                  headers=headers or {})
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, (json.loads(raw) if raw and r.getheader("Content-Type", "").startswith("application/json") else raw)

    s, t = req("GET", "/api/tiles")
    assert s == 200 and t["demo"] and len(t["pin"]) == 6 and t["lan_url"] is None and t["rows"][0]["id"] == "top"
    assert req("GET", "/api/state")[1]["surface"] == "launcher" and "gpu" in req("GET", "/api/stats")[1]
    assert req("POST", "/api/launch", {"id": "netflix"}) == (200, {"ok": True, "surface": "web"})
    assert req("POST", "/api/launch", {"id": "netflix"}, {"Origin": "https://evil.example"})[0] == 403
    assert req("POST", "/api/launch", b"{nope", {"Content-Type": "application/json"})[0] == 400
    assert req("POST", "/api/launch", [1, 2])[0] == 400 and req("POST", "/api/launch", {"id": 5})[0] == 400
    assert req("POST", "/api/launch", None, {"Content-Length": str(MAX_BODY + 1)})[0] == 413
    assert req("GET", "/nope")[0] == 404 and req("GET", "/ui/../sparkhome.json")[0] == 404
    assert req("GET", "/ui/%2e%2e/sparkhome.json")[0] == 404 and req("POST", "/api/nope", {})[0] == 404
    assert req("POST", "/api/ui", {"focused": "netflix", "row": "top", "screen": "home"}) == (200, {"ok": True})
    assert shell.state()["focused"] == "netflix" and req("POST", "/api/ui", {"focused": 3})[0] == 400
    assert req("POST", "/api/ui", {"screen": "home", "launch": "steam:1"})[0] == 200 and shell.ui["launch"] == "steam:1"
    assert req("POST", "/api/ui", {"launch": 1})[0] == 400 and shell.ui["launch"] == "steam:1"
    assert req("POST", "/api/action", {"name": "home", "args": {}})[1] == {"ok": True, "name": "home", "person": False}
    me = {"Origin": f"http://127.0.0.1:{port}"}  # our pages: the person for the switcher's moves and OK, nothing else
    person = lambda n: req("POST", "/api/action", {"name": n}, me)[1]["person"]
    assert [person(n) for n in ("navigate", "select", "close_app", "home")] == [True, True, False, False]
    assert req("POST", "/api/action", {"name": "screenshot"})[1]["image"] == base64.b64encode(b"\x89PNG").decode()
    assert req("POST", "/api/voice", {"text": "open netflix"}) == (200, {"ok": True})
    assert req("POST", "/api/pointer", {"dx": 3, "dy": -4000, "scroll_y": -40, "click": "left"}) == (200, {"ok": True})   # demo: drawn
    for bad in ({"dx": 1.5}, {"dy": True}, {"dx": 4001}, {"scroll_x": 41}, {"click": "middle"}, {"dx": "3"}):
        assert req("POST", "/api/pointer", bad)[0] == 400, bad
    assert req("POST", "/api/pointer", {"dx": 1}, {"Origin": "https://www.netflix.com"})[0] == 403
    assert req("POST", "/api/tilt", {"x": .5, "y": -1}) == (200, {"ok": True})
    for bad in ({"x": 1.5, "y": 0}, {"x": True, "y": 0}, {"x": "0", "y": 0}, {"x": 0}):
        assert req("POST", "/api/tilt", bad)[0] == 400, bad
    assert req("POST", "/api/voice", {"action": "explode"})[0] == 400
    assert req("GET", "/api/tools")[1][0]["name"] == "home"
    assert req("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"})[1]["id"] == 1
    assert req("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}) == (202, b"")
    assert req("GET", "/mcp")[0] == 405 and req("DELETE", "/mcp")[0] == 405
    s, svg = req("GET", "/art/demo/1091500/hero")
    assert s == 200 and svg.startswith(b"<svg") and req("GET", "/art/demo/1/hero")[0] == 404
    assert req("GET", "/art/steam/1091500/hero")[0] == 404                          # demo: no Steam
    assert req("POST", "/api/pair", {"pin": "12"})[0] == 400 and req("POST", "/api/pair", {"pin": "١٢٣٤٥٦"})[0] == 400
    s, res = req("POST", "/api/pair", {"pin": p.pin})
    assert s == 200 and p.check(res["token"])
    c = socket.create_connection(("127.0.0.1", port), timeout=5)                    # SSE: state first, then events
    c.sendall(b"GET /api/events HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
    f = c.makefile("rb")
    while f.readline() not in (b"\r\n", b""):
        pass
    assert f.readline() == b"event: state\n" and json.loads(f.readline()[6:])["surface"] == "web"
    f.readline()
    assert f.readline() == b"event: sleep\n" and json.loads(f.readline()[6:]) == {**nap, "full": False}  # (the TV's
    f.readline()                                                                    # sleep state, at once)
    hub.emit("toast", {"text": "hi", "kind": "info"})
    assert f.readline() == b"event: toast\n"
    shell.switcher(), shell.swipe("up", 9e3)                                       # the app switcher's events
    seen = []
    while "swipe" not in seen:
        line = f.readline()
        seen += [line[7:-1].decode()] if line.startswith(b"event: ") else []
    assert "switcher" in seen, seen
    c.close()
    # the app switcher's routes: a card's snapshot; /api/close only from our own pages (a person), checked shape
    assert req("GET", "/api/snap/steam%3A1174180?v=1")[0] == 404
    shell.open("steam:1174180"), shell.home()                                       # demo: a snapshot of the game left
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/api/snap/steam%3A1174180?v=1")
    r = c.getresponse()
    assert (r.status, r.getheader("Content-Type"), r.getheader("Cache-Control")) == (200, "image/png", "max-age=86400")
    assert r.read().startswith(b"\x89PNG\r\n\x1a\n")
    c.close()
    assert req("POST", "/api/close", {"key": "netflix"})[0] == 403                 # no Origin: a loopback agent
    for bad in ({"key": ""}, {"key": 5}, {"key": "x" * 201}, {"key": "netflix", "force": "true"}, {}):
        assert req("POST", "/api/close", bad, me)[0] == 400, bad
    assert req("POST", "/api/close", {"key": "netflix"}, me) == (200, {"ok": True, "state": "closed", "title": "Netflix"})
    assert req("POST", "/api/close", {"key": "steam:1174180", "force": False}, me)[1]["state"] == "closing"
    assert req("POST", "/api/close", {"key": "nope"}, me) == (200, {"ok": False, "error": "Nope isn't open"})
    # the builder's Keep sheet: the person's press only; with no builder set up, there's nothing to answer
    assert req("POST", "/api/build/keep", {})[0] == 403 and req("POST", "/api/build/keep", {}, me)[0] == 404
    assert req("GET", "/api/build")[0] == 404
    # Settings: any caller reads; an agent (no Origin) sets AGENTS' keys only (403), the mode never here (400), and the
    # display mode, power and Sleep Now take the person's own pages (403 without our Origin); unknown names are 404
    s, v = req("GET", "/api/settings")
    assert s == 200 and v["ok"] and set(v["values"]) == set(settings_mod.KEYS) and v["values"]["sleep.minutes"] == 0
    s, d = req("GET", "/api/settings/display")
    assert s == 200 and d["section"] == "display" and d["values"]["display.mode"] == "1920x1080@60.000"
    assert req("GET", "/api/settings/nope")[0] == 404 and req("GET", "/api/settings/")[0] == 404
    assert req("GET", "/api/settings/network")[1]["info"]["phone"]["pin"] == p.pin  # (loopback: the PIN; the LAN's
    assert shell.prefs.section("network", app)["info"]["phone"]["pin"] is None      # never)
    assert req("GET", "/api/settings/sleep")[1]["info"]["full"] is True and req("GET", "/api/sleep")[1] == {
        **nap, "full": True, "ok": True}
    assert req("GET", "/api/display/modes")[1]["current"] == "1920x1080@60.000"
    set_ = lambda k, body, h=None: req("POST", f"/api/settings/{k}", body, h)  # noqa: E731
    assert set_("display.brightness", {"value": 120}) == (200, {"ok": True, "key": "display.brightness", "value": 120})
    assert set_("sleep.minutes", {"value": 30}, me)[1]["value"] == 30 and shell.settings["sleep"]["minutes"] == 30
    s, r = set_("tts.voice", {"value": "bf_emma"})
    assert s == 403 and r["error"].startswith("Only the person at the TV") and "voice" not in shell.settings.get("tts", {})
    assert set_("tts.voice", {"value": "bf_emma"}, me)[1]["ok"] and shell.settings["tts"]["voice"] == "bf_emma"
    assert set_("sleep.minutes", {"value": 7}, me) == (200, {"ok": False, "error": "sleep.minutes must be one of: 0, 5, "
                                                            "10, 15, 30, 60"})
    assert [set_("nope", {"value": 1}, me)[0], set_("display.mode", {"value": "x"})[0],
            set_("display.mode", {"value": "x"}, me)[0]] == [404, 403, 400]
    for bad in ({}, {"value": 1, "preview": "yes"}, [1]):
        assert set_("display.brightness", bad, me)[0] == 400, bad
    for path in ("/api/display/apply", "/api/display/confirm", "/api/display/revert", "/api/power/reboot",
                 "/api/sleep/now"):
        assert req("POST", path, {"mode": "3840x2160@59.940"}) == (403, {"ok": False, "error": PERSON}), path
    assert req("POST", "/api/power/explode", {}, me)[0] == 404 and req("POST", "/api/display/nope", {}, me)[0] == 404
    assert req("POST", "/api/display/apply", {"mode": 5}, me)[0] == 400
    assert req("POST", "/api/display/apply", {"mode": "3840x2160@59.940"}, me)[1]["error"].startswith(
        "Quit Red Dead Redemption 2 first")                                     # (the demo's game, still quitting)
    shell.running_apps = lambda: []
    assert req("POST", "/api/display/apply", {"mode": "3840x2160@59.940"}, me)[1] == {"ok": True, "state": "pending",
                                                                                      "seconds": 30}
    assert req("POST", "/api/display/confirm", {}, me)[1] == {"ok": True, "state": "kept", "mode": "3840x2160@59.940"}
    assert req("POST", "/api/display/apply", {"mode": "1920x1080@60.000"}, me)[1]["state"] == "pending"
    assert req("POST", "/api/display/revert", {}, me)[1] == {"ok": True, "state": "reverted", "mode": "3840x2160@59.940"}
    assert req("POST", "/api/power/reboot", {}, me) == (200, {"ok": True})  # (demo: only a toast says so)
    assert req("POST", "/api/sleep/now", {}, me)[1] == {"ok": True, "state": "asleep"} and woke[-1] == ("now", "person")
    assert req("POST", "/api/sleep/wake", {}) == (200, {"ok": True, "woke": False}) and woke[-1] == ("page", False)
    # the phone's presses (our Origin) wake a dark TV and do nothing else; an agent's acts (its wake: Shell._action)
    agent = app.agent
    agent.calls.clear()
    wakes.add(("phone", True))
    assert req("POST", "/api/action", {"name": "select"}, me) == (200, {"ok": True, "woke": True}) and not agent.calls
    assert req("POST", "/api/pointer", {"dx": 5}) == (200, {"ok": True, "woke": True})
    assert req("POST", "/api/action", {"name": "select"})[1]["name"] == "select" and agent.calls == [("select", {})]
    assert req("POST", "/api/action", {"name": "status"}, me)[1]["name"] == "status"   # (no press: reads never wake)
    wakes.clear()
    assert req("POST", "/api/action", {"name": "select"}, me)[1]["person"] is True and req(
        "POST", "/api/pointer", {"dx": 5}) == (200, {"ok": True})
    srv.shutdown()
    srv.server_close()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
