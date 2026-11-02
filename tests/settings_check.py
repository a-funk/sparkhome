"""Settings and sleep on the launcher page in a real headless Chrome, on the demo daemon (settings-notes/SPEC.md §6): an
in-process demo Shell with its Prefs and the real server on a free port (LAN off); Chrome with a temporary profile,
--password-store=basic and a free CDP port, at 1920x1080. Inputs arrive as the daemon sends them (siri.py's thread, the
phone's /api/action with SparkHome's Origin, CDP keys and clicks); assertions go through window.SH.settings() and the
daemon's own values. At each state it can screenshot, no two visible Settings parts may overlap, and an uncaught page
exception (or an SSE handler's console error) fails it.

    python3 tests/settings_check.py CHROME [SHOTS_DIR]      # SHOTS_DIR: keep screenshots there

Like tests/switcher_check.py, it starts no Chrome beside the TV's kiosk (its DevTools port, 9223, answers) or when
SPARKHOME_NO_CHROME is set.
"""
import base64
import http.client
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sparkhome import server, settings as settings_mod, shell as shell_mod  # noqa: E402
from sparkhome.cdp import CDP  # noqa: E402

# The visible Settings parts (title, sections, rows and their label/track/value, the pushed list's title, footer
# paragraphs, the help list, toasts, the sheet) whose boxes cross, by name. Rows scrolled out of the pane's window
# don't count; the sheet sits over the dimmed page by design
OVERLAPS = ("(() => { const v = document.getElementById('setview').getBoundingClientRect(), seen = e => { if (!e.closest("
            "'#setview')) return true; const r = e.getBoundingClientRect(); return r.bottom > v.top + 1 && r.top < v.bottom "
            "- 1; }; const ps = [...document.querySelectorAll('#settings > h1, #setlist .srow, #setlist .srow > *, #sethead, "
            "#setrows .srow, #setrows .srow > *, #setrows .help > *, #setfoot p, .toast, #setsheet, #setsheet h2, #setsheet "
            "p, #setsheet .pill')].filter(e => e.checkVisibility({opacityProperty: true, visibilityProperty: true}) && seen("
            "e)), sheet = (a, b) => [a, b].some(e => e.closest('#setsheet')) && [a, b].some(e => !e.closest('#setsheet') && "
            "e.closest('#settings')), name = e => `${e.id || e.className || e.tagName} ${(e.textContent || '').slice(0, 24)}`"
            ".trim(); return ps.flatMap((a, i) => ps.slice(i + 1).filter(b => { const p = a.getBoundingClientRect(), q = b."
            "getBoundingClientRect(); return !a.contains(b) && !b.contains(a) && !sheet(a, b) && p.left < q.right - .5 && "
            "q.left < p.right - .5 && p.top < q.bottom - .5 && q.top < p.bottom - .5; }).map(b => `${name(a)} x ${name(b)}`)); "
            "})()")
# A row whose label and value don't fit it (a value cut to an ellipsis, a label pushed under it)
CUT = ("[...document.querySelectorAll('#setrows .srow, #setlist .srow')].filter(r => [...r.children].some(c => c.scrollWidth > "
       "c.clientWidth + 1)).map(r => r.textContent)")


def until(fn, t=5.0):
    end = time.monotonic() + t
    while not fn() and time.monotonic() < end:
        time.sleep(0.03)
    return fn()


def free_port():
    with socket.socket() as so:                  # never another Chrome's (the kiosk's is 9223)
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


