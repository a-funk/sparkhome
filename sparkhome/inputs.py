"""Gamepad buttons read straight from evdev, beside Chrome and Steam (no grab, so they still see every press).

Guide (settings.gamepad.home, BTN_MODE) = SparkHome Home: a short press on the launcher or a web tab, a hold of
home_hold_in_app_ms while a game/app is in front (a quick tap stays Steam's). Guide twice (the shell times it), or held
apps_hold_ms (1.2 s) anywhere, opens the app switcher. Steam's virtual pad (vendor 28de) is skipped: it mirrors the real
pad's Guide, which would count one press twice. Optional settings.gamepad.voice (e.g. "KEY_RECORD", the Xbox Share
button) is push-to-talk. Any button or D-pad press of a pad watched here wakes a sleeping screen (Shell.wake). Joystick
nodes are readable through uaccess ACLs; Linux only.

    python3 -m sparkhome.inputs --probe     list input devices, then print key events with code names and HID scancodes
"""
import fcntl
import glob
import logging
import os
import select
import struct
import sys
import threading
import time

log = logging.getLogger("sparkhome.inputs")
EV = struct.Struct("llHHi")                  # struct input_event on 64-bit: timeval(16) + type + code + value = 24 bytes
EV_KEY, EV_ABS, EV_MSC, MSC_SCAN = 1, 3, 4, 4
HATS = (16, 17)                              # ABS_HAT0X, ABS_HAT0Y: the D-pad of the Xbox pad and the DualSense
NAMES = {1: "KEY_ESC", 14: "KEY_BACKSPACE", 28: "KEY_ENTER", 103: "KEY_UP", 105: "KEY_LEFT", 106: "KEY_RIGHT",
         108: "KEY_DOWN", 113: "KEY_MUTE", 114: "KEY_VOLUMEDOWN", 115: "KEY_VOLUMEUP", 116: "KEY_POWER",
         127: "KEY_COMPOSE", 139: "KEY_MENU", 158: "KEY_BACK", 163: "KEY_NEXTSONG", 164: "KEY_PLAYPAUSE",
         165: "KEY_PREVIOUSSONG", 166: "KEY_STOPCD", 167: "KEY_RECORD", 172: "KEY_HOMEPAGE", 183: "KEY_F13",
         184: "KEY_F14", 185: "KEY_F15", 186: "KEY_F16", 187: "KEY_F17", 188: "KEY_F18", 207: "KEY_PLAY",
         217: "KEY_SEARCH", 304: "BTN_SOUTH", 305: "BTN_EAST", 306: "BTN_C", 307: "BTN_NORTH", 308: "BTN_WEST",
         309: "BTN_Z", 310: "BTN_TL", 311: "BTN_TR", 312: "BTN_TL2", 313: "BTN_TR2", 314: "BTN_SELECT",
         315: "BTN_START", 316: "BTN_MODE", 317: "BTN_THUMBL", 318: "BTN_THUMBR", 352: "KEY_OK", 353: "KEY_SELECT",
         358: "KEY_INFO", 544: "BTN_DPAD_UP", 545: "BTN_DPAD_DOWN", 546: "BTN_DPAD_LEFT", 547: "BTN_DPAD_RIGHT",
         582: "KEY_VOICECOMMAND", 583: "KEY_ASSISTANT", 586: "KEY_DICTATE"}
CODES = {**{v: k for k, v in NAMES.items()}, "BTN_A": 304, "BTN_B": 305, "BTN_X": 307, "BTN_Y": 308}


def code(name):
    """'BTN_MODE' or '316' → 316; None/unknown → None."""
    s = str(name or "").strip().upper()
    return int(s) if s.isdigit() else CODES.get(s)


def number(cfg, name, default):
    """A numeric setting (settings.gamepad.*, settings.switcher.*): a finite number >= 0, else logged and the default,
    as siri.knobs does. A quoted "300" or a null must not stop the daemon at start, nor a thread on its first use."""
    v = cfg.get(name, default) if isinstance(cfg, dict) else default
    if type(v) in (int, float) and 0 <= v < float("inf"):
        return v
    log.warning("setting %s = %r ignored: expected a number >= 0; using %s", name, v, default)
    return default


