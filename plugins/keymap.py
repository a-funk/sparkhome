"""keymap: any Linux input device as a SparkHome controller (an IR receiver, a presenter, a macro pad, a keyboard).

The template for a controller plugin. It reads evdev events from the devices whose name contains `match` and turns
each mapped key into a SparkHome button (plugins.BUTTONS), or into push-to-talk with "voice".

    "plugins": {"keymap": {"match": "flirc", "grab": true,
                           "map": {"KEY_UP": "up", "KEY_DOWN": "down", "KEY_LEFT": "left", "KEY_RIGHT": "right",
                                   "KEY_ENTER": "select", "KEY_BACKSPACE": "back", "KEY_HOMEPAGE": "home",
                                   "KEY_PLAYPAUSE": "playpause", "KEY_VOICECOMMAND": "voice"}}}

match: part of the device name, any case (`python3 -m sparkhome.inputs --probe` lists the names and key codes).
map: key name (inputs.NAMES) or the code number --probe prints -> button. Held arrows and volume repeat.
grab (default true): the keys reach SparkHome alone, not also the window in front. Off for a keyboard you type on.
Reading a keyboard-type device needs access to its /dev/input/event* node: docs/EXTENDING.md, "Device access".
"""
import fcntl
import glob
import os
import select
import time

from sparkhome import inputs

EVIOCGRAB = 0x40044590                           # _IOW('E', 0x90, int)
REPEATS = {"up", "down", "left", "right", "volume_up", "volume_down"}


def keymap(cfg):
    """settings -> {key code: button}; unknown key names are skipped."""
    out = {}
    for name, button in (cfg.get("map") or {}).items():
        c = inputs.code(name)
        if c is not None:
            out[c] = str(button)
    return out


def actions(events, keys):
    """[(type, code, value)] -> [button | ("voice", pressed)]: presses, the repeats of REPEATS, voice press+release."""
    out = []
    for typ, c, val in events:
        b = keys.get(c) if typ == inputs.EV_KEY else None
        if b == "voice" and val in (0, 1):
            out.append(("voice", val == 1))
        elif b and (val == 1 or val == 2 and b in REPEATS):
            out.append(b)
    return out


def devices(match):
    """/dev/input/eventN paths whose device name contains match (any case)."""
    found = []
    for p in sorted(glob.glob("/sys/class/input/event*/device/name")):
        try:
            with open(p) as f:
                if match and match.lower() in f.read().lower():
                    found.append("/dev/input/" + p.split("/")[4])
        except OSError:
            pass
    return found


def start(home):
    keys, match = keymap(home.config), str(home.config.get("match") or "")
    if home.demo or not keys or not match:
        home.log.info("keymap idle: %s", "demo mode" if home.demo else "set match and map in its settings")
        return
    fds = {}
    while True:
        for path in set(devices(match)) - set(fds.values()):   # (re)plugged devices: every 5 s
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                if home.config.get("grab", True):
                    fcntl.ioctl(fd, EVIOCGRAB, 1)
                fds[fd] = path
                home.log.info("reading %s", path)
            except OSError as e:
                home.log.warning("can't read %s: %s (docs/EXTENDING.md, Device access)", path, e)
        until = time.monotonic() + 5
        while fds and time.monotonic() < until:
            for fd in select.select(list(fds), [], [], 1)[0]:
                try:
                    buf = os.read(fd, inputs.EV.size * 64)
                except OSError:
                    buf = b""
                if not buf:                      # unplugged
                    home.log.info("lost %s", fds.pop(fd))
                    os.close(fd)
                    continue
                for a in actions(inputs.parse(buf), keys):
                    if isinstance(a, tuple):
                        home.voice(a[1])
                    else:
                        home.press(a)
        if not fds:
            time.sleep(5)


def _selftest():
    keys = keymap({"map": {"KEY_UP": "up", "KEY_ENTER": "select", "316": "home", "KEY_VOICECOMMAND": "voice",
                           "KEY_NOPE": "x"}})
    assert keys == {103: "up", 28: "select", 316: "home", 582: "voice"}, keys
    K = inputs.EV_KEY
    ev = [(K, 103, 1), (K, 103, 2), (K, 103, 0), (K, 28, 1), (K, 28, 2), (K, 28, 0), (inputs.EV_ABS, 103, 1),
          (K, 582, 1), (K, 582, 2), (K, 582, 0), (K, 999, 1)]
    assert actions(ev, keys) == ["up", "up", "select", ("voice", True), ("voice", False)], actions(ev, keys)
