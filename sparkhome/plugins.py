"""Plugins: new controllers, tile types and connectors, each one Python file, with no change to SparkHome's core.

settings.plugins names the plugins to load and gives each its settings (true: none). SparkHome looks for NAME.py in
~/.config/sparkhome/plugins/ first, then in the repo's plugins/ folder:

    "plugins": {"keymap": {"match": "FLIRC", "map": {"KEY_UP": "up", "KEY_ENTER": "select"}}, "flatpak": true}

A plugin module defines any of these; each is optional:

    start(home)             A controller or a background job. It runs once, on its own daemon thread: loop in it.
                            A controller reads its device and calls home.press("up"), home.voice(True)...
    TILE_TYPE = "flatpak"   A tile type, with tile(t) -> a built-in tile (type web, exec or steam) made from a tile
                            of this type: {"type": "flatpak", "app": "org.x"} -> {"type": "exec", "argv": [...]}.
    TOOLS = [...]           A connector: MCP-shaped tools ({name, description, inputSchema, annotations}) for the
    call(home, tool, args)  voice assistant, run by call() -> str | dict; raise to fail. A tool whose annotations say
                            readOnlyHint runs freely; any other runs only when the person's own words ask for it.
    WORDS = ("pc",)         Words that bring the connector's tools into a request. Its name always does.

home is a Home: the remote's buttons, push-to-talk, the pointer, typing, toasts and the state, the same verbs the
phone remote and the MCP tools use, plus ask() and say() to talk with the assistant. A plugin that fails to load, or
raises, is logged and skipped: the TV keeps going.
docs/EXTENDING.md has the contract in full, with recipes. Self-check: python3 -m sparkhome.plugins
"""
import importlib.util
import logging
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("sparkhome.plugins")
REPO = Path(__file__).resolve().parent.parent
DIRS = (Path("~/.config/sparkhome/plugins").expanduser(), REPO / "plugins")
NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")  # a connector's name too: no "__" (Connectors.dotted)

# The buttons any controller has, and the Shell action each one runs. One vocabulary for every input device.
BUTTONS = {
    "up": ("navigate", "up"), "down": ("navigate", "down"), "left": ("navigate", "left"),
    "right": ("navigate", "right"), "select": ("select",), "back": ("back",),
    "home": ("press",), "apps": ("switcher",),  # home twice quickly also opens the app switcher
    "playpause": ("media", "toggle"), "play": ("media", "play"), "pause": ("media", "pause"),
    "next": ("media", "next"), "previous": ("media", "previous"), "stop": ("media", "stop"),
    "volume_up": ("volume", "up"), "volume_down": ("volume", "down"), "mute": ("volume", "mute"),
}
# While a game or app is in front it reads its own controls: only these still reach SparkHome (as with the Siri Remote)
IN_APP = {"home", "apps", "playpause", "play", "pause", "next", "previous", "stop", "volume_up", "volume_down", "mute"}