def check(sh, app, prefs, cdp, s, previews, dims, shots):
    def js(expr, sess=s):
        r = cdp.call("Runtime.evaluate", session=sess, expression=expr, returnByValue=True, awaitPromise=True)
        assert "exceptionDetails" not in r, r["exceptionDetails"]
        return r["result"].get("value")

    st = lambda: js("SH.settings()")
    doze = lambda: js("SH.doze()")
    toasts = lambda: js("[...document.querySelectorAll('.toast')].map(t => t.textContent)")
    focused = lambda: js("document.querySelector('#setrows .srow.on')?.textContent ?? null")
    labels = lambda: js("[...document.querySelectorAll('#setrows .srow .lab')].map(l => l.textContent)")
    me = {"Origin": f"http://127.0.0.1:{app.port}"}   # SparkHome's own pages: the person

    def api(path, body, headers=None):          # no Origin: a loopback agent
        c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
        c.request("POST", path, json.dumps(body), {"Content-Type": "application/json", **(headers or {})})
        r = json.loads(c.getresponse().read())
        c.close()
        return r

    def siri(fn, *a):                            # siri.py runs its actions on this thread
        th = threading.Thread(target=fn, args=a, name="siri-actions")
        th.start()
        th.join()

    codes = {"ArrowUp": 38, "ArrowDown": 40, "ArrowLeft": 37, "ArrowRight": 39, "Enter": 13, "Escape": 27}

    def key(*ks):                                # the keyboard (and FLIRC's IR remote), as the page gets it
        for k in ks:
            for typ in ("keyDown", "keyUp"):
                cdp.call("Input.dispatchKeyEvent", session=s, type=typ, key=k, code=k, windowsVirtualKeyCode=codes[k])

    order = ["display", "sound", "voice", "sleep", "controllers", "network", "power", "about"]
    steps = lambda d: ["ArrowDown" if d > 0 else "ArrowUp"] * abs(d)

    def go(sec, label=None):                     # by the keys, as a person: Back to the sections, Up or Down to it,
        while st()["stack"] or st()["sheet"] or st()["pattern"] or st()["at"] == "pane":   # Right into its rows (its
            key("Escape")                        # data in), Up or Down to the row
        key(*steps(order.index(sec) - order.index(st()["sec"])))
        assert until(lambda: st()["sec"] == sec), (sec, st())
        if label:
            key("ArrowRight")
            assert until(lambda: st()["shown"] == sec and st()["loaded"] and st()["at"] == "pane" and label in labels()), st()
            key(*steps(labels().index(label) - st()["row"]))
            assert until(lambda: (focused() or "").startswith(label)), (label, focused())

    def look(name):                              # settled (the pane's data in, slides, toasts): nothing crosses, nothing
        assert until(lambda: not js("document.getElementById('setfoot').textContent.startsWith('Loading')"))   # is cut;
        time.sleep(0.5)                          # and a screenshot when SHOTS is given
        crossed, cut = js(OVERLAPS), js(CUT)
        assert crossed == [] and cut == [], (name, crossed, cut)
        if shots:
            png = cdp.call("Page.captureScreenshot", session=s, format="png", timeout=20)["data"]
            (Path(shots) / f"settings-{name}.png").write_bytes(base64.b64decode(png))

    # 1. the tile says what's in it; OK on it opens Settings on Display, focus in the sections; the daemon hears where
    assert js("document.querySelector('.wface.settings .wsub').textContent") == "Display · Sound · Sleep"
    js("focusId('settings')")
    time.sleep(0.3)
    key("Enter")
    assert until(lambda: (st() or {}).get("shown") == "display"), st()
    assert st()["at"] == "list" and js("SH.f().screen") == "settings"
    assert until(lambda: sh.ui.get("screen") == "settings" and sh.ui.get("row") == "settings" and
                 sh.ui.get("focused") == "settings:display"), sh.ui
    assert until(lambda: st()["vals"].get("display.brightness") == 100), st()
    assert st()["live"] == "Display", st()["live"]
    look("display")
    # 2. the sections, by the Siri Remote (siri.py's thread), down to About, each rendered whole; no wrap at the end; back
    # up by the keyboard; a step each way by the phone (SparkHome's Origin: the person)
    for sec in order[1:]:
        siri(sh.navigate, "down")
        assert until(lambda: st()["sec"] == sec and st()["shown"] == sec), (sec, st())
        look(sec)
    siri(sh.navigate, "down")
    time.sleep(0.3)
    assert st()["sec"] == "about" and until(lambda: sh.ui.get("focused") == "settings:about"), (st(), sh.ui)
    for sec in reversed(order[:-1]):
        key("ArrowUp")
        assert until(lambda: st()["sec"] == sec), (sec, st())
    assert api("/api/action", {"name": "navigate", "args": {"direction": "down"}}, me)["ok"]
    assert until(lambda: st()["sec"] == "sound")
    assert api("/api/action", {"name": "navigate", "args": {"direction": "up"}}, me)["ok"]
    assert until(lambda: st()["sec"] == "display" and st()["shown"] == "display")
    # 3. agents can't move or press in it (the phone could, above); the person's place stays
    for name in ("navigate", "select"):
        r = api("/api/action", {"name": name, "args": {"direction": "down"} if name == "navigate" else {}})
        assert r["ok"] is False and "Settings" in r["error"], r
    time.sleep(0.3)
    assert (st()["sec"], st()["at"]) == ("display", "list"), st()
    # 4. Display › Brightness: a slider changes only once OK takes it (a value changes after Select, as tvOS lists):
    # Right only bumps, said as such. Taken, Right twice is 120 % (the daemon's value); Left twice, 100 % again; OK
    # lets go, and Left then goes back to the sections, as from any row
    key("ArrowRight")
    assert until(lambda: st()["at"] == "pane" and st()["row"] == 0 and (focused() or "").startswith("Resolution")), st()
    key("ArrowDown", "ArrowRight")
    time.sleep(0.3)
    assert prefs.values()["display.brightness"] == 100 and st()["live"] == "Display, Brightness, 100 percent, press " \
        "OK to adjust", st()
    key("Enter", "ArrowRight", "ArrowRight")
    assert until(lambda: prefs.values()["display.brightness"] == 120), prefs.values()
    assert st()["live"] == "Display, Brightness, 120 percent, Left or Right to adjust, OK when done" and \
        focused() == "Brightness120 %", (st(), focused())
    look("brightness")
    key("ArrowLeft", "ArrowLeft")
    assert until(lambda: prefs.values()["display.brightness"] == 100), prefs.values()
    key("Enter", "ArrowLeft")
    assert until(lambda: st()["at"] == "list") and prefs.values()["display.brightness"] == 100, (st(), prefs.values())
    # 5. Sound › Volume, its first row: a Siri slide right from Sound (siri.py: a step per third of the surface, 0.12 s
    # apart) only enters the rows, and Left from Volume goes back, the TV's volume unchanged. It follows the remote's
    # Vol± (SSE settings), and taken, moves the daemon's; Mute flips both ways
    v0 = sh._demo["vol"]
    key(*steps(order.index("sound") - order.index(st()["sec"])))
    assert until(lambda: st()["shown"] == "sound" and st()["loaded"]), st()
    for _ in range(3):
        siri(sh.navigate, "right")
        time.sleep(0.12)
    assert until(lambda: st()["at"] == "pane" and (focused() or "").startswith("Volume")), st()
    time.sleep(0.3)
    key("ArrowLeft")
    assert until(lambda: st()["at"] == "list") and sh._demo["vol"] == v0, (st(), sh._demo)
    go("sound", "Volume")
    assert until(lambda: focused() == f"Volume{v0} %"), focused()
    sh.volume("up")
    assert until(lambda: focused() == f"Volume{v0 + 5} %"), focused()
    key("Enter", "ArrowRight")
    assert until(lambda: sh._demo["vol"] == v0 + 10), sh._demo
    key("ArrowDown", "Enter")
    assert until(lambda: sh._demo.get("muted") is True and focused() == "MuteOn"), (sh._demo, focused())
    key("Enter")
    assert until(lambda: sh._demo.get("muted") is False and focused() == "MuteOff"), (sh._demo, focused())
    # 6. Voice › Voice: its list, focused on the voice in use (checked); OK on another sets it and asks for its preview;
    # Speech volume asks for one preview, at the level it rests on (600 ms after the last step)
    go("voice", "Voice")
    key("Enter")
    assert until(lambda: (st()["stack"] or {}).get("key") == "tts.voice"), st()
    v0 = prefs.values()["tts.voice"]
    assert js("document.querySelector('#setrows .srow.on').classList.contains('chk')"), "the list isn't on the voice in use"
    key("ArrowDown", "Enter")
    assert until(lambda: prefs.values()["tts.voice"] != v0 and previews[-1:] == [prefs.values()["tts.voice"]]), \
        (prefs.values(), previews)
    assert js("document.querySelector('#setrows .srow.on').classList.contains('chk')"), "the checkmark didn't move"
    look("voices")
    key("Escape")
    assert until(lambda: st()["stack"] is None and (focused() or "").startswith("Voice")), st()
    n = len(previews)
    key("ArrowDown", "Enter", "ArrowLeft", "ArrowLeft")
    assert until(lambda: prefs.values()["tts.volume"] == 80), prefs.values()
    time.sleep(0.4)
    assert len(previews) == n + 1, previews
    # 7. Sleep › Sleep after: 5 minutes, and the status line says when
    go("sleep", "Sleep after")
    key("Enter")
    assert until(lambda: (st()["stack"] or {}).get("key") == "sleep.minutes"), st()
    key("ArrowDown", "Enter")
    assert until(lambda: prefs.values()["sleep.minutes"] == 5 and sh.settings["sleep"]["minutes"] == 5), prefs.values()
    key("Escape")
    assert until(lambda: "Sleeps in 5 minutes" in js("document.getElementById('setrows').textContent"), 7), \
        js("document.getElementById('setrows').textContent")
    look("sleep")
    # 8. Display › Resolution: its list, checked on the mode in use; another asks to keep it, Revert focused, counting
    # down from 15 s; Back reverts (a toast says to what) and the list stays; Keep keeps; then back to the first one
    go("display", "Resolution")
    key("Enter")
    assert until(lambda: (st()["stack"] or {}).get("key") == "display.mode"), st()
    mode0, label0 = prefs.values()["display.mode"], js("document.querySelector('#setrows .srow.chk').textContent")
    key(*["ArrowUp"] * len(labels()), "Enter")   # (the top: 4K at its highest refresh)
    assert until(lambda: (st()["sheet"] or {}).get("kind") == "keep"), st()
    assert st()["sheet"]["i"] == 0 and js("document.querySelector('#setsheet .pill.on').textContent") == "Revert"
    body = js("document.getElementById('setsb').textContent")
    assert re.fullmatch(r"4K · [\d.]+ Hz\. Switching back in 1[45] s\.", body), body
    look("keep")
    key("Escape")
    assert until(lambda: st()["sheet"] is None and prefs.values()["display.mode"] == mode0), (st(), prefs.values())
    assert until(lambda: f"Switched back to {label0}." in toasts()), toasts()
    assert (st()["stack"] or {}).get("key") == "display.mode", st()
    key("Enter")
    assert until(lambda: (st()["sheet"] or {}).get("kind") == "keep"), st()
    key("ArrowRight", "Enter")
    assert until(lambda: st()["sheet"] is None and prefs.values()["display.mode"] != mode0), (st(), prefs.values())
    assert until(lambda: js("document.querySelector('#setrows .srow.chk').textContent").startswith("4K")), "no checkmark"
    key(*steps(labels().index(label0) - st()["stack"]["i"]), "Enter")
    assert until(lambda: (st()["sheet"] or {}).get("kind") == "keep"), st()
    key("ArrowRight", "Enter")
    assert until(lambda: st()["sheet"] is None and prefs.values()["display.mode"] == mode0), (st(), prefs.values())
    # ...and by voice: its reply is up for the sheet's first 5 s, when the footer says "No picture? Press Back". One
    # Back (Escape: the IR remote's Menu, a pad's B) reverts at once, and the reply goes with it
    talk = lambda: js("document.getElementById('stage').classList.contains('talk')")   # noqa: E731
    assert api("/api/voice", {"text": "set the resolution to 4k"})["ok"]
    assert until(lambda: (st()["sheet"] or {}).get("kind") == "keep" and talk() and prefs.pending), (st(), talk())
    key("Escape")
    assert until(lambda: st()["sheet"] is None and prefs.pending is None and prefs.values()["display.mode"] == mode0), \
        (st(), prefs.pending)
    assert not talk()
    # 9. Display › Test pattern: over everything; OK closes it, and so does Back
    go("display", "Test pattern")
    key("Enter")
    assert until(lambda: st()["pattern"] and js("!document.getElementById('pattern').hidden")), st()
    assert js("[...document.querySelectorAll('#pattern i b')].map(b => +b.textContent)") == \
        [*range(0, 25, 2), 230, 234, 238, 242, 246, 250, 254, 255]
    look("pattern")
    key("Enter")
    assert until(lambda: not st()["pattern"])
    key("Enter")
    assert until(lambda: st()["pattern"])
    key("Escape")
    assert until(lambda: not st()["pattern"] and js("document.getElementById('pattern').hidden"))
    # 10. Power › Restart the Spark: the sheet, Cancel focused; OK cancels (nothing sent); again, Right and OK restarts
    # (the demo only toasts: no goodbye screen)
    go("power", "Restart the Spark")
    key("Enter")
    assert until(lambda: st()["sheet"] == {"kind": "power", "i": 0, "action": "reboot"}), st()
    assert js("[...document.querySelectorAll('#setsheet .pill')].map(p => p.textContent)") == ["Cancel", "Restart"]
    look("power")
    key("Enter")
    assert until(lambda: st()["sheet"] is None)
    time.sleep(0.3)
    assert not any(t.startswith("(demo)") for t in toasts()), toasts()
    key("Enter", "ArrowRight", "Enter")
    assert until(lambda: any(t.startswith("(demo)") for t in toasts())), toasts()
    assert st()["sheet"] is None and js("document.getElementById('setbye').hidden")
    # 11. Controllers › How to use the remotes (the old panel's list), pushed; Network's PIN, big; About scrolls by rows
    go("controllers", "How to use the remotes")
    key("Enter")
    assert until(lambda: (st()["stack"] or {}).get("help")), st()
    assert js("[...document.querySelectorAll('#setrows dt')].map(d => d.textContent)") == \
        ["Remote", "Siri Remote", "Gamepad", "Keyboard", "Voice", "Open apps"]
    look("help")
    go("network", "PIN")
    assert js("document.querySelector('#setrows .srow.code .val').textContent") == f"{app.pairing.pin[:3]} {app.pairing.pin[3:]}"
    look("network")
    go("about", "AI models loaded")
    assert until(lambda: st()["row"] == len(st()["rows"]) - 1 and st()["k"] >= 1), st()
    look("about")
    # ...and by the pointer (the phone's trackpad): a section is focus + OK, a slider's track sets the value under it, a
    # row is focus + OK, the wheel steps
    def click(sel, fx=.5):
        x, y = js(f"(r => [r.left + r.width * {fx}, r.top + r.height / 2])(document.querySelector({json.dumps(sel)})"
                  ".getBoundingClientRect())")
        for typ, at in (("mouseMoved", x - 40), ("mouseMoved", x), ("mousePressed", x), ("mouseReleased", x)):
            cdp.call("Input.dispatchMouseEvent", session=s, type=typ, x=at, y=y,
                     **({} if typ == "mouseMoved" else {"button": "left", "clickCount": 1}))
    click("#setlist .srow:nth-child(2)")
    assert until(lambda: st()["shown"] == "sound" and st()["at"] == "pane" and st()["loaded"]), st()
    click("#setrows .srow.slider .trk", .8)
    assert until(lambda: sh._demo["vol"] == 80 and focused() == "Volume80 %"), (sh._demo, focused())
    click("#setrows .srow:nth-child(2)")
    assert until(lambda: sh._demo.get("muted") is True and st()["row"] == 1), (sh._demo, st())
    click("#setrows .srow:nth-child(2)")
    assert until(lambda: sh._demo.get("muted") is False), sh._demo
    cdp.call("Input.dispatchMouseEvent", session=s, type="mouseWheel", x=1200, y=500, deltaX=0, deltaY=120)
    assert until(lambda: st()["row"] == 2), st()
    # 12. Home closes it from anywhere, the Settings tile focused; the daemon's open (voice) opens it again
    sh.home()
    assert until(lambda: st() is None and js("SH.f().screen") == "home" and js("SH.f().id") == "settings"), js("SH.f()")
    assert until(lambda: sh.ui.get("screen") == "home"), sh.ui
    assert sh.open("settings")["ok"] and until(lambda: (st() or {}).get("shown") == "display"), st()
    # 13. a deep link (SSE open_settings, as "restart the Spark" sends it): Power, the reboot sheet over it, Cancel first
    sh.emit("open_settings", {"section": "power", "confirm": "reboot"})
    assert until(lambda: st()["shown"] == "power" and st()["at"] == "pane" and st()["row"] == 1 and
                 st()["sheet"] == {"kind": "power", "i": 0, "action": "reboot"}), st()
    key("Escape")
    assert until(lambda: st()["sheet"] is None)
    # 14. Settings › Sleep Now: black at once, and it stays so (the keepalive its own key sends goes first: answered
    # after it, 2 in 20 woke the TV again). A Siri click wakes it and does nothing else (OK here would sleep it again);
    # the phone page says the TV is asleep, and its OK only wakes it; a key on the dark page doesn't move the focus
    go("sleep", "Sleep Now")
    rs = cdp.call("Target.attachToTarget", targetId=cdp.call("Target.createTarget", url="about:blank")["targetId"],
                  flatten=True)["sessionId"]
    cdp.call("Runtime.enable", session=rs)
    cdp.call("Page.navigate", session=rs, url=f"http://127.0.0.1:{app.port}/remote")
    phone = lambda: js("[document.getElementById('st').textContent, document.getElementById('reply').textContent]", rs)
    assert until(lambda: phone()[0] == "Home screen", 10), phone()
    where = lambda: (lambda d: (d["shown"], d["at"], d["row"], d["sheet"], d["stack"]))(st())
    status = lambda: js("[...document.querySelectorAll('#setrows .srow')].find(r => r.firstChild.textContent === "
                        "'Status')?.lastChild.textContent")
    at = where()
    for n in range(3):
        js("nudgeAt = -1e9")                     # (a keepalive due: this OK sends one too)
        key("Enter")
        assert until(lambda: "dark" in doze() and phone()[0] == "TV asleep"), (n, doze(), phone())
        time.sleep(0.6)
        assert sh.sleep_state()["state"] == "asleep" and "dark" in doze() and status() == "Asleep", \
            (n, sh.sleep_state(), doze(), status())
        siri(sh.select)
        assert until(lambda: "dark" not in doze() and phone()[0] == "Home screen"), (doze(), phone())
        assert until(lambda: status() != "Asleep", 0.7), status()   # (at once, not at its next 5 s refresh)
        time.sleep(0.3)
        assert where() == at and sh.sleep_state()["state"] == "awake", (where(), at, sh.sleep_state())
        time.sleep(0.8)                          # (past the grace after a wake)
    # a held OK is one press (X repeats it after 500 ms, every 30 ms on the Spark [M]): dark, and it stays so
    enter = lambda **kw: cdp.call("Input.dispatchKeyEvent", session=s, key="Enter", code="Enter",  # noqa: E731
                                  windowsVirtualKeyCode=13, **kw)
    enter(type="keyDown")
    assert until(lambda: "dark" in doze())
    time.sleep(0.5)
    for _ in range(4):
        enter(type="keyDown", autoRepeat=True)
        time.sleep(0.03)
    enter(type="keyUp")
    time.sleep(0.6)
    assert sh.sleep_state()["state"] == "asleep" and "dark" in doze(), (sh.sleep_state(), doze())
    siri(sh.select)
    assert until(lambda: "dark" not in doze()), doze()
    time.sleep(1.1)
    key("Enter")
    assert until(lambda: "dark" in doze())
    js("act('select')", rs)
    assert until(lambda: "dark" not in doze() and phone()[1] == "Woke the TV."), (doze(), phone())
    time.sleep(0.3)
    assert where() == at and sh.sleep_state()["state"] == "awake", (where(), at, sh.sleep_state())
    time.sleep(0.8)
    key("Enter")
    assert until(lambda: "dark" in doze())
    key("ArrowDown")
    assert until(lambda: "dark" not in doze()), doze()   # (its POST /api/sleep/wake woke the daemon)
    time.sleep(0.3)
    assert where() == at, (where(), at)
    # 15. the warning: Sleep after 5 minutes and 4:51 idle dim it and say so, counting down; a Siri step wakes it without
    # moving, and the picture's dim goes with it; past the grace, a step moves again
    time.sleep(0.8)
    dims.clear()
    sh._input_at = time.monotonic() - 291
    assert until(lambda: "drowsy" in doze() and js("getComputedStyle(document.getElementById('sleepmsg')).opacity") == "1",
                 3), doze()
    n1 = int(js("document.getElementById('sleepn').textContent"))
    assert 9 <= n1 <= 10 and js("document.querySelector('#sleepmsg b').textContent") == f"Going to sleep in {n1}"
    # (its words at full white, never faded with the dim they sit over: the TV halves them again, through gamma)
    assert js("[...document.querySelectorAll('#sleepmsg b, #sleepmsg > span')].map(e => { let o = 1; for (let a = e; "
              "a; a = a.parentElement) o *= +getComputedStyle(a).opacity; return [o, getComputedStyle(e).color]; })") \
        == [[1, "rgb(255, 255, 255)"]] * 2
    time.sleep(1.1)
    assert int(js("document.getElementById('sleepn').textContent")) < n1
    if shots:
        png = cdp.call("Page.captureScreenshot", session=s, format="png", timeout=20)["data"]
        (Path(shots) / "settings-warning.png").write_bytes(base64.b64decode(png))
    siri(sh.navigate, "down")
    assert until(lambda: "drowsy" not in doze() and dims == [True, False]), (doze(), dims)
    time.sleep(0.3)
    assert where() == at, (where(), at)
    time.sleep(0.8)
    siri(sh.navigate, "down")
    assert until(lambda: st()["row"] == at[2] + 1), st()
    assert api("/api/settings/sleep.minutes", {"value": 0})["ok"]   # (an agent may set the timer)
    # 16. Back from the rows, then from the sections, closes it: the launcher, the Settings tile focused
    key("Escape")
    assert until(lambda: st()["at"] == "list")
    key("Escape")
    assert until(lambda: st() is None and js("SH.f().id") == "settings" and sh.ui.get("screen") == "home"), sh.ui


