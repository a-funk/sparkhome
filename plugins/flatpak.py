"""flatpak: a tile type for Flatpak apps. The template for a tile-type plugin.

    "plugins": {"flatpak": true},
    "tiles": {"retroarch": {"type": "flatpak", "title": "RetroArch", "app": "org.libretro.RetroArch"}}

A tile-type plugin turns its tiles into built-in ones (web, exec or steam) when SparkHome loads its config, so the
launcher, the app switcher, close and force quit work for them as for any other tile. Here: an exec tile running
`flatpak run APP [ARGS]`, its window found by the app id unless the tile gives window_class.
"""
TILE_TYPE = "flatpak"


def tile(t):
    app = t.get("app")
    if not isinstance(app, str) or not app.count("."):
        raise ValueError("a flatpak tile needs app, a Flatpak id like org.libretro.RetroArch")
    return {"type": "exec", "argv": ["flatpak", "run", app, *map(str, t.get("args") or ())],
            "window_class": t.get("window_class") or [app, app.rsplit(".", 1)[-1]]}


def _selftest():
    assert tile({"app": "org.libretro.RetroArch", "args": ["-f"]}) == {
        "type": "exec", "argv": ["flatpak", "run", "org.libretro.RetroArch", "-f"],
        "window_class": ["org.libretro.RetroArch", "RetroArch"]}
    assert tile({"app": "a.b", "window_class": "x"})["window_class"] == "x"
    try:
        tile({"app": "nope"})
        raise AssertionError("a bad id must fail")
    except ValueError:
        pass