class Home:
    """What a plugin gets: one plugin's handle on SparkHome. config is its settings.plugins entry."""

    def __init__(self, shell, name, config):
        self._shell, self.name, self.config = shell, name, config
        self.demo = shell.demo
        self.log = logging.getLogger(f"sparkhome.plugin.{name}")

    def press(self, button):
        """One press of a BUTTONS button -> the action's result ({"ok": ...}). A press that wakes the sleeping screen
        does only that. It runs on the caller's thread and can take a moment (a busy web page)."""
        act = BUTTONS.get(button)
        if not act:
            return {"ok": False, "error": f"unknown button {button!r}: one of {', '.join(BUTTONS)}"}
        sh = self._shell
        if sh.wake("key"):                       # (a person's input: it turns the TV back on too)
            return {"ok": True, "woke": True}
        if sh.state()["surface"] == "app" and button not in IN_APP:
            return {"ok": True, "ignored": "a game or app is in front"}
        fn, *args = act
        return sh.press(self.name) if fn == "press" else getattr(sh, fn)(*args)

    def voice(self, pressed):
        """Push-to-talk: True on the press, False on the release. The release ends the recording and runs it."""
        if self._shell.on_voice and self._shell.settings.get("asr"):
            self._shell.on_voice(bool(pressed))
        elif pressed:
            self.toast("Voice isn't set up on this SparkHome.")

    def pointer(self, dx=0, dy=0, click=None, scroll_x=0, scroll_y=0):
        """Move the TV's pointer by (dx, dy) pixels; click "left" | "right" | "middle"; scroll in steps."""
        return self._shell.pointer(dx, dy, click, scroll_x, scroll_y)

    def type_text(self, text):
        return self._shell.type_text(text)

    def open(self, tile_id):
        return self._shell.open(tile_id)

    def ask(self, text):
        """Words a person said (a plugin's own speech recognition, a chat bridge for the household) -> the assistant's
        answer {"ok", "reply", ...}, as if said into the remote. They count as the owner's own commands: never pass
        text a stranger or a web page could write."""
        agent = getattr(self._shell, "agent", None)
        return agent.handle_text(str(text)) if agent else {"ok": False, "error": "the assistant isn't running"}

    def say(self, text):
        """A short message on the TV for the room, spoken aloud when speech is on ("Someone's at the door")."""
        agent = getattr(self._shell, "agent", None)
        if not agent:
            return self.toast(text)
        return agent.call("say", {"text": str(text)[:200]})

    def toast(self, text, kind="info"):
        """A short message on the TV: kind info | error."""
        self._shell.emit("toast", {"text": str(text), "kind": kind})

    def state(self):
        """{"surface": launcher | web | app, "active": the tile in front, "title", "running": [...], ...}"""
        return self._shell.state()


class Plugin:
    def __init__(self, name, mod, home):
        self.name, self.mod, self.home = name, mod, home

    def start(self):
        try:
            self.mod.start(self.home)
        except Exception:                        # a plugin's bug: logged, never the daemon's
            log.exception("plugin %s stopped", self.name)


def load(shell, dirs=DIRS):
    """settings.plugins -> [Plugin]. A bad entry or a module that doesn't import is logged and skipped."""
    want = shell.settings.get("plugins") or {}
    want = dict.fromkeys(want, True) if isinstance(want, list) else want if isinstance(want, dict) else {}
    out = []
    for name, cfg in want.items():
        if cfg is False:
            continue
        path = next((p for d in dirs if (p := Path(d) / f"{name}.py").is_file()), None)
        if not NAME.fullmatch(str(name)) or not path:
            log.warning("plugin %r skipped: %s", name, "no such plugin in " + ", ".join(map(str, dirs)) if
                        NAME.fullmatch(str(name)) else "names are lowercase letters, digits and single _")
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"sparkhome_plugin_{name}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception:
            log.exception("plugin %s (%s) didn't load", name, path)
            continue
        out.append(Plugin(name, mod, Home(shell, name, cfg if isinstance(cfg, dict) else {})))
        log.info("plugin %s from %s", name, path)
    return out


def tile_types(plugins):
    """{type: tile(t) -> a built-in tile} from the plugins that add a tile type."""
    return {p.mod.TILE_TYPE: p.mod.tile for p in plugins if isinstance(getattr(p.mod, "TILE_TYPE", None), str)
            and callable(getattr(p.mod, "tile", None))}


def start(plugins):
    for p in plugins:
        if callable(getattr(p.mod, "start", None)):
            threading.Thread(target=p.start, name=f"plugin-{p.name}", daemon=True).start()


