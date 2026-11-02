"""The TV skin and the page tools in a real headless Chrome, on tests/fixtures/tv.html, through Shell's own injection
and key path (as on the TV: --enable-spatial-navigation is on, so the skin must out-vote it); and sleep's warning over a
page (WARN_JS).

    python3 tests/skin_check.py CHROME [SHOTS_DIR]      # SHOTS_DIR: keep screenshots there

It starts no Chrome beside the TV's kiosk (its DevTools port, 9223, answers: the Spark) or when SPARKHOME_NO_CHROME is
set: a second Chrome there competes with the TV and a game, and once put a keyring prompt on the TV.
"""
import base64
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sparkhome.cdp import CDP  # noqa: E402
from sparkhome.shell import Shell, WARN_JS  # noqa: E402

URL = (ROOT / "tests" / "fixtures" / "tv.html").as_uri()


def until(fn, t=5.0):
    end = time.monotonic() + t
    while not fn() and time.monotonic() < end:
        time.sleep(0.05)
    return fn()


def check(sh, shots):
    cdp = sh.cdp
    errors = []                                  # an uncaught exception on any page fails the check
    cdp.on_event = lambda m: m.get("method") == "Runtime.exceptionThrown" and errors.append(
        (m["params"]["exceptionDetails"].get("exception") or {}).get("description") or m["params"]["exceptionDetails"])

    def open_tile(tid, url):
        target = cdp.call("Target.createTarget", url="about:blank")["targetId"]
        s = sh._attach(sh._tiles[tid], target)
        cdp.call("Emulation.setFocusEmulationEnabled", session=s, enabled=True)   # headless: focus as in a window
        cdp.call("Page.navigate", session=s, url=url)
        assert until(lambda: sh._eval(s, "document.readyState") == "complete"), url
        sh.tabs[tid], sh.active_tab, sh.surface = {"target": target, "session": s}, tid, "web"
        return s

    def shot(name):
        if shots:
            png = cdp.call("Page.captureScreenshot", session=sh._session(), format="png")["data"]
            (Path(shots) / f"fixture-{name}.png").write_bytes(base64.b64decode(png))

    s = open_tile("fx", URL)
    js = lambda expr: sh._eval(s, expr)
    focus = lambda: js("document.activeElement.id")
    log = lambda: js("document.getElementById('log').textContent")
    mouse = lambda kind, x, y, **kw: cdp.call("Input.dispatchMouseEvent", session=s, type=kind, x=x, y=y, **kw)

    def go(d, n=1, want=None):
        assert sh.navigate(d, n)["ok"]
        if want is not None:                     # an id, or "c*": any tile of that row (where depends on scrolling)
            assert focus() == want or want.endswith("*") and focus().startswith(want[:-1]), (d, n, focus(), want)
    assert js("getComputedStyle(document.getElementById('onetrust-consent-sdk')).display") == "none"   # generic
    assert js("getComputedStyle(document.querySelector('footer')).display") == "none"                 # skin.hide
    go("down", want="play")                       # the first press shows where focus is: in the content, not the menu
    assert js("document.documentElement.classList.contains('sh-kbd')")
    go("up", want="home")
    go("right", want="movies"), go("right", want="series"), go("right", want="q")   # one tile per press
    assert sh.type_text("dark")["ok"]
    go("left", want="q"), go("left", 2, want="q")  # the caret moves, not the focus
    assert js("document.activeElement.selectionStart") == 1
    go("down")
    assert focus() in ("play", "info"), focus()   # Down leaves the search box
    js("document.getElementById('info').focus()")
    go("down", want="t0"), go("right", 7, want="t7")
    assert js("document.getElementById('r1').scrollLeft") > 0                         # the row scrolled
    go("right", 9, want="t11")                    # the row's end: no jump to another row
    time.sleep(0.6)                               # (smooth scroll)
    ring, tile = js("JSON.stringify(document.getElementById('sh-ring').getBoundingClientRect())"), \
        js("JSON.stringify(document.getElementById('t11').getBoundingClientRect())")
    ring, tile = json.loads(ring), json.loads(tile)
    assert abs(ring["left"] + 3 - tile["left"]) < 2 and abs(ring["width"] - 6 - tile["width"]) < 2, (ring, tile)
    assert js("getComputedStyle(document.getElementById('sh-ring')).display") == "block"
    assert js("getComputedStyle(document.getElementById('t3')).pointerEvents") == "none"   # no hover previews
    shot("row")
    go("down", want="o*")                         # the tile below, by position
    o = focus()
    assert sh.select()["ok"] and f"click {o}" in log()                                   # role=button: Enter clicks
    go("up", want="t11")
    assert sh.select()["ok"] and until(lambda: log().count("hash #title-11") == 1)      # a link: once, natively
    go("down", 2, want="c*")                      # "tiles": a card only a script made clickable
    c = focus()
    assert sh.select()["ok"] and f"click {c}" in log()
    go("down", want="l*")
    i = int(focus()[1])
    js("rerender()")                              # the SPA re-renders the focused row: focus goes to its twin
    assert until(lambda: focus() == f"l{i}-2"), focus()
    go("left", want=f"l{i - 1}-2")
    assert log().count("page key Arrow") == 3, log()   # the page saw only the search box's caret keys
    # a trailer still playing far up the page is no player: at the page's end the remote keeps the arrows
    js("document.body.prepend(video()); document.activeElement.scrollIntoView({block: 'center'})")
    assert until(lambda: js("document.querySelector('video').currentTime > 0"))
    go("right", want=f"l{i}-2")
    assert log().count("page key Arrow") == 3, log()
    js("document.querySelector('video').remove()")
    # the trackpad scrolls to the top: the remote's next press starts from what's on screen, not the row it left...
    mouse("mouseMoved", 10, 10), mouse("mouseMoved", 60, 40), mouse("mouseWheel", 60, 40, deltaX=0, deltaY=-5000)
    assert until(lambda: js("scrollY") == 0), js("scrollY")
    go("down", want="play")
    r = json.loads(js("JSON.stringify(document.getElementById('movies').getBoundingClientRect())"))
    x, y = r["left"] + r["width"] / 2, r["top"] + r["height"] / 2
    mouse("mouseMoved", x - 5, y), mouse("mouseMoved", x, y)               # ...or from the tile it tapped
    mouse("mousePressed", x, y, button="left", clickCount=1), mouse("mouseReleased", x, y, button="left", clickCount=1)
    go("right", want="movies"), go("right", want="series")
    js("document.getElementById('info').focus()")
    assert sh.select()["ok"] and focus() == "ep1"                                        # the modal opened
    go("right", want="ep2"), go("right", want="close"), go("right", want="close"), go("down", want="close")
    shot("modal")
    assert sh.select()["ok"] and js("document.getElementById('modal').hidden")
    go("up")
    assert focus() not in ("close", "") and js("document.activeElement.checkVisibility()"), focus()
    # a native modal <dialog> paints over everything else (the top layer): the ring is drawn in it
    js("document.getElementById('dlg').showModal()")
    go("right", want="d2")
    assert until(lambda: js("document.getElementById('sh-ring').parentElement.id") == "dlg")
    shot("dialog")
    js("document.getElementById('dlg').close()")
    mouse("mouseMoved", 10, 10), mouse("mouseMoved", 60, 40)
    assert not js("document.documentElement.classList.contains('sh-kbd')")             # the trackpad: pointer mode
    # page tools: titles from alt text and aria-labels (a number too), not the menus, footer or hidden banner
    r = sh.list_titles()
    assert r["ok"] and "Stranger Things" in r["titles"] and "1923" in r["titles"], r
    assert "Original Dark" in r["titles"] and not {"Home", "Movies", "Help Center", "Accept all", "Play"} & set(r["titles"]), r
    assert sh.open_title("The Witcher") == {"ok": True, "opened": "The Witcher"}
    assert until(lambda: "hash #title-8" in log()) and focus() == "t8"
    assert sh.open_title("Nope")["ok"] is False
    # OK on Play opens a player over the page (taller than the screen). As it plays the ring goes, and so does the focus
    # the skin gave Play: every key is the player's (Right seeks; OK isn't Play under it pressed again)
    js("document.getElementById('play').focus()")
    go("down", want="play")                       # (a first press: it shows where focus is)
    assert sh.select()["ok"] and until(lambda: not js("document.documentElement.classList.contains('sh-kbd')"))
    assert js("document.activeElement === document.body"), focus()
    go("right"), sh.select()
    assert log().endswith("page key ArrowRight\npage key Enter\n"), log()
    assert js("document.querySelectorAll('.overlay').length") == 1
    js("document.querySelector('.overlay').remove()")  # ...and once it's gone, the remote goes on from the tile it
    go("right", want="info")                            # played, not the first one on screen
    # a playing full-size video with no tiles below it is a player: its own arrows (seek) reach it
    s = open_tile("fx", URL + "?player")
    js = lambda expr: sh._eval(s, expr)
    assert until(lambda: js("!document.querySelector('video').paused")), "the test video didn't play"
    go("right")
    assert "page key ArrowRight" in js("document.getElementById('log').textContent")
    # get_screen's playing, which the agent's "nothing is playing" turn trusts: a muted video plays only as a player
    # (the skin's test: full-width across the screen's middle, over the page or with no tiles below it), in a shadow
    # root or a same-origin frame too, never as a title page's hero trailer or a small preview; a big frame it can't
    # read is unknown (null), never "nothing is playing"
    big = "width: 100vw; height: 100vh; border: 0"
    for make, want in (("document.body.replaceChildren(v)", True),  # (v: a muted video playing, full size by the page's
                       ("document.body.appendChild(Object.assign(document.createElement('div'), "  # CSS)
                        "{className: 'overlay'})).append(v)", True),
                       ("document.body.prepend(v)", False),  # (the page's tiles below it: a hero)
                       ("document.body.prepend(Object.assign(v, {style: 'height: 60vh'}))", False),
                       ("document.body.prepend(Object.assign(v, {style: 'width: 480px; height: 270px'}))", False),
                       (f"document.body.attachShadow({{mode: 'open'}}).append(Object.assign(v, {{style: '{big}'}}))",
                        True),
                       (f"(f => {{ document.body.prepend(f); f.contentDocument.body.append(Object.assign(v, {{style: "
                        f"'{big}'}})); }})(Object.assign(document.createElement('iframe'), {{style: "
                        f"'{big}; position: fixed; inset: 0'}}))", True),
                       (f"document.body.prepend(Object.assign(document.createElement('iframe'), "
                        f"{{src: 'data:text/html,<p>player</p>', style: '{big}'}}))", None)):  # (v plays unseen)
        s = open_tile("fx", URL)
        sh._eval(s, f"window.v = video(); {make}")
        assert until(lambda: sh._eval(s, "v.currentTime > 0")), make
        assert until(lambda: sh.read_screen().get("playing", 0) is want), (make, sh.read_screen().get("playing", 0))
        assert sh._front_playing() is want, (make, sh._front_playing())   # sleep's playing hold reads it alike
    # text fields of every kind keep their arrows (the page gets them, focus stays), a web component's too
    s = open_tile("fx", URL + "?fields")
    js = lambda expr: sh._eval(s, expr)
    for f, d in (("f1", "left"), ("f2", "left"), ("f3", "up"), ("f4", "left"), ("f5", "down"), ("f6", "left")):
        js(f"(e => {{ e.focus(); if (e.value) e.selectionStart = e.selectionEnd = e.value.length; }})"
           f"(document.getElementById('{f}').shadowRoot?.firstChild || document.getElementById('{f}'))")
        n = log().count("page key")
        go(d, 2)
        assert focus() == f and log().count("page key") == n + 2, (f, focus(), log())
    # without the skin, Chrome's spatial navigation has the arrows (what the skin replaces)
    s = open_tile("plain", URL)
    js = lambda expr: sh._eval(s, expr)
    go("down"), go("down")
    assert js("!!document.activeElement.id") and not js("!!document.getElementById('sh-ring')")
    assert js("getComputedStyle(document.getElementById('onetrust-consent-sdk')).display") != "none"
    # a "tiles" selector Chrome can't parse counts as none: the remote and the page tools still work
    s = open_tile("bad", URL)
    js = lambda expr: sh._eval(s, expr)
    go("down", want="play")
    r = sh.list_titles()
    assert "Stranger Things" in r.get("titles", []), r
    # the sleep warning over the web app in front (the launcher's is its own page): in the top layer, counting down,
    # never in the page's text (get_screen) nor in the way of its keys; gone when the TV stays awake. Unanswered, it
    # goes black at 0 (the signal goes 1.5 s later), then removes itself
    warn = lambda: js("(d => d && [d.matches(':popover-open'), d.textContent, getComputedStyle(d).backgroundColor])"
                      "(document.getElementById('__sparkhomeSleep'))")
    sh._tab_warning(True)
    assert warn() == [True, "Going to sleep in 10Press any button to stay awake", "rgba(0, 0, 0, 0.75)"], warn()
    assert until(lambda: warn()[1].startswith("Going to sleep in 9")), warn()
    assert "Going to sleep" not in sh.read_screen()["text"]
    shot("sleep-warning")
    go("up", want="home")
    sh._tab_warning(False)
    assert warn() is None and sh._warn_tab is None
    js(WARN_JS.replace("__N__", "1"))
    assert until(lambda: warn() == [True, "", "rgb(0, 0, 0)"], 2), warn()
    assert until(lambda: warn() is None, 7), warn()
    assert not until(lambda: errors, 0.5), errors


