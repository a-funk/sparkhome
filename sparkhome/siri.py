"""Siri Remote (gen 1, A1962) read straight from hidraw: touch gestures and buttons become Shell actions.

BlueZ's hog plugin gives the paired remote a /dev/hidrawN (HID_ID 0005:0000004C:0000026D). Feature report 0xF0 <- 0xAF
unlocks its vendor input report 0xFC, and has to be sent again after every reconnect. Byte 1 of 0xFC is the kind, byte 2
the buttons held, in every kind: 3-byte button reports, 14-byte one-finger touch frames (~100 Hz, also the gestures),
21-byte two-finger frames (byte 1 = 2) and voice packets while Siri is held (byte 1 = 0x10, truncated Opus), and the
5-byte reply to the unlock (byte 1 = 0xE0). A click mostly shows only in touch frames. The node is root-only until
deploy/71-sparkhome-siri.rules tags it uaccess (deploy/install.sh root). Linux only.

A finger is down in touch states 3-6 (byte 13 bits 0-2: make, touching, break, linger, as on Apple's multitouch
devices). A lift sends states 7 and 0 with the contact bytes 10-12 zeroed, then nothing until the next touch; 1-2 are a
hovering or barely touching finger. Byte 6 bit 0 is not the finger: in the 2026-10-08 captures it flips mid-swipe and
often stays set through a lift, and read as the finger it merged 630 touches into 361.

The gesture engine (v2, from the remote lab) aims for the tvOS feel:
- a resting finger that moves a little leans the focused launcher tile toward it; focus doesn't move;
- sliding steps the focus along one locked axis, one step per `step` of travel, paced, and stepping back the other way
  takes extra travel (no jitter ping-pong);
- a quick flick coasts a few more steps with friction; the next touch stops it;
- a touch that stepped, or left the surface fast along its axis, also reports one swipe(dir, speed) at the lift, for
  actions that must happen once per gesture (the app switcher's swipe up to close a card).
Input goes through a One Euro filter. The first `settle` frames of a touch (the finger landing) and the last `guard`
before a lift (the finger rolling off) are dropped: they jump ~57 units, against ~4 per frame mid-swipe. Units are the
remote's (~1700 across); every KNOBS value can be overridden in settings.siri_remote.
"""
import fcntl
import glob
import logging
import math
import os
import queue
import select
import sys
import threading
import time

log = logging.getLogger("sparkhome.siri")
HID_ID = "HID_ID=0005:0000004C:0000026D"     # Bluetooth, Apple, Siri Remote gen 1
UNLOCK = b"\xf0\xaf"                          # feature report 0xF0 <- 0xAF
HIDIOCSFEATURE = (3 << 30) | (len(UNLOCK) << 16) | (ord("H") << 8) | 0x06   # as tools/siri_probe.py hidioc_rw (arm64)
BUTTONS = {0x80: "click", 0x20: "menu", 0x01: "tv", 0x08: "playpause", 0x02: "vol+", 0x04: "vol-",   # byte 2
           0x10: "siri"}
ACTIONS = {"click": ("select",), "menu": ("back",), "tv": ("home",), "playpause": ("media", "toggle"),
           "vol+": ("volume", "up"), "vol-": ("volume", "down")}
IN_APP = ("tv", "playpause", "vol+", "vol-")  # all a game or app in front gets, besides Siri
GAP = .05             # s of remote clock between touch frames that ends the touch: lost lift frames, or two fingers
                      # (ponytail: their 21-byte frames are skipped, so the remaining finger starts a new gesture)
STALE_S = .4          # a move still queued after this (the Shell was busy: a tile opening, wpctl) is dropped, not sent
                      # in a burst with the ones behind it
