"""wol: wake computers on the LAN by voice ("wake the gaming pc"). The template for a connector plugin.

    "plugins": {"wol": {"devices": {"gaming pc": "aa:bb:cc:dd:ee:ff", "nas": "aa:bb:cc:dd:ee:01"}}}

A connector gives the voice assistant tools. A tool that only reads says so (annotations.readOnlyHint) and runs
freely; any other runs only when the person's own words ask for it: the tool's verb (its name's first word, "wake"),
a word that names the connector (its name or WORDS) and every argument, said aloud. So text on a web page can't wake
anything. Pair it with a Moonlight tile to stream from the PC it wakes.
"""
import re
import socket

WORDS = ("wake", "pc", "computer", "desktop", "server", "nas")
TOOLS = [
    {"name": "list_devices", "description": "The computers this can wake, by name.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}},
    {"name": "wake_device", "description": "Wake a computer on the LAN (Wake-on-LAN) by its name from list_devices.",
     "inputSchema": {"type": "object", "properties": {"device": {"type": "string", "description": "Its name"}},
                     "required": ["device"]}},
]


def packet(mac):
    """The magic packet: 6 x FF, then the MAC 16 times."""
    hexes = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(hexes) != 12:
        raise ValueError(f"not a MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(hexes) * 16


def send(mac, host="255.255.255.255", port=9):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet(mac), (host, port))


def call(home, tool, args):
    devices = {str(k).lower(): v for k, v in (home.config.get("devices") or {}).items()}
    if tool == "list_devices":
        return {"devices": sorted(devices)}
    name = str(args.get("device", "")).lower().removeprefix("the ").strip()
    if name not in devices:
        raise ValueError(f"no device named {name!r}; known: {', '.join(sorted(devices)) or 'none'}")
    if not home.demo:
        send(devices[name], home.config.get("broadcast", "255.255.255.255"))
    return f"Waking the {name}."


def _selftest():
    p = packet("AA:bb:cc:dd:ee:ff")
    assert len(p) == 102 and p[:6] == b"\xff" * 6 and p[6:12] == bytes.fromhex("aabbccddeeff") and p[-6:] == p[6:12]
    home = type("H", (), {"demo": True, "config": {"devices": {"Gaming PC": "aa:bb:cc:dd:ee:ff"}}})()
    assert call(home, "list_devices", {}) == {"devices": ["gaming pc"]}
    assert call(home, "wake_device", {"device": "the gaming PC"}) == "Waking the gaming pc."
    for bad in ({"device": "toaster"},):
        try:
            call(home, "wake_device", bad)
            raise AssertionError("an unknown device must fail")
        except ValueError:
            pass
    try:
        packet("aa:bb")
        raise AssertionError("a short MAC must fail")
    except ValueError:
        pass
