"""The app switcher in a real headless Chrome, on the demo daemon (DESIGN.md §10.3): an in-process demo Shell and the
real server on a free port (LAN off, stuck_s 1); Chrome with a temporary profile, --password-store=basic and a free
CDP port, at 1920x1080; inputs as the daemon sends them (siri.py's thread, SSE, CDP keys and mouse), assertions
through window.SH; at each state it can screenshot, no two visible switcher parts may overlap (and at three, no glyph of a
wordmark or title is clipped).

    python3 tests/switcher_check.py CHROME [SHOTS_DIR]      # SHOTS_DIR: keep screenshots there

Like tests/skin_check.py, it starts no Chrome beside the TV's kiosk (its DevTools port, 9223, answers) or when
SPARKHOME_NO_CHROME is set.
"""
import base64
import http.client
import json
import logging
import os
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
from sparkhome import server, shell as shell_mod  # noqa: E402
from sparkhome.cdp import CDP  # noqa: E402

RDR2, DOS2 = "steam:1174180", "steam:435150"
FIX = {"open": True, "from": RDR2, "apps": [  # SH.swOpen's fixture (DESIGN §14.1): six cards, five fit and one peeks;
    {"key": k, "title": t, "kind": kind, "tile": None, "state": st, "windows": 1, "can_close": k != RDR2,  # RDR2:
     "can_force": kind == "game", "snap": None} for k, t, kind, st in (  # a Wine desktop, which can't be asked
        (RDR2, "Red Dead Redemption 2", "game", "open"), ("netflix", "Netflix", "tab", "open"),
        (DOS2, "Divinity: Original Sin 2", "game", "stuck"), ("bigpicture", "Steam Big Picture", "system", "open"),
        ("x:steam", "Steam", "system", "closing"))]}
# The switcher's animations still running, longer than reduced motion's .01 s transitions (one of those not yet ticked
# is no motion: under load the first frame can come after 50 ms)
MOVING = ("document.getAnimations().filter(a => a.playState === 'running' && a.effect?.target?.closest?.('#switcher') && "
          "a.effect.getComputedTiming().duration > 10).map(a => (a.animationName || a.transitionProperty || 'waapi') + "
          "' ' + (a.effect.target.id || a.effect.target.className))")
# The visible switcher parts (faces, their wordmarks, state pills, × Quit, titles, detail lines, the hint, toasts, the
# voice panel, the sheet) whose boxes cross, by name; the force sheet sits over the dimmed faces by design
OVERLAPS = ("(() => { const ps = [...document.querySelectorAll('#swrow .card:not(.bye) :is(.face, .wm, .st, .x, .label b, "
            ".note), #swhint, .toast, #voice, #swsheet')].filter(e => e.checkVisibility({opacityProperty: true, "
            "visibilityProperty: true})), sheet = (a, b) => [a, b].some(e => e.id === 'swsheet') && [a, b].some(e => "
            "e.closest('#swrow .face')), name = e => `${e.closest('.card')?.dataset.id ?? ''} ${e.id || e.className || "
            "e.tagName}`.trim(); return ps.flatMap((a, i) => ps.slice(i + 1).filter(b => { const p = a."
            "getBoundingClientRect(), q = b.getBoundingClientRect(); return !a.contains(b) && !b.contains(a) && !sheet(a, "
            "b) && p.left < q.right && q.left < p.right && p.top < q.bottom && q.top < p.bottom; }).map(b => "
            "`${name(a)} x ${name(b)}`)); })()")

# The cards whose logo or wordmark isn't between their state pill and their face's bottom edge (a logo, at its own
# height, ran 36 px past a 300 px face: its bottom cut off)
UNDER = ("[...document.querySelectorAll('#swrow .card:is(.starting, .closing, .stuck):not(.peek)')].filter(c => { const l = "
         "c.querySelector('.l1 > *')?.getBoundingClientRect(); return l && (l.top < c.querySelector('.st')"
         ".getBoundingClientRect().bottom || l.bottom > c.querySelector('.face').getBoundingClientRect().bottom); })"
         ".map(c => c.dataset.id)")
# The face wordmarks and titles that their line clamp cuts (taller unclamped)
CUT = ("[...document.querySelectorAll('#swrow .card:not(.peek) :is(.wm, .label b)')].filter(e => { const h = "
       "e.offsetHeight; e.style.webkitLineClamp = 'none'; const cut = e.offsetHeight > h; e.style.webkitLineClamp = ''; "
       "return cut; }).map(e => `${e.closest('.card').dataset.id} ${e.matches('.wm') ? 'face' : 'title'}`)")


def until(fn, t=5.0):
    end = time.monotonic() + t
    while not fn() and time.monotonic() < end:
        time.sleep(0.03)
    return fn()


def free_port():
    with socket.socket() as so:                  # never another Chrome's (the kiosk's is 9223)
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


