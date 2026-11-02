"""SparkHome Shell: everything the launcher does to the box.

Config and tiles (Steam library discovery and Steam's own cached art), the Chrome kiosk (supervised, driven over one CDP
connection: a tab per web tile with UA override, a `sparkhome` binding and a gamepad/remote bridge script), X11 window
choreography through ctypes libX11/libXtst (after GameSpark tools/xinput.py), Steam's console log, native launches,
stats, volume, sleep (the picture off after idle) and the actions agents use. --demo simulates all of it so the UI can
be built on a Mac.
"""
import base64
import ctypes
import datetime
import difflib
import fnmatch
import functools
import glob
import hashlib
import html
import http.client
import json
import logging
import os
import random
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import zlib
from collections import OrderedDict
from pathlib import Path

from . import inputs, plugins, siri
from .cdp import CDP

log = logging.getLogger("sparkhome")
REPO = Path(__file__).resolve().parent.parent
USER_CONFIG = Path("~/.config/sparkhome/sparkhome.json").expanduser()
TOOL_PREFIXES = ("Proton", "Steam Linux Runtime", "Steamworks")
# Source GameSpark's env.sh with GAMESPARK_STEAM (settings.steam_setup) cleared while a Steam runs: env.sh then detects
# that Steam's setup, so steam_launch writes to its pipe and reads its log. The setting only picks a cold start's Steam.
GS_ENV = 'pgrep -f "[u]buntu12_32/steam " >/dev/null && GAMESPARK_STEAM=; . "$1/lib/env.sh"; '
ART = {"hero": ("library_hero.*",), "logo": ("logo.*",), "capsule": ("library_capsule.*", "library_600x900.*"),
       "header": ("library_header.*", "header.*")}
# The demo's library mirrors the Spark's (most recently played first); its art is generated, or copied from a real Steam
# library into settings.demo_steam_root (see README, "Try the demo on the Mac").
DEMO_GAMES = {"292030": "The Witcher 3: Wild Hunt", "1174180": "Red Dead Redemption 2", "391220": "Rise of the Tomb Raider",
              "1091500": "Cyberpunk 2077", "435150": "Divinity: Original Sin 2", "4078430": "STAR WARS: Galactic Racer"}
DEMO_PROFILES = {"1091500": "DLSS and frame generation on, DLSS libraries kept current",   # GameSpark's summaries
                 "292030": "Skips the launcher, DLSS on, the online-check hitch every 100 ms gone",
                 "435150": "Skips the Larian launcher, DualSense pads work as controllers",
                 "4078430": "Nanite off (no flicker), fast cores only (no hitches), bigger audio buffer, patched FEX "
                            "for Denuvo"}
DEMO_STUCK = {"steam:1174180"}                   # demo: RDR2 ignores a graceful close (the stuck → force path)
DEMO_WINDOWS = {"steam:1174180": 2}              # demo: RDR2 plus its Rockstar Launcher ("2 windows")
SNAP = (640, 360)                                # app switcher snapshot box. ponytail: 30-60 ms of Xorg per leave [M];
                                                 # (480, 270) halves that (10-30 ms) if a game behind hitches
DRAWN = (256, 144)                               # a launch's "has the game drawn?" read (blank() still samples ~420 px)
LOG_RE = re.compile(r"Game process (added|removed)\s*: AppID (\d+)")   # (steamclient.so: "Game process %s : AppID %u ",
                                                                        # "Game process removed: AppID %u ")
# A Steam game's launch (LAUNCH-DESIGN.md §1.2), in seconds. No settings: the TV test tunes them once from the
# hand-over's journal line. PROMPT/NOPROC count from Steam taking the launch (a cold Steam start alone takes up to 120 s),
# EXIT from its process ending, SETTLE/DRAW from the game's window appearing, NOCONFIG from Steam starting the process
PROMPT_S, NOPROC_S, EXIT_S, SETTLE_S, DRAW_S, NOCONFIG_S = 20, 60, 10, 1.5, 20, 30
# Steam's launch prompts by window title (its UI bundle's English strTitles), full-matched: EULA, install, cloud
# conflict, "Launching..." (shaders with Skip, cloud sync, install scripts, downloads), another game running, errors
PROMPT_RE = re.compile(r"(?:Steam - )?(?:EULA|Install|Cloud Conflict|Launching\.\.\.|Launch .+|Setup failed|Error)"
                       r"(?: - Steam)?")
# GameSpark tools/launch.sh's record of what each launch applied (its own default path)
LAUNCHES = os.path.join(os.environ.get("SG_DATA") or os.path.expanduser("~/.local/share/gamespark"), "launch",
                        "launches.log")
SINK = "@DEFAULT_AUDIO_SINK@"
DIRS = ("up", "down", "left", "right")
MEDIA = ("play", "pause", "toggle", "next", "previous", "stop")
BAD_FLAGS = ("--remote-allow-origins", "--enable-automation", "--remote-debugging-pipe", "--remote-debugging-port",
             "--remote-debugging-address", "--disable-component-update")
CDP_KEYS = {"up": ("ArrowUp", "ArrowUp", 38, None), "down": ("ArrowDown", "ArrowDown", 40, None),
            "left": ("ArrowLeft", "ArrowLeft", 37, None), "right": ("ArrowRight", "ArrowRight", 39, None),
            "enter": ("Enter", "Enter", 13, "\r"), "escape": ("Escape", "Escape", 27, None), "space": (" ", "Space", 32, " "),
            "next": ("MediaTrackNext", "MediaTrackNext", 176, None),
            "previous": ("MediaTrackPrevious", "MediaTrackPrevious", 177, None),
            "N": ("N", "KeyN", 78, "N"), "P": ("P", "KeyP", 80, "P")}
X_KEYS = {"up": "Up", "down": "Down", "left": "Left", "right": "Right", "enter": "Return", "escape": "Escape",
          "play": "XF86AudioPlay", "pause": "XF86AudioPause", "toggle": "XF86AudioPlay", "next": "XF86AudioNext",
          "previous": "XF86AudioPrev", "stop": "XF86AudioStop"}
# Sleep (README "Sleep"): the Shell owns it, as TV mode turns GNOME's idle dim, screensaver and lock off. In seconds
SLEEP_WARN_S = 10        # the dim and "Going to sleep" before the picture goes off
SLEEP_OFF_DELAY_S = 1.5  # asleep: the launcher fades to black before the signal goes
WAKE_GRACE_S = 0.7       # presses this soon after a wake only wake too (a key's repeats; one press seen twice): app.js's 700 ms
X_IDLE_ASLEEP_S = 2      # while asleep: how often mutter's idle time is read (X input turns DPMS on by itself)
# The TV itself (README "Sleep" › The TV itself): a Roku TV's ECP, on the LAN (settings.tv). Only these wakes turn it on:
# a person's input, never an agent's, X input (a bench's XTest at 3 am) or the launcher page's (2 of the TV's 13
# power-ons came with a "page" wake within 1 s [M]: a Chromecast that switched the TV on must not lose it to the Spark)
TV_WAKERS = {"siri", "pad", "phone", "voice", "key"}
TV_UP_S = 10             # its wait after a wake-on-LAN or PowerOn: the TV's SSDP reply says WAKEUP: …;Timeout=10 [M]
SSDP_ECP = b'M-SEARCH * HTTP/1.1\r\nHost: 239.255.255.250:1900\r\nMan: "ssdp:discover"\r\nST: roku:ecp\r\nMX: 1\r\n\r\n'
PRESSES = {"navigate", "select", "back", "home", "press", "media", "volume", "switcher"}   # a remote's buttons
SCREEN = {"open", "home", "press", "switcher", "back", "select", "navigate", "search", "media", "type_text", "click",
          "open_title", "close_app"}                                                         # what changes the screen
_warned = set()


def warn_once(key, msg, *a):
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *a)


# ---------------------------------------------------------------- config

def load_config(path=None, user=USER_CONFIG):
    """--config wins; else ~/.config/sparkhome/sparkhome.json is used INSTEAD of the repo default (no merge)."""
    p = Path(path) if path else user if user.exists() else REPO / "sparkhome.json"
    cfg = json.loads(p.read_text())
    if not (isinstance(cfg, dict) and isinstance(cfg.get("settings"), dict) and isinstance(cfg.get("rows"), list)
            and isinstance(cfg.get("tiles"), dict)):
        raise ValueError(f"{p}: needs settings, rows and tiles")
    for k, t in cfg["tiles"].items():
        if isinstance(t, dict) and t.get("nav") not in (None, *NAV):
            raise ValueError(f"{p}: tile {k!r} has nav {t['nav']!r}; use one of {', '.join(NAV)} (or leave it out)")
        if isinstance(t, dict) and not isinstance(t.get("skin", False), (bool, dict)):
            raise ValueError(f'{p}: tile {k!r} has skin {t["skin"]!r}; use true, false or {{"hide": [selectors]}}')
        if isinstance(t, dict) and not isinstance(t.get("tiles", ""), str):
            raise ValueError(f"{p}: tile {k!r} has tiles {t['tiles']!r}; use a CSS selector string")
    return cfg


def expand(v):
    """~ expansion for path and argv settings. ponytail: every string starting with ~ is taken as a path."""
    if isinstance(v, str):
        return os.path.expanduser(v) if v.startswith("~") else v
    if isinstance(v, list):
        return [expand(x) for x in v]
    if isinstance(v, dict):
        return {k: expand(x) for k, x in v.items()}
    return v


# ---------------------------------------------------------------- Steam

def vdf(text):
    """Minimal Valve KeyValues text parser (appmanifest_*.acf, libraryfolders.vdf) → nested dicts."""
    root, stack, key = {}, [], None
    cur = root
    for s, brace in re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text):
        if brace == "{":
            stack.append(cur)
            cur[key] = cur = {}
            key = None
        elif brace == "}":
            cur, key = (stack.pop() if stack else cur), None
        elif key is None:
            key = s
        else:
            cur[key] = re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)
            key = None
    return root


def acf(path):
    """An appmanifest's AppState with lower-cased keys."""
    top = vdf(Path(path).read_text(errors="replace"))
    state = next((v for v in top.values() if isinstance(v, dict)), {})
    return {k.lower(): v for k, v in state.items()}


def find_art(steam_root, appid, kind):
    """Steam's cached library art in either layout (hashed subfolders or flat); the newest file wins."""
    base = glob.escape(os.path.join(steam_root, "appcache", "librarycache", str(appid)))
    hits = [p for pat in ART[kind] for p in glob.glob(os.path.join(base, "**", pat), recursive=True)
            if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
    try:
        return max(hits, key=os.path.getmtime) if hits else None
    except OSError:                              # Steam replaced the file meanwhile
        return None


def steam_log_events(text):
    """console_log.txt text → [("added"|"removed", appid)]."""
    return [(m.group(1), m.group(2)) for m in LOG_RE.finditer(text)]


def follow(path):
    """A log's new complete lines as text on each next() ("" if none): from its end at first (old history is stale), a
    file that appears later from its start; rotation (a new inode: the old file is finished first, its last line may
    be the one that matters) and truncation are followed, and a failed reopen is retried on the next call."""
    f, ino, rest, first = None, None, b"", True
    while True:
        data = b""
        try:
            st = os.stat(path)
            if f is None or st.st_ino != ino or st.st_size < f.tell():
                if f:
                    data = f.read() if st.st_ino != ino else b""
                    f.close()
                    f = None
                f, ino, rest = open(path, "rb"), st.st_ino, rest if data else b""
                if first:
                    f.seek(0, os.SEEK_END)
            first = False
            data += f.read()
        except OSError:
            first = False
        lines = b""
        if data:
            *done, rest = (rest + data).split(b"\n")
            lines = b"\n".join(done)
        yield lines.decode("utf-8", "replace")


def launch_record(line):
    """A launches.log line (GameSpark tools/launch.sh: `<date> <time> app <appid> applied: <files or none>`, then
    optional ` (exe -> …)`, ` (in container: …)`, ` (args: …)`, ` (FEX: …)`, then its warnings) → (appid, profile
    applied, error or None, [warnings]), else None. run.env (a benchmark's) and <appid>.Engine.ini are not the profile."""
    m = re.match(r"\S+ \S+ app (\d+) applied: (.*)", line)
    if not m:
        return None
    appid, rest = m.groups()
    files = re.split(r" \(| WARNING: | not launched: ", rest, maxsplit=1)[0].split()
    return (appid, f"{appid}.env" in files, "GameSpark's FEX build is missing" if " not launched: " in rest else None,
            [w.strip() for w in rest.split(" WARNING: ")[1:]])


def profile_summary(gs, appid):
    """GameSpark's launch profile for appid: its `# summary:` line ("" without one), None without <appid>.env (or
    without a GameSpark: Path("", …) would read the daemon's working directory)."""
    try:
        text = Path(gs, "profiles", "launch", f"{appid}.env").read_text(errors="replace") if gs else None
    except OSError:
        return None
    return None if text is None else next((s[10:].strip() for s in text.splitlines() if s.startswith("# summary:")), "")


def pipe_write(path, text, timeout):
    """Write to a FIFO without hanging when nobody reads it (Steam not running): retry a non-blocking open."""
    end = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            break
        except OSError:                      # ENXIO: no reader; ENOENT: no pipe yet
            if time.monotonic() > end:
                return False, "Steam isn't running (nobody reads its pipe)"
            time.sleep(0.5)
    try:
        os.write(fd, text.encode())
        return True, None
    finally:
        os.close(fd)


# ---------------------------------------------------------------- small helpers

def pointer_buttons(click, scroll_x, scroll_y):
    """Trackpad click/scroll → X buttons: wheel steps (4/5 up/down, 6/7 left/right; + is down/right), then the click
    (1 left, 3 right), so a tap that ends a scroll lands after it."""
    return ([5 if scroll_y > 0 else 4] * abs(scroll_y) + [7 if scroll_x > 0 else 6] * abs(scroll_x)
            + {"left": [1], "right": [3]}.get(click, []))


def local_port(url):
    """Port of a 127.0.0.1/localhost URL, else None (those tiles hide while their service is down)."""
    u = urllib.parse.urlsplit(url)
    if u.hostname in ("127.0.0.1", "localhost"):
        return u.port or (443 if u.scheme == "https" else 80)
    return None


def port_open(port, host="127.0.0.1"):
    try:
        socket.create_connection((host, port), timeout=0.5).close()
        return True
    except OSError:
        return False


def http_json(host, port, path, timeout=1):
    c = http.client.HTTPConnection(host, port, timeout=timeout)   # no proxy env surprises for local services
    try:
        c.request("GET", path)
        r = c.getresponse()
        if r.status != 200:
            raise OSError(f"HTTP {r.status}")
        return json.loads(r.read())
    finally:
        c.close()


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def png(w, h, rgb=(0, 0, 0), rows=None):
    """An RGB PNG: a solid colour (the demo's screenshot and snapshots), or rows= of filtered scanlines (rgb_rows).
    zlib level 1: a 640x360 snapshot encodes in 6-10 ms on the Spark [M]."""
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    raw = rows if rows is not None else (b"\x00" + bytes(rgb) * w) * h
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


def rgb_rows(w, h, bpl, buf):
    """XGetImage's BGRX scanlines (32 bpp, little-endian X) → PNG's filtered RGB rows, by slicing."""
    out = bytearray()
    for y in range(h):
        row, rgb = buf[y * bpl:y * bpl + w * 4], bytearray(w * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = row[2::4], row[1::4], row[0::4]
        out += b"\x00" + rgb
    return bytes(out)


def blank(w, h, bpl, buf):
    """A frame not worth showing: ~1400 sampled pixels (B, G, R, never the alpha byte) all near black, or all alike.
    Black comes from a game that stops drawing when unfocused, an unredirected flip surface, protected video."""
    s = [max(buf[o:o + 3]) for o in (y * bpl + x * 4 for y in range(0, h, 7) for x in range(0, w, 13))]
    return max(s) < 24 or max(s) - min(s) < 6


def demo_art(appid, kind):
    """Generated SVG art for a demo game, layered like Steam's: hero = gradient only, logo = title only."""
    title = DEMO_GAMES.get(str(appid))
    if not title or kind not in ART:
        return None
    w, h = {"hero": (1920, 620), "logo": (640, 360), "capsule": (600, 900), "header": (460, 215)}[kind]
    hue = int(hashlib.md5(title.encode()).hexdigest()[:4], 16) % 360
    words = html.escape(title).split()
    size = {"logo": 92, "capsule": 96, "header": 46}.get(kind, 0)
    lines = "".join(f'<tspan x="50%" dy="{0 if i == 0 else 1.05}em">{wd}</tspan>' for i, wd in enumerate(words))
    text = (f'<text x="50%" y="{h / 2 - (len(words) - 1) * size * 0.52:.0f}" font-family="system-ui,sans-serif" '
            f'font-weight="800" font-size="{size}" fill="#fff" text-anchor="middle" dominant-baseline="middle" '
            f'style="letter-spacing:-0.02em;paint-order:stroke" stroke="rgba(0,0,0,.35)" stroke-width="4">{lines}</text>')
    bg = (f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="hsl({hue},70%,46%)"/>'
          f'<stop offset="1" stop-color="hsl({(hue + 50) % 360},75%,14%)"/></linearGradient></defs>'
          f'<rect width="{w}" height="{h}" fill="url(#g)"/>'
          + "".join(f'<circle cx="{w * x:.0f}" cy="{h * y:.0f}" r="{min(w, h) * r:.0f}" fill="hsl({(hue + d) % 360},90%,70%)" '
                    f'fill-opacity="{o}"/>' for x, y, r, d, o in
                    ((0.8, 0.25, 0.55, 30, 0.18), (0.15, 0.85, 0.4, -40, 0.14), (0.55, 0.6, 0.18, 80, 0.2))))
    body = text if kind == "logo" else bg if kind == "hero" else bg + text
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">{body}</svg>'


def _bg(fn, *a):
    def run():
        try:
            fn(*a)
        except Exception:
            log.exception("%s failed", getattr(fn, "__name__", fn))
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- processes (the app switcher's force quit; Linux)

def proc_table():
    """{pid: (ppid, starttime, argv)} of our own uid's processes (/proc/<pid>/stat fields 4 and 22, cmdline); {}
    without /proc. 10-15 ms on the Spark [M]."""
    out, uid = {}, os.getuid()
    for d in os.listdir("/proc") if os.path.isdir("/proc") else ():
        if d.isdigit():
            try:
                if os.stat(f"/proc/{d}").st_uid != uid:
                    continue
                with open(f"/proc/{d}/stat", "rb") as f:
                    st = f.read()
                with open(f"/proc/{d}/cmdline", "rb") as f:
                    argv = f.read().split(b"\0")
                fields = st[st.rindex(b")") + 2:].split()
                out[int(d)] = (int(fields[1]), int(fields[19]), argv)
            except (OSError, ValueError, IndexError):  # gone meanwhile
                continue
    return out


def descendants(table, root):
    """Every process under root (by ppid), root itself not included."""
    kids = {}
    for pid, (ppid, _, _) in table.items():
        kids.setdefault(ppid, []).append(pid)
    out, todo = [], list(kids.get(root, []))
    while todo:
        p = todo.pop()
        out.append(p)
        todo += kids.get(p, [])
    return out


def _exe(argv):
    """The program a process runs: argv[0], or argv[1] under FEX's loader ("FEXInterpreter /…/reaper …")."""
    b = os.path.basename(argv[0])
    return os.path.basename(argv[1]) if b.startswith(b"FEX") and len(argv) > 1 else b


def reapers(table, appid, launch=b"SteamLaunch"):
    """Steam's `reaper SteamLaunch AppId=<appid>`: the root of a game's process tree. Anchored on the program run: a
    token-only match also hits steam-launch-wrapper's argv when the wrapper doesn't exec. (launch: the selftest's own
    dummy tree says FakeLaunch, so neither can be taken for the other.)"""
    tok = b"AppId=" + str(appid).encode()
    return [p for p, (_, _, argv) in table.items() if _exe(argv) == b"reaper" and launch in argv and tok in argv]


def _protected(argv):
    """Steam itself, the desktop or the X server: a force quit whose set holds one signals nothing."""
    return any(os.path.basename(a) in (b"steam", b"steamwebhelper", b"gnome-shell", b"Xorg", b"Xwayland")
               or b"ubuntu12_32/steam" in a for a in argv[:2])


def _ours(pid):
    """pid is a live process of ours (signal 0 reaches it). False when it's gone, someone else's, or unset; a number
    from another PID namespace (a Flatpak window's _NET_WM_PID) is a root process or none here. ponytail: a pid reused
    by another process of ours reads as alive (the wait it buys ends stuck, never in a wrong close)."""
    try:
        return bool(pid) and os.kill(pid, 0) is None
    except (OSError, OverflowError):
        return False


def terminate(pids, table, grace=3.0):
    """SIGCONT+SIGTERM, then SIGKILL whatever is left after grace → (exited, killed). Through pidfds, kept only if the
    process's start time still matches the scan, so a pid reused since then is never signalled."""
    fds = {}
    for p in pids:
        try:
            fd = os.pidfd_open(p)
        except OSError:                          # gone already
            continue
        try:
            with open(f"/proc/{p}/stat", "rb") as f:
                st = f.read()
            same = int(st[st.rindex(b")") + 2:].split()[19]) == table[p][1]
        except (OSError, ValueError, IndexError):
            same = False
        if same:
            fds[fd] = p
        else:
            os.close(fd)
    for sig in (signal.SIGCONT, signal.SIGTERM):
        for fd in fds:
            try:
                signal.pidfd_send_signal(fd, sig)
            except OSError:
                pass
    end, live = time.monotonic() + grace, set(fds)
    while live and time.monotonic() < end:       # a pidfd reads ready once its process has exited
        live -= set(select.select(list(live), [], [], max(0.0, end - time.monotonic()))[0])
    for fd in live:
        try:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        except OSError:
            pass
    for fd in fds:
        os.close(fd)
    return [fds[fd] for fd in fds if fd not in live], [fds[fd] for fd in live]


# ---------------------------------------------------------------- page scripts

# Injected into every web tile (Page.addScriptToEvaluateOnNewDocument). Remote Home/Back keys (BrowserHome/BrowserBack,
# if hwdb didn't remap them) and the pad's Guide button come back through the `sparkhome` binding. The rest of the pad
# follows the tile's nav: "keys" (YouTube leanback) gets Starboard key codes 32768+i, the way VacuumTube drives
# leanback; "pad" pages read the gamepad themselves (cloud gaming); anything else ("pointer") sends D-pad/left stick,
# A, B/View and Start to the daemon, which answers with trusted arrows/Enter (Chrome's spatial navigation), Back, media.
NAV = ("keys", "pad", "pointer")
BRIDGE_JS = r"""(() => {
  if (window.__sparkhome) return;
  window.__sparkhome = true;
  let tiles = __TILES__;                         // the tile's "tiles" (read-only to the page); one Chrome can't parse
  try { document.createDocumentFragment().querySelector(tiles); } catch (e) { tiles = ''; }   // counts as none
  Object.defineProperty(window, '__sparkhomeTiles', {value: tiles});
  const send = action => { try { window.sparkhome(JSON.stringify({action})); } catch (e) {} };
  addEventListener('keydown', e => {
    const a = e.key === 'BrowserHome' ? 'home' : e.key === 'BrowserBack' ? 'back' : null;
    if (a) { e.preventDefault(); e.stopImmediatePropagation(); send(a); }
  }, true);
  if (window.top !== window) return;
  const NAV = __NAV__, KEYS = NAV === 'keys', DIRS = ['up', 'down', 'left', 'right'];
  const ACT = {0: 'select', 1: 'back', 8: 'back', 9: 'media', 12: 'up', 13: 'down', 14: 'left', 15: 'right', 16: 'home',
               17: 'up', 18: 'down', 19: 'left', 20: 'right'};
  const AXES = [[1, -1, 17], [1, 1, 18], [0, -1, 19], [0, 1, 20], [3, -1, 21], [3, 1, 22], [2, -1, 23], [2, 1, 24]];
  const held = new Map();
  const key = (type, code) => { const ev = new Event(type); ev.keyCode = code; ev.which = code; document.dispatchEvent(ev); };
  const poll = now => {
    requestAnimationFrame(poll);
    const down = new Set();
    if (document.hasFocus()) for (const p of navigator.getGamepads()) {
      if (!p || p.id.includes('28de')) continue;
      p.buttons.forEach((b, i) => b.pressed && i <= 16 && down.add(i));   // past Guide (Share on a BT Xbox Series pad)
                                                                     // a button would read as stick direction 17-24
      for (const [a, s, v] of AXES) if ((p.axes[a] || 0) * s > 0.5) down.add(v);
    }
    for (const b of down) {
      const a = b === 16 || NAV === 'pointer' ? ACT[b] : null;      // null: a leanback key code, or ignored
      if (!a && !KEYS) continue;
      const fire = () => a ? send(a) : key('keydown', 32768 + b);
      if (!held.has(b)) { fire(); held.set(b, a && !DIRS.includes(a) ? Infinity : now + 500); }
      else if (now >= held.get(b)) { fire(); held.set(b, now + 100); }
    }
    for (const b of [...held.keys()]) if (!down.has(b)) { held.delete(b); if (KEYS && b !== 16) key('keyup', 32768 + b); }
  };
  requestAnimationFrame(poll);
})();"""
# What a remote can land on: the skin's tiles, get_screen's elements, the page tools' titles.
FOCUSABLE = ('a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],[role=menuitem],'
             '[role=tab],[role=option],[role=checkbox],[role=switch],[onclick],[tabindex]:not([tabindex="-1"])')
# The TV skin, after the bridge for a "pointer" tile with "skin": the arrows move focus to the nearest visible tile that
# way (Chrome's own spatial navigation never sees them), a ring shows it, it scrolls into view, a re-render can't lose
# it, and Enter clicks a tile the page made clickable without a link or button. Text fields keep their arrows (Up/Down
# leave a one-line field); a playing full-size video on screen (over the page, or with no tiles below it) or fullscreen
# gets every key, so the player's own seek works. While the remote is in use the parked pointer hovers nothing (no
# hover previews) and the cookie banners of the common consent tools, plus the tile's skin.hide selectors, are hidden.
# A tile's "tiles" selector adds what counts as a tile (cards a page made clickable by script alone). ponytail: one
# query of the page per press (fine to a few thousand links).
SKIN_JS = r"""(() => {
  if (window.top !== window || window.__sparkhomeSkin) return;
  window.__sparkhomeSkin = true;
  const CFG = __SKIN__, SEL = __FOCUSABLE__ + (window.__sparkhomeTiles ? ',' + window.__sparkhomeTiles : '');
  const KEYS = {ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right'};
  const OPP = {up: 'down', down: 'up', left: 'right', right: 'left'}, html = () => document.documentElement;
  const HIDE = ['#onetrust-consent-sdk', '#CybotCookiebotDialog', '#usercentrics-root', '#truste-consent-track',
                '#didomi-host', '#qc-cmp2-container', '.fc-consent-root', '[id^="sp_message_container"]'];
  const sheet = new CSSStyleSheet();             // adopted: there's no <html> yet, and a SPA can't rewrite it away
  sheet.replaceSync(HIDE.concat(CFG.hide || []).map(s => s + '{display:none!important}').join('')
    + '#sh-ring{position:fixed;z-index:2147483647;pointer-events:none;box-sizing:border-box;display:none;'
    + 'border:4px solid #fff;border-radius:10px;box-shadow:0 0 0 3px #000b,0 0 26px 6px #fff9}'
    + 'html.sh-kbd #sh-ring{display:block}html.sh-kbd,html.sh-kbd *{cursor:none!important}'
    + '[data-sh-focus]{scroll-margin:80px 120px!important}'      // a row scrolls its next tile into view too
    + 'html.sh-kbd body *{pointer-events:none!important}');
  const ring = document.createElement('div');
  ring.id = 'sh-ring';
  const mount = () => {
    if (!document.adoptedStyleSheets.includes(sheet)) document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
    if (html() && !ring.isConnected) html().append(ring);
  };
  mount();
  let cur = null, last = null, kbd = false, until = 0, loop = false;
  const rect = e => e.getBoundingClientRect();
  const key = e => e.getAttribute('aria-label') || e.getAttribute('href') || e.id || e.textContent.trim().slice(0, 80);
  const scrolls = e => {                         // inside a scroller: a row's cards scrolled off to the left still count
    for (let p = e.parentElement; p; p = p.parentElement)
      if (p.scrollWidth > p.clientWidth + 1 && /auto|scroll|hidden/.test(getComputedStyle(p).overflowX)) return true;
    return false;
  };
  const tiles = () => {                          // [element, rect] of every visible tile; a modal's own while one is open
    const modal = [...document.querySelectorAll('dialog:modal,[aria-modal="true"]')].filter(m => rect(m).width * rect(m).height
      && m.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})).pop();   // (not a closed menu marked modal)
    const big = innerWidth * innerHeight / 2, out = [];
    for (const e of (modal || document).querySelectorAll(SEL)) {
      const r = rect(e);
      if (r.width >= 8 && r.height >= 8 && r.width * r.height < big && !e.matches(':disabled') && !e.closest('[inert]')
          && e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})
          && (r.right + scrollX > 0 && r.bottom + scrollY > 0 || scrolls(e))) out.push([e, r]);
    }
    return out;
  };
  const proj = (r, d) => d === 'down' ? [r.left, r.right, r.top, r.bottom] : d === 'up' ? [r.left, r.right, -r.bottom, -r.top]
    : d === 'right' ? [r.top, r.bottom, r.left, r.right] : [r.top, r.bottom, -r.right, -r.left];
  const best = (from, d, list) => {              // [nearest in line with `from`, nearest at all, its offset] past it in d
    const [x1, x2, y1, y2] = proj(from, d);
    let line = [null, Infinity], any = [null, Infinity, 0];
    for (const [e, r] of list) {
      const [u1, u2, v1, v2] = proj(r, d);
      if (e === cur || v1 < y2 - 0.3 * Math.min(y2 - y1, v2 - v1)) continue;
      const off = Math.max(0, u1 - x2, x1 - u2), mid = (u1 + u2) / 2;   // ties (centres within this tile): page order
      const s = Math.max(0, v1 - y2) + 2 * off + Math.max(0, x1 - mid, mid - x2) / 10;
      if (s < any[1]) any = [e, s, off];
      if (u1 < x2 && x1 < u2 && s < line[1]) line = [e, s];
    }
    return [line[0], any[0], any[2]];
  };
  const show = (e, scroll) => {
    if (e.tabIndex < 0 && !e.hasAttribute('tabindex')) e.setAttribute('tabindex', '-1');   // a card made clickable by JS
    if (cur) cur.removeAttribute('data-sh-focus');
    cur = e;
    e.setAttribute('data-sh-focus', '');
    e.focus({preventScroll: true});
    const r = rect(e), out = r.top < 60 || r.bottom > innerHeight - 60;
    if (scroll) e.scrollIntoView({behavior: 'smooth', block: out ? 'center' : 'nearest', inline: 'nearest'});
    last = {r, key: key(e)};
    kick();
  };
  const recover = list => {                      // focus was re-rendered away: its twin nearest its place, else the nearest
    const mid = r => [(r.left + r.right) / 2, (r.top + r.bottom) / 2], [ox, oy] = mid(last.r);
    let b = null, bs = Infinity;
    for (const [e, r] of list) {
      const [x, y] = mid(r), s = Math.hypot(x - ox, y - oy) + (key(e) === last.key ? 0 : 1e5);
      if (s < bs) [b, bs] = [e, s];
    }
    if (b) show(b, false);
  };
  const place = () => {                          // the ring follows its tile through scrolls and carousel slides
    if (kbd && cur && cur.isConnected) {
      const r = rect(cur), host = cur.closest('dialog:modal') || html();   // a modal <dialog> paints over everything
      if (ring.parentNode !== host) host.append(ring);                      // else (the top layer): the ring joins it
      // ponytail: a dialog with a CSS transform becomes the ring's containing block and offsets it; a popover ring then
      last = {r, key: last && last.key};
      Object.assign(ring.style, {left: r.left - 3 + 'px', top: r.top - 3 + 'px', width: r.width + 6 + 'px',
                                 height: r.height + 6 + 'px', borderRadius: getComputedStyle(cur).borderRadius});
    }
    if (performance.now() < until) requestAnimationFrame(place); else loop = false;
  };
  const kick = () => { until = performance.now() + 1000; if (!loop) { loop = true; requestAnimationFrame(place); } };
  // The remote is put down. A player took over: its next press goes on from the tile it played (or its twin, after a
  // re-render). The trackpad moved (pad): its next press starts over from where focus is.
  const off = pad => { if (pad) cur = last = null; if (kbd) { kbd = false; html().classList.remove('sh-kbd'); } };
  const typing = (a, d) => {                     // arrows a text field (or the suggestion list it opened) keeps
    const side = d === 'left' || d === 'right';
    if (!a || !a.tagName) return false;
    if (a.isContentEditable) return side;
    if (a.tagName === 'TEXTAREA') return side || (d === 'up' ? a.selectionStart > 0 : a.selectionEnd < a.value.length);
    if (a.tagName === 'SELECT') return !side;
    if (a.tagName !== 'INPUT') return false;
    if (a.type === 'range') return side;
    return /^(text|search|email|url|tel|password|number)$/.test(a.type) && (side || a.getAttribute('aria-expanded') === 'true');
  };
  const fixed = e => !!e && (getComputedStyle(e).position === 'fixed' || fixed(e.parentElement));
  const player = () => !!document.fullscreenElement || [...document.querySelectorAll('video')].some(v => {
    const r = rect(v);   // playing, full-width across the screen's middle, and over the page (fixed) or at its end
    return !v.paused && r.width >= innerWidth * 0.8 && r.top < innerHeight / 2 && r.bottom > innerHeight / 2
      && (fixed(v) || !tiles().some(([, t]) => t.top >= innerHeight));
  });   // ponytail: a fixed video counts even behind the page (a backdrop trailer); hit-test it if a site does that
  addEventListener('keydown', e => {
    const d = KEYS[e.key], a = document.activeElement;
    if (!d && e.key !== 'Enter' || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    if (player()) return off();
    if (!d) {                                    // Enter: links, buttons and fields act by themselves
      if (kbd && cur && a === cur && !cur.matches('a[href],area[href],button,input,select,textarea,summary,[contenteditable]')) {
        e.preventDefault(); e.stopImmediatePropagation(); cur.click();
      }
      return;
    }
    if (typing(e.composedPath()[0], d)) return;  // (a field inside a web component too)
    e.preventDefault(); e.stopImmediatePropagation();
    mount();
    if (!kbd) { kbd = true; html().classList.add('sh-kbd'); }
    const list = tiles();
    if (!cur || !cur.isConnected || !list.some(([t]) => t === cur)) {   // the first press shows where focus is
      // (if it's on screen: not a tile the trackpad scrolled away)
      const on = r => r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
      const own = list.find(([t, r]) => t === a && on(r)), seen = list.filter(([, r]) => r.top >= 0 && r.left >= 0
        && r.bottom <= innerHeight && r.right <= innerWidth), first = seen.find(([t]) => !t.closest(
        'header,nav,[role=banner],[role=navigation]')) || seen[0];             // content first, as on a TV
      return own ? show(a, true) : last ? recover(list) : first && show(first[0], true);
    }
    const from = rect(cur), [line, any, gap] = best(from, d, list);
    // Left/right stay in their row: at its end nothing moves (its tiles lie the other way); a lone item (a side menu)
    // jumps to the nearest tile that way, if it's near (not a row far down the page).
    const next = d === 'up' || d === 'down' ? any
      : line || (best(from, OPP[d], list)[0] || gap > innerHeight / 3 ? null : any);
    if (next) show(next, true);
  }, true);
  addEventListener('playing', () => {            // a video started full-size (OK on Play): the ring goes, and so does
    if (!kbd || !player()) return;               // the focus the skin gave a tile under it or a player control, so OK
    if (cur && cur === document.activeElement && cur.hasAttribute('data-sh-focus')) cur.blur();   // is the player's
    off();
  }, true);                                      // (media events don't bubble; a capture listener sees them)
  addEventListener('focusin', e => {             // the page moved focus itself (a modal opened): the ring goes along
    if (kbd && e.target !== cur && e.target instanceof Element) { cur = e.target; last = {r: rect(cur), key: key(cur)}; kick(); }
  }, true);
  addEventListener('mousemove', e => (e.movementX || e.movementY) && off(true), true);   // the trackpad: the pointer
  addEventListener('wheel', () => off(true), true);                                    // again
  addEventListener('mousedown', () => off(true), true);
  new MutationObserver(() => {
    if (!kbd) return;
    if (cur && !cur.isConnected && last) recover(tiles());
    kick();
  }).observe(document, {childList: true, subtree: true});
})();""".replace("__FOCUSABLE__", json.dumps(FOCUSABLE))
PAUSE_JS = "document.querySelectorAll('video,audio').forEach(m => m.pause())"
SENT = "sent: the page is busy and acts on it once free; don't send it again"   # an action on a busy page (Shell._act)
MEDIA_JS = r"""(a => { const m = document.querySelector('video') || document.querySelector('audio'); if (!m) return null;
  const play = a === 'play' || (a === 'toggle' && m.paused); if (play) m.play(); else m.pause();
  return play ? 'playing' : 'paused'; })(%s)"""
ELS_JS = r"""
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.opacity !== '0'; };
  const onScreen = e => { const r = e.getBoundingClientRect();
    return r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth; };
  const label = e => (e.getAttribute('aria-label') || e.innerText || e.title || e.alt || e.placeholder
    || (e.querySelector('img[alt]') || {}).alt || e.value || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const all = [...document.querySelectorAll(__FOCUSABLE__ + (window.__sparkhomeTiles ? ',' + window.__sparkhomeTiles : ''))]
    .filter(vis);
  const els = all.filter(onScreen).concat(all.filter(e => !onScreen(e)));   // what's on screen first
""".replace("__FOCUSABLE__", json.dumps(FOCUSABLE))
# playing (the agent's STILL turn reads it, and sleep's hold: PLAYING_JS): the show plays, and a muted video is the show
# only as the skin's player() sees one (full-width across the screen's middle, over the page or with no tiles below
# it), not a title page's preview or hero trailer; in open shadow roots and same-origin frames too; a big frame it
# can't read is null (unknown), never false. ponytail: a trailer with its sound on, or a muted backdrop fixed behind
# the page, reads as playing, and a show muted in a small player as not; a hit test if that bites. One snippet for both
# scripts, after ELS_JS (end() reads its `all`), so they can't drift apart
PLAYS_FN = r"""
  const half = innerWidth * innerHeight / 2;
  const seen = (r, x, y) => Math.max(0, Math.min(r.right + x, innerWidth) - Math.max(r.left + x, 0))
    * Math.max(0, Math.min(r.bottom + y, innerHeight) - Math.max(r.top + y, 0));      // its area on screen
  const fixed = e => !!e && (getComputedStyle(e).position === 'fixed' || fixed(e.parentElement));
  const end = () => !all.some(e => e.getBoundingClientRect().top >= innerHeight);     // no tiles below the screen
  let unread = false;
  const plays = (root, x, y, f) => [...root.querySelectorAll('*')].some(e => {
    if (e.shadowRoot && plays(e.shadowRoot, x, y, f)) return true;
    if (e.tagName === 'IFRAME') {                // (a frame's own coordinates start at its corner; f: it's fixed)
      const r = e.getBoundingClientRect(), d = e.contentDocument;
      if (d) return plays(d, x + r.left, y + r.top, f || fixed(e));
      unread = unread || seen(r, x, y) >= half;
      return false;
    }
    if (!/^(VIDEO|AUDIO)$/.test(e.tagName) || e.paused || e.ended) return false;
    const r = e.getBoundingClientRect();         // muted: a player, as the skin's player() sees one
    return !e.muted || r.width >= innerWidth * 0.8 && r.top + y < innerHeight / 2 && r.bottom + y > innerHeight / 2
      && (f || fixed(e) || end());
  });
"""
READ_JS = "(() => {" + ELS_JS + r"""
  const text = (document.body ? document.body.innerText : '').replace(/[ \t]+/g, ' ').replace(/\s*\n\s*/g, '\n').trim();
  const shown = els.slice(0, 60);""" + PLAYS_FN + r"""  return {title: document.title, url: location.href, text: text.slice(0, 2500),
          playing: plays(document, 0, 0) || (unread ? null : false),
          focused: shown.indexOf(document.activeElement),        // -1: nothing listed has focus
          elements: shown.map((e, i) => [i, e.getAttribute('role') || e.tagName.toLowerCase(), label(e)])};
})()"""
PLAYING_JS = "(() => {" + ELS_JS + PLAYS_FN + "  return plays(document, 0, 0) || (unread ? null : false);\n})()"
# The sleep warning over the web app in front (the launcher says it on its own page: app.js doze): "Going to sleep in
# N · Press any button to stay awake" over a dimmed page, counting down, then black (as the launcher fades) until the
# next call, with 0, takes it off, else by itself 5 s later (the signal is off by then; a daemon gone meanwhile can't
# leave a black page). A popover, in the top layer: over a fullscreen player too. No input, no focusables, outside
# <body> (get_screen's text never has it); DOM calls only (YouTube enforces Trusted Types: no innerHTML), every style
# its own (all: initial), so the page's CSS can't touch it
WARN_JS = r"""(n => {
  clearInterval(window.__sparkhomeSleepT); document.getElementById('__sparkhomeSleep')?.remove();
  if (!n) return;
  const d = document.createElement('div'), b = document.createElement('b'), s = document.createElement('b');
  d.id = '__sparkhomeSleep'; d.popover = 'manual';
  d.style.cssText = 'all:initial;position:fixed;inset:0;z-index:2147483647;display:flex;flex-direction:column;' +
    'gap:16px;align-items:center;justify-content:center;pointer-events:none;background:rgba(0,0,0,.75)';
  b.style.cssText = 'all:initial;color:#fff;font:700 57px/66px system-ui,sans-serif;font-variant-numeric:tabular-nums';
  s.style.cssText = 'all:initial;color:#fff;font:500 29px/36px system-ui,sans-serif';
  s.textContent = 'Press any button to stay awake';
  b.textContent = `Going to sleep in ${n}`; d.append(b, s); document.documentElement.append(d);
  try { d.showPopover(); } catch (e) {}
  window.__sparkhomeSleepT = setInterval(() => {
    if (--n > 0) b.textContent = `Going to sleep in ${n}`;
    else if (!n) { d.style.background = '#000'; d.replaceChildren(); }
    else if (n < -4) { clearInterval(window.__sparkhomeSleepT); d.remove(); }
  }, 1000);
})(__N__)"""
CLICK_JS = "(t => {" + ELS_JS + r"""
  const e = typeof t === 'number' ? els[t] : els.find(e => label(e).toLowerCase().includes(t.toLowerCase()));
  if (!e) return null;
  e.scrollIntoView({block: 'center'}); if (e.focus) e.focus(); e.click();
  return label(e) || e.tagName.toLowerCase();
})(%s)"""
# The page tools: a tile's title as its accessible name gives it (aria-label, aria-labelledby, its heading, its image's
# alt), outside the site's menus, header and footer. Page text: the agent treats it as data, never as instructions.
TITLE_JS = ELS_JS + r"""
  const name = e => {                            // (a labelledby element's first line: the title, not its channel)
    const h = e.querySelector('h1,h2,h3,h4,h5,h6,[role=heading]'), img = e.querySelector('img[alt]');
    const by = document.getElementById((e.getAttribute('aria-labelledby') || '').split(/\s+/)[0]);
    return (e.getAttribute('aria-label') || by && by.innerText.split('\n')[0] || h && h.innerText || img && img.alt
      || e.title || (e.innerText || '').split('\n')[0]).replace(/\s+/g, ' ').trim().slice(0, 100); };
  const card = e => e.matches('a[href],[role=link]') || e.querySelector('img,picture,h1,h2,h3,h4,h5,h6,[role=heading]')
    || window.__sparkhomeTiles && e.matches(window.__sparkhomeTiles);    // links and cards, not the page's controls
  const titled = els.filter(e => card(e) && !e.closest('nav,header,footer,[role=navigation],[role=banner],'
    + '[role=contentinfo],[role=search]')).map(e => [e, name(e)]).filter(([, t]) => /[\p{L}\p{N}].*[\p{L}\p{N}]/u.test(t));
"""
TITLES_JS = "(() => {" + TITLE_JS + r"""
  const by = new Map(), seen = new Map();       // a card's links to one place: the name the others hold ("Just Chatting",
  for (const [e, t] of titled) {                // not "Just Chatting cover image" or "148K viewers for Just Chatting")
    const k = e.getAttribute('href') || t;
    by.set(k, (by.get(k) || []).concat(t));
  }
  for (const ts of by.values()) {
    const n = t => ts.filter(x => x.includes(t)).length, t = ts.reduce((a, b) => n(b) > n(a) ? b : a);
    if (!seen.has(t.toLowerCase())) seen.set(t.toLowerCase(), t);
  }
  return {url: location.href, titles: [...seen.values()].slice(0, 40)};
})()"""
OPEN_TITLE_JS = "(t => {" + TITLE_JS + r"""
  const hit = titled.find(([, n]) => n === t);
  if (!hit) return null;
  hit[0].scrollIntoView({block: 'center'}); hit[0].focus(); hit[0].click();
  return t;
})(%s)"""


# ---------------------------------------------------------------- X11 (ctypes libX11 + libXtst, like GameSpark xinput.py)

class XKeyEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("root", ctypes.c_ulong),
                ("subwindow", ctypes.c_ulong), ("time", ctypes.c_ulong), ("x", ctypes.c_int), ("y", ctypes.c_int),
                ("x_root", ctypes.c_int), ("y_root", ctypes.c_int), ("state", ctypes.c_uint),
                ("keycode", ctypes.c_uint), ("same_screen", ctypes.c_int)]


class XClientMessageEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int), ("l", ctypes.c_long * 5)]


class XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("xkey", XKeyEvent), ("xclient", XClientMessageEvent), ("pad", ctypes.c_long * 24)]


class XWindowAttributes(ctypes.Structure):
    _fields_ = [(n, ctypes.c_int) for n in ("x", "y", "width", "height", "border_width", "depth")] + [
        ("visual", ctypes.c_void_p), ("root", ctypes.c_ulong), ("class_", ctypes.c_int), ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int), ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong), ("save_under", ctypes.c_int), ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int), ("map_state", ctypes.c_int), ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long), ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p)]


class XImage(ctypes.Structure):                  # (up to f.destroy_image: all thumb() reads)
    _fields_ = [(n, ctypes.c_int) for n in ("width", "height", "xoffset", "format")] + [("data", ctypes.c_void_p)] + [
        (n, ctypes.c_int) for n in ("byte_order", "bitmap_unit", "bitmap_bit_order", "bitmap_pad", "depth",
                                    "bytes_per_line", "bits_per_pixel")] + [
        (n, ctypes.c_ulong) for n in ("red_mask", "green_mask", "blue_mask")] + [
        ("obdata", ctypes.c_void_p), ("create_image", ctypes.c_void_p), ("destroy_image", ctypes.c_void_p)]


class XTransform(ctypes.Structure):
    _fields_ = [("m", ctypes.c_int * 9)]         # XFixed 16.16, row-major 3x3


class XRenderPictureAttributes(ctypes.Structure):   # (thumb() sets subwindow_mode only)
    _fields_ = [("repeat", ctypes.c_int), ("alpha_map", ctypes.c_ulong)] + [(n, ctypes.c_int) for n in (
        "alpha_x_origin", "alpha_y_origin", "clip_x_origin", "clip_y_origin")] + [("clip_mask", ctypes.c_ulong)] + [
        (n, ctypes.c_int) for n in ("graphics_exposures", "subwindow_mode", "poly_edge", "poly_mode")] + [
        ("dither", ctypes.c_ulong), ("component_alpha", ctypes.c_int)]


class XErrorEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("display", ctypes.c_void_p), ("resourceid", ctypes.c_ulong),
                ("serial", ctypes.c_ulong), ("error_code", ctypes.c_ubyte), ("request_code", ctypes.c_ubyte),
                ("minor_code", ctypes.c_ubyte)]


_xerr = [0]


@ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(XErrorEvent))
def _on_xerror(_d, e):
    """Xlib's default handler exits the process: BadAccess on a hotkey someone else grabbed, BadWindow on a window
    that closed between two calls. Record and carry on."""
    _xerr[0] = e.contents.error_code
    return 0


_xlibs = None


def _xlib():
    """(libX11, libXtst) with prototypes, or None (not Linux / no libraries). XInitThreads runs before anything else.
    ponytail: an X IO error (server gone = the session ended) still exits the process; systemd restarts us."""
    global _xlibs
    if _xlibs is None:
        _xlibs = False
        if sys.platform.startswith("linux"):
            try:
                X, T = ctypes.CDLL("libX11.so.6"), ctypes.CDLL("libXtst.so.6")
            except OSError as e:
                log.warning("X11 libraries unavailable: %s", e)
                return None
            vp, ul, ui, i, P = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_uint, ctypes.c_int, ctypes.POINTER
            for name, res, args in (
                    ("XInitThreads", i, []), ("XOpenDisplay", vp, [ctypes.c_char_p]), ("XCloseDisplay", i, [vp]),
                    ("XDefaultRootWindow", ul, [vp]), ("XDefaultScreen", i, [vp]), ("XInternAtom", ul, [vp, ctypes.c_char_p, i]),
                    ("XGetWindowProperty", i, [vp, ul, ul, ctypes.c_long, ctypes.c_long, i, ul, P(ul), P(i), P(ul), P(ul),
                                               P(P(ctypes.c_ubyte))]),
                    ("XFree", i, [vp]), ("XSendEvent", i, [vp, ul, i, ctypes.c_long, P(XEvent)]), ("XFlush", i, [vp]),
                    ("XSync", i, [vp, i]), ("XIconifyWindow", i, [vp, ul, i]), ("XStringToKeysym", ul, [ctypes.c_char_p]),
                    ("XKeysymToKeycode", ctypes.c_ubyte, [vp, ul]), ("XKeycodeToKeysym", ul, [vp, ui, i]),
                    ("XGrabKey", i, [vp, i, ui, ul, i, i, i]), ("XConnectionNumber", i, [vp]), ("XPending", i, [vp]),
                    ("XNextEvent", i, [vp, P(XEvent)]), ("XPeekEvent", i, [vp, P(XEvent)]), ("XSetErrorHandler", vp, [vp]),
                    ("XGetWindowAttributes", i, [vp, ul, P(XWindowAttributes)]), ("XFreePixmap", i, [vp, ul]),
                    ("XCreatePixmap", ul, [vp, ul, ui, ui, ui]), ("XGetImage", P(XImage), [vp, ul, i, i, ui, ui, ul, i])):
                f = getattr(X, name)
                f.restype, f.argtypes = res, args
            T.XTestFakeKeyEvent.restype, T.XTestFakeKeyEvent.argtypes = i, [vp, ui, i, ul]
            T.XTestFakeRelativeMotionEvent.restype, T.XTestFakeRelativeMotionEvent.argtypes = i, [vp, i, i, ul]
            T.XTestFakeButtonEvent.restype, T.XTestFakeButtonEvent.argtypes = i, [vp, ui, i, ul]
            X.XInitThreads()
            X.XSetErrorHandler(ctypes.cast(_on_xerror, vp))
            _xlibs = (X, T)
    return _xlibs or None


_xr = None


def _xrender():
    """libXrender with the prototypes thumb() uses, loaded on first use; None without it (then no snapshots)."""
    global _xr
    if _xr is None:
        _xr = False
        try:
            R = ctypes.CDLL("libXrender.so.1")
        except OSError as e:
            log.warning("libXrender unavailable (%s): no app switcher snapshots", e)
            return None
        vp, ul, ui, i = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_uint, ctypes.c_int
        for name, res, args in (("XRenderFindVisualFormat", vp, [vp, vp]), ("XRenderFindStandardFormat", vp, [vp, i]),
                                ("XRenderCreatePicture", ul, [vp, ul, vp, ul, vp]), ("XRenderFreePicture", None, [vp, ul]),
                                ("XRenderSetPictureTransform", None, [vp, ul, ctypes.POINTER(XTransform)]),
                                ("XRenderSetPictureFilter", None, [vp, ul, ctypes.c_char_p, vp, i]),
                                ("XRenderComposite", None, [vp, i, ul, ul, ul, i, i, i, i, i, i, ui, ui])):
            f = getattr(R, name)
            f.restype, f.argtypes = res, args
        _xr = R
    return _xr or None


class X11:
    """One X display connection; use it from ONE thread. Window ids and atoms are server-global, so they can be shared."""
    atoms = {}

    def __init__(self):
        self.X, self.T = _xlib() or (None, None)
        if not self.X:
            raise OSError("no X11 libraries")
        self.d = self.X.XOpenDisplay(None)
        if not self.d:
            raise OSError("cannot open the X display (DISPLAY/XAUTHORITY)")
        self.root = self.X.XDefaultRootWindow(self.d)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.d:
            self.X.XCloseDisplay(self.d)
            self.d = None

    def atom(self, name):
        if name not in X11.atoms:
            X11.atoms[name] = self.X.XInternAtom(self.d, name.encode(), 0)
        return X11.atoms[name]

    def prop(self, w, name):
        """Property → list of ints (format 32 arrives as C longs) or bytes; None when absent."""
        at, fmt, n, after = ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(), ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        if self.X.XGetWindowProperty(self.d, w, self.atom(name), 0, 1 << 16, 0, 0, ctypes.byref(at), ctypes.byref(fmt),
                                     ctypes.byref(n), ctypes.byref(after), ctypes.byref(data)) != 0 or not data:
            return None
        try:
            if fmt.value == 32:
                return list(ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[:n.value])
            return ctypes.string_at(data, n.value * fmt.value // 8)
        finally:
            self.X.XFree(data)

    def active(self):
        v = self.prop(self.root, "_NET_ACTIVE_WINDOW")
        return v[0] if v else 0

    def clients(self):
        return self.prop(self.root, "_NET_CLIENT_LIST") or []

    def wm_class(self, w):
        parts = (self.prop(w, "WM_CLASS") or b"").decode("latin-1").split("\0")
        return parts[0], parts[1] if len(parts) > 1 else ""

    def pid(self, w):
        v = self.prop(w, "_NET_WM_PID")
        return v[0] if v else None

    def name(self, w):
        v = self.prop(w, "_NET_WM_NAME") or self.prop(w, "WM_NAME") or b""
        return v.decode("utf-8", "replace") if isinstance(v, bytes) else ""

    def iconic(self, w):
        v = self.prop(w, "WM_STATE")
        return bool(v) and v[0] == 3

    def traits(self, w):
        """(plain, deletable). plain: a window a person sees as an app's own (EWMH type NORMAL or DIALOG, or none; not
        skip-taskbar; not transient for another), not a dock, the desktop's icons, a splash or a game's dialog."""
        kind = (self.prop(w, "_NET_WM_WINDOW_TYPE") or [0])[0]
        plain = (kind in (0, self.atom("_NET_WM_WINDOW_TYPE_NORMAL"), self.atom("_NET_WM_WINDOW_TYPE_DIALOG"))
                 and self.atom("_NET_WM_STATE_SKIP_TASKBAR") not in (self.prop(w, "_NET_WM_STATE") or [])
                 and not any(self.prop(w, "WM_TRANSIENT_FOR") or []))
        return plain, self.deletable(w)

    def deletable(self, w):
        """w can be asked to close: WM_PROTOCOLS lists WM_DELETE_WINDOW, and w isn't Wine's virtual desktop window
        ("Wine Desktop", "<name> - Wine Desktop": RDR2's only window on the Spark). Wine turns that window's close into
        a logoff of its whole prefix (explorer → ExitWindows → wineboot --end-session): the game and its launcher are
        ended at once, their own quit skipped. Such a card offers force quit (Cancel first) instead.
        ponytail: Proton's English title is what's matched."""
        return self.atom("WM_DELETE_WINDOW") in (self.prop(w, "WM_PROTOCOLS") or []) and \
            not re.fullmatch(r"(?:.+ - )?Wine desktop", self.name(w), re.I)

    def _root_msg(self, w, name, *l):
        """An EWMH client message about w, sent to the root window (SubstructureNotify|Redirect) as the WM expects."""
        ev = XEvent()
        c = ev.xclient
        c.type, c.send_event, c.window, c.format, c.message_type = 33, 1, w, 32, self.atom(name)
        for i, v in enumerate(l):
            c.l[i] = v
        self.X.XSendEvent(self.d, self.root, 0, (1 << 19) | (1 << 20), ctypes.byref(ev))
        self.X.XFlush(self.d)

    def activate(self, w):
        """EWMH activation as a pager (source 2, timestamp 0), which mutter 46 honours without focus-stealing checks."""
        self._root_msg(w, "_NET_ACTIVE_WINDOW", 2)

    def above(self, w, on):
        """EWMH _NET_WM_STATE add/remove ABOVE, as a pager. mutter 46: an above, unmaximized window is in its TOP layer,
        over every NORMAL one, fullscreen or focused; a window mapping under it isn't focused (a Proton game is: it
        drops X focus first) and is stacked just under it."""
        self._root_msg(w, "_NET_WM_STATE", int(bool(on)), self.atom("_NET_WM_STATE_ABOVE"), 0, 2)

    def iconify(self, w):
        self.X.XIconifyWindow(self.d, w, self.X.XDefaultScreen(self.d))
        self.X.XFlush(self.d)

    def delete(self, w):
        """Ask w to close, as its own close button would: ICCCM WM_DELETE_WINDOW sent to its client. Not
        _NET_CLOSE_WINDOW, so mutter adds no 5 s ping ("not responding" on the TV) and never turns it into
        XKillClient. False when w can't be asked (deletable(): no protocol, or Wine's desktop)."""
        if not self.deletable(w):
            return False
        ev = XEvent()
        c = ev.xclient
        c.type, c.send_event, c.window, c.format, c.message_type = 33, 1, w, 32, self.atom("WM_PROTOCOLS")
        c.l[0], c.l[1] = self.atom("WM_DELETE_WINDOW"), 0                  # (timestamp CurrentTime)
        self.X.XSendEvent(self.d, w, 0, 0, ctypes.byref(ev))
        self.X.XFlush(self.d)
        return True

    def thumb(self, w, box=SNAP):
        """w's own pixels scaled to fit box, read off-screen through XRender from the window drawable (no XComposite;
        it maps, raises and focuses nothing; measured on a covered window): (width, height, bytes_per_line, BGRX bytes),
        or None (not viewable, gone meanwhile, no libXrender). Its child windows included: Proton renders a game into a
        child of its steam_app_ window (winex11 create_client_window), which ClipByChildren would read as black."""
        R, X, d, a = _xrender(), self.X, self.d, XWindowAttributes()
        if not (R and X.XGetWindowAttributes(d, w, ctypes.byref(a)) and a.map_state == 2 and a.width and a.height):
            return None                          # (2: IsViewable)
        fmt, s = R.XRenderFindVisualFormat(d, a.visual), min(box[0] / a.width, box[1] / a.height)
        if not fmt:
            return None
        ow, oh, t = max(1, round(a.width * s)), max(1, round(a.height * s)), XTransform()
        t.m[0], t.m[4], t.m[8] = round(a.width / ow * 65536), round(a.height / oh * 65536), 65536
        _xerr[0] = 0
        src = R.XRenderCreatePicture(d, w, fmt, 1 << 8, ctypes.byref(XRenderPictureAttributes(subwindow_mode=1)))
        R.XRenderSetPictureTransform(d, src, ctypes.byref(t))   # (CPSubwindowMode: IncludeInferiors)
        R.XRenderSetPictureFilter(d, src, b"good", None, 0)
        pix = X.XCreatePixmap(d, self.root, ow, oh, 32)
        dst = R.XRenderCreatePicture(d, pix, R.XRenderFindStandardFormat(d, 0), 0, None)   # PictStandardARGB32
        R.XRenderComposite(d, 1, src, 0, dst, 0, 0, 0, 0, 0, 0, ow, oh)                    # PictOpSrc
        img, out = X.XGetImage(d, pix, 0, 0, ow, oh, 0xFFFFFFFF, 2), None                 # ZPixmap
        if img:
            im = img.contents
            out = (ow, oh, im.bytes_per_line, ctypes.string_at(im.data, im.bytes_per_line * oh))
            ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(im.destroy_image)(img)       # (XDestroyImage is a macro)
        R.XRenderFreePicture(d, src)
        R.XRenderFreePicture(d, dst)
        X.XFreePixmap(d, pix)
        X.XSync(d, 0)
        return None if _xerr[0] else out         # BadDrawable/BadMatch: it went meanwhile (ponytail: _xerr is shared,
                                                 # so another thread's X error costs one snapshot, never a wrong one)

    def keycode(self, name):
        ks = self.X.XStringToKeysym(name.encode())
        return self.X.XKeysymToKeycode(self.d, ks) if ks else 0

    def keys(self, names):
        """XTest taps of keysym names; '+' makes a chord (Shift_L+n)."""
        for spec in names:
            codes = [self.keycode(p) for p in spec.split("+")]
            if not all(codes):
                raise ValueError(f"no key for {spec} on this keyboard map")
            for c in codes:
                self.T.XTestFakeKeyEvent(self.d, c, 1, 0)
            self.X.XFlush(self.d)
            time.sleep(0.03)
            for c in reversed(codes):
                self.T.XTestFakeKeyEvent(self.d, c, 0, 0)
            self.X.XFlush(self.d)
            time.sleep(0.06)

    def pointer(self, dx, dy, buttons):
        """XTest: move the pointer by (dx, dy), then press and release each X button in turn."""
        if dx or dy:
            self.T.XTestFakeRelativeMotionEvent(self.d, dx, dy, 0)
        for b in buttons:
            self.T.XTestFakeButtonEvent(self.d, b, 1, 0)
            self.X.XFlush(self.d)
            if b <= 3:
                time.sleep(0.01)                 # a click, not a wheel step: a real press lasts a moment
            self.T.XTestFakeButtonEvent(self.d, b, 0, 0)
        self.X.XFlush(self.d)

    def type_text(self, text):
        """ASCII through XTest (Latin-1 keysyms equal code points); returns how many characters were typed."""
        shift, n = self.keycode("Shift_L"), 0
        for ch in text:
            ks = {"\n": 0xFF0D, "\t": 0xFF09}.get(ch, ord(ch))
            kc = self.X.XKeysymToKeycode(self.d, ks) if 0x20 <= ks <= 0x7E or ks > 0xFF00 else 0
            if not kc:
                continue
            up = self.X.XKeycodeToKeysym(self.d, kc, 0) != ks
            for code, down in ([(shift, 1)] if up else []) + [(kc, 1), (kc, 0)] + ([(shift, 0)] if up else []):
                self.T.XTestFakeKeyEvent(self.d, code, down, 0)
            self.X.XFlush(self.d)
            time.sleep(0.01)
            n += 1
        return n

    def grab(self, names):
        """Passive root grabs (AnyModifier) → {keycode: keysym name}; keys another client holds are logged and skipped."""
        got = {}
        for name in names:
            kc = self.keycode(name)
            if not kc:
                log.warning("hotkey %s has no keycode on this keyboard map; skipped", name)
                continue
            _xerr[0] = 0
            self.X.XGrabKey(self.d, kc, 1 << 15, self.root, 0, 1, 1)
            self.X.XSync(self.d, 0)
            if _xerr[0]:
                log.warning("hotkey %s is grabbed by another client (X error %d); skipped", name, _xerr[0])
                continue
            got[kc] = name
        return got


# ---------------------------------------------------------------- the Shell

class _FairLock:
    """Shell.lock: reentrant, and the threads waiting for it take turns. threading.RLock has no hand-off on CPython 3.12
    and 3.13 (the Spark runs 3.12): a thread that releases it and at once takes it again goes first, so Home waited out
    every page call the agent sends back to back (2 s each on a busy page), not just the one in progress (measured).
    asked: when its holder asked for it (Shell.press times a press from then, not from its turn).
    ponytail: no timeout or non-blocking acquire, as no caller uses one."""

    def __init__(self):
        self._cv, self._queue, self._owner, self._depth = threading.Condition(threading.Lock()), [], None, 0

    def acquire(self):
        me = threading.get_ident()
        with self._cv:
            if self._owner != me:
                asked = time.monotonic()
                self._queue.append(me)
                self._cv.wait_for(lambda: self._owner is None and self._queue[0] == me)
                self._owner, self.asked = self._queue.pop(0), asked
            self._depth += 1
        return True

    def release(self):
        with self._cv:
            if self._owner != threading.get_ident():
                raise RuntimeError("release of a lock this thread doesn't hold")
            self._depth -= 1
            if not self._depth:
                self._owner = None
                self._cv.notify_all()

    __enter__ = acquire

    def __exit__(self, *exc):
        self.release()


def _action(fn):
    """Actions run one at a time and never raise: failures come back as {"ok": False, "error"} (the TV keeps going).
    Input first (README "Sleep"): a Siri Remote press (siri.py's thread) that wakes the screen, or comes within
    WAKE_GRACE_S of a wake, does nothing else; whatever else changes the screen (agents, the page's and the phone's
    calls, nested actions) wakes it and acts. Both read one _woke_at, so home → press or click → open agree."""
    name = fn.__name__

    @functools.wraps(fn)
    def run(self, *a, **kw):
        siri = threading.current_thread().name == "siri-actions"
        if (siri and name in PRESSES or name in SCREEN) and self.wake("siri" if siri else "agent", press=siri) and siri:
            return {"ok": True, "woke": True, "surface": self.surface}
        with self.lock:
            try:
                return fn(self, *a, **kw)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
                log.warning("%s: %s", fn.__name__, e)
                return {"ok": False, "error": str(e) or type(e).__name__}
            except Exception as e:
                log.exception("%s failed", fn.__name__)
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return run


class Shell:
    def __init__(self, config, demo, emit):
        self.config, self.demo, self.emit = config, demo, emit
        self.settings = expand(config["settings"])
        self.tile_defs = {}
        self.plugins = plugins.load(self)        # settings.plugins: controllers, tile types, connectors (plugins.py)
        made = plugins.tile_types(self.plugins)
        for tid, t in config["tiles"].items():
            t = t if isinstance(t, dict) else {}
            if t.get("type") in made:            # a plugin's tile type: made into a built-in tile (web, exec, steam)
                try:
                    t = {**t, **made[t["type"]](dict(t))}
                except Exception as e:
                    warn_once(f"tile:{tid}", "tile %r: its %s plugin failed (%r); skipped", tid, t.get("type"), e)
                    continue
            kind = t.get("type")
            need = {"web": "url", "exec": "argv", "widget": "widget", "steam": "appid" if "appid" in t else "uri"}.get(kind)
            if not (need and t.get(need) and t.get("title")):
                warn_once(f"tile:{tid}", "tile %r is incomplete; skipped", tid)
                continue
            self.tile_defs[tid] = {**t, "id": tid, **({"argv": [str(a) for a in expand(t["argv"])]} if kind == "exec" else {})}
        self.on_voice = None
        self.on_dim = None                       # server: the warning's dim on or off (Prefs.brightness, dim=on)
        self.launch_chrome = True                # server sets False for --no-browser
        self.port = int(self.settings.get("port", 8780))
        self.origin = f"http://127.0.0.1:{self.port}"
        self.lock = _FairLock()
        self.surface, self.active, self.title, self._voice = "launcher", None, "SparkHome", "idle"
        # Sleep: its state and clocks (_clock: the selftest's fake one). _sleep_lock guards the state's changes and is
        # never held while taking self.lock; _screen_lock puts the signal and the dim in the order the state asks for
        # them. _x_from: X input after it wakes a sleeping screen. _off: this nap turned the signal off. _held: the hold
        # last logged. _sleep_cfg: settings.sleep as last checked
        self._clock, self._sleep_lock, self._screen_lock = time.monotonic, threading.Lock(), threading.Lock()
        self._sleep, self._input_at, self._woke_at, self._slept_at = "awake", self._clock(), float("-inf"), 0.0
        self._warn_at, self._x_from, self._x_polled, self._off = 0.0, 0.0, float("-inf"), False
        self._held, self._sleep_cfg, self._warn_tab = None, (None,), None   # _warn_tab: the page WARN_JS went on
        # The TV over ECP (_tv): one step at a time; its address (host:port) and MAC as last found
        self._tv_lock, self._tv_at, self._tv_mac = threading.Lock(), None, None
        self.ui = {"focused": None, "row": None, "screen": None}
        self.running = set()                     # tile ids of running games/apps
        self.log_running = set()                 # appids Steam's log says are running
        self.tabs = OrderedDict()                # web tile id → {"target", "session"}, least recently used first
        self.launcher = {"target": None, "session": None}
        self.active_tab = None                   # web tile in front (None: the launcher tab)
        self._launcher_due = False               # the kiosk's page closed behind a game: the launcher's when it's back
        self.apps = {}                           # exec tile id → {"proc", "win"}
        self.pending = {}                        # tile id → {"until", "tries"}: activate its window once it maps
        self.chrome = self.cdp = None
        self.x_ok, self._kwin, self._tile_wins = False, None, {}
        self._wins, self._seen, self._prev_running, self._keys_down = {}, set(), set(), set()
        self._log_removed, self._steam_guard, self._home_guard, self._started = False, 0.0, 0.0, False
        self._ptr_lock = threading.Lock()        # the trackpad: one X connection at a time, input kept in order
        self._bpm = next((k for k, t in self.tile_defs.items() if "bigpicture" in str(t.get("uri", ""))), None)
        # The Games row (auto: steam) lists games: "quit the game" means one (Big Picture aside). Its web tiles are
        # cloud games: closing one ends a game session, a game's person rule
        self._games_row = {i for r in config["rows"] if r.get("auto") == "steam" for i in r.get("tiles", [])}
        self._game_tabs = {i for i in self._games_row if self.tile_defs.get(i, {}).get("type") == "web"}
        wc = lambda t: [t["window_class"]] if isinstance(t.get("window_class"), str) else t.get("window_class") or []
        self._classes = {c.lower(): k for k, t in self.tile_defs.items() for c in wc(t)}   # a WM_CLASS (or a shell
        # pattern: GLFW names a window after its first title when the app sets no class, "Minecraft* 1.21.1"), or a list
        self._art, self._stats, self._stats_lock, self._last_state = {}, (0.0, None), threading.Lock(), None
        self._demo = {"util": 35.0, "temp": 52.0, "power": 48.0, "mem": 41.0, "load": 1.2, "vol": 50}
        self._demo_runs = {}                     # demo: tile id → its latest simulated launch (_demo_launch)
        self._rows, self._tiles, self.games = [], {}, {}
        sw, sr = self.settings.get("switcher"), self.settings.get("siri_remote")   # (a bad value: logged, its default)
        self.double_s, self.stuck_s = inputs.number(sw, "double_press_ms", 300) / 1000, inputs.number(sw, "stuck_s", 6)
        self.swipe_speed = siri.knobs(sr if isinstance(sr, dict) else {})["swipe_speed"]
        # The app switcher. Each field is replaced whole, never changed in place: running_apps() reads them lock-free.
        self._press, self._left, self._used, self._active_key, self._acted_at = {}, (None, 0.0), {}, None, 0.0
        self._groups, self._closable, self._closing, self._snaps, self._apps_sent = {}, set(), {}, {}, None
        self._snap_n = time.time_ns()            # the snapshots' ?v=, never one a previous run used: the kiosk's Chrome
                                                 # outlives a restart and keeps each snapshot cached for a day
        self._ptab, self._scanning, self._snap_due, self._last_app = (0.0, {}), False, None, None
        # (e): appid → when Steam started a profiled game whose launches.log line hasn't come yet; appid → when its last
        # line came. _splash: (the game launch whose loading screen was asked for last, its pending entry), the one
        # handed over (_hold). _refocused: the windows that gave the pinned kiosk the focus back, once a pin (_tick)
        self._noconfig, self._lines, self._splash, self._refocused = {}, {}, None, set()
        self._refresh()

    # ---- lifecycle

    def start(self):
        """Background work: sleep and plugins (demo too); in real mode Chrome supervisor + CDP, X11 watch/hotkeys,
        Steam log, gamepad, Siri Remote, housekeeping."""
        self._started = True
        threading.Thread(target=self._sleeper, name="sleep", daemon=True).start()
        plugins.start(self.plugins)              # (in --demo too: a plugin checks home.demo before touching hardware)
        if self.demo:
            threading.Thread(target=self._demo_ticker, name="demo-ticker", daemon=True).start()
            return
        _bg(self._screen, True)                  # a daemon restarted while the signal was off must not leave it off: a
                                                 # fresh state is awake, so no wake would turn it on
        _xlib()                                  # XInitThreads before any thread touches X
        self.cdp = CDP(int(self.settings.get("cdp_port", 9223)), self._cdp_event, self._cdp_connected).start()
        for fn in (self._chrome_supervisor, self._watch, self._tail_steam_log, self._housekeeping):
            threading.Thread(target=fn, name=fn.__name__.strip("_"), daemon=True).start()
        threading.Thread(target=inputs.watch, args=(self,), name="inputs", daemon=True).start()
        threading.Thread(target=siri.watch, args=(self,), name="siri", daemon=True).start()
        if self.settings.get("steam_autostart"):
            _bg(self._steam_autostart)

    def _demo_ticker(self):
        while True:                              # demo has no _tick: refresh an open switcher (a demo quit, a stuck timer)
            time.sleep(0.5)
            self._apps_tick()

    def _housekeeping(self):
        while True:
            time.sleep(60)
            try:
                self._refresh()
            except Exception:
                log.exception("tile refresh failed")

    # ---- tiles

    def _refresh(self):
        """Rebuild the visible rows: Steam library scan, PATH/flatpak checks, local service ports."""
        games = self._scan_games()
        installed = {g["appid"] for g in games}
        ports = {local_port(t["url"]) for t in self.tile_defs.values() if t["type"] == "web"} - {None}
        closed = set() if self.demo else {p for p in ports if not port_open(p)}
        defs = {**self.tile_defs, **{g["id"]: g for g in games}}
        rows, visible = [], {}
        for r in self.config["rows"]:
            ids = ([g["id"] for g in games] if r.get("auto") == "steam" else []) + list(r.get("tiles", []))
            ts = [defs[i] for i in dict.fromkeys(ids) if i in defs and self._visible(defs[i], closed, installed)]
            visible.update((t["id"], t) for t in ts)
            if ts:
                rows.append({"id": r["id"], "title": r.get("title"), "style": r.get("style", "icons"), "ids": [t["id"] for t in ts]})
        with self.lock:
            changed = (rows, visible) != (self._rows, self._tiles)
            self._rows, self._tiles, self.games = rows, visible, {g["appid"]: g for g in games}
        if changed and self._started:
            self.emit("tiles", {})

    def _visible(self, t, closed, installed):
        if self.demo:
            return True                          # ponytail: demo shows every tile; the hiding rules need the real box
        kind = t["type"]
        if kind == "web":
            return local_port(t["url"]) not in closed
        if kind == "exec":
            a = t["argv"]
            if a[:2] == ["flatpak", "run"]:
                app = next((x for x in a[2:] if not x.startswith("-")), "")
                return bool(app) and any(os.path.isdir(os.path.join(d, app)) for d in
                                         (os.path.expanduser("~/.local/share/flatpak/app"), "/var/lib/flatpak/app"))
            return shutil.which(a[0]) is not None
        if kind == "steam":
            return str(t["appid"]) in installed if "appid" in t else os.path.isdir(self.settings.get("steam_root", ""))
        return True

    def _scan_games(self):
        if self.demo:
            return [{"id": f"steam:{a}", "type": "steam", "appid": a, "title": n,
                     "art": {k: f"/art/{'steam' if self.art_file(a, k) else 'demo'}/{a}/{k}" for k in ART},
                     **({"profile": DEMO_PROFILES[a]} if a in DEMO_PROFILES else {})} for a, n in DEMO_GAMES.items()]
        root = self.settings.get("steam_root", "")
        libs = [root]
        try:
            lf = vdf(Path(root, "steamapps", "libraryfolders.vdf").read_text(errors="replace"))
            libs += [v["path"] for v in next(iter(lf.values()), {}).values() if isinstance(v, dict)
                     and isinstance(v.get("path"), str)]
        except (OSError, AttributeError):
            pass
        found = {}
        for lib in dict.fromkeys(os.path.realpath(p) for p in libs if p):
            for f in glob.glob(os.path.join(glob.escape(lib), "steamapps", "appmanifest_*.acf")):
                try:                             # one damaged manifest must not keep the launcher from starting
                    st = acf(f)
                    appid, name, last = str(st.get("appid", "")), st.get("name", ""), str(st.get("lastplayed", ""))
                    if appid.isdigit() and name and not name.startswith(TOOL_PREFIXES):
                        found[appid] = {"id": f"steam:{appid}", "type": "steam", "appid": appid, "title": name,
                                        "last": int(last) if last.isdecimal() else 0}
                except Exception as e:
                    warn_once(f"acf:{f}", "skipping %s: %r", f, e)
        self._art = {}
        games = sorted(found.values(), key=lambda g: (-g["last"], g["title"].lower()))
        for g in games:
            g["art"] = {k: f"/art/steam/{g['appid']}/{k}" for k in ART if self.art_file(g["appid"], k)}
            s = profile_summary(self.settings.get("gamespark", ""), g["appid"])   # GameSpark-optimized: the badge
            if s is not None:
                g["profile"] = s
        return games

    def _profiled(self, appid):
        """appid has a GameSpark launch profile (<settings.gamespark>/profiles/launch/<appid>.env), read now."""
        return profile_summary(self.settings.get("gamespark", ""), appid) is not None

    def art_file(self, appid, kind):
        """Path of Steam's cached art for /art/steam/<appid>/<kind>, or None (cached until the next library scan)."""
        root = self.settings.get("demo_steam_root" if self.demo else "steam_root", "")
        if not root or not str(appid).isdigit() or kind not in ART:
            return None
        key = (str(appid), kind)
        if key not in self._art:
            self._art[key] = find_art(os.path.expanduser(root), appid, kind)
        return self._art[key]

    def _public(self, t):
        out = {k: t[k] for k in ("id", "type", "title", "subtitle", "color", "note", "widget", "art", "game", "shows",
                                 "profile", "voice") if k in t}   # voice: Agent._handoff (a lab app)
        out["running"], out["search"] = t["id"] in self.running, bool(t.get("search"))  # (search: it has a link)
        return out

    def tiles(self):
        with self.lock:
            return {"rows": [{"id": r["id"], "title": r["title"], "style": r["style"],
                              "tiles": [self._public(self._tiles[i]) for i in r["ids"]]} for r in self._rows],
                    "demo": self.demo}

    def tile(self, tile_id):
        with self.lock:
            t = self._tiles.get(tile_id)
            return self._public(t) if t else None

    def find(self, name):
        """Exact id → exact title (case-insensitive) → the only title containing it → difflib best ≥ 0.6. Substring
        before fuzzy: 'witcher' means 'The Witcher 3: Wild Hunt', not the closer-spelled 'Twitch'."""
        q = str(name or "").strip().lower()
        if not q:
            return None
        with self.lock:
            ts = list(self._tiles.values())
            titles = [t["title"].lower() for t in ts]
            hit = next((t for t in ts if t["id"].lower() == q), None) or next((t for t in ts if t["title"].lower() == q), None)
            if not hit:
                m = difflib.get_close_matches(q, titles, n=1, cutoff=0.6)
                subs = [t for t, ti in zip(ts, titles) if len(q) >= 3 and q in ti]
                hit = subs[0] if len(subs) == 1 else ts[titles.index(m[0])] if m else None
            return self._public(hit) if hit else None

    def titles(self):
        with self.lock:
            return [t["title"] for t in self._tiles.values()]

    # ---- state

    def state(self):
        # ponytail: no lock, so status reads (UI, phone, the Guide button's input thread) never queue behind CDP I/O.
        # Every field is replaced whole and list() of the OrderedDict is atomic under the GIL; a read racing _set() may
        # mix old and new fields, the state event _set() sends after it is whole. A free-threaded Python needs the lock.
        ui = self.ui
        return {"surface": self.surface, "active": self.active, "title": self.title, "focused": ui["focused"],
                "row": ui["row"], "tabs": list(self.tabs), "running": sorted(self.running), "voice": self.voice}

    def _set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)
            if kw.get("active"):                 # _tick sets it every 500 ms: the app in front is the most recent
                self._stamp(kw["active"])
            s = self.state()
            if s == self._last_state:
                return
            self._last_state = s
        self.emit("state", s)

    def _stamp(self, key):
        """The app switcher's recency, newest wins. ponytail: past 64 keys it is pruned to the open ones."""
        u = {**self._used, key: time.monotonic()}
        self._used = u if len(u) <= 64 else {k: v for k, v in u.items() if k in self.tabs or k in self.running
                                             or k in self._groups}

    def ui_report(self, focused, row, screen, launch=None):
        with self.lock:                          # (launch: the tile whose splash the page shows: front_key)
            self.ui = {"focused": focused, "row": row, "screen": screen, "launch": launch}

    def stats(self):
        with self._stats_lock:
            t, s = self._stats
            if s is None or time.monotonic() - t >= 1:
                s = self._fake_stats() if self.demo else self._read_stats()
                self._stats = (time.monotonic(), s)
            return s

    def _read_stats(self):
        gpu = {"util": None, "temp": None, "power": None}
        if shutil.which("nvidia-smi"):
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,power.draw",
                                      "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3).stdout
                gpu = dict(zip(("util", "temp", "power"), (num(v) for v in out.splitlines()[0].split(","))))
            except (OSError, subprocess.SubprocessError, IndexError) as e:
                warn_once("nvidia-smi", "nvidia-smi failed: %s", e)
        mem = {"used_gb": None, "total_gb": None}
        try:
            info = dict(re.findall(r"^(\w+):\s+(\d+)", Path("/proc/meminfo").read_text(), re.M))
            total, avail = int(info["MemTotal"]) / 2**20, int(info["MemAvailable"]) / 2**20
            mem = {"used_gb": round(total - avail, 1), "total_gb": round(total, 1)}
        except (OSError, KeyError):
            pass
        models = []
        llm = self.settings.get("llm") or {}
        if llm.get("api", "ollama") == "ollama":
            u = urllib.parse.urlsplit(llm.get("base_url", "http://127.0.0.1:11434"))
            try:
                models = [m.get("name") for m in http_json(u.hostname, u.port or 80, "/api/ps").get("models", [])]
            except (OSError, ValueError, http.client.HTTPException, AttributeError):
                pass
        return {"gpu": gpu, "mem": mem, "load1": round(os.getloadavg()[0], 2), "models": models, "time": now_iso()}

    def _fake_stats(self):
        d = self._demo

        def drift(k, lo, hi, step):
            d[k] = min(hi, max(lo, d[k] + random.uniform(-step, step)))
            return d[k]
        return {"gpu": {"util": round(drift("util", 2, 97, 9)), "temp": round(drift("temp", 38, 79, 1.2)),
                        "power": round(drift("power", 15, 140, 6), 1)},
                "mem": {"used_gb": round(drift("mem", 20, 100, 1.5), 1), "total_gb": 119.7},
                "load1": round(drift("load", 0.2, 6, 0.25), 2),
                "models": ["qwen3:4b-instruct-2507-q4_K_M"] + (["qwen3-vl:8b-instruct"] if d["util"] > 60 else []),
                "time": now_iso()}

    # ---- sleep (README "Sleep"): awake → warning (SLEEP_WARN_S, the picture dimmed) → asleep (signal off, or black)

    @property
    def voice(self):
        """The assistant's phase: "idle", "listening" or "thinking" (server.py mirrors its SSE voice events here)."""
        return self._voice

    @voice.setter
    def voice(self, phase):
        # idle → listening or thinking is input: words wake the screen, and act. Here, as it changes: a typed fast-path
        # command is over in milliseconds, between two of the sleep thread's ticks
        was, self._voice = self._voice, phase
        if was == "idle" and phase != "idle":
            self.wake("voice", press=False)

    def wake(self, src, press=True):
        """Input (src: siri pad key page phone voice agent x settings, or the hold that ended a warning): the idle clock
        restarts, and a warning or a sleeping screen wakes. → True when this call woke it, or when a press comes within
        WAKE_GRACE_S of a wake: a press's caller then drops the press, so the one that wakes the screen does nothing
        else. Words and agents (press=False) ignore it: they wake it and act. Any thread may call it (the CDP reader's,
        siri.py's under the Shell lock): it takes only _sleep_lock, and the awake event goes out before it returns, so
        the page has it before anything this input does."""
        now = self._clock()
        with self._sleep_lock:
            self._input_at, was = now, self._sleep
            if was == "awake":
                return press and now - self._woke_at < WAKE_GRACE_S
            self._sleep, self._woke_at = "awake", now
            self.emit("sleep", {"state": "awake", "by": src})
        log.info("sleep: awake (%s%s)", src, "; the press only woke it" if press else "")
        _bg(self._wake_effects, src)
        return True

    def sleep_now(self, src="person"):
        """The picture off at once (Settings' Sleep Now, "go to sleep"): no warning, and nothing holds it off (a person
        asked)."""
        self._go_asleep(src)
        return {"ok": True, "state": "asleep"}

    def sleep_state(self, full=False):
        """{"state": awake|warning|asleep, "minutes", "game_minutes", "signal", "idle_s", "in_s", "held"}: in_s counts
        to the warning while awake and to sleep during it (None when asleep, or when sleep is off for what is in front);
        held (only when full: it may read the page, 0.5 s at most) is what holds sleep off now. Lock-free, like state()."""
        minutes, game, signal = self._sleep_settings()
        now, state = self._clock(), self._sleep
        limit, idle = (game if self.surface == "app" else minutes) * 60, now - self._input_at
        left = None if state == "asleep" or not limit else \
            limit - SLEEP_WARN_S - idle if state == "awake" else SLEEP_WARN_S - (now - self._warn_at)
        return {"state": state, "minutes": minutes, "game_minutes": game, "signal": signal, "idle_s": int(idle),
                "in_s": None if left is None else max(0, round(left)), "held": self._sleep_hold() if full else None}

    def _sleep_settings(self):
        """(minutes, game_minutes, signal), live from settings.sleep: 0 minutes is never; signal "off" turns the HDMI
        signal off, "black" keeps it and shows black. A bad value is logged (once per change, not per tick) and its
        default used."""
        s = self.settings.get("sleep")
        s = s if isinstance(s, dict) else {}
        raw = repr([s.get(k) for k in ("minutes", "game_minutes", "signal")])
        if raw != self._sleep_cfg[0]:            # (replaced whole: sleep_state's readers race the tick harmlessly)
            signal = s.get("signal", "off")
            if signal not in ("off", "black"):
                log.warning('setting signal = %r ignored: expected "off" or "black"; using "off"', signal)
                signal = "off"
            self._sleep_cfg = (raw, (inputs.number(s, "minutes", 0), inputs.number(s, "game_minutes", 0), signal))
        return self._sleep_cfg[1]

    def _sleep_hold(self):
        """What holds sleep off now, or None: the assistant listening or thinking, a game's launch holding (its loading
        screen), the web app in front playing (or unknown), a game or app in front while "during games" is never."""
        if self.voice in ("listening", "thinking"):
            return "voice"
        if any(self._holding(k, p) for k, p in list(self.pending.items())):
            return "launch"
        if self.surface == "web" and self._front_playing() is not False:
            return "playing"
        if self.surface == "app" and not self._sleep_settings()[1]:
            return "game"
        return None

    def _front_playing(self):
        """The web app in front plays video or music (PLAYING_JS: get_screen's own test), or None when that can't be
        read (a big cross-origin player frame, a page busy past 0.5 s, no browser): unknown holds. Elsewhere False."""
        if self.demo or self.surface != "web":
            return False
        try:
            return self._eval(self._session(), PLAYING_JS, timeout=0.5, gesture=False)
        except (OSError, RuntimeError):
            return None

    def _cmd(self, *argv, timeout=3):
        """A sleep command's stdout (xset, busctl), None when it fails (logged once); the selftest's runner fakes it."""
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            warn_once(f"sleep:{argv[0]}", "sleep: %s: %s", argv[0], e)
            return None
        if r.returncode:
            warn_once(f"sleep:{argv[0]}:{r.returncode}", "sleep: %s failed: %s", " ".join(argv), r.stderr.strip()[-200:])
            return None
        return r.stdout

    def _x_idle(self):
        """Seconds since the last X input (a key or the mouse in a page or game, the trackpad's XTest), from mutter's idle
        monitor ("t 1195892": ms [M]); None in the demo or when it can't be read."""
        out = None if self.demo else self._cmd("busctl", "--user", "call", "org.gnome.Mutter.IdleMonitor",
                                              "/org/gnome/Mutter/IdleMonitor/Core", "org.gnome.Mutter.IdleMonitor",
                                              "GetIdletime", timeout=2)
        m = re.fullmatch(r"t (\d+)\s*", out or "")
        return int(m[1]) / 1000 if m else None

    def _screen(self, on):
        """The TV's HDMI signal on or off (X DPMS; X input turns it back on by itself). Not in the demo."""
        if not self.demo:
            self._cmd("xset", "dpms", "force", "on" if on else "off")

    def _tv(self, on):
        """The TV itself, after the signal (README "Sleep" › The TV itself), over Roku's ECP: on, out of standby (a
        wake-on-LAN first when it's off the network) with the Spark's input (settings.tv.input, "hdmi3") in front; off,
        to standby, only while it shows that input (never the Chromecast someone is watching). Under _tv_lock, against
        the sleep state then, so a wake and a nap that raced end as the state says. No settings.tv, or the demo:
        nothing."""
        tv = self.settings.get("tv")
        want = tv.get("input") if isinstance(tv, dict) else None
        if self.demo or not (isinstance(want, str) and re.fullmatch(r"[a-z]+\d*", want)):
            return
        front = lambda: re.search(r'<app id="([^"]+)"', self._ecp("query/active-app") or "")   # noqa: E731
        with self._tv_lock:
            if (self._sleep == "awake") != on:
                return
            if not on:
                app = front()
                if app and app[1] == f"tvinput.{want}":
                    if self._ecp("keypress/PowerOff", post=True) is not None:
                        log.info("tv: off")
                elif app:
                    log.info("tv: left on (%s in front)", app[1])
                return
            # power-mode first: in standby a Roku TV answers device-info alone [I], so active-app can't say it's dark
            done, info = [], self._ecp("query/device-info")
            if info is None and self._tv_mac and self._wol(self._tv_mac):
                done.append("wake-on-LAN")
                info = self._tv_until(lambda: self._ecp("query/device-info"))
            if info is None:
                warn_once("tv:away", 'tv: no answer (off the network? README "Sleep" › The TV itself)')
                return
            if "<power-mode>PowerOn<" not in info and self._ecp("keypress/PowerOn", post=True) is not None:
                done.append("power on")
                self._tv_until(lambda: "<power-mode>PowerOn<" in (self._ecp("query/device-info") or ""))
            app = front()                        # (whether a TV just on can miss the launch is [?])
            if not (app and app[1] == f"tvinput.{want}") and self._ecp(f"launch/tvinput.{want}", post=True) is not None:
                done.append(want)
            if done:
                log.info("tv: on (%s)", ", ".join(done))

    @staticmethod
    def _tv_until(read):
        """read() every 0.5 s until it's truthy, for TV_UP_S at most: its last value."""
        end = time.monotonic() + TV_UP_S
        while not (v := read()) and time.monotonic() < end:
            time.sleep(0.5)
        return v

    def _ecp(self, path, post=False):
        """A Roku ECP call's reply, or None: the TV away (its address is found again next time: a new lease), or a
        refusal (logged once). The TV is settings.tv.ecp ("192.168.1.30", host:port), else the first Roku to answer SSDP;
        its device-info tells its MAC, for the wake-on-LAN."""
        at = self._tv_at or self._tv_find()
        if not at:
            return None
        host, _, port = at.partition(":")
        c = http.client.HTTPConnection(host, int(port or 8060), timeout=2)
        try:                                     # (by address: ECP refuses a Host header that names it [I])
            c.request("POST" if post else "GET", "/" + path, body=b"" if post else None)
            r = c.getresponse()
            body = r.read().decode(errors="replace")
        except (OSError, http.client.HTTPException) as e:
            self._tv_at = None
            log.debug("tv: %s: %s", path, e)
            return None
        finally:
            c.close()
        self._tv_at = at
        if r.status != 200:                      # 403: keys while Control by mobile apps is Limited [M]; most reads in
            if post:                             # standby [I], which are no news
                warn_once(f"tv:{path}:{r.status}", "tv: %s refused (HTTP %s)%s", path, r.status, "; on the TV, "
                          "Settings › System › Advanced system settings › Control by mobile apps › Network access: "
                          "Enabled" if path.startswith("keypress/") and r.status == 403 else "")
            return None
        net = re.search(r"<network-type>(\w+)</", body)
        mac = net and re.search(rf"<{net[1]}-mac>([0-9a-f:]{{17}})</", body)
        self._tv_mac = mac[1] if mac else self._tv_mac
        return body

    def _tv_find(self):
        """settings.tv.ecp (host or host:port), else the address in the first SSDP answer to roku:ecp (1.5 s at most),
        else None."""
        ecp = self.settings["tv"].get("ecp")
        if ecp:
            if re.fullmatch(r"[\w.-]+(:\d+)?", str(ecp)):
                return str(ecp)
            warn_once("tv:ecp", 'setting tv.ecp = %r ignored: expected "host" or "host:port"; finding the TV', ecp)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(1.5)
                s.sendto(SSDP_ECP, ("239.255.255.250", 1900))
                m = re.search(rb"(?im)^location:\s*http://([\d.]+:\d+)", s.recv(2048))
        except OSError:
            return None
        return m and m[1].decode()

    def _wol(self, mac):
        """A wake-on-LAN magic packet to mac, broadcast (the TV advertises it: SSDP's WAKEUP: MAC=…): True once sent."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16, ("255.255.255.255", 9))
            return True
        except OSError as e:
            warn_once("tv:wol", "tv: wake-on-LAN: %s", e)
            return False

    def _dim(self, on):
        """The warning's dim (half the picture through the gamma ramp, so over any app: on_dim) and its words over a
        web app in front (_tab_warning) on, or the owner's brightness back and the words gone, if the state still asks
        for it on its turn: a dim and an undim asked together land in the state's order, never the dim last on an awake
        screen."""
        with self._screen_lock:
            if (self._sleep != "awake") == on:
                if self.on_dim:
                    self.on_dim(on)
                self._tab_warning(on)

    def _tab_warning(self, on):
        """WARN_JS on the web app in front, or off again on the page it went on, wherever that is by then (Show black
        put the launcher in front). None over a game (another X window: the dim alone) or the launcher (its own page
        says it), nor in the demo (no tabs). A page busy past 2 s goes without."""
        s = None if self.demo else self._front().get("session") if on and self.surface == "web" else self._warn_tab
        self._warn_tab = s if on else None
        if s:
            try:
                self._cdp_ok()
                self._eval(s, WARN_JS.replace("__N__", str(SLEEP_WARN_S if on else 0)), timeout=2, gesture=False)
            except (OSError, RuntimeError) as e:
                log.debug("sleep: the warning on the page: %s", e)

    def _wake_effects(self, src=None):
        """After a wake (_bg): the signal back on if this nap turned it off (and the TV, for a person's input), then the
        owner's brightness."""
        with self._screen_lock:
            if self._off and self._sleep == "awake":
                self._off = False
                self._screen(True)
                if src in TV_WAKERS:
                    _bg(self._tv, True)
        self._dim(False)

    def _go_asleep(self, src, warned=False):
        """→ asleep, from the warning (warned: only if still in it) or at once (sleep_now). Its event goes first; the
        signal goes off SLEEP_OFF_DELAY_S later (the tick, once the launcher's fade has shown), or, to show black, the
        launcher (its page black) comes in front. X input counts after WAKE_GRACE_S: the release of the key that chose
        Sleep Now must not wake it."""
        signal = self._sleep_settings()[2]
        with self._sleep_lock:
            if self._sleep == "asleep" or warned and self._sleep != "warning":
                return
            self._sleep, self._slept_at = "asleep", self._clock()
            self._x_from = self._slept_at + WAKE_GRACE_S
            self.emit("sleep", {"state": "asleep"})
        log.info("sleep: asleep (%s, signal %s)", src, signal)
        if signal == "black" and self.surface != "launcher":
            _bg(self._black)

    def _black(self):
        """Show black (sleep.signal "black"): the launcher in front, as Home brings it (a web video pauses: this mode's
        cost)."""
        with self.lock:
            if self._sleep == "asleep" and self.surface != "launcher":
                self._home()

    def _signal_off(self):
        with self._screen_lock:                  # (a wake meanwhile turns it on after this, or finds nothing to do)
            if self._sleep != "asleep":
                return
            self._off = True
            self._screen(False)
            _bg(self._tv, False)
        # X input counts from WAKE_GRACE_S after the off: whether the off itself resets mutter's idle time, even a moment
        # later, is [?] (the next read would then wake it at once, every nap). Real input meanwhile turns the signal on
        # by itself (X's DPMS), and the next one wakes it
        self._x_from = max(self._x_from, self._clock() + WAKE_GRACE_S)

    def _sleeper(self):
        """Thread "sleep": its own, so mutter's idle time (busctl) and the page read never hold up the window watch's
        Home and push-to-talk keys."""
        while True:
            time.sleep(0.5)
            try:
                self._sleep_tick()
            except Exception as e:
                warn_once(f"sleep:{type(e).__name__}:{e}", "sleep: %s", e)

    def _sleep_tick(self):
        """One step. Awake → warning once idle for the limit less SLEEP_WARN_S, unless X input came since or something
        holds sleep off (each read only then; either restarts the idle clock). Warning → asleep SLEEP_WARN_S later,
        unless X input, a hold, or a limit now 0 (never) or longer cancels it. Asleep: the signal goes off once due, and
        X input (read every X_IDLE_ASLEEP_S) wakes it. Calls no action: those count as input."""
        minutes, game, signal = self._sleep_settings()
        now, state = self._clock(), self._sleep
        limit = (game if self.surface == "app" else minutes) * 60
        if state == "awake":
            if not limit or now - self._input_at < limit - SLEEP_WARN_S:
                return
            x = self._x_idle()
            if x is not None and now - x > self._input_at + 1:   # X input since (+1 s: the read's own latency)
                with self._sleep_lock:
                    self._input_at = max(self._input_at, now - x)
                return
            held = self._sleep_hold()
            if held and held != self._held:
                log.info("sleep: held (%s): idle clock restarted", held)
            self._held = held
            if held:
                with self._sleep_lock:
                    self._input_at = max(self._input_at, now)
                return
            with self._sleep_lock:               # (an input meanwhile: those reads take a while)
                t = self._clock()
                if self._sleep != "awake" or t - self._input_at < limit - SLEEP_WARN_S:
                    return
                self._sleep, self._warn_at, idle = "warning", t, t - self._input_at
                self.emit("sleep", {"state": "warning", "in": SLEEP_WARN_S})
            log.info("sleep: warning (idle %d:%02d, %s)", *divmod(int(idle), 60), self.surface)
            _bg(self._dim, True)
        elif state == "warning":
            if not limit or now - self._input_at < limit - SLEEP_WARN_S:
                why = "settings"                 # never now, or a longer limit (a setting, a game in front)
            else:
                x = self._x_idle()
                why = "x" if x is not None and x < now - self._warn_at else self._sleep_hold()
            if why:
                self.wake(why, press=False)      # cancelled: awake, the dim lifted
            elif now - self._warn_at >= SLEEP_WARN_S:
                self._go_asleep("idle", warned=True)
        else:
            due = signal == "off" and not self._off and now - self._slept_at >= SLEEP_OFF_DELAY_S
            if not due and now - self._x_polled < X_IDLE_ASLEEP_S:
                return
            self._x_polled, x = now, self._x_idle()
            if x is not None and x < now - self._x_from:
                self.wake("x", press=False)      # X input since (with the signal off, X turned it back on itself)
            elif due:
                self._signal_off()

    # ---- actions (agents, phone, UI)

    @_action
    def open(self, tile_id):
        try:
            return self._open(tile_id)
        finally:                                 # at its end: a _tick whose X reads are older doesn't undo it (_tick)
            self._acted_at = time.monotonic()

    def _open(self, tile_id):
        t = self._tiles.get(tile_id)
        if not t:                                # an untiled card: a window, or a Steam game no tile lists
            return self._open_window(tile_id) if str(tile_id).startswith("x:") or tile_id in self._groups or \
                tile_id in self.running else {"ok": False, "error": f"no tile {tile_id!r}"}
        if t["type"] == "web":
            return self._open_web(t)
        if t["type"] == "widget":                # the launcher's own panels (Settings): focus + select it there
            if self.surface != "launcher":
                self._home()
            if t.get("widget") == "settings":    # (here, not by the page's report 100 ms later: an agent's navigate
                self.ui = {**self.ui, "screen": "settings"}   # or select right after its own open is refused, as in
            self.emit("focus", {"id": tile_id})                # the switcher)
            self.emit("select", {})
            return {"ok": True, "surface": "launcher"}
        if self.demo:                            # a simulated launch: the same toast + state the real one gives
            if tile_id not in self.running:
                if tile_id[:6] == "steam:":      # (a game's splash stages: it says Starting…, then Loading…)
                    self.emit("launch", {"id": tile_id, "stage": "starting"})
                    self._demo_runs[tile_id] = run = object()
                    _bg(self._demo_launch, tile_id, run)
                else:
                    self.emit("toast", {"text": f"Starting {t['title']}…", "kind": "info"})
            self._set(surface="app", active=tile_id, title=t["title"], running=self.running | {tile_id})
            return {"ok": True, "surface": "app"}
        self._pause_front()                      # the game or app takes the screen: don't leave a video playing
        proc, p, now = (self.apps.get(tile_id) or {}).get("proc"), self.pending.get(tile_id), time.monotonic()
        if self._holding(tile_id, p) and "at" in p:   # its launch holds: its splash again, over the launcher as a new
            if self.surface != "launcher":       # launch's (the page grows it on 'starting', then takes the stage), and
                self._home()                     # a Steam prompt the launcher covered comes up again; never a second
            p.pop("prompt", None)                # launch, nor its window before it drew. It is the launch handed over
            self._splash = (tile_id, p)          # (_hold), another one loading stays behind
            for stage in ("starting", "loading") if "added" in p else ("starting",):
                self.emit("launch", {"id": tile_id, "stage": stage})
            return {"ok": True, "surface": "app"}
        # No relaunch: bring its window back. A live exec child counts before the 500 ms tick lists it; a Steam game
        # counts once its window is known, so a ghost left by a missed 'Game process removed' can't block it forever.
        if (tile_id in self.running and (t["type"] != "steam" or tile_id in self._tile_wins)
                or proc and proc.poll() is None):
            w = self._tile_wins.get(tile_id)
            if w and self.x_ok:
                with X11() as x:
                    x.activate(w)
                    if self._kwin:
                        x.iconify(self._kwin)
            else:
                self.pending[tile_id] = {"until": now + 30, "tries": 0}
            self._home_guard = self._steam_guard = 0.0   # what the user chose stays up (Big Picture within 3 s of Home)
            self._set(surface="app", active=tile_id, title=t["title"])
            return {"ok": True, "surface": "app"}
        if p and now < p.get("at", 0) + (30 if t["type"] == "steam" else 5):
            return {"ok": True, "surface": "app"}  # ponytail: a re-press while it starts; Steam can take >20 s to log it
        if t.get("appid") and tile_id in self.running and reapers(self._procs(), t["appid"]):
            return {"ok": False, "error": f"{t['title']} has no window to show yet"}   # running (its reaper lives): no
            # second launch. A ghost (no reaper: a missed 'removed') still launches
        game = tile_id[:6] == "steam:"
        if game and self.surface != "launcher":  # a game's loading screen is the launcher's: started over a game, an app
            self._home()                         # or a tab (voice, the phone, an agent), the launcher comes first
        self.pending[tile_id] = entry = {"until": now + (300 if t["type"] == "steam" else 120), "tries": 0, "at": now,
                                         "t0": now, "old": set(self._wins)}   # (the windows before it: _hold)
        self._stamp(tile_id)                     # the switcher's newest card (Starting…), as the demo's launch is
        if game:                                 # the splash: grown here for a launch the page didn't start. It says
            self._splash = (tile_id, entry)      # Starting…, then Loading…: no toast over it
            self.emit("launch", {"id": tile_id, "stage": "starting"})
        else:
            self.emit("toast", {"text": f"Starting {t['title']}…", "kind": "info"})
        _bg(self._launch, t, entry)
        return {"ok": True, "surface": "app"}

    def _demo_launch(self, tid, run, stages=(("loading", 1.0), ("up", 2.0))):
        """Demo: a game's launch stages as the Spark sends them (Steam starts its process at 1 s, it draws 2 s later).
        Quit meanwhile, it fails at its next stage, as a quit while a launch holds does there (ponytail: at once there,
        up to 2 s late here; a pending entry for it would give close_app's at once). Opened again since (run is no
        longer its latest), it stops: that launch's own stages follow."""
        for stage, after in stages:
            time.sleep(after)
            if self._demo_runs.get(tid) is not run:
                return
            if tid not in self.running or tid in self._closing:
                return self.emit("launch", {"id": tid, "stage": "failed"})
            self.emit("launch", {"id": tid, "stage": stage})

    @staticmethod
    def _holding(k, p):
        """Launch entry p is a Steam game's launch that holds (the kiosk pinned over it, _tick): not handed over, and
        its process not ended (or only just: EXIT_S for the next, after Proton's install-script evaluator)."""
        return k[:6] == "steam:" and bool(p) and not p.get("go") and ("at" in p or "gone" in p)

    def pointer(self, dx=0, dy=0, click=None, scroll_x=0, scroll_y=0):
        """The phone trackpad. XTest moves the TV's real pointer, so it works over the kiosk, Big Picture and games
        alike. Not an @_action: an action that is slow (a page still loading) must not stall the pointer."""
        if self.demo:                            # no X server: the launcher draws the pointer (app.js demoPointer)
            self.emit("pointer", {"dx": dx, "dy": dy, "click": click, "scroll_x": scroll_x, "scroll_y": scroll_y})
            return {"ok": True}
        if not self.x_ok:
            return {"ok": False, "error": "no X display here: the trackpad drives the Spark's TV"}
        try:
            with self._ptr_lock, X11() as x:
                x.pointer(dx, dy, pointer_buttons(click, scroll_x, scroll_y))
        except OSError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    @_action
    def home(self):
        # ponytail: siri.py's TV button is the one caller of home() on its "siri-actions" thread (Menu's Back and every
        # internal Home call _home()), so there home() is a physical TV press; the selftest pins both facts in siri.py.
        # Upgrade: siri.py calls press("tv", t) with the reader's own time (offered by its owner); this branch then goes.
        if threading.current_thread().name == "siri-actions":
            return self.press("tv")
        return self._home()

    @_action
    def press(self, src, t=None):
        """One physical Home button: "tv" (siri.py through the home() shim: t None, timed here), "guide" (evdev read time),
        "key" (X server time of a hotkey grab). Home at once; the same button again within double_s opens the switcher
        instead, and the press after that is a first press. abs(): two presses that _bg threads ran swapped still pair, and
        an X time that wrapped (49.7 days) reads as far apart. A press is timed from when it asked for the lock, not from
        its turn: the lock hands over in order, so an agent's call that queued meanwhile runs first. A Siri press that
        waited behind the previous press's Home pairs too: siri.py runs its actions on one thread, so press 2 asks at
        that Home's end (SIRI-INTERFACE.md). Within 0.5 s of a double the button is swallowed: the page drops that Home
        (HOME_GRACE), so it must not pair with the next press either (TV pressed fast and often would end on a reopened
        switcher, never Home). Each press also brings the TV to the Spark (_tv: on, its input in front)."""
        _bg(self._tv, True)
        now = self.lock.asked
        t0, end = self._press.pop(src, (None, 0.0))
        if t0 is None and now - end < 0.5:       # (None, the switcher's time): still in the page's grace
            self._press[src] = (None, end)
            return {"ok": True, "surface": self.surface}
        if t0 is not None and (abs((now if t is None else t) - t0) <= self.double_s or t is None and now - end < 0.02):
            r = self._switcher()
            self._press[src] = (None, time.monotonic())
            return r
        r = self._home()
        self._press[src] = (now if t is None else t, time.monotonic())
        return r

    @_action
    def switcher(self):
        return self._switcher()

    def _switcher(self):
        """The app switcher over the launcher: Home first if something else is in front, else keep Big Picture back
        (Steam hears a Guide press 2 too). The cards ride the event; `from` is the app a Home left under 2 s ago (TV TV,
        a Guide hold, voice from a game). ui.screen is set here, not by the page's report 100 ms later, so a swipe right
        after opening isn't lost."""
        if self.surface != "launcher":
            self._home()
        else:
            self._home_guard = time.monotonic() + 3
            self._kiosk_front()
        key, at = self._left
        apps = self._apps_sent = self.running_apps()
        self.ui = {**self.ui, "screen": "switcher"}
        self.emit("switcher", {"open": True, "apps": apps, "from": key if time.monotonic() - at < 2 else None})
        return {"ok": True, "surface": self.surface, "open": [a["title"] for a in apps]}

    def swipe(self, direction, speed):
        """siri.py, once per touch at its lift, under the lock (so not an action). A fast flick up while the launcher is
        in front goes to the page: one close of the focused switcher card per touch (the page acts only while it's open).
        A touch that wakes the screen does nothing else."""
        if self.wake("siri"):
            return
        if direction == "up" and speed >= self.swipe_speed and self.surface == "launcher":
            self.emit("swipe", {"dir": "up", "speed": speed})

    def _front_key(self):
        return self.active_tab if self.surface == "web" else self.active or self._active_key

    def front_key(self):
        """The card in front ("quit it"): a tab, tile or untiled window's x: key, which state() has no field for; on the
        launcher, the game whose loading screen it shows (the kiosk is pinned over that game's window), else None. The
        page says which it shows (ui launch): it alone takes one down on a Back the daemon never hears (the IR remote's
        Escape, the pad's B) or on coming back into view. Lock-free like state()."""
        if self.surface != "launcher":
            return self._front_key()
        s, se = self._splash or (None, {})
        return s if self.ui.get("launch") == s and self.pending.get(s) is se and self._holding(s, se) else None

    def _home(self):
        # No debounce: a Guide press arriving twice (page bridge + evdev) is harmless (the second call finds the
        # launcher in front already), while a debounce silently dropped real Home presses from agents and the phone.
        if self.surface != "launcher":          # the app we leave: the switcher focuses it and Menu goes back to it
            self._left = (self._front_key(), time.monotonic())
            if self.demo and self.active in self.running:   # (a real snapshot is _tick's, after Home)
                self._snap_put(self.active, png(*SNAP, hashlib.md5(self.active.encode()).digest()[:3]))
        if self.launcher["target"] and not self.demo and self.cdp and self.cdp.connected.is_set():
            try:                                 # launcher first: a page too busy to pause can't keep Home away
                self._show(self.launcher)
            except (OSError, RuntimeError) as e:
                log.warning("home: %s", e)
        self._pause_front()
        self._home_guard = time.monotonic() + 3  # Steam hears the same Guide press and raises Big Picture: see _tick
        self._kiosk_front()
        self.emit("home", {})
        self._set(surface="launcher", active=None, active_tab=None, title=self._page_title(self.launcher) or "SparkHome")
        self._acted_at = time.monotonic()        # at its end, not its start: a _tick that waited for the lock reads X
        return {"ok": True, "surface": "launcher"}  # too soon after it to see it (mutter activates asynchronously)

    @_action
    def back(self):
        s = self.surface
        if s == "launcher":
            self.emit("back", {})
        elif s == "web":
            t = self._tiles.get(self.active_tab) or {}
            if self.demo:
                return self._home()
            tab = self.tabs.get(self.active_tab) or {}
            pop = tab.get("popup")
            sess = None if pop else self._session()   # (a popup still being set up has no session yet)
            if pop:                              # Back leaves a popup for its tile: untracked now, so the next action
                self._close(pop["target"])       # can't hit its dead session (a second quick Back: the tile's)
                if tab.get("popup") is pop:
                    del tab["popup"]
                self._show(tab)
            elif t.get("back") == "escape":
                self._key(sess, "escape")
            else:                                # history: step back, or leave the tile from its first page
                h = self.cdp.call("Page.getNavigationHistory", session=sess)
                i, entries = h["currentIndex"], h["entries"]
                if i < 1 or entries[i - 1]["url"] in ("about:blank", ""):
                    return self._home()
                self.cdp.call("Page.navigateToHistoryEntry", session=sess, entryId=entries[i - 1]["id"])
        elif not self.demo:
            self._xkeys(["Escape"])
        return {"ok": True, "surface": self.surface}

    @_action
    def select(self):
        if self.surface == "launcher":
            self.emit("select", {})
        elif self.demo:
            pass
        elif self.surface == "web":
            self._key(self._session(), "enter")
        else:
            self._xkeys(["Return"])
        return {"ok": True, "surface": self.surface}

    @_action
    def navigate(self, direction, count=1):
        if direction not in DIRS:
            raise ValueError("direction must be up, down, left or right")
        if isinstance(count, bool) or not isinstance(count, (int, str)) or not str(count).isdigit() or not 1 <= int(count) <= 20:
            raise ValueError("count must be 1-20")
        count = int(count)
        if self.surface == "launcher":           # touch: a Siri step (the switcher ignores them up: a close is a swipe)
            touch = threading.current_thread().name == "siri-actions"
            self.emit("nav", {"dir": direction, "count": count, **({"touch": True} if touch else {})})
        elif self.demo:
            pass
        elif self.surface == "web":
            sess, end = self._session(), None
            for i in range(count):
                end = self._key(sess, direction, end=end)
                if i < count - 1:
                    time.sleep(0.08)             # let TV UIs animate between steps
        else:
            self._xkeys([X_KEYS[direction]] * count)
        return {"ok": True, "surface": self.surface}

    @_action
    def search(self, service, query):
        hit = self.find(service)
        t = self._tiles.get(hit["id"]) if hit else None
        if not t:
            return {"ok": False, "error": f"no service called {service!r}"}
        if not t.get("search"):
            return {"ok": False, "error": f"{t['title']} has no search link; open it and use type_text"}
        q = str(query or "").strip()
        if not q:
            raise ValueError("empty search")
        return self._open_web(t, t["search"].replace("{q}", urllib.parse.quote(q, safe="")))

    @_action
    def media(self, action):
        if action not in MEDIA:
            raise ValueError("action must be one of " + ", ".join(MEDIA))
        if self.surface == "launcher" or self.demo:
            return {"ok": True}
        if self.surface == "app":
            self._xkeys([X_KEYS[action]])
            return {"ok": True}
        sess = self._session()
        if action in ("next", "previous"):
            u = urllib.parse.urlsplit(self._page_url())
            yt_desktop = (u.hostname or "").endswith("youtube.com") and not u.path.startswith("/tv")
            self._key(sess, ("N" if action == "next" else "P") if yt_desktop else action, modifiers=8 if yt_desktop else 0)
            return {"ok": True}
        r = self._act(sess, MEDIA_JS % json.dumps("pause" if action == "stop" else action), press=False)
        return {"ok": True, "media": r} if r else {"ok": False, "error": "no video or audio on this page"}

    @_action
    def volume(self, change):
        c = str(change).strip().lower()
        if c in ("up", "down"):
            args = ["set-volume", "-l", "1.0", SINK, "5%+" if c == "up" else "5%-"]
        elif c in ("mute", "unmute"):
            args = ["set-mute", SINK, "1" if c == "mute" else "0"]
        elif c.isdigit() and int(c) <= 100:
            args = ["set-volume", SINK, f"{int(c) / 100:.2f}"]
        else:
            raise ValueError("change must be up, down, mute, unmute or 0-100")
        if self.demo:                            # mute sticks over Vol±, as on wpctl: the phone's Mute label trusts it
            d, v = self._demo, self._demo["vol"]
            d["vol"] = v = min(100, v + 5) if c == "up" else max(0, v - 5) if c == "down" else int(c) if c.isdigit() else v
            d["muted"] = c == "mute" if c in ("mute", "unmute") else d.get("muted", False)
            r = {"ok": True, "volume": v, "muted": d["muted"]}
        else:
            if not shutil.which("wpctl"):
                raise RuntimeError("wpctl (PipeWire) not found")
            subprocess.run(["wpctl", *args], check=True, capture_output=True, timeout=3)
            out = subprocess.run(["wpctl", "get-volume", SINK], capture_output=True, text=True, timeout=3).stdout
            m = re.search(r"([\d.]+)", out)                 # "Volume: 0.88 [MUTED]"
            r = {"ok": True, "volume": round(float(m.group(1)) * 100) if m else None, "muted": "MUTED" in out}
        vals = {"sound.volume": r["volume"], "sound.muted": r["muted"]}   # an open Settings' Sound pane follows the
        self.emit("settings", {"values": {k: v for k, v in vals.items() if v is not None}})   # remote's Vol± and voice
        return r

    @_action
    def type_text(self, text):
        if not isinstance(text, str) or not text or len(text) > 2000:
            raise ValueError("text must be 1-2000 characters")
        if self.surface == "launcher":
            return {"ok": False, "error": "nothing to type into"}
        if self.demo:
            return {"ok": True, "typed": len(text)}
        if self.surface == "web":                # (a busy page answers late, as for a key press: sent, not failed)
            try:
                self.cdp.call("Input.insertText", session=self._session(), text=text, timeout=2)
            except TimeoutError:
                return {"ok": True, "typed": SENT}
            return {"ok": True, "typed": len(text)}
        with X11() as x:
            n = x.type_text(text)
        return {"ok": True, "typed": n, "skipped": len(text) - n}

    @_action
    def read_screen(self):
        if self.surface == "launcher":
            f = self.ui["focused"]
            ft = self._tiles.get(f) or {}
            return {"surface": "launcher",
                    "focused": {"id": f, "title": ft.get("title"), "row": self.ui["row"]} if f else None,
                    "rows": [{"id": r["id"], "title": r["title"],
                              "tiles": [{"id": i, "title": self._tiles[i]["title"], "type": self._tiles[i]["type"],
                                         "running": i in self.running} for i in r["ids"]]} for r in self._rows]}
        if self.surface == "web":
            t = self._tiles.get(self.active_tab) or {}
            if self.demo:
                return {"surface": "web", "tile": self.active_tab, "title": t.get("title"), "url": t.get("url"),
                        "text": f"(demo) {t.get('title')} would be on screen here.", "elements": []}
            return {"surface": "web", "tile": self.active_tab, **(self._eval(self._session(), READ_JS, timeout=2) or {})}
        return {"surface": "app", "title": self.title, **({"tile": self.active} if self.active else {})}

    @_action
    def click(self, target):
        if self.surface == "launcher":
            hit = self.find(str(target))
            return self.open(hit["id"]) if hit else {"ok": False, "error": f"no tile matching {target!r}"}
        if self.surface == "app":
            return {"ok": False, "error": "can't click inside a game or app; use navigate/select"}
        if self.demo:
            return {"ok": True, "clicked": str(target)}
        t = target if isinstance(target, int) and not isinstance(target, bool) else \
            int(target) if str(target).strip().isdigit() else str(target).strip()
        if t == "":
            raise ValueError("empty click target")
        sess = self._session()
        label = self._act(sess, CLICK_JS % json.dumps(t))
        return {"ok": True, "clicked": label} if label is not None else \
            {"ok": False, "error": f"nothing clickable matching {target!r} (see read_screen elements)"}

    @_action
    def list_titles(self):
        """The titles on the open web app's page (shows, films, channels), on screen first: {"titles", "url"}."""
        if self.surface != "web":
            return {"ok": False, "error": "no web app is open (on the launcher, get_screen lists the tiles)"}
        if self.demo:
            return {"ok": True, "titles": [], "url": (self._tiles.get(self.active_tab) or {}).get("url")}
        return {"ok": True, **(self._eval(self._session(), TITLES_JS, timeout=2) or {})}   # (the agent polls it)

    @_action
    def open_title(self, title):
        """Focus and activate the tile list_titles called exactly `title` (the agent matches what was said to it)."""
        if self.surface != "web":
            return {"ok": False, "error": "no web app is open"}
        if self.demo:
            return {"ok": True, "opened": title}
        if self._act(self._session(), OPEN_TITLE_JS % json.dumps(str(title))) is None:
            return {"ok": False, "error": f"no title {title!r} on this page (see list_titles)"}
        return {"ok": True, "opened": title}

    def screenshot(self):
        """PNG bytes of what's on the TV. Raises RuntimeError/OSError when it can't. Only what is in front is read under
        the action lock; the capture, which only reads, waits outside it (a busy page answers late: up to 15 s), so
        Home never queues behind a screenshot."""
        with self.lock:
            if self.demo:
                return png(320, 180, (30, 40, 70))
            sess = None if self.surface == "app" else self._session()
        if sess is None:
            if not shutil.which("gnome-screenshot"):
                raise RuntimeError("screenshots of games and apps need gnome-screenshot")
            with tempfile.TemporaryDirectory() as d:
                f = os.path.join(d, "shot.png")
                subprocess.run(["gnome-screenshot", "-f", f], capture_output=True, timeout=10)
                if not os.path.exists(f):
                    raise RuntimeError("gnome-screenshot failed")
                return Path(f).read_bytes()
        return base64.b64decode(self.cdp.call("Page.captureScreenshot", session=sess, format="png", timeout=15)["data"])

    # ---- the app switcher: what's open, closing it

    def running_apps(self):
        """The switcher's cards, most recently used first: each web tab, game, app and window that is open. Lock-free
        like state(): it reads only fields replaced whole, list() of the tabs and of the launches, single get()s, and
        makes no X calls."""
        now, groups, closing, used, snaps = time.monotonic(), self._groups, self._closing, self._used, self._snaps
        tabs, cards = list(self.tabs)[::-1], []                        # most recent first
        ours = {k for k, p in list(self.pending.items()) if "at" in p}   # our launches: Starting… before Steam's log
        for k in dict.fromkeys(tabs + list(groups) + sorted(self.running | ours)):   # ties: tabs, stacking order, ids
            kind = "tab" if k in tabs else self._kind(k)
            n, c, t = 1 if kind == "tab" else DEMO_WINDOWS.get(k, 1) if self.demo else len(groups.get(k, ())), \
                closing.get(k), self._tiles.get(k)
            if self._ghost(k, n):
                continue
            cards.append({"key": k, "title": self._title_of(k), "kind": kind, "tile": self._public(t) if t else None,
                          "state": ("stuck" if now - c["at"] >= self.stuck_s else "closing") if c else
                          "starting" if not n and "at" in (self.pending.get(k) or {}) else "open",
                          # quitting: it took the quit (the asked window went, or its last one) or was force quit, and
                          # is exiting: stuck, it is "still quitting", never "didn't quit" (a game's launcher may still
                          # be open; a force quit's process may be stuck in the kernel)
                          "windows": n, "quitting": bool(c and (c.get("gone") or c.get("force") or not n)),
                          "game": kind == "game" or kind != "system" and k in self._games_row,   # ("quit the game")
                          "can_close": kind == "tab" or self.demo or k in self._closable,
                          "can_force": self._can_force(k, kind)[0],
                          "snap": f"/api/snap/{urllib.parse.quote(k, safe='')}?v={snaps[k][0]}" if k in snaps else None})
        cards.sort(key=lambda c: -used.get(c["key"], 0))
        return cards

    def _kind(self, key):
        """The kind of a card that isn't a tab: Big Picture and Steam's window are system, any other untiled window
        is a window, a Steam game a game, the rest (exec tiles) apps."""
        return "system" if key in (self._bpm, "x:steam") else "window" if key[:2] == "x:" else \
            "game" if key.startswith("steam:") else "app"

    def _ghost(self, key, n):
        """A Steam game only Steam's log still lists (a missed 'removed'): no window, not our launch, no reaper. Its card
        is hidden; state.running keeps it."""
        return key.startswith("steam:") and not n and not self.pending.get(key) and not reapers(self._procs(), key[6:])

    def _procs(self):
        """The last _scan's proc_table() (about 1 s old while a Steam game runs or starts; replaced whole). Never
        scanned here: callers hold the lock, and a cmdline read waits on a wedged process's mmap_lock."""
        return self._ptab[1]

    def _scan(self):
        """_tick starts one at a time on a thread of its own: a cmdline read that waits on a wedged game's mmap_lock
        holds up only this (the table goes stale), never the lock nor the window watch (the Home and push-to-talk keys).
        ponytail: a scan wedged for good stops further scans; a per-pid timeout needs a process per read."""
        try:
            self._ptab = (time.monotonic(), proc_table())
        finally:
            self._scanning = False

    def _pid(self, w):
        """Window w's process (_NET_WM_PID) when it is a live one of ours, else None (unset, or another namespace's)."""
        p = (self._wins.get(w) or (0, 0, None))[2]
        return p if _ours(p) else None

    def _title_of(self, key):
        """A card's name: its tile's, the Steam library's, or one made from its window class (x:org.gnome.Nautilus →
        "Nautilus"). Never a live window title: that's anyone's text (a web page's, in a browser window). Only an app
        id loses its prefix: a class with a version or a file name keeps its name (x:minecraft* 1.21.1 was "1",
        x:hollow_knight.x86_64 "X86 64", x:battle.net.exe "Exe")."""
        t = self._tiles.get(key) or self.games.get(key[6:] if key.startswith("steam:") else None) or {}
        name = re.sub(r"\.(?:exe|x86(?:_64)?|arm64|aarch64)$", "", key[2:] if key[:2] == "x:" else
                      key.replace("steam:", "steam app "))   # (not in the library)
        if re.fullmatch(r"[a-z][\w-]*(?:\.[a-z][\w-]*){2,}", name):
            name = name.rsplit(".", 1)[-1]       # a reverse-DNS app id: org.gnome.nautilus
        return t.get("title") or name.replace("-", " ").replace("_", " ").title()

    def _open_window(self, key):
        """Switch to an untiled card (an x: window, a Steam game no tile lists): its top-most window to the front, the
        kiosk iconified, as a tile resumes. Under the lock (open())."""
        ws = self._groups.get(key)
        if not (ws and self.x_ok):               # (a running Steam game with no window yet, or none: as a tile's says)
            return {"ok": False, "error": f"{self._title_of(key)} " +
                    ("has no window to show yet" if key in self.running else "isn't open anymore")}
        self._pause_front()
        with X11() as x:
            x.activate(ws[0])
            if self._kwin:
                x.iconify(self._kwin)
        self._home_guard = self._steam_guard = 0.0   # what the user chose stays up (Steam's own window included)
        self._active_key = key
        self._stamp(key)
        self._set(surface="app", active=None, title=self._title_of(key))
        return {"ok": True, "surface": "app"}

    def _can_force(self, key, kind):
        """(True, None) if force quit can end key's processes, else (False, why). Never a tab, Big Picture, Steam's own
        window or an untiled one: those can take Steam, and every game, down."""
        if kind not in ("game", "app"):
            return False, "only games and apps can be force quit"
        if self.demo:
            return True, None
        if kind == "game":                       # exactly one launch's process tree
            n = len(reapers(self._procs(), key[6:]))
            return (True, None) if n == 1 else (False, "two launches match" if n else "its Steam process wasn't found")
        if (self._tiles.get(key) or {}).get("argv", [])[:2] == ["flatpak", "run"]:
            return (True, None) if key in self.running else (False, "it isn't running")
        return ((True, None) if (self.apps.get(key) or {}).get("proc") else   # (_tick drops an exited one)
                (False, "it wasn't started from here"))

    @_action
    def close_app(self, key, force=False, person=False):
        """Quit an open app the way its own close button would: a tab at once, anything else asked with WM_DELETE_WINDOW,
        top-most window first; force ends a game's or app's processes. person: someone at the TV asked (the switcher's
        page, their own words); agents may close tabs, Big Picture and plain windows, never a game or app, never force."""
        card = next((c for c in self.running_apps() if c["key"] == key), None)
        name, now = card["title"] if card else self._title_of(key), time.monotonic()
        held = self._holding(key, self.pending.get(key))   # a game's launch, its splash up
        log.info("close %s (%s%s), asked by %s", key, card and card["kind"], ", force" if force else "",
                 "a person" if person else "an agent")
        if not card:
            return {"ok": False, "error": f"{name} isn't open"}
        kind, state = card["kind"], card["state"]
        if not person and (force or kind in ("game", "app") or key in self._game_tabs):   # (a cloud game's tab too)
            return {"ok": False, "error": f"Games and apps quit only when someone at the TV asks: say “quit {name}”, or "
                                          "use the app switcher (press TV twice)."}
        if kind == "tab":
            tab = self.tabs.pop(key, {})         # untracked first, as back() does a popup
            if self.cdp:
                self._close(tab.get("target"), (tab.get("popup") or {}).get("target"))
            self._tab_gone(key)
            self.emit("toast", {"text": f"Closed {name}", "kind": "info"})
            r = {"ok": True, "state": "closed", "title": name}
        elif state == "starting" and not card["can_force"] and key not in self.running:
            # Nothing started to signal: Steam took the launch and ran nothing (a launch-option chooser, a cloud
            # conflict, a missing executable). Our launch ends: its card goes, a retry launches at once (it said "still
            # starting" for the 5 min a Steam launch waits). A window that still comes is raised (pending stays)
            self.pending[key].pop("at", None)
            self.emit("toast", {"text": f"Closed {name}", "kind": "info"})
            r = {"ok": True, "state": "closed", "title": name}
        elif force:
            ok, why = self._can_force(key, kind)
            if not ok:
                return {"ok": False, "error": f"Couldn't force quit {name}: {why}."}
            self.pending.pop(key, None)          # a resume retry must never raise what we're closing (only then: a
            was = self._closing.get(key)         # refused close leaves a launch be); a force that fails puts was back
            self._closing = {**self._closing, key: {"at": now, "force": True}}
            self.emit("toast", {"text": f"Force quitting {name}…", "kind": "info"})
            _bg(self._force, key, kind, name, was)
            r = {"ok": True, "state": "forcing", "title": name}
        elif state == "closing":
            return {"ok": True, "state": "closing", "title": name}
        elif state in ("stuck", "starting") or not card["can_close"]:
            # Stuck once its window went: it took the quit and is still exiting (D24). A window that can't be asked:
            # Wine's desktop (that ends every process in it at once, unsaved games too) or one without WM_DELETE_WINDOW
            n, ask = card["windows"], "can't be asked to quit safely: quit it from its own menu"
            why = {"starting": f"is still starting: say “force quit {name}” to stop it." if card["can_force"] else
                   "is still starting.", "stuck": "is still quitting." if card["quitting"] else
                   "didn't quit when asked."}.get(state) or (ask + (", or force quit it." if card["can_force"] else ".")
                                                             if n else "has no window to ask.")
            return {"ok": False, "error": f"{name} {why}", "force": card["can_force"]}
        else:
            self.pending.pop(key, None)          # (as force, above)
            entry = {"at": now}
            if not self.demo:                    # the top-most window only (_tick asks the next once it and its process
                w = (self._groups.get(key) or [0])[0]   # are gone); one gone meanwhile, or no longer listing it, is
                with X11() as x:                 # stuck at once
                    entry = {"at": now if w and x.delete(w) else now - self.stuck_s, "w": w, "pid": self._pid(w)}
            elif key not in DEMO_STUCK:
                _bg(self._demo_quit, key, 1.2)
            if key == self._bpm:                 # Steam's main window popping up as Big Picture goes: behind the kiosk
                self._steam_guard = now + 15
            self._closing = {**self._closing, key: entry}
            self.emit("toast", {"text": f"Asking {name} to quit…" if kind in ("game", "app") else f"Closing {name}",
                                "kind": "info"})
            r = {"ok": True, "state": "closing", "title": name}
        if held and not self._holding(key, self.pending.get(key)):   # quit while it loads (by voice: the splash is up)
            self.emit("launch", {"id": key, "stage": "failed"})       # its splash goes with it
        self._apps_tick()
        return r

    def _force(self, key, kind, name, was=None):
        """Off the lock (the TERM grace is 3 s): end key's processes. A Steam game: its one reaper's descendants, the
        reaper spared (it exits once its tree is gone, and Steam logs "removed"); nothing at all is signalled if that set
        holds Steam, the desktop, the kiosk or us. Flatpak: `flatpak kill`. Our own child: its process group. Demo: it
        exits 0.5 s later. On failure the close entry it replaced (was) comes back, so a stuck app stays "Didn't quit"
        (not "open"), else none; a toast says why."""
        if self.demo:
            return self._demo_quit(key, 0.5)
        try:
            argv = (self._tiles.get(key) or {}).get("argv", [])
            if kind == "game":                   # the last scan's table, never a new one: a cmdline read waits on a
                table = self._procs()            # wedged game's mmap_lock, and that game is what this is for. A pid
                # reused since is never signalled (terminate checks start times). ponytail: a process started since
                # that scan (under ~1 s) is missed; the reaper waits for it, so the card stays and force goes again
                rs = reapers(table, key[6:])
                if len(rs) != 1:
                    raise RuntimeError("two launches match" if rs else "its Steam process wasn't found")
                kids, mine = descendants(table, rs[0]), {os.getpid(), getattr(self.chrome, "pid", None)}
                if any(p in mine or _protected(table[p][2]) for p in kids):
                    raise RuntimeError("Steam or the desktop is in its process tree")
                exited, killed = terminate(kids, table)
                log.info("force quit %s: reaper %d, %d exited on TERM, %d killed", key, rs[0], len(exited), len(killed))
            elif argv[:2] == ["flatpak", "run"]:
                app = next((a for a in argv[2:] if not a.startswith("-")), "")
                r = subprocess.run(["flatpak", "kill", app], capture_output=True, text=True, timeout=10)
                if r.returncode:
                    raise RuntimeError(r.stderr.strip() or "flatpak kill failed")
            else:                                # ponytail: children that outlive their leader are left alone
                proc = (self.apps.get(key) or {}).get("proc")
                if not (proc and proc.poll() is None):
                    raise RuntimeError("it wasn't started from here")
                os.killpg(proc.pid, signal.SIGTERM)          # (start_new_session: its pid is its group's)
                try:
                    proc.wait(3)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)      # the leader still lives: the group can't be anyone else's
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            with self.lock:                      # (none if it went meanwhile: it exited)
                c = {k: v for k, v in self._closing.items() if k != key}
                self._closing = {**c, key: was} if was and key in self._closing else c
            self.emit("toast", {"text": f"Couldn't force quit {name}: {e}", "kind": "error"})
            self._apps_tick()

    def _demo_quit(self, key, after):
        """Demo: the app exits `after` s after it was asked, as a real one leaves the windows and Steam's log."""
        time.sleep(after)
        with self.lock:
            if key in self._closing:             # (its close entry and snapshot go: both replaced whole)
                self._closing = {k: v for k, v in self._closing.items() if k != key}
                self._snaps = {k: v for k, v in self._snaps.items() if k != key}
                front = self.surface == "app" and self.active == key
                self._set(running=self.running - {key}, **({"surface": "launcher", "active": None, "title": "SparkHome"}
                                                            if front else {}))
        self._apps_tick()

    def _apps_tick(self):
        """While the switcher is open, send its cards again whenever they changed (a close, a launch, a stuck timer)."""
        if self.ui["screen"] == "switcher":
            apps = self.running_apps()
            if apps != self._apps_sent:
                self._apps_sent = apps
                self.emit("switcher", {"apps": apps})

    def snap(self, key):
        """PNG of key's app as it was when last left, else None (GET /api/snap/<key>)."""
        return (self._snaps.get(key) or (0, None))[1]

    def _snap_put(self, key, data):
        self._snap_n += 1                        # the URL's ?v=: a new snapshot is a new URL (served with max-age)
        self._snaps = {**self._snaps, key: (self._snap_n, data)}

    # ---- web tabs over CDP

    def _cdp_ok(self):
        if not (self.cdp and self.cdp.connected.is_set()):
            raise ConnectionError("the browser isn't ready yet")
        return self.cdp

    def _front(self):
        """The page on screen: the web tile's tracked popup, else its tab, else the launcher tab."""
        tab = self.tabs.get(self.active_tab) or self.launcher
        return tab.get("popup") or tab

    def _session(self):
        s = self._front().get("session")
        if not s:
            raise ConnectionError("the browser isn't ready yet")
        self._cdp_ok()
        return s

    def _pause_front(self):
        """Pause the media of the web tile in front (its tab and its popup) before something else takes the screen."""
        tab = self.tabs.get(self.active_tab) or {}
        for s in {tab.get("session"), (tab.get("popup") or {}).get("session")} - {None}:
            try:                                 # a busy page still runs it once free (measured): don't wait long
                self.cdp.call("Runtime.evaluate", session=s, expression=PAUSE_JS, timeout=1)
            except (OSError, RuntimeError) as e:
                log.warning("pause: %s", e)

    def _eval(self, sess, expr, timeout=10, gesture=True):
        """A page script's value. Under the action lock a busy page holds Home back until it answers: page reads the
        agent polls pass a short timeout (as _pause_front does), and actions run through _act. gesture=False: no user
        activation for the page (sleep's background read is nobody's press)."""
        r = self.cdp.call("Runtime.evaluate", session=sess, expression=expr, returnByValue=True, userGesture=gesture,
                          timeout=timeout)
        if "exceptionDetails" in r:
            raise RuntimeError("page script failed: " + r["exceptionDetails"].get("text", "?"))
        return r.get("result", {}).get("value")

    def _act(self, sess, js, press=True):
        """An action's page script -> its value, or SENT when a busy page doesn't answer in 2 s (Home waits on the lock
        meanwhile): it still runs once the page is free (measured), so that's no failure for the model to send again. On
        a leanback page (nav "keys") a press is the trusted Enter after the script, and a busy page takes that first, on
        its old focus (measured): there a busy page is no press (TimeoutError). ponytail: SENT trusts the page not to
        change before it runs, and a busy leanback page hands the press back to the model; re-run the script, then the
        Enter, once the page answers if that bites."""
        keys = press and (self._tiles.get(self.active_tab) or {}).get("nav") == "keys"
        try:
            v = self._eval(sess, js, timeout=2)
        except TimeoutError:
            if keys:
                raise
            return SENT
        if keys and v is not None:
            self._key(sess, "enter")             # leanback ignores DOM click(); the script focused it, an Enter acts
        return v

    def _key(self, sess, name, modifiers=0, end=None):
        """Trusted key press (rawKeyDown/keyUp; keyDown with text for Enter/Space so buttons activate). Chrome answers
        only once the page handles it, so a busy page held the lock (Home behind it) 10 s: like _act, a press waits 2 s
        in all (end: navigate's presses share it), then counts as sent, as it lands once the page is free (measured);
        the rest go unawaited. -> end, for the next press of the same action."""
        key, code, vk, text = CDP_KEYS[name]
        kw = dict(session=sess, key=key, code=code, windowsVirtualKeyCode=vk, modifiers=modifiers)
        end = time.monotonic() + 2 if end is None else end
        for typ in ("keyDown" if text else "rawKeyDown", "keyUp"):
            try:
                self.cdp.call("Input.dispatchKeyEvent", type=typ, timeout=max(0, end - time.monotonic()),
                              **({"text": text} if text and typ != "keyUp" else {}), **kw)
            except TimeoutError:
                end = 0
            except RuntimeError as e:            # its keyDown may have closed the page (a sign-in popup's last
                if typ != "keyUp":               # button): the press is done anyway
                    raise
                log.info("keyUp: %s", e)
        return end

    def _page_title(self, tab):
        """The tab's live title. Chrome sends no Target.targetInfoChanged when document.title changes (measured, Chrome
        154), so the event-fed cache keeps the load-time placeholder ("example.com"); it's only the fallback here."""
        target = tab.get("target") if self.cdp and tab else None
        if not target:
            return None
        try:
            return self.cdp.call("Target.getTargetInfo", targetId=target, timeout=2)["targetInfo"]["title"]
        except (OSError, RuntimeError, KeyError):
            return (self.cdp.targets.get(target) or {}).get("title")

    def _page_url(self):
        return (self.cdp.targets.get(self._front().get("target")) or {}).get("url", "")

    def _attach(self, t, target):
        """Session on a web tab + what makes it TV-drivable. UA overrides are per session, so sessions stay attached."""
        cdp = self.cdp
        s = cdp.call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        cdp.call("Page.enable", session=s)       # measured: without these the init script and the binding
        cdp.call("Runtime.enable", session=s)    # never reach new documents
        if t.get("ua"):
            cdp.call("Emulation.setUserAgentOverride", session=s,
                     userAgent=(self.config.get("user_agents") or {}).get(t["ua"], t["ua"]))
        cdp.call("Runtime.addBinding", session=s, name="sparkhome")
        nav, skin = t.get("nav") or "pointer", t.get("skin")
        cdp.call("Page.addScriptToEvaluateOnNewDocument", session=s, runImmediately=True,
                 source=BRIDGE_JS.replace("__NAV__", json.dumps(nav)).replace("__TILES__", json.dumps(t.get("tiles") or ""))
                 + (SKIN_JS.replace(
                     "__SKIN__", json.dumps(skin if isinstance(skin, dict) else {}))
                    if (skin is True or isinstance(skin, dict)) and nav == "pointer" else ""))   # ({}: the plain skin)
        return s

    def _show(self, tab):
        tab = tab.get("popup") or tab            # a tile's popup sits on top of it
        self.cdp.call("Target.activateTarget", targetId=tab["target"])
        if tab.get("session"):                   # (a popup still being set up has none yet)
            self.cdp.call("Page.bringToFront", session=tab["session"])

    def _close(self, *targets):
        for target in filter(None, targets):
            try:
                self.cdp.call("Target.closeTarget", targetId=target)
            except (OSError, RuntimeError):  # gone already
                pass

    def _open_web(self, t, url=None):
        tid = t["id"]
        if tid != self.active_tab:
            self._pause_front()
        if not self.demo:
            cdp = self._cdp_ok()
            tab = self.tabs.get(tid)
            if tab and tab["target"] not in cdp.targets:
                del self.tabs[tid]
                tab = None
            new = tab is None
            if new:                              # a tab per tile: about:blank → attach + set up → navigate
                tab = {"target": cdp.call("Target.createTarget", url="about:blank")["targetId"], "session": None}
            try:
                if new:
                    tab["session"] = self._attach(t, tab["target"])
                    url = url or t["url"]
                if url:
                    try:
                        cdp.call("Page.navigate", session=tab["session"], url=url, timeout=3)   # don't hold the lock
                    except TimeoutError:
                        log.warning("%s is slow to load", url)
                self._show(tab)
            except (OSError, RuntimeError):      # never leave a blank half-made tab on the TV
                for fn in ([lambda: cdp.call("Target.closeTarget", targetId=tab["target"])] if new else []) + \
                          [lambda: self._show(self.launcher)]:
                    try:
                        fn()
                    except (OSError, RuntimeError, KeyError):
                        pass
                raise
            self.tabs[tid] = tab
            self._kiosk_front()
        self.tabs[tid] = self.tabs.get(tid, {})
        self.tabs.move_to_end(tid)
        self._cap_tabs()
        self._set(surface="web", active=tid, active_tab=tid, title=t["title"])
        return {"ok": True, "surface": "web"}

    def _cap_tabs(self):
        """Past max_web_tabs the least recently used tabs close, never the newest nor a cloud game's: closing that ends
        the game, a person's call (close_app), never a side effect of an agent's opens. ponytail: game tabs may stay
        past the cap (a few Games-row web tiles at most)."""
        n = len(self.tabs) - max(1, int(self.settings.get("max_web_tabs", 4)))
        for old in [k for k in list(self.tabs)[:-1] if k not in self._game_tabs][:max(0, n)]:
            tab = self.tabs.pop(old)
            if old == self.active_tab:
                self.active_tab = None
            if self.cdp:                                       # its popup goes with it
                self._close(tab.get("target"), (tab.get("popup") or {}).get("target"))

    def _tile_for_url(self, url):
        # host AND port: on 127.0.0.1 the port is the app (10-31: a stray copy of our own :8780 page was adopted as a lab
        # app's tab, the only tile on 127.0.0.1:87..., so that app opened onto a second SparkHome)
        host = urllib.parse.urlsplit(url).netloc.lower()
        cands = [t for t in self._tiles.values() if t["type"] == "web" and host and urllib.parse.urlsplit(t["url"]).netloc.lower() == host]
        return max(cands, key=lambda t: len(os.path.commonprefix([url, t["url"]])), default=None)

    def _cdp_connected(self):
        """(Re)connected to Chrome: find or make the launcher tab, re-attach our web tabs (overrides and bindings are
        per session), adopt pages a previous daemon left (by URL) and close the rest (e.g. a video still playing)."""
        cdp = self.cdp
        cdp.call("Target.setDiscoverTargets", discover=True)
        cdp.targets.update((t["targetId"], t) for t in cdp.call("Target.getTargets")["targetInfos"])
        with self.lock:                          # (a copy: the CDP reader thread adds targets meanwhile)
            pages = {k: v for k, v in list(cdp.targets.items()) if v.get("type") == "page"}
            mine = (f"http://127.0.0.1:{self.port}/", f"http://localhost:{self.port}/")
            target = next((k for k, v in pages.items() if (v.get("url", "") + "/").startswith(mine)), None)
            if not target:
                target = cdp.call("Target.createTarget", url=self.origin + "/")["targetId"]
            self.launcher = {"target": target, "session": cdp.call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]}
            cdp.call("Page.reload", session=self.launcher["session"])   # may show an error page from while we were down
            for tid in [k for k, tab in self.tabs.items() if tab.get("target") not in pages or k not in self._tiles]:
                del self.tabs[tid]
            tracked = {tab["target"] for tab in self.tabs.values()}
            adopted = []
            for k, v in pages.items():
                if k == target or k in tracked:
                    continue
                t = self._tile_for_url(v.get("url", ""))
                if t and t["id"] not in self.tabs:
                    self.tabs[t["id"]] = {"target": k}
                    adopted.append(t["id"])
                else:
                    self._close(k)               # popups included: their sessions died with the connection
            for tid, tab in list(self.tabs.items()):
                t = self._tiles[tid]
                tab.pop("popup", None)
                try:
                    tab["session"] = self._attach(t, tab["target"])
                    if tid in adopted and self._eval(tab["session"], "document.visibilityState") == "visible":
                        self.active_tab = tid
                    if t.get("ua"):
                        cdp.call("Page.reload", session=tab["session"])
                except (OSError, RuntimeError) as e:   # the page went away meanwhile, or hangs (a dialog, a busy page)
                    log.warning("dropping tab %s: %s", tid, e)
                    self._close(self.tabs.pop(tid)["target"])
            if self.active_tab not in self.tabs:
                self.active_tab = None
            self._cap_tabs()
            self._show(self.tabs[self.active_tab] if self.active_tab else self.launcher)
            if self.surface != "app":
                self._set(surface="web" if self.active_tab else "launcher", active=self.active_tab,
                          title=self._page_title(self.tabs.get(self.active_tab) or self.launcher) or "SparkHome")
        log.info("kiosk ready: launcher tab %s, %d web tab(s)", target[:8], len(self.tabs))
        if not self.demo:
            _bg(self._nudge)

    def _nudge(self):
        """Move the pointer 1 px and back once the kiosk is up. Until Chrome sees motion over its window, the TV keeps the
        desktop's arrow instead of the launcher's cursor: none (measured at boot: 294 visible cursor pixels; 0 once the
        launcher's 3 s pointer timeout runs out after a nudge)."""
        time.sleep(1)                            # the window is mapped by now; Chrome only needs one motion event
        if _xlib():
            try:
                with X11() as x:
                    x.pointer(1, 0, ())
                    x.pointer(-1, 0, ())
            except OSError as e:
                warn_once("x-nudge", "X11: %s", e)

    def _cdp_event(self, m):
        # Never wait for self.lock here: only this thread answers dialogs, and an action holding the lock may be waiting
        # on one (an Enter whose page calls alert(); measured: 20 s stalls). Work that needs the lock runs in _bg.
        meth, p = m.get("method"), m.get("params") or {}
        if meth == "Runtime.bindingCalled" and p.get("name") == "sparkhome":
            s = m.get("sessionId")
            if not s or self.surface != "web" or s != self._front().get("session"):
                return      # only the page on screen speaks for the pad and remote: not a hidden tab, not the opener
                            # behind its popup, not a page left behind a game (select/arrows would key the game)
            try:
                action = json.loads(p.get("payload") or "{}").get("action")
            except (ValueError, AttributeError):
                return
            if action not in (*DIRS, "home", "back", "select", "media") or self._sleep == "asleep" or self.wake("page"):
                return      # (a press that wakes the screen does nothing else.) Input, as a pad's stick there is,
                            # but never a wake from asleep: any script on the page can call this, and a pad's press
                            # wakes the screen itself (evdev), a key through X
            # Not on this thread: an Enter that opens alert() waits for the dialog, and this thread answers dialogs.
            # ponytail: a thread per press, so presses queued behind a stalled page may run out of order; a queue if so
            if action in DIRS:
                _bg(self.navigate, action)
            elif action in ("home", "back", "select"):
                _bg(getattr(self, action))
            elif action == "media":
                _bg(self.media, "toggle")
        elif meth == "Target.targetCreated":
            # A tile's window.open / target=_blank / sign-in popup is on screen now: set it up like its tile and drive
            # it instead. Back or closing the tile closes it, nothing sooner (OAuth needs window.opener). ponytail: one
            # popup per tile; a popup's own popup replaces it and the first lingers untracked until CDP reconnects. A
            # stack per tab if nested sign-ins ever need it.
            ti = p.get("targetInfo") or {}
            tid, tab = next(((k, tab) for k, tab in list(self.tabs.items()) if ti.get("type") == "page" and
                             ti.get("openerId") in {tab.get("target"), (tab.get("popup") or {}).get("target")} - {None}),
                            (None, None))
            if tab is not None:  # in front from now on, in event order (one dict store, no lock); _popup attaches it.
                tab["popup"] = pop = {"target": ti["targetId"], "session": None}   # measured: recorded only after
                _bg(self._popup, tid, tab, pop)  # the attach, an OAuth hop's next window lost the race to the close
        elif meth == "Page.javascriptDialogOpening" and m.get("sessionId"):
            # Nobody can answer a dialog on the TV, and it wedges the tab's CDP: dismiss alerts, let a navigation
            # leave (beforeunload), cancel confirm and prompt.
            try:
                self.cdp.call("Page.handleJavaScriptDialog", session=m["sessionId"],
                              accept=p.get("type") in ("alert", "beforeunload"))
            except (OSError, RuntimeError):
                pass
        elif meth in ("Target.targetDestroyed", "Target.targetCrashed"):
            target = p.get("targetId")
            if meth == "Target.targetCrashed" and target != self.launcher.get("target"):
                _bg(self._close, target)         # its targetDestroyed cleans up
            elif target in {self.launcher.get("target")} | {x for tab in list(self.tabs.values())
                                                           for x in (tab.get("target"), (tab.get("popup") or {}).get("target"))}:
                _bg(self._target_gone, meth, target)
        elif meth == "Target.targetInfoChanged" and self.surface != "app":
            ti = p.get("targetInfo") or {}
            if ti.get("targetId") == self._front().get("target"):
                _bg(lambda: self._set(title=ti.get("title", "")))   # out of order at worst: _tick reads it live

    def _popup(self, tid, tab, pop):
        """Set a tile's new popup up like the tile, off the event thread (Page.enable waits while a dialog is up). Until
        then actions get "not ready" rather than reaching the tile hidden behind it (measured: a second OK opened a
        second popup); one closed or replaced meanwhile is out of its tab, so its session lands in a dict nobody reads."""
        try:
            pop["session"] = self._attach(self._tiles.get(tid, {}), pop["target"])
        except (OSError, RuntimeError) as e:
            log.warning("popup of %s: %s", tid, e)
            with self.lock:
                if tab.get("popup") is pop:
                    del tab["popup"]
                    self._close(pop["target"])       # still open but undriven (busy past the timeout): not left
                    if tid == self.active_tab:       # on screen over the tile the remote drives now
                        try:
                            self._show(tab)
                        except (OSError, RuntimeError):
                            pass

    def _tab_gone(self, tid):
        """Web tile tid's tab was closed and untracked. Was it the kiosk's page, the launcher's takes its place, with
        Home only when the kiosk was on screen (§6.1): behind a game or app the kiosk stays put, and _tick shows the
        launcher page once the kiosk is back in front. Not at once: activating a tab activates Chrome's window too
        (Browser::ActivateContents [I]; unmeasured on the Spark), which would put the kiosk over the game."""
        if tid != self.active_tab:
            return self._set()
        self.active_tab = None
        if self.surface == "web":
            return self._home()
        self._launcher_due = True
        self._set()

    def _target_gone(self, meth, target):
        with self.lock:
            tid = next((k for k, tab in self.tabs.items() if tab.get("target") == target), None)
            pid = next((k for k, tab in self.tabs.items() if (tab.get("popup") or {}).get("target") == target), None)
            if pid:                              # its popup closed (sign-in done, or Back): the tile again
                if (self.tabs[pid].get("popup") or {}).get("target") == target:   # (not a newer one: ponytail, the
                    del self.tabs[pid]["popup"]  # event thread records popups lock-free; a few bytecodes of race left)
                if pid == self.active_tab:
                    self._show(self.tabs[pid])
            elif tid:
                self._close((self.tabs.pop(tid).get("popup") or {}).get("target"))
                self._tab_gone(tid)
            elif target == self.launcher.get("target") and self.cdp.connected.is_set():
                if meth == "Target.targetCrashed":
                    self.cdp.call("Page.reload", session=self.launcher["session"])
                else:                            # someone closed the launcher tab: make a new one
                    t = self.cdp.call("Target.createTarget", url=self.origin + "/")["targetId"]
                    self.launcher = {"target": t, "session": self.cdp.call("Target.attachToTarget", targetId=t,
                                                                           flatten=True)["sessionId"]}

    # ---- Chrome kiosk

    def _chrome_supervisor(self):
        """Keep one kiosk Chrome alive: adopt whatever answers on the CDP port, else launch ours; relaunch 2→30 s."""
        port, delay, started = int(self.settings.get("cdp_port", 9223)), 1, 0.0
        while True:
            if self.chrome and self.chrome.poll() is None:
                time.sleep(1)
                continue
            if self.chrome:
                delay = min(delay * 2, 30) if time.monotonic() - started < 60 else 2
                log.warning("Chrome exited (code %s); relaunching in %d s", self.chrome.returncode, delay)
                self.chrome = None
                time.sleep(delay)
            try:
                http_json("127.0.0.1", port, "/json/version")
                time.sleep(2)                    # a Chrome already answers (e.g. after a daemon restart): adopt it
                continue
            except (OSError, ValueError, http.client.HTTPException):
                pass
            if not self.launch_chrome:
                time.sleep(2)
                continue
            self.chrome, started = self._launch_chrome(port), time.monotonic()
            if not self.chrome:
                time.sleep(30)

    def _launch_chrome(self, port):
        prof = self.settings.get("chrome_profile") or os.path.expanduser("~/.local/share/sparkhome/chrome")
        flags = [f for f in self.settings.get("chrome_flags", []) if not str(f).startswith(BAD_FLAGS)]
        for f in set(map(str, self.settings.get("chrome_flags", []))) - set(map(str, flags)):
            warn_once(f"flag:{f}", "refusing Chrome flag %s (CDP security / Widevine updates)", f)
        argv = [self.settings.get("chrome", "google-chrome"), "--kiosk", "--class=SparkHome", f"--user-data-dir={prof}",
                f"--remote-debugging-port={port}", "--password-store=basic", "--no-first-run", "--no-default-browser-check",
                "--disable-session-crashed-bubble", "--noerrdialogs", "--autoplay-policy=no-user-gesture-required",
                "--check-for-update-interval=31536000", *map(str, flags), self.origin + "/"]
        try:
            os.makedirs(prof, exist_ok=True)
            _mark_clean_exit(prof)
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        except OSError as e:
            warn_once("chrome", "can't start Chrome (%s): %s", argv[0], e)
            return None
        log.info("started Chrome kiosk (pid %d)", p.pid)
        return p

    def _kiosk_front(self):
        """Activate the kiosk window unless it's already in front (needs X; no-op elsewhere)."""
        if not (self.x_ok and self._kwin):
            return
        try:
            with X11() as x:
                if x.active() != self._kwin:
                    x.activate(self._kwin)
        except OSError as e:
            warn_once("x-front", "X11: %s", e)

    def _xkeys(self, names):
        if not self.x_ok:
            raise RuntimeError("no X11 display to send keys to")
        with X11() as x:
            x.keys(names)

    # ---- native apps: Steam and exec

    def _launch(self, t, entry):
        """Off the lock: start t, launch entry (its pending one, touched only while it is still that launch's)."""
        try:
            ok, err = self._steam(t) if t["type"] == "steam" else self._exec(t)
        except (OSError, subprocess.SubprocessError) as e:
            ok, err = False, str(e)
        with self.lock:
            mine = self.pending.get(t["id"]) is entry
            if ok and mine:
                entry["sent"] = time.monotonic()  # Steam took it: the prompt and no-process clocks start (_hold)
            elif not ok and mine:
                del self.pending[t["id"]]
        if not ok:
            self.emit("toast", {"text": f"Couldn't start {t['title']}: {err}", "kind": "error"})
        if not ok and mine:                      # (a stale launch's thread never fails a newer launch's splash)
            self.emit("launch", {"id": t["id"], "stage": "failed"})
            ws = self._groups.get("x:steam") if t["id"][:6] == "steam:" and self.x_ok else None
            if ws:                               # what Steam shows (a sign-in, an update) says why: it was under the
                self._home_guard = self._steam_guard = 0.0   # splash's pinned kiosk. It stays up: no guard (a cold
                with X11() as x:                 # start's, (b)) pushes it back. Unpinned first, as _hold raises
                    if self._kwin:
                        x.above(self._kwin, False)
                    x.activate(ws[0])

    def _exec(self, t):
        argv = list(t["argv"])
        if t.get("cpus") and shutil.which("taskset"):
            argv = ["taskset", "-c", str(t["cpus"])] + argv
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        with self.lock:
            self.apps[t["id"]] = {"proc": p, "win": None}
        return True, None

    def _gs_env(self):
        """GameSpark's env.sh uses the running Steam's setup, else GAMESPARK_STEAM, else snap. settings.steam_setup (as in
        GameSpark's console mode, "fex") counts only for a cold start: see GS_ENV."""
        return {**os.environ, "GAMESPARK_STEAM": str(self.settings.get("steam_setup") or "")}

    def _steam(self, t):
        """GameSpark's steam_launch (starts Steam if needed, confirms on Steam's log, retries); else the pipe."""
        arg, gs = str(t.get("appid") or t.get("uri")), self.settings.get("gamespark", "")
        if os.path.isfile(os.path.join(gs, "lib", "env.sh")):
            wait = ('for i in $(seq 120); do [ -p "$STEAM_PIPE" ] && pgrep -f "[s]teamwebhelper" >/dev/null && break; '
                    'sleep 1; done; ')
            if t.get("appid"):
                script = (GS_ENV + 'steam_running || { echo started; setsid -f $STEAM_START -silent >/dev/null 2>&1 '
                          '</dev/null; }; ' + wait + 'steam_launch "$2" ""')
            else:                                # Steam ignores pipe commands until "Loading user data" is done
                script = (GS_ENV + 'up=1; steam_running || { up=0; echo started; setsid -f $STEAM_START -silent '
                          '>/dev/null 2>&1 </dev/null; }; ' + wait + '[ $up = 1 ] || sleep 20; steam_cmd "$2"')
            r = subprocess.run(["bash", "-c", script, "_", gs, arg], capture_output=True, text=True, timeout=360,
                               env=self._gs_env())
            if "started" in r.stdout.split():    # (b) we started Steam: its own window may pop up for a while, as after
                self._steam_guard = time.monotonic() + 60   # _steam_autostart (no _kiosk_front: a game may be in front)
            if r.returncode:
                log.warning("steam launch %s failed: %s", arg, r.stderr.strip()[-300:])
            return r.returncode == 0, "Steam didn't accept the launch"
        return pipe_write(self.settings.get("steam_pipe", ""), (f"-applaunch {arg}" if t.get("appid") else arg) + "\n", 10)

    def _steam_autostart(self):
        gs = self.settings.get("gamespark", "")
        if not os.path.isfile(os.path.join(gs, "lib", "env.sh")):
            log.info("steam_autostart: no GameSpark at %s, leaving Steam alone", gs)
            return
        script = (GS_ENV + 'steam_running && { echo up; exit 0; }; setsid -f $STEAM_START -silent >/dev/null 2>&1 '
                  '</dev/null; for i in $(seq 120); do [ -p "$STEAM_PIPE" ] && pgrep -f "[s]teamwebhelper" >/dev/null '
                  '&& exit 0; sleep 1; done; exit 1')
        r = subprocess.run(["bash", "-c", script, "_", gs], capture_output=True, text=True, timeout=150, env=self._gs_env())
        if r.stdout.strip() == "up":
            return
        log.info("started Steam (%s)", "ready" if r.returncode == 0 else "pipe not ready after 120 s")
        self._steam_guard = time.monotonic() + 60       # Steam's own window may pop up over the kiosk for a while
        self._kiosk_front()

    def _tail_steam_log(self, poll=1):
        """Follow Steam's console_log.txt ('Game process added/removed : AppID N') and GameSpark's launches.log (what
        tools/launch.sh applied), new lines only (follow()); (e): a profiled game whose launch wrote no line."""
        console = follow(os.path.join(self.settings.get("steam_root", ""), "logs", "console_log.txt"))
        launches = follow(LAUNCHES)
        while True:
            for kind, appid in steam_log_events(next(console)):
                self._steam_event(kind, appid)
            for line in next(launches).splitlines():
                self._launch_line(line)
            now = time.monotonic()
            for appid in [a for a, t0 in list(self._noconfig.items()) if now - t0 >= NOCONFIG_S]:
                self._no_config(appid)
            time.sleep(poll)

    def _steam_event(self, kind, appid):
        """Steam's log: a game's process started (added) or ended (removed). Our launch of it ("at": its Starting…
        card) ended when its process did (it died at start): no card, a retry launches at once, and its splash fails
        EXIT_S later unless a process started after it (the game, after Proton's install-script evaluator) makes it
        Starting… again. Its window, if one still comes, is still raised."""
        k, now = f"steam:{appid}", time.monotonic()
        with self.lock:
            p = self.pending.get(k) or {}
            if kind == "added":
                self.log_running.add(appid)
                p.setdefault("at", now)
                p.setdefault("added", now)
                p.pop("gone", None)
                if self._profiled(appid) and now - self._lines.get(appid, -1e9) > 5:   # (e): its launch.sh line is due
                    self._noconfig[appid] = now          # (or came first, same poll). From the latest process: Proton's
                    # install-script evaluator runs first, without the launch options, then the game through launch.sh
            else:
                self.log_running.discard(appid)
                self._log_removed = True
                if p.pop("at", None) is not None:
                    p["gone"] = now
        if kind == "added" and p:
            self.emit("launch", {"id": k, "stage": "loading"})

    def _launch_line(self, line):
        """A launches.log line: GameSpark's FEX build missing (no game starts: an error, and our launch fails), its
        warnings (the game starts), and (e) a profiled game it says ran without its <appid>.env."""
        r = launch_record(line)
        if not r:
            return
        appid, applied, error, warns = r
        k = f"steam:{appid}"
        title = self._title_of(k)
        with self.lock:
            self._lines[appid] = time.monotonic()
            self._noconfig.pop(appid, None)
            ours = self.pending.pop(k, None) if error else None
        if error:
            self.emit("toast", {"text": f"Couldn't start {title}: {error}", "kind": "error"})
            if ours:
                self.emit("launch", {"id": k, "stage": "failed"})
        if warns:
            self.emit("toast", {"text": f"{title}: {'; '.join(warns)}", "kind": "warn"})
        if not (error or applied) and self._profiled(appid):
            self.emit("toast", {"text": f"{title} started without its GameSpark config", "kind": "warn"})

    def _no_config(self, appid):
        """(e) A profiled game Steam started without a launches.log line: GameSpark's launch options aren't set for it
        (fix: GameSpark tools/steam-config.sh launch-options profiled). Said once per launch."""
        with self.lock:
            if self._noconfig.pop(appid, None) is None:
                return
        self.emit("toast", {"text": f"{self._title_of(f'steam:{appid}')} started without its GameSpark config",
                            "kind": "warn"})

    # ---- window choreography (X11)

    def _watch(self):
        """Every 500 ms: windows → surface/running, launch and return choreography, exec exits; X hotkeys in between."""
        x, hot = None, {}
        if _xlib():
            try:
                x = X11()
                keys = self.settings.get("hotkeys") or {}   # a lone string is one key name, not its letters
                names = {n: act for act in ("home", "voice")
                         for n in ([keys[act]] if isinstance(keys.get(act), str) else keys.get(act) or [])}
                hot = {kc: names[n] for kc, n in x.grab(list(names)).items()}
                self.x_ok = True
            except OSError as e:
                log.warning("X11 unavailable (%s): no window choreography or hotkeys", e)
                x = None
        next_tick = 0.0
        while True:
            if x:
                self._x_events(x, hot)
                select.select([x.X.XConnectionNumber(x.d)], [], [], max(0.0, next_tick - time.monotonic()))
                self._x_events(x, hot)
            else:
                time.sleep(max(0.0, next_tick - time.monotonic()))
            if time.monotonic() >= next_tick:
                next_tick = time.monotonic() + 0.5
                try:
                    self._tick(x)
                except Exception as e:
                    warn_once(f"tick:{type(e).__name__}:{e}", "window watch: %s", e)

    def _x_events(self, x, hot):
        X, ev = x.X, XEvent()
        while X.XPending(x.d):
            X.XNextEvent(x.d, ctypes.byref(ev))
            kc = ev.xkey.keycode
            if ev.type not in (2, 3) or kc not in hot:
                continue
            press = ev.type == 2
            if not press and X.XPending(x.d):    # auto-repeat = Release + Press with the same timestamp
                nxt = XEvent()
                X.XPeekEvent(x.d, ctypes.byref(nxt))
                if nxt.type == 2 and nxt.xkey.keycode == kc and nxt.xkey.time == ev.xkey.time:
                    X.XNextEvent(x.d, ctypes.byref(nxt))
                    continue
            if press == (kc in self._keys_down):
                continue
            (self._keys_down.add if press else self._keys_down.discard)(kc)
            if hot[kc] == "home" and press:      # X server time (ms): two presses within the window open the switcher
                if not self.wake("key"):         # (a press that wakes the screen does nothing else)
                    _bg(self.press, "key", ev.xkey.time / 1000)
            elif hot[kc] == "voice" and self.on_voice:
                self.wake("key", press=False)    # words wake it and act
                self.on_voice(press)             # here, in order: a quick release must not overtake its press

    def _is_kiosk(self, w):
        inst, cls, pid, *_ = self._wins[w]
        return "sparkhome" in (inst.lower(), cls.lower()) or bool(self.chrome and pid == self.chrome.pid)

    def _tile_for_window(self, x, w, apps):
        inst, cls, pid, *_ = self._wins[w]
        for c in (cls, inst):
            if c.startswith("steam_app_") and c[10:].isdigit():
                return "steam:" + c[10:]         # Proton/Steam games, whoever started them
        t, p = self._procs() if time.monotonic() - self._ptab[0] < 5 else {}, pid   # (scanned while a game runs)
        for _ in range(64):                      # a native one: its process runs under that game's Steam reaper (as
            if p not in t:                       # gamescope finds it). Bounded: a stale table may hold a pid loop
                break
            p, _, argv = t[p]
            if _exe(argv) == b"reaper" and b"SteamLaunch" in argv:
                return next(("steam:" + a[6:].decode() for a in argv if a[:6] == b"AppId=" and a[6:].isdigit()), None)
        if cls.lower() == "steam" and self._bpm and "big picture" in x.name(w).lower():
            return self._bpm
        for tid, a in apps.items():
            if a["win"] == w or (a["proc"] and pid == a["proc"].pid):
                return tid
        return next((k for n in (inst.lower(), cls.lower()) for c, k in self._classes.items()
                     if fnmatch.fnmatchcase(n, c)), None)

    def _window_groups(self, x, stack, kiosk, apps):
        """Property reads only (no activation), so it can run against a live :0. stack: client windows, top-most first.
        → ({tile: its top-most window}, {card key: its plain windows, top-most first}). A tile's plain windows are its
        card (a game and its launcher are one); any other plain window is an x:<WM_CLASS> card, one per application,
        that joins an exec tile's card when it has that tile's class (its second window). Transients, docks, the
        desktop's icons and the kiosk get no card."""
        wins, groups = {}, {}
        for w in stack:
            if w in kiosk or w not in self._wins:
                continue
            tid = self._tile_for_window(x, w, apps)
            if tid:
                wins.setdefault(tid, w)          # running, as before
            if self._wins[w][3]:
                groups.setdefault(tid or "x:" + (self._wins[w][1] or self._wins[w][0] or "window").lower(), []).append(w)
        join = {self._wins[ws[0]][1].lower(): k for k, ws in groups.items() if k[:2] != "x:" and k != self._bpm}
        for k in [k for k in groups if k[:2] == "x:" and k[2:] in join]:   # (still top-most first)
            groups[join[k[2:]]] = sorted(groups[join[k[2:]]] + groups.pop(k), key=stack.index)
        wins.update({k: ws[0] for k, ws in groups.items() if k[:2] != "x:"})   # resume and raise: the top-most one
        return wins, groups

    def _hold(self, x, k, entry, p, now, g, stack, kwin):
        """One tick of game launch k (LAUNCH-DESIGN.md §1.3; entry: its pending dict, p: _tick's copy) → "hold" (the
        kiosk stays pinned over it), a Steam prompt's window (raised, over the kiosk: Steam waits on it), "go" (it drew:
        hand over now) or None (it failed, or ended behind a later launch). g: the game's top-most plain window (a
        splash or a transient never counts). Steam's windows come over the splash only if Steam is stuck: PROMPT_S after
        it took the launch, no process of this launch yet (ever: one that ended has EXIT_S; never Steam's log alone,
        which still lists a game whose 'removed' it missed) and no window, one titled as a launch prompt; NOPROC_S after,
        any."""
        if now > p["until"]:
            return self._fail(k, entry, "it didn't start within 5 minutes")
        if "at" not in p and now - p["gone"] >= EXIT_S and not g:
            return self._fail(k, entry, "it quit right after starting")
        if g:
            if p.get("win") != g:                # SETTLE_S and DRAW_S restart with each window: the one before may
                t0 = None if "seen" in p else self._noconfig.get(k[6:])   # have gone undrawn (a probe's, a stub's)
                self._put(k, entry, p, seen=now, win=g)
                if t0 is not None and now - t0 >= 2:   # (e) launch.sh writes its line before the game runs: none by its
                    self._no_config(k[6:])             # first window is none at all, said on the splash (by NOCONFIG_S
                    # the game would be up, in front of it)
            if now - p["seen"] < SETTLE_S:
                return "hold"
            t = time.monotonic()
            f = x.thumb(g, DRAWN)                # (the switcher's off-screen read: a covered window still composites)
            drawn, ms = f is not None and not blank(*f), (time.monotonic() - t) * 1000
            if not drawn and now - p["seen"] < DRAW_S:
                return "hold"
            t0 = p.get("t0", now)
            rel = lambda key: f"+{p[key] - t0:.1f} s" if key in p else "-"   # noqa: E731
            log.info("launch %s: Steam took it %s, process %s, window %s, %s +%.1f s (thumb %.0f ms)", k, rel("sent"),
                     rel("added"), rel("seen"), "drawn" if drawn else "no frame", now - t0, ms)   # (for tuning)
            s, se = self._splash or (None, {})   # a game launched, or chosen again, since (Home on this one), still
            if s not in (None, k) and (self.pending.get(s) is se and self._holding(s, se)   # loading or up: this one
                                       or "go" in se and (s in self._tile_wins or s[6:] in self.log_running)):
                with self.lock:                  # stays behind its loading screen or game, its launch done (one that
                    if self.pending.get(k) is entry:   # failed, or whose game quit, leaves this one to come up).
                        del self.pending[k]            # ponytail: a window it maps after that one's hand-over still
                return None                            # comes over that game (Proton focuses it, nothing is pinned
                # then): keep the pin for it if that bites
            if kwin:                             # unpinned first (the sync would, a tick late): an iconified pinned
                x.above(kwin, False)             # kiosk keeps focus from new windows, as the game's next one
            self._put(k, entry, p, go=now)
            self.emit("launch", {"id": k, "stage": "up"})
            _bg(self._handed_over)               # (a page that's busy holds it up to 1 s: never this thread)
            return "go"
        if "added" in p or "sent" not in p or now - p["sent"] < PROMPT_S:
            return "hold"
        late = now - p["sent"] >= NOPROC_S
        # Steam's windows carry no appid: one another launch raised is that launch's, and so is one that came after a
        # later launch began (its loading screen is the one up). ponytail: by time, so a prompt of this launch that
        # comes only after a later one began waits until that one is up or has failed (NOPROC_S may fail this first)
        others = [e for kk, e in list(self.pending.items()) if kk != k and self._holding(kk, e)]
        steam = [w for w in stack if self._wins[w][1].lower() == "steam" and not any(   # (top-most first)
            w == e.get("prompt") or e.get("t0", 0) > p.get("t0", 0) and w not in e.get("old", ()) for e in others)]
        hit = next((w for w in steam if PROMPT_RE.fullmatch(x.name(w))), steam[0] if late and steam else None)
        if hit is None:
            return self._fail(k, entry, "Steam started nothing in a minute") if late else "hold"
        if hit == p.get("prompt") and kwin in stack and stack.index(hit) > stack.index(kwin):
            return "hold"                        # covered (Home: left for later): it holds, pinned, till a re-press
        if hit != p.get("prompt"):
            if kwin:                             # unpinned first: mutter keeps relative stacking as it re-layers a
                x.above(kwin, False)             # window, so a prompt raised first would stay under the kiosk
            x.activate(hit)
            self._home_guard = self._steam_guard = 0.0   # Steam needs the user: no guard brings the kiosk back over it
            self._put(k, entry, p, prompt=hit)           # (a cold start's (b), as Steam's own windows pop up; Home's)
            self.emit("launch", {"id": k, "stage": "prompt"})
        return hit

    def _handed_over(self):
        """A launch handed over (_hold): the game takes the screen, so the web app in front pauses, as _open's does (one
        opened while it loaded: Home on its loading screen). active_tab is still that app's once the surface is the
        game's."""
        with self.lock:
            self._pause_front()

    def _put(self, k, entry, p, **kw):
        """Note what a tick found in launch k's entry (and in p, the tick's copy) if it is still that launch's."""
        p.update(kw)
        with self.lock:
            if self.pending.get(k) is entry:
                entry.update(kw)

    def _fail(self, k, entry, why):
        """Launch k ends (if entry is still its launch): the splash shrinks, a toast says why. → None."""
        with self.lock:
            if self.pending.get(k) is not entry:
                return None
            del self.pending[k]
        self.emit("toast", {"text": f"Couldn't start {self._title_of(k)}: {why}", "kind": "error"})
        self.emit("launch", {"id": k, "stage": "failed"})
        return None

    def _tick(self, x):
        now = time.monotonic()
        with self.lock:
            for k, a in self.apps.items():
                if a["proc"] and a["proc"].poll() is not None:
                    a["proc"] = None             # exited (and reaped). Its launch, if still waiting, has ended: no
                    p = self.pending.get(k)      # Starting… card (it died at start), and 5 s more for its window
                    if p:                        # (a client whose server maps it: gnome-terminal)
                        p.pop("at", None)
                        p["until"] = min(p["until"], now + 5)
            apps = {k: dict(a) for k, a in self.apps.items()}
            live, pending = dict(self.pending), {k: dict(p) for k, p in self.pending.items()}   # (live: the entries)
            procs = {k for k, a in apps.items() if a["proc"]}
            logged = {f"steam:{a}" for a in self.log_running}
        # The switcher's reaper checks (_procs) scan /proc while a Steam game runs or starts, and once at start (a
        # native game already running is known by its reaper only). 10-15 ms a scan [M]: never here, nor under the lock
        if not self._scanning and now - self._ptab[0] >= 1 and (not self._ptab[0] or any(
                k[:6] == "steam:" for k in self.running | logged | set(pending))):
            self._scanning = True
            _bg(self._scan)
        if not x:                                # no X (e.g. the Mac): processes, Steam's log and the page title only
            self._set(running=procs | logged, **({} if self.surface == "app" else
                                                 {"title": self._page_title(self._front()) or self.title}))
            return
        active, clients = x.active(), x.clients()
        for w in clients:
            if w not in self._wins:
                self._wins[w] = (*x.wm_class(w), x.pid(w), *x.traits(w))
        for w in set(self._wins) - set(clients):
            del self._wins[w]
        kiosk = [w for w in clients if self._is_kiosk(w)]
        kwin = active if active in kiosk else kiosk[0] if kiosk else None
        stack = [w for w in (x.prop(x.root, "_NET_CLIENT_LIST_STACKING") or clients)[::-1] if w in self._wins]
        wins, groups = self._window_groups(x, stack, kiosk, apps)   # (mutter: the stacking order is activation order)
        for w in clients:                        # a new window goes to an exec launch still waiting for one
            if w in self._seen or w in kiosk or w in wins.values():
                continue                         # (Flatpak PIDs live in their own namespace, so PIDs can't match)
            waiting = [k for k in pending if k in apps and k not in wins]
            if waiting:
                wins[waiting[-1]] = w
                with self.lock:
                    if waiting[-1] in self.apps:
                        self.apps[waiting[-1]]["win"] = w
            elif self._bpm in pending and self._wins[w][1].lower() == "steam":
                x.activate(w)                    # a Steam dialog while Big Picture starts (a sign-in): not behind us. A
                                                 # game's launch raises only Steam's prompts, and late (_hold)
        self._seen, self._kwin, self._tile_wins = set(clients), kwin, wins
        # can_close: the top-most window takes WM_DELETE (only it is asked: a launcher closed under its game may take
        # the game down outside the game's own quit)
        prev, self._groups, self._closable = self._groups, groups, {k for k, ws in groups.items() if self._wins[ws[0]][4]}
        running = set(wins) | procs | logged
        pin, asking, raised = False, set(), False   # a game's launch holds (the kiosk pinned over it), or asks (a
        for tid, p in pending.items():           # prompt); a window was raised. Launch: new window to the front, kiosk
            w = wins.get(tid)                    # iconified (Chrome keeps rendering when merely covered)
            if self._holding(tid, p):            # a game: only once it has drawn (_hold)
                r = self._hold(x, tid, live[tid], p, now, (groups.get(tid) or [None])[0], stack, kwin)
                pin |= r == "hold"
                if isinstance(r, int):
                    asking.add(r)
                if r != "go":
                    continue
            done = w and active == w and (not kwin or x.iconic(kwin))
            if done or now > p["until"] or p["tries"] >= 8:
                with self.lock:
                    self.pending.pop(tid, None)
            elif w:
                x.activate(w)
                raised = True
                if kwin:
                    x.iconify(kwin)
                with self.lock:
                    if tid in self.pending:
                        self.pending[tid]["tries"] += 1
        # The pin: the kiosk (the splash) is EWMH "above" while a game's launch holds and none asks, read back from X each
        # tick (a stale one, after a crash, goes at the next start). Never while iconified: mutter still counts an
        # iconified above window as covering, and new windows (a game's next one) wouldn't get focus. Pinned, yet under
        # a plain window (a game's non-fullscreen topmost one, also above): pushed back, as without the pin
        want, unpinned = pin and not asking and bool(kwin) and not x.iconic(kwin), False
        if kwin:
            if (x.atom("_NET_WM_STATE_ABOVE") in (x.prop(kwin, "_NET_WM_STATE") or [])) != want:
                x.above(kwin, want)
                unpinned = not want
            elif want and stack and stack[0] not in kiosk and self._wins[stack[0]][3]:
                warn_once("pin", "the pinned kiosk is under %s; pushing it back", self._wins[stack[0]][:2])
                x.activate(kwin)
                active = kwin
        # What shows has the focus (the keyboard, the FLIRC remote and the page's gamepad go by it): a window that took
        # it under the pin (a game's: Proton focuses it as it maps) gives it back, once (one that takes it again keeps
        # it: no ping-pong); and a pin that ends with nothing handed over or asking (a fail, a quit, a launch left
        # behind a later one) takes it back from the game under the launcher. ponytail: so a game loads unfocused, and
        # one that stops drawing (or minimises) unfocused comes up at DRAW_S ("no frame"); if that bites, leave it the
        # focus and give the pad's B a route through inputs.py instead
        if (kwin and active in self._wins and active not in kiosk and not raised
                and (want and active not in self._refocused
                     or unpinned and not asking and stack and stack[0] in kiosk and not x.iconic(kwin))):
            self._refocused.add(active)
            x.activate(kwin)
            active = kwin
        self._refocused = self._refocused if want else set()   # (once per window while pinned)
        # Return: the last app gone while the kiosk is hidden or nothing has focus. A launch that holds counts as neither
        # an app nor a launch waiting for its window: it wants the launcher in front, pinned again (a game played
        # meanwhile, quit from its own menu, leaves its loading screen, not the desktop and its first, black window).
        # Not while one asks: the prompt Steam waits on stays on top, and the return waits for its answer
        held = {k for k, p in pending.items() if self._holding(k, p)}
        rest = running - held
        if ((self._prev_running and not rest) or self._log_removed) and set(pending) <= held and not asking:
            self._log_removed = False
            if kwin and not rest and (x.iconic(kwin) or active not in clients):
                x.activate(kwin)
                active = kwin
        self._prev_running = self._prev_running if asking else rest
        if (kwin and active in self._wins and active != kwin and active not in asking and self._bpm not in pending
                and self._wins[active][1].lower() == "steam" and not x.iconic(kwin)
                and (want or now < self._home_guard
                     or now < self._steam_guard and "big picture" not in x.name(active).lower())):
            x.activate(kwin)                     # Steam answering the Guide press that was our Home, its main window
            active = kwin                        # popping up after we started it, or any Steam window taking focus under a
            # game's splash (input to what can't be seen). Never over a game (the kiosk iconified), nor a prompt raised.
            # ponytail: up to one 500 ms tick of Big Picture shows first; watch _NET_ACTIVE_WINDOW events if that bites
        akey = None if active not in self._wins or active in kiosk else \
            next((k for k, ws in groups.items() if active in ws), None) or \
            next((k for k, w in wins.items() if w == active), None) or self._tile_for_window(x, active, apps)
        with self.lock:                          # the app switcher's closes (close_app adds them under the lock too)
            closing, told = dict(self._closing), []
            for k in set(prev) - set(groups) - set(closing):  # lost its last window, nobody asked: it may be saving
                if k in running and not self._ghost(k, 0):
                    closing[k] = {"at": now, "auto": True}
            for k, e in list(closing.items()):
                ws = groups.get(k, [])
                if not ws and k == self._active_key and not e.get("auto") and kwin and active != kwin:
                    x.activate(kwin)             # it quit in front: the launcher, not Big Picture under it
                    active, akey = kwin, None
                if not ws and (k not in running or self._ghost(k, 0)):
                    del closing[k]               # it exited
                elif ws and e.get("auto"):
                    del closing[k]               # a window came back (a launcher handing over to its game)
                elif "w" in e and e["w"] not in ws:
                    # The asked window went, however long after the ask (a save past stuck_s, its prompt answered
                    # later): it took the quit, and its shutdown is timed from here, as D24's (Quitting…: it may be
                    # saving; never "didn't quit", nor dropped for its time in front: an in-game "quit the game", Wine
                    # raising it, its own prompt answered). The next window, top-most first, is asked once that process
                    # exited too (a launcher closed under a game still quitting takes it down; the tick it exits in
                    # doesn't matter) or is that process's own, while that shutdown is fresh, as a new ask; else (past
                    # stuck_s, a pid we can't see) that close is done
                    e = closing[k] = e if e.get("gone") else {**e, "gone": True, "at": now}
                    if ws and (not e.get("pid") or not _ours(e["pid"]) or self._wins[ws[0]][2] == e["pid"]):
                        if e.get("pid") and now - e["at"] < self.stuck_s:
                            closing[k] = {"w": ws[0], "pid": self._pid(ws[0]),
                                          "at": now if x.delete(ws[0]) else now - self.stuck_s}
                        else:
                            del closing[k]
                elif akey == k:                  # in front: its prompt is being answered, or it's in use again
                    closing[k] = e = {**e, "front": e.get("front", now)}
                    if now - e["front"] >= self.stuck_s:
                        del closing[k]
                elif now - e["at"] >= self.stuck_s and not (e.get("auto") or e.get("force") or e.get("told")):
                    closing[k] = {**e, "told": True}
                    told.append(k)
            self._closing = closing
            self._snaps = {k: v for k, v in self._snaps.items() if k in running or k in groups}   # gone: dropped
        for k in told:                           # (force only where it can be: never Big Picture, Steam or a window)
            self.emit("toast", {"text": f"{self._title_of(k)} didn't quit. Open it to see why"
                                        f"{', or force quit it' if self._can_force(k, self._kind(k))[0] else ''}.",
                                "kind": "info"})
        # Nothing focused (a keyring prompt gone, a game just exited): the kiosk on top is still the launcher, so Siri
        # moves aren't dropped. A home()/open() that ended after this tick began, or under 0.25 s before, wins it: its
        # X reads may predate mutter applying that action's activation (asynchronous: a roundtrip for timestamp 0).
        # Pinned, the kiosk is what's on screen whatever has focus: a Proton game takes focus as it maps under it
        shown, key = kwin if want else active if active in clients else stack[0] if stack else None, akey
        if shown in kiosk:
            tab = self.active_tab
            title = self._page_title(self._front()) or "SparkHome"
            with self.lock:
                if tab == self.active_tab and self._acted_at <= now - .25:
                    self._active_key = None
                    if self._launcher_due and not tab:   # its page closed while a game was in front (_tab_gone)
                        self._launcher_due = False
                        try:
                            self._show(self.launcher)
                        except (OSError, RuntimeError) as e:
                            log.warning("launcher page: %s", e)
                    self._set(surface="web" if tab else "launcher", active=tab, title=title, running=running)
        elif active in clients:                  # (an x: key is an untiled window: no tile, but a card)
            title = x.name(active)
            with self.lock:
                if self._acted_at <= now - .25:
                    self._active_key = key
                    if key and key[:2] == "x:":
                        self._stamp(key)
                    self._set(surface="app", active=key if key and key[:2] != "x:" else None, title=title,
                              running=running)
        else:
            with self.lock:
                if self._acted_at <= now - .25:
                    self._active_key = None
                    self._set(surface="app" if wins else "launcher", active=None, title="", running=running)
        # The switcher's snapshot of an app, once per leave: the tick after the kiosk came in front (0.5-1 s after
        # Home: past the switcher's entrance), if the kiosk is still in front and the window still there. Never while
        # an app is in front, never periodic.
        if shown not in kiosk:
            self._snap_due = None
        elif self._last_app:
            self._snap_due = (*self._last_app, now + .4)
        elif self._snap_due and now >= self._snap_due[2]:
            k, w, _ = self._snap_due
            self._snap_due, f = None, x.thumb(w) if w in self._wins and k in groups else None
            if f and not blank(*f):
                self._snap_put(k, png(f[0], f[1], rows=rgb_rows(*f)))
        self._last_app = (akey, (groups.get(akey) or [active])[0]) if akey and shown not in kiosk else None
        self._apps_tick()


def _mark_clean_exit(profile):
    """No "Restore pages?" bubble on the TV after a crash or power cut."""
    p = os.path.join(profile, "Default", "Preferences")
    try:
        prefs = json.loads(Path(p).read_text())
        pr = prefs.setdefault("profile", {})
        if pr.get("exit_type") != "Normal" or pr.get("exited_cleanly") is not True:
            pr.update(exit_type="Normal", exited_cleanly=True)
            Path(p + ".sh").write_text(json.dumps(prefs))
            os.replace(p + ".sh", p)
    except (OSError, ValueError, AttributeError):
        pass


# ---------------------------------------------------------------- selftest

def _selftest_terminate(tmp):
    """Linux: force quit's signalling on a real dummy tree of our own, never anything else. A fake reaper (argv[0]
    .../reaper, but FakeLaunch: no Steam process matches it, nor it Steam's) starts a plain child, one that ignores
    SIGTERM and one that setsid()s and forks a grandchild (as wineserver does). Everything carries a marker; nothing is
    signalled unless the tree is exactly ours."""
    mark = f"sparkhome-selftest-{os.getpid()}-{time.monotonic_ns()}".encode()
    child = ("import os, signal, sys, time\nif sys.argv[1] == 'stubborn': signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
             "if sys.argv[1] == 'session':\n    os.setsid()\n    if os.fork() == 0:\n"
             "        os.execv(sys.executable, [sys.executable, '-c', 'import time; time.sleep(60)', sys.argv[2]])\n"
             "time.sleep(60)\n")
    fake = ("import subprocess, sys\nfor k in [subprocess.Popen([sys.argv[3], '-c', sys.argv[1], k, sys.argv[2]])\n"
            "          for k in ('plain', 'stubborn', 'session')]:\n    k.wait()\n")
    os.symlink(sys.executable, tmp / "reaper")                  # (its argv[0] is then .../reaper, as Steam's is)
    p = subprocess.Popen([str(tmp / "reaper"), "-c", fake, child, mark.decode(), sys.executable, "FakeLaunch",
                          "AppId=99999"])
    ours = lambda: {q: a for q, (_, _, a) in proc_table().items() if mark in a}
    try:
        end = time.monotonic() + 10
        while len(ours()) < 5 and time.monotonic() < end:      # the reaper, 3 children, a grandchild
            time.sleep(.05)
        table = proc_table()
        assert reapers(table, "99999", b"FakeLaunch") == [p.pid] and table[p.pid][0] == os.getpid()
        assert reapers(table, "99999") == []                   # Steam's token never matches it
        kids = descendants(table, p.pid)
        assert len(kids) == 4 and all(mark in table[k][2] for k in kids), kids
        exited, killed = terminate(kids, table, grace=1.0)
        assert len(exited) == 3 and [b"stubborn" in table[k][2] for k in killed] == [True], (exited, killed)
        assert p.wait(5) == 0                                   # the reaper, spared, exits once its tree is gone
        time.sleep(.1)
        assert not ours(), ours()
    finally:
        for q in ours():                                        # by exact pid: only ours (the marker) remain
            os.kill(q, signal.SIGKILL)
        if p.poll() is None:
            p.kill()
            p.wait()


def _selftest():
    tmp = tempfile.mkdtemp(prefix="sparkhome-test-")
    try:
        _selftest_in(Path(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _selftest_in(tmp):
    # config precedence: --config > user file (replaces, no merge) > repo default; ~ expansion; the nav check
    repo = load_config(str(REPO / "sparkhome.json"))
    user = tmp / "user.json"
    assert load_config(None, user) == repo
    user.write_text(json.dumps({**repo, "settings": {"port": 1}}))
    assert load_config(None, user)["settings"] == {"port": 1} and load_config(str(REPO / "sparkhome.json"), user) == repo
    user.write_text(json.dumps({**repo, "tiles": {"x": {"nav": "mouse"}}}))
    try:
        load_config(None, user)
        raise AssertionError("an unknown nav must fail the config check")
    except ValueError as e:
        assert "'mouse'" in str(e), e
    user.write_text(json.dumps({**repo, "tiles": {"x": {"skin": "yes"}}}))
    try:
        load_config(None, user)
        raise AssertionError("a skin that is neither true/false nor an object must fail the config check")
    except ValueError as e:
        assert "'yes'" in str(e), e
    assert expand({"a": ["~/x", "y"], "b": "~"})["a"][0] == os.path.expanduser("~/x")
    # every exec tile's windows are known by their WM_CLASS (after a restart, or started elsewhere): an app's card, which
    # agents can't quit, never a plain window's; a list names an app's several (Moonlight streams in SDL's own window)
    rs = Shell(repo, True, lambda *_: None)
    assert {k for k, t in rs.tile_defs.items() if t["type"] == "exec"} <= set(rs._classes.values()), rs._classes
    assert rs._classes["moonlight"] == rs._classes["com.moonlight_stream.moonlight"] == "moonlight", rs._classes
    t = {i: rs.tile(i) for i in ("netflix", "jellyfin", "terminal", "steam:1091500")}   # ...and its tiles say what the
    assert t["netflix"]["search"] and t["netflix"]["shows"] and t["jellyfin"]["shows"] and t["terminal"]["game"] is \
        False and t["steam:1091500"]["profile"].startswith("DLSS"), t   # agent reads (Agent._show, _game) and the badge
    # the rest runs on a pinned config, not the repo default: users may edit that one (it must still load, above)
    web = {k: {"type": "web", "title": v, "url": f"https://{k}.example/"} for k, v in (
        ("youtube", "YouTube"), ("netflix", "Netflix"), ("disney", "Disney+"), ("max", "HBO Max"),
        ("prime", "Prime Video"), ("twitch", "Twitch"))}
    web["youtube"].update(search="https://youtube.example/#/search?q={q}", nav="keys")
    base = {"settings": {"max_web_tabs": 4}, "tiles": {
                **web, "bigpicture": {"type": "steam", "title": "Steam Big Picture", "uri": "steam://open/bigpicture"},
                "retroarch": {"type": "exec", "title": "RetroArch", "argv": ["flatpak", "run", "org.libretro.RetroArch"]},
                "xcloud": {"type": "web", "title": "Xbox Cloud Gaming", "url": "https://xbox.example/play", "nav": "pad"},
                "settings": {"type": "widget", "widget": "settings", "title": "Settings"}},
            "rows": [{"id": "top", "style": "dock", "tiles": ["youtube", "netflix", "steam:1091500", "bigpicture"]},
                     {"id": "streaming", "tiles": list(web)},
                     {"id": "games", "style": "posters", "auto": "steam", "tiles": ["bigpicture", "xcloud", "retroarch"]},
                     {"id": "system", "style": "widgets", "tiles": ["settings"]}]}
    # VDF / ACF
    v = vdf('"AppState"\n{\n\t"appid"\t\t"42"\n\t"name"\t"Say \\"hi\\""\n\t"UserConfig"\n\t{\n\t\t"language"\t"english"\n\t}\n'
            '\t"LastPlayed"\t"7"\n}\n')
    assert v == {"AppState": {"appid": "42", "name": 'Say "hi"', "UserConfig": {"language": "english"}, "LastPlayed": "7"}}
    # a fake Steam: four games (newest first), a tool, two damaged manifests, both art layouts, a library folder elsewhere
    steam, lib2 = tmp / "Steam", tmp / "lib2"
    for root, appid, name, last in ((steam, "1091500", "Cyberpunk 2077", 300), (steam, "391220", "Rise of the Tomb Raider", 100),
                                    (steam, "1493710", "Proton Experimental", 999), (lib2, "435150", "Divinity: Original Sin 2", 200),
                                    (steam, "292030", "The Witcher 3: Wild Hunt", 50)):
        (root / "steamapps").mkdir(parents=True, exist_ok=True)
        (root / "steamapps" / f"appmanifest_{appid}.acf").write_text(
            f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"{name}"\n\t"LastPlayed"\t\t"{last}"\n}}\n')
    (steam / "steamapps" / "appmanifest_1.acf").write_text('"AppState" { "appid" "1" "name" { "x" "y" } }')
    (steam / "steamapps" / "appmanifest_2.acf").write_text('"AppState" { "appid" "2" "LastPlayed" "7" { "a" "b" } }')
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n\t"0"\n\t{{\n\t\t"path"\t\t"{steam}"\n\t}}\n\t"1"\n\t{{\n\t\t"path"\t\t"{lib2}"\n\t}}\n}}\n')
    cache = steam / "appcache" / "librarycache"
    for rel in ("1091500/cf8c/library_hero.jpg", "1091500/cf8c/library_hero_blur.jpg", "1091500/3768/logo.png",
                "1091500/6399/library_capsule.jpg", "1091500/812a/library_header.jpg", "391220/library_hero.jpg",
                "391220/logo.png", "391220/library_600x900.jpg", "391220/header.jpg"):
        (cache / rel).parent.mkdir(parents=True, exist_ok=True)
        (cache / rel).write_bytes(b"x")
    assert find_art(str(steam), "1091500", "hero").endswith("cf8c/library_hero.jpg")
    assert find_art(str(steam), "1091500", "capsule").endswith("library_capsule.jpg")
    assert find_art(str(steam), "391220", "capsule").endswith("391220/library_600x900.jpg")
    assert find_art(str(steam), "391220", "header").endswith("391220/header.jpg")
    assert find_art(str(steam), "435150", "logo") is None
    # local services: one listening, one closed
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    up = srv.getsockname()[1]
    s2 = socket.socket()
    s2.bind(("127.0.0.1", 0))
    down = s2.getsockname()[1]
    s2.close()
    cfg = json.loads(json.dumps(base))
    cfg["settings"].update(steam_root=str(steam), gamespark=str(tmp / "nope"))
    cfg["tiles"].update(up={"type": "web", "title": "Up", "url": f"http://127.0.0.1:{up}/", "nav": "keys"},
                        down={"type": "web", "title": "Down", "url": f"http://localhost:{down}/"},
                        broken={"type": "web", "title": "No URL"},
                        py={"type": "exec", "title": "Py", "argv": [sys.executable]},
                        nobin={"type": "exec", "title": "Nobin", "argv": ["no-such-binary-sparkhome"]},
                        nopak={"type": "exec", "title": "Nopak", "argv": ["flatpak", "run", "org.example.NoSuchApp"]})
    cfg["rows"].append({"id": "test", "title": "Test", "tiles": ["up", "down", "broken", "nonexistent", "py", "nobin", "nopak"]})
    prof = tmp / "nope" / "profiles" / "launch"  # GameSpark's launch profiles: a "# summary:" line by its prefix, on
    prof.mkdir(parents=True)                     # any line, or none; an Engine.ini alone is no profile
    (prof / "1091500.env").write_text("# Cyberpunk 2077\n# summary: DLSS and frame generation on, DLSS libraries kept "
                                      "current\nPROTON_ENABLE_NVAPI=1\n")
    (prof / "292030.env").write_text("# The Witcher 3\n# DLSS needs NVAPI\n# summary: Skips the launcher \nEXTRA_ARGS=x\n")
    (prof / "435150.env").write_text("SWAP_FROM=/bin/SupportTool.exe\n")
    (prof / "4078430.Engine.ini").write_text("[SystemSettings]\n")
    events = []
    sh = Shell(cfg, False, lambda t, d: events.append((t, d)))
    srv.close()
    assert sh._tile_for_url(f"http://127.0.0.1:{up}/x")["id"] == "up"   # a page a previous daemon left, by host and port:
    assert sh._tile_for_url(f"http://127.0.0.1:{sh.port}/") is None    # never our own page as a tile's (same host)
    assert sh.tile("steam:1091500")["profile"] == "DLSS and frame generation on, DLSS libraries kept current"
    assert sh.tile("steam:292030")["profile"] == "Skips the launcher" and sh.tile("steam:435150")["profile"] == ""
    assert "profile" not in sh.tile("steam:391220") and profile_summary(str(tmp / "nope"), "4078430") is None
    assert profile_summary("", "1091500") is None   # no GameSpark set: never the daemon's working directory
    # GameSpark's launches.log, real Spark lines (2026-10-10) and launch.sh's other shapes: the profile applied when its
    # <appid>.env is listed (not run.env, nor in a suffix); "not launched" is the missing FEX build; each WARNING
    L = lambda s: launch_record("2026-10-10 02:14:51 app " + s)   # noqa: E731
    fex = "/home/spark/.local/share/gamespark/fex/current/bin/FEX"
    assert L("391220 applied: none") == ("391220", False, None, [])
    assert L("435150 applied: 435150.env run.env (exe -> /DefEd/bin/EoCApp.exe)") == ("435150", True, None, [])
    assert L("292030 applied: 292030.env run.env (in container: VK_IMPLICIT_LAYER_PATH=/home/spark/.local/share/gamespark/"
             "vklayers:/usr/lib/pressure-vessel/overrides/share/vulkan/implicit_layer.d) (args: --launcher-skip)")[1]
    assert L(f"4078430 applied: 4078430.env run.env (FEX: {fex})") == ("4078430", True, None, [])
    assert L("1091500 applied: run.env")[1] is False and L("292030 applied: none (args: 292030.env)")[1] is False
    ini = "4078430.Engine.ini not applied (needs UE_PROJECT and Steam's STEAM_COMPAT_DATA_PATH)"
    assert L(f"4078430 applied: 4078430.env run.env (FEX: {fex} (missing)) not launched: run system/fex-patched.sh "
             f"install WARNING: {ini}") == ("4078430", True, "GameSpark's FEX build is missing", [ini])
    chg = "the system FEX package changed since the patched build was installed; run system/fex-patched.sh install"
    assert L(f"4078430 applied: 4078430.env run.env 4078430.Engine.ini (FEX: {fex}) WARNING: {chg} WARNING: {ini}") == \
        ("4078430", True, None, [chg, ini])
    assert launch_record("garbage") is None and launch_record("") is None
    # Steam's launch prompts by title (its English UI); never its own windows
    assert all(PROMPT_RE.fullmatch(t) for t in ("EULA", "Install", "Cloud Conflict", "Launching...", "Error",
                                                "Error - Steam", "Launch Cyberpunk 2077", "Steam - Setup failed"))
    assert not any(PROMPT_RE.fullmatch(t) for t in ("Steam", "Steam Big Picture Mode", "Friends List", "Steam Settings",
                                                    "Launching", "Cyberpunk 2077"))
    rows = {r["id"]: [t["id"] for t in r["tiles"]] for r in sh.tiles()["rows"]}
    assert rows["games"][:3] == ["steam:1091500", "steam:435150", "steam:391220"], rows["games"]
    assert "steam:1493710" not in rows["games"] and "bigpicture" in rows["games"] and "steam:292030" in rows["games"]
    assert rows["top"] == ["youtube", "netflix", "steam:1091500", "bigpicture"]
    assert rows["test"] == ["up", "py"], rows["test"]       # closed port, missing binary/flatpak, bad tile: hidden
    g = sh.tile("steam:1091500")
    assert g["art"] == {k: f"/art/steam/1091500/{k}" for k in ART} and g["running"] is False
    assert set(sh.tile("steam:391220")["art"]) == set(ART) and sh.tile("steam:435150").get("art") == {}
    assert sh.art_file("1091500", "logo").endswith("logo.png") and sh.art_file("../x", "logo") is None
    # find: id → title → unique substring → fuzzy ('witcher' is closer to 'Twitch' by edit distance)
    assert sh.find("netflix")["id"] == "netflix" and sh.find("PRIME VIDEO")["id"] == "prime"
    assert sh.find("cyberpunk")["id"] == "steam:1091500" and sh.find("tomb raider")["id"] == "steam:391220"
    assert sh.find("divinity")["id"] == "steam:435150" and sh.find("zzzz") is None and sh.find("") is None
    assert sh.find("witcher")["id"] == "steam:292030" and sh.find("twitch")["id"] == "twitch"
    assert sh.find("netflx")["id"] == "netflix"
    assert "Netflix" in sh.titles()
    # Steam log lines and the pipe fallback
    text = ("[2026-10-08 00:06:01] Game process added : AppID 435150 \"/home/spark/...reaper SteamLaunch AppId=435150\"\n"
            "[2026-10-08 00:07:00] Game process updated : AppID 435150 ...\n"
            "[2026-10-08 00:09:12] Game process removed: AppID 435150 \"...\"\n")
    assert steam_log_events(text) == [("added", "435150"), ("removed", "435150")]
    assert pipe_write(str(tmp / "no.pipe"), "x\n", 0.2)[0] is False
    assert pointer_buttons("left", 0, 2) == [5, 5, 1] and pointer_buttons(None, -1, -2) == [4, 4, 6]
    assert pointer_buttons("right", 3, 0) == [7, 7, 7, 3] and pointer_buttons(None, 0, 0) == []
    assert sh.pointer(dx=5)["ok"] is False                       # no X display in the selftest
    # Home guard: Big Picture raised by Steam right after our Home goes back behind the kiosk; chosen later, it stays

    class FakeX:                                 # a window manager: its windows, stacking order (bottom → top) and focus
        root = 0
        W = {1: ("sparkhome", "SparkHome", "SparkHome", True, True),                      # (inst, class, name, plain,
             2: ("steamwebhelper", "steam", "Steam Big Picture Mode", True, True),        # WM_DELETE_WINDOW)
             3: ("gjs", "Gjs", "Desktop Icons", False, True),                             # type DESKTOP, skip-taskbar
             4: ("steam_app_435150", "steam_app_435150", "Larian Launcher", True, True),
             5: ("steam_app_435150", "steam_app_435150", "Divinity: Original Sin 2", True, True),
             6: ("steam_app_435150", "steam_app_435150", "Save?", False, True),           # transient for 5
             7: ("steamwebhelper", "steam", "Steam", True, True),
             8: ("org.gnome.Nautilus", "org.gnome.Nautilus", "Home", True, True),
             9: ("xterm", "XTerm", "xterm", True, False),                                 # no WM_DELETE_WINDOW
             10: ("py", "Py", "Py", True, True), 11: ("py", "Py", "Py: settings", True, True),   # an exec tile's two
             12: ("steam_app_1174180", "steam_app_1174180", "Wine Desktop", True, True),  # RDR2 in a Wine desktop
             13: ("org.gnome.Nautilus", "org.gnome.Nautilus", "Downloads", True, True),
             14: ("RiseOfTheTombRaider", "RiseOfTheTombRaider", "Rise of the Tomb Raider", True, True),   # native
             15: ("Minecraft* 1.21.1", "Minecraft* 1.21.1", "Minecraft* 1.21.1", True, True),   # GLFW, no class
             16: ("steamwebhelper", "steam", "Launching...", True, True),                 # Steam's launch prompts
             17: ("steamwebhelper", "steam", "EULA", True, True),
             18: ("steam_app_1091500", "steam_app_1091500", "Cyberpunk 2077", True, True),
             19: ("steam_app_1091500", "steam_app_1091500", "Cyberpunk 2077", True, True)}   # (its window, recreated)

        def __init__(self, stack):
            self.stack, self.front, self.calls, self.hook, self.deleted = list(stack), stack[-1], [], None, []
            self.thumbs, self.frame = [], bytes((i * 37) % 251 for i in range(256 * 36))   # (a 64x36 BGRX frame)
            self.P = {}                          # window → _NET_WM_PID
            self.pinned, self.log = set(), []    # _NET_WM_STATE_ABOVE; every above/activate/iconify, in order
            self.icons = set()                   # iconified (mutter: activating a window de-iconifies it)

        def delete(self, w):
            self.deleted.append(w)
            return self.deletable(w)

        def thumb(self, w, box=SNAP):
            self.thumbs.append(w)
            return 64, 36, 256, self.frame
        active = lambda self: self.front
        atom = lambda self, name: name
        prop = lambda self, w, name: self.stack[:] if (w, name) == (0, "_NET_CLIENT_LIST_STACKING") else \
            ["WM_DELETE_WINDOW"] if name == "WM_PROTOCOLS" and self.W[w][4] else \
            ["_NET_WM_STATE_ABOVE"] if name == "_NET_WM_STATE" and w in self.pinned else None
        deletable = X11.deletable                # (the real rule, over these windows' protocols and names)
        wm_class = lambda self, w: self.W[w][:2]
        name = lambda self, w: self.W[w][2]
        traits = lambda self, w: (self.W[w][3], self.deletable(w))
        pid = lambda self, w: self.P.get(w, 0)
        iconic = lambda self, w: w in self.icons
        __enter__ = lambda self: self
        __exit__ = lambda self, *a: None

        def clients(self):
            hook, self.hook = self.hook, None
            if hook:
                hook()                           # an action that finishes while a tick reads X
            return sorted(self.stack)

        def activate(self, w):
            self.calls.append(w)
            self.log.append(("activate", w))
            self.front, self.stack = w, [v for v in self.stack if v != w] + [w]
            self.icons.discard(w)

        def iconify(self, w):
            self.calls.append(-w)
            self.log.append(("iconify", w))
            self.icons.add(w)

        def above(self, w, on):                  # (mutter: the pinned kiosk stays on top while it is above)
            self.log.append(("above", w, on))
            (self.pinned.add if on else self.pinned.discard)(w)
    fx = FakeX((2, 1))                           # Big Picture behind the kiosk
    sh._tick(fx)
    sh.home()
    fx.front = 2
    sh._tick(fx)
    assert fx.calls == [1] and sh.state()["surface"] == "launcher", (fx.calls, sh.state())
    sh._home_guard, fx.front, fx.calls = 0.0, 2, []
    time.sleep(.26)                              # (a tick 0.25 s after an action: mutter has applied it; see _tick)
    sh._tick(fx)
    assert fx.calls == [] and sh.state()["active"] == "bigpicture", (fx.calls, sh.state())
    # the app switcher's X model: one card per app from the stacking order, top-most first; a game and its launcher are
    # one card, resumed on its top-most window; transients, the desktop's icons and the kiosk get none; any other window
    # is an x:<class> card named from its class; Big Picture and Steam's window are "system"
    fx = FakeX((3, 1, 2, 7, 8, 9, 4, 5, 6))
    fx.front = 5
    sh._tick(fx)
    assert sh._groups == {"steam:435150": [5, 4], "x:xterm": [9], "x:org.gnome.nautilus": [8], "x:steam": [7],
                          "bigpicture": [2]}, sh._groups
    assert sh._tile_wins["steam:435150"] == 5 and sh._closable == {"steam:435150", "x:org.gnome.nautilus", "x:steam",
                                                                    "bigpicture"}   # xterm lists no WM_DELETE_WINDOW
    cards = {c["key"]: c for c in sh.running_apps()}
    assert list(cards)[0] == "steam:435150" and sh.state()["active"] == "steam:435150", list(cards)
    assert (cards["steam:435150"]["title"], cards["steam:435150"]["windows"]) == ("Divinity: Original Sin 2", 2)
    assert [(cards[k]["title"], cards[k]["kind"]) for k in ("x:org.gnome.nautilus", "x:steam", "bigpicture")] == \
        [("Nautilus", "window"), ("Steam", "system"), ("Steam Big Picture", "system")]
    assert [sh._title_of("x:" + c) for c in ("minecraft* 1.21.1", "hollow_knight.x86_64", "battle.net.exe",
                                             "com.moonlight_stream.moonlight")] == \
        ["Minecraft* 1.21.1", "Hollow Knight", "Battle.Net", "Moonlight"]   # (a version, a file name: its name whole)
    assert not cards["x:xterm"]["can_close"] and cards["x:steam"]["can_close"] and not cards["x:steam"]["can_force"]
    # a native Steam game (no steam_app_ class): its window is the game's card, by its process under that game's
    # reaper. Not a "window" card an agent may quit, nor a second, windowless "Running without a window" one
    fx.stack, fx.P[14] = fx.stack + [14], 79
    sh.log_running, sh._ptab = {"391220"}, (9e9, {78: (1, 1, [b"/s/ubuntu12_32/reaper", b"SteamLaunch",
                                                               b"AppId=391220", b"--", b"x"]), 79: (78, 2, [b"./RotTR"])})
    sh._tick(fx)
    c = [(c["key"], c["kind"], c["windows"]) for c in sh.running_apps() if c["key"][-6:] in ("391220", "raider")]
    assert c == [("steam:391220", "game", 1)] and sh._groups["steam:391220"] == [14], (c, sh._groups)
    assert sh.close_app("steam:391220")["error"].startswith("Games and apps quit only")
    fx.stack, sh.log_running, sh._ptab = fx.stack[:-1], set(), (0.0, {})
    # Minecraft from the repo's Prism tile: GLFW names its window after its first title (Mojang sets no class) and its
    # pid is the Flatpak sandbox's, so only its class pattern ties it: the Minecraft card, an app agents can't quit, not
    # a "window" card titled "1" that they may
    rs._tick(FakeX((1, 15)))
    c = [(c["key"], c["kind"], c["title"]) for c in rs.running_apps()]
    assert c == [("prism", "app", "Minecraft")] and rs.close_app("prism")["error"].startswith("Games and apps"), c
    sh._tick(fx)
    fx2, sh.apps["py"] = FakeX((1, 10, 11)), {"proc": None, "win": 10}    # an exec tile's second window (same class,
    fx2.front = 11                               # no pid to match: Flatpak) joins its card, top-most first
    sh._tick(fx2)
    assert sh._groups == {"py": [11, 10]} and sh._tile_wins["py"] == 11 and sh.state()["active"] == "py", sh._groups
    del sh.apps["py"]
    sh._tick(fx)                                 # (back to the first fixture)
    real_x11, sh.x_ok, sh._kwin = X11, True, 1   # switching: x: keys to their top-most window; guards cleared
    globals()["X11"] = lambda: fx
    try:
        sh._home_guard = sh._steam_guard = 9e9
        assert sh.open("x:org.gnome.nautilus") == {"ok": True, "surface": "app"} and fx.calls == [8, -1], fx.calls
        assert sh._home_guard == sh._steam_guard == 0.0 and sh.state()["title"] == "Nautilus"
        assert sh._active_key == "x:org.gnome.nautilus" and sh.running_apps()[0]["key"] == "x:org.gnome.nautilus"
        assert sh.open("x:gone") == {"ok": False, "error": "Gone isn't open anymore"}
        sh._home_guard, fx.calls = 9e9, []
        assert sh.open("steam:435150")["ok"] and fx.calls == [5, -1] and sh._home_guard == 0.0, fx.calls
        fx.stack.append(12)                      # a Steam game no tile lists (RDR2 isn't in this library): its card
        sh._tick(fx)                             # opens too, as an untiled window's does (OK, Menu back to it)
        fx.calls = []
        assert sh.open("steam:1174180") == {"ok": True, "surface": "app"} and fx.calls == [12, -1], fx.calls
        fx.stack.remove(12)
        sh._tick(fx)                             # ...and with no window yet (Steam's log lists it), as a tile's card,
        sh.running = sh.running | {"steam:1174180"}   # never "no tile 'steam:1174180'"
        assert sh.open("steam:1174180") == {"ok": False, "error": "Steam App 1174180 has no window to show yet"}
        sh.running = sh.running - {"steam:1174180"}
    finally:
        globals()["X11"], sh.x_ok, sh._kwin = real_x11, False, None
    fx.front = 8                                 # an untiled window in front: its x: key, no tile; newest
    time.sleep(.26)
    sh._tick(fx)
    assert (sh.state()["surface"], sh.state()["active"], sh._active_key) == ("app", None, "x:org.gnome.nautilus")
    fx.front, fx.stack = 0, [3, 2, 7, 8, 9, 4, 5, 6, 1]     # nothing focused, the kiosk on top: the launcher
    sh._tick(fx)
    assert sh.state()["surface"] == "launcher" and sh._active_key is None
    fx.stack = [3, 2, 7, 8, 9, 4, 1, 6, 5]                  # ...a game on top: the app
    sh._tick(fx)
    assert sh.state()["surface"] == "app"
    fx.front = 5
    sh._tick(fx)
    fx.hook = sh.home                            # a Home finishing while a tick reads X: that tick doesn't undo it
    sh._tick(fx)
    assert sh.state()["surface"] == "launcher" and sh._left[0] == "steam:435150", sh.state()
    sh._tick(fx)
    assert sh.state()["surface"] == "launcher"   # (nor one under 0.25 s after it: mutter may not have applied it yet)
    time.sleep(.26)
    sh._tick(fx)
    assert sh.state()["surface"] == "app"        # (one later reads what is really in front)
    fx.front = 1
    sh._tick(fx)
    fx.hook = lambda: sh.open("steam:435150")   # ...nor an open() that a tick reading the kiosk in front would undo
    sh._tick(fx)
    assert sh.state()["surface"] == "app", sh.state()
    sh.pending.clear()
    # ...nor a tick that began while a Home held the lock: it reads X once that Home is done, but before mutter applied
    # its activation, so it still sees the game in front (_acted_at is the action's end, not its start)
    time.sleep(.26)
    fx.front = 5
    sh._tick(fx)
    went, sh._pause_front = threading.Event(), lambda: went.set() or time.sleep(.5)   # (a Home from a game: ~0.5 s)
    th = threading.Thread(target=sh.home)
    th.start()
    went.wait(2)
    time.sleep(.3)
    sh._tick(fx)                                 # (0.3 s into that Home: it waits for the lock Home holds)
    th.join()
    del sh._pause_front
    assert sh.state()["surface"] == "launcher", sh.state()

    def until(fn, t=2.0):
        end = time.monotonic() + t
        while not fn() and time.monotonic() < end:
            time.sleep(0.01)
        return fn()
    # a Steam-log ghost (no window, not our launch, no reaper) has no card; state.running still lists it
    assert until(lambda: not sh._scanning)       # (the ticks' /proc scan, on its own thread: done before ours is set)
    sh.running, sh._ptab = sh.running | {"steam:292030"}, (9e9, {})
    assert "steam:292030" not in [c["key"] for c in sh.running_apps()] and "steam:292030" in sh.state()["running"]
    sh._ptab = (9e9, {77: (1, 1, [b"/s/ubuntu12_32/reaper", b"SteamLaunch", b"AppId=292030", b"--", b"x"])})
    assert {k: v for k, v in sh.running_apps()[-1].items() if k in ("key", "windows", "state")} == \
        {"key": "steam:292030", "windows": 0, "state": "open"}            # its reaper lives: "Running without a window"
    sh._ptab, sh.pending["steam:292030"] = (9e9, {}), {"until": 9e9, "tries": 0, "at": 1}
    assert sh.running_apps()[-1]["state"] == "starting"                 # our launch, no window yet
    # (Steam's log lists it: its reaper may just be newer than the last scan, so a quit is refused, force offered once
    # a scan finds it)
    assert sh.close_app("steam:292030", person=True) == {"ok": False, "force": False,
                                                        "error": "The Witcher 3: Wild Hunt is still starting."}
    # ...and once it does, "quit it" on its loading screen (the agent, on this real Shell) says how to stop it
    sh._ptab = (9e9, {77: (1, 1, [b"/s/ubuntu12_32/reaper", b"SteamLaunch", b"AppId=292030", b"--", b"x"])})
    sh._splash = ("steam:292030", sh.pending["steam:292030"])
    sh.ui_report(None, "games", "home", "steam:292030")
    from .agent import Agent
    assert Agent(sh, {}, lambda *e: None).handle_text("quit it")["reply"] == "The Witcher 3: Wild Hunt is still " \
        "starting: say “force quit The Witcher 3: Wild Hunt” to stop it."
    sh._ptab, sh._splash = (9e9, {}), None
    sh.ui_report(None, "games", "home")
    sh.running = sh.running - {"steam:292030"}  # ...nor in Steam's log yet (it can take 20 s): Starting… too
    assert [c["state"] for c in sh.running_apps() if c["key"] == "steam:292030"] == ["starting"]
    # ...and with nothing started to signal (Steam took the launch and ran nothing: a launch-option chooser, a cloud
    # conflict), a quit ends our launch: its card goes and a re-press launches; a window that still comes is raised
    assert sh.close_app("steam:292030", person=True) == {"ok": True, "state": "closed",
                                                        "title": "The Witcher 3: Wild Hunt"}
    assert sh.pending["steam:292030"] == {"until": 9e9, "tries": 0} and \
        "steam:292030" not in [c["key"] for c in sh.running_apps()]
    sh.pending.clear()
    # force quit's process tree (the Spark's launch chain, FEX-prefixed): the reaper is anchored on its program (the
    # wrapper carries the same tokens) and must be the only one; it is spared; Steam or the desktop in the set aborts
    A = lambda s: s.encode().split(b" ")
    T = {4829: (1, 1, A("/home/t/.steam/ubuntu12_32/steam -silent")), 5000: (4829, 2, A("/bin/sh -c launch.sh")),
         5001: (5000, 3, A("/s/ubuntu12_32/steam-launch-wrapper -- reaper SteamLaunch AppId=435150 --")),
         5002: (5001, 4, A("FEXInterpreter /s/ubuntu12_32/reaper SteamLaunch AppId=435150 -- x")),
         5003: (5002, 5, A("pv-adverb --exit-with-parent -- wine EoCApp.exe")), 5004: (5003, 6, A("wineserver")),
         5005: (5003, 7, A("wine64 EoCApp.exe")), 6001: (4829, 8, A("/s/ubuntu12_32/reaper SteamLaunch AppId=1174180 -- x"))}
    assert reapers(T, "435150") == [5002] and reapers(T, 1174180) == [6001] and reapers(T, "9") == []
    assert sorted(descendants(T, 5002)) == [5003, 5004, 5005] and not any(_protected(T[p][2]) for p in descendants(T, 5002))
    assert _protected(T[4829][2]) and _exe(T[5002][2]) == b"reaper" and _exe(T[5001][2]) == b"steam-launch-wrapper"
    sh._ptab = (9e9, {**T, 6002: (4829, 9, A("/s/ubuntu12_32/reaper SteamLaunch AppId=1174180 -- y"))})
    assert sh._can_force("steam:1174180", "game") == (False, "two launches match")
    assert sh._can_force("steam:9", "game") == (False, "its Steam process wasn't found")
    # ...from the last scan's table, never a new scan: a cmdline read waits on a wedged game's mmap_lock, and a wedged
    # game is what force quit is for (a scan here hung it for good, nothing signalled, no toast)
    signalled, scans, real_pt, real_term = [], [], proc_table, terminate
    globals().update(proc_table=lambda: scans.append(1) or {},
                     terminate=lambda pids, table: signalled.append(sorted(pids)) or ([], []))
    try:
        sh._ptab = (9e9, {**T, 5006: (5003, 11, A("/home/t/.steam/ubuntu12_32/steamwebhelper x"))})
        events.clear()                           # (on a stuck game: a force that fails leaves it stuck, not "open")
        sh._closing = {"steam:435150": {"at": 0, "w": 5}}
        assert sh.close_app("steam:435150", force=True, person=True)["state"] == "forcing"
        assert until(lambda: ("toast", {"kind": "error", "text": "Couldn't force quit Divinity: Original Sin 2: Steam "
                                        "or the desktop is in its process tree"}) in events), events
        assert signalled == [] and sh._closing == {"steam:435150": {"at": 0, "w": 5}}, sh._closing
        sh._closing = {}
        sh._ptab = (9e9, T)
        sh._force("steam:435150", "game", "Divinity")
        assert signalled == [[5003, 5004, 5005]] and scans == [], (signalled, scans)   # the reaper itself is spared
    finally:
        globals().update(proc_table=real_pt, terminate=real_term)
    if sys.platform.startswith("linux"):         # the real signalling, on dummy processes of our own
        _selftest_terminate(tmp)
        stub = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN)"
                                 "; print(flush=True); time.sleep(60)"], stdout=subprocess.PIPE, start_new_session=True)
        try:                                     # an exec app (our child) ignoring TERM: its group is killed after 3 s
            stub.stdout.readline()
            sh.apps["py"] = {"proc": stub, "win": None}
            sh._force("py", "app", "Py")
            assert stub.wait(2) == -signal.SIGKILL, stub.poll()
        finally:
            sh.apps.pop("py", None)
            if stub.poll() is None:
                stub.kill()
            stub.wait()
            stub.stdout.close()
    # closing (real X): one window at a time, top-most first; the per-kind rules; the close states _tick keeps
    fx, kid = FakeX((3, 1, 2, 7, 8, 9, 4, 5, 6)), lambda: subprocess.Popen(["sleep", "30"])   # (processes of ours)
    game, toasts = kid(), lambda: [x["text"] for e, x in events if e == "toast"]
    fx.P = {4: os.getpid(), 5: game.pid, 8: os.getpid()}   # DOS2's process and its launcher's (two), Nautilus's
    real_x11, sh.x_ok, sh.stuck_s, sh._wins = X11, True, .3, {}   # (_wins: read again, with these pids)
    sh._ptab = (9e9, {70: (1, 1, A("/s/reaper SteamLaunch AppId=435150")), 71: (1, 2, A("/s/reaper SteamLaunch "
                      "AppId=1174180"))})   # (DOS2's and RDR2's launches, at pids none of ours has: no window walks in)
    globals()["X11"] = lambda: fx
    try:
        fx.front = 1                             # the launcher (the switcher) in front
        sh._tick(fx)
        events.clear()
        assert sh.close_app("steam:435150")["error"].startswith("Games and apps quit only") and fx.deleted == []
        assert sh.close_app("steam:435150", person=True) == {"ok": True, "state": "closing",
                                                            "title": "Divinity: Original Sin 2"} and fx.deleted == [5]
        assert sh.close_app("steam:435150", person=True)["state"] == "closing" and fx.deleted == [5]   # idempotent
        sh._tick(fx)
        assert fx.deleted == [5]                 # its launcher, 4, is asked only once 5 is gone...
        fx.stack.remove(5), fx.stack.remove(6)
        sh._tick(fx)
        assert fx.deleted == [5]                 # ...and its process too (a launcher closed under a game still quitting
        game.kill(), game.wait()                 # takes it down outside its own quit)
        sh._tick(fx)
        assert fx.deleted == [5, 4] and sh._closing["steam:435150"]["w"] == 4
        time.sleep(.35)
        sh._tick(fx), sh._tick(fx)
        assert toasts().count("Divinity: Original Sin 2 didn't quit. Open it to see why, or force quit it.") == 1
        assert sh.close_app("steam:435150", person=True) == {"ok": False, "force": True,
                                                            "error": "Divinity: Original Sin 2 didn't quit when asked."}
        fx.front = 4                             # it came to the front (its prompt): after stuck_s there, in use again
        sh._tick(fx)
        assert "front" in sh._closing["steam:435150"]
        time.sleep(.35)
        sh._tick(fx)
        assert "steam:435150" not in sh._closing and [c["state"] for c in sh.running_apps()][0] == "open"
        fx.calls = []                            # asked in front ("quit the game"), its windows go: the launcher
        assert sh.close_app("steam:435150", person=True)["state"] == "closing" and fx.deleted[-1] == 4
        fx.stack.remove(4)
        sh._tick(fx)
        assert fx.calls[:1] == [1] and sh.state()["surface"] == "launcher" and "steam:435150" not in sh._closing
        fx.stack += [4, 5]                       # windowless in front but still running, nobody asked: "Quitting…",
        fx.front = 5                             # and the kiosk stays put (it may be a launcher handing over)
        sh._tick(fx)
        sh.log_running = {"435150"}
        fx.stack = [w for w in fx.stack if w not in (4, 5)]
        sh._tick(fx)
        assert sh._closing["steam:435150"]["auto"] and fx.calls[1:] == [] and \
            next(c for c in sh.running_apps() if c["key"] == "steam:435150")["state"] == "closing"
        fx.stack += [5]                          # a window came back (a launcher handing over to its game)
        sh._tick(fx)
        assert "steam:435150" not in sh._closing
        sh.log_running = set()
        assert sh.close_app("bigpicture")["state"] == "closing" and fx.deleted[-1] == 2       # an agent may
        assert sh._steam_guard > time.monotonic() + 10                   # Steam's window popping up: back behind
        assert sh.close_app("bigpicture", force=True, person=True)["error"].endswith("only games and apps can be force quit.")
        assert sh.close_app("x:xterm") == {"ok": False, "force": False,   # (a window, but no WM_DELETE_WINDOW)
                                           "error": "Xterm can't be asked to quit safely: quit it from its own menu."}
        assert sh.close_app("x:org.gnome.nautilus", force=True, person=True)["ok"] is False
        # RDR2 in a Wine virtual desktop: its one window lists WM_DELETE_WINDOW, but Wine would end the whole prefix
        # (game and launcher at once, their own quit skipped): it is never asked; its card offers force (Cancel first),
        # and says why with its window on screen (not "no window"): the game's own menu saves first
        fx.stack.append(12)
        sh._tick(fx)
        c = next(c for c in sh.running_apps() if c["key"] == "steam:1174180")
        r = sh.close_app("steam:1174180", person=True)
        assert not c["can_close"] and c["windows"] == 1 and r == {"ok": False, "force": True, "error": "Steam App "
            "1174180 can't be asked to quit safely: quit it from its own menu, or force quit it."}, (c, r)   # (RDR2)
        assert 12 not in fx.deleted
        fx.stack.remove(12)
        xs, sent = real_x11.__new__(real_x11), []   # (the real X11.delete, over stubbed reads: nothing sent to it)
        xs.d, xs.atom, xs.prop = None, {"WM_DELETE_WINDOW": 1, "WM_PROTOCOLS": 2}.get, lambda w, name: [1]
        xs.name = lambda w: {1: "Default - Wine Desktop", 2: "Red Dead Redemption 2"}[w]
        xs.X = type("Xlib", (), {"XSendEvent": lambda self, d, w, *a: sent.append(w), "XFlush": lambda self, d: 0})()
        assert xs.delete(1) is False and xs.delete(2) is True and sent == [2]
        # the next window once that process exited, however late its window went (stuck: a save past stuck_s, a
        # confirm answered later; its process gone in the same tick as its window, the slow save below a tick after);
        # in front meanwhile too (an in-game "quit the game", Wine raising it, its own prompt answered: the launcher is
        # asked, its time in front counted afresh); the same process's next window at once (one app's two windows);
        # never for a pid we can't see (another namespace's). Untiled windows' toasts offer no force (nor Steam's)
        sh.close_app("x:steam")
        sh._closing = {k: {**e, "at": e["at"] - 1} for k, e in sh._closing.items()}   # (a second on)
        for case in ("stuck", "front", "unknown", "same"):
            p = kid()
            fx.P[13] = {"unknown": 0, "same": os.getpid()}.get(case, p.pid)   # (8: os.getpid(), another process)
            fx.stack, fx.front, fx.deleted = fx.stack + [13], 1, []
            sh._tick(fx)
            assert sh.close_app("x:org.gnome.nautilus")["state"] == "closing" and fx.deleted == [13], case
            if case == "stuck":
                e = sh._closing["x:org.gnome.nautilus"]
                sh._closing = {**sh._closing, "x:org.gnome.nautilus": {**e, "at": e["at"] - 1}}
            fx.front = 13 if case == "front" else 1
            sh._tick(fx)
            fx.stack.remove(13)
            p.kill(), p.wait()
            sh._tick(fx)
            asked = case != "unknown"
            assert fx.deleted == ([13, 8] if asked else [13]), (case, fx.deleted)
            assert ("x:org.gnome.nautilus" in sh._closing) == asked, case   # (else that close is done)
            assert "front" not in sh._closing.get("x:org.gnome.nautilus", {}), sh._closing
            sh._closing = {k: e for k, e in sh._closing.items() if k != "x:org.gnome.nautilus"}
        assert {"Steam didn't quit. Open it to see why.", "Nautilus didn't quit. Open it to see why."} <= set(toasts())
        assert toasts().count("Steam Big Picture didn't quit. Open it to see why.") == 1
        card = lambda k: next((c for c in sh.running_apps() if c["key"] == k), None)
        # the asked window went: it took the quit, and its shutdown is timed from then. Its prompt answered in front,
        # then a slow exit (Proton, FEX) past stuck_s from the ask: Quitting…, never dropped for its time in front, and
        # its launcher is asked once that process exits (within stuck_s of its window going)
        g2, sh._closing, sh._wins = kid(), {}, {}   # (DOS2 again: game 5, launcher 4; _wins read again)
        fx.P[5], fx.stack, fx.front, fx.deleted = g2.pid, [w for w in fx.stack if w not in (4, 5)] + [4, 5], 1, []
        sh._tick(fx)
        assert sh.close_app("steam:435150", person=True)["state"] == "closing" and fx.deleted == [5]
        fx.front = 5                             # its prompt, in front
        sh._tick(fx)
        e = sh._closing["steam:435150"]          # (0.25 s on)
        sh._closing = {**sh._closing, "steam:435150": {**e, "at": e["at"] - .25, "front": e["front"] - .25}}
        fx.stack.remove(5)
        fx.front = 4                             # answered: its window goes, mutter focuses its launcher
        sh._tick(fx)
        time.sleep(.1)                           # (0.35 s since the ask and in front: past stuck_s)
        sh._tick(fx)
        assert card("steam:435150")["state"] == "closing" and fx.deleted == [5], (sh._closing, fx.deleted)
        g2.kill(), g2.wait()
        sh._tick(fx)
        assert fx.deleted == [5, 4] and sh._closing["steam:435150"]["w"] == 4, (fx.deleted, sh._closing)
        # ...and with no window left (Steam's log still lists it, its reaper lives): Quitting… for stuck_s from then,
        # no "didn't quit" toast; then stuck as "still quitting" (force, Cancel first), never "open it to see why"
        sh._closing, sh.log_running = {}, {"435150"}
        fx.stack, fx.front, fx.deleted = [w for w in fx.stack if w != 4] + [5], 1, []
        sh._tick(fx)
        assert sh.close_app("steam:435150", person=True)["state"] == "closing" and fx.deleted == [5]
        n, e = len(toasts()), sh._closing["steam:435150"]
        sh._closing = {**sh._closing, "steam:435150": {**e, "at": e["at"] - .25}}
        fx.stack.remove(5)                       # its last window goes; its processes are still exiting
        sh._tick(fx)
        time.sleep(.1)
        sh._tick(fx)
        assert (card("steam:435150")["state"], card("steam:435150")["windows"]) == ("closing", 0), card("steam:435150")
        time.sleep(.25)
        sh._tick(fx)
        assert card("steam:435150")["state"] == "stuck" and toasts()[n:] == [], toasts()[n:]
        assert sh.close_app("steam:435150", person=True) == {"ok": False, "force": True,
                                                            "error": "Divinity: Original Sin 2 is still quitting."}
        sh.log_running = set()
        sh._tick(fx)
        assert "steam:435150" not in sh._closing and card("steam:435150") is None   # it exited
        # ...and with its launcher still open: "still quitting" too (card, reply), never "didn't quit when asked"
        g3, sh._closing, sh._wins = kid(), {}, {}
        fx.P[5], fx.stack, fx.front, fx.deleted = g3.pid, [w for w in fx.stack if w not in (4, 5)] + [4, 5], 1, []
        sh._tick(fx)
        assert sh.close_app("steam:435150", person=True)["state"] == "closing" and fx.deleted == [5]
        fx.stack.remove(5)                       # (at once: it took the quit; it saves past stuck_s)
        sh._tick(fx)
        time.sleep(.35)
        sh._tick(fx)
        c = card("steam:435150")
        assert (c["state"], c["windows"], c["quitting"]) == ("stuck", 1, True) and fx.deleted == [5], c
        assert sh.close_app("steam:435150", person=True)["error"] == "Divinity: Original Sin 2 is still quitting."
        # ...and its window gone long after the ask (a slow save under FEX, not in front), its process still exiting:
        # Quitting… from then, never "open" with its launcher asked by a second quit; once it exited, its launcher
        sh._closing, fx.stack, fx.deleted = {}, fx.stack + [5], []
        sh._tick(fx)
        assert sh.close_app("steam:435150", person=True)["state"] == "closing" and fx.deleted == [5]
        e = sh._closing["steam:435150"]
        sh._closing = {**sh._closing, "steam:435150": {**e, "at": e["at"] - 1, "told": True}}   # (stuck: a second on)
        fx.stack.remove(5)
        sh._tick(fx)
        assert card("steam:435150")["state"] == "closing" and sh.close_app("steam:435150", person=True)["state"] == \
            "closing" and fx.deleted == [5], (sh._closing, fx.deleted)
        g3.kill(), g3.wait()
        sh._tick(fx)
        assert fx.deleted == [5, 4], fx.deleted
        # a force quit that hasn't taken past stuck_s (a process stuck in the kernel keeps its windows) is still
        # quitting: never "Didn't quit", "Open it to see why", nor "didn't quit when asked" (it was never asked)
        sh._closing = {"steam:435150": {"at": time.monotonic() - 1, "force": True}}
        c = card("steam:435150")
        assert (c["state"], c["windows"], c["quitting"]) == ("stuck", 1, True), c
        assert sh.close_app("steam:435150", person=True)["error"] == "Divinity: Original Sin 2 is still quitting."
        # snapshots: on the step from an app to the kiosk (Home), taken a tick later if the kiosk is still in front and
        # the window still there; an app back in front first: none; a black frame: none; gone with the app
        fx, sh._closing = FakeX((3, 1, 2, 7, 8, 9, 4, 5, 6)), {}
        fx.front = 5
        sh._tick(fx)
        fx.front = 1
        sh._tick(fx)
        assert sh._snap_due[:2] == ("steam:435150", 5) and fx.thumbs == []
        sh._tick(fx)
        assert fx.thumbs == []                   # (not due yet: 0.4 s)
        sh._snap_due = (*sh._snap_due[:2], 0.0)
        sh._tick(fx)
        snap = next(c for c in sh.running_apps() if c["key"] == "steam:435150")["snap"]
        assert fx.thumbs == [5] and sh.snap("steam:435150")[:8] == b"\x89PNG\r\n\x1a\n" and \
            snap == f"/api/snap/steam%3A435150?v={sh._snap_n}", snap
        for front in (8, 1, 8):                  # Nautilus left, then back in front before its snapshot
            fx.front = front
            sh._tick(fx)
        assert sh._snap_due is None and fx.thumbs == [5]
        fx.front, fx.frame = 1, bytes(256 * 36)  # left again, but its frame is black
        sh._tick(fx)
        sh._snap_due = (*sh._snap_due[:2], 0.0)
        sh._tick(fx)
        assert fx.thumbs == [5, 8] and sh.snap("x:org.gnome.nautilus") is None
        fx.stack = [w for w in fx.stack if w not in (4, 5, 6)]
        sh._tick(fx)
        assert sh.snap("steam:435150") is None   # DOS2 exited: its snapshot went with it
    finally:
        globals()["X11"], sh.x_ok, sh.stuck_s = real_x11, False, 6
        game.kill(), game.wait()                 # (ours: by its Popen)
    # the switcher never scans /proc under the lock nor on the watch thread (a cmdline read waits on a wedged game's
    # mmap_lock; the Home and push-to-talk keys are that thread's): _tick starts one scan at a time on a thread of its
    # own, and only while a Steam game runs or starts, and once at start
    scans, real_pt, wedge = [], proc_table, threading.Event()
    globals()["proc_table"] = lambda: scans.append(sh.lock._owner == threading.get_ident()) or wedge.wait(5) and {}
    try:
        sh._ptab, sh.log_running, sh.running = (0.0, {}), {"292030"}, {"steam:292030"}   # (no window: a reaper?)
        with sh.lock:                            # (as _switcher and close_app run it)
            sh.running_apps(), sh.close_app("steam:292030", person=True)
        assert scans == [], scans
        t0 = time.monotonic()
        sh._tick(fx), sh._tick(fx)               # (a scan that waits on a wedged game: the ticks go on, no second one)
        assert time.monotonic() - t0 < 1 and until(lambda: scans == [False]), (time.monotonic() - t0, scans)
        wedge.set()
        assert until(lambda: not sh._scanning) and sh._ptab[0] > 0
        # ...while one starts (our launch, before Steam logs it): its card's force is judged from what runs now, never
        # from a table kept since the game last quit (its old reaper: force offered, the old pids signalled, the card
        # dropped while the game still started)
        sh.log_running, sh.running = set(), set()
        sh._ptab = (time.monotonic() - 60, {77: (1, 1, [b"/s/ubuntu12_32/reaper", b"SteamLaunch", b"AppId=292030"])})
        sh.pending["steam:292030"] = {"until": 9e9, "tries": 0, "at": time.monotonic()}
        sh._tick(fx)
        assert until(lambda: not sh._scanning) and sh._ptab[1] == {} and \
            [c["can_force"] for c in sh.running_apps() if c["key"] == "steam:292030"] == [False], sh._ptab
        sh.pending.clear()
        # ...and once at start: a native game (no steam_app_ class) already running when the daemon restarts is known by
        # its reaper only (Steam's log is read from its end): its game card, not a "window" card an agent may quit
        globals()["proc_table"] = lambda: {78: (1, 1, [b"/s/ubuntu12_32/reaper", b"SteamLaunch", b"AppId=391220"]),
                                           79: (78, 2, [b"./RotTR"])}
        fresh, fx3 = Shell(cfg, False, lambda *_: None), FakeX((1, 14))
        fx3.P[14] = 79
        fresh._tick(fx3)
        assert until(lambda: not fresh._scanning)
        fresh._tick(fx3)
        c = [(c["key"], c["kind"]) for c in fresh.running_apps()]
        assert c == [("steam:391220", "game")] and fresh.state()["running"] == ["steam:391220"], c
    finally:
        globals()["proc_table"], sh.log_running = real_pt, set()
    sh._wins, sh._seen, sh._kwin, sh._tile_wins, sh._prev_running = {}, set(), None, {}, set()
    sh._groups, sh._closable, sh._ptab, sh.running, sh._closing, sh._steam_guard = {}, set(), (0.0, {}), set(), {}, 0.0
    # GameSpark Steam setup: settings.steam_setup picks a cold start's Steam; a running Steam's own setup wins
    gs, fake = tmp / "gs", tmp / "fakebin"
    (gs / "lib").mkdir(parents=True)
    fake.mkdir()
    (gs / "lib" / "env.sh").write_text(': "${GAMESPARK_STEAM:=detected}"; echo "$GAMESPARK_STEAM" >> "$1/seen"\n'
                                       'STEAM_START=true STEAM_PIPE=$1/pipe\n'
                                       'steam_running() { pgrep -f "[u]buntu12_32/steam " >/dev/null; }\n'
                                       'steam_launch() { :; }\n')
    os.mkfifo(gs / "pipe")
    (fake / "pgrep").write_text('#!/bin/sh\ncase "$*" in *buntu12_32/steam*) [ -f "$(dirname "$0")/up" ] ;; esac\n')
    (fake / "pgrep").chmod(0o755)
    path = os.environ["PATH"]
    os.environ["PATH"] = f"{fake}:{path}"
    try:
        sh.settings.update(gamespark=str(gs), steam_setup="fex")
        sh._steam_autostart()                    # no Steam: start the configured setup
        (fake / "up").touch()
        assert sh._steam({"appid": "435150"})[0] is True
        assert (gs / "seen").read_text().split() == ["fex", "detected"], (gs / "seen").read_text()
        sh._steam_guard = 0.0                    # (b) a launch that started Steam: its window kept behind for a minute,
        assert sh._steam({"appid": "435150"})[0] is True and sh._steam_guard == 0.0   # as after autostart; not when it
        (fake / "up").unlink()                                                       # was running
        assert sh._steam({"appid": "435150"})[0] is True and sh._steam_guard > time.monotonic() + 50
        sh._steam_guard = 0.0
    finally:
        os.environ["PATH"] = path
        sh.settings["gamespark"] = str(tmp / "nope")
    # the log tailer finishes a rotated file: its last line may be the 'removed' (else the game stays 'running')
    logf = steam / "logs" / "console_log.txt"
    logf.parent.mkdir()
    logf.write_text("")
    lines = tmp / "launches.log"                 # (GameSpark's, followed with it: an old line is history)
    lines.write_text("2026-10-10 01:00:00 app 435150 applied: 435150.env (FEX: /x (missing)) not launched: x\n")
    real_launches, globals()["LAUNCHES"] = LAUNCHES, str(lines)
    threading.Thread(target=sh._tail_steam_log, args=(0.01,), daemon=True).start()
    time.sleep(0.2)
    with open(logf, "a") as f:
        f.write('[2026-10-08 02:18:40] Game process added : AppID 292030 "x"\n')
    assert until(lambda: "292030" in sh.log_running)
    os.replace(logf, logf.with_name("console_log.previous.txt"))
    with open(logf.with_name("console_log.previous.txt"), "a") as f:
        f.write('[2026-10-08 02:26:22] Game process removed: AppID 292030 "x"\n')
    logf.write_text("")
    assert until(lambda: not sh.log_running), sh.log_running
    os.replace(logf, logf.with_name("console_log.previous.txt"))     # a reopen that fails once is retried
    logf.mkdir()                                                      # (open() of a directory fails)
    time.sleep(0.1)
    logf.rmdir()
    logf.write_text('[2026-10-08 02:30:00] Game process added : AppID 7 "x"\n')
    assert until(lambda: "7" in sh.log_running)
    with open(logf, "a") as f:
        f.write('[2026-10-08 02:31:00] Game process removed: AppID 7 "x"\n')
    assert until(lambda: not sh.log_running), sh.log_running
    # launches: a second OK while one starts doesn't start a second copy; after a failed launch it does
    sh._launch = lambda t, entry: None           # (a launch in flight)
    events.clear()
    starts = lambda: [e for e, _ in events].count("toast")   # (an app's "Starting …"; a game's splash says it)
    t0 = time.monotonic()
    assert sh.open("py") == sh.open("py") == {"ok": True, "surface": "app"} and starts() == 1
    assert [(c["key"], c["state"]) for c in sh.running_apps()] == [("py", "starting")] and sh._used["py"] >= t0
    sh.pending.pop("py")                         # what a failed launch does
    assert sh.open("py")["ok"] and starts() == 2

    class Child:                                 # alive, before the 500 ms tick lists it as running
        poll = staticmethod(lambda: None)
    sh.pending.clear()
    sh.apps["py"] = {"proc": Child(), "win": None}
    assert sh.open("py")["ok"] and starts() == 2 and sh.state()["surface"] == "app"
    # a Steam game running without a window (its reaper lives: loading, or its launcher handing over) is never launched
    # a second time from its card or tile; a ghost (no reaper: Steam's log missed 'removed') still launches
    sh.running = {"steam:435150"}
    sh._ptab = (9e9, {77: (1, 1, [b"/s/ubuntu12_32/reaper", b"SteamLaunch", b"AppId=435150", b"--", b"x"])})
    assert sh.open("steam:435150") == {"ok": False, "error": "Divinity: Original Sin 2 has no window to show yet"}
    assert starts() == 2 and "steam:435150" not in sh.pending
    sh._ptab, n = (9e9, {}), len(events)        # (over an app: the launcher first, for the game's loading screen; no
    assert sh.open("steam:435150")["ok"] and starts() == 2 and "steam:435150" in sh.pending   # toast over it)
    ev = [e for e, _ in events[n:]]
    assert ev[0] == "home" and ev[-1] == "launch" and sh.surface == "launcher", ev
    sh.pending.clear(), sh.running.clear()
    # a launch that dies at start: no Starting… card (it stayed 2 min for an app, 5 for a Steam game, and couldn't be
    # quit nor force quit: no process) and a retry launches at once. An app's child exited before its window: that
    # window gets 5 s more (a client whose server maps it: gnome-terminal), then nothing waits for it
    keys = lambda: [c["key"] for c in sh.running_apps()]

    class Dead:
        poll = staticmethod(lambda: 1)
    sh.apps.pop("py")
    assert sh.open("py")["ok"] and starts() == 3 and keys() == ["py"]
    sh.apps["py"] = {"proc": Dead(), "win": None}
    sh._tick(FakeX((1,)))
    assert keys() == [] and sh.pending["py"]["until"] <= time.monotonic() + 5, (keys(), sh.pending)
    assert sh.open("py")["ok"] and starts() == 4 and keys() == ["py"]
    sh.pending.clear(), sh.apps.pop("py", None)
    # ...a Steam game that Steam logged, then 'removed' (a crash under Proton or FEX): its window is still raised if
    # one comes (pending), and a process Steam starts after it (the game, after Proton's install-script evaluator) is
    # Starting… again

    def steam_log(what):
        with open(logf, "a") as f:
            f.write(f'[2026-10-10 01:00:00] Game process {what}: AppID 1091500 "x"\n')
    n = len(events)                              # (on the launcher: no Home first)
    assert sh.open("steam:1091500")["ok"] and starts() == 4 and "home" not in [e for e, _ in events[n:]]
    for what, want in (("added", ["steam:1091500"]), ("removed", []), ("added", ["steam:1091500"]), ("removed", [])):
        steam_log(what)
        assert until(lambda: ("1091500" in sh.log_running) == (what == "added") and keys() == want), (what, keys())
        assert [c["state"] for c in sh.running_apps()] == ["starting"] * len(want) and "steam:1091500" in sh.pending
    assert sh.open("steam:1091500")["ok"] and starts() == 4 and keys() == ["steam:1091500"]
    sh.pending.clear()
    sh._ptab = (0.0, {})
    # GameSpark's launches.log, followed with Steam's log (new lines only): its FEX error ends our launch, its warnings
    # are said; (e) a profiled game Steam started with no line for it within NOCONFIG_S, or with a line that lists no
    # <appid>.env: "started without its GameSpark config", once; never for a game without a profile

    def steam(what, appid):
        with open(logf, "a") as f:
            f.write(f'[2026-10-10 05:00:00] Game process {what} : AppID {appid} "/home/spark/gamespark/tools/launch.sh"\n')

    def gs_log(s):
        with open(lines, "a") as f:
            f.write(f"2026-10-10 05:00:01 app {s}\n")
    said = lambda kind: [x["text"] for e, x in events if e == "toast" and x["kind"] == kind]   # noqa: E731
    events.clear()
    steam("added", "1091500")                    # its line after its process (as launch.sh writes it), or before it
    assert until(lambda: "1091500" in sh._noconfig)
    gs_log("1091500 applied: 1091500.env run.env")
    gs_log("292030 applied: 292030.env (args: --launcher-skip)")
    assert until(lambda: "292030" in sh._lines) and "1091500" not in sh._noconfig
    steam("added", "292030")
    assert until(lambda: "292030" in sh.log_running) and "292030" not in sh._noconfig and said("warn") == []
    real_nc, globals()["NOCONFIG_S"] = NOCONFIG_S, .2
    try:
        steam("added", "435150"), steam("added", "391220")      # profiled, no line; no profile
        assert until(lambda: said("warn") == ["Divinity: Original Sin 2 started without its GameSpark config"])
        time.sleep(.3)
        assert said("warn") == ["Divinity: Original Sin 2 started without its GameSpark config"], said("warn")
    finally:
        globals()["NOCONFIG_S"] = real_nc
    with sh.lock:                                # its clock restarts at each process Steam starts: Proton's install-
        sh._noconfig["435150"] = 1.0             # script evaluator runs first, without the launch options, then the
        sh._steam_event("added", "435150")       # game through launch.sh, which writes the line
        assert sh._noconfig.pop("435150") > 1.0
    events.clear()
    gs_log("1091500 applied: run.env")           # a line without its profile: at once
    assert until(lambda: said("warn") == ["Cyberpunk 2077 started without its GameSpark config"]), events
    sh.pending["steam:435150"] = {"until": 9e9, "tries": 0, "at": time.monotonic()}   # (our launch)
    events.clear()
    gs_log("435150 applied: 435150.env (FEX: /x/FEX (missing)) not launched: run system/fex-patched.sh install")
    assert until(lambda: "steam:435150" not in sh.pending) and \
        said("error") == ["Couldn't start Divinity: Original Sin 2: GameSpark's FEX build is missing"] and \
        ("launch", {"id": "steam:435150", "stage": "failed"}) in events and said("warn") == [], events
    gs_log(f"435150 applied: 435150.env WARNING: {chg}")   # the game starts, with a warning
    assert until(lambda: said("warn") == [f"Divinity: Original Sin 2: {chg}"]), events
    # ...and our launch's stages from Steam's log: its process started (the splash says Loading…), it ended (gone: the
    # splash fails EXIT_S later, unless another starts)
    sh.pending["steam:1091500"] = e = {"until": 9e9, "tries": 0, "at": 1.0}
    events.clear()
    steam("added", "1091500")
    assert until(lambda: ("launch", {"id": "steam:1091500", "stage": "loading"}) in events) and "added" in e
    steam("removed", "1091500")
    assert until(lambda: "gone" in e) and "at" not in e
    for a in ("292030", "435150", "391220"):
        steam("removed", a)
    assert until(lambda: not sh.log_running), sh.log_running
    globals()["LAUNCHES"] = real_launches
    sh.pending.clear(), sh._noconfig.clear()
    # A game's launch holds (LAUNCH-DESIGN.md §1): the kiosk, its splash, pinned (EWMH above) over Steam's windows and
    # the game's until the game has drawn; Steam's only once Steam waits on them; then the hand-over
    fx, k = FakeX((3, 1)), "steam:1091500"
    under = lambda w: fx.stack.insert(fx.stack.index(1), w)   # noqa: E731  (mutter: mapped under the pinned kiosk)
    real_x11, sh.x_ok, sh._wins = X11, True, {}
    globals()["X11"] = lambda: fx
    try:
        fx.front, fx.pinned = 1, {1}             # nothing launching, a pin a crash left: unpinned
        sh._tick(fx)
        assert fx.pinned == set() and fx.log == [("above", 1, False)], fx.log
        events.clear()
        assert sh.open(k) == {"ok": True, "surface": "app"} and ("launch", {"id": k, "stage": "starting"}) in events
        e = sh.pending[k]
        sh._tick(fx)
        assert fx.pinned == {1}
        under(16)                                # Steam's Launching... maps under it and takes focus: pushed back
        fx.front = 16
        sh._tick(fx)
        assert fx.log[-1] == ("activate", 1) and ("activate", 16) not in fx.log, fx.log
        sh.log_running = {"1091500"}             # its process, then its window: Proton focuses it as it maps, under
        under(18)                                # the pin; the splash is what's on screen, and it gets the focus (the
        fx.front, fx.log = 18, []                # pad, the keys) back, once: the game taking it again keeps it, no
        time.sleep(.26)                          # ping-pong
        sh._tick(fx)
        assert "seen" in e and fx.log == [("activate", 1)] and fx.thumbs == [], (fx.log, e)
        assert sh.state()["surface"] == "launcher"
        fx.front = 18
        sh._tick(fx)
        assert fx.log == [("activate", 1)] and fx.pinned == {1} and sh.state()["surface"] == "launcher", fx.log
        e["seen"] -= SETTLE_S                    # settled: a black frame is no hand-over; its first drawn one is
        fx.frame = bytes(256 * 36)
        sh._tick(fx)
        assert fx.thumbs == [18] and "go" not in e and fx.pinned == {1}
        fx.frame = bytes((i * 37) % 251 for i in range(256 * 36))
        sent, sh.active_tab, sh.tabs["netflix"] = [], "netflix", {"target": "TN", "session": "SN"}  # (Home on its
        sh.cdp = type("CDP", (), {"call": lambda _, *a, **kw: sent.append((*a, kw))})()  # loading screen, Netflix)
        sh._tick(fx)
        assert fx.log[-3:] == [("above", 1, False), ("activate", 18), ("iconify", 1)] and "go" in e, fx.log
        assert ("launch", {"id": k, "stage": "up"}) in events and fx.pinned == set()
        assert until(lambda: sent == [("Runtime.evaluate", {"session": "SN", "expression": PAUSE_JS, "timeout": 1})]), \
            sent                                 # ...pauses, as a game opened over it does
        sh.cdp, sh.active_tab = None, None
        del sh.tabs["netflix"]
        fx.iconic = lambda w: w == 1             # the game in front, the kiosk iconified: done
        sh._tick(fx)
        assert k not in sh.pending and fx.pinned == set()
        fx, sh._wins = FakeX((3, 1)), {}         # never drawn (black while covered, or unreadable): at DRAW_S anyway
        fx.front, fx.frame = 1, bytes(256 * 36)
        sh.pending[k] = e = {"until": 9e9, "tries": 0, "at": 1.0}
        under(18)
        sh._tick(fx)
        e["seen"] -= DRAW_S
        sh._tick(fx)
        assert "go" in e and fx.log[-2:] == [("activate", 18), ("iconify", 1)], fx.log
        # ...counted from its window: one that went before it drew (a device probe's, a stub's) gives the next one,
        # mapped later, SETTLE_S and DRAW_S of its own (its black frame never handed over at once)
        fx, sh._wins = FakeX((3, 1)), {}
        fx.front, fx.frame = 1, bytes(256 * 36)
        sh.pending[k] = e = {"until": 9e9, "tries": 0, "at": 1.0}
        under(18)
        sh._tick(fx)
        fx.stack.remove(18)
        sh._tick(fx)
        e["seen"] -= DRAW_S
        under(19)
        sh._tick(fx)
        assert "go" not in e and fx.thumbs == [] and fx.pinned == {1}, (e, fx.thumbs)
        e["seen"] -= SETTLE_S
        sh._tick(fx)
        assert "go" not in e and fx.thumbs == [19]
        e["seen"] -= DRAW_S
        sh._tick(fx)
        assert "go" in e and fx.log[-2:] == [("activate", 19), ("iconify", 1)], fx.log
        # (X applying the unpin after the tick's reads, mutter being slow: the pin read back still on is no hold ended
        # with nothing handed over, so the kiosk isn't brought back over the game just handed over)
        fx.stack, fx.front, fx.pinned, fx.icons = [3, 19, 1], 19, {1}, set()   # (19 under the pin, focused by Proton)
        fx.above = lambda w, on: fx.log.append(("above", w, on))
        sh.pending[k] = e = {"until": 9e9, "tries": 0, "at": 1.0, "seen": time.monotonic() - DRAW_S, "win": 19}
        n = len(fx.log)
        sh._tick(fx)
        assert "go" in e and ("activate", 19) in fx.log[n:] and ("activate", 1) not in fx.log[n:], fx.log[n:]
        # Steam stuck: PROMPT_S after it took the launch, no process and no window: a window titled as a launch prompt
        # comes over the splash (unpinned first), never Steam's own; it isn't pushed back while it asks; a process
        # ends that; NOPROC_S on, any Steam window; with none, the launch fails
        fx, sh._wins, sh.log_running = FakeX((3, 7, 1)), {}, set()
        fx.front = 1
        sh.pending[k] = e = {"until": 9e9, "tries": 0, "at": 1.0}
        under(17)                                # EULA, under the pin
        sh._tick(fx)
        e["sent"] = time.monotonic() - PROMPT_S + 1
        sh._tick(fx)
        assert fx.pinned == {1} and ("activate", 17) not in fx.log
        e["sent"] -= 1
        events.clear()
        sh._steam_guard = sh._home_guard = time.monotonic() + 60   # (Steam just started by it, (b); Home pressed)
        sh._tick(fx)
        assert fx.log[-2:] == [("above", 1, False), ("activate", 17)] and fx.pinned == set(), fx.log
        assert ("launch", {"id": k, "stage": "prompt"}) in events and ("activate", 7) not in fx.log
        sh._tick(fx)
        assert fx.log[-1] == ("activate", 17) and fx.pinned == set()
        fx.front, n = 7, len(fx.log)             # Steam's main window takes focus (the guards' case), or a window the
        sh._tick(fx)                             # user opened from the prompt: the kiosk never comes back over the
        assert fx.log[n:] == [] and sh._steam_guard == sh._home_guard == 0.0, fx.log[n:]   # prompt Steam waits on
        sh.home()                                # Home over it (left for later): asking only while it shows, so the
        sh._tick(fx)                             # covered prompt holds, pinned (every launch's loading screen with it),
        fx.front = 17                            # the focus it takes under the pin is given back, and OK on the game
        sh._tick(fx)                             # raises it again
        assert fx.pinned == {1} and fx.front == 1 and ("activate", 17) not in fx.log[n:], fx.log[n:]
        assert sh.open(k)["ok"]
        sh._tick(fx)
        assert fx.log[-2:] == [("above", 1, False), ("activate", 17)] and fx.pinned == set(), fx.log
        sh.log_running, e["added"] = {"1091500"}, 1.0   # answered: its process; Steam taking focus is pushed back again
        fx.stack.remove(17)
        under(16)
        fx.front = 16
        sh._tick(fx)
        assert fx.pinned == {1} and fx.log[-1] == ("activate", 1), fx.log
        sh.log_running = set()
        sh.pending[k] = e = {"until": 9e9, "tries": 0, "at": 1.0, "sent": time.monotonic() - NOPROC_S}
        fx.stack.remove(16)
        sh._tick(fx)
        assert fx.log[-1] == ("activate", 7) and e["prompt"] == 7, fx.log
        fx.stack.remove(7)
        events.clear()
        sh._tick(fx)
        assert k not in sh.pending and said("error") == ["Couldn't start Cyberpunk 2077: Steam started nothing in a "
                                                         "minute"] and ("launch", {"id": k, "stage": "failed"}) in events
        # its process ended: held EXIT_S (the next may come: the game, after Proton's install-script evaluator), then
        # it failed; at `until` (5 min), whatever it waits on, it failed. A launch that had a process never raises
        # Steam's windows (Launching... stays open until the game's window), nor fails as one that started nothing
        n = len(fx.log)
        under(16)
        sh.pending[k] = {"until": 9e9, "tries": 0, "sent": time.monotonic() - NOPROC_S, "added": 1.0,
                         "gone": time.monotonic() - EXIT_S + 1}
        sh._tick(fx)
        assert k in sh.pending and fx.pinned == {1} and ("activate", 16) not in fx.log[n:], fx.log[n:]
        fx.stack.remove(16)
        sh._tick(fx)
        assert k in sh.pending and fx.pinned == {1}
        sh.pending[k]["gone"] -= 1
        sh._tick(fx)
        assert k not in sh.pending and said("error")[-1] == "Couldn't start Cyberpunk 2077: it quit right after starting"
        sh.pending[k] = {"until": time.monotonic() - 1, "tries": 0, "at": 1.0}
        sh._tick(fx)
        assert k not in sh.pending and said("error")[-1] == "Couldn't start Cyberpunk 2077: it didn't start within 5 " \
                                                            "minutes" and fx.pinned == set()
        # pressed again while it holds (its tile, its card, voice): the splash again and its stage, a prompt the launcher
        # covered raised again, no second launch, its window not raised before it drew. Home leaves the splash, not
        # the hold; the kiosk iconified (another app opened): unpinned; pinned yet under a plain window: pushed back
        sh.pending[k] = e = {"until": 9e9, "tries": 0, "at": 1.0, "added": 1.0, "prompt": 7}
        sh.log_running = {"1091500"}
        under(18)
        sh._tick(fx)
        fx.log = []
        events.clear()
        assert sh.open(k)["ok"] and starts() == 0 and fx.log == [] and "prompt" not in e, (fx.log, events)
        stage_ev = lambda: [(t_, x.get("stage")) for t_, x in events if t_ in ("home", "launch")]   # noqa: E731
        assert stage_ev() == [("launch", "starting"), ("launch", "loading")], events   # (the page grows it on starting)
        sh._set(surface="web", active="netflix", active_tab="netflix")   # over a tab or an app (voice, the phone, an
        events.clear()                                                   # agent): the launcher first, as for a launch
        assert sh.open(k)["ok"] and sh.surface == "launcher" and ("activate", 18) not in fx.log
        assert stage_ev() == [("home", None), ("launch", "starting"), ("launch", "loading")], events
        # ("quit it", "quit the game": the card in front is the game whose loading screen is up, the launcher pinned
        # over it, as the page says (ui launch); once the page took that screen down (Home, Back, B, Esc, the switcher,
        # Settings, coming back into view: tests/switcher_check.py), none: the launcher)
        assert sh.open(k)["ok"] and sh.surface == "launcher"
        for up in (k, None):
            sh.ui_report(None, None, "home", up)
            assert sh.front_key() == up, up
        sh._tick(fx)
        assert fx.pinned == {1} and k in sh.pending
        fx.iconic = lambda w: w == 1
        sh._tick(fx)
        assert fx.pinned == set()
        del fx.iconic
        sh._tick(fx)
        fx.stack.append(8)
        fx.log = []
        sh._tick(fx)
        assert fx.pinned == {1} and fx.log == [("activate", 1)], fx.log
        events.clear()                           # quit while it loads ("quit Cyberpunk": its splash is up): it goes too
        assert sh.close_app(k, person=True)["state"] == "closing" and k not in sh.pending and fx.deleted == [18]
        assert ("launch", {"id": k, "stage": "failed"}) in events, events
        sh._closing = {}
        # (e) by its window: launch.sh writes its line before the game runs, so a window and no line since Steam started
        # it means no config, said on the splash (at NOCONFIG_S it would be behind the game)
        fx, sh._wins = FakeX((3, 1)), {}
        fx.front = 1
        sh.pending[k] = e = {"until": 9e9, "tries": 0, "at": 1.0}
        sh._noconfig["1091500"] = time.monotonic() - 3
        under(18)
        events.clear()
        sh._tick(fx)
        assert said("warn") == ["Cyberpunk 2077 started without its GameSpark config"] and not sh._noconfig
        # the guards: Home's (Steam answering the Guide press) also while a launch other than Big Picture's is pending;
        # Big Picture's own launch keeps today's raise of Steam's windows; never over a game (the kiosk iconified)
        fx, sh._wins, sh.log_running = FakeX((2, 1)), {}, set()
        sh.pending = {"py": {"until": 9e9, "tries": 0, "at": 1.0}}
        sh.home()
        fx.front = 2
        sh._tick(fx)
        assert fx.log[-1] == ("activate", 1), fx.log
        sh.pending = {"bigpicture": {"until": 9e9, "tries": 0, "at": 1.0}}
        sh.home()
        fx.front, fx.log = 2, []
        fx.stack.insert(0, 16)                   # (and a Steam dialog while Big Picture starts: raised, as before)
        sh._tick(fx)
        assert ("activate", 1) not in fx.log and ("activate", 16) in fx.log, fx.log
        fx.stack.remove(16)
        sh.pending, sh._home_guard, sh._steam_guard = {}, 0.0, time.monotonic() + 60
        fx.iconic, fx.front, fx.log = (lambda w: w == 1), 7, []
        fx.stack.append(7)
        sh._tick(fx)
        assert fx.log == [], fx.log
        # Steam refused the launch: the splash fails, and what Steam shows (a sign-in) comes up, unpinned first: it was
        # under the pin. It stays up: the guard of a Steam the launch started (b) doesn't push it back
        del fx.iconic
        sh._steam_guard, sh._groups = time.monotonic() + 60, {"x:steam": [7]}
        sh.pending[k] = e = {"until": 9e9, "tries": 0, "at": 1.0}
        t = sh.tile(k)
        sh._steam = lambda t: (False, "Steam didn't accept the launch")
        events.clear()
        Shell._launch(sh, t, e)
        assert k not in sh.pending and said("error") == ["Couldn't start Cyberpunk 2077: Steam didn't accept the launch"]
        assert ("launch", {"id": k, "stage": "failed"}) in events and fx.log[-2:] == [("above", 1, False),
                                                                                       ("activate", 7)], fx.log
        sh._tick(fx)
        assert fx.log[-1] == ("activate", 7) and fx.front == 7, fx.log
        sh.pending[k] = e2 = {"until": 9e9, "tries": 0, "at": 1.0}
        events.clear(), fx.log.clear()
        Shell._launch(sh, t, e)                  # (a stale launch refused: a toast, never the new one's splash)
        assert sh.pending[k] is e2 and fx.log == [] and [x for _, x in events if "stage" in x] == [], events
        sh._steam = lambda t: (True, None)
        Shell._launch(sh, t, e)                  # (a stale launch's thread: its entry is gone, this one isn't touched)
        Shell._launch(sh, t, e2)
        del sh._steam
        assert "sent" in e2 and "sent" not in e, (e, e2)
        events.clear()                           # (a stale tick, too, writes and fails only the entry it read)
        assert sh._fail(k, e, "x") is None and sh.pending[k] is e2 and events == []
        sh._put(k, e, {}, go=1.0)
        assert "go" not in e2
        # two launches holding (OK on Cyberpunk, Home, OK on Divinity): the loading screen up is the later one's, so
        # only it is handed over; the earlier one, drawn first, stays behind it, its launch done (the kiosk still
        # pinned, nothing activated). OK on the earlier one again makes it the one handed over
        fx, sh._wins, sh.pending, sh.log_running = FakeX((3, 1)), {}, {}, set()
        fx.front, k2 = 1, "steam:435150"
        sh._tick(fx)
        assert sh.open(k)["ok"] and sh.home()["ok"] and sh.open(k2)["ok"]
        e, e2 = sh.pending[k], sh.pending[k2]
        under(18)
        sh._tick(fx)
        e["seen"] -= SETTLE_S
        fx.log = []
        sh._tick(fx)
        assert k not in sh.pending and fx.log == [] and fx.pinned == {1}, (fx.log, sh.pending)
        under(5)
        sh._tick(fx)
        e2["seen"] -= SETTLE_S
        sh._tick(fx)
        assert "go" in e2 and fx.log[-3:] == [("above", 1, False), ("activate", 5), ("iconify", 1)], fx.log
        fx, sh._wins, sh.pending = FakeX((3, 1)), {}, {}
        fx.front = 1
        sh._tick(fx)
        assert sh.open(k)["ok"] and sh.open(k2)["ok"] and sh.open(k)["ok"]   # (the third: a re-press of the first)
        e, e2 = sh.pending[k], sh.pending[k2]
        under(5), under(18)
        sh._tick(fx)
        e["seen"] -= SETTLE_S
        e2["seen"] -= SETTLE_S
        fx.log = []
        sh._tick(fx)
        assert "go" in e and k2 not in sh.pending and ("activate", 5) not in fx.log and ("activate", 18) in fx.log
        # ...until the later one ends with nothing handed over (Steam refused it, GameSpark's FEX build is missing, its
        # Starting… card was closed, it quit right after starting), or its game has quit since (from its own menu: the
        # launcher its hand-over iconified comes back, pinned): the earlier one then comes up once drawn, as alone
        fex = "2026-10-10 05:00:01 app 435150 applied: 435150.env (FEX: /x (missing)) not launched: x"
        for end in ("refused", "fex", "closed", "quit", "exited"):
            fx, sh._wins, sh.pending = FakeX((3, 1)), {}, {}
            fx.front = 1
            sh._tick(fx)
            assert sh.open(k)["ok"] and sh.home()["ok"] and sh.open(k2)["ok"]
            e, e2 = sh.pending[k], sh.pending[k2]
            if end == "refused":
                sh._steam = lambda t: (False, "Steam didn't accept the launch")
                Shell._launch(sh, sh._tiles[k2], e2)
                del sh._steam
            elif end == "fex":
                sh._launch_line(fex)
            elif end == "closed":
                assert sh.close_app(k2, person=True)["ok"]
            elif end == "quit":
                e2.pop("at")
                e2["gone"] = time.monotonic() - EXIT_S
            else:                                # (up, then its game quit: its window gone, Steam's log empty)
                under(5)
                sh._tick(fx)
                e2["seen"] -= SETTLE_S
                sh._tick(fx)
                sh._tick(fx)
                assert "go" in e2 and k2 not in sh.pending and fx.iconic(1)
                fx.stack.remove(5)
                sh._tick(fx)
                sh._tick(fx)
                assert fx.front == 1 and not fx.iconic(1) and fx.pinned == {1}, (fx.log, fx.icons)
            under(18)
            sh._tick(fx)
            e["seen"] -= SETTLE_S
            events.clear()
            sh._tick(fx)
            assert "go" in e and fx.log[-2:] == [("activate", 18), ("iconify", 1)], (end, fx.log)
            assert ("launch", {"id": k, "stage": "up"}) in events, (end, events)
        # The focus while it loads: the game's window, given it back once (above), took it again (a game activating
        # itself). A hold that then ends with nothing handed over (quit while loading; left behind a later launch that
        # failed since) gives it back to the kiosk: the launcher shows, so the surface is "launcher" (Siri's moves, the
        # page), never the hidden game's "app", and the pad and the keys go to the page. Not when the kiosk isn't what
        # shows: a window on top of it (one the game made "above" too, as the pin) keeps it
        for end in ("quit", "behind", "covered"):
            fx, sh._wins, sh.pending, sh._closing = FakeX((3, 1)), {}, {}, {}
            fx.front = 1
            sh._tick(fx)
            assert sh.open(k)["ok"] and (end != "behind" or sh.home()["ok"] and sh.open(k2)["ok"])
            e = sh.pending[k]
            under(18)
            fx.front = 18
            sh._tick(fx)
            fx.front = 18
            sh._tick(fx)
            if end == "quit":
                assert sh.close_app(k, person=True)["state"] == "closing"
            elif end == "behind":
                e["seen"] -= SETTLE_S
                sh._tick(fx)                     # (drawn: left behind Divinity's loading screen, still pinned)
                assert k not in sh.pending and fx.pinned == {1} and fx.front == 18, (sh.pending, fx.pinned)
                sh._launch_line(fex)
            else:
                fx.stack.append(8)
                fx.front = 8
                sh._fail(k, e, "it didn't start within 5 minutes")
            fx.log = []
            time.sleep(.26)
            sh._tick(fx)
            assert fx.log == [("above", 1, False)] + [("activate", 1)] * (end != "covered"), (end, fx.log)
            assert sh.state()["surface"] == ("app" if end == "covered" else "launcher"), (end, sh.state())
        sh._closing = {}
        # a running game resumed from its tile meanwhile (not one still loading) doesn't keep it back (README, Games):
        # the kiosk iconified, nothing is pinned, its window maps on top and it is handed over once drawn
        fx, sh._wins, sh.pending = FakeX((3, 5, 1)), {}, {}
        fx.front = 1
        sh._tick(fx)
        assert sh.open(k)["ok"] and sh.home()["ok"] and sh.open(k2)["ok"] and fx.log[-2:] == [("activate", 5),
                                                                                              ("iconify", 1)]
        e, fx.iconic = sh.pending[k], (lambda w: w == 1)
        fx.stack.append(18)
        sh._tick(fx)
        e["seen"] -= SETTLE_S
        sh._tick(fx)
        assert "go" in e and fx.pinned == set() and fx.log[-2:] == [("activate", 18), ("iconify", 1)], fx.log
        # two launches waiting on Steam (OK on Cyberpunk, its EULA raised; Home; OK on Divinity): the covered EULA
        # holds, so Divinity's loading screen stays pinned; Divinity's Launching... is never Cyberpunk's prompt (it came
        # after Divinity began), nor the EULA Divinity's (Cyberpunk raised it)
        fx, sh._wins, sh.pending, sh.log_running = FakeX((3, 7, 1)), {}, {}, set()
        fx.front = 1
        sh._tick(fx)
        assert sh.open(k)["ok"]
        e = sh.pending[k]
        e["sent"] = time.monotonic() - PROMPT_S
        under(17)
        sh._tick(fx)
        assert e["prompt"] == 17 and fx.front == 17 and sh.home()["ok"] and sh.open(k2)["ok"]
        e2 = sh.pending[k2]
        e2["sent"] = time.monotonic()
        under(16)
        fx.log = []
        sh._tick(fx), sh._tick(fx)
        assert fx.log == [("above", 1, True)] and fx.front == 1, fx.log
        fx.stack.remove(16)
        e2["sent"] -= PROMPT_S                   # (Divinity waits on Steam too: on its own window only)
        sh._tick(fx)
        assert fx.log == [("above", 1, True)] and "prompt" not in e2, fx.log
        under(16)
        sh._tick(fx)
        assert (e["prompt"], e2["prompt"]) == (17, 16) and fx.log[-2:] == [("above", 1, False), ("activate", 16)]
        # an app played meanwhile, a Steam prompt raised over it, then the app quits (from its own menu, a crash) before
        # the prompt is answered: the prompt stays on top, unpinned (Steam waits on it); once it is answered, the
        # launcher comes back, pinned, as for a game quit while another loads
        fx, sh._wins, sh.pending, sh._log_removed = FakeX((3, 7, 1, 10)), {}, {}, False
        fx.front, fx.icons, sh.apps["py"] = 10, {1}, {"proc": None, "win": 10}   # (the kiosk iconified under it)
        sh._tick(fx)
        sh.pending[k] = e = {"until": 9e9, "tries": 0, "at": 1.0, "t0": 1.0, "sent": time.monotonic() - PROMPT_S}
        fx.stack.insert(0, 17)
        sh._tick(fx)
        assert e["prompt"] == 17 and fx.front == 17 and fx.iconic(1) and fx.pinned == set(), fx.log
        fx.stack.remove(10)
        n = len(fx.log)
        sh._tick(fx), sh._tick(fx)
        assert fx.front == 17 and fx.iconic(1) and fx.pinned == set() and fx.log[n:] == [], fx.log[n:]
        fx.stack.remove(17)                      # answered: its process
        sh.log_running, e["added"] = {"1091500"}, time.monotonic()
        sh._tick(fx), sh._tick(fx)
        assert fx.front == 1 and not fx.iconic(1) and fx.pinned == {1}, fx.log[n:]
        del sh.apps["py"]
        # a ghost (Steam's log missed a 'removed': it still lists the game) is no process of this launch: Steam's prompt
        # comes over the splash at PROMPT_S, and with no Steam window the launch fails at NOPROC_S
        fx, sh._wins, sh.pending, sh.log_running = FakeX((3, 1)), {}, {}, {"1091500"}
        fx.front = 1
        sh.pending[k] = e = {"until": 9e9, "tries": 0, "at": 1.0, "sent": time.monotonic() - PROMPT_S}
        under(17)
        sh._tick(fx)
        assert e.get("prompt") == 17 and fx.front == 17 and fx.pinned == set(), fx.log
        fx.stack.remove(17)
        e["sent"] -= NOPROC_S
        sh._tick(fx)
        assert k not in sh.pending and said("error")[-1] == "Couldn't start Cyberpunk 2077: Steam started nothing in a " \
                                                            "minute", said("error")
    finally:
        globals()["X11"], sh.x_ok = real_x11, False
    sh._wins, sh._seen, sh._kwin, sh._tile_wins, sh._prev_running = {}, set(), None, {}, set()
    sh.pending, sh.log_running, sh._groups, sh._steam_guard, sh._home_guard = {}, set(), {}, 0.0, 0.0
    sh._set(running=set())
    # demo: simulated state machine + art + stats
    events.clear()
    d = Shell(base, True, lambda t, d: events.append((t, d)))
    t = d.tiles()
    assert t["demo"] and [x["id"] for x in t["rows"][0]["tiles"]][2] == "steam:1091500"
    assert d.tile("steam:1091500")["art"]["hero"] == "/art/demo/1091500/hero" and d.tile("retroarch")
    assert "<svg" in demo_art("1091500", "hero") and "Cyberpunk" in demo_art("1091500", "logo") and demo_art("1", "hero") is None
    lc = tmp / "ds" / "appcache" / "librarycache" / "1091500"   # demo art copied from a real library wins, per kind
    lc.mkdir(parents=True)
    (lc / "library_hero.jpg").write_bytes(b"jpg")
    d2 = Shell({**base, "settings": {**base["settings"], "demo_steam_root": str(tmp / "ds")}}, True, lambda *_: None)
    assert d2.tile("steam:1091500")["art"]["hero"] == "/art/steam/1091500/hero" and d2.art_file("1091500", "hero")
    assert d2.tile("steam:1091500")["art"]["logo"] == "/art/demo/1091500/logo"
    assert d.navigate("right", 2) == {"ok": True, "surface": "launcher"} and ("nav", {"dir": "right", "count": 2}) in events
    assert d.navigate("sideways")["ok"] is False and d.navigate("up", 21)["ok"] is False
    assert d.type_text("hi") == {"ok": False, "error": "nothing to type into"}
    assert d.open("netflix") == {"ok": True, "surface": "web"} and d.state()["surface"] == "web"
    assert d.read_screen()["tile"] == "netflix" and d.type_text("hi")["ok"]
    for k in ("youtube", "disney", "max", "prime"):
        d.open(k)
    assert d.state()["tabs"] == ["youtube", "disney", "max", "prime"]          # max_web_tabs 4: netflix closed (LRU)
    assert d.open("steam:1091500")["surface"] == "app" and ("launch", {"id": "steam:1091500", "stage": "starting"}) \
        in events   # (its stages, as the Spark sends them: its splash says Starting…, so no toast does)
    assert ("toast", {"text": "Starting Cyberpunk 2077…", "kind": "info"}) not in events
    stages, D = [], type("D", (), {"emit": lambda self, t, x: stages.append(x["stage"]), "running": {"steam:1"},
                                   "_closing": {}, "_demo_runs": {"steam:1": 1}})
    Shell._demo_launch(D(), "steam:1", 1, ((s, 0) for s in ("loading", "up")))
    assert stages == ["loading", "up"] and d.tile("steam:4078430")["profile"].startswith("Nanite off")
    for gone in ({"running": set()}, {"_closing": {"steam:1": {"at": 0.0}}}):   # quit while it loads (exited, or
        stages.clear()                                                         # asked): failed, its splash goes
        Shell._demo_launch(type("Q", (D,), gone)(), "steam:1", 1, ((s, 0) for s in ("loading", "up")))
        assert stages == ["failed"], (gone, stages)
    stages.clear()                               # quit, then opened again before its next stage: that launch's stages
    Shell._demo_launch(D(), "steam:1", 0, ((s, 0) for s in ("loading", "up")))   # follow, never this one's
    assert stages == [] and d._demo_runs["steam:1091500"], stages   # (d.open's run: its thread)
    assert "profile" not in d.tile("steam:1174180")   # (the demo library's profiles: GameSpark's four of its six)
    s = d.state()
    assert s["surface"] == "app" and s["active"] == "steam:1091500" and s["running"] == ["steam:1091500"]
    assert d.home() == {"ok": True, "surface": "launcher"} and d.state()["running"] == ["steam:1091500"]
    assert ("home", {}) in events and any(e == "state" for e, _ in events)
    assert d.search("youtube", "lofi beats")["ok"] and d.state()["active"] == "youtube"
    assert d.search("disney", "x")["ok"] is False and d.search("nope", "x")["ok"] is False
    assert d.volume("up")["volume"] == 55 and d.volume(20)["volume"] == 20 and d.volume("loud")["ok"] is False
    assert [d.volume(c)["muted"] for c in ("mute", "up", "unmute")] == [True, True, False]
    assert d.media("toggle")["ok"] and d.media("rewind")["ok"] is False
    assert d.screenshot().startswith(b"\x89PNG\r\n\x1a\n")
    events.clear()
    d.home(), d.navigate("down"), d.home()
    assert [e for e, _ in events].count("home") == 2          # a quick second Home (agents, phone) is never dropped
    assert d.read_screen()["surface"] == "launcher" and d.click("Netflix")["surface"] == "web"
    s1, s2 = d.stats(), d._fake_stats()
    assert 2 <= s2["gpu"]["util"] <= 97 and s1["mem"]["total_gb"] == 119.7 and "T" in s1["time"]
    d.ui_report("netflix", "top", "home")      # (Settings is open from here, not from the page's report 100 ms later)
    assert d.open("settings")["surface"] == "launcher" and ("focus", {"id": "settings"}) in events
    assert d.ui == {"focused": "netflix", "row": "top", "screen": "settings", "launch": None}
    assert d.open("retroarch")["ok"] and ("toast", {"text": "Starting RetroArch…", "kind": "info"}) in events
    # the switcher's settings: a bad value (quoted, null, not an object) is its default, never a daemon that can't start
    # (Restart=always would loop it) or a window watch that stops at its first use
    for bad in ({"double_press_ms": "300", "stuck_s": None}, True):
        d3 = Shell({**base, "settings": {**base["settings"], "switcher": bad}}, True, lambda *_: None)
        assert (d3.double_s, d3.stuck_s) == (.3, 6), (bad, d3.double_s, d3.stuck_s)
    # app switcher, press(): Home at once; the same button again within 300 ms opens the switcher instead and the next
    # press is a first press; order-tolerant (_bg threads), X-time-wrap safe; a Siri press queued behind Home pairs
    sw, rec = Shell(base, True, lambda t, d: events.append((t, d))), []
    sw._home, sw._switcher = (lambda: rec.append("home")), (lambda: rec.append("switcher"))

    def presses(*ts, src="guide"):
        rec.clear()
        sw._press = {}
        for t in ts:
            sw.press(src, t)
        return rec[:]
    assert presses(0, .14) == ["home", "switcher"] and presses(0, .35) == ["home", "home"]
    assert presses(.14, 0) == ["home", "switcher"]
    assert presses(0, .14, .28, .42) == ["home", "switcher"]   # pressed fast and often: the presses within the page's
    sw._press["guide"] = (None, time.monotonic() - .5)          # HOME_GRACE after a double are swallowed (it drops
    rec.clear()                                                 # that Home), never a reopened switcher; the first
    sw.press("guide", 9.0)                                      # press after it goes Home
    assert rec == ["home"], rec
    assert presses(4294967.2, .1, src="key") == ["home", "home"]
    rec.clear()
    sw._press = {}
    sw.press("guide", 0), sw.press("key", .1)
    assert rec == ["home", "home"]                           # two buttons never pair
    rec.clear()
    sw._press = {"tv": (time.monotonic() - .5, time.monotonic() - .005)}   # queued: entered 5 ms after a 500 ms Home
    sw.press("tv")
    sw._press = {"tv": (time.monotonic() - .4, time.monotonic() - .05)}    # a re-press 50 ms after the Home's end
    sw.press("tv")
    assert rec == ["switcher", "home"], rec
    rec.clear()
    sw._home = lambda: rec.append("home") or time.sleep(.35)              # the real FIFO: a 350 ms Home, press 2 behind it
    sw._press = {}
    sw.press("tv"), sw.press("tv")
    assert rec == ["home", "switcher"], rec
    rec.clear()                                  # ...though an agent's next call, queued during that Home, goes first
    sw._press, held, go = {}, threading.Event(), threading.Event()   # (the lock hands over in turn; 400 ms): a press
    def agent():                                                    # is timed from when it asked for the lock
        with sw.lock:
            held.set(), go.wait(2)
        with sw.lock:                            # (asked at once)
            time.sleep(.4)
    a = threading.Thread(target=agent)
    a.start(), held.wait(2)
    s = threading.Thread(target=lambda: (sw.press("tv"), sw.press("tv")))  # (siri.py: one thread, press 2 queued)
    s.start(), until(lambda: sw.lock._queue), go.set(), s.join(), a.join()
    assert rec == ["home", "switcher"], rec
    rec.clear()                                  # ...as is one pressed 0.1 s after the first that waited out an agent's
    sw._press, sw._home = {}, lambda: rec.append("home")   # call begun in between (0.4 s)
    sw.press("tv")
    held.clear()
    a = threading.Thread(target=lambda: sw.lock.acquire() and (held.set(), time.sleep(.4), sw.lock.release()))
    a.start(), held.wait(2), time.sleep(.1), sw.press("tv"), a.join()
    assert rec == ["home", "switcher"], rec
    del sw._home, sw._switcher
    sw._press = {}                               # (that double's grace)

    def siri_thread(fn, *a):                     # siri.py runs its actions on this thread
        th = threading.Thread(target=fn, args=a, name="siri-actions")
        th.start()
        th.join()
    homes = lambda: [e for e, _ in events if e in ("home", "switcher")]
    events.clear()
    siri_thread(sw.home), time.sleep(.14), siri_thread(sw.home)
    assert homes() == ["home", "switcher"] and sw.ui["screen"] == "switcher", homes()
    events.clear()
    sw.home(), time.sleep(.14), sw.home()                    # agents, the phone, the page: never a double
    assert homes() == ["home", "home"], homes()
    sw.open("netflix")
    sw._press, _ = {}, events.clear()
    siri_thread(sw.back), siri_thread(sw.home)               # Menu reaching Home is no TV press
    assert homes() == ["home", "home"], homes()
    src = (REPO / "sparkhome" / "siri.py").read_text()       # the shim's two facts (see home())
    assert 'name="siri-actions"' in src and '"tv": ("home",)' in src
    events.clear()
    siri_thread(sw.navigate, "up"), sw.navigate("up")
    assert [x for e, x in events if e == "nav"] == [{"dir": "up", "count": 1, "touch": True}, {"dir": "up", "count": 1}]
    sw.swipe("up", 4499.9), sw.swipe("down", 9e3), sw.swipe("up", 4500)
    sw.surface = "web"
    sw.swipe("up", 9e3)
    sw.surface = "launcher"
    assert [x for e, x in events if e == "swipe"] == [{"dir": "up", "speed": 4500}]  # up, fast, over the launcher only
    # the cards: most recent first, `from` the app TV TV left, the card record; demo snapshots of games left
    sw = Shell(base, True, lambda t, d: events.append((t, d)))
    for k in ("steam:1174180", "steam:435150"):
        sw.open(k), sw.home()
    sw.open("netflix")
    events.clear()
    siri_thread(sw.home), siri_thread(sw.home)
    op = next(x for e, x in events if e == "switcher")
    assert [a["key"] for a in op["apps"]] == ["netflix", "steam:435150", "steam:1174180"] and op["from"] == "netflix"
    assert op["apps"][2] == {"key": "steam:1174180", "title": "Red Dead Redemption 2", "kind": "game", "state": "open",
                             "tile": sw.tile("steam:1174180"), "windows": 2, "quitting": False, "game": True,
                             "can_close": True, "can_force": True,
                             "snap": f"/api/snap/steam%3A1174180?v={sw._snap_n - 1}"}, op["apps"][2]
    assert op["apps"][0]["kind"] == "tab" and not op["apps"][0]["can_force"] and op["apps"][0]["snap"] is None
    assert sw.snap("steam:1174180").startswith(b"\x89PNG") and sw.snap("netflix") is None
    again = Shell(base, True, lambda *_: None)   # a restarted daemon's snapshot of it is a new URL (the kiosk's Chrome
    again.open("steam:1174180"), again.home()    # cached the old one's for a day)
    assert again.running_apps()[0]["snap"] != op["apps"][2]["snap"], again.running_apps()[0]["snap"]
    sw._left = ("netflix", time.monotonic() - 2.5)          # opened a while after leaving: no `from`
    assert sw.switcher() == {"ok": True, "surface": "launcher",
                             "open": ["Netflix", "Divinity: Original Sin 2", "Red Dead Redemption 2"]}
    assert events[-1] == ("switcher", {"open": True, "apps": op["apps"], "from": None}), events[-1]
    # closing: a tab at once (an agent may); a game only for a person, idempotent, gone 1.2 s later (demo); RDR2 (demo)
    # ignores it: closing → stuck → force → gone; each change refreshes the open switcher
    card = lambda k: next((c for c in sw.running_apps() if c["key"] == k), None)
    sw.stuck_s = .3
    events.clear()
    assert sw.close_app("netflix") == {"ok": True, "state": "closed", "title": "Netflix"} and "netflix" not in sw.tabs
    assert [x["apps"][0]["key"] for e, x in events if e == "switcher"] == ["steam:435150"]          # a refresh
    assert sw.close_app("steam:435150")["error"].startswith("Games and apps quit only when someone at the TV asks")
    assert sw.close_app("steam:435150", force=True)["ok"] is False and card("steam:435150")["state"] == "open"
    assert sw.close_app("steam:435150", person=True) == {"ok": True, "state": "closing", "title": "Divinity: Original Sin 2"}
    assert sw.close_app("steam:435150", person=True)["state"] == "closing" and card("steam:435150")["state"] == "closing"
    assert until(lambda: "steam:435150" not in sw.running) and card("steam:435150") is None
    assert sw.close_app("steam:1174180", person=True)["state"] == "closing"
    assert until(lambda: card("steam:1174180")["state"] == "stuck")
    assert sw.close_app("steam:1174180", person=True) == {"ok": False, "force": True,
                                                         "error": "Red Dead Redemption 2 didn't quit when asked."}
    assert sw.close_app("steam:1174180", force=True, person=True)["state"] == "forcing"
    assert until(lambda: card("steam:1174180") is None) and sw.snap("steam:1174180") is None
    assert sw.close_app("steam:1174180", person=True) == {"ok": False, "error": "Red Dead Redemption 2 isn't open"}
    sw.open("youtube")
    assert sw.close_app("youtube")["state"] == "closed" and sw.state()["surface"] == "launcher"     # in front: Home
    sw.open("bigpicture"), sw.home()
    assert card("bigpicture")["kind"] == "system" and not card("bigpicture")["can_force"]
    assert sw.close_app("bigpicture", force=True, person=True)["error"] == \
        "Couldn't force quit Steam Big Picture: only games and apps can be force quit."
    assert sw.close_app("bigpicture")["state"] == "closing"                                 # an agent may
    sw.open("xcloud"), sw.home()                 # a cloud game's tab (a web tile on the Games row): closing it ends the
    assert sw.close_app("xcloud")["error"].startswith("Games and apps quit only") and "xcloud" in sw.tabs   # game:
    assert sw.close_app("xcloud", person=True)["state"] == "closed" and "xcloud" not in sw.tabs   # a person's call
    for k in ("xcloud", "netflix", "youtube", "disney", "max", "prime"):   # nor do an agent's opens past max_web_tabs
        sw.open(k)                                                        # (4) end it: the oldest others close
    assert list(sw.tabs) == ["xcloud", "disney", "max", "prime"], list(sw.tabs)
    # the Home hotkey grab (XF86HomePage, F14): each press goes to press("key") with its X server time (s); an
    # auto-repeat (release + press at one timestamp) is no press

    def key(typ, t):
        e = XEvent()
        e.type, e.xkey.keycode, e.xkey.time = typ, 200, t
        return e

    class Xq:                                    # _x_events' view of Xlib: a queue of events
        def __init__(self, evs):
            self.q = evs
        XPending = lambda self, d: len(self.q)

        def XNextEvent(self, d, ref):            # (ref: byref(ev); the event is copied into ev)
            ctypes.pointer(ref._obj)[0] = self.q.pop(0)

        def XPeekEvent(self, d, ref):
            ctypes.pointer(ref._obj)[0] = self.q[0]
    got = []
    sw.press = lambda src, t=None: got.append((src, t))
    q = Xq([key(2, 4294967200), key(3, 4294967290), key(2, 4294967290), key(3, 4294967300), key(2, 4294967440)])
    sw._x_events(type("X11", (), {"X": q, "d": None})(), {200: "home"})
    assert until(lambda: len(got) == 2) and sorted(got) == [("key", 4294967.2), ("key", 4294967.44)], got
    del sw.press
    # the injected bridge: a nav mode per tile (leanback key codes, the page's own pad, or the daemon), Guide → binding
    assert "__NAV__" in BRIDGE_JS and "32768 + b" in BRIDGE_JS and "'28de'" in BRIDGE_JS and "i <= 16 &&" in BRIDGE_JS
    assert "__TILES__" in BRIDGE_JS and "window.__sparkhomeTiles" in ELS_JS and "window.__sparkhomeTiles" in SKIN_JS
    # tab titles: live from Chrome (no event on document.title changes), the event cache only when Chrome can't answer

    class FakeCDP:                               # records (method, session or target); answers every call alike
        targets, up, connected = {"T1": {"title": "example.com"}}, True, threading.Event()

        def __init__(self):
            self.log, self.kw = [], {}
            self.connected.set()

        def call(self, method, session=None, **kw):
            if not self.up:
                raise ConnectionError("browser not connected")
            self.log.append((method, session or kw.get("targetId")))
            self.kw = kw
            return {"sessionId": "S" + kw.get("targetId", ""), "result": {"value": "Library"},
                    "targetInfo": {"targetId": kw.get("targetId"), "title": "Example Domain"}}
    sh.cdp = cdp = FakeCDP()
    assert sh._page_title({"target": "T1"}) == "Example Domain" and sh._page_title({"target": None}) is None
    cdp.up = False
    assert sh._page_title({"target": "T1"}) == "example.com"
    cdp.up = True
    # the TV skin rides on the bridge, for a "pointer" tile that asks for it (tests/skin_check.py drives it in Chrome)
    sh._attach({"skin": {"hide": ["#promo"]}, "tiles": ".card"}, "T9")
    assert "__sparkhomeSkin" in cdp.kw["source"] and '{"hide": ["#promo"]}' in cdp.kw["source"]
    assert 'let tiles = ".card";' in cdp.kw["source"]
    assert json.dumps(FOCUSABLE) in cdp.kw["source"] and "__SKIN__" not in cdp.kw["source"]
    for t in ({"skin": True, "nav": "keys"}, {"skin": True, "nav": "pad"}, {}, {"skin": False}):
        sh._attach(t, "T9")
        assert "__sparkhomeSkin" not in cdp.kw["source"] and 'let tiles = "";' in cdp.kw["source"], t
    for t in ({"skin": True}, {"skin": {}}):     # an empty object is the plain skin too, not none
        sh._attach(t, "T9")
        assert "const CFG = {}," in cdp.kw["source"], t
    # a tile's popup is set up like the tile and driven instead of it; Back closes it, and the tile is back in front
    sh.launcher, sh.active_tab, sh.surface = {"target": "L", "session": "SL"}, "up", "web"
    sh.tabs["up"] = {"target": "T1", "session": "ST1"}

    def popup(p):                                # (the CDP reader adds a target before its event)
        cdp.targets[p] = {}
        sh._cdp_event({"method": "Target.targetCreated",
                       "params": {"targetInfo": {"targetId": p, "type": "page", "openerId": "T1"}}})
    def gone(p):
        cdp.targets.pop(p, None)
        sh._cdp_event({"method": "Target.targetDestroyed", "params": {"targetId": p}})
    popup("P1")
    assert until(lambda: sh.tabs["up"].get("popup") == {"target": "P1", "session": "SP1"}) and sh._session() == "SP1"
    cdp.log.clear()
    assert sh.back()["ok"] and "popup" not in sh.tabs["up"] and sh._session() == "ST1"     # untracked at once
    assert cdp.log == [("Target.closeTarget", "P1"), ("Target.activateTarget", "T1"), ("Page.bringToFront", "ST1")]
    gone("P1")                                   # its targetDestroyed: nothing left to do
    assert not until(lambda: len(cdp.log) > 3, 0.2), cdp.log
    # the sleep warning's words go on the web app in front with its dim (no user activation), and off again with the
    # undim on that page, wherever the front is by then; none over the launcher (its own page says them) or a game
    cdp.log.clear()
    sh._sleep = "warning"
    sh._dim(True)
    assert cdp.log == [("Runtime.evaluate", "ST1")] and cdp.kw["expression"] == WARN_JS.replace("__N__", "10")
    assert cdp.kw["userGesture"] is False and cdp.kw["timeout"] == 2
    sh._sleep, sh.surface = "awake", "launcher"
    sh._dim(False)
    assert cdp.log[-1] == ("Runtime.evaluate", "ST1") and cdp.kw["expression"].endswith("})(0)") and not sh._warn_tab
    for sh.surface in ("launcher", "app"):
        sh._tab_warning(True), sh._tab_warning(False)
    assert len(cdp.log) == 2, cdp.log
    sh.surface = "web"
    # this thread never waits for the lock (an action holding it may wait on a dialog only this thread answers)
    n0, held, free = threading.active_count(), threading.Event(), threading.Event()
    sh.tabs["x"] = {"target": "TX", "session": "STX"}
    threading.Thread(target=lambda: sh.lock.acquire() and (held.set(), free.wait(5), sh.lock.release()), daemon=True).start()
    held.wait(2)
    t0 = time.monotonic()
    popup("P9"), gone("P9"), gone("TX")
    sh._cdp_event({"method": "Target.targetInfoChanged", "params": {"targetInfo": {"targetId": "T1"}}})
    assert time.monotonic() - t0 < 0.5
    free.set()                                   # P9 closed before it was stored: it doesn't stay tracked
    assert until(lambda: threading.active_count() <= n0) and "popup" not in sh.tabs["up"] and "x" not in sh.tabs
    # Home waiting for the lock goes next, not after every page call the agent sends back to back (a busy page: 2 s
    # each). (Run on CPython 3.12 or 3.13 to see an RLock fail this: from 3.14 it hands off too.)
    order, waiting = [], threading.Event()
    sh.lock.acquire()
    home = threading.Thread(target=lambda: (waiting.set(), sh.lock.acquire(), order.append("home"), sh.lock.release()))
    home.start(), waiting.wait(2), time.sleep(0.05)
    sh.lock.release(), sh.lock.acquire(), order.append("agent"), sh.lock.release()
    home.join(2)
    assert order == ["home", "agent"], order
    # a popup is in front from its targetCreated on, in event order: while it is set up, actions get "not ready"
    # instead of reaching the tile behind it, and an OAuth hop (it opens the next window, then closes) keeps the next
    held, free, slow = threading.Event(), threading.Event(), threading.Event()
    threading.Thread(target=lambda: sh.lock.acquire() and (held.set(), free.wait(5), sh.lock.release()), daemon=True).start()
    held.wait(2)
    real_call, cdp.call = cdp.call, lambda m, session=None, **kw: (m != "Page.enable" or slow.wait(5)) and \
        real_call(m, session, **kw)            # (a setup that takes a while: Page.enable answers late)
    popup("P7")
    assert sh._front()["target"] == "P7"
    try:                                         # (actions now say so instead of keying the tile behind it)
        sh._session()
        raise AssertionError("a popup being set up has no session yet")
    except ConnectionError:
        pass
    cdp.targets["P8"] = {}
    sh._cdp_event({"method": "Target.targetCreated",
                   "params": {"targetInfo": {"targetId": "P8", "type": "page", "openerId": "P7"}}})
    gone("P7")
    slow.set(), free.set()
    assert until(lambda: sh.tabs["up"].get("popup") == {"target": "P8", "session": "SP8"}), sh.tabs["up"]
    cdp.call = real_call
    gone("P8")
    assert until(lambda: "popup" not in sh.tabs["up"])
    slow.clear()                                 # Back while a popup is still being set up closes it (no session needed)
    cdp.call = lambda m, session=None, **kw: (m != "Page.enable" or slow.wait(5)) and real_call(m, session, **kw)
    popup("P5")
    cdp.log.clear()
    assert sh.back()["ok"] and "popup" not in sh.tabs["up"] and cdp.log[0] == ("Target.closeTarget", "P5"), cdp.log
    slow.set()

    def gone_early(m, session=None, **kw):       # a popup gone before its setup: the attach fails, it's untracked
        if m == "Target.attachToTarget" and kw.get("targetId") == "P6":
            raise RuntimeError("No target with given id found")
        return real_call(m, session, **kw)
    cdp.call = gone_early
    popup("P6")
    assert until(lambda: "popup" not in sh.tabs["up"])

    def too_busy(m, session=None, **kw):         # still open but too busy to set up in time: closed, the tile shown
        if m == "Page.enable" and session == "SP4":
            raise TimeoutError("Page.enable: no reply in 10 s")
        return real_call(m, session, **kw)
    cdp.call = too_busy
    cdp.log.clear()
    popup("P4")
    assert until(lambda: ("Target.activateTarget", "T1") in cdp.log) and "popup" not in sh.tabs["up"], cdp.log
    assert ("Target.closeTarget", "P4") in cdp.log, cdp.log
    cdp.call = real_call
    # Home shows the launcher first (a page too busy to pause can't keep it away), then pauses the tile and its popup
    popup("P2")
    assert until(lambda: "popup" in sh.tabs["up"])
    cdp.log.clear()
    assert sh.home()["surface"] == "launcher"
    assert cdp.log[:2] == [("Target.activateTarget", "L"), ("Page.bringToFront", "SL")]
    assert sorted(s for m, s in cdp.log if m == "Runtime.evaluate") == ["SP2", "ST1"]
    assert sh.open("up")["ok"] and ("Target.activateTarget", "P2") in cdp.log      # the tile resumes on its popup
    # a press whose keyDown closes its page: the keyUp finds it gone, and that's no failure (it used to wait 10 s)
    real_call = cdp.call

    def closes_on_keydown(m, session=None, **kw):
        if kw.get("type") == "keyUp":
            raise RuntimeError("Input.dispatchKeyEvent: target closed")
        return real_call(m, session, **kw)
    cdp.call = closes_on_keydown
    sh._key("ST1", "enter")
    cdp.call = real_call
    # leanback ignores a DOM click: a trusted Enter follows it; a dialog is answered so it can't wedge the tab
    cdp.log.clear()
    assert sh.click("Library") == {"ok": True, "clicked": "Library"}
    assert [m for m, _ in cdp.log] == ["Runtime.evaluate", "Input.dispatchKeyEvent", "Input.dispatchKeyEvent"]
    sh._cdp_event({"method": "Page.javascriptDialogOpening", "sessionId": "SP2", "params": {"type": "confirm"}})
    assert cdp.log[-1] == ("Page.handleJavaScriptDialog", "SP2") and cdp.kw["accept"] is False
    # the page tools the agent polls while a results page loads (get_screen after each step too) and the page actions: a
    # busy page holds the lock (Home waits) 2 s at most
    waits, cdp.call = [], lambda m, session=None, **kw: waits.append((m, kw.get("timeout"))) or real_call(m, session, **kw)
    sh.read_screen(), sh.list_titles(), sh.open_title("Library"), sh.click("Library"), sh.media("pause")
    assert [t for m, t in waits if m == "Runtime.evaluate"] == [2] * 5, waits

    def busy(m, session=None, **kw):             # ...and still runs an action's script once free (measured): it's sent,
        if m == "Runtime.evaluate":              # not failed (the model would send it again). On leanback ("up") the
            raise TimeoutError(m + ": no reply in 2 s")   # Enter that presses would land first, on the old focus
        return real_call(m, session, **kw)       # (measured): no Enter, no press
    cdp.call, cdp.log[:] = busy, []
    assert sh.media("pause") == {"ok": True, "media": SENT}
    assert not sh.click("Library")["ok"] and not sh.open_title("Library")["ok"] and cdp.log == [], cdp.log
    sh.active_tab = "down"                       # a pointer page: its DOM click lands once free
    r = sh.click("Library"), sh.open_title("Library")
    assert r == ({"ok": True, "clicked": SENT}, {"ok": True, "opened": "Library"}) and cdp.log == [], (r, cdp.log)
    cdp.call, sh.active_tab = real_call, "up"
    waits = []                                   # ...and a key press or typing: Chrome answers once the page handles
                                                 # it, so each waits 2 s in all (navigate's presses together), then
    def busy_input(m, session=None, **kw):       # counts as sent: never 10 s each, with Home queued behind them
        if m.startswith("Input."):
            waits.append(kw.get("timeout"))
            raise TimeoutError(m + ": no reply")
        return real_call(m, session, **kw)
    cdp.call = busy_input
    for act in (sh.select, lambda: sh.navigate("down", 3), lambda: sh.type_text("bear")):
        waits.clear()
        assert act()["ok"] and None not in waits and sum(waits) <= 2, waits
    shooting, shots = threading.Event(), []      # ...and a screenshot reads what's in front under the lock, then waits

    def busy_shot(m, session=None, **kw):        # for its capture outside it (a busy page answers late: up to 15 s),
        if m == "Page.captureScreenshot":        # so Home (an action: the lock) never queues behind one
            shooting.set(), time.sleep(1)
            return {"data": base64.b64encode(png(2, 2, (1, 2, 3))).decode()}
        return real_call(m, session, **kw)
    cdp.call = busy_shot
    threading.Thread(target=lambda: shots.append(sh.screenshot()), daemon=True).start()
    assert shooting.wait(2)
    t0 = time.monotonic()
    with sh.lock:
        assert time.monotonic() - t0 < 0.5, time.monotonic() - t0
    assert until(lambda: shots) and shots[0].startswith(b"\x89PNG"), shots
    cdp.call = real_call
    # the bridge's binding: payload → Shell action, from the page on screen only (the popup here: not its opener, not a
    # hidden tab, nothing once a game is in front); the action runs off the CDP event thread (select stands in for an
    # Enter whose alert() waits on that thread)
    calls, sh.tabs["bg"], dialog = [], {"target": "T2", "session": "ST2"}, threading.Event()
    for n in ("home", "back", "select", "media", "navigate"):
        setattr(sh, n, lambda *a, n=n: (n != "select" or dialog.wait(5)) and calls.append((n, *a)))
    bind = lambda s, a: sh._cdp_event({"method": "Runtime.bindingCalled", "sessionId": s,
                                       "params": {"name": "sparkhome", "payload": json.dumps({"action": a})}})
    t0 = time.monotonic()
    for s, a in (("SP2", "right"), ("SP2", "select"), ("SP2", "back"), ("SP2", "media"), ("SP2", "home"), ("ST1", "home"),
                 ("ST1", "select"), ("ST2", "home"), ("ST2", "down"), ("SP2", "reboot"), (None, "up")):
        bind(s, a)
    assert time.monotonic() - t0 < 1
    dialog.set()
    want = [("navigate", "right"), ("select",), ("back",), ("media", "toggle"), ("home",)]
    assert until(lambda: len(calls) == 5) and sorted(calls) == sorted(want), calls
    sh.surface = "app"
    bind("SP2", "select"), bind("SP2", "down")
    assert not until(lambda: len(calls) > 5, 0.2), calls
    # the app switcher closes a tab: untracked at once, its popup with it. The kiosk's own page behind a game (one
    # opened from it; then "close up" said in the game, or an agent's close): no Home over the game, no tab activated
    # (Chrome's window with it); the launcher's page once the kiosk is back in front. In front: Home
    cdp.log.clear(), events.clear()
    assert sh.close_app("up") == {"ok": True, "state": "closed", "title": "Up"} and "up" not in sh.tabs
    assert {("Target.closeTarget", "T1"), ("Target.closeTarget", "P2")} <= set(cdp.log)
    assert sh.surface == "app" and ("home", {}) not in events and not any(m == "Target.activateTarget" for m, _ in
                                                                           cdp.log), (sh.surface, events, cdp.log)
    time.sleep(.26)
    sh._tick(FakeX((2, 1)))                      # the game exited: the kiosk is back in front
    assert ("Target.activateTarget", "L") in cdp.log and sh.surface == "launcher", (cdp.log, sh.surface)
    sh.active_tab, sh.surface = "bg", "web"
    assert sh.close_app("bg")["state"] == "closed" and sh.surface == "launcher" and ("home", {}) in events
    sh.cdp = None
    # Chrome profile: crash → clean exit flag
    (tmp / "prof" / "Default").mkdir(parents=True)
    (tmp / "prof" / "Default" / "Preferences").write_text('{"profile": {"exit_type": "Crashed", "exited_cleanly": false}}')
    _mark_clean_exit(str(tmp / "prof"))
    assert json.loads((tmp / "prof" / "Default" / "Preferences").read_text())["profile"]["exit_type"] == "Normal"
    assert png(2, 2, (1, 2, 3))[12:16] == b"IHDR"
    # snapshots: blank() reads B, G, R (never alpha): black and flat grey are blank, an icon on black and noise aren't;
    # BGRX rows → a PNG that decodes to RGB
    w, h = 640, 360
    black, grey, noise = b"\0\0\0\xff" * (w * h), b"ZZZ\xff" * (w * h), bytes((i * 37) % 251 for i in range(w * h * 4))
    icon = bytearray(black)
    for y in range(100, 148):                    # a 48 px icon on a black desktop
        icon[(y * w + 100) * 4:(y * w + 148) * 4] = b"\xff" * 192
    assert blank(w, h, w * 4, black) and blank(w, h, w * 4, grey) and not blank(w, h, w * 4, bytes(icon))
    assert not blank(w, h, w * 4, noise)
    p = png(2, 1, rows=rgb_rows(2, 1, 12, bytes([1, 2, 3, 0, 4, 5, 6, 0, 9, 9, 9, 9])))   # (a padded scanline)
    n = struct.unpack(">I", p[33:37])[0]
    assert struct.unpack(">II", p[16:24]) == (2, 1) and zlib.decompress(p[41:41 + n]) == b"\x00\x03\x02\x01\x06\x05\x04"
    # thumb() reads a window with its child windows (IncludeInferiors: Proton draws a game into a child of its toplevel)
    # at the box asked (a launch's DRAWN: 256x144 of a 1080p window); a frame XGetImage can't read is None
    got = {}

    class Xlib:
        def XGetWindowAttributes(self, d, w, ref):
            ref._obj.map_state, ref._obj.width, ref._obj.height = 2, 1920, 1080
            return 1
        XCreatePixmap = lambda self, d, root, w, h, depth: got.update(pix=(w, h)) or 7
        XGetImage = XFreePixmap = XSync = lambda self, *a: None

    class Render:
        XRenderFindVisualFormat = XRenderFindStandardFormat = lambda self, *a: 1
        XRenderSetPictureTransform = XRenderSetPictureFilter = XRenderComposite = XRenderFreePicture = \
            lambda self, *a: None

        def XRenderCreatePicture(self, d, w, fmt, mask, attrs):
            got.setdefault("src", (w, mask, attrs and attrs._obj.subwindow_mode))
            return 5
    xs, real_xr = X11.__new__(X11), _xr
    xs.X, xs.d, xs.root = Xlib(), None, 0
    globals()["_xr"] = Render()
    try:
        assert xs.thumb(9, DRAWN) is None and got == {"src": (9, 1 << 8, 1), "pix": (256, 144)}, got
    finally:
        globals()["_xr"] = real_xr
    _selftest_sleep(sh, base, until, siri_thread, key, Xq)


def _selftest_sleep(sh, base, until, siri_thread, key, Xq):
    """Sleep (README "Sleep") on a demo Shell with a fake clock: the dim and the signal recorded, X input (mutter's idle
    time) and the page's playing scripted; then the real commands' argv through a fake runner."""
    ev, dims, sig, T, xin, front = [], [], [], [1000.0], [None], [False]
    z = Shell({**base, "settings": {**base["settings"], "sleep": {"minutes": 5}}}, True, lambda t, d: ev.append((t, d)))
    z._clock, z._input_at = (lambda: T[0]), T[0]
    z.on_dim, z._screen, z._front_playing = dims.append, sig.append, lambda: front[0]
    z._x_idle = lambda: None if xin[0] is None else T[0] - xin[0]           # xin: when X input last came
    sleeps = lambda: [d for t, d in ev if t == "sleep"]   # noqa: E731

    def idle(s):                                 # s seconds after the last input, a tick
        T[0] = z._input_at + s
        z._sleep_tick()
        return z._sleep

    def tick(dt):                                # dt seconds on, a tick
        T[0] += dt
        z._sleep_tick()
        return z._sleep
    # off by default (no sleep block): a day idle stays awake
    off = Shell(base, True, lambda *_: None)
    off._clock = lambda: off._input_at + 86400
    off._sleep_tick()
    assert off._sleep == "awake" and off.sleep_state()["in_s"] is None and off.sleep_state()["minutes"] == 0
    # 5 minutes: at 4:49 idle nothing; at 4:51 the warning (the dim, the event with its countdown); X input in it cancels
    assert idle(289) == "awake" and z.sleep_state() == {"state": "awake", "minutes": 5, "game_minutes": 0,
                                                        "signal": "off", "idle_s": 289, "in_s": 1, "held": None}
    assert tick(2) == "warning" and sleeps() == [{"state": "warning", "in": 10}] and until(lambda: dims == [True])
    assert z.sleep_state()["in_s"] == 10
    xin[0] = z._warn_at + 2
    assert tick(5) == "awake" and sleeps()[-1] == {"state": "awake", "by": "x"} and until(lambda: dims == [True, False])
    # at the deadline: X input since (a key in a page or game) restarts the clock from it; so do the holds, from now
    t0 = z._input_at
    xin[0] = t0 + 231
    assert idle(291) == "awake" and z._input_at == t0 + 231 and len(sleeps()) == 2
    xin[0] = None

    def held(why):
        assert idle(291) == "awake" and z._input_at == T[0] and len(sleeps()) == 2, why
        assert z.sleep_state(full=True)["held"] == why, (why, z.sleep_state(full=True))
    z._voice = "listening"                       # (a turn that began before: its start woke it, through the setter)
    held("voice")
    z._voice, z.pending["steam:1091500"] = "idle", {"until": 9e9, "tries": 0, "at": 1.0}   # a game's launch holding
    held("launch")
    z.pending.clear()
    z.surface = "web"
    for front[0] in (True, None):                # a video or music playing in the web app in front, or unknown
        held("playing")
    front[0], z.surface = False, "app"           # a game in front, "during games" never: no limit at all
    assert idle(86400) == "awake" and z.sleep_state(full=True)["held"] == "game" and z.sleep_state()["in_s"] is None
    z.settings["sleep"]["game_minutes"] = 30     # ...or 30 minutes: read live, at the next tick
    assert idle(29 * 60 + 49) == "awake" and idle(29 * 60 + 51) == "warning"
    # warning + 10 s: asleep, its event first; the signal off 1.5 s later (the launcher's fade), X input read meanwhile
    assert tick(9.5) == "warning" and tick(0.5) == "asleep" and sleeps()[-1] == {"state": "asleep"}
    assert z.sleep_state()["in_s"] is None and tick(1) == "asleep" and sig == [] and tick(0.5) == "asleep"
    assert sig == [False] and tick(0.5) == "asleep" and sig == [False]
    # a press wakes it: True (its caller drops it), the awake event at once, the signal and the brightness back; another
    # press within 0.7 s only wakes too, one after acts; words and agents (press=False) always act
    n = len(ev)
    assert z.wake("siri") is True and ev[n:] == [("sleep", {"state": "awake", "by": "siri"})]
    assert until(lambda: sig == [False, True] and dims[-1] is False), (sig, dims)
    T[0] += 0.25
    assert z.wake("siri") is True and z.wake("agent", press=False) is False
    T[0] += 0.5
    assert z.wake("siri") is False and len(ev) == n + 1
    # the decorator: a Siri press (siri.py's thread) while asleep only wakes it, as does one within the grace, nothing
    # sent; one after acts. An agent's move wakes it and acts, the awake event first; reads never wake it
    z.surface, out = "launcher", []
    z.sleep_now()
    siri_thread(lambda: out.append(z.navigate("left")))
    T[0] += 0.25
    siri_thread(lambda: out.append(z.select()))
    assert out == [{"ok": True, "woke": True, "surface": "launcher"}] * 2 and "nav" not in [e for e, _ in ev], out
    T[0] += 0.5
    siri_thread(lambda: out.append(z.navigate("left")))
    assert out[-1] == {"ok": True, "surface": "launcher"} and ev[-1] == ("nav", {"dir": "left", "count": 1, "touch": True})
    z.sleep_now()
    n = len(ev)
    assert z.navigate("right")["ok"] and [e for e, _ in ev[n:]] == ["sleep", "nav"] and ev[n][1]["by"] == "agent"
    z.sleep_now()
    assert z.read_screen()["surface"] == "launcher" and z.volume(20)["ok"] and z._sleep == "asleep"
    assert ev[-1] == ("settings", {"values": {"sound.volume": 20, "sound.muted": False}})   # (the Sound pane follows)
    # hooks: the page's binding (a pad in a web tile) never wakes a sleeping TV, as any script on the page can call it:
    # the pad's press wakes it itself (evdev), and the binding's echo of it, in the grace after, does nothing. A warning
    # it ends (input, as the pad's stick there is), doing nothing else. The Home hotkey and a Siri touch while asleep
    # only wake it; push-to-talk wakes it and talks
    calls = []
    z.surface, z.active_tab, z.tabs["netflix"] = "web", "netflix", {"target": "T", "session": "S"}
    z.select = lambda: calls.append("select")
    bind = lambda: z._cdp_event({"method": "Runtime.bindingCalled", "sessionId": "S",   # noqa: E731
                                 "params": {"name": "sparkhome", "payload": json.dumps({"action": "select"})}})
    bind()
    assert z._sleep == "asleep" and sleeps()[-1] == {"state": "asleep"} and not until(lambda: calls, 0.1)
    assert z.wake("pad")
    bind()
    assert z._sleep == "awake" and not until(lambda: calls, 0.1)
    T[0] += 1
    bind()
    assert until(lambda: calls == ["select"])
    assert idle(291) == "warning"
    bind()
    assert z._sleep == "awake" and sleeps()[-1] == {"state": "awake", "by": "page"} and not until(lambda: calls != [
        "select"], 0.1)
    del z.select
    z.surface, z.active_tab = "launcher", None
    z.tabs.clear()
    z.press, z.on_voice = (lambda src, t=None: calls.append(src)), calls.append
    for hot, want in (("home", []), ("voice", [True, False])):
        z.sleep_now()
        calls.clear()
        z._x_events(type("X11", (), {"X": Xq([key(2, 1000), key(3, 1100)]), "d": None})(), {200: hot})
        assert z._sleep == "awake" and not until(lambda: calls != want, 0.1) and calls == want, (hot, calls)
    del z.press
    z.sleep_now()
    z.swipe("up", 9e3)
    assert z._sleep == "awake" and "swipe" not in [e for e, _ in ev]
    T[0] += 1
    z.swipe("up", 9e3)
    assert ev[-1] == ("swipe", {"dir": "up", "speed": 9e3})
    # "go to sleep" said: its turn still thinking at the next ticks doesn't wake it, nor its end; the next turn does
    z.voice = "thinking"
    z.sleep_now()
    assert [tick(0.5) for _ in range(4)] == ["asleep"] * 4 and sig[-1] is False
    z.voice = "idle"
    assert z._sleep == "asleep"
    z.voice = "listening"
    assert z._sleep == "awake" and sleeps()[-1] == {"state": "awake", "by": "voice"} and until(lambda: sig[-1] is True)
    z.voice = "idle"
    # the key that chose Sleep Now: its release (X input 0.1 s on) doesn't wake it, nor would an idle reset the signal's
    # off itself caused ([?], up to 0.7 s after it); X input after that does
    z.sleep_now()
    xin[0] = T[0] + 0.1
    assert tick(0.5) == "asleep" and tick(1) == "asleep" and sig[-1] is False
    xin[0] = T[0] + 0.3
    assert tick(2) == "asleep"
    xin[0] = T[0] + 0.5
    assert tick(2) == "awake" and sleeps()[-1]["by"] == "x"
    xin[0] = None
    # a wake while the signal's off is under way (its X read): the signal stays on
    z.sleep_now()
    n, real_xi, z._x_idle = len(sig), z._x_idle, lambda: z.wake("pad") and None
    assert tick(1.5) == "awake" and not until(lambda: len(sig) > n, 0.2), sig[n:]
    z._x_idle = real_xi
    # a dim and an undim asked at once land in the state's order, whichever thread runs first: never dimmed awake
    n = len(dims)
    with z._screen_lock:
        idle(291), z.wake("agent", press=False)
    assert until(lambda: len(dims) == n + 1) and dims[-1] is False and not until(lambda: len(dims) > n + 1, 0.2), dims
    # "Show black": no signal off; something other than the launcher in front gives way to it (Home, stubbed here)
    z.settings["sleep"], z.surface, homes, n = {"minutes": 5, "signal": "black"}, "web", [], len(sig)
    z._home = lambda: homes.append(z._sleep)
    assert idle(291) == "warning" and tick(10) == "asleep" and until(lambda: homes == ["asleep"])
    assert tick(2) == tick(2) == "asleep" and len(sig) == n
    assert z.wake("pad") and not until(lambda: len(sig) > n, 0.2)        # (no signal to turn back on)
    del z._home
    z.surface = "launcher"
    # bad settings are their defaults, never a crash, checked once per change (not logged per tick); a change counts
    # at the next tick, and a limit of 0 (never) cancels a warning
    for bad in ({"minutes": "30"}, {"minutes": None}, {"minutes": -1, "game_minutes": True}, {"signal": "dim"}, 30):
        z.settings["sleep"] = bad
        assert z._sleep_settings() == (0, 0, "off") and idle(86400) == "awake", bad
    c = z._sleep_cfg
    assert z._sleep_settings() and z._sleep_cfg is c
    z.settings["sleep"] = {"minutes": 30}
    assert idle(6 * 60) == "awake"
    z.settings["sleep"]["minutes"] = 5
    assert tick(0) == "warning"
    z.settings["sleep"]["minutes"] = 0
    assert tick(0.5) == "awake" and sleeps()[-1] == {"state": "awake", "by": "settings"}
    z.settings["sleep"]["minutes"] = 5           # ...and so does a longer one (idle 4:50 is far from 29:50)
    assert idle(291) == "warning"
    z.settings["sleep"]["minutes"] = 30
    assert tick(0.5) == "awake" and sleeps()[-1] == {"state": "awake", "by": "settings"}
    # the sleep thread runs in the demo too; the page's playing test is get_screen's own snippet (skin_check.py runs
    # both in Chrome)
    s3 = Shell(base, True, lambda *_: None)
    s3.start()
    assert "sleep" in [t.name for t in threading.enumerate()]
    assert PLAYS_FN in READ_JS and PLAYS_FN in PLAYING_JS and "return plays(document, 0, 0) || (unread ? null : false)" \
        in PLAYING_JS and "playing: plays(document, 0, 0) || (unread ? null : false)," in READ_JS
    # the real commands, through a fake runner: mutter's idle time (ms) and the signal; a failing one is None
    runs = []
    sh._cmd = lambda *argv, timeout=3: runs.append((argv, timeout)) or "t 1195892\n"
    assert sh._x_idle() == 1195.892
    sh._screen(False), sh._screen(True)
    assert runs == [(("busctl", "--user", "call", "org.gnome.Mutter.IdleMonitor", "/org/gnome/Mutter/IdleMonitor/Core",
                      "org.gnome.Mutter.IdleMonitor", "GetIdletime"), 2), (("xset", "dpms", "force", "off"), 3),
                    (("xset", "dpms", "force", "on"), 3)], runs
    sh._cmd = lambda *argv, timeout=3: None
    assert sh._x_idle() is None
    del sh._cmd
    assert sh._cmd(sys.executable, "-c", "print('t 5')") == "t 5\n" and sh._cmd("sparkhome-no-such-binary") is None
    assert sh._cmd(sys.executable, "-c", "raise SystemExit(3)") is None
    _selftest_tv(sh, until)


def _selftest_tv(sh, until):
    """The TV itself (settings.tv) against a fake Roku on loopback, as this TCL answers (2026-10-21 [M], standby [I]):
    active-app only while on, keys refused while Control by mobile apps is Limited, launches allowed; off the network,
    no answer at all. The wake-on-LAN is stubbed: nothing is broadcast."""
    import http.server

    tv = {"power": "PowerOn", "app": "tvinput.hdmi3", "boot": "tvinput.hdmi2", "limited": False, "away": False}

    class Roku(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.do_POST()

        def do_POST(self):
            p, on = self.path[1:], tv["power"] == "PowerOn"
            tv["calls"].append(f"{self.command} {p}")
            if tv["away"]:
                return                           # (the connection dropped unanswered)
            body = None                          # (None: refused, 403)
            if p == "query/device-info":
                body = (f"<device-info><wifi-mac>02:00:00:00:00:c5</wifi-mac><ethernet-mac>02:00:00:00:00:73"
                        f"</ethernet-mac><network-type>wifi</network-type><power-mode>{tv['power']}</power-mode>"
                        "</device-info>")
            elif p == "query/active-app" and on:
                body = f'<active-app><app id="{tv["app"]}" type="tvin">x</app></active-app>'
            elif p in ("keypress/PowerOn", "keypress/PowerOff") and not tv["limited"]:
                if p.endswith("On"):
                    tv.update(power="PowerOn", app=tv["boot"])     # (it powers on to the input used last)
                else:
                    tv["power"] = "DisplayOff"
                body = ""
            elif p.startswith("launch/tvinput.") and on:
                tv["app"], body = p[7:], ""
            code = 403 if body is None else 200
            self.send_response(code)
            self.send_header("Content-Length", str(len(body or "")))
            self.end_headers()
            self.wfile.write((body or "").encode())

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Roku)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    wol, real_screen = [], sh._screen
    sh._wol = lambda mac: wol.append(mac) or tv.update(away=False) or True
    sh._screen = lambda on: None

    def run(on, **state):
        tv.update(state, calls=[])
        sh._sleep = "awake" if on else "asleep"
        sh._tv(on)
        return tv["calls"]
    try:
        assert run(True) == [] and not wol                                   # no settings.tv: nothing
        sh.settings["tv"] = {"input": "hdmi3", "ecp": f"127.0.0.1:{srv.server_port}"}
        # a nap: standby only while it shows the Spark's input, never over the Chromecast someone is watching
        assert run(False) == ["GET query/active-app", "POST keypress/PowerOff"] and tv["power"] == "DisplayOff"
        assert run(False, power="PowerOn", app="tvinput.hdmi2") == ["GET query/active-app"] and tv["power"] == "PowerOn"
        # a wake: out of standby, then the Spark's input over the one it powers on to; on and showing it, reads only
        assert run(True, power="DisplayOff") == ["GET query/device-info", "POST keypress/PowerOn",
                                                 "GET query/device-info", "GET query/active-app",
                                                 "POST launch/tvinput.hdmi3"] and tv["app"] == "tvinput.hdmi3"
        assert run(True) == ["GET query/device-info", "GET query/active-app"]
        # off the network: a wake-on-LAN to the MAC its device-info told (the Wi-Fi one), then as from standby
        assert run(True, away=True, power="DisplayOff")[0] == "GET query/device-info" and wol == ["02:00:00:00:00:c5"]
        assert tv["power"] == "PowerOn" and tv["app"] == "tvinput.hdmi3" and sh._tv_at, tv
        # Limited (keys refused): a TV that's on still gets the input; standby, and the nap's off, stay as they are
        assert run(True, limited=True, app="tvinput.hdmi2")[-1] == "POST launch/tvinput.hdmi3" and tv["app"] == \
            "tvinput.hdmi3"
        assert run(False, limited=True)[-1] == "POST keypress/PowerOff" and tv["power"] == "PowerOn"
        assert run(True, limited=True, power="DisplayOff")[1:] == ["POST keypress/PowerOn", "GET query/active-app",
                                                                    "POST launch/tvinput.hdmi3"] and tv["power"] != "PowerOn"
        # the state decides (a wake and a nap that raced); a bad input is no command (a bad tv.ecp would find the TV by
        # SSDP: never here, beside a real one)
        sh._sleep, tv["calls"], tv["limited"] = "asleep", [], False
        sh._tv(True)
        sh._sleep = "awake"
        sh._tv(False)
        assert tv["calls"] == []
        sh.settings["tv"]["input"] = "hdmi3/../keypress/PowerOff"
        assert run(False) == []
        # only a person's wake turns it on (not an agent's, X input or the page's: a bench, the TV's own hotplug); every
        # nap that turns the signal off asks for standby
        tvs = []
        sh._tv = tvs.append
        for src, want in (("siri", [True]), ("pad", [True]), ("phone", [True]), ("voice", [True]), ("key", [True]),
                          ("agent", []), ("x", []), ("page", []), ("settings", [])):
            tvs.clear()
            sh._off, sh._sleep = True, "awake"
            sh._wake_effects(src)
            assert (until(lambda: tvs == want) if want else not until(lambda: tvs, 0.1)) and not sh._off, (src, tvs)
        tvs.clear()
        sh._sleep = "asleep"
        sh._signal_off()
        assert until(lambda: tvs == [False]) and sh._off
    finally:
        srv.shutdown()
        srv.server_close()
        sh._screen, sh._sleep, sh._off, sh._tv_at, sh._tv_mac = real_screen, "awake", False, None, None
        sh.settings.pop("tv", None)
        for k in ("_tv", "_wol"):
            sh.__dict__.pop(k, None)