def parse(buf):
    """input_event records → [(type, code, value)]; a trailing partial record is ignored."""
    return [EV.unpack_from(buf, i)[2:] for i in range(0, len(buf) - EV.size + 1, EV.size)]


class Buttons:
    """The Guide/voice decision logic, kept apart from device I/O so the selftest can drive it."""

    def __init__(self, home, voice=None, hold_s=0.6, apps_s=1.2):
        self.home, self.voice, self.hold, self.apps, self.down, self.homed = home, voice, hold_s, apps_s, None, False

    def feed(self, c, value, now, surface):
        if c == self.home and value == 1:
            self.down, self.homed = now, surface != "app"   # in a game: only a hold counts (tick() fires it)
            if self.homed:
                return ["home"]
        elif c == self.home and value == 0:
            self.down = None
        elif c == self.voice and value in (0, 1):
            return ["voice_on" if value else "voice_off"]
        return []

    def tick(self, now):
        if self.down is not None and not self.homed and now - self.down >= self.hold:
            self.homed = True
            return ["home"]
        if self.down is not None and now - self.down >= self.apps:
            self.down = None                     # held on: the app switcher (Windows' Xbox-button Task View)
            return ["apps"]
        return []

    def wait(self, now, default):
        return default if self.down is None else max(0.0, self.down + (self.apps if self.homed else self.hold) - now)