def check(sh, port, cdp, s, ag, shots):
    def js(expr):
        r = cdp.call("Runtime.evaluate", session=s, expression=expr, returnByValue=True, awaitPromise=True)
        assert "exceptionDetails" not in r, r["exceptionDetails"]
        return r["result"].get("value")

    sw = lambda: js("SH.sw()")
    keys = lambda: (sw() or {}).get("keys")

    def look(name):                              # settled (entrance, focus, fly-off, toasts): no two parts overlap; and
        time.sleep(0.6)                          # a screenshot when SHOTS is given, without the toasts: stuck_s 1 and
        crossed = js(OVERLAPS)                   # this pace leave ones a TV never shows with them ("Asking RDR2 to
        assert crossed == [], (name, crossed)    # quit…" beside its "Didn't quit", a setup's "Starting …")
        if shots:
            js("document.querySelectorAll('.toast').forEach(t => t.remove())")
            png = cdp.call("Page.captureScreenshot", session=s, format="png", timeout=20)["data"]
            (Path(shots) / f"switcher-{name}.png").write_bytes(base64.b64decode(png))

    # No glyph of a wordmark or title is clipped: frozen, the frame looks the same with their clips taken off (no pixel
    # moves by 24 levels: a cut descender moves by 100+, anti-aliasing at a face's corners by under 10). Text shadows are
    # off in both: a clip trims their blur
    def whole():
        shot = lambda: cdp.call("Page.captureScreenshot", session=s, format="png", timeout=20)["data"]
        js("document.querySelectorAll('.toast').forEach(t => t.remove()); document.getAnimations().forEach(a => a.pause());"
           "document.head.insertAdjacentHTML('beforeend', '<style id=whole>#swrow .label b { text-shadow: none }</style>')")
        a = shot()
        js("document.getElementById('whole').textContent += '#swrow :is(.wm, .label b) { overflow: visible !important }'")
        b = shot()
        js("document.getElementById('whole').remove(); document.getAnimations().forEach(a => a.play())")
        return js("(async (...pngs) => { const [p, q] = await Promise.all(pngs.map(async u => { const i = new Image(); i.src = "
                  "'data:image/png;base64,' + u; await i.decode(); const c = new OffscreenCanvas(i.width, i.height)"
                  ".getContext('2d'); c.drawImage(i, 0, 0); return c.getImageData(0, 0, i.width, i.height).data; })); "
                  f"return p.every((v, k) => Math.abs(v - q[k]) < 24); }})({json.dumps(a)}, {json.dumps(b)})")

    def api(path, body):                         # a loopback client: no Origin
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        r = json.loads(c.getresponse().read())
        c.close()
        return r

    def siri(fn, *a):                            # siri.py runs its actions on this thread
        th = threading.Thread(target=fn, args=a, name="siri-actions")
        th.start()
        th.join()

    def swipe():                                 # siri.py calls it under the lock, once per touch
        with sh.lock:
            sh.swipe("up", 9000)

    def tv_tv():
        siri(sh.home), time.sleep(0.14), siri(sh.home)
        assert until(lambda: sw() and sw()["keys"][1:] == [a["key"] for a in sh.running_apps()]), sw()

    def leave(*ids):                             # open each, then Home (the demo's "app" is the launch splash)
        for k in ids:
            assert api("/api/launch", {"id": k})["ok"]
            time.sleep(0.3)
            sh.home()
            time.sleep(0.3)

    def key(k, code):
        for typ in ("keyDown", "keyUp"):
            cdp.call("Input.dispatchKeyEvent", session=s, type=typ, key=k, code=k, windowsVirtualKeyCode=code)

    def center(sel):
        b = js(f"(() => {{ const b = document.querySelector({json.dumps(sel)}).getBoundingClientRect(); "
               "return [b.left + b.width / 2, b.top + b.height / 2]; })()")
        return {"x": b[0], "y": b[1]}

    def mouse(typ, at, **kw):
        cdp.call("Input.dispatchMouseEvent", session=s, type=typ, **at, **kw)

    def click(at):
        mouse("mouseMoved", at), mouse("mousePressed", at, button="left", clickCount=1)
        mouse("mouseReleased", at, button="left", clickCount=1)

    def keys_js(*ks):                            # keydowns as the page's own handler takes them, in one evaluation
        return "".join(f"dispatchEvent(new KeyboardEvent('keydown', {{key: '{k}'}}));" for k in ks)

    def dismiss():                               # an open switcher closes on a Home past its grace (no switch)
        time.sleep(0.6)
        sh.emit("home", {})
        assert until(lambda: sw() is None)

    talking = lambda: js("document.getElementById('stage').classList.contains('talk')")

    def frames():                                # rAF intervals over the next ~1 s: p95 (informational)
        js("window.__f = []; (function f(t) { __f.push(t); __f.length < 61 && requestAnimationFrame(f); })(performance.now())")
        return lambda: js("(() => { const d = __f.slice(1).map((t, i) => t - __f[i]).sort((a, b) => a - b); "
                          "return d[Math.floor(d.length * .95)]; })()")

    # 1. TV TV from Netflix: Home, then the switcher, newest first, focused on the app just left
    for k in (RDR2, DOS2):                       # one after another: a game left by Home would keep the demo's snapshot
        assert api("/api/launch", {"id": k})["ok"]   # (a flat colour) over its art in every screenshot
        time.sleep(0.3)
    assert api("/api/launch", {"id": "netflix"})["ok"]
    time.sleep(0.6)
    p95_open = frames()
    tv_tv()
    assert keys() == ["home", "netflix", DOS2, RDR2] and sw()["i"] == 1 and sw()["from"] == "netflix", sw()
    assert js("document.getElementById('swlive').textContent") == "Netflix, Web app, 2 of 4"
    time.sleep(1)
    p95_open = p95_open()
    # ...over an opaque backdrop (an rgb(), no alpha): nothing of the launcher reads through between the cards
    assert js("getComputedStyle(document.getElementById('switcher'), '::before').backgroundColor").startswith("rgb(")
    look("four")
    assert js("[...document.querySelectorAll('#swrow .card .face')].map(f => [getComputedStyle(f.querySelector('.shade'))"
              ".opacity, getComputedStyle(f).outlineStyle])") == [["0.4", "none"], ["0", "solid"], ["0.4", "none"],
                                                                   ["0.4", "none"]], "focus: one ring, the other faces dim"
    assert js("document.getElementById('swhint').innerText") == "Swipe up to quit · OK to open · Back to return"
    # ...and under a state pill, each logo stays whole on its 380 px face too (it ran 30 px past the face's bottom)
    js(f"SH.swRefresh({json.dumps({'apps': [{**c, 'state': 'closing'} for c in sh.running_apps()]})})")
    assert js(UNDER) == [] and js("document.querySelectorAll('#swrow .card.closing .l1 > img').length") == 2, js(UNDER)
    js(f"SH.swRefresh({json.dumps({'apps': sh.running_apps()})})")
    time.sleep(0.4)
    # 2. moves: one card per step, no wrap (a bump); a Siri up-step never arms
    assert sh.navigate("right")["ok"] and until(lambda: sw()["i"] == 2)
    siri(sh.navigate, "up")
    time.sleep(0.2)
    assert sw()["armed"] is None and sw()["i"] == 2, sw()
    sh.navigate("left"), sh.navigate("left")
    assert until(lambda: sw()["i"] == 0)
    assert js("document.getElementById('swhint').innerText") == "OK to open · Back to return", "Home can't be quit"
    time.sleep(0.4)
    sh.navigate("left")                          # the bump is a script animation (focus changes are CSS transitions)
    assert until(lambda: js("document.querySelector('#swrow .focused .lift').getAnimations()"
                            ".some(a => a.constructor === Animation)"), 1), "no bump"
    assert sw()["i"] == 0
    # 3. the D-pad's two-step close: Up arms, OK closes; a tab flies off at once and focus takes the next older card. The
    # Apple IR remote's ring ↑ and centre are these keys (FLIRC types them): the README's controls say so
    quit = next(r for r in (ROOT / "docs" / "MANUAL.md").read_text().splitlines() if r.startswith("| Quit an app (in the switcher)"))
    assert quit.split("|")[2].strip() == "ring ↑, then centre", quit
    sh.navigate("right")
    assert until(lambda: sw()["i"] == 1)
    sh.navigate("up")
    assert until(lambda: sw()["armed"] == "netflix")
    assert js("(c => [c.querySelector('.note').textContent, getComputedStyle(c.querySelector('.face')).outlineColor])"
              "(document.querySelector('#swrow .focused'))") == ["OK to close · Down to cancel", "rgb(255, 69, 58)"]
    look("armed")
    sh.select()
    closed_at = time.monotonic()
    assert until(lambda: "netflix" not in sh.tabs and keys() == ["home", DOS2, RDR2]), (sh.tabs, sw())
    # ...and the daemon hears where focus went: "quit it" now means that card (it kept the one that went)
    assert until(lambda: sh.ui["focused"] == DOS2, 1), sh.ui
    # the fly-off first, from where it was (lifted, at focus scale); the neighbours glide in only once it's gone
    glide = js(f"(async () => {{ const el = document.querySelector('#swrow .tile[data-id=\"{DOS2}\"]'), "
               "x = () => new DOMMatrix(getComputedStyle(el).transform).m41, x0 = x(); "
               "await new Promise(r => setTimeout(r, 120)); const b = document.querySelector('#swrow .bye .lift'); "
               "return [x() - x0, b && new DOMMatrix(getComputedStyle(b).transform).a, b && getComputedStyle(b).translate]; "
               "})()")
    assert glide[0] == 0 and glide[1:] in ([1.12, "0px -30px"], [None, None]), glide
    sh.select()                                  # a double-tapped confirm: the second OK opens nothing (the guard)
    time.sleep(0.4)
    assert sw() and keys() == ["home", DOS2, RDR2] and sh.surface == "launcher", (sw(), sh.surface)
    assert sw()["i"] == 1 and sw()["armed"] is None, sw()
    assert until(lambda: js("!document.querySelector('#swrow .bye')"), 1), "the fly-off didn't finish"
    # 4. the guard: for 1.2 s after a close no close input acts (Up, a swipe)
    sh.navigate("up"), swipe()
    time.sleep(0.25)
    assert sw()["armed"] is None and sw()["states"] == [None, "open", "open"] and not sh._closing, (sw(), sh._closing)
    # 5. a Siri flick up quits the focused card: Quitting… until the demo game exits 1.2 s later
    time.sleep(max(0, closed_at + 1.3 - time.monotonic()))
    swipe()
    t5 = time.monotonic()
    assert until(lambda: sw()["states"][1] == "closing"), sw()
    assert js("document.querySelector('#swrow .focused .st b').textContent") == "Quitting…"
    look("closing")
    # ...said once, on its pill, which sits on no part of its logo (whole on its face, under the pill); its ring stays
    # white over its face at half; the hint no longer offers to quit it
    assert js("(c => [getComputedStyle(c.querySelector('.note')).display, getComputedStyle(c.querySelector('.face'))"
              ".outlineColor])(document.querySelector('#swrow .focused'))") == ["none", "rgb(255, 255, 255)"]
    assert js(UNDER) == [], "the Quitting… pill sits on the logo, or pushes it off its face"
    assert js("document.getElementById('swhint').innerText") == "OK to open · Back to return", "Swipe up to quit, again"
    assert until(lambda: keys() == ["home", RDR2], 3), sw()
    assert sw()["i"] == 1
    # ...its card left by itself (its Quitting… over), focus is on its neighbour under the close guard: a re-flick meant
    # for it, landing just as it went, quits nothing (it used to quit the next app)
    gone = time.monotonic()
    swipe()
    time.sleep(0.25)
    assert sw()["states"] == [None, "open"] and not sh._closing, (sw(), sh._closing)
    # 6. RDR2 (demo) ignores it: closing, stuck after 1 s, a swipe brings the sheet (Cancel first), swipes can't force
    time.sleep(max(0, gone + 1.3 - time.monotonic()))
    swipe()
    t6 = time.monotonic()
    assert until(lambda: sw()["states"][1] == "closing") and until(lambda: sw()["states"][1] == "stuck", 3), sw()
    assert js("document.querySelector('#swrow .focused .st b').textContent") == "Didn’t quit"
    look("stuck")
    assert js(UNDER) == [], "the Didn't quit pill sits on the logo, or pushes it off its face"
    time.sleep(max(0, t6 + 1.3 - time.monotonic()))
    swipe()
    assert until(lambda: sw()["sheet"] == {"key": RDR2, "i": 0, "why": "stuck"}), sw()
    assert js("document.querySelector('#swsheet .pill.on').textContent") == "Cancel"
    assert js("(s => s.textContent === 'Red Dead Redemption 2?' && s.offsetHeight < 60)(document.querySelector('#swst span'))"), \
        "the force sheet splits the app's name"
    look("sheet")
    sh.navigate("right")
    assert until(lambda: sw()["sheet"]["i"] == 1)
    swipe()                                      # ignored: neither a force nor a fresh Cancel-first sheet
    time.sleep(0.3)
    assert sw()["sheet"]["i"] == 1 and sw()["states"][1] == "stuck" and RDR2 in sh.running, sw()
    sh.select()
    assert until(lambda: keys() == ["home"], 3) and RDR2 not in sh.running, sw()
    gone = time.monotonic()
    assert sw()["sheet"] is None and js("document.querySelector('#swrow .focused .note').textContent") == "Nothing else is open"
    look("empty")
    # ...with no hint (nothing to quit; OK and Back both just go back to the launcher), at the size it opens with alone,
    # not the 380 px it had with four
    assert js("getComputedStyle(document.getElementById('swhint')).opacity") == "0", "a lone Home's hint"
    assert js("[...document.querySelectorAll('#swrow .card')].map(c => c.offsetWidth)") == [720], "a lone Home kept its size"
    # OK on Home closes it, and that is activity: the launcher doesn't slide off to its idle Top Shelf right after
    js("document.getElementById('stage').classList.add('idle')")   # (as after 5 s without input)
    time.sleep(max(0, gone + 1.3 - time.monotonic()))   # (past the close guard of its card's leaving: it holds OK too)
    sh.select()
    assert until(lambda: sw() is None) and not js("document.getElementById('stage').classList.contains('idle')")
    # 7. Menu goes back to the app you came from: the card zooms in, then /api/launch
    assert api("/api/launch", {"id": "netflix"})["ok"] and until(lambda: sw() is None)   # the switcher closes over an app
    time.sleep(0.6)
    tv_tv()
    assert keys() == ["home", "netflix"] and sw()["from"] == "netflix"
    look("one")
    sh.back()
    assert until(lambda: sh.surface == "web" and sh.active == "netflix" and sw() is None, 3), (sh.surface, sw())
    # ...a web app waits for its zoom (520 ms) to switch: Home or TV TV during it takes the switch back, so nothing opens
    # under the launcher, or under the switcher just opened (it closed as the abandoned tab came up). A tile, then a card
    sh.home()
    time.sleep(0.6)
    sh.emit("focus", {"id": "netflix"}), sh.select()
    time.sleep(0.15)
    sh.home()
    time.sleep(0.8)
    assert sh.surface == "launcher" and sw() is None, (sh.surface, sw())
    tv_tv()
    assert keys() == ["home", "netflix"] and sw()["i"] == 1, sw()
    time.sleep(0.6)                              # (past the double's grace: the next TV press is a first press)
    sh.select()
    time.sleep(0.15)
    siri(sh.home), time.sleep(0.14), siri(sh.home)
    time.sleep(0.8)
    assert sw() and sh.surface == "launcher", (sw(), sh.surface)
    dismiss()
    # ...and so does opening the switcher any other way (the phone's Apps, the pad's View, voice), every opener's path:
    # a tile's zoom, then a card's (the switcher opened, then closed as the tab came up over it)
    sh.emit("focus", {"id": "netflix"}), sh.select()
    time.sleep(0.15)
    sh.switcher()
    time.sleep(0.8)
    assert sw() and sw()["keys"] == ["home", "netflix"] and sh.surface == "launcher", (sw(), sh.surface)
    sh.select()
    time.sleep(0.15)
    sh.switcher()
    time.sleep(0.8)
    assert sw() and sh.surface == "launcher", (sw(), sh.surface)
    dismiss()
    # ...and a second OK (the Siri clickpad's double click) during a web app card's switch, or just after OK on Home,
    # opens nothing else: never the launcher's focused tile (Cyberpunk, not running, started instead of the switch)
    for card in ("netflix", "home"):
        sh.emit("focus", {"id": "steam:1091500"})
        time.sleep(0.3)
        sh.switcher()
        assert until(lambda: sw() and sw()["keys"] == ["home", "netflix"]), sw()
        if card == "home":
            sh.navigate("left")
        assert until(lambda: sw()["i"] == ["home", "netflix"].index(card)), sw()
        sh.select(), time.sleep(0.2), sh.select()
        time.sleep(1)
        assert (sh.surface, sh.active, "steam:1091500" in sh.running) == (("web", "netflix", False) if card ==
                                                                          "netflix" else ("launcher", None, False)), \
            (card, sh.surface, sh.active, sh.running)
        sh.home()
        assert until(lambda: sw() is None and sh.surface == "launcher")
        time.sleep(0.6)
    # (OK under a launch's splash is ignored now; Settings opened by the daemon there, its focus then OK, still opens:
    # a Steam game still starting keeps the launcher in front, under its splash)
    js("grow(cur(), curEl())")
    assert sh.open("settings")["ok"] and until(lambda: js("SH.f().screen") == "settings"), js("SH.f()")
    sh.back()
    assert until(lambda: js("SH.f().screen") == "home")
    # 8. HOME_GRACE: a late duplicate Home (100 ms) is ignored; a real one (700 ms) closes it
    sh.home()
    time.sleep(0.6)
    sh.switcher()
    assert until(lambda: sw())
    t8 = time.monotonic()
    time.sleep(0.1)
    sh.emit("home", {})
    time.sleep(0.2)
    assert sw(), "a Home 100 ms after opening closed the switcher"
    time.sleep(max(0, t8 + 0.7 - time.monotonic()))
    p95_close = frames()
    sh.emit("home", {})
    assert until(lambda: sw() is None)
    time.sleep(1)
    p95_close = p95_close()
    # 9. the keyboard's Home twice within 300 ms asks for the switcher; 400 ms apart are two Homes
    time.sleep(0.4)
    ag.calls.clear()
    key("Home", 36), time.sleep(0.15), key("Home", 36)
    assert until(lambda: ag.calls == ["app_switcher"]), ag.calls
    assert until(lambda: sw()), "Home Home didn't open the switcher"   # (the agent's app_switcher tool, DESIGN §8.4)
    time.sleep(0.6)
    sh.emit("home", {})
    assert until(lambda: sw() is None)
    time.sleep(0.4)
    ag.calls.clear()
    key("Home", 36), time.sleep(0.4), key("Home", 36)
    time.sleep(0.3)
    assert ag.calls == [] and sw() is None, ag.calls
    # 10. pointer: hover focuses, the focused card's × quits it, a click outside the cards is Menu
    leave(RDR2, DOS2)
    time.sleep(2.1)                              # past `from`: Menu just closes
    sh._snaps = {}                               # (the demo's stand-ins are flat colours, which the TV drops: art, as there)
    sh.switcher()
    assert until(lambda: keys() == ["home", DOS2, RDR2, "netflix"]) and sw()["i"] == 1 and sw()["from"] is None, sw()
    time.sleep(0.5)
    mouse("mouseMoved", center(f'#swrow .tile[data-id="{RDR2}"] .face'))
    assert until(lambda: sw()["i"] == 2), sw()
    mouse("mouseMoved", center(f'#swrow .tile[data-id="{DOS2}"] .face'))
    assert until(lambda: sw()["i"] == 1 and js("getComputedStyle(document.querySelector('#swrow .focused .x')).display")
                 != "none")
    look("pointer")                              # (its × Quit over the card, clear of its face)
    click(center("#swrow .focused .x"))
    assert until(lambda: sw()["states"][1] == "closing") and until(lambda: keys() == ["home", RDR2, "netflix"], 3), sw()
    time.sleep(1.3)                              # (past the close guard of its leaving; flown off, the row settled)
    assert until(lambda: js("!document.querySelector('#swrow .bye') && !document.getAnimations().some(a => "
                            "a.transitionProperty === 'transform' && a.effect?.target?.classList?.contains('card'))"), 2)
    mouse("mouseMoved", center('#swrow .tile[data-id="netflix"] .face'))
    assert until(lambda: sw()["i"] == 2 and js("getComputedStyle(document.querySelector('#swrow .focused .x')).display")
                 != "none"), sw()
    at = center('#swrow .tile[data-id="netflix"] .x')   # a double click on a web app's ×: the second lands on the card
    click(at)                                          # flying off (or where it was): nothing, not Menu
    t10 = time.monotonic()
    assert until(lambda: js("!!document.querySelector('#swrow .tile[data-id=\"netflix\"].bye')"), 1), "no fly-off"
    click(at)
    time.sleep(0.4)
    assert sw() and keys() == ["home", RDR2] and "netflix" not in sh.tabs and sh.surface == "launcher", (sw(), sh.surface)
    time.sleep(max(0, t10 + 1.3 - time.monotonic()))
    click({"x": 960, "y": 150})
    assert until(lambda: sw() is None) and sh.surface == "launcher"
    # 11. a snapshot of the game just left cross-fades in over its card's art
    leave(RDR2)
    sh.switcher()
    assert until(lambda: js(f"!!document.querySelector('#swrow .tile[data-id=\"{RDR2}\"] img.snap.on')")), "no snapshot"
    time.sleep(0.6)                              # past HOME_GRACE
    sh.home()
    assert until(lambda: sw() is None)
    # ...and the opening: by 120 ms, when the cards start to come in, nothing of the launcher reads (its rows, Top Shelf
    # and status chips are gone; the backdrop all but in), frozen there
    time.sleep(1)
    sh.switcher()
    assert until(lambda: sw())
    o = js("(() => { document.getAnimations().forEach(a => { a.pause(); a.currentTime = 120; }); return ['page', 'shelf', "
           "'chips', 'switcher'].map(id => +getComputedStyle(document.getElementById(id)).opacity); })()")
    js("document.getAnimations().forEach(a => a.play())")
    assert max(o[:3]) < .02 and o[3] > .75, o
    dismiss()
    # 12. reduced motion: nothing in the switcher still animates 50 ms after it opens
    cdp.call("Emulation.setEmulatedMedia", session=s, features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
    time.sleep(0.3)
    sh.switcher()
    assert until(lambda: sw())
    time.sleep(0.05)
    moving = js(MOVING)
    assert moving == [], moving
    dismiss()                                    # ...nor a closing card's loader, nor the force sheet's pills
    js(f"SH.swOpen({json.dumps(FIX)});" + keys_js("Delete", "Enter", "ArrowRight"))   # (RDR2: the sheet, Force Quit)
    time.sleep(0.05)
    moving = js(MOVING)
    assert sw()["sheet"] == {"key": RDR2, "i": 1, "why": "unaskable"} and moving == [], (sw(), moving)
    js(keys_js("Escape"))
    dismiss()
    cdp.call("Emulation.setEmulatedMedia", session=s, features=[])
    # 13. six cards: the one peeking past the five shows neither its label nor its state pill (the stage edge would cut
    # them); the focused card's detail line is never cut: the stuck one wraps in two lines at most (not at half the card:
    # a short title's label went to 150 px, 4-5 lines), before "or", never after it; the armed one not at all; armed, the
    # hint goes (it says OK opens, and OK now quits)
    fits = ("const fits = () => { const n = document.querySelector('#swrow .focused .note'), t = n.textContent, r = "
            "document.createRange(), y = i => (r.setStart(n.firstChild, i), r.setEnd(n.firstChild, i + 1), r."
            "getBoundingClientRect().top); return n.scrollWidth <= n.clientWidth && n.scrollHeight <= n.clientHeight && "
            "[t.replaceAll('\\u00a0', ' '), Math.round((n.offsetHeight - 2) / 32), t.includes(' or') && y(t.indexOf(' or') "
            "+ 1) > y(0)]; };")
    r = js(f"(() => {{ SH.swOpen({json.dumps(FIX)}); {fits} const peek = ['.label', '.st'].map(s => getComputedStyle("
           "document.querySelector(`#swrow .tile[data-id=\"x:steam\"] ${s}`)).opacity);" +
           keys_js("ArrowRight", "ArrowRight") +
           "const stuck = fits();" + keys_js("ArrowLeft", "Delete") +
           "const h = document.getElementById('swhint'); h.getAnimations().forEach(a => a.finish()); "
           "return [peek, stuck, fits(), getComputedStyle(h).opacity]; })()")
    assert [r[0], r[1], r[2][:2], r[3]] == [["0", "0"], ["Open it to see why, or quit again to force quit", 2, True],
                                            ["OK to close · Down to cancel", 1], "0"], r
    look("fixture")
    assert whole(), "a wordmark or title is clipped (Redemption's p, Original's g)"
    one = {"open": True, "from": DOS2, "apps": [c for c in FIX["apps"] if c["key"] == DOS2]}   # 720 px: one line
    assert js(f"(() => {{ SH.swOpen({json.dumps(one)}); {fits} return fits(); }})()") == \
        ["Open it to see why, or quit again to force quit", 1, False]
    # a card that can't be force quit (Big Picture, Steam, a window) never promises it: its stuck line, the live region
    r = js(f"(() => {{ const F = {json.dumps(FIX)}; SH.swOpen(F); SH.swRefresh({{apps: F.apps.map(c => c.key === "
           "'x:steam' ? {...c, state: 'stuck'} : c)}); const live = document.getElementById('swlive').textContent;" +
           keys_js(*["ArrowRight"] * 4) + "return [live, document.getElementById('swlive').textContent]; })()")
    assert r == ["Steam didn’t quit", "Steam, Open it to see why, 6 of 6"], r
    dismiss()
    # the force sheet's Cancel holds the close guard too: a double-tapped OK on it opens nothing. (RDR2, a Wine desktop:
    # straight to the sheet, which says why with its window on screen: not "no window", but its own menu saves first)
    r = js(f"(() => {{ SH.swOpen({json.dumps(FIX)});" + keys_js("Delete", "Enter") + "const s = SH.sw().sheet, "
           "t = document.getElementById('swsb').textContent;" + keys_js("Enter", "Enter") + "return [s, t, SH.sw()]; })()")
    assert r[:2] == [{"key": RDR2, "i": 0, "why": "unaskable"}, "It can’t be asked to quit safely: quit it from its own "
                     "menu to save first. Anything unsaved will be lost."] and (r[2]["sheet"], r[2]["i"]) == (None, 1), r
    dismiss()
    # ...and so does its card leaving by itself while the sheet is up: an OK meant for the sheet (or the flick meant for
    # the card) never switches into, or quits, the card that took its place
    r = js(f"(() => {{ const F = {json.dumps(FIX)}; SH.swOpen(F);" + keys_js("Delete", "Enter") + "const s = SH.sw()"
           f".sheet; SH.swRefresh({{apps: F.apps.filter(c => c.key !== '{RDR2}')}});" + keys_js("Delete") +
           "const armed = SH.sw().armed;" + keys_js("Enter") + "return [s, armed, SH.sw()]; })()")
    assert r[0]["key"] == RDR2 and r[1] is None and r[2] and (r[2]["keys"][1], r[2]["i"], r[2]["sheet"]) == \
        ("netflix", 1, None), r
    dismiss()
    # ...and a click outside it: a double click's second half is no Menu (back into `from`, the app the sheet was about)
    js(f"SH.swOpen({json.dumps(FIX)});" + keys_js("Delete", "Enter"))
    assert sw()["sheet"]["key"] == RDR2 == sw()["from"], sw()
    mouse("mouseMoved", {"x": 300, "y": 120})    # (the pointer moved since the keys did: a click is a click, not OK)
    click({"x": 960, "y": 150}), click({"x": 960, "y": 150})
    time.sleep(0.1)
    assert sw() and (sw()["sheet"], sw()["i"]) == (None, 1), sw()
    dismiss()
    # ...and Back pressed twice (the phone's double tap, a remote's bounce), or held (the keyboard's or FLIRC's repeat):
    # the sheet goes, and the switcher stays (no Menu into `from`, the app the sheet was about)
    js(f"SH.swOpen({json.dumps(FIX)});" + keys_js("Delete", "Enter"))
    assert sw()["sheet"]["key"] == RDR2 == sw()["from"], sw()
    js(keys_js("Escape", "Escape"))
    time.sleep(0.6)
    js("dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', repeat: true}))")
    time.sleep(0.1)
    assert sw() and (sw()["sheet"], sw()["i"]) == (None, 1), sw()
    dismiss()
    # /api/close answered here (page only): what the page sends, and what it shows of the reply
    def answered(fixture, reply, *ks, then=""):
        return js(f"(async () => {{ const sent = [], f = window.fetch, F = {json.dumps(fixture)}; SH.swOpen(F); window."
                  "fetch = async (u, o) => u !== '/api/close' ? f(u, o) : (sent.push(JSON.parse(o.body)), " + then +
                  f"new Response({json.dumps(json.dumps(reply))}));" + keys_js(*ks) + "await new Promise(r => "
                  "setTimeout(r, 100)); window.fetch = f; return [sent, SH.sw().keys, SH.sw().states]; })()")
    # a Starting… card with nothing started to signal (no force quit) is asked too: the daemon ends that launch (the
    # page said "is still starting." itself, and the card stayed the 5 min a Steam launch waits)
    r = answered({**FIX, "apps": [{**FIX["apps"][0], "state": "starting", "windows": 0, "can_force": False}]},
                 {"ok": True, "state": "closed", "title": "Red Dead Redemption 2"}, "Delete", "Enter")
    assert r[:2] == [[{"key": RDR2}], ["home"]], r
    dismiss()
    # a force quit that fails at once: the daemon's refresh (its card back to stuck) can come before the reply, which
    # then mustn't paint it Quitting… (no refresh follows: it stayed so, and couldn't be quit, until the switcher closed)
    r = answered(FIX, {"ok": True, "state": "forcing", "title": "Divinity: Original Sin 2"}, "ArrowRight", "ArrowRight",
                 "Delete", "Enter", "ArrowRight", "Enter", then="SH.swRefresh({apps: JSON.parse(JSON.stringify(F.apps))}), ")
    assert r[0] == [{"key": DOS2, "force": True}] and r[2][3] == "stuck", r
    dismiss()
    # a card stuck once it took the quit (quitting: its window, or its last one, went) is still exiting, its launcher
    # maybe still open: "Still quitting", never "Open it to see why" nor "didn't quit when asked"
    for n in (0, 1):
        f6 = {**FIX, "apps": [{**c, "state": "closing", "windows": n, "quitting": True} if c["key"] == DOS2 else c
                              for c in FIX["apps"]]}
        r = js(f"(() => {{ const F = {json.dumps(f6)}; SH.swOpen(F); SH.swRefresh({{apps: F.apps.map(c => c.key === "
               f"'{DOS2}' ? {{...c, state: 'stuck'}} : c)}}); const live = document.getElementById('swlive').textContent;"
               + keys_js("ArrowRight", "ArrowRight") + "const el = document.querySelector('#swrow .focused'); const r = "
               "[live, el.querySelector('.st b').textContent, el.querySelector('.note').textContent];" +
               keys_js("Delete", "Enter") + "return [...r, SH.sw().sheet, document.getElementById('swsb').textContent]; "
               "})()")
        assert r == ["Divinity: Original Sin 2 is still quitting; force quit is available", "Still quitting",
                     "Quit again to force quit", {"key": DOS2, "i": 0, "why": "quitting"},
                     "It’s still quitting. Anything unsaved will be lost."], (n, r)
        js(keys_js("Escape"))
        dismiss()
    # the last apps leaving at once (Steam exiting, force quits): a lone Home re-fits to 720 px, and the cards flying off
    # keep their size (they ballooned to 720 px, over Home and past the stage's edge)
    assert js(f"(() => {{ SH.swOpen({json.dumps(FIX)}); SH.swRefresh({{apps: []}}); return [...document."
              "querySelectorAll('#swrow .bye')].map(c => c.offsetWidth); })()") == [300] * 5
    dismiss()
    # a long title on a card with a state pill (a stuck window's, a starting Big Picture's) at 300 px: its wordmark stays
    # on its face, under the pill
    long = {"open": True, "from": None, "apps": [{**FIX["apps"][2], "key": "w:1", "kind": "window", "can_force": False,
            "title": "Rockstar Games Launcher - Red Dead Redemption 2 Setup"}, {**FIX["apps"][3], "state": "starting"},
            {**FIX["apps"][2], "state": "starting"}, FIX["apps"][1]]}
    js(f"SH.swOpen({json.dumps(long)})")
    look("long")
    # ...whole there, sized to the two lines a pill leaves it (clamped, its last line's ellipsis sat off-centre, and cut a
    # word in Inter), and whole under it while it has focus (up to four lines: three cut "2 Setup" in Inter), and while
    # it hasn't (two ended in an ellipsis 4-13 px off-centre: no CSS clamp in Chrome centres that line)
    assert js(CUT) == [], js(CUT)
    assert js(keys_js("ArrowRight") + "SH.sw().i") == 2 and js(CUT) == [], js(CUT)
    js(keys_js("ArrowLeft"))
    assert len(keys()) == 5 and js("[...document.querySelectorAll('#swrow .face')].every(f => { const a = f."
                                   "getBoundingClientRect(), b = (f.querySelector('.wm') ?? f).getBoundingClientRect(); "
                                   "return b.top >= a.top && b.bottom <= a.bottom; })"), sw()
    # the focused card's ring (x1.12, its outline too) stays 20 px or more clear of its neighbours at 2, 3 and 4 cards
    # (it all but touched them at 3 and 4: 6-9 px)
    clear = []
    for n in (2, 3, 4):
        js(f"SH.swOpen({json.dumps({**FIX, 'from': None, 'apps': FIX['apps'][:n - 1]})})")
        time.sleep(0.6)
        clear.append(js("(() => { const els = [...document.querySelectorAll('#swrow .card')], i = els.findIndex(e => e."
                        "classList.contains('focused')), f = els[i].querySelector('.face'), b = f.getBoundingClientRect(), "
                        "ring = 8 * b.width / f.offsetWidth, r = j => els[j]?.querySelector('.face').getBoundingClientRect()"
                        "; return Math.round(Math.min(b.left - ring - r(i - 1).right, r(i + 1) ? r(i + 1).left - b.right - "
                        "ring : 1e9)); })()"))
    assert min(clear) >= 20, clear
    dismiss()
    # 14. "show me the apps": the reply ("Showing 2 open apps.") doesn't sit over the switcher for 5 s; nor does a reply
    # still up when TV TV opens it
    assert api("/api/voice", {"text": "show me the apps"})["ok"] and until(lambda: sw())
    time.sleep(0.3)
    assert not talking(), "the voice reply covers the switcher's hint"
    dismiss()
    sh.emit("voice", {"phase": "done", "reply": "Volume 40%."})
    assert until(talking, 1)
    sh.switcher()
    assert until(lambda: sw()) and until(lambda: not talking(), 1), "a finished reply stays over the switcher"
    # ...and Back on a request it hears (Thinking…) cancels that request (README), and only that: it went Menu, into
    # the app the switcher came from, and the request still ran
    sh.emit("voice", {"phase": "thinking", "transcript": "quit youtube"})
    assert until(talking, 1)
    ag.voice.clear()
    js(keys_js("Escape"))
    assert until(lambda: ag.voice == ["cancel"], 1) and not talking() and sw() and sh.surface == "launcher", \
        (ag.voice, sw(), sh.surface)
    dismiss()
    # ...toasts: only the newest shows in the switcher, up in the status chips' row once they're gone, and back down
    # before they return (it sat over them, text over text, for 0.1 s); it glides (it jumped 60 px in a frame), and the
    # older ones it hid stay gone (they faded back in on the launcher). Frozen every 20 ms of each transition
    over = lambda: js("(() => { const c = document.getElementById('chips'), t = document.querySelector('.toast'), r = [...Array"
                      "(25).keys()].map(k => 20 * k).filter(at => { document.getAnimations().forEach(a => { a.pause(); a."
                      "currentTime = at; }); const p = t.getBoundingClientRect(), q = c.getBoundingClientRect(); return "
                      "+getComputedStyle(c).opacity > .05 && p.top < q.bottom && q.top < p.bottom && p.left < q.right && "
                      "q.left < p.right; }); document.getAnimations().forEach(a => a.effect.getComputedTiming().endTime < "
                      "1e9 ? a.finish() : a.play()); return r; })()")
    sh.emit("toast", {"text": "Closed Netflix", "kind": "info"}), sh.emit("toast", {"text": "Asking DOS2 to quit…"})
    time.sleep(0.7)
    sh.switcher()
    assert until(lambda: sw()) and over() == [], "a toast over the status chips as the switcher opens"
    time.sleep(0.6)                              # (past HOME_GRACE)
    y = js("(t => { const y = t.getBoundingClientRect().top; dispatchEvent(new KeyboardEvent('keydown', {key: 'Home'}));"
           " return [y, t.getBoundingClientRect().top, !!SH.sw()]; })(document.querySelector('.toast'))")
    assert over() == [], "a toast over the status chips as the switcher closes"
    time.sleep(0.6)
    shown = js("[...document.querySelectorAll('.toast')].filter(t => +getComputedStyle(t).opacity > .5).map(t => "
               "t.textContent)")
    assert y[0] == y[1] and not y[2] and shown == ["Asking DOS2 to quit…"], (y, shown)
    # 15. a reply over a switcher voice opened (and no toast saying it again), many (five fit, a sixth peeks) and paged
    # (nine, focus on the 7th: a whole page on, a peek each side), none with two parts overlapping, every title whole
    # (two lines, never cut mid-word). Apps open one after another (as in step 1), and RDR2's snapshot from step 11 goes
    # with it
    sh.close_app(RDR2, force=True, person=True)
    assert until(lambda: RDR2 not in sh.running, 3)
    assert api("/api/launch", {"id": "netflix"})["ok"] and api("/api/voice", {"text": "show me the apps"})["ok"]
    assert until(lambda: keys() == ["home", "netflix"]) and api("/api/voice", {"text": "quit netflix"})["ok"]
    assert until(lambda: keys() == ["home"] and js("document.getElementById('vreply').textContent") == "Closed Netflix.")
    assert js("![...document.querySelectorAll('.toast')].some(t => t.textContent.startsWith('Closed Netflix'))"), \
        "a toast repeats the reply"
    look("voice")
    # ...and a refused quit's reply, its only word, whole in two lines (cut to one, it lost "or force quit it"), with no
    # "Couldn't do that" line raising the panel into the label above
    sh.emit("voice", {"phase": "thinking", "transcript": "quit it"})
    sh.emit("voice", {"phase": "error", "error": "Red Dead Redemption 2 can’t be asked to quit safely: quit it from its "
                                                 "own menu, or force quit it."})
    assert until(lambda: js("document.getElementById('voice').dataset.phase") == "error", 1)
    assert js("(r => [r.scrollWidth <= r.clientWidth && r.scrollHeight <= r.clientHeight, Math.round(r.offsetHeight / "
              "36)])(document.getElementById('vreply'))") == [True, 2], "a refused quit's reply is cut"
    look("refused")
    sh.emit("voice", {"phase": "idle"})
    dismiss()
    peek = lambda j: js(f"document.querySelectorAll('#swrow .card')[{j}].classList.contains('peek')")
    for ids, name in (((DOS2, "steam:1091500", "steam:292030", "youtube", "netflix"), "many"),
                      ((RDR2, "steam:391220", "disney"), "paged")):
        for k in ids:
            assert api("/api/launch", {"id": k})["ok"]
            time.sleep(0.25)
        time.sleep(0.4)
        tv_tv()
        if name == "many":
            assert len(keys()) == 6 and peek(5) and not peek(4), sw()
        else:
            for _ in range(5):
                sh.navigate("right")
            assert until(lambda: sw()["i"] == 6) and peek(1) and peek(7) and not peek(2) and not peek(6), sw()
        assert js("[...document.querySelectorAll('#swrow .card:not(.peek) .label b')].every(b => b.scrollHeight <= "
                  "b.clientHeight)"), "a title is cut"
        look(name)
        assert whole(), "a wordmark or title is clipped"
        if name == "many":                       # ...each under a state pill: every logo under it, whole on its face
            st = ("stuck", "closing", "starting")
            js(f"SH.swRefresh({json.dumps({'apps': [{**c, 'state': st[j % 3]} for j, c in enumerate(sh.running_apps())]})})")
            look("pills")
            assert js(UNDER) == [] and js("document.querySelectorAll('#swrow .card .l1 > img').length") >= 3, js(UNDER)
        dismiss()
    print(f"frame p95: open {p95_open:.1f} ms, close {p95_close:.1f} ms (informational: headless on a Mac, not the Spark)")


def launch_check(sh, cdp, s, shots):
    """A game's launch on the demo (LAUNCH-DESIGN.md §10.2): "Optimized for Spark" on a profiled game's tile (a chip
    where the Running pill shares the face) and on its Top Shelf, never on another's; OK grows its splash with the
    profile's summary, then the daemon's stages (Starting…, Loading…, Playing); a resume opens up, without it, unless
    the stages say its launch still holds; a launch the page didn't start grows it (over the switcher too), failed
    shrinks it; a warn toast is amber; no two parts of the splash overlap."""
    def js(expr):
        r = cdp.call("Runtime.evaluate", session=s, expression=expr, returnByValue=True, awaitPromise=True)
        assert "exceptionDetails" not in r, r["exceptionDetails"]
        return r["result"].get("value")

    def enter():
        for typ in ("keyDown", "keyUp"):
            cdp.call("Input.dispatchKeyEvent", session=s, type=typ, key="Enter", code="Enter", windowsVirtualKeyCode=13)

    def shot(name):
        if shots:
            png = cdp.call("Page.captureScreenshot", session=s, format="png", timeout=20)["data"]
            (Path(shots) / f"{name}.png").write_bytes(base64.b64decode(png))
    launch = lambda: js("SH.launch()")
    box = "(e => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; })"
    cross = lambda a, b: a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
    GR, CP, RDR = "steam:4078430", "steam:1091500", "steam:1174180"
    sh.open(CP), sh.home()                       # (Cyberpunk running: its poster's badge is a chip)
    assert until(lambda: js(f"!!document.querySelector('.tile.posters.running[data-id=\"{CP}\"]')")) and \
        until(lambda: launch() is None and js("SH.sw()") is None)
    assert js(f"!!document.querySelector('.row.dock .tile[data-id=\"{CP}\"] .face .opt')"), "the Dock's Cyberpunk: no badge"
    assert not js(f"document.querySelector('.tile[data-id=\"{RDR}\"] .face .opt')"), "RDR2 has no GameSpark profile"
    poster = lambda k, sel: js(f"{box}(document.querySelector('.tile.posters[data-id=\"{k}\"] {sel}'))")
    face, opt = poster(GR, ".face"), poster(GR, ".face .opt")   # (not running: the words, inside its face)
    assert opt[2] - opt[0] > 150 and face[0] < opt[0] and opt[2] <= face[2] - 8, (face, opt)
    opt, run = poster(CP, ".face .opt"), poster(CP, ".face .run")   # (running: a chip beside the Running pill)
    assert opt[2] - opt[0] < 50 and not cross(opt, run), (opt, run)
    js(f"focus(0, rows[0].tiles.findIndex(t => t.id === '{CP}'))")   # its Top Shelf says it too
    assert until(lambda: js(f"document.querySelector('#shelf .pane.on[data-id=\"{CP}\"] .badge.opt')?.textContent")
                 == "Optimized for Spark")
    js(f"focusId('{GR}')")                       # OK on a profiled game: its summary on the splash, then its stages
    enter()
    want = {"id": GR, "msg": "Starting STAR WARS: Galactic Racer…", "up": False, "sum": "Optimized for Spark · Nanite "
            "off (no flicker), fast cores only (no hitches), bigger audio buffer, patched FEX for Denuvo"}
    assert until(lambda: launch() == want, 2), launch()
    assert until(lambda: launch()["msg"] == "Loading STAR WARS: Galactic Racer…", 3), launch()
    assert not js("[...document.querySelectorAll('.toast')].some(t => t.textContent.startsWith('Starting'))"), \
        "a Starting… toast over the splash, which says it"
    time.sleep(0.6)                              # (the zoom done)
    parts = js("[...document.querySelectorAll('#launch .who > *')].map(e => [e.className, " + box + "(e)])")
    assert len(parts) == 3 and not any(cross(a[1], b[1]) for i, a in enumerate(parts) for b in parts[i + 1:]), parts
    shot("launch-splash")
    assert until(lambda: launch() == {**want, "msg": "Playing · Home comes back here", "up": True}, 4), launch()
    sh.home()
    assert until(lambda: launch() is None)
    js(f"focusId('{GR}')")                       # running: a resume, up at once, no summary
    enter()
    assert until(lambda: launch() == {"id": GR, "msg": "Playing · Home comes back here", "up": True, "sum": None}, 2)
    for stage in ("starting", "loading"):        # ...unless its launch still holds (the Spark: Steam's log lists it as
        sh.emit("launch", {"id": GR, "stage": stage})   # running while it loads): its stages, then its launch look
    assert until(lambda: launch() == {**want, "msg": "Loading STAR WARS: Galactic Racer…"}, 2), launch()
    sh.home()
    assert until(lambda: launch() is None)
    js(f"focusId('{GR}')")                       # up, and its game quits ("quit Galactic Racer"; on the Spark, a game
    enter()                                      # opened elsewhere, whose splash grew from its tile): the splash goes
    assert until(lambda: (launch() or {}).get("up"), 2), launch()   # with it (the kiosk was never hidden, so no
    assert sh.close_app(GR, person=True)["ok"]   # visibilitychange), so OK opens tiles again
    assert until(lambda: launch() is None and GR not in sh.running, 4), launch()
    sh.switcher()                                # started elsewhere (voice, the phone), over the switcher too: grown
    assert until(lambda: js("SH.sw()") is not None)   # here, the switcher closed as on Home; failed: shrunk
    sh.emit("launch", {"id": "steam:292030", "stage": "starting"})
    assert until(lambda: (launch() or {}).get("id") == "steam:292030") and js("SH.sw()") is None, js("SH.sw()")
    sh.emit("launch", {"id": "steam:292030", "stage": "failed"})
    assert until(lambda: launch() is None)
    # "quit it" on a loading screen names its game only while the page shows that screen, as the page says (/api/ui):
    # a Back the daemon never hears takes it down too (the IR remote's Menu, which FLIRC types as Escape; the pad's B)
    js(f"focusId('{GR}')")
    enter()                                      # (Loading…: the demo's state event before it, surface "app", is in)
    assert until(lambda: (launch() or {}).get("msg") == "Loading STAR WARS: Galactic Racer…"), launch()
    sh.pending[GR] = e = {"until": 9e9, "tries": 0, "at": 1.0}   # (as on the Spark: its launch holds, the launcher
    sh._splash, sh.surface = (GR, e), "launcher"                 # in front, pinned over it)
    assert until(lambda: sh.front_key() == GR), sh.ui
    for typ in ("keyDown", "keyUp"):
        cdp.call("Input.dispatchKeyEvent", session=s, type=typ, key="Escape", code="Escape", windowsVirtualKeyCode=27)
    assert until(lambda: launch() is None and sh.front_key() is None), (launch(), sh.ui)
    del sh.pending[GR]
    sh.emit("toast", {"text": "STAR WARS: Galactic Racer started without its GameSpark config", "kind": "warn"})
    assert until(lambda: js("!!document.querySelector('.toast.warn.on')"))
    js(f"focus(0, rows[0].tiles.findIndex(t => t.id === '{CP}'))")
    time.sleep(1)
    shot("badge")


def main():
    chrome, shots = sys.argv[1], (sys.argv[2:] or [None])[0]
    try:
        socket.create_connection(("127.0.0.1", 9223), 0.3).close()
        kiosk = True
    except OSError:
        kiosk = False
    if kiosk or os.environ.get("SPARKHOME_NO_CHROME"):
        return print("switcher check skipped: " + ("the TV's kiosk Chrome is up here" if kiosk else "SPARKHOME_NO_CHROME"))
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
    logging.disable(logging.CRITICAL)
    tmp = Path(tempfile.mkdtemp(prefix="sparkhome-switcher-"))
    port, cport = free_port(), free_port()
    p = subprocess.Popen([chrome, "--headless=new", f"--remote-debugging-port={cport}", f"--user-data-dir={tmp / 'chrome'}",
                          "--password-store=basic",       # as the kiosk: no keyring prompt (see skin_check.py)
                          "--window-size=1920,1080", "--no-first-run", "--mute-audio", "about:blank"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    srv = None
    try:
        cfg = shell_mod.load_config(ROOT / "sparkhome.json")
        cfg["settings"].update(port=port, lan=False, request_log=False, mcp_servers=[], switcher={"stuck_s": 1})
        hub = server.Hub()
        sh = shell_mod.Shell(cfg, True, hub.emit)
        agent = server.load_agent(sh, hub.emit)
        ag, real, real_voice = SimpleNamespace(calls=[], voice=[]), agent.call, agent.voice
        agent.call = lambda name, args=None, **kw: (ag.calls.append(name), real(name, args, **kw))[1]   # what was asked
        agent.voice = lambda action: (ag.voice.append(action), real_voice(action))[1]   # (push-to-talk, cancel)
        app = SimpleNamespace(shell=sh, agent=agent, hub=hub, pairing=server.Pairing(tmp / "tokens.json"), lan=False,
                              port=port, lan_url=None, hosts={"127.0.0.1", "localhost"})
        srv = server.make_server(app, "127.0.0.1", port)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        sh.start()
        cdp = CDP(cport).start()
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
        check(sh, port, cdp, s, ag, shots)
        print("switcher check passed")
        launch_check(sh, cdp, s, shots)
        print("launch check passed")
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