class Local:
    """A connector plugin behind the interface Connector expects of an MCP client (list_tools, call_tool)."""

    def __init__(self, plugin):
        self.p = plugin

    def list_tools(self, timeout=None):
        return [t for t in self.p.mod.TOOLS if isinstance(t, dict) and isinstance(t.get("name"), str)]

    def call_tool(self, name, args, timeout=None):
        import json
        try:
            r = self.p.mod.call(self.p.home, name, args or {})
        except Exception as e:
            log.warning("plugin %s: %s failed: %r", self.p.name, name, e)
            return {"isError": True, "content": [{"type": "text", "text": str(e) or type(e).__name__}]}
        return {"content": [{"type": "text", "text": r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)}]}


def connectors(plugins):
    return [p for p in plugins if isinstance(getattr(p.mod, "TOOLS", None), list) and callable(getattr(p.mod, "call",
                                                                                                         None))]


# ---- self-check ----------------------------------------------------------------------------------------------------

def _selftest():
    import tempfile

    class Sh:                                    # the parts of Shell a Home uses
        demo, on_voice = True, None

        def __init__(self, plugins):
            self.settings, self.calls = {"plugins": plugins, "asr": {}}, []
            self.surface, self.asleep = "launcher", False

        def wake(self, src):
            was, self.asleep = self.asleep, False
            return was

        def state(self):
            return {"surface": self.surface}

        def emit(self, typ, data):
            self.calls.append((typ, data))

        def __getattr__(self, fn):               # navigate, select, press, media...: recorded
            return lambda *a: self.calls.append((fn, *a)) or {"ok": True}

    with tempfile.TemporaryDirectory() as d:
        Path(d, "good.py").write_text("TILE_TYPE = 'thing'\ndef tile(t): return {'type': 'exec', 'argv': [t['x']]}\n"
                                      "TOOLS = [{'name': 'hi', 'inputSchema': {'type': 'object'}}]\n"
                                      "def call(home, tool, args): return {'said': args}\n"
                                      "def start(home): home.press('up')\n")
        Path(d, "broken.py").write_text("raise ImportError('nope')\n")
        sh = Sh({"good": {"k": 1}, "broken": True, "missing": True, "Bad-Name": True, "off": False})
        ps = load(sh, [d])
        assert [p.name for p in ps] == ["good"] and ps[0].home.config == {"k": 1}, ps
        assert tile_types(ps)["thing"]({"x": "y"}) == {"type": "exec", "argv": ["y"]}
        assert connectors(ps) == ps and Local(ps[0]).list_tools()[0]["name"] == "hi"
        assert Local(ps[0]).call_tool("hi", {"a": 1}) == {"content": [{"type": "text", "text": '{"said": {"a": 1}}'}]}
        ps[0].mod.call = lambda home, tool, args: 1 / 0
        assert Local(ps[0]).call_tool("hi", {})["isError"]
        assert load(Sh(["good"]), [d])[0].home.config == {}  # a list of names: no settings
        start(ps)
        for _ in range(100):
            if ("navigate", "up") in sh.calls:
                break
            time.sleep(0.01)
        assert ("navigate", "up") in sh.calls, sh.calls
        h, sh.calls = ps[0].home, []
        assert h.press("select") == {"ok": True} and h.press("home") == {"ok": True}
        assert sh.calls == [("select",), ("press", "good")], sh.calls  # Home is keyed by the plugin: double = switcher
        assert not h.press("warp")["ok"]
        sh.asleep, sh.calls = True, []           # the press that wakes the screen does only that
        assert h.press("select") == {"ok": True, "woke": True} and sh.calls == []
        sh.surface = "app"                       # a game in front: navigation is the game's, volume still SparkHome's
        assert "ignored" in h.press("up") and h.press("volume_up") == {"ok": True} and sh.calls == [("volume", "up")]
        h.voice(True)                            # no voice set up: a toast, not a crash
        assert sh.calls[-1][0] == "toast", sh.calls
        sh.on_voice, sh.settings["asr"], said = (lambda on: said.append(on)), {"server": "x"}, []
        h.voice(True), h.voice(False)
        assert said == [True, False]
    _selftest_live()
    # The plugins that ship in plugins/ check themselves too
    for path in sorted((REPO / "plugins").glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"sparkhome_plugin_{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if callable(getattr(mod, "_selftest", None)):
            mod._selftest()
    print("plugins selftest OK")


def _selftest_live():
    """The shipped plugins through the real Shell (--demo), Agent and Connectors: a tile type, a connector's tools for
    the model, its guard, its call and toast."""
    import json
    import os
    import tempfile
    from . import connectors as conn_mod, shell as shell_mod
    from .agent import REFUSED, Agent
    config = shell_mod.load_config(REPO / "sparkhome.json", Path("/nonexistent"))
    config["settings"]["plugins"] = {"flatpak": True, "wol": {"devices": {"gaming pc": "aa:bb:cc:dd:ee:ff"}}}
    config["settings"].update(tts={"enabled": False}, request_log=False, mcp_servers=[],  # no speech, log, servers
                              llm={**config["settings"]["llm"], "base_url": "http://127.0.0.1:9"})  # nor a model
    config["tiles"]["kart"] = {"type": "flatpak", "title": "Kart", "app": "net.supertuxkart.SuperTuxKart"}
    config["tiles"]["badpak"] = {"type": "flatpak", "title": "Bad", "app": "nope"}
    config["rows"][0]["tiles"] += ["kart", "badpak"]
    events, cache0 = [], conn_mod.CACHE
    conn_mod.CACHE = tempfile.mkdtemp(prefix="sparkhome-plugins-")
    try:
        sh = shell_mod.Shell(config, True, lambda typ, data: events.append((typ, data)))
        kart = sh.tile_defs["kart"]
        assert kart["type"] == "exec" and kart["argv"][:3] == ["flatpak", "run", "net.supertuxkart.SuperTuxKart"], kart
        assert "badpak" not in sh.tile_defs     # a tile its plugin refused is skipped, the rest load
        assert sh._classes.get("net.supertuxkart.supertuxkart") == "kart"   # its window is found as an exec tile's
        a = sh.agent = Agent(sh, sh.settings, sh.emit)
        c = a.connectors
        for _ in range(200):                     # tools are listed in the background (kick)
            if c.all["wol"].tools:
                break
            time.sleep(0.01)
        names = [t["function"]["name"] for t in c.llm_tools("wake the gaming pc")]
        assert names == ["wol__list_devices", "wol__wake_device"] and c.llm_tools("play netflix") == [], names
        assert "wol__* tools" in c.prompt("wake the gaming pc")
        args = {"device": "gaming pc"}
        assert c.guard("wake the gaming pc", "wol.wake_device", args) is None
        assert c.guard("is the gaming pc awake?", "wol.wake_device", args) == REFUSED       # a question only looks
        assert c.guard("wake the pc", "wol.wake_device", {"device": "nas"}) == REFUSED     # an argument not said
        assert c.guard("anything", "wol.list_devices", {}) is None and c.read_only("wol.list_devices")
        r = a.call("wol.wake_device", args)
        assert r["ok"] and r["reply"] == "Waking the gaming pc." and events[-1] == (
            "toast", {"text": "Waking the gaming pc.", "kind": "info"}), (r, events[-1:])
        r = a.call("wol.wake_device", {"device": "toaster"})
        assert not r["ok"] and "toaster" in r["error"], r
        assert json.loads(a.call("wol.list_devices", {})["reply"]) == {"devices": ["gaming pc"]}
        home = sh.plugins[1].home
        assert home.say("Hello")["ok"] and events[-1] == ("toast", {"text": "Hello", "kind": "info"}), events[-1]
        assert home.ask("go home")["ok"]
        sh.agent = None
        assert not home.ask("go home")["ok"]
    finally:
        for f in os.listdir(conn_mod.CACHE):
            os.unlink(os.path.join(conn_mod.CACHE, f))
        os.rmdir(conn_mod.CACHE)
        conn_mod.CACHE = cache0


if __name__ == "__main__":
    _selftest()
