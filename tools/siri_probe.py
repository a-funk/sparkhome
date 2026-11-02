#!/usr/bin/env python3
"""Siri Remote experiment E1: probe an Apple Siri Remote that BlueZ's HOG plugin already owns, via /dev/hidraw.

*** UNTESTED ON HARDWARE. *** Only the descriptor parser and the ioctl numbers are checked (--selftest). Nobody has run
this against a real Siri Remote yet; the hidraw reads and writes are the experiment.

Hypothesis (docs/RESEARCH.md, Siri Remote section): with BlueZ 5.72's `hog` plugin left on (the BLE Xbox pad needs
it), writing Apple's enable byte to the remote's Feature/Output reports through hidraw unlocks the buttons (report
0xFB), touch (0xFC) and microphone (0xFA) on gen 2/3. On gen 1, reports of 2, 13, 20 and 101 bytes are expected.

Run it on the Spark from ~/sparkhome after pairing the remote (deploy/pair.sh 'Siri Remote'):
  python3 -I tools/siri_probe.py --selftest                           # parser check, runs anywhere
  python3 -I tools/siri_probe.py --list                               # /dev/hidrawN, product id, report IDs by type
  sudo python3 -I tools/siri_probe.py /dev/hidrawN --seconds 20       # baseline: press buttons, no enable
  sudo python3 -I tools/siri_probe.py /dev/hidrawN --enable af --seconds 30     # azais-corentin/siri-remote variant
  sudo python3 -I tools/siri_probe.py /dev/hidrawN --enable f000 --seconds 30   # SiriRemoteESP / Yanndroid gen-3 variant
Opening /dev/hidrawN needs root. Without sudo, use GameSpark's privileged-container trick (docker group):
  docker run --rm -it --privileged --pid=host alpine nsenter -t 1 -m -u -i -n -p -- \
    python3 -I /home/spark/sparkhome/tools/siri_probe.py /dev/hidrawN --enable af --seconds 30
or let the logged-in user open Apple Bluetooth hidraw nodes with a udev rule (as root, then re-pair or re-trigger):
  /etc/udev/rules.d/71-sparkhome-remotes.rules:  SUBSYSTEM=="hidraw", KERNELS=="0005:004C:*", TAG+="uaccess"

Evidence gates:
  G1 pass/fail: after an enable, every button gives a 0xFB report (gen 2/3; names are decoded) or a 2/13/20-byte one
     (gen 1).
  G2 touching the surface or ring gives 0xFC reports (gen 2/3), or 13/20-byte reports with 0x32 in byte 2 (gen 1), at
     about 60 Hz.
  G3 holding Siri gives about 50 reports/s of 0xFA (gen 2/3) or of 101 bytes (gen 1), with an Opus packet (TOC 0xB8)
     at the payload offset.
  G4 after 5 minutes idle, the first press reconnects the remote and is delivered (note the latency).
If G1 fails, run `sudo btmon` during the enable. An ATT Write Request with no notifications after it means the payload
or the report is wrong. No write at all means HOG didn't map the (type, report ID). Then fall back to the paths in
docs/RESEARCH.md: BlueZ >= 5.80 ExportClaimedServices, an ESP32 bridge, or disabling hog.
"""
import argparse, fcntl, glob, os, select, sys, time

MAIN = {0x8: "input", 0x9: "output", 0xB: "feature"}
# Gen-2/3 0xFB bitmask, per SiriRemoteESP report_decoder.h and azais-corentin/siri-remote.
GEN3_BUTTONS = {0x0001: "TV", 0x0002: "Vol+", 0x0004: "Vol-", 0x0008: "Select", 0x0010: "Power",
                0x0020: "Siri", 0x0040: "Back", 0x0080: "Mute", 0x0100: "PlayPause",
                0x0200: "Up", 0x0400: "Right", 0x0800: "Down", 0x1000: "Left"}


def report_ids(desc):
    """{input,output,feature} -> set of report IDs declared in a HID report descriptor."""
    out = {name: set() for name in MAIN.values()}
    rid, stack, i = 0, [], 0
    while i < len(desc):
        prefix = desc[i]
        if prefix == 0xFE:  # long item: prefix, data size, tag, data
            i += 3 + desc[i + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 3]
        kind, tag = (prefix >> 2) & 3, prefix >> 4
        value = int.from_bytes(desc[i + 1:i + 1 + size], "little")
        if kind == 1 and tag == 0x8:
            rid = value
        elif kind == 1 and tag == 0xA:
            stack.append(rid)
        elif kind == 1 and tag == 0xB and stack:
            rid = stack.pop()
        elif kind == 0 and tag in MAIN:
            out[MAIN[tag]].add(rid)
        i += 1 + size
    return out