def _has_keys(fd, codes):
    bits = bytearray(96)                     # EVIOCGBIT(EV_KEY, 96): KEY_MAX 0x2ff → 768 bits
    fcntl.ioctl(fd, (2 << 30) | (len(bits) << 16) | (ord("E") << 8) | (0x20 + EV_KEY), bits)
    return any(bits[c // 8] >> (c % 8) & 1 for c in codes if c < 768)


def _steam_pad(name, root="/sys/class/input"):
    """eventN is Steam's virtual pad (USB vendor 28de), which mirrors the real pad: skipped."""
    try:
        with open(f"{root}/{name}/device/id/vendor") as f:
            return f.read().strip().lower() == "28de"
    except OSError:
        return False


def _bg(fn, *a):
    threading.Thread(target=fn, args=a, daemon=True).start()


def watch(shell):
    """Run forever (thread): rescan /dev/input/event* every 5 s, act on the configured buttons. No-op off Linux."""
    if not sys.platform.startswith("linux"):
        return
    cfg = shell.settings.get("gamepad")
    cfg = cfg if isinstance(cfg, dict) else {}
    b = Buttons(code(cfg.get("home", "BTN_MODE")), code(cfg.get("voice")),
                number(cfg, "home_hold_in_app_ms", 600) / 1000, number(cfg, "apps_hold_ms", 1200) / 1000)
    wanted = [c for c in (b.home, b.voice) if c]
    if not wanted:
        return
    fds, skip, next_scan = {}, set(), 0.0    # fd → path; paths without our buttons (until they vanish)
    while True:
        now = time.monotonic()
        if now >= next_scan:
            next_scan = now + 5
            paths = set(glob.glob("/dev/input/event*"))
            skip &= paths
            for p in paths - skip - set(fds.values()):
                if _steam_pad(os.path.basename(p)):
                    skip.add(p)
                    continue
                try:
                    fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
                except OSError:              # keyboards/remotes are root:input; joysticks have uaccess ACLs
                    continue
                try:
                    keep = _has_keys(fd, wanted)
                except OSError:
                    keep = False
                if keep:
                    fds[fd] = p
                    log.info("watching %s for gamepad buttons", p)
                else:
                    os.close(fd)
                    skip.add(p)
        timeout = b.wait(now, max(0.0, next_scan - now))
        if not fds:
            time.sleep(timeout)
            ready = []
        else:
            ready = select.select(list(fds), [], [], timeout)[0]
        for fd in ready:
            try:
                buf = os.read(fd, EV.size * 64)
            except OSError:
                buf = b""
            if not buf:                      # unplugged / disconnected
                log.info("lost %s", fds.pop(fd))
                os.close(fd)
                continue
            for typ, c, val in parse(buf):
                _event(shell, b, typ, c, val)
        t = time.monotonic()
        for act in b.tick(t):
            _act(shell, act, t)


def _event(shell, b, typ, c, val):
    """One evdev event. Any button or D-pad press is input (README "Sleep"): it wakes a sleeping screen, and a Guide
    press that does (or comes within the wake's grace) is swallowed, its hold too. Other buttons reach the game or page
    anyway, and voice talks. Sticks are no input: they jitter at rest (the launcher page sees its own)."""
    if (typ == EV_KEY and val == 1 or typ == EV_ABS and c in HATS and val) and shell.wake("pad") and \
            typ == EV_KEY and c == b.home:
        return
    if typ == EV_KEY:
        t = time.monotonic()
        surface = shell.state()["surface"] if c == b.home and val == 1 else None
        for act in b.feed(c, val, t, surface):
            _act(shell, act, t)


def _act(shell, act, t):
    if act == "home":                        # a physical press, read at t: two within the shell's window are a double
        _bg(shell.press, "guide", t)
    elif act == "apps":
        _bg(shell.switcher)
    elif shell.on_voice:
        shell.on_voice(act == "voice_on")    # here, in order: a quick release must not overtake its press


def probe():
    if not sys.platform.startswith("linux"):
        sys.exit("evdev is Linux-only")
    fds = {}
    for p in sorted(glob.glob("/dev/input/event*"), key=lambda p: int(p[16:]) if p[16:].isdigit() else 0):
        n = os.path.basename(p)
        rd = lambda f: open(f"/sys/class/input/{n}/device/{f}").read().strip()
        try:
            name, ids = rd("name"), ":".join(rd(f"id/{k}") for k in ("bustype", "vendor", "product"))
        except OSError:
            name, ids = "?", "?"
        try:
            fds[os.open(p, os.O_RDONLY | os.O_NONBLOCK)] = f"{n} {name}"
            access = "readable"
        except OSError:
            access = "NO ACCESS (run as root, or tag it uaccess with a udev rule)"
        print(f"{p:20} bus:vendor:product {ids:16} {name!r:40} {access}")
    if not fds:
        sys.exit("no readable input devices")
    print("\npress buttons (Ctrl-C to stop); scancodes are what hwdb KEYBOARD_KEY_<scancode> lines match", flush=True)
    scan = {}
    try:
        while True:
            for fd in select.select(list(fds), [], [])[0]:
                for typ, c, val in parse(os.read(fd, EV.size * 64)):
                    if typ == EV_MSC and c == MSC_SCAN:
                        scan[fd] = val & 0xFFFFFFFF
                    elif typ == EV_KEY:
                        sc = f"  scancode 0x{scan.pop(fd):x}" if fd in scan else ""
                        print(f"{fds[fd]}: {NAMES.get(c, f'KEY_{c}')} ({c}) {('release', 'press', 'repeat')[min(val, 2)]}{sc}",
                              flush=True)
    except KeyboardInterrupt:
        pass


def _selftest():
    raw = EV.pack(1, 2, EV_MSC, MSC_SCAN, 0x90001) + EV.pack(1, 3, EV_KEY, 316, 1) + EV.pack(1, 4, 0, 0, 0) + b"\x00" * 5
    assert EV.size == 24 and parse(raw) == [(EV_MSC, MSC_SCAN, 0x90001), (EV_KEY, 316, 1), (0, 0, 0)]
    assert code("BTN_MODE") == 316 and code("btn_y") == 308 and code("167") == 167 and code(None) is None
    # numeric settings: a finite number >= 0; anything else (quoted, null, a bool, NaN, negative) is its default
    assert [number({"n": v}, "n", 7) for v in (300, 2.5, 0, "300", None, True, float("nan"), float("inf"), -1)] == \
        [300, 2.5, 0, 7, 7, 7, 7, 7, 7] and number({}, "n", 7) == 7 and number(True, "n", 7) == 7
    b = Buttons(316, 167, 0.6)
    assert b.feed(316, 1, 10.0, "launcher") == ["home"] and b.feed(316, 0, 10.1, "launcher") == []
    assert b.feed(316, 1, 20.0, "app") == [] and b.tick(20.3) == [] and abs(b.wait(20.3, 5) - 0.3) < 1e-9
    assert b.tick(20.6) == ["home"] and b.tick(20.7) == []                  # held in a game: fires once
    assert b.feed(316, 1, 30.0, "app") == [] and b.feed(316, 0, 30.2, "app") == [] and b.tick(31) == []  # quick tap: Steam's
    assert b.feed(167, 1, 0, "app") == ["voice_on"] and b.feed(167, 0, 0, "app") == ["voice_off"]
    assert b.feed(167, 2, 0, "app") == [] and b.feed(304, 1, 0, "launcher") == []
    # held 1.2 s: the app switcher, once, on any surface (in a game Home first, at 0.6 s); wait() is the next deadline
    b = Buttons(316, 167, 0.6, 1.2)
    assert b.feed(316, 1, 40.0, "launcher") == ["home"] and abs(b.wait(40.2, 5) - 1.0) < 1e-9 and b.tick(40.6) == []
    assert b.tick(41.2) == ["apps"] and b.tick(41.3) == [] and b.wait(41.3, 5) == 5
    assert b.feed(316, 1, 50.0, "app") == [] and abs(b.wait(50.0, 5) - 0.6) < 1e-9 and b.tick(50.6) == ["home"]
    assert abs(b.wait(50.6, 5) - 0.6) < 1e-9 and b.tick(51.0) == [] and b.tick(51.2) == ["apps"] and b.tick(52) == []
    assert b.feed(316, 1, 60.0, "app") == [] and b.feed(316, 0, 60.1, "app") == [] and b.tick(62) == []  # a tap: Steam's
    # Steam's virtual pad (vendor 28de) is skipped; a real pad (045e) and a node without ids are not
    import shutil
    import tempfile
    root = tempfile.mkdtemp(prefix="sparkhome-test-")
    try:
        for n, v in (("event7", "28de"), ("event8", "045e")):
            os.makedirs(f"{root}/{n}/device/id")
            with open(f"{root}/{n}/device/id/vendor", "w") as f:
                f.write(v + "\n")
        assert _steam_pad("event7", root) and not _steam_pad("event8", root) and not _steam_pad("event9", root)
    finally:
        shutil.rmtree(root)
    # a Guide press goes to the shell's press() with its read time; a hold to switcher(); voice in order, here
    calls, end = [], time.monotonic() + 2
    sh = type("Sh", (), {"press": lambda s, *a: calls.append(a), "switcher": lambda s: calls.append("sw"),
                         "on_voice": lambda s, on: calls.append(on)})()
    _act(sh, "voice_on", 1.0), _act(sh, "home", 2.5), _act(sh, "apps", 3.0)
    while len(calls) < 3 and time.monotonic() < end:     # (press and switcher run on their own threads)
        time.sleep(.01)
    assert calls[0] is True and sorted(map(str, calls[1:])) == ["('guide', 2.5)", "sw"], calls
    # input wakes the screen: any button or D-pad press, never a release or a stick. While it sleeps (wake says it
    # woke), Guide's press and its hold do nothing else; voice still talks
    woke, b = [True], Buttons(316, 167, 0.6, 1.2)
    sh.wake, sh.state = (lambda src: calls.append(src) or woke[0]), (lambda: {"surface": "launcher"})
    calls.clear()
    for ev in ((EV_KEY, 316, 1), (EV_KEY, 167, 1), (EV_KEY, 304, 1), (EV_ABS, 16, -1), (EV_ABS, 17, 0),
               (EV_ABS, 0, 30000), (EV_KEY, 304, 0), (EV_KEY, 167, 0)):
        _event(sh, b, *ev)
    assert calls == ["pad", "pad", True, "pad", "pad", False] and b.tick(time.monotonic() + 2) == [], calls
    _event(sh, b, EV_KEY, 316, 0)                        # awake: Guide is Home again
    woke[0], end = False, time.monotonic() + 2
    calls.clear()
    _event(sh, b, EV_KEY, 316, 1)
    while len(calls) < 2 and time.monotonic() < end:
        time.sleep(.01)
    assert calls[0] == "pad" and calls[1][0] == "guide", calls


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    elif "--selftest" in sys.argv:
        _selftest()
        print("inputs selftest OK")
    else:
        sys.exit(__doc__)
