# Apple IR remote + FLIRC → SparkHome

The white Apple Remote (A1156, shipped with the first Apple TV) and the aluminium one (A1294) are plain infrared
remotes. You don't need to jailbreak them: a FLIRC USB dongle learns their buttons once and then acts as a USB
keyboard. The Spark needs no driver, no udev rule and no root at runtime, and Bluetooth isn't involved.
The mapping is stored on the dongle, so you can program it on the Spark or on the Mac.
Background and alternatives (an IR receiver with `ir-keytable`, Siri Remotes) are in `docs/RESEARCH.md`.

Labels: **[M]** measured while building SparkHome (2026-10-08), **[D]** documented by the vendor, **[I]** inferred,
**[U]** unknown until you try it.

## What you need

- A FLIRC USB receiver (US$24.95 at flirc.tv). The Spark's rear ports are USB-C only, so plug it into the USB hub
  that's already attached, or use a USB-C→USB-A adapter. It has to be in line of sight of the couch.
- On the Spark: `libusb-1.0-0` is installed and `libhidapi-hidraw0` is not [M], so install it (below). Programming
  uses `sudo` because the FLIRC udev rule isn't installed (the `.deb` adds it, but it also pulls in Qt5 for the GUI).
- If a Mac or Apple TV nearby also reacts to the remote, change the remote's ID **before** recording: hold Menu + ⏯
  (the centre button on the white remote) for about 5 s [I; reports vary on hold time]. The ID is part of every
  code, so changing it later means recording again.

## Key choices

| Button | `flirc_util` | the Spark sees | SparkHome does |
|---|---|---|---|
| ring Up / Down / Left / Right | `record up` / `down` / `left` / `right` | arrow keys | move focus (hold to repeat) |
| Centre | `record return` | Enter | select |
| Menu | `record escape` | Escape | back |
| Menu, held ½ s | `record_lp browser_home` | XF86HomePage | **Home from anywhere**: a global X key grab (`settings.hotkeys.home`), so it works in games unless one grabs the keyboard |
| ⏯ | `record play/pause` | XF86AudioPlay | GNOME forwards it to the tab that's playing (MPRIS) |
| ⏯, if you have a microphone | `record_api 0 106` | F15 = XF86Launch6 | hold to talk (`settings.hotkeys.voice`) |

On the white A1156 the ring is Vol+ / Vol− / ⏮ / ⏭; record those as up / down / left / right. It has no separate ⏯,
so play/pause goes on a long press of the centre button.

**Why Home is `browser_home` and not F14.** The plan was Menu-hold → F14 (XF86Launch5, SparkHome's other Home key).
`flirc_util` 3.27.19 can't do that: `record` and `record_lp` take only named keys, and the named keys stop at F12 [M:
its help text]. F14 can only be recorded as a short press, with `record_api 0 105` (HID usage 0x69). The binary's key
table also contains a name its help text doesn't list, `browser_home` [M: strings of the arm64 binary]. Its name says
it is HID consumer usage AC Home, which Linux turns into KEY_HOMEPAGE, which X turns into XF86HomePage. SparkHome grabs
that key as Home [I: inferred from the name, not tested on a dongle]. If `record_lp browser_home` is refused, pick one:

- `record_lp home`: the plain Home key. The launcher jumps to the Dock, but inside a game or a streaming app it is just
  a Home key.
- Give up ⏯ and record it as `record_api 0 105` (F14 = SparkHome Home on a short press). Put play/pause on the
  Menu long press instead with `record_lp play/pause`.

## Program it (on the Spark)

```bash
sudo apt install libhidapi-hidraw0
mkdir -p ~/opt/flirc && cd ~/opt/flirc
curl -LO http://apt.flirc.tv/arch/arm64/flirc.latest.arm64.tar.gz        # plain HTTP, so check the hash:
echo "d88f5563e0b5b656f4c73ac749a66ad419406111971e7ce97196f556e147355e  flirc.latest.arm64.tar.gz" | sha256sum -c
tar xzf flirc.latest.arm64.tar.gz && cd Flirc-3.27.19-arm64              # flirc_util, irtools, the Qt GUI, a .deb
sudo ./flirc_util version
sudo ./flirc_util format                    # start clean: deletes every recorded button
```

