"""SparkHome Settings: the values behind the TV's Settings page, where each one lives and what changing it does.

- What the owner changes on the TV (brightness, color range, spoken replies, sleep) is kept in STATE: a flat
  {"display.brightness": 120, …}, mode 600, written atomically. Prefs.load() merges it over the config in use, key by
  key and in place, into shell.settings: the dict the Agent and the Shell's sleep read live. The config replaces the
  repo default whole and deploys never touch STATE, so the owner's choices survive both (rm STATE to reset).
- Volume, mute and the output are never stored here: PipeWire and WirePlumber keep them. The display mode is mutter's
  ~/.config/monitors.xml, which Keep rewrites itself.
- Brightness is a software transfer curve (xrandr's gamma ramp on HDMI-0): the TCL has no DDC/CI and NVIDIA has no CEC,
  so the Spark can't reach the TV's backlight. A new mode is applied with mutter's method 1 (temporary: method 2 pops a
  gnome-shell dialog that reverts in about 20 s when nobody can see it [M]) behind SparkHome's own 15 s Keep, and the
  display guard puts the saved mode back whenever mutter shows another (a hotplug, a crash).
- Every subprocess has a timeout and degrades to an error value, never an exception in a request thread. --demo runs
  none: the measured Spark's values (2026-10-11), changed in memory.
Stdlib only. It takes the Shell as an argument and never imports shell.py.   Self-check: python3 -m sparkhome --selftest
"""
import functools
import glob
import hashlib
import html
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from . import siri

log = logging.getLogger("sparkhome.settings")
STATE = Path("~/.local/state/sparkhome/settings.json").expanduser()
MONITORS = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "monitors.xml"
KEEP_S, GUARD_S = 15, 10     # a new mode's Keep countdown; how often the display guard reads mutter's state
GUARD_EVERY_S = 30           # ...and how seldom it may re-apply the saved mode (it fights a manual xrandr --mode)
APPLY_S = 20                 # busctl's wait for ApplyMonitorsConfig: mutter replies once the modeset and gnome-shell's
                             # relayout are done [I] (an NVIDIA + TCL 4K modeset's time is unmeasured)
POWER_DELAY_S = 1            # a restart or shutdown waits this long after its answer, so the page shows why first
STARTED = time.monotonic()   # SparkHome's uptime (About): this module loads with the daemon
SECTIONS = ("display", "sound", "voice", "sleep", "controllers", "network", "power", "about")
KEYS = {"display.mode": "display", "display.brightness": "display", "display.color_range": "display",
        "sound.volume": "sound", "sound.muted": "sound", "sound.output": "sound",
        "tts.enabled": "voice", "tts.voice": "voice", "tts.volume": "voice",
        "sleep.minutes": "sleep", "sleep.game_minutes": "sleep", "sleep.signal": "sleep"}
AGENTS = {"display.brightness", "sound.volume", "sound.muted", "sleep.minutes"}  # all an agent may change
LIVE = {"display.mode", "sound.volume", "sound.muted", "sound.output"}  # read and set where they live, never stored
OVERLAY = set(KEYS) - LIVE
# The sleep defaults, one place (D7: sleep is off until the owner turns it on). load() fills them into
# shell.settings["sleep"] under the config's own, so the Shell's sleep never needs its own copy.
SLEEP = {"minutes": 0, "game_minutes": 0, "signal": "off"}
RANGES = {"display.brightness": (50, 150, 1), "sound.volume": (0, 100, 1), "tts.volume": (10, 100, 10)}
DEFAULTS = {"display.brightness": 100, "tts.volume": 100}


def minutes_label(m):
    """60 -> "1 hour", 120 -> "2 hours", 30 -> "30 minutes"."""
    return f"{m // 60} hour{'s' * (m > 60)}" if m and not m % 60 else f"{m} minutes"


CHOICES = {  # the static pickers (the backend and the page agree on them); the UI owns all other copy
    "display.color_range": [{"value": "full", "label": "Full (0–255)"}, {"value": "limited", "label": "Limited (16–235)"}],
    "sleep.minutes": [{"value": m, "label": minutes_label(m) if m else "Off"} for m in (0, 5, 10, 15, 30, 60)],
    "sleep.game_minutes": [{"value": m, "label": minutes_label(m) if m else "Never"} for m in (0, 30, 60, 120)],
    "sleep.signal": [{"value": "off", "label": "Turn off the signal", "detail": "The TV goes to standby when SparkHome "
                      "can reach it, else shows No signal"},
                     {"value": "black", "label": "Show black", "detail": "The signal stays on and the screen is black"}]}
TV_SIZES = {(3840, 2160): "4K", (1920, 1080): "1080p", (1280, 720): "720p"}  # the Resolution picker's sizes
MUTTER = ("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig", "org.gnome.Mutter.DisplayConfig")
VOICES = "~/.local/share/sparkhome/tts/kokoro/voices-v1.0.bin"  # where the README's install puts Kokoro's voices
POWER_SUPPLY = "/sys/class/power_supply"  # a pad's battery when BlueZ has none (hid-playstation: ps-controller-battery-<mac>)
# The person's power actions (no sudo: polkit lets the session's own daemon reboot and power off [M]), and what the page
# says meanwhile
POWER = {"restart_sparkhome": (["systemctl", "--user", "restart", "--no-block", "sparkhome.service"],
                               "Restarting SparkHome…", "(demo) SparkHome would restart now."),
         "reboot": (["systemctl", "reboot"], "Restarting the Spark…", "(demo) The Spark would restart now."),
         "shutdown": (["systemctl", "poweroff"], "Shutting down the Spark…", "(demo) The Spark would shut down now.")}
PERSON = ("Only the person at the TV changes {key}: Settings › {section} (agents may set display.brightness, "
          "sound.volume, sound.muted and sleep.minutes).")
PERSON_MODE = "Only the person at the TV changes the resolution: Settings › Display, or say “set the resolution to 1080p”."
PERSON_POWER = "Only the person at the TV restarts or shuts down the Spark: Settings › Power."
NO_TV = "No display connected (is the TV on?)"
# --demo: the Spark as measured on 2026-10-11 (mutter's TV modes, the TCL's monitor spec, the paired devices, the box)
TV = ["HDMI-0", "TCL", "50S425", "0x00000000"]
DEMO_MODES = ("3840x2160@30.000 3840x2160@59.940 4096x2160@59.940 3840x2160@29.970 3840x2160@23.976 1920x1080@60.000 "
              "1920x1080@59.939 1920x1080@29.972 1920x1080@23.977 1680x1050@59.954 1280x720@60.000 1280x720@59.943 "
              "1280x720@29.972 1280x720@23.976").split()
DEMO_SINK = {"id": 44, "name": "alsa_output.platform-NVDA2014_00.hdmi-stereo", "label": "TV (HDMI)"}
DEMO_DEVICES = [{"name": "Siri Remote", "kind": "remote", "connected": True, "battery": 64},
                {"name": "Xbox Wireless Controller", "kind": "controller", "connected": True, "battery": 80},
                {"name": "DualSense Wireless Controller", "kind": "controller", "connected": False, "battery": None}]
DEMO_ADDRESSES = [{"kind": "Wi-Fi", "address": "192.168.1.20"}, {"kind": "Tailscale", "address": "100.64.0.2"}]
DEMO_BOX = {"device": "NVIDIA DGX Spark", "os": "Ubuntu 24.04.4 LTS", "kernel": "6.17.0-1029-nvidia",
            "driver": "580.173.02"}
DEMO_VOICES = [f"{p}_{n}" for p, ns in (("af", "alloy aoede bella heart jessica kore nicole nova river sarah sky"),
                                        ("am", "adam echo eric fenrir liam michael onyx puck santa"),
                                        ("bf", "alice emma isabella lily"), ("bm", "daniel fable george lewis"))
               for n in ns.split()]


def _run(argv, timeout=3):
    """-> (exit code, stdout, stderr); the code is None when it couldn't run or ran out of time. Never raises."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return None, "", str(e)


def _spawn(fn, *a):
    def run():
        try:
            fn(*a)
        except Exception:
            log.exception("%s failed", getattr(fn, "__name__", fn))
    threading.Thread(target=run, daemon=True).start()


def _int(v):
    """A whole number as JSON sends one (120, 120.0), never a bool."""
    return v if type(v) is int else int(v) if type(v) is float and v.is_integer() else None


def _last(err, default):
    """The last line a tool wrote to stderr: what it says went wrong."""
    lines = [x for x in str(err).strip().splitlines() if x.strip()]
    return lines[-1].strip()[:200] if lines else default


def snap(n):
    """Idle minutes said -> the sleep timer's nearest choice (ties go up): 20 -> 15, 45 -> 1 hour, an hour or more ->
    1 hour. Any wait at all is at least 5 minutes, never Off: 0 alone is Off."""
    return 0 if n <= 0 else min((5, 10, 15, 30, 60), key=lambda c: (abs(c - n), -c))


def xrandr_argv(output, pct, dim=False):
    """The owner's brightness as xrandr's ramp on output: (i/n)^(1/gamma) × brightness, clipped at 1 [I]. At most 100 %,
    brightness scales everything down; above, gamma = pct/100 lifts dark and middle tones while black and white stay put
    (brightness above 1 would clip the highlights). dim (the sleep warning) halves the picture. Both flags always:
    xrandr derives a missing one from the CRTC's current ramp."""
    g, b = (pct / 100, 1) if pct > 100 else (1, pct / 100)
    b *= .5 if dim else 1
    return ["xrandr", "--output", output, "--gamma", f"{g:g}:{g:g}:{g:g}", "--brightness", f"{b:g}"]


def _hz(r):
    return f"{r:.2f}".rstrip("0").rstrip(".")


def mode_label(w, h, refresh):
    return f"{TV_SIZES.get((w, h)) or f'{w}×{h}'} · {_hz(refresh)} Hz"


def parse_state(obj):
    """mutter's GetCurrentState, as busctl --json=short prints it -> the first monitor's {"serial", "spec" [connector,
    vendor, product, serial], "monitor" ("TCL 50S425": mutter's display-name for it is wrong), "monitors" (how many),
    "scale" (its logical monitor's), "current" (mode id, None: not driven), "all" {id: (w, h, refresh)}, "modes" (the
    picker's: the TV sizes, plus the current mode whatever it is, by height then refresh, both descending)}; None with no
    monitor. The EDID-preferred mode isn't marked: it is 4K30, which gives no picture on this cable [M]."""
    serial, monitors, logical = obj["data"][:3]
    if not monitors:
        return None
    spec, modes = list(monitors[0][0]), monitors[0][1]
    every = {m[0]: (m[1], m[2], m[3]) for m in modes}
    cur = next((m[0] for m in modes if ((m[6] or {}).get("is-current") or {}).get("data")), None)
    ids = sorted((i for i, (w, h, _) in every.items() if (w, h) in TV_SIZES or i == cur),
                 key=lambda i: (-every[i][1], -every[i][2]))
    return {"serial": serial, "spec": spec, "monitor": f"{spec[1]} {spec[2]}", "monitors": len(monitors),
            "scale": next((lm[2] for lm in logical if spec in [list(s) for s in lm[5]]), 1.0), "current": cur,
            "all": every, "modes": [{"id": i, "width": every[i][0], "height": every[i][1], "refresh": every[i][2],
                                     "label": mode_label(*every[i]), "current": i == cur} for i in ids]}


def apply_argv(serial, connector, mode, scale, method=1):
    """ApplyMonitorsConfig (uua(iiduba(ssa{sv}))a{sv}) [I until the live test]: the serial, method 1 (temporary), one
    logical monitor (x 0, y 0, scale, transform 0, primary) of one monitor (connector, mode id, no properties), no
    properties. Scale in full (repr): mutter matches it against its supported scales exactly."""
    return ["busctl", "--user", "call", *MUTTER, "ApplyMonitorsConfig", "uua(iiduba(ssa{sv}))a{sv}", str(serial),
            str(method), "1", "0", "0", repr(float(scale)), "0", "true", "1", connector, mode, "0", "0"]