NO_VOICE = "Voice isn't set up on this SparkHome. Use the phone remote's text box."
KNOBS = {             # calibration, tuned on captures from this remote; settings.siri_remote overrides any of them
    "step": 560,             # travel per focus step (~3 across the surface)
    "lock": 140,             # travel before a slide's axis locks...
    "lock_ratio": 1.5,       # ...and how much the major axis must win by
    "turn": 1.25,            # travel (in steps) on the other axis that switches the locked axis
    "back": 1.4,             # travel (in steps) needed to step back against the last step
    "pace_s": .12,           # least time between slide steps
    "settle": 3,             # frames dropped after touch-down
    "guard": 2,              # frames held back and dropped at a lift
    "flick": 8000,           # release speed (units/s) along the locked axis that coasts... (4200 coasted 57% of the
                             # swipes in the free-play capture once lifts were read right; 8000: ~34%)
    "flick_min": 200,        # ...after at least this much travel along it
    "coast_speed": 3000,     # each this much faster than `flick` coasts one more step...
    "coast_max": 4,          # ...up to this many (2 after a slide of 3+ steps)
    "coast_first_s": .11,    # time to the first coasting step; each later gap grows by `friction`
    "friction": 1.3,
    "swipe_speed": 4500,     # release speed (units/s) along the locked axis that makes a touch a swipe() even without a
                             # step (guided swipes left at 6900-10200, a finger repositioning at 2700 or less)
    "tilt_hz": 20,           # most tilt events a second
    "tilt_eps": .05,         # least tilt change worth sending
    "tilt_dead": .06,        # tilt dead zone (a resting finger's jitter)
    "min_cutoff": 1.5,       # One Euro: cutoff (Hz) when still (lower = steadier)...
    "beta": .004,            # ...and how fast it opens with speed (higher = less lag)
    "siri_release_s": .15,   # a Siri release counts after this: mid-press the remote drops the bit for 20-40 ms
}


def touch(r):
    """A 14-byte touch report -> (remote clock in 1/2000 s ticks, x, y, finger down); x/y signed 12-bit, +x right,
    +y up."""
    s12 = lambda v: v - 4096 if v & 0x800 else v
    return r[4] | r[5] << 8, s12(r[7] | (r[8] & 15) << 8), s12(r[8] >> 4 | r[9] << 4), 3 <= r[13] & 7 <= 6


class OneEuro:
    """Casiez, Roussel & Vogel (CHI 2012): a low-pass whose cutoff rises with speed."""

    def __init__(self, min_cutoff, beta, d_cutoff=1.0):
        self.mc, self.beta, self.dc, self.x, self.dx, self.t = min_cutoff, beta, d_cutoff, None, 0.0, None

    @staticmethod
    def _a(cutoff, dt):
        r = 2 * math.pi * cutoff * dt
        return r / (r + 1)

    def __call__(self, t, x):
        if self.x is None or t <= self.t:
            self.x, self.t = x, t
            return x
        dt = t - self.t
        self.dx += self._a(self.dc, dt) * ((x - self.x) / dt - self.dx)
        self.x += self._a(self.mc + self.beta * abs(self.dx), dt) * (x - self.x)
        self.t = t
        return self.x