def hidioc_rw(nr, size):  # _IOC(_IOC_READ|_IOC_WRITE, 'H', nr, size), asm-generic layout (arm64)
    return (3 << 30) | (size << 16) | (ord("H") << 8) | nr


def apple_hidraw():
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        info = dict(l.split("=", 1) for l in open(node + "/device/uevent").read().split("\n") if "=" in l)
        bus, vendor, product = (int(x, 16) for x in info.get("HID_ID", "0:0:0").split(":"))
        if bus == 5 and vendor == 0x004C:  # BUS_BLUETOOTH, Apple
            desc = open(node + "/device/report_descriptor", "rb").read()
            yield "/dev/" + os.path.basename(node), product, info.get("HID_NAME", "?"), desc


def main():
    ap = argparse.ArgumentParser(description="Siri Remote hidraw probe (experiment E1; UNTESTED on hardware)")
    ap.add_argument("dev", nargs="?")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--enable", type=bytes.fromhex,
                    help="hex payload sent to every Feature and Output report, e.g. af or f000")
    ap.add_argument("--seconds", type=float, default=20)
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.list or not a.dev:
        found = list(apple_hidraw())
        for path, product, name, desc in found:
            print(f"{path} product=0x{product:04x} name={name!r} ids={report_ids(desc)}")
        if not found:
            print("no Apple Bluetooth hidraw nodes: pair the remote first (deploy/pair.sh 'Siri Remote')")
        return 0
    desc = next((d for p, _, _, d in apple_hidraw() if p == a.dev), None)
    if desc is None:
        sys.exit(f"{a.dev} is not an Apple Bluetooth hidraw node")
    ids = report_ids(desc)
    fd = os.open(a.dev, os.O_RDWR)
    if a.enable:
        hexed = a.enable.hex()
        for rid in sorted(ids["feature"]):
            try:
                fcntl.ioctl(fd, hidioc_rw(0x06, 1 + len(a.enable)), bytearray([rid]) + a.enable)  # HIDIOCSFEATURE
                print(f"feature 0x{rid:02x} <- {hexed}: ok")
            except OSError as e:
                print(f"feature 0x{rid:02x} <- {hexed}: {e}")
        for rid in sorted(ids["output"]):
            try:
                os.write(fd, bytes([rid]) + a.enable)
                print(f"output  0x{rid:02x} <- {hexed}: ok")
            except OSError as e:
                print(f"output  0x{rid:02x} <- {hexed}: {e}")
    end = time.monotonic() + a.seconds
    while (left := end - time.monotonic()) > 0:
        if select.select([fd], [], [], left)[0]:
            r = os.read(fd, 4096)
            note = ""
            if r[:1] == b"\xfb" and len(r) >= 3:
                mask = int.from_bytes(r[1:3], "little")
                note = " " + "+".join(n for b, n in GEN3_BUTTONS.items() if mask & b)
            print(f"{time.monotonic():.3f} len={len(r):3d} {r.hex(' ')}{note}")
    return 0


def selftest():
    # Synthetic descriptor shaped like the gen-3 layout: input 0xFB (buttons), input 0xFA (mic),
    # feature 0xF0, a Push/Pop pair, and a long item that must be skipped.
    desc = bytes.fromhex(
        "050c 0901 a101"
        " 85fb 1500 2501 7501 9510 81 02"
        " 85fa 7508 9563 8102"
        " a4 85f1 9101 b4"
        " fe 02 10 aabb"
        " 85f0 9501 b102 c0".replace(" ", ""))
    got = report_ids(desc)
    assert got == {"input": {0xFB, 0xFA}, "output": {0xF1}, "feature": {0xF0}}, got
    assert hidioc_rw(0x06, 2) == 0xC0024806  # HIDIOCSFEATURE(2)
    assert hidioc_rw(0x07, 256) == 0xC1004807  # HIDIOCGFEATURE(256)
    print("selftest ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