def pick_mode(said, every):
    """The mode words name ("4k", "1080p", "4k@30", "4k at 30 hz", "1920x1080@59.94", a mutter id) among every {id: (w,
    h, refresh)} -> its id, else None. No rate said: the highest up to 60.5 Hz (this TV's panel is 60 Hz [M]), else the
    lowest; a rate said: the closest."""
    s = " ".join(str(said).lower().replace("×", "x").split())
    if s in every:
        return s
    m = re.fullmatch(r"(?:(4k|uhd|2160p?|1080p?|full ?hd|fhd|720p?)|(\d{3,4}) ?(?:x|by) ?(\d{3,4}))(?: ?(?:@|at) ?"
                     r"(\d{2,3}(?:[. ]\d{1,3})?)(?: ?hz| hertz)?| (\d{2,3}(?:[. ]\d{1,3})?) ?(?:hz|hertz))?", s)
    if not m:
        return None
    size = (int(m[2]), int(m[3])) if m[2] else (3840, 2160) if m[1] in ("4k", "uhd", "2160", "2160p") else \
        (1920, 1080) if m[1] in ("1080", "1080p", "full hd", "fullhd", "fhd") else (1280, 720)
    ids = [i for i, (w, h, _) in every.items() if (w, h) == size]
    hz = m[4] or m[5]
    if not ids:
        return None
    if hz:
        return min(ids, key=lambda i: abs(every[i][2] - float(hz.replace(" ", "."))))
    tv = [i for i in ids if every[i][2] <= 60.5]
    return max(tv, key=lambda i: every[i][2]) if tv else min(ids, key=lambda i: every[i][2])


_SPEC = ("connector", "vendor", "product", "serial")
# A configuration mutter writes, for one monitor (its format: two spaces a level)
_CONFIG = """  <configuration>
    <logicalmonitor>
      <x>0</x>
      <y>0</y>
      <scale>{scale:g}</scale>
      <primary>yes</primary>
      <monitor>
        <monitorspec>
          <connector>{0}</connector>
          <vendor>{1}</vendor>
          <product>{2}</product>
          <serial>{3}</serial>
        </monitorspec>
        <mode>
          <width>{w}</width>
          <height>{h}</height>
          <rate>{r:.3f}</rate>
        </mode>
      </monitor>
    </logicalmonitor>
  </configuration>
"""


def _text(block, tag):
    m = re.search(rf"<{tag}>([^<]*)</{tag}>", block)
    return html.unescape(m[1].strip()) if m else None


def _configs(xml_text, spec):
    """monitors.xml's <configuration>s (as matches) for exactly this monitor set: every <monitorspec> in one (logical
    and disabled) is spec, and there is one. Read as text, not XML: mutter writes a fixed format with no attributes
    inside the root (and the Mac's Homebrew Python 3.14 can't load pyexpat [M]). ponytail: a comment holding tags
    would be read as them; mutter writes none."""
    for m in re.finditer(r"<configuration>.*?</configuration>", xml_text, re.S):
        if [[_text(ms, k) or "" for k in _SPEC] for ms in re.findall(r"<monitorspec>.*?</monitorspec>", m[0], re.S)] \
                == [list(spec)]:
            yield m


def saved_mode(xml_text, spec):
    """monitors.xml's mode for this monitor alone -> (w, h, rate, scale), else None (no file, no configuration for
    it, or one SparkHome can't read)."""
    try:
        c = next(_configs(xml_text, spec), None)[0]
        mode = re.search(r"<mode>.*?</mode>", c, re.S)[0]
        return int(_text(mode, "width")), int(_text(mode, "height")), float(_text(mode, "rate")), \
            float(_text(c, "scale") or 1)
    except (TypeError, ValueError):
        return None


def save_mode(xml_text, spec, mode, scale):
    """monitors.xml (text) with this monitor's configuration at mode (w, h, refresh) and scale, every other byte as it
    was. The rate is written to 3 decimals: within mutter's 0.001 match of each listed mode [I]. No configuration for
    it: one is appended, as mutter writes them (x 0, y 0, primary); no file: a new one. ponytail: interlaced modes
    (none on this TV [M]) would lose their <flag>."""
    w, h, r = mode
    c = next(_configs(xml_text, spec), None)
    if c is None:
        block = _CONFIG.format(*(html.escape(x, quote=False) for x in spec), scale=scale, w=w, h=h, r=r)
        end = xml_text.rfind("</monitors>")
        if end < 0:
            return '<monitors version="2">\n' + block + "</monitors>\n"
        pre = xml_text[:end].rstrip(" ")
        return pre + ("" if pre.endswith("\n") else "\n") + block + xml_text[end:]
    vals = {"width": str(w), "height": str(h), "rate": f"{r:.3f}"}
    new = re.sub(r"<scale>[^<]*</scale>", f"<scale>{scale:g}</scale>", c[0], count=1)
    new = re.sub(r"<mode>.*?</mode>", lambda m: re.sub(r"<(width|height|rate)>[^<]*</\1>",
                                                      lambda x: f"<{x[1]}>{vals[x[1]]}</{x[1]}>", m[0]), new, 1, re.S)
    return xml_text[:c.start()] + new + xml_text[c.end():]


def guard_mode(state, saved, pending):
    """The mode the display guard re-applies, else None: with nothing pending, a monitor and a saved mode for it that
    mutter lists, while mutter shows another (the same size, refresh within 0.002, counts as the same)."""
    if pending or not state or not saved:
        return None
    w, h, rate = saved[:3]
    same = lambda i: i in state["all"] and state["all"][i][:2] == (w, h) and abs(state["all"][i][2] - rate) < .002  # noqa: E731
    want = next((i for i in state["all"] if same(i)), None)
    return None if want is None or same(state["current"]) else want


def parse_bluez(obj, battery=lambda mac: None):
    """BlueZ's GetManagedObjects (busctl --json=short) -> ("on" | "off" | "unavailable", the paired devices: {"name",
    "kind", "connected", "battery"}, connected first). Apple's Modalias v004Cp026D is the Siri Remote (its name is its
    serial number); a battery is BlueZ's Battery1, else battery(mac)."""
    objs, var = obj["data"][0], lambda d, k: ((d or {}).get(k) or {}).get("data")  # noqa: E731
    adapter = next((i["org.bluez.Adapter1"] for i in objs.values() if "org.bluez.Adapter1" in i), None)
    devs = []
    for ifs in objs.values():
        d = ifs.get("org.bluez.Device1")
        if not var(d, "Paired"):
            continue
        apple = str(var(d, "Modalias") or "").lower().startswith("bluetooth:v004cp026d")
        icon, pct = str(var(d, "Icon") or ""), var(ifs.get("org.bluez.Battery1"), "Percentage")
        devs.append({"name": "Siri Remote" if apple else var(d, "Alias") or var(d, "Name") or var(d, "Address"),
                     "kind": "remote" if apple else "controller" if icon == "input-gaming" else "keyboard" if icon ==
                     "input-keyboard" else "audio" if icon.startswith("audio") else "device",
                     "connected": bool(var(d, "Connected")),
                     "battery": pct if pct is not None else battery(str(var(d, "Address") or ""))})
    devs.sort(key=lambda x: (not x["connected"], x["name"].lower()))
    return "unavailable" if adapter is None else "on" if var(adapter, "Powered") else "off", devs


def _battery(mac):
    for p in glob.glob(os.path.join(POWER_SUPPLY, f"*{mac.lower()}*", "capacity")) if mac else ():
        try:
            return int(Path(p).read_text())
        except (OSError, ValueError):
            pass
    return None


def parse_sinks(objs):
    """pw-dump -> ([{"id", "name" (node.name), "label"}] of the Audio/Sink nodes, the default sink's node.name). An
    HDMI sink is the TV, labelled "TV (HDMI)"; any other (Bluetooth headphones) by its description."""
    sinks, default = [], None
    for o in (o for o in objs if isinstance(o, dict)) if isinstance(objs, list) else ():
        p = (o.get("info") or {}).get("props") or {}
        if o.get("type") == "PipeWire:Interface:Node" and p.get("media.class") == "Audio/Sink" and p.get("node.name"):
            sinks.append({"id": o.get("id"), "name": p["node.name"], "label": "TV (HDMI)" if str(p.get(
                "api.alsa.path", "")).startswith("hdmi") else p.get("node.description") or p["node.name"]})
        if o.get("type") == "PipeWire:Interface:Metadata" and (o.get("props") or {}).get("metadata.name") == "default":
            for m in o.get("metadata") or []:
                v = m.get("value")
                if m.get("key") == "default.audio.sink":
                    try:
                        v = json.loads(v) if isinstance(v, str) else v
                    except ValueError:
                        v = None
                    default = v.get("name") if isinstance(v, dict) else None
    return sinks, default


def parse_addresses(ifaces):
    """ip -j -4 addr show -> [{"kind": "Wi-Fi" | "Ethernet" | "Tailscale", "address"}]; docker, bridges and the rest
    skipped, and an interface with no IPv4 address (Ethernet unplugged) is no row."""
    kinds = (("wl", "Wi-Fi"), ("en", "Ethernet"), ("eth", "Ethernet"), ("tailscale", "Tailscale"))
    out = []
    for i in (i for i in ifaces if isinstance(i, dict)) if isinstance(ifaces, list) else ():
        kind = next((k for p, k in kinds if str(i.get("ifname", "")).startswith(p)), None)
        out += [{"kind": kind, "address": a["local"]} for a in i.get("addr_info") or []
                if kind and a.get("family") == "inet" and a.get("local")]
    return out


def voice_names(path):
    """Kokoro's English voices in its voices file (a zip of <name>.npy): a… American, b… British English; f… female, m…
    male. Sorted, which is by accent, then gender, then name. [] when it can't be read."""
    try:
        with zipfile.ZipFile(os.path.expanduser(path)) as z:
            names = z.namelist()
    except (OSError, zipfile.BadZipFile):
        return []
    return sorted(n[:-4] for n in names if re.fullmatch(r"[ab][fm]_[a-z]+\.npy", n))


def voice_choice(name):
    """"bf_emma" -> {"value": "bf_emma", "label": "Emma", "detail": "British · female"}."""
    return {"value": name, "label": name[3:].replace("_", " ").title(),
            "detail": f"{'American' if name[0] == 'a' else 'British'} · {'female' if name[1] == 'f' else 'male'}"}


def _flag(argv, name):
    """The value after name in an argv list (tts.server_cmd's --voice, --voices), else None."""
    argv = argv if isinstance(argv, list) else []
    i = argv.index(name) + 1 if name in argv else len(argv)
    return argv[i] if i < len(argv) and isinstance(argv[i], str) else None


@functools.lru_cache(maxsize=1)
def _build():
    root, h, newest = Path(__file__).resolve().parent.parent, hashlib.sha1(), 0.0
    for p in sorted([*root.glob("sparkhome/*.py"), *root.glob("ui/*")]):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode() + b"\0" + p.read_bytes())
            newest = max(newest, p.stat().st_mtime)
    return h.hexdigest()[:7], time.strftime("%Y-%m-%d", time.localtime(newest))


def build():
    """This SparkHome: (the first 7 hex digits of SHA-1 over sparkhome/*.py and ui/*, by sorted name, the newest of
    their mtimes as a date), once per run. The Spark's copy has no .git. (None, None) while a file can't be read."""
    try:
        return _build()
    except OSError:
        return None, None