class Gestures:
    """Touch frames -> emit("move", dir=...), emit("tilt", x=, y=), and once per touch at the lift emit("swipe", dir=,
    speed=). t is the remote's clock (speeds); clock() is the host's (pacing, and coasting, which runs after the frames
    stop)."""

    def __init__(self, emit, k, clock=time.monotonic):
        self.emit, self.k, self.clock = emit, k, clock
        self.down, self.coast, self.tilt, self.tilt_at = False, [], (0.0, 0.0), 0.0

    def frame(self, t, x, y, down):
        if down and not self.down:           # touch: stop coasting, start a gesture
            self.coast.clear()
            self.down, self.n, self.buf, self.hist = True, 0, [], []
            self.fx, self.fy = (OneEuro(self.k["min_cutoff"], self.k["beta"]) for _ in "xy")
            self.anchor = self.axis = self.last = None
            self.steps, self.next_at = 0, 0.0
        if not down:
            if self.down:
                self._release()
            return
        self.n += 1
        if self.n <= self.k["settle"]:
            return
        self.buf.append((t, x, y))           # held back: they may be the roll-off before a lift
        if len(self.buf) <= self.k["guard"]:
            return
        t, x, y = self.buf.pop(0)
        self._move(t, self.fx(t, x), self.fy(t, y))

    def _move(self, t, x, y):
        k, step = self.k, self.k["step"]
        self.hist = [h for h in self.hist if t - h[0] <= .08] + [(t, x, y)]
        if self.anchor is None:
            self.anchor = [x, y]
            return
        dx, dy = x - self.anchor[0], y - self.anchor[1]
        if self.axis is None and max(abs(dx), abs(dy)) >= k["lock"]:
            if abs(dx) >= k["lock_ratio"] * abs(dy):
                self.axis = "x"
            elif abs(dy) >= k["lock_ratio"] * abs(dx):
                self.axis = "y"
        elif self.axis == "x" and abs(dy) >= k["turn"] * step:   # a deliberate turn
            self.axis, self.anchor[0], self.last = "y", x, None
        elif self.axis == "y" and abs(dx) >= k["turn"] * step:
            self.axis, self.anchor[1], self.last = "x", y, None
        if self.axis:
            i = 0 if self.axis == "x" else 1
            d = (x, y)[i] - self.anchor[i]
            name = ("right" if d > 0 else "left") if i == 0 else ("up" if d > 0 else "down")
            now = self.clock()
            if abs(d) >= step * (k["back"] if self.last and name != self.last else 1) and now >= self.next_at:
                self.emit("move", dir=name)
                self.steps += 1
                self.last, self.next_at = name, now + k["pace_s"]
                # the anchor follows the finger, keeping at most half a step of extra travel (no stored-up burst)
                self.anchor[i] = (x, y)[i] - math.copysign(min(abs(d) - step, step * .5), d)
                self.anchor[1 - i] = (x, y)[1 - i]
        self._tilt(x - self.anchor[0], y - self.anchor[1])

    def _tilt(self, dx, dy, force=False):
        k = self.k
        tx, ty = (max(-1.0, min(1.0, v / (k["step"] * .8))) for v in (dx, dy))
        tx, ty = (0.0 if abs(v) < k["tilt_dead"] else round(v, 3) for v in (tx, ty))
        now = self.clock()
        if force or (abs(tx - self.tilt[0]) + abs(ty - self.tilt[1]) >= k["tilt_eps"]
                     and now - self.tilt_at >= 1 / k["tilt_hz"]):
            self.tilt, self.tilt_at = (tx, ty), now
            self.emit("tilt", x=tx, y=ty)

    def _release(self):
        self.down = False
        if self.tilt != (0.0, 0.0):
            self._tilt(0, 0, force=True)
        k, h = self.k, self.hist             # (the held-back roll-off is dropped: the speed is from before it)
        if not self.axis:
            return
        i = 1 if self.axis == "x" else 2
        v = (h[-1][i] - h[0][i]) / (h[-1][0] - h[0][0]) if len(h) >= 3 and h[-1][0] > h[0][0] else 0.0
        swipe = self.last                    # one swipe per touch: the way of its last step, or of a fast release
        if abs(v) >= k["swipe_speed"]:
            swipe = ("right" if v > 0 else "left") if i == 1 else ("up" if v > 0 else "down")
        if abs(v) >= k["flick"] and abs(h[-1][i] - self.anchor[i - 1]) + self.steps * k["step"] >= k["flick_min"]:
            swipe = name = ("right" if v > 0 else "left") if i == 1 else ("up" if v > 0 else "down")
            if not (self.last and name != self.last):
                n = min(k["coast_max"] if self.steps < 3 else 2, 1 + int((abs(v) - k["flick"]) / k["coast_speed"]))
                gap = k["coast_first_s"]
                due = self.clock() + gap
                for _ in range(int(n)):
                    self.coast.append((due, name))
                    gap *= k["friction"]
                    due += gap
        if swipe:                            # after the touch's steps, before its coasting
            self.emit("swipe", dir=swipe, speed=round(abs(v), 1))

    def tick(self):
        """Send the coasting steps that are due."""
        t = self.clock()
        while self.coast and self.coast[0][0] <= t:
            self.emit("move", dir=self.coast.pop(0)[1])