The hash is the one measured on 2026-10-07/08 [M]. If FLIRC has published a newer build since then, verify it
another way before running it.

Each `record` waits for one button press aimed at the dongle. `record_lp` adds a long-press function to a button
that is **already recorded**: run it, then press that same button again.

Aluminium A1294:

```bash
sudo ./flirc_util record up                 # ring up
sudo ./flirc_util record down               # ring down
sudo ./flirc_util record left               # ring left
sudo ./flirc_util record right              # ring right
sudo ./flirc_util record return             # centre
sudo ./flirc_util record escape             # Menu
sudo ./flirc_util record_lp browser_home    # Menu again (now: short = Escape, held ½ s = Home)
sudo ./flirc_util record play/pause         # ⏯   (or, with a mic: sudo ./flirc_util record_api 0 106)
```

White A1156:

```bash
sudo ./flirc_util record up                 # Vol+ (top of the ring)
sudo ./flirc_util record down               # Vol−
sudo ./flirc_util record left               # ⏮
sudo ./flirc_util record right              # ⏭
sudo ./flirc_util record return             # centre ⏯
sudo ./flirc_util record_lp play/pause      # centre again (held ½ s = play/pause)
sudo ./flirc_util record escape             # Menu
sudo ./flirc_util record_lp browser_home    # Menu again (held ½ s = Home)
```

A button with a long press can only send its short key once it knows the press was short, so holding Menu or the
centre button doesn't repeat [I]. The arrows have no long press, so they keep repeating while held.

Save the mapping into the repo so a new or reset dongle is one command away. Keep it in the **Mac** checkout:
`~/sparkhome` on the Spark is an `rsync --delete` mirror of it, so a file saved only there is deleted by the next deploy.

```bash
sudo ./flirc_util saveconfig /tmp/flirc-a1294   # on the Spark; flirc_util adds .fcfg [I: from the binary's "%s.fcfg"]
scp spark:/tmp/flirc-a1294.fcfg deploy/   # on the Mac, in the repo (flirc-a1156 for the white remote)
# after the next rsync: sudo ./flirc_util loadconfig ~/sparkhome/deploy/flirc-a1294.fcfg
```

On the Mac, the FLIRC app from flirc.tv programs the same dongle [D]. The commands above are for `flirc_util`.

## Check it

You don't need root to watch the keys: X sees the dongle as a keyboard. Run this in a terminal on the TV, or over ssh
(it takes the session's display from systemd, which is `:1` after a password login and likely `:0` after autologin):

```bash
env $(systemctl --user show-environment | grep -E '^(DISPLAY|XAUTHORITY)=') xinput test-xi2 --root | grep --line-buffered -A2 RawKeyPress | grep --line-buffered detail
```

Expected X keycodes (the Linux key code + 8): Up 111, Down 116, Left 113, Right 114, Enter 36, Escape 9,
XF86HomePage 180, XF86AudioPlay 172, F15 193 (F14 192, Home 110 for the alternatives). With root, run
`sudo python3 -m sparkhome.inputs --probe` from `~/sparkhome` to read the dongle directly.

Gates:

1. Every button gives exactly one expected key, and nothing else reacts (no GNOME pop-up, no other Apple device).
2. **A1294 only: Centre and ⏯ stay separate keys.** Both send the same second IR frame, and nobody has published a
   FLIRC test of this [U]. If `record play/pause` answers that the button already exists, or ⏯ acts as Enter, then
   FLIRC can't tell them apart on your remote. Use the A1156 recipe for the centre instead (`record_lp play/pause`
   on the centre).
3. A short Menu press goes back. Holding Menu goes Home, both in the launcher and while a game has focus.
4. Holding an arrow keeps moving the focus. If presses stick or double, try `sudo ./flirc_util interkey_delay 3`
   (the help text calls 3 the "sweet spot"). If you see phantom presses, try `noise_canceler enable`.

If a gate fails, `sudo ./flirc_util format` and record again, or `sudo ./flirc_util delete` and then press the one
button to drop it.