def _read(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def _write(path, text, mode):
    """text into path atomically (a temp file beside it, then os.replace): a reader sees the old file or the new one."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), mode)  # (O_CREAT's mode is only for a new file, and the umask may take bits off it)
        f.write(text)
    os.replace(tmp, path)


def _getstate(serial, current, ids, spec=TV):
    """A GetCurrentState answer in busctl's --json=short shape, from mode ids: --demo's, and the selftest's fake mutter."""
    modes = [[i, int(i.split("x")[0]), int(i.split("x")[1].split("@")[0]), float(i.split("@")[1]), 1.0, [1.0],
              {"is-current": {"type": "b", "data": True}} if i == current else {}] for i in ids]
    return {"type": "ua((ssss)a(siiddada{sv})a{sv})a(iiduba(ssss)a{sv})a{sv}", "data": [
        serial, [[spec, modes, {}]] if spec else [], [[0, 0, 1.0, 0, True, [spec], {}]] if spec else [], {}]}


class Prefs:
    """The Settings page's backend (server.py routes it, agent.py's tools use it): the owner's overlay, live values,
    the display's Keep and guard, power. Thread-safe: _lock serialises changes and the display; _xlock the gamma ramp."""
    agent = None  # server.main: the Agent (voice previews, the remote microphone's status)

    def __init__(self, shell, path=STATE, monitors=MONITORS, keep_s=KEEP_S):
        self.shell, self.path, self.monitors, self.keep_s = shell, Path(path), Path(monitors), keep_s
        self.run = _run  # every subprocess goes through it (the selftest's fakes replace it)
        self.overlay, self.pending, self._output, self._dim, self._seen, self._guarded = {}, None, None, False, None, 0.0
        self._lock, self._xlock, self._warned = threading.Lock(), threading.Lock(), set()
        self._demo = {"mode": "1920x1080@60.000", "saved": "1920x1080@60.000", "serial": 1, "range": "full",
                      "sink": DEMO_SINK["name"]}

    def _warn(self, key, msg, *a):
        if key not in self._warned:
            self._warned.add(key)
            log.warning(msg, *a)

    # ---- the overlay: STATE over the config in use, in shell.settings

    def load(self):
        """STATE -> shell.settings, in place (before shell.start()). A corrupt file loads as {}; an unknown key or a bad
        value is dropped, logged; nothing is fatal. A voice is kept while Kokoro's voices file can't be read (a
        reinstall, a moved --voices): it can't be checked then, and the next save would lose it for good. The sleep
        defaults go in under the config's own (SLEEP). Nothing on the box changes here: start() applies the brightness
        and an owned color range once X answers. --demo reads no file: its changes live in memory, as its mode does."""
        try:
            raw = {} if self.shell.demo else json.loads(self.path.read_text())
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as e:
            log.warning("settings: %s unreadable (%s); starting from the config", self.path, e)
            raw = {}
        if not isinstance(raw, dict):
            log.warning("settings: %s isn't an object; starting from the config", self.path)
            raw = {}
        for k, v in raw.items():
            ok, err = self._valid(k, v) if k in OVERLAY else (None, "no such setting")
            if err and k == "tts.voice" and isinstance(v, str) and re.fullmatch(r"[ab][fm]_[a-z]+", v) and \
                    not self.voices():
                ok, err = v, None                # (the TTS server can't start without the file: Piper speaks meanwhile)
            if err:
                log.warning("settings: %s = %r dropped from %s: %s", k, v, self.path, err)
            else:
                self.overlay[k] = ok
        sl = self.shell.settings.setdefault("sleep", {})
        for k, v in SLEEP.items() if isinstance(sl, dict) else ():
            sl.setdefault(k, v)
        for k, v in self.overlay.items():
            self._merge(k, v)

    def _merge(self, key, v):
        """key's value into shell.settings in place ("tts.voice" sets settings["tts"]["voice"]); a parent that isn't
        a dict ("tts": false) is left alone, logged."""
        parent, leaf = key.split(".")
        d = self.shell.settings.setdefault(parent, {})
        if isinstance(d, dict):
            d[leaf] = v
        else:
            self._warn(f"parent:{parent}", "settings: %s left as the config has it (settings.%s = %r isn't an "
                       "object)", key, parent, d)

    def _save(self):
        """The overlay -> STATE: sorted, mode 600, atomic; its directory made 700 if need be (as tokens.json). Never in
        --demo: a demo beside the real daemon would hand it the demo's brightness and sleep timer at its next start."""
        if self.shell.demo:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write(self.path, json.dumps(self.overlay, sort_keys=True, indent=1) + "\n", 0o600)

    def _valid(self, key, v):
        """-> (the value as stored, None), else (None, why)."""
        if key in RANGES:
            lo, hi, step = RANGES[key]
            n = _int(v)
            return (n, None) if n is not None and lo <= n <= hi and not n % step else \
                (None, f"{key} must be a whole number from {lo} to {hi}" + (f" in steps of {step}" if step > 1 else ""))
        if key in ("sound.muted", "tts.enabled"):
            return (v, None) if isinstance(v, bool) else (None, f"{key} must be true or false")
        if key in CHOICES:
            ok = [c["value"] for c in CHOICES[key]]
            n = _int(v) if isinstance(ok[0], int) else v if isinstance(v, str) else None
            return (n, None) if n in ok else (None, f"{key} must be one of: {', '.join(map(str, ok))}")
        if key == "tts.voice":
            names = [c["value"] for c in self.voices()]
            return (v, None) if v in names else (None, "tts.voice must be an installed English voice: " + (
                ", ".join(names) if names else "none is installed (README, Speech)"))
        if key == "sound.output":
            return (v, None) if isinstance(v, str) and v else (None, "sound.output must be an output's name")
        return None, f"{key} can't be set here"

    # ---- values

    def _setting(self, key):
        parent, leaf = key.split(".")
        d = self.shell.settings.get(parent)
        return d.get(leaf) if isinstance(d, dict) else None

    def values(self, keys=None):
        """{key: its value now}: for the stored keys the overlay's, else the config's, else the default; live keys are
        read now (None when they can't be)."""
        out, vol = {}, None
        for k in KEYS if keys is None else keys:
            v = self._setting(k)
            if k == "display.mode":
                out[k] = (self._state() or {}).get("current")
            elif k in ("sound.volume", "sound.muted"):
                vol = vol or self._volume()
                out[k] = vol[k == "sound.muted"]
            elif k == "sound.output":
                out[k] = self._sinks()[1]
            elif k == "display.color_range":
                out[k] = v if v in ("full", "limited") else self._range()
            elif k in RANGES:
                out[k] = self._valid(k, v)[0] or DEFAULTS[k]
            elif k == "tts.enabled":
                out[k] = v is True
            elif k == "tts.voice":
                out[k] = v if isinstance(v, str) and v else _flag(self._setting("tts.server_cmd"), "--voice") or \
                    "af_heart"
            elif k == "sleep.signal":
                out[k] = v if v in ("off", "black") else "off"
            else:  # sleep minutes: what the Shell's sleep reads (any number >= 0, as inputs.number takes)
                ok = type(v) in (int, float) and 0 <= v < float("inf")
                out[k] = (int(v) if float(v).is_integer() else v) if ok else SLEEP[k.split(".")[1]]
        return out

    def _volume(self):
        """-> (volume 0-100, muted) of the default sink: wpctl, as Shell.volume reads it ("Volume: 0.45 [MUTED]")."""
        if self.shell.demo:
            return self.shell._demo["vol"], self.shell._demo.get("muted", False)
        code, out, err = self.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
        m = re.search(r"Volume: ([\d.]+)", out) if code == 0 else None
        if not m:
            self._warn("wpctl", "settings: wpctl get-volume failed: %s", _last(err, out))
        return (round(float(m[1]) * 100), "MUTED" in out) if m else (None, None)

    def _sinks(self):
        """-> (the outputs, the default's node.name): pw-dump (5 s)."""
        if self.shell.demo:
            return [DEMO_SINK], self._demo["sink"]
        code, out, err = self.run(["pw-dump"], 5)
        try:
            return parse_sinks(json.loads(out)) if code == 0 else ([], None)
        except ValueError:
            self._warn("pw-dump", "settings: pw-dump's output isn't JSON: %s", _last(err, out[:200]))
            return [], None

    def _out(self):
        """The connector brightness and the color range drive (HDMI-0): the last state read's, else read now."""
        return self._output or ((self._state() or {}).get("spec") or [None])[0]

    def _range(self):
        """The output's quantization range now: nvidia-settings' ColorRange, 0 full, 1 limited [M]; None unknown."""
        if self.shell.demo:
            return self._demo["range"]
        out = self._out()
        code, text, _ = self.run(["nvidia-settings", "-t", "-q", f"[dpy:{out}]/ColorRange"]) if out else (1, "", "")
        return {"0": "full", "1": "limited"}.get(text.strip()) if code == 0 else None

    def voices(self):
        """The Voice picker: the installed English Kokoro voices (the voices file tts.server_cmd's --voices names)."""
        names = DEMO_VOICES if self.shell.demo else voice_names(_flag(self._setting("tts.server_cmd"), "--voices") or VOICES)
        return [voice_choice(n) for n in names]

    # ---- one section of the page (GET /api/settings/<section>)

    def section(self, name, app=None, local=False):
        """{"ok", "section", "values" (its keys), "choices" (every picker of it, static ones too), "info" (what else it
        shows)}. app: server.py's (the phone's pairing and the ports); local: a loopback client, the only one the PIN
        goes to (as /api/tiles)."""
        keys = [k for k, s in KEYS.items() if s == name and k != "display.mode"]
        r = {"ok": True, "section": name, "values": self.values(keys), "choices": {k: CHOICES[k] for k in keys if k in
                                                                                   CHOICES}, "info": {}}
        if name == "display":
            m = self.modes()
            r["values"] = {"display.mode": m.get("current"), **r["values"]}
            r["choices"]["display.mode"] = [{"value": x["id"], "label": x["label"]} for x in m.get("modes", ())]
            r["info"] = {"output": m.get("output"), "monitor": m.get("monitor"), "saved": m.get("saved"),
                         "pending": m.get("pending"), **({} if m["ok"] else {"error": m["error"]})}
        elif name == "sound":
            r["choices"]["sound.output"] = [{"value": s["name"], "label": s["label"]} for s in self._sinks()[0]]
        elif name == "voice":
            sr = self.shell.settings.get("siri_remote", {})
            sr = sr if isinstance(sr, dict) else {"enabled": bool(sr)}  # (as siri.watch reads it)
            r["choices"]["tts.voice"] = self.voices()
            r["info"] = {"kokoro": bool(r["choices"]["tts.voice"]), "siri_remote": "off" if not sr.get("enabled", True)
                         else "connected" if self.shell.demo or siri.find() else "not connected",
                         **(self.agent.mic_status() if self.agent else {"remote_mic": "off", "asr": "not set up"})}
        elif name == "sleep":
            st = getattr(self.shell, "sleep_state", None)  # (shell.py's sleep)
            r["info"] = st(full=True) if st else {}
        elif name == "controllers":
            r["info"] = dict(zip(("bluetooth", "devices"), self._bluetooth()))
        elif name == "network":
            r["info"] = {"hostname": "spark" if self.shell.demo else socket.gethostname().split(".")[0],
                         "addresses": self._addresses(), "phone": app and {
                             "url": app.lan_url, "pin": app.pairing.pin if local else None,
                             "paired": len(app.pairing.hashes)},
                         "mcp": app and f"http://127.0.0.1:{app.port}/mcp"}
        elif name == "power":
            r["info"] = {"service": "INVOCATION_ID" in os.environ}  # (systemd sets it for its units' processes)
        elif name == "about":
            r["info"] = self._about()
        return r

    def _bluetooth(self):
        if self.shell.demo:
            return "on", [dict(d) for d in DEMO_DEVICES]
        code, out, err = self.run(["busctl", "--system", "--json=short", "call", "org.bluez", "/",
                                   "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"])
        try:
            return parse_bluez(json.loads(out), _battery) if code == 0 else ("unavailable", [])
        except (ValueError, LookupError, TypeError, AttributeError):
            self._warn("bluez", "settings: BlueZ's objects unreadable: %s", _last(err, out[:200]))
            return "unavailable", []

    def _addresses(self):
        if self.shell.demo:
            return [dict(a) for a in DEMO_ADDRESSES]
        code, out, _ = self.run(["ip", "-j", "-4", "addr", "show"])
        try:
            return parse_addresses(json.loads(out)) if code == 0 else []
        except ValueError:
            return []

    def _about(self):
        b, built = build()
        box = dict(DEMO_BOX) if self.shell.demo else {
            "device": _read("/sys/class/dmi/id/product_name").strip().replace("_", " ") or None,
            "os": (re.search(r'^PRETTY_NAME="?([^"\n]*)', _read("/etc/os-release"), re.M) or [None, None])[1],
            "kernel": platform.release(),
            "driver": (re.search(r"\b(\d+\.\d+(?:\.\d+)?)\b", _read("/proc/driver/nvidia/version")) or [None, None])[1]}
        up = _read("/proc/uptime").split()
        s = self.shell.stats()
        try:
            disk = shutil.disk_usage(Path.home())
            storage = {"used_gb": round(disk.used / 2 ** 30), "total_gb": round(disk.total / 2 ** 30)}
        except OSError:
            storage = None
        return {"build": b, "built": built, **box, "uptime_s": int(float(up[0])) if up else int(time.monotonic()),
                "sparkhome_uptime_s": int(time.monotonic() - STARTED), "gpu": s.get("gpu"), "mem": s.get("mem"),
                "storage": storage, "models": s.get("models")}

    # ---- changes (POST /api/settings/<key>; the agent's tools)

    def change(self, key, value, person=False, preview=False):
        """Set one setting -> {"ok": True, "key", "value"} | {"ok": False, "error"}. Checked in order: the key, who asks
        (an agent may set AGENTS only), the value. Then, under the lock: the effect on the box first, and only once that
        worked, the overlay (stored keys), shell.settings, and SSE settings (not for volume and mute: Shell.volume sends
        those). No change wakes the screen: an agent setting the sleep timer is no input, and the Shell reads sleep.*
        live. preview (a tts.* key): the agent speaks a sample in the voice and level now set."""
        if key not in KEYS:
            return {"ok": False, "error": f"no setting {key}"}
        if not person and key not in AGENTS:
            return {"ok": False, "error": PERSON.format(key=key, section=KEYS[key].title())}
        if key == "display.mode":
            return {"ok": False, "error": "display.mode changes with apply_mode (POST /api/display/apply), behind the "
                                          "15-second Keep"}
        v, err = self._valid(key, value)
        if err:
            return {"ok": False, "error": err}
        with self._lock:
            err = self._effect(key, v)
            if err:
                return {"ok": False, "error": err}
            if key in OVERLAY:
                self.overlay[key] = v
                self._merge(key, v)
                try:
                    self._save()
                except OSError as e:
                    log.error("settings: %s = %r applied but not saved: %s", key, v, e)
                    return {"ok": False, "error": f"{key} changed, but couldn't be saved: {e.strerror or e}"}
            if key not in ("sound.volume", "sound.muted"):
                self.shell.emit("settings", {"values": {key: v}})
        log.info("settings: %s = %r%s", key, v, "" if person else " (an agent)")
        if preview and key.startswith("tts.") and self.agent:
            self.agent.preview_voice(self.values(["tts.voice"])["tts.voice"])
        return {"ok": True, "key": key, "value": v}

    def _effect(self, key, v):
        """key's effect on the box -> None, else why it failed. Volume and mute go through Shell.volume (the remote's
        own path: it sends the page the new level)."""
        if key == "display.brightness":
            return None if self.brightness(pct=v) else "Couldn't set the brightness (xrandr failed; is the TV on?)"
        if key == "display.color_range":
            return self._color_range(v)
        if key in ("sound.volume", "sound.muted"):
            r = self.shell.volume(v if key == "sound.volume" else "mute" if v else "unmute")
            return None if r.get("ok") else r.get("error") or "Couldn't change the volume"
        if key == "sound.output":
            sinks, _ = self._sinks()
            s = next((s for s in sinks if s["name"] == v), None)
            if not s:
                return "sound.output must be one of: " + (", ".join(x["name"] for x in sinks) or "no output is listed")
            if self.shell.demo:
                self._demo["sink"] = v
                return None
            code, _, err = self.run(["wpctl", "set-default", str(s["id"])])
            return None if code == 0 else f"Couldn't switch the output: {_last(err, 'wpctl failed')}"
        return None  # tts.*, sleep.*: read live by the Agent and the Shell

    def _color_range(self, v):
        """HDMI's quantization range: nvidia-settings' ColorRange, 0 full (0-255), 1 limited (16-235) [M]. It must
        match the TV's black level, or black looks grey (washed out) or dark detail is crushed. -> None, else why not.
        Whether the TV blanks for a moment on a change is [?] (the live test)."""
        if self.shell.demo:
            self._demo["range"] = v
            return None
        out = self._out()
        if not out:
            return NO_TV
        code, text, err = self.run(["nvidia-settings", "-a", f"[dpy:{out}]/ColorRange={int(v == 'limited')}"])
        if code != 0 or "ERROR" in text + err:
            return f"Couldn't set the color range: {_last(err or text, 'nvidia-settings failed')}"
        return None

    def brightness(self, dim=None, pct=None):
        """Load the owner's brightness (or pct) into the output's gamma ramp, halved while dim (Shell.on_dim: the sleep
        warning; None keeps the last, so a change made then stays dimmed). -> True once applied, or with nothing to
        apply it to (no TV: the guard applies it when one comes). Its own lock: a dim and an undim never interleave, and
        the last one asked is the one applied. Called by on_dim, change, after every mode apply or revert, and at start.
        --demo: nothing."""
        if dim is not None:
            self._dim = dim  # (before the lock: of two calls racing, the later ask wins)
        with self._xlock:
            if self.shell.demo:
                return True
            out = self._out()
            if not out:
                return True
            pct = self.values(["display.brightness"])["display.brightness"] if pct is None else pct
            code, _, err = self.run(xrandr_argv(out, pct, self._dim))
            if code != 0:
                self._warn(f"xrandr:{err.strip()[:60]}", "settings: xrandr failed: %s", _last(err, "no reason given"))
            return code == 0

    # ---- the display mode: mutter's state, Keep, revert, the guard

    def _state(self):
        """mutter's display state now (parse_state), None with no monitor or no mutter. Each read also notes the
        connector brightness() drives."""
        if self.shell.demo:
            st = parse_state(_getstate(self._demo["serial"], self._demo["mode"], DEMO_MODES))
        else:
            code, out, err = self.run(["busctl", "--user", "--json=short", "call", *MUTTER, "GetCurrentState"])
            try:
                st = parse_state(json.loads(out)) if code == 0 else None
            except (ValueError, LookupError, TypeError, AttributeError) as e:
                st, err = None, f"unreadable: {e}"
            if code != 0 or err.startswith("unreadable"):
                self._warn("mutter", "settings: can't read mutter's display state: %s", _last(err, "no reason given"))
        self._output = st and st["spec"][0]
        return st

    def _saved(self, st):
        """monitors.xml's mode for this TV -> its mode id (one mutter lists, else as mutter would name it), else None."""
        if self.shell.demo:
            return self._demo["saved"]
        s = saved_mode(_read(self.monitors), st["spec"])
        if not s:
            return None
        i = next((i for i, (w, h, r) in st["all"].items() if (w, h) == s[:2] and abs(r - s[2]) < .002), None)
        return i or f"{s[0]}x{s[1]}@{s[2]:.3f}"

    def _game(self):
        """The running game's card (Steam games, Games-row apps, cloud-game tabs), else None: a mode change under one
        can crash it."""
        try:
            return next((c for c in self.shell.running_apps() if c.get("game")), None)
        except Exception:
            return None

    def _pending(self):
        p = self.pending
        return p and {"mode": p["mode"], "previous": p["previous"][0],
                      "seconds": max(0, round(p["until"] - time.monotonic()))}

    def modes(self):
        """GET /api/display/modes: {"ok", "output", "monitor", "current", "saved", "modes" (the picker's), "pending"}, or
        {"ok": False, "error"} with no TV."""
        st = self._state()
        if not st:
            return {"ok": False, "error": NO_TV}
        return {"ok": True, "output": st["spec"][0], "monitor": st["monitor"], "current": st["current"],
                "saved": self._saved(st), "modes": st["modes"], "pending": self._pending()}

    def _apply(self, st, mode, scale):
        """mutter's ApplyMonitorsConfig, method 1 -> None, else why not. A stale serial (the state moved under us: a
        hotplug) is read again and tried once more. busctl waits APPLY_S for the reply; a call that fails or runs out of
        time anyway counts as applied when mutter then shows the mode (killing busctl doesn't take the call back: a
        mode applied but answered as failed got no Keep sheet and no revert)."""
        if self.shell.demo:
            self._demo.update(mode=mode, serial=self._demo["serial"] + 1)
            return None
        for again in (False, True):
            code, _, err = self.run(apply_argv(st["serial"], st["spec"][0], mode, scale), APPLY_S)
            if code == 0:
                return None
            if again or "stale" not in err:
                break
            st = self._state()
            if not st or mode not in st["all"]:
                return "the display changed meanwhile; try again"
        now = self._state()
        return None if now and now["current"] == mode else _last(err, "mutter refused it")

    def apply_mode(self, mode, person=False):
        """The person's new mode: refused for agents, while a change waits for Keep, and under a running game (Steam
        games, Games-row apps, cloud-game tabs). Settings › Display comes up (Home first), mutter applies it (method 1,
        at the current scale: the launcher's fit() scales its stage to any window [I]), SSE display pending starts the
        page's Keep sheet, and KEEP_S later it reverts unless kept. -> {"ok": True, "state": "pending", "seconds"}, or
        {"ok": True, "state": "current"} for the mode already shown."""
        if not person:
            return {"ok": False, "error": PERSON_MODE}
        with self._lock:
            if self.pending:
                return {"ok": False, "error": "Keep or revert the last change first."}
            game = self._game()
            if game:
                return {"ok": False, "error": f"Quit {game['title']} first: changing the resolution under a running game "
                                              "can crash it."}
            st = self._state()
            if not st:
                return {"ok": False, "error": NO_TV}
            if st["monitors"] > 1:  # (one logical monitor is applied: the others would go dark)
                return {"ok": False, "error": "More than one display is connected: use GNOME's Settings for that."}
            if mode not in st["all"]:
                return {"ok": False, "error": f"The TV doesn't offer {mode}."}
            if mode == st["current"]:
                return {"ok": True, "state": "current", "mode": mode}
            self.open("display")
            err = self._apply(st, mode, st["scale"])
            if err:
                return {"ok": False, "error": f"Couldn't switch to {mode_label(*st['all'][mode])}: {err}"}
            self.brightness()  # (a modeset may reset the ramp [I])
            p = self.pending = {"mode": mode, "previous": (st["current"], st["scale"]), "size": st["all"][mode],
                                "until": time.monotonic() + self.keep_s}
            p["timer"] = threading.Timer(self.keep_s, self.revert_mode, ("timeout", p))
            p["timer"].daemon = True
            p["timer"].start()
        self.shell.emit("display", {"state": "pending", "mode": mode, "previous": p["previous"][0],
                                    "seconds": self.keep_s})
        log.info("display: %s was %s, now %s: %d s to keep it", st["monitor"], st["current"], mode, self.keep_s)
        return {"ok": True, "state": "pending", "seconds": self.keep_s}

    def keep_mode(self):
        """Keep: monitors.xml gets the new mode for this TV (mutter reads it at login; the guard enforces it until
        then), only while mutter still shows it. A TV power cycle, an input switch or a hotplug in the countdown puts
        mutter back on its login config: Keep then would save a mode nobody has seen work (4K on this cable: no picture
        at every login, and the guard putting it back after every power cycle), so it switches back instead. --demo
        writes nothing."""
        with self._lock:
            p = self.pending
            if not p:
                return {"ok": False, "error": "nothing to keep"}
            st = self._state()
            shown = bool(st) and st["current"] == p["mode"]
            if shown:
                if self.shell.demo:
                    self._demo["saved"] = p["mode"]
                else:
                    try:
                        old = self.monitors.stat().st_mode & 0o777 if self.monitors.exists() else 0o664
                        _write(self.monitors, save_mode(_read(self.monitors), st["spec"], p["size"], p["previous"][1]),
                               old)
                    except OSError as e:  # (pending stays: the timer still reverts it)
                        return {"ok": False, "error": f"Couldn't save {self.monitors}: {e.strerror or e}"}
                p["timer"].cancel()
                self.pending = None
        if not shown:
            log.warning("display: %s not kept: mutter shows %s now", p["mode"], st and st["current"])
            r, was = self.revert_mode("changed", only=p), (st or {}).get("all", {}).get(p["previous"][0])
            return {"ok": False, "error": f"The TV isn't showing {mode_label(*p['size'])} any more, so it wasn't "
                                          f"kept." + (f" Back to {mode_label(*was)}." if r["ok"] and was else "")}
        self.shell.emit("display", {"state": "kept", "mode": p["mode"]})
        log.info("display: %s kept (monitors.xml)", p["mode"])
        return {"ok": True, "state": "kept", "mode": p["mode"]}

    def revert_mode(self, why="asked", only=None):
        """Back to the mode before (Revert, Back, Home, the timeout, a Keep that found it gone: why), then the
        brightness; nothing to apply when mutter shows it already. A failure is SSE display failed and a toast. only:
        the change the timer was set for (never a later one)."""
        with self._lock:
            p = self.pending
            if not p or only is not None and p is not only:
                return {"ok": False, "error": "nothing to revert"}
            p["timer"].cancel()
            self.pending = None
            st, (mode, scale) = self._state(), p["previous"]
            err = NO_TV if not st else "the mode before is unknown" if not mode else None if st["current"] == mode \
                else self._apply(st, mode, scale)
            self.brightness()
        if err:
            log.warning("display: revert to %s failed: %s", mode, err)
            self.shell.emit("display", {"state": "failed", "error": err})
            self.shell.emit("toast", {"text": f"Couldn't switch back: {err}", "kind": "error"})
            return {"ok": False, "error": err}
        self.shell.emit("display", {"state": "reverted", "mode": mode, "why": why})
        log.info("display: back to %s (%s)", mode, why)
        return {"ok": True, "state": "reverted", "mode": mode}

    def _guard_tick(self):
        """One look by the display guard: with nothing pending, the saved mode back on the TV when mutter shows another
        (mutter reads monitors.xml only at login, so a TV power cycle re-applies what it loaded then [M]; a killed
        daemon's pending change; a manual xrandr --mode), at most every GUARD_EVERY_S, and never while a game runs (as
        apply_mode: a game may set its own mode, and changing it under the game can crash it; the guard acts once it
        has quit). A TV that comes back gets the brightness again (a hotplug may reset the ramp [?])."""
        with self._lock:
            st = None if self.pending else self._state()
            if self.pending:
                return
            seen, self._seen = self._seen, bool(st)
            if st and seen is False:
                self.brightness()
            saved = st and st["monitors"] == 1 and saved_mode(_read(self.monitors), st["spec"])
            want = guard_mode(st, saved, self.pending)
            if not want or time.monotonic() - self._guarded < GUARD_EVERY_S or self._game():
                return
            self._guarded = time.monotonic()
            err = self._apply(st, want, saved[3])
            self.brightness()
        if err:
            log.warning("display guard: %s was %s, saved %s: couldn't re-apply it: %s", st["monitor"], st["current"],
                        want, err)
        else:
            log.info("display guard: %s was %s, saved %s: re-applied", st["monitor"], st["current"], want)

    def start(self):
        """The background part (real mode): once X answers, the brightness (always, so a crashed daemon's dim can't
        stick) and an owned color range; then the display guard every GUARD_S."""
        if not self.shell.demo:
            threading.Thread(target=self._start, name="settings", daemon=True).start()

    def _start(self):
        time.sleep(2)
        for _ in range(30):  # (the daemon starts with the session: mutter and X are normally up already)
            if self.brightness():
                break
            time.sleep(2)
        self._seen = bool(self._output)
        if "display.color_range" in self.overlay:
            err = self._color_range(self.overlay["display.color_range"])
            if err:
                log.warning("settings: %s", err)
        while True:
            time.sleep(GUARD_S)
            try:
                self._guard_tick()
            except Exception:
                log.exception("display guard failed")

    # ---- power and opening the page

    def power(self, action, person=False):
        """Restart SparkHome, restart the Spark or shut it down (the person's, from the page's confirm sheet). The
        answer and a toast first; POWER_DELAY_S later the act; a failure is an error toast with its last line."""
        if action not in POWER:
            return {"ok": False, "error": f"no power action {action}"}
        if not person:
            return {"ok": False, "error": PERSON_POWER}
        argv, text, demo = POWER[action]
        if self.shell.demo:
            self.shell.emit("toast", {"text": demo, "kind": "info"})
            return {"ok": True}
        if action == "restart_sparkhome" and "INVOCATION_ID" not in os.environ:
            return {"ok": False, "error": "SparkHome isn't running as its service here (deploy/install.sh enable)"}
        self.shell.emit("toast", {"text": text, "kind": "info"})
        log.warning("power: %s (the person at the TV)", action)

        def act():
            time.sleep(POWER_DELAY_S)
            code, _, err = self.run(argv)
            if code != 0:
                log.warning("power: %s failed: %s", action, err.strip())
                self.shell.emit("toast", {"text": f"Couldn't {'shut down' if action == 'shutdown' else 'restart'}: "
                                                  f"{_last(err, 'systemctl failed')}", "kind": "error"})
        _spawn(act)
        return {"ok": True}

    def ask_power(self, action):
        """The voice's power commands only ask: the confirm sheet on the TV, Cancel focused (a misheard "shut down"
        never powers off the box)."""
        if action not in POWER:
            return {"ok": False, "error": f"no power action {action}"}
        return {**self.open("power", confirm=action), "confirm": action}

    def open(self, section=None, confirm=None):
        """Settings on the TV, at section (SSE open_settings), with the confirm sheet for a power action. The launcher
        comes first (Shell.open of the Settings tile goes Home), unless Settings is open there already. ui.screen is
        Settings from here, as Shell.open sets it for the tile, not from the page's report 100 ms later: agents'
        navigate and select in it are refused at once (an MCP open_settings, select, navigate right, select restarted
        the box)."""
        sh = self.shell
        if not (sh.ui.get("screen") == "settings" and sh.state().get("surface") == "launcher"):
            r = sh.open("settings")
            if not r.get("ok") and sh.state().get("surface") != "launcher":  # (a config without the Settings tile)
                sh.home()
        if section or confirm:
            with sh.lock:
                sh.ui = {**sh.ui, "screen": "settings"}
            sh.emit("open_settings", {"section": section or "power", **({"confirm": confirm} if confirm else {})})
        return {"ok": True, "section": section}


# ---- self-check ------------------------------------------------------------------------------------------------------

def _selftest():
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="sparkhome-test-"))
    try:
        _selftest_in(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _FakeShell:
    """What Prefs uses of a Shell, recorded: settings, emit, volume, open, home, running_apps, state, ui, stats."""

    def __init__(self, demo, settings=None):
        self.demo, self.settings, self.events, self.calls = demo, settings if settings is not None else {}, [], []
        self._demo, self.ui, self.games, self.surface = {"vol": 50}, {"screen": "home"}, [], "web"
        self.lock = threading.RLock()

    def emit(self, typ, data):
        self.events.append((typ, data))

    def volume(self, change):
        self.calls.append(("volume", change))
        if isinstance(change, int):
            self._demo["vol"] = change
        else:
            self._demo["muted"] = change == "mute"
        return {"ok": True, "volume": self._demo["vol"], "muted": self._demo.get("muted", False)}

    def open(self, tid):
        self.calls.append(("open", tid))
        self.surface = "launcher"
        return {"ok": True}

    def home(self):
        self.calls.append(("home",))
        return {"ok": True}

    def running_apps(self):
        return [{"key": g, "title": "Red Dead Redemption 2", "game": True} for g in self.games]

    def state(self):
        return {"surface": self.surface}

    def stats(self):
        return {"gpu": {"util": 2, "temp": 41, "power": 11.0}, "mem": {"used_gb": 18.2, "total_gb": 121.6},
                "models": []}


class _Box:
    """The Spark's tools for a real-mode Prefs, faked: mutter over busctl (its state, ApplyMonitorsConfig: a stale
    serial once on request; slow: busctl runs out of time, after mutter applied it, or "lost" before), BlueZ, xrandr,
    nvidia-settings, wpctl, pw-dump, ip and systemctl, answering from the 2026-10-11 captures (trimmed), every argv (and
    each apply's timeout) recorded."""
    BLUEZ = {"type": "a{oa{sa{sv}}}", "data": [{
        "/org/bluez/hci0": {"org.bluez.Adapter1": {"Powered": {"type": "b", "data": True},
                                                   "Pairable": {"type": "b", "data": False}}},
        "/org/bluez/hci0/dev_02_00_00_00_00_83": {"org.bluez.Device1": {
            "Alias": {"type": "s", "data": "02-00-00-00-00-83"}, "Paired": {"type": "b", "data": False}}},
        "/org/bluez/hci0/dev_02_00_00_00_00_B2": {"org.bluez.Device1": {
            "Address": {"type": "s", "data": "02:00:00:00:00:B2"}, "Alias": {"type": "s", "data": "Xbox Wireless "
                                                                                                  "Controller"},
            "Icon": {"type": "s", "data": "input-gaming"}, "Paired": {"type": "b", "data": True},
            "Connected": {"type": "b", "data": True}, "Modalias": {"type": "s", "data": "usb:v045Ep0B13d0509"}},
            "org.bluez.Battery1": {"Percentage": {"type": "y", "data": 80}}},
        "/org/bluez/hci0/dev_02_00_00_00_00_21": {"org.bluez.Device1": {
            "Address": {"type": "s", "data": "02:00:00:00:00:21"}, "Alias": {"type": "s", "data": "C00000000000"},
            "Paired": {"type": "b", "data": True}, "Trusted": {"type": "b", "data": False},
            "Connected": {"type": "b", "data": False}, "Modalias": {"type": "s", "data": "bluetooth:v004Cp026Dd0000"}}},
        "/org/bluez/hci0/dev_02_00_00_00_00_5F": {"org.bluez.Device1": {
            "Address": {"type": "s", "data": "02:00:00:00:00:5F"}, "Alias": {"type": "s", "data": "DualSense Wireless "
                                                                                                  "Controller"},
            "Icon": {"type": "s", "data": "input-gaming"}, "Paired": {"type": "b", "data": True},
            "Connected": {"type": "b", "data": True}}, "org.bluez.Input1": {}}}]}
    PW = [{"id": 36, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"}, "metadata": [
              {"subject": 0, "key": "default.configured.audio.sink", "type": "Spa:String:JSON",
               "value": {"name": "alsa_output.platform-NVDA2014_00.hdmi-stereo"}},
              {"subject": 0, "key": "default.audio.sink", "type": "Spa:String:JSON",
               "value": {"name": "alsa_output.platform-NVDA2014_00.hdmi-stereo"}}]},
          {"id": 44, "type": "PipeWire:Interface:Node", "info": {"props": {
              "api.alsa.path": "hdmi:0", "media.class": "Audio/Sink", "node.description": "Built-in Audio Digital "
                                                                                        "Stereo (HDMI)",
              "node.name": "alsa_output.platform-NVDA2014_00.hdmi-stereo"}, "state": "suspended"}},
          {"id": 71, "type": "PipeWire:Interface:Node", "info": {"props": {
              "media.class": "Audio/Sink", "node.description": "AirPods Pro", "node.name": "bluez_output.AA_BB.1"}}},
          {"id": 72, "type": "PipeWire:Interface:Node", "info": {"props": {
              "media.class": "Audio/Source", "node.name": "alsa_input.mic"}}}]
    IP = [{"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1"}]},
          {"ifname": "veth0", "addr_info": [{"family": "inet", "local": "10.99.0.1"}]},
          {"ifname": "wlP9s9", "addr_info": [{"family": "inet", "local": "192.168.1.20"}]},
          {"ifname": "tailscale0", "addr_info": [{"family": "inet", "local": "100.64.0.2"}]},
          {"ifname": "enP7s7", "addr_info": []},
          {"ifname": "docker0", "addr_info": [{"family": "inet", "local": "172.17.0.1"}]}]

    def __init__(self):
        self.calls, self.mode, self.serial, self.range, self.vol, self.default = [], "1920x1080@60.000", 7, "0", "0.45", 44
        self.tv, self.stale, self.fail, self.slow, self.timeouts = True, 0, set(), False, []

    def __call__(self, argv, timeout=3):
        self.calls.append(argv)
        name, cmd = argv[0], " ".join(argv)
        if name in self.fail:
            return 1, "", f"{name}: something went wrong\nERROR: it failed"
        if "GetCurrentState" in argv:
            return 0, json.dumps(_getstate(self.serial, self.mode, DEMO_MODES, TV if self.tv else None)), ""
        if "ApplyMonitorsConfig" in argv:
            self.timeouts.append(timeout)
            if self.slow:                        # (subprocess.run's own words, through _run)
                if self.slow != "lost":
                    self.mode, self.serial = argv[-3], self.serial + 1
                return None, "", f"Command {argv!r} timed out after {timeout} seconds"
            if self.stale:
                self.stale -= 1
                self.serial += 1
                return 1, "", "Call failed: The requested configuration is based on stale information"
            if argv[8] != str(self.serial):
                return 1, "", "Call failed: The requested configuration is based on stale information"
            self.mode, self.serial = argv[-3], self.serial + 1
            return 0, "", ""
        if "GetManagedObjects" in argv:
            return 0, json.dumps(self.BLUEZ), ""
        if name == "nvidia-settings":
            if argv[1] == "-a":
                self.range = argv[2][-1]
                return 0, "", ""
            return 0, self.range + "\n", ""
        if cmd == "wpctl get-volume @DEFAULT_AUDIO_SINK@":
            return 0, f"Volume: {self.vol}\n", ""
        if argv[:2] == ["wpctl", "set-default"]:
            self.default = int(argv[2])
            return 0, "", ""
        if name == "pw-dump":
            return 0, json.dumps(self.PW), ""
        if name == "ip":
            return 0, json.dumps(self.IP), ""
        if name in ("xrandr", "systemctl"):
            return 0, "", ""
        return None, "", f"[Errno 2] No such file or directory: '{name}'"