class Remote:
    """Reports -> gestures and button edges -> an ordered action queue that work() runs. No device I/O here, so the
    selftest drives it."""

    def __init__(self, shell, k, clock=time.monotonic):
        self.shell, self.k, self.clock, self.q = shell, k, clock, queue.Queue()
        self.mask, self.siri_off = 0, None
        self.reset()

    def reset(self):
        """No finger and no buttons, as after a reconnect. A Siri press the disconnect cut off ends here."""
        if self.mask & 0x10 or self.siri_off is not None:
            self.q.put(("voice", False))
        self.g = Gestures(self._gesture, self.k, self.clock)
        self.mask, self.siri_off, self.ticks, self.t = 0, None, None, 0.0

    def report(self, r):
        if len(r) < 3 or r[0] != 0xFC:
            return
        if r[1] == 0xE0:                         # the unlock's reply: what was held to wake it (Menu+Vol+, the pairing
            self.mask = r[2] & ~0x10             # combo) is where we start, not a press; a Siri hold still talks
            return
        old, self.mask = self.mask, r[2]
        for bit, name in BUTTONS.items():
            if self.mask & bit and not old & bit:
                if name != "siri":
                    self.q.put((name, None))
                elif self.siri_off is None:      # with the time it came: the agent's start of the remote's mic
                    self.q.put(("voice", True, self.clock()))
                else:                            # back within siri_release_s: the same press
                    self.siri_off = None
            elif old & bit and not self.mask & bit and name == "siri":
                self.siri_off = self.clock() + self.k["siri_release_s"]
        if len(r) == 14 and r[3] == 0x32:
            ticks, x, y, down = touch(r)
            dt = 0.0 if self.ticks is None else (ticks - self.ticks) % 65536 / 2000   # the clock wraps every 32.8 s
            self.ticks, self.t = ticks, self.t + dt
            if dt > GAP and self.g.down:
                self.g.frame(self.t, x, y, False)
            self.g.frame(self.t, x, y, down)

    def tick(self):
        self.g.tick()
        if self.siri_off is not None and self.clock() >= self.siri_off:
            self.siri_off = None
            self.q.put(("voice", False))

    def _gesture(self, kind, **f):
        if kind == "move":
            self.q.put(("move", f["dir"], self.clock()))
        elif kind == "swipe":
            self.q.put(("swipe", (f["dir"], f["speed"])))
        elif self.shell.surface == "launcher" or not (f["x"] or f["y"]):   # the settle always goes: no lean left behind
            self.shell.emit("tilt", {"x": f["x"], "y": f["y"]})

    def work(self):
        """The action thread: in order, and a slow action (a busy page, wpctl) never stalls the reader."""
        while True:
            self.do(*self.q.get())

    def do(self, name, arg, queued=None):
        """One queued action. All but voice check the surface and act under the Shell lock (an RLock: the action takes
        it again), so the surface can't change in between, as when _tick brings a game to the front."""
        sh = self.shell
        try:
            if name == "voice":                  # push-to-talk anywhere, like the gamepad's voice button
                if sh.on_voice and sh.settings.get("asr"):
                    sh.on_voice(arg, queued)
                elif arg:
                    sh.emit("toast", {"text": NO_VOICE, "kind": "info"})
                return
            with sh.lock:
                if sh.surface == "app" and name not in IN_APP:
                    pass
                elif name == "move":
                    if self.clock() - queued <= STALE_S:
                        sh.navigate(arg)
                elif name == "swipe":            # once per touch (the app switcher's close); an older Shell skips it
                    fn = getattr(sh, "swipe", None)
                    if fn:
                        fn(*arg)
                else:
                    fn, *a = ACTIONS[name]
                    getattr(sh, fn)(*a)
        except Exception:                        # (Shell actions don't raise; this keeps the thread alive regardless)
            log.exception("Siri Remote %s failed", name)