def main():
    chrome, shots = sys.argv[1], (sys.argv[2:] or [None])[0]
    try:
        socket.create_connection(("127.0.0.1", 9223), 0.3).close()
        kiosk = True
    except OSError:
        kiosk = False
    if kiosk or os.environ.get("SPARKHOME_NO_CHROME"):
        return print("skin check skipped: " + ("the TV's kiosk Chrome is up here" if kiosk else "SPARKHOME_NO_CHROME"))
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
    logging.disable(logging.CRITICAL)            # (the CDP client logs Chrome's exit at the end)
    prof = tempfile.mkdtemp(prefix="sparkhome-skin-")
    with socket.socket() as so:                  # a free port: never another Chrome's (the kiosk's is 9223)
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    p = subprocess.Popen([chrome, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={prof}",
                          "--enable-spatial-navigation", "--window-size=1280,720", "--no-first-run", "--mute-audio",
                          # as the kiosk: on the Spark's autologin desktop the login keyring is locked, and Chrome's
                          # default libsecret store put an "Authentication required" prompt on the TV (2026-10-09)
                          "--password-store=basic",
                          "--autoplay-policy=no-user-gesture-required", "about:blank"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        cfg = {"settings": {}, "rows": [{"id": "r", "tiles": ["fx", "plain", "bad"]}], "tiles": {
            "fx": {"type": "web", "title": "Fixture", "url": URL, "skin": {"hide": ["footer"]}, "tiles": ".card"},
            "plain": {"type": "web", "title": "Plain", "url": URL},
            "bad": {"type": "web", "title": "Bad", "url": URL, "skin": True, "tiles": ".card,"}}}
        sh = Shell(cfg, False, lambda *a: None)
        sh.cdp = CDP(port).start()
        assert sh.cdp.connected.wait(15), "Chrome's DevTools didn't come up"
        check(sh, shots)
        print("skin check passed")
    finally:                                     # an orderly exit, so no helper writes the profile after it's removed
        p.terminate()                            # (measured: after a kill, Linux Chrome left Default/ behind)
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        shutil.rmtree(prof, ignore_errors=True)


if __name__ == "__main__":
    main()