def _selftest_in(tmp):
    # -- the overlay: a corrupt file is {}; unknown keys and bad values dropped; merged in place into nested dicts; a
    # parent that isn't a dict left alone; the sleep defaults under the config's own; saved 600, atomically
    tts = {"enabled": False, "server_cmd": ["py", "tools/tts_server.py", "--voices", str(tmp / "voices.bin"),
                                           "--voice", "bf_emma"]}
    cfg = {"tts": tts, "display": False, "sleep": {"minutes": 30}}
    sh = _FakeShell(False, cfg)
    p = Prefs(sh, tmp / "state" / "settings.json", tmp / "monitors.xml", keep_s=.2)
    p.run = box = _Box()
    p.path.parent.mkdir()
    p.path.write_text("{nope")
    p.load()
    assert p.overlay == {} and cfg["sleep"] == {"minutes": 30, "game_minutes": 0, "signal": "off"}
    import zipfile as zf
    with zf.ZipFile(tmp / "voices.bin", "w") as z:  # (a zip of .npy files, as Kokoro's voices-v1.0.bin [M])
        for n in ("af_heart", "bf_emma", "am_adam", "bm_george", "ef_dora", "jf_alpha", "zf_xiaobei", "hf_alpha"):
            z.writestr(f"{n}.npy", b"\x93NUMPY")
    p.path.write_text(json.dumps({"tts.enabled": True, "tts.voice": "af_heart", "tts.volume": 55, "sleep.minutes": 45,
                                  "sleep.signal": "black", "display.brightness": 120, "display.mode": "1280x720@60.000",
                                  "sound.volume": 30, "nope": 1, "sleep.game_minutes": True}))
    p.load()
    assert p.overlay == {"tts.enabled": True, "tts.voice": "af_heart", "sleep.signal": "black",
                         "display.brightness": 120}, p.overlay
    assert tts == {**tts, "enabled": True, "voice": "af_heart"} and "volume" not in tts and cfg["display"] is False
    assert cfg["sleep"] == {"minutes": 30, "game_minutes": 0, "signal": "black"}  # (the config's 30 under no overlay)
    v = p.values(["display.brightness", "tts.volume", "tts.voice", "sleep.minutes", "sleep.signal", "tts.enabled"])
    assert v == {"display.brightness": 100, "tts.volume": 100, "tts.voice": "af_heart", "sleep.minutes": 30,
                 "sleep.signal": "black", "tts.enabled": True}, v  # (display isn't a dict: the default)
    cfg["display"] = {}
    assert p.change("display.brightness", 120.0, person=True) == {"ok": True, "key": "display.brightness", "value": 120}
    assert cfg["display"] == {"brightness": 120} and json.loads(p.path.read_text())["display.brightness"] == 120
    assert oct(p.path.stat().st_mode & 0o777) == "0o600" and os.listdir(p.path.parent) == ["settings.json"]
    assert ("settings", {"values": {"display.brightness": 120}}) in sh.events
    assert p.change("sleep.minutes", 5) == {"ok": True, "key": "sleep.minutes", "value": 5} and cfg["sleep"]["minutes"] == 5
    for bad in ("30", None, -1, 7, True, 4.5):
        assert not p.change("sleep.minutes", bad)["ok"] and cfg["sleep"]["minutes"] == 5, bad
    assert p.values(["tts.voice"]) == {"tts.voice": "af_heart"}
    del tts["voice"]
    assert p.values(["tts.voice"]) == {"tts.voice": "bf_emma"}  # (unset: server_cmd's --voice)
    # -- who may change what: an agent only AGENTS (403 at the route); a person all but the mode (apply_mode's)
    assert p.change("tts.voice", "bf_emma")["error"].startswith("Only the person at the TV changes tts.voice")
    assert p.change("tts.voice", "ef_dora", person=True)["error"].startswith("tts.voice must be an installed English")
    assert p.change("tts.voice", "bm_george", person=True)["ok"] and tts["voice"] == "bm_george"
    assert p.change("sound.volume", 35) == {"ok": True, "key": "sound.volume", "value": 35} and \
        sh.calls[-1] == ("volume", 35) and not any(e == "settings" and "sound.volume" in d["values"] for e, d in sh.events)
    assert p.change("sound.muted", True)["ok"] and sh.calls[-1] == ("volume", "mute")
    assert "apply_mode" in p.change("display.mode", "1280x720@60.000", person=True)["error"]
    assert p.change("display.mode", "1280x720@60.000")["error"] == PERSON.format(key="display.mode", section="Display")
    assert p.change("nope", 1, person=True) == {"ok": False, "error": "no setting nope"}
    assert p.change("tts.volume", 55, person=True)["error"].endswith("in steps of 10")
    # -- brightness: a gamma curve above 100 %, a plain scale at or below; dim halves it; both flags always
    a = lambda pct, dim=False: " ".join(xrandr_argv("HDMI-0", pct, dim)[3:])  # noqa: E731
    assert [a(50), a(100), a(120), a(150)] == ["--gamma 1:1:1 --brightness 0.5", "--gamma 1:1:1 --brightness 1",
                                               "--gamma 1.2:1.2:1.2 --brightness 1", "--gamma 1.5:1.5:1.5 --brightness 1"]
    assert [a(50, True), a(100, True), a(120, True), a(150, True)] == [
        "--gamma 1:1:1 --brightness 0.25", "--gamma 1:1:1 --brightness 0.5", "--gamma 1.2:1.2:1.2 --brightness 0.5",
        "--gamma 1.5:1.5:1.5 --brightness 0.5"] and xrandr_argv("HDMI-0", 80)[:3] == ["xrandr", "--output", "HDMI-0"]
    box.calls.clear()
    assert p.brightness(dim=True) and box.calls[-1][3:] == ["--gamma", "1.2:1.2:1.2", "--brightness", "0.5"]
    assert p.change("display.brightness", 90)["ok"] and box.calls[-1][-1] == "0.45"  # (a change while dim stays dim)
    assert p.brightness(dim=False) and box.calls[-1][-1] == "0.9" and box.calls[-1][2] == "HDMI-0"
    box.fail = {"xrandr"}
    assert p.change("display.brightness", 110)["error"].startswith("Couldn't set the brightness")
    assert cfg["display"]["brightness"] == 90 and json.loads(p.path.read_text())["display.brightness"] == 90
    box.fail, box.tv, p._output = set(), False, None  # no TV: nothing to apply to, which is no failure
    n = len(box.calls)
    assert p.brightness() and all(c[0] != "xrandr" for c in box.calls[n:])
    box.tv = True
    # -- color range: nvidia-settings on the connector; ERROR in its output fails
    assert p.values(["display.color_range"]) == {"display.color_range": "full"}
    assert p.change("display.color_range", "limited", person=True)["ok"] and box.range == "1"
    assert box.calls[-1] == ["nvidia-settings", "-a", "[dpy:HDMI-0]/ColorRange=1"] and \
        p.values(["display.color_range"]) == {"display.color_range": "limited"}
    box.fail = {"nvidia-settings"}
    assert p.change("display.color_range", "full", person=True)["error"] == "Couldn't set the color range: ERROR: it failed"
    box.fail = set()
    # -- sound: wpctl's level; the outputs from pw-dump (HDMI is the TV); set-default by the node's id
    sh.demo = False
    assert p.values(["sound.volume", "sound.muted", "sound.output"]) == {
        "sound.volume": 45, "sound.muted": False, "sound.output": "alsa_output.platform-NVDA2014_00.hdmi-stereo"}
    box.vol = "0.30 [MUTED]"
    assert p.values(["sound.volume", "sound.muted"]) == {"sound.volume": 30, "sound.muted": True}
    sinks = p.section("sound")["choices"]["sound.output"]
    assert sinks == [{"value": "alsa_output.platform-NVDA2014_00.hdmi-stereo", "label": "TV (HDMI)"},
                     {"value": "bluez_output.AA_BB.1", "label": "AirPods Pro"}], sinks
    assert p.change("sound.output", "bluez_output.AA_BB.1", person=True)["ok"] and box.default == 71
    assert p.change("sound.output", "nope", person=True)["error"].startswith("sound.output must be one of")
    assert parse_sinks([{"type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"}, "metadata": [
        {"key": "default.audio.sink", "value": '{"name": "x"}'}]}]) == ([], "x")  # (a JSON string, as older ones print)
    # -- mutter's state: the TV sizes and the current mode, 4096 wide dropped, labels as the picker shows them; read
    # verbatim from the capture (busctl --json=short) too
    raw = ('{"type":"ua((ssss)a(siiddada{sv})a{sv})a(iiduba(ssss)a{sv})a{sv}","data":[7,[[["HDMI-0","TCL","50S425",'
           '"0x00000000"],[["3840x2160@30.000",3840,2160,3.000000000000000000000000000000000000e+01,1.000000000000000000'
           '000000000000000000e+00,[1.000000000000000000000000000000000000e+00,2.000000000000000000000000000000000000e+0'
           '0],{"is-preferred":{"type":"b","data":true}}],["4096x2160@59.940",4096,2160,5.99404029846191406250000000000'
           '0000000000e+01,1.0,[1.0],{}],["3840x2160@59.940",3840,2160,5.994040298461914062500000000000000000e+01,1.0,'
           '[1.0],{}],["1920x1080@60.000",1920,1080,6.000000000000000000000000000000000000e+01,1.0,[1.0,2.0],{"is-curre'
           'nt":{"type":"b","data":true}}],["1920x1080@59.939",1920,1080,5.993939208984375000000000000000000000e+01,1.0'
           ',[1.0,2.0],{}],["1680x1050@59.954",1680,1050,5.995425033569335937500000000000000000e+01,1.0,[1.0],{}],["128'
           '0x720@60.000",1280,720,6.000000000000000000000000000000000000e+01,1.0,[1.0],{}]],{"is-builtin":{"type":"b",'
           '"data":false},"display-name":{"type":"s","data":"Technical Concepts Ltd 36\\""}}]],[[0,0,1.0000000000000000'
           '00000000000000000000e+00,0,true,[["HDMI-0","TCL","50S425","0x00000000"]],{}]],{"renderer":{"type":"s","data'
           '":"xrandr"},"layout-mode":{"type":"u","data":2},"global-scale-required":{"type":"b","data":true}}]}')
    st = parse_state(json.loads(raw))
    assert (st["serial"], st["spec"], st["monitor"], st["scale"], st["current"], st["monitors"]) == (
        7, TV, "TCL 50S425", 1.0, "1920x1080@60.000", 1)
    assert [(m["id"], m["label"], m["current"]) for m in st["modes"]] == [
        ("3840x2160@59.940", "4K · 59.94 Hz", False), ("3840x2160@30.000", "4K · 30 Hz", False),
        ("1920x1080@60.000", "1080p · 60 Hz", True), ("1920x1080@59.939", "1080p · 59.94 Hz", False),
        ("1280x720@60.000", "720p · 60 Hz", False)] and "4096x2160@59.940" in st["all"]
    st2 = parse_state(_getstate(9, "1680x1050@59.954", DEMO_MODES))
    assert [m["id"] for m in st2["modes"] if m["current"]] == ["1680x1050@59.954"] and len(st2["modes"]) == 13
    assert parse_state(_getstate(9, None, DEMO_MODES, None)) is None
    assert [mode_label(*st2["all"][i]) for i in ("3840x2160@23.976", "1920x1080@29.972", "1680x1050@59.954")] == [
        "4K · 23.98 Hz", "1080p · 29.97 Hz", "1680×1050 · 59.95 Hz"]
    assert apply_argv(7, "HDMI-0", "3840x2160@59.940", 1.0) == [
        "busctl", "--user", "call", "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
        "org.gnome.Mutter.DisplayConfig", "ApplyMonitorsConfig", "uua(iiduba(ssa{sv}))a{sv}", "7", "1", "1", "0", "0",
        "1.0", "0", "true", "1", "HDMI-0", "3840x2160@59.940", "0", "0"]
    # -- what words name a mode: a size (4K, 1080p, 720p, WxH), a rate said or the best up to 60 Hz; a mutter id as is
    e = st2["all"]
    for said, want in (("4k", "3840x2160@59.940"), ("4K@30", "3840x2160@30.000"), ("4k at 24 hz", "3840x2160@23.976"),
                       ("1080p", "1920x1080@60.000"), ("1920x1080@59.94", "1920x1080@59.939"), ("720p", "1280x720@60.000"),
                       ("1080p 30 hz", "1920x1080@29.972"), ("1280 x 720 at 59 94", "1280x720@59.943"),
                       ("1680x1050@59.954", "1680x1050@59.954"), ("full hd", "1920x1080@60.000"), ("8k", None),
                       ("2560x1440", None), ("max", None)):
        assert pick_mode(said, e) == want, (said, pick_mode(said, e))
    # -- monitors.xml: Keep sets this TV's mode and nothing else; another configuration stays; none for it: appended
    xml = ("<monitors version=\"2\">\n  <configuration>\n    <logicalmonitor>\n      <x>0</x>\n      <y>0</y>\n      "
           "<scale>1</scale>\n      <primary>yes</primary>\n      <monitor>\n        <monitorspec>\n          "
           "<connector>HDMI-0</connector>\n          <vendor>TCL</vendor>\n          <product>50S425</product>\n"
           "          <serial>0x00000000</serial>\n        </monitorspec>\n        <mode>\n          <width>1920</width>\n"
           "          <height>1080</height>\n          <rate>60.000</rate>\n        </mode>\n      </monitor>\n    "
           "</logicalmonitor>\n  </configuration>\n</monitors>\n")  # (2026-10-11, hand-written to mutter's format [M])
    assert saved_mode(xml, TV) == (1920, 1080, 60.0, 1.0) and saved_mode(xml, ["DP-1", "TCL", "50S425", "x"]) is None
    k = save_mode(xml, TV, e["3840x2160@59.940"], 1.0)
    assert k == xml.replace("1920", "3840").replace("1080", "2160").replace("60.000", "59.940"), k
    assert save_mode(xml, TV, e["1920x1080@59.939"], 1.0) == xml.replace("60.000", "59.939")  # (mutter's 0.001 match)
    sony = xml.replace("HDMI-0", "HDMI-1").replace("TCL", "SNY").replace("50S425", "XBR").replace("1080<", "2160<")
    two = sony.replace("</monitors>\n", xml[xml.index("  <configuration>"):])  # (the Sony's, then the TCL's)
    assert saved_mode(two, TV) == (1920, 1080, 60.0, 1.0) and saved_mode(two, ["HDMI-1", "SNY", "XBR", "0x00000000"]) \
        == (1920, 2160, 60.0, 1.0)
    k2 = save_mode(two, TV, e["1280x720@60.000"], 1.0)
    assert saved_mode(k2, TV) == (1280, 720, 60.0, 1.0) and saved_mode(k2, ["HDMI-1", "SNY", "XBR", "0x00000000"]) == (
        1920, 2160, 60.0, 1.0) and k2.count("<configuration>") == 2
    k3 = save_mode(sony, TV, e["1920x1080@60.000"], 1.0)  # (no configuration for the TCL: appended)
    assert k3.count("<configuration>") == 2 and saved_mode(k3, TV) == (1920, 1080, 60.0, 1.0) and \
        saved_mode(k3, ["HDMI-1", "SNY", "XBR", "0x00000000"]) == (1920, 2160, 60.0, 1.0) and k3.startswith(sony[:200])
    assert "\n  <configuration>\n    <logicalmonitor>\n      <x>0</x>" in k3[200:] and k3.endswith("</monitors>\n")
    k4 = save_mode("", TV, e["1920x1080@60.000"], 1.0)  # (no file at all)
    assert saved_mode(k4, TV) == (1920, 1080, 60.0, 1.0) and k4.startswith('<monitors version="2">\n  <configuration>')
    assert saved_mode("<monitors", TV) is None and saved_mode(xml.replace("60.000", "fast"), TV) is None
    # -- the guard: re-apply only the saved mode mutter lists, when it shows another; never while a change waits
    cur = lambda m: parse_state(_getstate(1, m, DEMO_MODES))  # noqa: E731
    assert guard_mode(cur("1920x1080@60.000"), (1920, 1080, 60.0, 1.0), None) is None  # equal
    assert guard_mode(cur("3840x2160@30.000"), (1920, 1080, 60.0, 1.0), None) == "1920x1080@60.000"  # a hotplug's
    assert guard_mode(cur("1920x1080@59.939"), (1920, 1080, 59.939, 1.0), None) is None  # (0.002: the same)
    assert guard_mode(cur("3840x2160@30.000"), (1920, 1080, 60.0, 1.0), {"mode": "x"}) is None  # pending
    assert guard_mode(None, (1920, 1080, 60.0, 1.0), None) is None  # no monitor
    assert guard_mode(cur("3840x2160@30.000"), (2560, 1440, 60.0, 1.0), None) is None  # not listed: no fight
    assert guard_mode(cur("3840x2160@30.000"), None, None) is None  # nothing saved
    assert guard_mode(cur(None), (1920, 1080, 60.0, 1.0), None) == "1920x1080@60.000"  # (not driven: the saved one)
    # -- apply, Keep and revert on the fake mutter: Settings › Display first, method 1 at the current scale, the
    # brightness again, SSE pending; Keep writes monitors.xml (its mode bits kept); a stale serial is tried once more
    (tmp / "monitors.xml").write_text(xml)
    os.chmod(tmp / "monitors.xml", 0o664)
    sh.events.clear(), sh.calls.clear()
    box.calls.clear()
    assert p.apply_mode("3840x2160@59.940") == {"ok": False, "error": PERSON_MODE} and not box.calls
    assert p.apply_mode("8192x4320@60.000", True)["error"] == "The TV doesn't offer 8192x4320@60.000."
    assert p.apply_mode("1920x1080@60.000", True) == {"ok": True, "state": "current", "mode": "1920x1080@60.000"}
    p.keep_s = 30
    assert p.apply_mode("3840x2160@59.940", True) == {"ok": True, "state": "pending", "seconds": 30}
    ap = [c for c in box.calls if "ApplyMonitorsConfig" in c]
    assert ap == [apply_argv(7, "HDMI-0", "3840x2160@59.940", 1.0)] and box.mode == "3840x2160@59.940"
    assert box.calls[-1][0] == "xrandr" and sh.calls[0] == ("open", "settings")
    assert sh.events[-1] == ("display", {"state": "pending", "mode": "3840x2160@59.940", "previous": "1920x1080@60.000",
                                         "seconds": 30}) and ("open_settings", {"section": "display"}) in sh.events
    assert p.apply_mode("1280x720@60.000", True)["error"] == "Keep or revert the last change first."
    assert p.modes()["pending"] == {"mode": "3840x2160@59.940", "previous": "1920x1080@60.000", "seconds": 30}
    assert p.keep_mode() == {"ok": True, "state": "kept", "mode": "3840x2160@59.940"} and p.pending is None
    assert saved_mode((tmp / "monitors.xml").read_text(), TV) == (3840, 2160, 59.94, 1.0)
    assert oct((tmp / "monitors.xml").stat().st_mode & 0o777) == "0o664" and sh.events[-1][1]["state"] == "kept"
    assert p.keep_mode() == {"ok": False, "error": "nothing to keep"} and p.modes()["saved"] == "3840x2160@59.940"
    box.stale = 1
    assert p.apply_mode("1280x720@60.000", True)["ok"] and box.mode == "1280x720@60.000"  # (the retry: a new serial)
    assert p.revert_mode() == {"ok": True, "state": "reverted", "mode": "3840x2160@59.940"} and box.mode == \
        "3840x2160@59.940" and sh.events[-1] == ("display", {"state": "reverted", "mode": "3840x2160@59.940",
                                                             "why": "asked"})
    assert p.revert_mode()["error"] == "nothing to revert"
    p.keep_s = .2  # the timeout reverts by itself, and only the change it was set for
    assert p.apply_mode("1920x1080@60.000", True)["ok"] and box.mode == "1920x1080@60.000"
    time.sleep(.5)
    assert box.mode == "3840x2160@59.940" and sh.events[-1][1] == {"state": "reverted", "mode": "3840x2160@59.940",
                                                                    "why": "timeout"}
    box.fail = {"busctl"}
    assert p.apply_mode("1920x1080@60.000", True)["error"] == "No display connected (is the TV on?)"
    box.fail = set()
    p.apply_mode("1920x1080@60.000", True)
    box.fail = {"busctl"}  # (a revert that fails: SSE failed and a toast)
    assert p.revert_mode()["error"] == NO_TV and sh.events[-2] == ("display", {"state": "failed", "error": NO_TV})
    box.fail, box.mode = set(), "3840x2160@59.940"
    sh.games = ["steam:1174180"]
    assert p.apply_mode("1920x1080@60.000", True)["error"] == "Quit Red Dead Redemption 2 first: changing the " \
        "resolution under a running game can crash it."
    sh.games = []
    # -- Keep keeps only what mutter shows: a TV power cycle (an input switch, a hotplug) in the countdown put mutter
    # back on its login config. Then Keep saves nothing and switches back: nothing to apply when mutter shows the mode
    # before already; from a third mode, to the mode before
    xml_kept, p.keep_s, box.mode = (tmp / "monitors.xml").read_text(), 30, "1920x1080@60.000"
    assert p.apply_mode("3840x2160@30.000", True)["state"] == "pending" and box.mode == "3840x2160@30.000"
    box.mode, box.serial = "1920x1080@60.000", box.serial + 1
    box.calls.clear()
    assert p.keep_mode() == {"ok": False, "error": "The TV isn't showing 4K · 30 Hz any more, so it wasn't kept. Back "
                                                   "to 1080p · 60 Hz."}
    assert p.pending is None and (tmp / "monitors.xml").read_text() == xml_kept and box.mode == "1920x1080@60.000"
    assert not [c for c in box.calls if "ApplyMonitorsConfig" in c] and sh.events[-1] == (
        "display", {"state": "reverted", "mode": "1920x1080@60.000", "why": "changed"})
    assert p.apply_mode("1280x720@60.000", True)["ok"]
    box.mode = "3840x2160@30.000"
    assert p.keep_mode()["error"].endswith(" Back to 1080p · 60 Hz.") and box.mode == "1920x1080@60.000"
    # -- a modeset slower than busctl's wait (APPLY_S): mutter has the call and applies it, so it is a change waiting
    # for Keep (the sheet, the timer), not a failure; so is a slow revert. One mutter never applied is the failure
    box.slow = True
    assert p.apply_mode("3840x2160@59.940", True) == {"ok": True, "state": "pending", "seconds": 30}
    assert box.mode == "3840x2160@59.940" and box.timeouts[-1] == APPLY_S and sh.events[-1][1]["state"] == "pending"
    assert p.revert_mode() == {"ok": True, "state": "reverted", "mode": "1920x1080@60.000"} and \
        box.mode == "1920x1080@60.000"
    box.slow = "lost"
    assert p.apply_mode("3840x2160@59.940", True)["error"].startswith("Couldn't switch to 4K · 59.94 Hz: Command [")
    assert p.pending is None and box.mode == "1920x1080@60.000"
    box.slow = False
    # -- the guard on the fake mutter: monitors.xml says 4K59.94 (kept above); mutter shows 1080p after a TV power
    # cycle -> re-applied, then not again within GUARD_EVERY_S; a TV that comes back gets the brightness again
    box.mode, box.tv, p._guarded, p._seen = "1920x1080@60.000", False, 0.0, True
    p._guard_tick()
    assert p._seen is False and box.mode == "1920x1080@60.000"  # (no TV: nothing)
    box.tv = True
    box.calls.clear()
    p._guard_tick()
    assert box.mode == "3840x2160@59.940" and [c[0] for c in box.calls].count("xrandr") == 2, box.calls
    box.mode = "1920x1080@60.000"
    p._guard_tick()
    assert box.mode == "1920x1080@60.000"  # (at most every 30 s)
    p._guarded = 0.0
    p.pending = {"mode": "x"}
    p._guard_tick()
    assert box.mode == "1920x1080@60.000"  # (never while a change waits for Keep)
    p.pending = None
    sh.games = ["steam:1174180"]  # (nor while a game runs, which may have set its own mode: once it has quit)
    p._guard_tick()
    assert box.mode == "1920x1080@60.000"
    sh.games = []
    p._guard_tick()
    assert box.mode == "3840x2160@59.940"
    # -- the demo: the measured modes in memory; apply then wait out keep_s gives reverted (timeout); apply then Keep
    # saves it; a running demo game refuses. Its settings live in memory: the owner's file is never read or written
    d = _FakeShell(True, {})
    dp = Prefs(d, tmp / "demo.json", tmp / "never.xml", keep_s=.2)
    dp.run = None  # (--demo runs nothing)
    (tmp / "demo.json").write_text('{"display.brightness": 60}')
    dp.load()
    assert dp.overlay == {} and dp.values(["display.brightness"]) == {"display.brightness": 100}
    assert dp.modes()["current"] == "1920x1080@60.000" and dp.apply_mode("3840x2160@30.000", True)["ok"]
    assert dp.values(["display.mode"]) == {"display.mode": "3840x2160@30.000"}
    time.sleep(.5)
    assert d.events[-1] == ("display", {"state": "reverted", "mode": "1920x1080@60.000", "why": "timeout"})
    assert dp.apply_mode("1280x720@60.000", True)["ok"] and dp.keep_mode()["ok"] and dp.modes()["saved"] == \
        "1280x720@60.000" and not (tmp / "never.xml").exists()
    d.games = ["steam:1"]
    assert dp.apply_mode("1920x1080@60.000", True)["error"].startswith("Quit Red Dead Redemption 2 first")
    assert dp.change("display.brightness", 130, person=True)["ok"] and dp.brightness(dim=True)
    assert dp.change("sleep.minutes", 5)["ok"] and (tmp / "demo.json").read_text() == '{"display.brightness": 60}'
    assert dp.change("display.color_range", "limited", person=True)["ok"] and \
        dp.values(["display.color_range"])["display.color_range"] == "limited"
    assert dp.change("sound.volume", 70)["ok"] and dp.values(["sound.volume"]) == {"sound.volume": 70}
    # -- the sections: their keys, every picker, the info; the PIN only to a loopback client
    app = type("App", (), {"lan_url": "http://spark.local:8780/remote", "port": 8780,
                           "pairing": type("P", (), {"pin": "123456", "hashes": {"a"}})()})()
    s = dp.section("display")
    assert s["values"] == {"display.mode": "1280x720@60.000", "display.brightness": 130, "display.color_range":
                           "limited"} and s["info"] == {"output": "HDMI-0", "monitor": "TCL 50S425", "saved":
                                                        "1280x720@60.000", "pending": None}
    assert s["choices"]["display.mode"][0] == {"value": "3840x2160@59.940", "label": "4K · 59.94 Hz"} and \
        s["choices"]["display.color_range"] == CHOICES["display.color_range"]
    s = dp.section("voice")
    assert s["values"] == {"tts.enabled": False, "tts.voice": "af_heart", "tts.volume": 100} and len(
        s["choices"]["tts.voice"]) == 28 and s["info"] == {"kokoro": True, "siri_remote": "connected", "remote_mic":
                                                           "off", "asr": "not set up"}
    assert dp.section("sleep")["choices"]["sleep.minutes"][-1] == {"value": 60, "label": "1 hour"} and \
        CHOICES["sleep.game_minutes"][-1] == {"value": 120, "label": "2 hours"}
    assert dp.section("controllers")["info"]["devices"][0] == {"name": "Siri Remote", "kind": "remote",
                                                                "connected": True, "battery": 64}
    net = dp.section("network", app, local=True)["info"]
    assert net["phone"] == {"url": app.lan_url, "pin": "123456", "paired": 1} and net["mcp"] == \
        "http://127.0.0.1:8780/mcp" and net["addresses"][0] == {"kind": "Wi-Fi", "address": "192.168.1.20"}
    assert dp.section("network", app)["info"]["phone"]["pin"] is None
    ab = dp.section("about")["info"]
    assert re.fullmatch(r"[0-9a-f]{7}", ab["build"]) and ab["device"] == "NVIDIA DGX Spark" and ab["gpu"]["temp"] == 41
    assert set(dp.values()) == set(KEYS) and dp.section("power")["info"] == {"service": "INVOCATION_ID" in os.environ}
    # -- BlueZ: the Siri Remote by Apple's Modalias (its name is its serial), two pads, the unpaired one skipped; a
    # battery from BlueZ or the power-supply class; the pw-dump and ip parses
    state, devs = parse_bluez(_Box.BLUEZ, lambda mac: 55 if mac == "02:00:00:00:00:5F" else None)
    assert state == "on" and devs == [
        {"name": "DualSense Wireless Controller", "kind": "controller", "connected": True, "battery": 55},
        {"name": "Xbox Wireless Controller", "kind": "controller", "connected": True, "battery": 80},
        {"name": "Siri Remote", "kind": "remote", "connected": False, "battery": None}], devs
    assert parse_bluez({"data": [{}]})[0] == "unavailable" and parse_sinks([1, None, {"type": "x"}]) == ([], None)
    assert parse_addresses(["x", {"ifname": "eth0", "addr_info": [{"family": "inet6", "local": "fe80::1"}]}]) == []
    assert p.section("controllers")["info"]["bluetooth"] == "on" and p.section("network")["info"]["addresses"] == [
        {"kind": "Wi-Fi", "address": "192.168.1.20"}, {"kind": "Tailscale", "address": "100.64.0.2"}]
    box.fail = {"busctl", "ip"}
    assert p.section("controllers")["info"] == {"bluetooth": "unavailable", "devices": []}
    assert p.section("display")["info"]["error"] == NO_TV and p.section("network")["info"]["addresses"] == []
    box.fail = set()
    # -- voices: only the English ones (a… American, b… British; f female, m male), labelled; none: no choices
    assert voice_names(tmp / "voices.bin") == ["af_heart", "am_adam", "bf_emma", "bm_george"]
    assert voice_choice("bf_emma") == {"value": "bf_emma", "label": "Emma", "detail": "British · female"} and \
        voice_choice("am_adam")["detail"] == "American · male" and voice_names(tmp / "none.bin") == []
    assert [c["value"] for c in p.voices()] == ["af_heart", "am_adam", "bf_emma", "bm_george"]
    # ...and a saved voice outlives a start without the voices file (Kokoro reinstalled, --voices moved): kept through
    # a save meanwhile, it's the voice again once the file is back; junk, or a voice the file lacks, is still dropped
    cmd = lambda: {"tts": {"server_cmd": tts["server_cmd"]}}  # noqa: E731  (its --voice: bf_emma)
    q = Prefs(_FakeShell(False, cmd()), tmp / "state" / "q.json", tmp / "monitors.xml")
    q.run = box
    q.path.write_text(json.dumps({"tts.voice": "am_adam", "display.brightness": 110, "sleep.signal": "dim"}))
    (tmp / "voices.bin").rename(tmp / "voices.off")
    q.load()
    assert q.overlay == {"tts.voice": "am_adam", "display.brightness": 110} and q.values(["tts.voice"]) == {
        "tts.voice": "am_adam"}, q.overlay
    q.shell.settings["display"] = {}
    assert q.change("display.brightness", 120, person=True)["ok"] and json.loads(q.path.read_text()) == {
        "display.brightness": 120, "tts.voice": "am_adam"}
    (tmp / "voices.off").rename(tmp / "voices.bin")
    q = Prefs(_FakeShell(False, cmd()), q.path, tmp / "monitors.xml")
    q.load()
    assert q.values(["tts.voice"]) == {"tts.voice": "am_adam"}
    for bad in ("../x", "zf_xiaobei", 7):
        q.path.write_text(json.dumps({"tts.voice": bad}))
        (tmp / "voices.bin").rename(tmp / "voices.off")
        q = Prefs(_FakeShell(False, cmd()), q.path, tmp / "monitors.xml")
        q.load()
        (tmp / "voices.off").rename(tmp / "voices.bin")
        assert q.overlay == {}, bad
    q.path.write_text(json.dumps({"tts.voice": "bf_lily"}))  # (the file is back, and has no Lily)
    q = Prefs(_FakeShell(False, cmd()), q.path, tmp / "monitors.xml")
    q.load()
    assert q.overlay == {}
    # -- the sleep timer's choices for minutes said
    assert [snap(n) for n in (20, 45, 0, 200, 1, 7.5, 60, 90, -5)] == [15, 60, 0, 60, 5, 10, 60, 60, 0]
    # -- power: the person's alone; the answer first, then systemctl; SparkHome restarts only as its unit; failures
    # are toasts; --demo only says what it would do
    global POWER_DELAY_S
    POWER_DELAY_S, wait = 0, lambda: time.sleep(.2)  # noqa: E731
    assert p.power("reboot") == {"ok": False, "error": PERSON_POWER} and p.power("explode", True)["ok"] is False
    box.calls.clear()
    assert p.power("reboot", True) == {"ok": True} and sh.events[-1] == ("toast", {"text": "Restarting the Spark…",
                                                                                   "kind": "info"})
    wait()
    assert box.calls == [["systemctl", "reboot"]]
    inv = os.environ.pop("INVOCATION_ID", None)
    assert p.power("restart_sparkhome", True)["error"].startswith("SparkHome isn't running as its service here")
    os.environ["INVOCATION_ID"] = "x"
    try:
        box.fail = {"systemctl"}
        assert p.power("restart_sparkhome", True)["ok"]
        wait()
        assert box.calls[-1] == ["systemctl", "--user", "restart", "--no-block", "sparkhome.service"]
        assert sh.events[-1] == ("toast", {"text": "Couldn't restart: ERROR: it failed", "kind": "error"})
    finally:
        os.environ.pop("INVOCATION_ID")
        if inv is not None:
            os.environ["INVOCATION_ID"] = inv
    box.fail = set()
    assert dp.power("shutdown", True) == {"ok": True} and d.events[-1] == (
        "toast", {"text": "(demo) The Spark would shut down now.", "kind": "info"})
    # -- the voice's power words only ask: Settings comes up at Power with the confirm sheet (no reopen when open)
    d.events.clear(), d.calls.clear()
    d.ui, d.surface = {"screen": "settings"}, "launcher"
    assert dp.ask_power("shutdown") == {"ok": True, "section": "power", "confirm": "shutdown"} and not d.calls
    assert d.events == [("open_settings", {"section": "power", "confirm": "shutdown"})]
    d.ui, d.surface = {"screen": "home"}, "web"
    d.open = lambda tid: {"ok": False, "error": "no tile 'settings'"}  # (a config without the Settings tile)
    assert dp.open("sound")["ok"] and d.calls == [("home",)] and d.events[-1] == ("open_settings", {"section": "sound"})
    assert d.ui["screen"] == "settings"  # (from here: agents' navigate and select there are refused at once)
    POWER_DELAY_S = 1
    print("settings selftest OK: overlay, policy, brightness, color range, sound, mutter modes, Keep/revert/guard, "
          "monitors.xml, BlueZ, pw-dump, ip, voices, power, demo")