def knobs(cfg):
    """settings.siri_remote -> KNOBS with its numbers applied; anything else is logged and ignored. Each must be finite
    and at least 0; step, tilt_hz and coast_speed are divided by, so above 0 (a 0 would stop the reader thread). enabled
    is watch()'s, mic the agent's (the remote's own mic, through its tap)."""
    k = dict(KNOBS)
    for name, v in cfg.items():
        if name in KNOBS and type(v) in (int, float) and 0 <= v < math.inf and (   # (no NaN, inf or negatives)
                v > 0 or name not in ("step", "tilt_hz", "coast_speed")):
            k[name] = v
        elif name not in ("enabled", "mic"):
            log.warning("settings.siri_remote.%s = %r ignored: expected a number >= 0 (> 0 for step, tilt_hz and "
                        "coast_speed) for one of %s", name, v, ", ".join(KNOBS))
    return k


def find():
    """/dev/hidrawN of a connected gen-1 Siri Remote, else None."""
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(node + "/device/uevent") as f:
                if HID_ID in f.read().splitlines():
                    return "/dev/" + os.path.basename(node)
        except OSError:
            pass
    return None


def watch(shell):
    """Run forever (thread): find the remote (every 0.5 s while it's away: asleep, out of range, not paired), unlock it,
    act on its reports. No-op off Linux, in --demo and with settings.siri_remote.enabled false."""
    cfg = shell.settings.get("siri_remote", {})
    cfg = cfg if isinstance(cfg, dict) else {"enabled": bool(cfg)}   # "siri_remote": false works too
    if not sys.platform.startswith("linux") or shell.demo or not cfg.get("enabled", True):
        return
    rc = Remote(shell, knobs(cfg))
    threading.Thread(target=rc.work, name="siri-actions", daemon=True).start()
    while True:
        path, fd = find(), None
        try:
            if path:
                fd = os.open(path, os.O_RDWR)
                fcntl.ioctl(fd, HIDIOCSFEATURE, bytearray(UNLOCK))
                log.info("Siri Remote on %s", path)
                while True:                      # 10 ms: coasting steps and the Siri release are due between reports
                    if select.select([fd], [], [], .01)[0]:
                        r = os.read(fd, 64)
                        if not r:
                            break
                        rc.report(r)
                    rc.tick()
        except PermissionError:
            from .shell import warn_once         # (here: shell imports this module)
            warn_once("siri-access", "Siri Remote: no access to %s; 'deploy/install.sh root' installs the udev rule "
                      "that lets you read it (then re-pair, or press a button to reconnect it)", path)
        except OSError as e:
            log.info("Siri Remote %s: %s", path, e)          # gone (asleep, out of range) or the unlock failed
        except Exception:                        # a bug: say so and start over, not lose the remote until a restart
            log.exception("Siri Remote %s failed", path)
        finally:
            if fd is not None:
                os.close(fd)
                rc.reset()
        time.sleep(2 if fd is not None else .5)  # soon while it's away: presses between a wake and the unlock are lost