def main():
    chrome, shots = sys.argv[1], (sys.argv[2:] or [None])[0]
    try:
        socket.create_connection(("127.0.0.1", 9223), 0.3).close()
        kiosk = True
    except OSError:
        kiosk = False
    if kiosk or os.environ.get("SPARKHOME_NO_CHROME"):
        return print("settings check skipped: " + ("the TV's kiosk Chrome is up here" if kiosk else "SPARKHOME_NO_CHROME"))
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
    logging.disable(logging.CRITICAL)
    tmp = Path(tempfile.mkdtemp(prefix="sparkhome-settings-"))
    port, cport = free_port(), free_port()
    p = subprocess.Popen([chrome, "--headless=new", f"--remote-debugging-port={cport}", f"--user-data-dir={tmp / 'chrome'}",
                          "--password-store=basic",       # as the kiosk: no keyring prompt (see skin_check.py)
                          "--window-size=1920,1080", "--no-first-run", "--mute-audio", "about:blank"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    srv, errors = None, []
    try:
        cfg = shell_mod.load_config(ROOT / "sparkhome.json")
        cfg["settings"].update(port=port, lan=False, request_log=False, mcp_servers=[],
                               tts={**cfg["settings"].get("tts", {}), "enabled": False})   # (nothing is spoken here)
        hub = server.Hub()
        sh = shell_mod.Shell(cfg, True, hub.emit)
        app = SimpleNamespace(shell=sh, hub=hub, pairing=server.Pairing(tmp / "tokens.json"), lan=False, port=port,
                              lan_url=f"http://spark.local:{port}/remote",   # (as with "lan": the PIN row shows)
                              hosts={"127.0.0.1", "localhost"})
        previews, dims = [], []                  # the voice previews asked for (never spoken here); the picture's dims
        prefs = sh.prefs = settings_mod.Prefs(sh, path=tmp / "settings.json", monitors=tmp / "monitors.xml")
        prefs.load()                             # (as server.main wires them, the owner's files in the temp dir)
        sh.on_dim = lambda on: (dims.append(on), prefs.brightness(dim=on))
        app.agent = agent = prefs.agent = server.load_agent(sh, hub.emit)
        agent.preview_voice = lambda voice: (previews.append(voice), True)[1]
        srv = server.make_server(app, "127.0.0.1", port)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        sh.start()
        prefs.start()
        cdp = CDP(cport, on_event=lambda m: errors.append(m["params"]) if m.get("method") == "Runtime.exceptionThrown" or
                  m.get("method") == "Runtime.consoleAPICalled" and m["params"].get("type") == "error" else None).start()
        assert cdp.connected.wait(15), "Chrome's DevTools didn't come up"
        target = cdp.call("Target.createTarget", url="about:blank")["targetId"]
        s = cdp.call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        cdp.call("Emulation.setDeviceMetricsOverride", session=s, width=1920, height=1080, deviceScaleFactor=1, mobile=False)
        cdp.call("Emulation.setFocusEmulationEnabled", session=s, enabled=True)
        cdp.call("Runtime.enable", session=s)
        cdp.call("Page.navigate", session=s, url=f"http://127.0.0.1:{port}/")
        ready = lambda: cdp.call("Runtime.evaluate", session=s, expression="!!window.SH?.ready",
                                 returnByValue=True)["result"].get("value")
        assert until(ready, 15), "the launcher didn't load"
        time.sleep(1)
        check(sh, app, prefs, cdp, s, previews, dims, shots)
        assert not errors, errors
        print("settings check passed")
    finally:                                     # an orderly exit, so no helper writes the profile after it's removed
        if srv:
            srv.shutdown()
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