def _selftest():
    assert HIDIOCSFEATURE == 0xC0024806                       # HIDIOCSFEATURE(2), as tools/siri_probe.py checks it
    # decode: real reports from this remote (lab captures, 2026-10-08)
    assert touch(bytes.fromhex("fc0100320c340187acef42611584")) == (13324, -889, -262, True)   # state 4: touching
    assert touch(bytes.fromhex("fc01003298bc08e688f3c3160a8b"))[1:] == (-1818, -200, True)    # 3, landing; bit 0 clear
    assert touch(bytes.fromhex("fc0100329c60005859d8b9542084"))[1:] == (-1704, -635, True)    # 4, bit 0 clear
    assert touch(bytes.fromhex("fc010032a4d309427fd67a1c0562"))[1:] == (-190, -665, False)    # 2, hovering; bit 0 set
    assert touch(bytes.fromhex("fc01003234c301945d0800000087"))[1:] == (-620, 133, False)     # 7, the lift; bit 0 set
    assert touch(bytes.fromhex("fc010032621201ef3b13a1a0258c"))[1:3] == (-1041, 307)

    def pack(ticks, x, y, down=True):            # a touch report in that layout
        x, y = x & 0xFFF, y & 0xFFF
        return bytes([0xFC, 1, 0, 0x32, ticks & 255, ticks >> 8 & 255, 1, x & 255, x >> 8 | (y & 15) << 4, y >> 4,
                      *((0x80, 0x80, 0x20, 0x84) if down else (0, 0, 0, 0x87))])
    assert touch(pack(65535, -1704, 307)) == (65535, -1704, 307, True) and touch(pack(0, 5, -5, False)) == (0, 5, -5, False)

    class FakeShell:                             # records what the remote asks of the Shell
        def __init__(self):
            self.surface, self.settings, self.calls = "launcher", {"asr": {"server": "x"}}, []
            self.on_voice, self.lock = self.voice, threading.RLock()

        def voice(self, on, at):                 # a press says when it came; a release doesn't
            self.calls.append(("voice", on, at) if on else ("voice", on))

        def __getattr__(self, name):             # navigate, select, back, home, media, volume, emit
            return lambda *a: self.calls.append((name, *a))
    now = [100.0]
    sh, clock = FakeShell(), lambda: now[0]
    rc = Remote(sh, KNOBS, clock)

    def feed(*reports):                          # 10 ms apart, as the remote sends them
        for r in reports:
            rc.report(bytes.fromhex(r) if isinstance(r, str) else r)
            now[0] += .01
            rc.tick()

    def drain(tilts=False):                      # run the queued actions (work() does this on its thread): the
        while not rc.q.empty():                  # Shell calls made since the last drain, the tilt events or the rest
            rc.do(*rc.q.get_nowait())
        calls, sh.calls = sh.calls, []
        return [c[2] if tilts else c for c in calls if (c[:2] == ("emit", "tilt")) == tilts]
    # buttons (byte 2): click, Menu, TV, Play/Pause, Vol+, Vol-, Menu+Vol+ (the pairing combo), each released; a voice
    # packet and a two-finger frame with nothing held; the unlock's reply with the pairing combo held, let go: no action
    buttons = ("fc0080", "fc0000", "fc0020", "fc0000", "fc0001", "fc0000", "fc0008", "fc0000", "fc0002", "fc0000",
               "fc0004", "fc0000", "fc0022", "fc0000", "fc1000ff000e0045b81739355347a3ab9added257f",
               "fc0200323c4409e6181e7a040aac6efc05c67d1274", "fce0226002", "fc0000")
    want = [("select",), ("back",), ("home",), ("media", "toggle"), ("volume", "up"), ("volume", "down"), ("back",),
            ("volume", "up")]
    for surface in ("launcher", "web"):
        sh.surface = surface
        feed(*buttons)
        assert drain() == want and not rc.g.down, surface
    sh.surface = "app"                           # a game: TV, Play/Pause and volume only
    feed(*buttons)
    assert drain() == [("home",), ("media", "toggle"), ("volume", "up"), ("volume", "down"), ("volume", "up")]
    # a real click (lab3): it shows only in the touch frames' byte 2, as most do; a 3-byte press that never got its
    # 3-byte release (also seen) doesn't hide it
    click = ("fc01003238fc01245ce35b64168b fc0100324cfc0126ace35b651a8c fc01003260fc0126ace3616a1e8c "
             "fc01003274fc0127cce3666f208c fc01003288fc0128ece36b74218c fc0100329cfc01291ce46e77228c "
             "fc010032b0fc01293ce4727b238c fc010032c4fc01296ce4757e248c fc018032d9fc012a7ce47981268c "
             "fc018032edfc012b7ce47d84268c fc01803201fd012e8ce48187278c fc01803215fd01307ce48488278c "
             "fc01803229fd01347ce48789288c fc0180323dfd01376ce4878a288c fc01803251fd01395ce4888a288c "
             "fc01803265fd013b2ce4888a288c fc01803279fd013dece3878b288c fc0180328dfd013d9ce3878b288c "
             "fc018032a1fd013c3ce3868b288c fc018032b5fd0139cce2858a278c fc010032c8fd01366ce28289268c "
             "fc010032dcfd01311ce27f87248c fc010032f0fd012c0ce27780208c fc01003204fe01260ce26772178c "
             "fc01003218fe011f5ce26571058c fc0100322cfe011f5ce20000008f fc01003240fe011f5ce200000088").split()
    sh.surface = "launcher"
    feed("fc0080", *click)
    assert drain() == [("select",), ("select",)] and not rc.g.down
    # a move that waited out a busy Shell (a tile opening, wpctl) is dropped, not sent in a burst with the next ones
    rc._gesture("move", dir="left")
    now[0] += STALE_S + .01
    rc._gesture("move", dir="right")
    assert drain() == [("navigate", "right")]

    class Tick:                                  # the Shell lock, held by a _tick that brings a game to the front
        def __enter__(self):
            sh.surface = "app"

        def __exit__(self, *exc):
            pass
    sh.lock = Tick()                             # checked under the lock, a launcher move never reaches the game
    rc._gesture("move", dir="right")
    assert drain() == []
    sh.surface, sh.lock = "launcher", threading.RLock()
    # a real flick right (fast and short, to the lift's two zero frames) coasts one step once the finger is gone
    flick = ("fc0100323a2d0537fae296692084 fc0100324e2d05644be694822084 fc010032622d053f3ce88d841f84 "
             "fc010032762d05644dea8c842184 fc0100328a2d05dbddea8e7d1fa4 fc0100329e2d052b4eeb8e6b1ba4 "
             "fc010032b22d0566beeb8a511684 fc010032c62d05913eec7a431284 fc010032da2d05b2ceec68350c84 "
             "fc010032ee2d05cc6eed70290784 fc010032022e05cc6eed00000087 fc010032162e05cc6eed00000080").split()
    rc.g.k = dict(rc.g.k, flick=4200)           # this captured flick (~6000 u/s) checks coasting per surface, not the
    for surface in ("launcher", "web", "app"):  # default threshold
        sh.surface = surface
        feed(*flick)
        swipe = drain()                          # one swipe at the lift, before the coasting; none in a game
        assert len(rc.g.coast) == 1 and ([c[:2] for c in swipe] == ([] if surface == "app" else [("swipe", "right")])
                                         and all(c[2] >= 4200 for c in swipe)), (surface, swipe, rc.g.coast)
        now[0] += .2
        rc.tick()
        assert drain() == ([] if surface == "app" else [("navigate", "right")]), surface
    errs, log.exception = [], lambda *a, **kw: errs.append(a)   # a Shell older than swipe() skips it quietly
    Remote(type("Old", (), {"surface": "launcher", "lock": threading.RLock()})(), KNOBS).do("swipe", ("up", 9e3))
    del log.exception
    assert not errs, errs
    # tilt: a resting finger that drifts leans the launcher's focused tile; the settle (0, 0) at the lift always goes
    for surface in ("launcher", "web", "app"):
        sh.surface = surface
        feed(*(pack(20 * i, -900 + 5 * i, -300) for i in range(40)), pack(800, -700, -300, False))
        tilts = drain(tilts=True)
        assert tilts[-1] == {"x": 0.0, "y": 0.0} and (len(tilts) > 1) == (surface == "launcher"), (surface, tilts)
    # Siri is push-to-talk, in a game too; the bit's 20-40 ms drop mid-press is the same press
    sh.surface = "app"
    t = now[0]
    feed("fc0010", "fc1010")                     # press (with when it came), then a short voice-kind report (byte 1):
    assert drain() == [("voice", True, t)]       # still held
    feed("fc1000", "fc0010", "fc0000")           # dropped for 10 ms and back; then let go: it counts after a while
    assert drain() == []
    now[0] += .2
    rc.tick()
    assert drain() == [("voice", False)]
    t = now[0]
    feed("fc0010")
    rc.reset()                                   # the remote dropped off mid-press: the recording ends
    assert drain() == [("voice", True, t), ("voice", False)]
    sh.settings = {}                             # no speech recognition set up: a toast instead
    feed("fc0010", "fc0000")
    now[0] += .2
    rc.tick()
    assert drain() == [("emit", "toast", {"text": NO_VOICE, "kind": "info"})]
    # the remote's clock wraps; a gap in it is a lift whose zero frames were lost
    feed(pack(65530, 0, 0))
    t0 = rc.t
    feed(pack(14, 0, 0))
    assert abs(rc.t - t0 - .01) < 1e-9 and rc.g.n == 2
    feed(pack(250, 0, 0))
    assert abs(rc.t - t0 - .128) < 1e-9 and rc.g.n == 1
    # the engine on synthetic strokes (the host clock advances with the frames)
    out, t = [], [0.0]
    g = Gestures(lambda kind, **f: out.append((kind, f)), KNOBS, clock)

    def stroke(path):
        out.clear()
        for x, y in path:
            g.frame(t[0], x, y, True)
            t[0] += .01
            now[0] += .01
        g.frame(t[0], 0, 0, False)
        return [f["dir"] for kind, f in out if kind == "move"]
    swipes = lambda: [f["dir"] for kind, f in out if kind == "swipe"]                            # once per touch
    assert stroke([(5 * math.sin(i), 4 * math.cos(i * 1.3)) for i in range(120)]) == []         # resting: jitter only
    assert not any(kind == "tilt" and (f["x"] or f["y"]) for kind, f in out) and not swipes(), out
    assert stroke([(0, 0)] + [(400, 0)] * 2 + [(400 + 8 * i, 0) for i in range(40)]) == []      # the landing jump
    assert not swipes()
    assert stroke([(9 * i, 0) for i in range(200)]) == ["right"] * 3 and swipes() == ["right"]  # ~3 per surface, paced
    assert stroke([(0, -60 * i) for i in range(12)]) == [] and swipes() == ["down"] and not g.coast  # short and fast
    assert stroke([(0, 27 * i) for i in range(20)]) == [] and not swipes()      # short, slower: a finger repositioning
    assert stroke([(0, 9 * i) for i in range(200)]) == ["up"] * 3
    assert stroke([(90 * i, 0) for i in range(40)]) == ["right"] * 3                             # fast: still paced
    there_and_back = lambda n: stroke([(9 * i, 0) for i in range(70)] + [(630 - 9 * i, 0) for i in range(n)])
    assert there_and_back(85) == ["right"] and there_and_back(110) == ["right", "left"]   # back takes 1.4 steps
    assert swipes() == ["left"]                                                              # the last step's way
    assert stroke([(-400 * i, 0) for i in range(8)]) == [] and len(g.coast) == KNOBS["coast_max"]   # a hard flick
    assert [(f["dir"], f["speed"] >= KNOBS["flick"]) for kind, f in out if kind == "swipe"] == [("left", True)]
    gaps = [b[0] - a[0] for a, b in zip(g.coast, g.coast[1:])]
    assert all(abs(b / a - KNOBS["friction"]) < 1e-9 for a, b in zip(gaps, gaps[1:])), gaps        # friction
    now[0] = g.coast[0][0]
    g.tick()
    assert [f["dir"] for kind, f in out if kind == "move"] == ["left"] and len(g.coast) == 3
    g.frame(t[0], 0, 0, True)                    # the next touch stops the coasting
    assert not g.coast
    # settings: numbers override; typos, other types, negatives, NaN, inf and a 0 that's divided by are ignored; in
    # --demo (and off Linux) watch() returns
    assert knobs({"enabled": True, "step": 400, "settle": 0, "flik": 1, "pace_s": "fast", "guard": True, "tilt_hz": 0,
                  "coast_speed": -1, "beta": math.inf, "lock": math.nan}) == {**KNOBS, "step": 400, "settle": 0}
    warned, log.warning = [], lambda *a: warned.append(a)   # enabled and mic are settings, not knobs: no warning
    assert knobs({"enabled": False, "mic": False}) == KNOBS and not warned
    del log.warning

    class Demo:
        settings, demo = {}, True
    assert watch(Demo()) is None
