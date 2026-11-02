# SparkHome research

Consolidated research behind SparkHome, an Apple-TV-style launcher for the user's NVIDIA DGX Spark
(`spark`, user `spark`) on a Sony TV. It merges seven reports written on 2026-10-07/08: [the on-device
lab](research/spark-lab.md), [Apple remotes](research/apple-remotes.md), [controllers and TV
control](research/controllers.md), [streaming](research/streaming.md), [games](research/games.md), [voice and
agents](research/voice-agent.md), and [the UI and session design](research/ui-session.md). All on-box checks were
read-only: commands over ssh, plus headless Chrome with throwaway profiles. The one exception is that the lab pulled two
Ollama models (8.6 GB), which are still on disk.

**Sources.** The seven reports are kept as written in [`docs/research/`](research/), with only the build's local
scratch paths shortened to `<scratchpad>/`. The scripts and raw downloads they mention were not kept; the Siri probe
became `tools/siri_probe.py`. Where a report disagrees with this document, this document is current; §10 resolves the
conflicts between the reports themselves.

**Labels.** Every claim carries one. A linked label points to its source.

| Label | Meaning |
|---|---|
| [M] | **Measured.** Run on this Spark (read-only), or read from a primary artifact during the research. The command is given where it matters. "(user)" marks numbers the user measured earlier. |
| [D] | **Documented.** Vendor docs, specs or source code. |
| [R] | **Reported.** Press, forums or third-party tests, not reproduced here. |
| [I] | **Inferred.** Reasoning from the above. Treat it as a hypothesis until tested. |
| [U] | **Unknown.** Not published, or not found. |

"Contended" means another session was running Divinity: Original Sin 2 on the GPU during the measurement.
Commands use SparkHome's ports: daemon and MCP on 8780, whisper-server on 8781, CDP on 9223. The reports used other ports
for their experiments.

**Contents:**
0. [Can a "Gen 1" Apple TV remote be jailbroken or used?](#0-can-a-gen-1-apple-tv-remote-be-jailbroken-or-used)
1. [The Spark as measured](#1-the-spark-as-measured)
2. [Remotes, controllers and voice hardware](#2-remotes-controllers-and-voice-hardware) (includes the buy list)
3. [TV control, power and thermals](#3-tv-control-power-and-thermals)
4. [Streaming](#4-streaming)
5. [Chrome kiosk and CDP](#5-chrome-kiosk-and-cdp)
6. [Games](#6-games)
7. [Voice, agent and computer use](#7-voice-agent-and-computer-use)
8. [Session, windows and input on GNOME/Xorg](#8-session-windows-and-input-on-gnomexorg)
9. [Launcher look: tvOS numbers](#9-launcher-look-tvos-numbers)
10. [Contradictions between the reports, resolved](#10-contradictions-between-the-reports-resolved)
11. [Decisions and open questions](#11-decisions-and-open-questions)

---

## 0. Can a "Gen 1" Apple TV remote be jailbroken or used?

**Yes, it can be used. No, it can't be jailbroken, and it doesn't need to be.**

- **Which remote is it?** "Gen 1" most likely means the **white Apple Remote A1156**, which shipped with the first
  Apple TV in 2007 [[R]][wiki-remote]. It could also mean the **1st-generation Siri Remote** (A1513/A1962, 2015: black,
  with a glass touch surface) [[D]][wiki-siri]. §2.1 shows how to tell them apart.
- **The IR remotes (A1156 white, A1294 aluminium)** are fixed-function NEC-style infrared transmitters. They have no
  updatable firmware, so there is nothing to jailbreak [[D]][hid-appleir]. The Spark has **no IR receiver**
  [[D]][spark-hw]. A **FLIRC USB** dongle (US$24.95 [M], [flirc.tv][flirc]) learns the remote once and replays it as a
  plain USB keyboard: arrows, Enter, Escape and media keys. Chrome, Steam and GNOME accept those with no extra software
  [[D]][flirc]. These remotes have **no microphone, so no voice**.
- **The Siri Remotes** are Bluetooth LE HID devices. No public custom firmware, exploit or flash dump exists [U], and
  none is needed. Buttons, touch and (on gen 3) the microphone become readable from userspace after a one-byte "enable"
  write [[R]][azais] [[R]][yanndroid]. Mainline Linux has no Siri Remote driver [M: `hid-ids.h` and `hid-apple.c` on
  master]. The real obstacle is BlueZ 5.72's `hog` plugin. It owns the remote's HID service, and the user's BLE Xbox
  controller needs it (§2.3).
- **Reflashing** a remote risks bricking it and may void its FCC authorization [[D]][cfr1521]. Receiving the
  standard IR or BLE signals of a remote you own involves no circumvention [I], and US law permits analysis for
  interoperability [[D]][usc1201]. This is not legal advice.
- **The 2007 Apple TV box itself** (A1218: Pentium M, 256 MB RAM) once ran Linux and XBMC through community
  "jailbreaks" such as atv-bootloader and Crystalbuntu. OSMC dropped it on 2017-09-24 [[D]][osmc]. Using it as an IR
  bridge for the Spark would be a bad trade against a US$25 FLIRC [I]. Don't.
- **Recommendation.** For an IR remote, use FLIRC. The recipe is in
  [`deploy/flirc-apple-remote.md`](../deploy/flirc-apple-remote.md); note the long-press Home caveat in §2.2. For a Siri
  Remote, run experiment E1 (`tools/siri_probe.py`, untested on hardware) before anything else.

[wiki-remote]: https://en.wikipedia.org/wiki/Apple_Remote
[wiki-siri]: https://en.wikipedia.org/wiki/Siri_Remote
[hid-appleir]: https://raw.githubusercontent.com/torvalds/linux/master/drivers/hid/hid-appleir.c
[spark-hw]: https://docs.nvidia.com/dgx/dgx-spark/hardware.html
[flirc]: https://flirc.tv/products/flirc-usb-receiver
[azais]: https://github.com/azais-corentin/siri-remote
[yanndroid]: https://github.com/Yanndroid/SiriRemote-Linux
[cfr1521]: https://www.law.cornell.edu/cfr/text/47/15.21
[usc1201]: https://www.law.cornell.edu/uscode/text/17/1201
[osmc]: https://my.osmc.tv/saying-goodbye-to-apple-tv/

---

## 1. The Spark as measured

**Conclusion.** The box is a capable 1080p TV computer. It has Chrome with software-only DRM, a working local LLM and
speech stack, no microphone, no CEC and no IR receiver. Its GDM autologin has never actually run at boot.

| Area | Fact | Evidence |
|---|---|---|
| OS | DGX OS 7.2.3 (Ubuntu 24.04.4 arm64), kernel 6.17.0-1029-nvidia, NVIDIA driver 580.173.02 | [M] |
| Session | GNOME 46 (mutter 46.2) on **Xorg**. The user manager's environment has `DISPLAY=:1`, `XAUTHORITY=/run/user/1000/gdm/Xauthority` and `XDG_SESSION_TYPE=x11`; `graphical-session.target` is active | [M] `systemctl --user show-environment` |
| Autologin | `AutomaticLogin=spark` was written at 2026-10-07 09:19, but the running session was started by `gdm-password` on 2026-10-06. **Autologin has never run at boot.** | [M] `/etc/gdm3/custom.conf`, `loginctl` |
| TV | Sony on HDMI-0 at **1920×1080@60 only**: the EDID lists only 1080p modes. EDID maker `SNY`, product 0xEA01 with a placeholder date; HDMI physical address 1.0.0.0, so the Spark is on TV input 1. **The model and year are unknown.** | [M] `xrandr --verbose`; [linuxhw EDID][edid] |
| TV network | No Sony or BRAVIA device answered SSDP | [M] SSDP M-SEARCH |
| CEC / IR | No `/dev/cec*`: the GB10 has no HDMI-CEC. No IR receiver | [M]; [[D]][spark-hw] |
| Audio | PipeWire 1.0.5. One sink (HDMI). **No capture device** (`arecord -l` is empty) | [M] |
| Chrome | `google-chrome` 155.0.8059.39 arm64 in `/opt/google/chrome`, with the bundled Widevine CDM 4.10.3112.0 | [M] |
| CPU | X925 performance cores are CPUs 5–9 and 15–19 (3.9 GHz); A725 cores are 0–4 and 10–14 | [M] sysfs topology |
| GPU | NVIDIA GB10 (integrated), Vulkan 1.4.312, Vulkan Video decode for H.264, H.265 and AV1 | [M] `vulkaninfo` |
| Memory | 121.6 GiB usable unified LPDDR5x at about 208 GB/s, shared by CPU, GPU, games and models | [M] (user) |
| Rear I/O | 4× USB-C (one is for power, so 3 are usable), HDMI 2.1a, 10 GbE, 2× QSFP. No USB-A, no IR | [[D]][spark-hw] |
| USB | A Genesys hub with a USB keyboard and a Logitech mouse is attached. USB-A dongles can go there | [M] `lsusb` (see §10 #3) |
| Permissions | `spark` is not in `input`, `video` or `dialout`, and has no passwordless sudo. Gamepad nodes (`js0`, `event7`) carry a uaccess ACL. `/dev/uinput` is writable through steam-launcher's `60-steam-input.rules`. Keyboard and remote event nodes are `root:input` | [M] `getfacl`, `id` |
| Bluetooth | MediaTek MT7925, BlueZ 5.72 with `Privacy = device` and `JustWorksRepairing = always`. The Xbox Wireless Controller (BLE) and a DualSense are paired. The radio sometimes stops hearing devices, and a power off/on clears it | [M]; GameSpark `controller/README.md` |
| Xbox pad | Guide = `BTN_MODE` (316), Share = `KEY_RECORD` (167) | [M] decoded `B: KEY=` |
| Power | `AllowSuspend=no`; `s2idle` only; no hibernate. NVIDIA staff confirmed that Wake-on-LAN is unsupported | [M]; [[R]][wol] |
| Thermals | `acpitz` zones at 40.8–42.4 °C with the display on, near idle | [M] |
| Ollama | 0.32.5 on 127.0.0.1:11434. The lab pulled `qwen3:4b-instruct-2507-q4_K_M` (2.5 GB) and `qwen3-vl:8b-instruct` (6.1 GB). The preinstalled `gemma3:1b` cannot call tools | [M] |
| Speech | whisper.cpp 1.9.2 (CUDA) with `ggml-large-v3-turbo-q8_0.bin`, and Piper 1.6.0 with `en_US-lessac-medium`. Both were moved to `~/code/voice/`, and **absolute paths broke**: whisper's RUNPATH and piper's shebang. Workarounds: `LD_LIBRARY_PATH=~/code/voice/whisper.cpp/build/bin` and `python -m piper`. ffmpeg and sox are not installed | [M] |
| Steam | Canonical snap 1.0.0.85 rev 245. Installed games: 1091500 Cyberpunk 2077, 1174180 Red Dead Redemption 2, 391220 Rise of the Tomb Raider, 435150 Divinity: Original Sin 2. Installed tools: 1070560, 1493710, 228980, 4183110 | [M] `appmanifest_*.acf` |
| Packaging | Flatpak is not installed (snap is). `libfuse2t64` is missing. AppArmor restricts unprivileged user namespaces | [M] |
| Missing tools | ffmpeg, xdotool, wmctrl, evtest, pactl, sox, gamescope | [M] `command -v` |

[edid]: https://github.com/linuxhw/EDID/tree/master/Digital/Sony/SNYEA01
[wol]: https://forums.developer.nvidia.com/t/why-doesn-t-the-dgx-spark-support-wake-on-lan-wol/360492

---

## 2. Remotes, controllers and voice hardware

**Conclusion.**
- **Use what already works:** the Xbox pad, the DualSense, the USB keyboard and the phone remote.
- **For an IR Apple remote, add FLIRC.**
- **Voice needs new hardware:** a USB microphone, or a 2.4 GHz voice remote whose dongle is a USB audio device.
- **A Siri Remote is an experiment, not a plan.**

### 2.1 Which Apple remote is it?

| Model | Look | Radio | Power | Shipped with | Evidence |
|---|---|---|---|---|---|
| **A1156** Apple Remote | white plastic, 6 buttons: ring Vol+/Vol−/⏮/⏭, centre ⏯, Menu | **IR only**, 38 kHz NEC-style | CR2032 | Front Row Macs, **Apple TV 1st gen**; 2005–2009 | [[D]][wiki-remote] |
| **A1294** Apple Remote | thin aluminium, 7 buttons: ring, centre Select, Menu, separate ⏯ | **IR only** | CR2032 | Apple TV 2nd/3rd gen, Macs; from Oct 2009 | [[D]][apple-identify] |
| **A1513 / A1962** Siri Remote gen 1 | black aluminium, **glass touch surface** on the top third | **BLE 4.0** + IR blaster | Lightning | Apple TV HD (2015); A1962 shipped with Apple TV 4K (2017) | [[D]][wiki-siri]; sources conflict on A1962 (§10 #17) |
| **A2540** Siri Remote gen 2 | silver aluminium, clickpad ring, power button | BLE 5.0 + IR | Lightning | May 2021 | [[D]][wiki-siri] |
| **A2854** Siri Remote gen 3 | as A2540 | BLE + IR | **USB-C** | Nov 2022, current | [[D]][wiki-siri] |

### 2.2 Apple remote options (ranked)

| Rank | Remote | Path | Voice | Effort, cost | Confidence |
|---|---|---|---|---|---|
| 1 | A1156 or A1294 (IR) | **FLIRC USB**, programmed once with `flirc_util` (an arm64 build exists, and the Mac works too). The Spark just sees a USB keyboard | none | ~30 min, US$24.95 | high [M]/[[D]][flirc] |
| 2 | A1156 or A1294 (IR) | A raw IR receiver with an rc-core driver (`mceusb`, `ir_toy`, both in this kernel [M]) plus an `ir-keytable` NEC32 keymap. Apple keys decode as `0x87EE<ID><CMD>`, and no Apple keymap ships [[D]][rc-protos]/[M]. Needs root | none | 1–2 h, ≈US$15–40 [R] | high [D] |
| 3 | Siri gen 2/3 | **Experiment E1**: keep BlueZ `hog`, send the enable report through `/dev/hidraw`, read reports 0xFA (mic), 0xFB (buttons) and 0xFC (touch) | yes (Opus) | 0.5–2 days | medium-low [I] |
| 4 | Siri gen 2/3 | [azais-corentin/siri-remote][azais] (Rust, GPL-3.0; gen 3 tested; publishes the mic as a PipeWire source). On BlueZ 5.72 it needs `hog` **off**, which breaks the BLE Xbox pad | yes | ~1 day | medium [R] |
| 5 | Siri gen 2/3 | An ESP32-S3 bridge ([Jakkumn/SiriRemoteESP][siriesp], gen 3 tested) on a Seeed XIAO ESP32-S3 (US$7.49 [M]). A fork that enumerates as a USB keyboard and microphone would take BlueZ out of the picture | yes (decoded on the ESP) | 1–5 days | medium [R/I] |
| 6 | Siri gen 1 | [Yanndroid/SiriRemote-Linux][yanndroid] (bluepy, root; no licence file) for buttons and touch, or the E1 hidraw variant. The gen-1 mic has never been made to work on Linux | not on Linux | 1–2 days | medium-low [R] |
| — | 2007 Apple TV box | Don't repurpose it (§0) | — | — | — |

**FLIRC facts.**
- The arm64 tarball is at `http://apt.flirc.tv/arch/arm64/flirc.latest.arm64.tar.gz`: version 3.27.19, 20,185,904 bytes,
  sha256 `d88f5563e0b5b656f4c73ac749a66ad419406111971e7ce97196f556e147355e` [M]. It is served over **plain HTTP**, so
  check the hash.
- `flirc_util` needs only `libusb-1.0-0` and `libhidapi-hidraw0` [M].
- The key map is stored on the dongle, so the Spark needs no FLIRC software [[D]][flirc].
- `record_lp` adds a second key, sent when the button is held for half a second [M: help text].

**Long-press Home: the spec's mapping can't be recorded as written.** SPEC planned Menu long-press → F14, which X maps to
XF86Launch5, SparkHome's global Home key.
- [M] The help text of `flirc_util` 3.27.19 documents these names for `record` and `record_lp`: a–z, return, enter,
  escape, backspace, delete, tab, space, **F1–F12**, printscreen, scroll, pause, insert, home, pageup, pagedown, end,
  the arrows, the media keys and suspend. **F13–F24 appear in none of the binary's key-name tables** [M: `strings`].
- F14 can only be sent with `record_api 0 105`. Usage 0x69 maps to `KEY_F14` [[D]][hid-input]. But `record_api` sets a
  button's short-press function, not its long press [M: help text].
- The binary also contains undocumented consumer key names: `browser_home`, `browser_back`, `media_home`, `menu_pick`,
  `ac_search`, `power` [M: `strings`].
  - `browser_home` would send AC Home → `KEY_HOMEPAGE` → XF86HomePage [[D]][hid-input], which SparkHome already grabs as
    Home [M].
  - Whether `record_lp` accepts it is [U].
- **Try, in this order:**
  1. `record_lp browser_home`.
  2. Give a spare button F14 as its short press with `record_api 0 105`. On the A1294, that could be ⏯.
  3. `record_lp home`. It sends `KEY_HOME`, which works as Dock inside the launcher only; it is not a global key.

**Other caveats.**
- Holding a button sends IR repeat frames, so FLIRC keeps the key down and X autorepeat scrolls rows [I]. Verify with
  `xev` and tune `interkey_delay`.
- The A1294's Centre and ⏯ both end with the same second frame, so check that they stay distinct [[D]][hid-appleir]. No
  FLIRC + A1294 test was found [U].
- A nearby Mac or Apple TV also reacts to the remote. Holding Menu + ⏯ changes the remote's ID, and FLIRC must then be
  re-learned [[R]][atv-boot].

### 2.3 Siri Remote paths and the BlueZ `hog` conflict

**Conclusion.** Every Linux Siri Remote implementation is blocked on BlueZ 5.72 by the `hog` plugin, which the BLE Xbox
pad needs. Experiment E1 is the only path that might keep both. It is untested.

- **Protocol.** These facts are reported by several projects that agree with each other [[R]][azais]
  [[R]][siriesp] [[R]][yanndroid]:
  - Gen 2/3 report IDs: **0xFA** microphone, **0xFB** buttons, **0xFC** touch. Buttons arrive as a 2-byte bitmask.
  - The microphone sends Opus (CELT-only, 16 kHz wideband, 20 ms frames), and only while Siri is held.
  - Two enable writes each work for their authors: `0xAF` to every writable non-input report, or `0xF0 0x00` to the
    first report without notify.
  - Gen 1 sends 2-, 13-, 20- or 101-byte notifications after `0xAF`. Its microphone has never been decoded on Linux
    [[R]][jackr1].
- **Ownership.**
  - BlueZ 5.72's `hog` plugin claims the HID service and hides it from D-Bus GATT clients [[D]][bluez-device]. So bleak
    and azais cannot write the enable report while `hog` runs [R].
  - Disabling `hog` (`bluetoothd -P hog` through a systemd drop-in) stops input from **every** BLE HID device, including
    the Xbox pad [I].
  - BlueZ ≥ 5.80 fixes this with `[GATT] ExportClaimedServices = read-write` [[D]][bluez-commit]. Ubuntu 24.04 ships
    5.72 [M].

| Option | How | Keeps the Xbox BLE pad? | Risk |
|---|---|---|---|
| O1 hidraw (E1) | Leave `hog` alone. `hog-lib.c` forwards hidraw feature/output writes to the GATT report matched by type and ID [[D]][bluez-hog] | yes | untested; depends on the report descriptor declaring the IDs |
| O2 disable `hog` | systemd drop-in `ExecStart=/usr/libexec/bluetooth/bluetoothd -P hog` | **no** | the pad must move to USB or an Xbox Wireless Adapter |
| O3 BlueZ ≥ 5.80 | `ExportClaimedServices = read-write` | yes | replaces Ubuntu's BlueZ; DGX OS updates may fight it |
| O4 raw ATT socket | bluepy-style client next to bluetoothd | probably | coexistence with bluetoothd's auto-reconnect [U] |
| O5 ESP32 bridge | the ESP32 owns the BLE link | yes | firmware work, one more device |

**E1 procedure.** It needs the remote. `tools/siri_probe.py` is the research probe; its parser self-test passes, but its
hidraw and ioctl parts are untested.

```bash
python3 -I tools/siri_probe.py --selftest
python3 -I tools/siri_probe.py --list                            # Apple BT hidraw nodes and report IDs by type
sudo python3 -I tools/siri_probe.py /dev/hidrawN --seconds 20    # baseline, no enable
sudo python3 -I tools/siri_probe.py /dev/hidrawN --enable af   --seconds 30
sudo python3 -I tools/siri_probe.py /dev/hidrawN --enable f000 --seconds 30
```

**Gates:**
- G1: an 0xFB report (or a gen-1 report of 2, 13 or 20 bytes) arrives for every button.
- G2: touch produces about 60 reports a second.
- G3: holding Siri produces about 50 reports a second of 0xFA, carrying Opus TOC byte 0xB8.
- G4: after 5 minutes idle, the first press reconnects and is delivered.

If G1 fails, run `sudo btmon`. A write with no notifications after it means the payload is wrong. No write at all means
`hog` did not map the report. Then fall back to O3, O5 or O2.

Pairing combos:
- Gen 1: **Menu + Vol+** for about 5 s.
- Gen 2/3: **Back + Vol+** for about 5 s. The address rotates about every 20 s, so pair promptly.
- Sources: [[D]][apple-pair]; rotation [[R]][azais].

### 2.4 Linux input facts that decide which remote works

- **X11 cannot see evdev codes ≥ 248** [[D]][xorg-limit]. The lost keys include `KEY_OK` 352, `KEY_SELECT` 353 (the
  "OK" of many remotes), `KEY_VOICECOMMAND` 582, `KEY_ROOT_MENU` 618 and the `KEY_NUMERIC_*` digits. Read them with evdev,
  or remap them with hwdb.
- **GNOME grabs many remote keys** [M] (`gsettings list-recursively …media-keys`):
  - **Grabbed:** XF86Search, XF86PowerOff, the media keys, the volume keys, XF86Tools (F13 and `KEY_CONFIG`, which open
    Settings), XF86AudioMicMute (F20), the touchpad toggles (F21–F23), WWW, Mail and Calculator.
  - **Not grabbed:** XF86HomePage, XF86Back, XF86MenuKB and XF86Launch5–9 (F14–F18).
  - Chrome itself acts on XF86HomePage and XF86Back [[D]][chrome-accel].
- **SparkHome's own grabs.** Passive X11 grabs on the root window succeed for XF86HomePage (180), XF86Search (225),
  XF86Back (166) and XF86MenuKB (147) [M]. A passive grab delivers the key to the grabber instead of the focused window
  [[D]][xgrabkey]. So SparkHome's configured hotkeys (Home = XF86HomePage or F14/XF86Launch5, voice = F15/XF86Launch6)
  never reach Chrome [I].
- **Power-key hazard.** Every key-capable device is tagged as a logind power switch, and GNOME's
  `power-button-action` is `interactive` [M]. So a remote's Power key opens GNOME's power-off dialog over the TV.
  Remap it.
- **Media and volume keys** go to gnome-settings-daemon [M]. Volume changes the HDMI sink. Play/pause goes to MPRIS,
  which Chrome serves for the playing tab [I]. So the launcher page itself may never see `MediaPlayPause` on the Spark
  [I].
- **Permissions.** Keyboard-type remotes are `root:input` [M]. Don't add `spark` to `input`, because then any process
  could read every keyboard. Use a per-device uaccess udev rule that sorts before `73-seat-late.rules`, or a hwdb
  remap. Both need root once [M]/[[D]][hwdb].
- **hwdb remaps.** `deploy/remotes.hwdb` is a template that only `install.sh root` installs. Property lines start with
  one space. Apply with `systemd-hwdb update --usr && udevadm trigger -s input` [[D]][hwdb]. `--usr` is what Ubuntu's
  udev package runs; without it the update writes `/etc/udev/hwdb.bin`, which libudev reads first and no distro update
  refreshes [M: `udev.postinst`, libudev's search order].

| Remote key | Default effect here | Remap to | Evidence |
|---|---|---|---|
| Home `c0223` (`KEY_HOMEPAGE`) | Chrome Home accelerator, unless SparkHome grabs it | `f14` (XF86Launch5) | [M]/[[D]][chrome-accel] |
| Back `c0224` (`KEY_BACK`) | Chrome history back | `esc` | [[D]][chrome-accel] |
| Search `c0221` (`KEY_SEARCH`) | the GNOME Overview opens over the kiosk | `f15` (XF86Launch6 = push-to-talk) | [M] |
| Config `c0183` and F13 | GNOME Settings opens | `f16` (the research's suggestion; the shipped template omits it) | [M] |
| Power `c0030` | GNOME power-off dialog | `reserved` (SparkHome's choice; the research suggested `f17`) | [M] |
| OK `c0041` (`KEY_SELECT`) | invisible to X11 | `enter` | [M]/[[D]][xorg-limit] |
| Voice `c00cf` (`KEY_VOICECOMMAND`) | invisible to X11 | `f15` (SparkHome's choice; the research suggested `f18`) | [M] |

### 2.5 Controllers, TV-style remotes, phones and microphones

**Gamepads.**

| Pad | Status here | Notes |
|---|---|---|
| **Xbox Wireless Controller** (BLE, 045E:0B13) | **paired and working** [M] | Needs the BlueZ `Privacy = device` and `JustWorksRepairing = always` settings [D: GameSpark]. USB-C works with `xpad` and needs no pairing. List price US$64.99 since May 2025, deals around $44 [[R]][xbox-price] |
| **DualSense** (054C:0CE6) | **paired over BT** [M] | Its touchpad moves the X pointer [I]. The microphone works **only over USB**, as a standard USB audio device [[R]][ds-audio]. Plugging it in by USB-C is a free push-to-talk mic for prototyping [I]. US$74.99 list [[R]][ds-price] |
| 8BitDo Ultimate 2C Wireless | not owned | The 2.4 GHz dongle in XInput mode binds to `xpad` (vendor-wide 2DC8 match in this kernel [M]). No Bluetooth and no MT7925 involved. US$29.99 at launch, $24–26 on deal [[R]][8bitdo-price] |
| Steam Controller (2026) | don't buy for the launcher | Kernel 6.17 has no driver; mainline 7.3 adds one [[D]][steamctl-commit]. Works through Steam Input only. US$99 [[R]][steamctl] |
| Switch 2 Pro | don't buy | No mainline driver; the patch series is at v13 and unmerged [[R]][switch2] |

**TV-style remotes with a microphone.**
- **G20S Pro, 2.4 GHz USB-dongle version** (≈US$16–30 [R]; the "Pro Plus" with 2.4G + BT is $43–48 on Newegg
  [[R]][newegg-g20s]):
  - This is the only voice remote with a Linux report of its dongle appearing as a **standard USB audio capture card**,
    recorded with `arecord -D hw:1,0 -f S16_LE -r 16000` [[R]][coreelec-g20s].
  - The same report describes a hard lock-up on an Amlogic box. That is probably platform-specific [R/I].
  - Its keycodes are unpublished [U]. The mic probably streams only while its button is held [R].
  - Buy a listing that says "2.4G" and "USB receiver". The "BT" variant uses ATVV, and Linux sees no mic from it
    [[R]][arch-g20sbt].
- **ATVV BLE remotes** (Google "ATV Voice", e.g. Xiaomi RC003, Onn) are feasible with a userspace BLE daemon and IMA ADPCM
  audio at 8–16 kHz [[D]][onn-remote]. BlueZ has no native support [[D]][bluez-1086].
- **Fire TV (Alexa) remote.** No Linux voice implementation exists [U]. Not recommended.
- **Mini keyboards** (Logitech K400 Plus, €24–50 [[R]][k400-price]; Rii i8+ [[R]][rii]) solve text entry but have no
  mic.

**Phone.**
- SparkHome serves its own phone remote. Buttons and text work over plain HTTP on the LAN.
- **iOS keyboard dictation** into a text field works over plain HTTP, because it is an OS keyboard feature [I, high
  confidence].
- Raw microphone capture with `getUserMedia` **needs HTTPS** [[D]][mdn-gum]. `tailscale serve` could provide it
  [[D]][ts-serve].

**Hands-free voice (phase 2).**
- **Mic:** the Seeed reSpeaker XVF3800 USB 4-Mic Array. It is USB Audio Class 2 and driverless, with on-chip AEC and
  beamforming [[D]][xvf3800-wiki]. US$49.99–60.99 bare, $62.99 with case [[R]][xvf3800-case].
- **The TV audio problem.** TV audio leaves over HDMI, so the board's own AEC has no reference signal. Use PipeWire's
  WebRTC echo-cancel module with `monitor.mode = true` instead [[D]][pw-aec]; it is present on the Spark [M].
- **Wake word:**
  - `pymicro-wakeword` 2.5.0 (2026-09-17) ships an aarch64 wheel [[M]][pymww].
  - Avoid Porcupine: free-tier keys stopped working after 2026-06-30 [[R]][porcupine].
  - openWakeWord's pre-trained models are CC BY-NC-SA [[R]][oww-license].
- **Order of work:** ship push-to-talk first, and add a wake word only after measuring false accepts with a movie playing
  [I].

**Acceptance test for any new remote** (about 10 minutes):
1. Run `lsusb` and `cat /proc/bus/input/devices`.
2. Read events and press every button, in both air-mouse modes. Note each `MSC_SCAN` and `KEY_*`. Use `python3 -m
   sparkhome.inputs --probe`, `sudo evtest`, or the root/udev path.
3. Check which key the mic button sends, and whether a release event arrives.
4. Record 60 s with `arecord -D plughw:CARD=<name>,DEV=0 -f S16_LE -r 16000 -c 1 -d 60`, check that the system doesn't
   stall, and transcribe the recording with whisper.cpp.
5. Pass when every button gives a stable, unique code, the mic works, and no GNOME dialog appears [I].

### 2.6 Buy list (2026 prices as reported)

| # | Item | Price | Why | Gate before relying on it |
|---|---|---|---|---|
| 1 | **FLIRC USB** (only if the remote is IR: A1156/A1294) | US$24.95 [M] ([flirc.tv][flirc]) | Turns the Apple remote into a plain keyboard. Independent of Bluetooth | every button gives exactly one key; long-press Home per §2.2 |
| 2 | **G20S Pro voice air mouse, 2.4 GHz dongle** | ≈US$16–30 [R] | Couch remote plus a push-to-talk mic over USB audio | the acceptance test above |
| 3 | **Pulse-Eight USB-CEC adapter** (P8-USBCECv1) | ≈US$48 at Pulse-Eight, £42.49 on Amazon UK, ≈$30 used [[R]][p8] [[R]][p8-uk] | TV on/standby and input switching, and the Sony remote drives SparkHome over CEC | the thermal test in §3; whether the Sony forwards keys [U] |
| 4 | reSpeaker XVF3800 (with case) | US$62.99 ($49.99–60.99 bare) [[R]][xvf3800-case] | Hands-free far-field mic (phase 2) | PipeWire AEC false-accept test |
| 5 | 8BitDo Ultimate 2C Wireless | US$24–30 [[R]][8bitdo-price] | Optional Bluetooth-free second pad | none |
| 6 | Logitech K400 Plus | €24–50 [[R]][k400-price] | Optional couch keyboard for logins | none |
| — | USB-C (male) → USB-A (female) adapter | a few US$ [U] | Only if the attached hub is full (§10 #3) | — |
| — | Smart plug | [U] | Cold boot from the couch (BIOS auto power-on) [[R]][wol] | — |

**Already owned, use first:** the Xbox controller (BLE, working), the DualSense (BT; USB for its mic), an iPhone (phone
remote and dictation) and the Apple remote.

**Don't buy:**
- Switch 2 Pro controller.
- Steam Controller 2026 as a launcher remote.
- Porcupine.
- Fire TV remote, for voice.
- Onn or Google TV remotes as the primary remote (the kernel rejects their descriptor [[D]][onn-remote]).
- XVF3000 v2.0, which is end-of-life [R].

[apple-identify]: https://support.apple.com/en-us/103233
[apple-pair]: https://support.apple.com/en-us/HT201254
[rc-protos]: https://docs.kernel.org/userspace-api/media/rc/rc-protos.html
[siriesp]: https://github.com/Jakkumn/SiriRemoteESP
[jackr1]: https://github.com/Jack-R1/SiriRemoteVoiceDecoder
[atv-boot]: https://github.com/lemonjesus/atv-bootloader/wiki/AppleIR_LIRC
[bluez-device]: https://github.com/bluez/bluez/blob/5.72/src/device.c
[bluez-hog]: https://github.com/bluez/bluez/blob/5.72/profiles/input/hog-lib.c
[bluez-commit]: https://github.com/bluez/bluez/commit/dbd6591bd1d0
[hid-input]: https://github.com/torvalds/linux/blob/master/drivers/hid/hid-input.c
[xorg-limit]: https://lists.freedesktop.org/archives/xorg/2010-April/049675.html
[chrome-accel]: https://github.com/chromium/chromium/blob/main/chrome/browser/ui/accelerator_table.cc
[xgrabkey]: https://tronche.com/gui/x/xlib/input/XGrabKey.html
[hwdb]: https://github.com/systemd/systemd/blob/v255/hwdb.d/60-keyboard.hwdb
[xbox-price]: https://www.gematsu.com/2025/05/xbox-series-global-price-increase-announced-for-consoles-controllers-and-games
[ds-audio]: https://phoronix.com/news/Sony-DualSense-Audio-Handling
[ds-price]: https://thecontrollerpeople.com/ps5-controller-pricing-in-2026/
[8bitdo-price]: https://www.techradar.com/gaming/the-8bitdo-ultimate-2c-could-be-the-cheap-pc-controller-youre-looking-for-when-it-launches-next-month
[steamctl]: https://www.gamingonlinux.com/2026/05/the-new-steam-controller-from-valve-is-out-now-some-early-thoughts/
[steamctl-commit]: https://github.com/torvalds/linux/commit/0a80b4e8ec6a6e40937c7abdf8fb2ebe6cc7c1e5
[switch2]: https://ratatoskr.run/linux-input/2026/08/17399481/t
[newegg-g20s]: https://www.newegg.com/greva-g20s-pro-plus/p/17A-00Y5-001X0
[coreelec-g20s]: https://discourse.coreelec.org/t/ugoos-sk1-s928x-hard-lockup-when-using-usb-microphone-g20s-pro/58460
[arch-g20sbt]: https://bbs.archlinux.org/viewtopic.php?id=285325
[onn-remote]: https://github.com/FloMaetschke/onn-remote-linux
[bluez-1086]: https://github.com/bluez/bluez/issues/1086
[k400-price]: https://www.tomshw.it/offerte/logitech-k400-plus-al-46-laffare-per-gestire-pc-e-tv-dal-divano
[rii]: https://thepihut.com/products/rii-i8-plus-mini-wireless-keyboard-with-touchpad
[mdn-gum]: https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia
[ts-serve]: https://tailscale.com/kb/1242/tailscale-serve
[xvf3800-wiki]: https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/
[xvf3800-case]: https://www.seeedstudio.com/ReSpeaker-XVF3800-USB-4-Mic-Array-With-Case-p-6490.html
[pw-aec]: https://docs.pipewire.org/page_module_echo_cancel.html
[pymww]: https://pypi.org/project/pymicro-wakeword/
[porcupine]: https://community.home-assistant.io/t/fyi-picovoice-confirmed-free-tier-accesskeys-will-stop-working-after-june-30-2026/1012744
[oww-license]: https://huggingface.co/davidscripka/openwakeword
[p8]: https://www.pulse-eight.com/p/104/usb-hdmi-cec-adapter
[p8-uk]: https://www.amazon.co.uk/Pulse-Eight-na-USB-CEC-Adapter/dp/B005JU6LWM

---

## 3. TV control, power and thermals

**Conclusion.**
- The only TV-control path known to fit this TV is **HDMI-CEC through a Pulse-Eight adapter**. The GB10 has no CEC, the
  TV is not on the network, and its model is unknown.
- "Off" can only mean **TV standby**, because the Spark never sleeps.
- **Test thermals before shipping any "TV off" feature.**

- **Pulse-Eight on this kernel.**
  - `pulse8-cec.ko` is on disk, and `inputattach` 1.8.1 is installed and supports `--pulse8-cec` [M].
  - The pieces are `inputattach --pulse8-cec /dev/ttyACMx` (a templated systemd service plus a udev rule) and `cec-ctl`
    from `v4l-utils` (apt; not installed) [[D]][cec-guide].
  - `/dev/cec*` belongs to group `video`, which `spark` is not in, so add a uaccess rule (root once) [M].
  - Pass-through goes up to 4K60 4:2:0, irrelevant on a 1080p TV [[D]][p8-4k].
- **Useful commands.** `[D]` marks commands from the kernel guide or man page; `[I]` marks spellings to confirm with
  `cec-ctl --help-all` [[D]][cec-ctl]:

  ```bash
  cec-ctl -d0 -p 1.0.0.0 --playback --osd-name SparkHome   # claim a Playback address at 1.0.0.0  [D/I]
  cec-ctl -d0 --to 0 --image-view-on                       # wake the TV            [D]
  cec-ctl -d0 --to 15 --active-source phys-addr=1.0.0.0    # switch to the Spark    [I]
  cec-ctl -d0 --to 0 --standby                             # TV off                 [I]
  cec-ctl -d0 -M                                           # watch what the Sony remote sends     [D]
  ```

- **The Sony remote over CEC.**
  - RC passthrough is on by default. The `rc-cec` keymap maps CEC Select to **`KEY_OK`**, which X11 can't see, so a
    helper must read that device with evdev [[D]][rc-cec].
  - Turn on "BRAVIA Sync" / "Control for HDMI" on the TV.
  - Whether this Sony forwards keys to a playback device is [U].
- **Sony IP control** works only if the TV is networked and from 2013 or later [[D]][ha-bravia]:
  - `POST http://<tv>/sony/system` with `{"method":"setPowerStatus",…}` and header `X-Auth-PSK` [[D]][sony-power].
  - `POST /sony/avContent` with `extInput:hdmi?port=1` switches input [[D]][sony-input].
  - The TV answered no SSDP [M], so this probably doesn't apply.
- **IR blaster fallback.** An mceusb transceiver with `ir-ctl -S sony12:0x10015` [[D]][ir-ctl]. That code is the power
  toggle in common IR databases [R, unverified].
- **Power.**
  - The Spark can't suspend or hibernate [M], and Wake-on-LAN is unsupported [[R]][wol].
  - It idles at about 37–40 W [R].
  - A smart plug plus the BIOS default "auto boot on power restore" gives a cold boot [R].
- **Thermal risk.**
  - A 2026 NVIDIA forum thread reports **fans stopping when the display goes dark**, on the same OS, kernel, driver and
    BIOS as this unit [[R]][fans]/[M].
  - A user tied it to `mtk_pcie_hotplug` removing an uncabled ConnectX-7 [R]. Here that module is loaded and no
    ConnectX-7 is present [M].
  - Today the screen never blanks [M]. Don't add display blanking.
  - Gate before any CEC standby feature: put the TV in standby for 30 minutes and log
    `/sys/class/thermal/thermal_zone*/temp` every 30 s.

[cec-guide]: https://www.kernel.org/doc/html/latest/admin-guide/media/cec.html
[cec-ctl]: https://man.archlinux.org/man/cec-ctl.1.en
[p8-4k]: https://support.pulse-eight.com/support/solutions/articles/30000053069-cec-adapter-4k-120hz-hdr-and-signal-issues-at-high-resolutions
[rc-cec]: https://github.com/torvalds/linux/blob/master/drivers/media/rc/keymaps/rc-cec.c
[ha-bravia]: https://www.home-assistant.io/integrations/braviatv/
[sony-power]: https://pro-bravia.sony.net/develop/integrate/rest-api/spec/service/system/v1_0/setPowerStatus/
[sony-input]: https://pro-bravia.sony.net/develop/integrate/rest-api/spec/service/avcontent/v1_0/setPlayContent/
[ir-ctl]: https://man.archlinux.org/man/ir-ctl.1.en
[fans]: https://forums.developer.nvidia.com/t/dgx-spark-fans-stop-when-the-screen-goes-dark-or-running-from-ssh-box-gets-too-hot-to-touch-fire-hazard/378945

---

## 4. Streaming

**Conclusion.**
- In Chrome on this box, **Netflix reaches 1080p stereo, Prime Video SD, and Disney+ about 480p, at most 720p.** Peacock
  probably won't play at all.
- **YouTube is excellent**, and it is the only service with a real TV interface in a browser.
- Spoofing the user agent can't raise DRM tiers.
- **The HD and surround path for premium services is a companion streamer** on another HDMI input.
- SparkHome shows honest quality badges on its tiles.

### 4.1 Why 4K/HDR (and most HD) is impossible here

1. **The TV is 1080p only.** Its EDID lists only 1080p modes [M], so 4K is moot whatever else changes.
2. **Widevine L3 only.**
   - `requestMediaKeySystemAccess` succeeds only for `SW_SECURE_CRYPTO` and `SW_SECURE_DECODE`; every `HW_SECURE_*`
     level fails [M].
   - Linux browsers are outside Widevine's verified media path, so services treat Linux Chrome as their lowest tier
     [[R]][wiki-widevine].
3. **No HDCP.**
   - `getStatusForPolicy` returns `output-restricted` for HDCP 1.0 through 2.3. That was measured headless; it must be
     re-checked in a window on the TV [M].
   - On non-ChromeOS platforms, Chromium's output-protection proxy reports no protection [[D]][out-prot].
   - Services that need verified HDCP cap or refuse: Hulu needs HDCP 1.4 for 720p [[D]][hulu-computers], and HBO Max
     needs HDCP 2.2 for 4K [[D]][max-4k].
4. **Codecs.** Chrome here decodes H.264, VP9 and AV1, but **not HEVC, AC-3 or E-AC-3** [M]. So there is no Dolby audio,
   and HEVC-only content won't play. Plex and Jellyfin will transcode.
5. **HDR.** The session is Xorg and the TV is SDR, so there is no HDR path [I].
6. **User-agent spoofing doesn't help.**
   - The Widevine licence request carries client capabilities (HDCP version, platform verification) that a page can't
     change [[D]][pywidevine].
   - A Disney+ user who spoofed Windows stayed at 480p [[R]][ltt-dp].
   - Chrome on arm64 already sends `X11; Linux x86_64` in its UA [M].

### 4.2 Per-service matrix (Chrome 155, Linux arm64, 1080p Sony)

"Search URL" is the deep-link template SparkHome uses. Each search URL was tested signed out from the Mac [M]. Signed-in
quality on the Spark is still to be measured (§11, open question 6).

| Service | Plays here? | Expected max | Audio | Search URL | Evidence |
|---|---|---|---|---|---|
| Netflix | yes, expected. `/browse` redirects to login with no "unsupported browser" message [M] | **1080p** (Chrome 117+ on Linux) | stereo. 5.1 needs Edge on Windows; no E-AC-3 [M] | `netflix.com/search?q=` (login keeps the target) [M] | [[D]][nf-browsers] [[D]][nf-51] |
| YouTube (TV UI) | **yes** [M]: the leanback UI loads with a TV UA, and a `#/watch` deep link played with 0 dropped frames | 1080p on this TV [I] | Opus/AAC stereo | `youtube.com/tv#/search?q=` works signed out [M] | [M] |
| YouTube TV | probably. The member UI renders at `/tv/upg` [M]; playback untested | ≤1080p [I]; needs location access [D] | stereo [I] | — | [[D]][yttv] |
| Disney+ | yes, unofficially | **480p observed on Linux** [R]. The browser ceiling is 720p [D] | stereo [D] | none (in-app routes 404 signed out) [M] | [[D]][dp-quality] [[R]][ltt-dp] |
| Hulu | unknown, probably | 720p only with verified HDCP 1.4, so **≤480p** here [D/I]. The standalone app is folding into Disney+ during 2026 [R] | stereo [I] | `hulu.com/search?q=` (redirects to marketing signed out) [M] | [[D]][hulu-computers] [[R]][hulu-merge] |
| HBO Max | unknown | ≤720p if it plays [I] | unknown | `/search` drops `q` [M] | [[D]][max-computers] |
| Prime Video | **yes** | **SD**: "other than Windows or macOS, playback is restricted to standard definition"; about 480–540p by bitrate [R] | stereo [D] | `primevideo.com/search/ref=atv_nb_sug?phrase=` renders publicly [M] | [[D]][prime-req] [[R]][prime-linux] |
| Apple TV | unknown. The web app names no OS [D] | ≤720p reported on Linux in older reports [R] | unknown | `tv.apple.com/us/search?term=` renders publicly [M] | [[D]][atv-web] [[R]][atv-1080] |
| Paramount+ | unknown; its help pages are stale [D] | unknown | unknown | `/search/?q=` renders; prefill unverified | [[D]][pp-help] |
| Peacock | **likely no.** Browsers are Windows/Mac only [D], and it is reported to refuse Linux [R] | — | — | `/watch/search?q=` (sign-in keeps the target) [M] | [[D]][peacock]; [R] (LTT forum, Ubuntu MATE forum, July 2026) |
| Crunchyroll | unknown | unknown | unknown | `/search?q=` renders [M] | [R] |
| Twitch | yes, no DRM [I] | source quality, commonly 1080p60 [I] | stereo | `/search?term=` [M] | [I] |
| Spotify Web | yes | — | **AAC 256 kbit/s** (Premium); not adjustable on the web | `open.spotify.com/search/{q}` [M] | [[D]][spotify-q] |
| Plex / Jellyfin web | yes [I] | H.264/VP9/AV1 play directly; **HEVC, AC-3 and E-AC-3 transcode** [M] | stereo or transcoded | Plex `#!/search?query=` [M] | [M] |
| Xbox Cloud | probably. No Linux row officially [D]; works per community tests [R] | 1080p [R]. Monthly hour caps from Nov 2026: Ultimate 15 h, Premium 10 h, Essential 5 h [D] | — | `xbox.com/en-US/play/search?q=` [M] | [[D]][xcloud-browsers] [[R]][xcloud-gol] [[D]][xcloud-caps] |
| GeForce NOW (web) | probably. The browser is officially supported only on Windows, macOS and ChromeOS, and the native Linux app is **x86_64 only** [D] | 1080p60 reported without a Windows UA (2024) [R] | — | — (login wall) [M] | [[D]][gfn-req] |
| Amazon Luna | unknown. The service was restructured in April 2026 [R] | cloud stream | — | `/search?query=` [M] | [[R]][luna] |

### 4.3 Getting around desktop sites from a couch

- **YouTube leanback** (`https://www.youtube.com/tv`) needs a TV user agent.
  - PS4/Cobalt, Tizen, webOS, Android TV and Xbox UAs all render "YouTube on TV"; a desktop UA redirects to the
    desktop site [M].
  - Synthetic `Input.dispatchKeyEvent` arrows arrive with `isTrusted:true` and move its focus [M].
  - Leanback's focus was **not** exposed as DOM focus or in the accessibility tree, so an agent couldn't read it from
    the DOM [M]. Re-measured 2026-10-08 on the welcome screen, it is: `document.activeElement` follows the D-pad ("Get
    started" → "Terms & Privacy") and `get_screen` reports `focused` 0 ("Get started"); on a search results page it
    reported no focused element [M].
  - Gamepad buttons map to `keyCode = 32768 + buttonIndex`; sticks map to virtual buttons 17–24 with a 500/100 ms repeat
    [[D]][vacuumtube].
  - A fresh profile shows an account picker before deep links. Signing in once with the TV-code flow avoids it [M]/[I].
- **No other service exposes a TV UI by user agent.** Netflix, Disney+, Prime, Spotify, Twitch and Plex return 404 or
  their desktop page [M].
- **Desktop sites** (Netflix, Disney+, Prime, HBO Max) work best with a pointer remote plus each site's keyboard
  shortcuts [I]. Netflix documents Space/Enter for play/pause, ←/→ to seek 10 s, F, M and S [[D]][nf-keys].
- **`--enable-spatial-navigation`** makes arrows move focus on any page [[D]][content-switches]. Expect partial results
  on complex single-page apps; test it for 10 minutes per service [I].
- **VacuumTube** (an Electron wrapper for leanback; arm64 `.deb`, AppImage and Flathub) is a drop-in alternative YouTube
  tile if the Chrome tab proves fiddly. It bundles ad-block and SponsorBlock modules, which are a ToS consideration
  [[D]][vacuumtube].

### 4.4 The companion streamer (the HD/surround path)

A Google TV Streamer, Apple TV 4K or Fire TV on another HDMI input gives **1080p with Dolby audio** for Disney+/Hulu,
Prime, Max, Paramount+, Apple TV and Peacock on this TV [I]. SparkHome could still drive it:
- **Google TV:** [`androidtvremote2`][atvremote2] needs no ADB. It handles pairing, keys and "send URLs to open apps"
  [D].
- **Apple TV:** [`pyatv`][pyatv] can launch apps and deep links [D].
- **Input switching:** CEC (Pulse-Eight) or Sony's REST API [D].

Check first whether the Sony TV is itself an Android/Google TV. If it is, its built-in apps are the companion. The model
is unknown (open question 2). Prices for these devices were not captured [U].

### 4.5 What doesn't help

- **Kodi.** The Netflix add-on suspended development and documents SD-only playback [[D]][kodi-nf]. Every add-on hits
  the same L3 cap [I].
- **Waydroid.** NVIDIA's driver gets software rendering only, playback is L3 at about 480–540p, and it needs Wayland
  [[R]][waydroid].
- **Native Linux apps.** Spotify, Plex HTPC and GeForce NOW are x86_64-only on Flathub or in their vendors' repos [D].
  Jellyfin Desktop does ship aarch64 [D].
- **`nvidia-vaapi-driver` 0.0.18** gained a Chromium path [[D]][nvvaapi]. It is untested on GB10 and optional:
  software decode handled 720p60 with zero dropped frames [M].

[wiki-widevine]: https://en.wikipedia.org/wiki/Widevine
[out-prot]: https://chromium.googlesource.com/chromium/src/+/HEAD/chrome/browser/media/output_protection_proxy.cc
[pywidevine]: https://github.com/devine-dl/pywidevine
[ltt-dp]: https://linustechtips.com/topic/1640895-disney-intentionally-limits-linux-browsers-to-480p-confirmed-by-support-my-investigation/
[nf-browsers]: https://help.netflix.com/en/node/23742
[nf-51]: https://help.netflix.com/en/node/14163
[nf-keys]: https://help.netflix.com/en/node/24855
[yttv]: https://support.google.com/youtubetv/answer/7129767
[dp-quality]: https://help.disneyplus.com/article/disneyplus-data-usage
[hulu-computers]: https://help.hulu.com/article/hulu-supported-computers
[hulu-merge]: https://www.tvline.com/news/hulu-app-disney-plus-combining-schedule-pricing-1235485863
[max-computers]: https://help.hbomax.com/us/Answer/Detail/000002506
[max-4k]: https://help.hbomax.com/us/Answer/Detail/000002523
[prime-req]: https://www.primevideo.com/help?nodeId=GUX9FYHU5D8LC9EJ
[prime-linux]: https://en.linuxadictos.com/My-latest-failure-trying-to-play-Prime-Video-at-1080p-on-Linux.html
[atv-web]: https://support.apple.com/guide/tvapp/get-the-apple-tv-app-and-sign-in-atvbf947803a/1.0/web/1.0
[atv-1080]: https://fabianwoedl.com/apple-tv-looks-bad/
[pp-help]: https://help.paramountplus.com/s/article/PD-Which-browsers-are-supported-by-Paramount
[peacock]: https://www.peacocktv.com/help/article/what-devices-and-platforms-are-supported-by-peacock
[spotify-q]: https://support.spotify.com/us/article/audio-quality/
[xcloud-browsers]: https://support.xbox.com/en-US/help/games-apps/cloud-gaming/supported-browsers
[xcloud-gol]: https://gamingonlinux.com/2022/11/microsoft-upgrades-xbox-cloud-gaming-for-linux-and-chromeos
[xcloud-caps]: https://news.xbox.com/en-us/2026/09/03/xbox-cloud-gaming-changes/
[gfn-req]: https://www.nvidia.com/en-us/geforce-now/system-reqs/
[luna]: https://en.wikipedia.org/wiki/Amazon_Luna
[vacuumtube]: https://github.com/shy1132/VacuumTube
[content-switches]: https://chromium.googlesource.com/chromium/src/+/HEAD/content/public/common/content_switches.cc
[atvremote2]: https://github.com/tronikos/androidtvremote2
[pyatv]: https://pyatv.dev/development/apps/
[kodi-nf]: https://github.com/CastagnaIT/plugin.video.netflix
[waydroid]: https://wiki.archlinux.org/title/Waydroid
[nvvaapi]: https://github.com/elFarto/nvidia-vaapi-driver

---

## 5. Chrome kiosk and CDP

**Conclusion.** Run one branded Chrome in `--kiosk` with its own profile, and drive it over CDP on a fixed localhost port.
Every piece of that was measured to work here. CDP is the most sensitive interface on the box: it is localhost-only and
reached only through an ssh tunnel.

**Measured.**
- CDP clients that send no `Origin` header (stdlib, Node) connect without `--remote-allow-origins`; a foreign `Origin`
  gets 403 [M].
- `Page.addScriptToEvaluateOnNewDocument` and `Runtime.addBinding` work [M]. Bindings avoid Chrome 142's Local Network
  Access prompts [D].
- `Input.dispatchKeyEvent` delivers arrows, Enter, Escape, Backspace and MediaPlayPause with `isTrusted:true` [M].
- Widevine licensing and playback work under CDP control: a Shaka Widevine test stream played [M].
- Headless screenshots at 1920×1080 took 250–273 ms as PNG and 39–51 ms as JPEG q70 [M].

**Flags.**
- **Required:** a non-default `--user-data-dir`. Since Chrome 136 remote debugging ignores the default profile
  [[D]][rdp-136].
- **Also used:** `--kiosk`, `--class=SparkHome` (a stable `WM_CLASS`), `--password-store=basic` (no keyring prompt
  under autologin [[D]][pwstore]), `--no-first-run`, `--no-default-browser-check`, `--noerrdialogs` and
  `--autoplay-policy=no-user-gesture-required`.
- **A fixed `--remote-debugging-port`** keeps `navigator.webdriver` false. Port 0, `--remote-debugging-pipe` and
  `--enable-automation` set it to true [[D]][runtime-features], and it was measured false with a fixed port [M].
- **Never use:**
  - `--remote-allow-origins` or `--remote-debugging-address=0.0.0.0`.
  - `--disable-component-update`: Widevine updates arrive through the component updater [M] (pointer file).
  - `--load-extension`: removed from branded Chrome 137 [[D]][loadext].
  - `--headless` for the kiosk.
- **Unresolved:** the crash-restore bubble switch (§10 #7).

**Security.**
- Anything that can reach the CDP port can drive the browser and read the cookies of every signed-in service. CDP has no
  authentication [[D]][rdp-136].
- Chrome binds CDP to 127.0.0.1 [M]. Never forward it to the LAN.
- `--password-store=basic` encrypts cookies with a fixed, well-known key, so anyone who can read the profile can lift
  sessions [[D]][pwstore]/[I]. That is acceptable on a single-user TV box that already logs in automatically.

**Operations.**
- Chrome now ships every two weeks (153 on 2026-09-08, 154 on 09-22, 155 on 10-06) [[D]][chrome-2wk].
- Restart the kiosk when idle after an apt update, and re-run the smoke tests [I].
- Managed policies (`/etc/opt/chrome/policies/managed/`, root) can set `AutoplayAllowed`, `TranslateEnabled` and
  `CommandLineFlagSecurityWarningsEnabled:false`. The last one hides the yellow "unsupported flag" bar [[D]][policies].

**Not yet verified on the TV.**
- Whether `Target.activateTarget` or `Page.bringToFront` visibly switches tabs in `--kiosk`. Fall back to `--app`
  windows if not [U].
- Whether DRM video appears in screenshots [U].
- Whether the headful window is GPU-composited. Headless used SwiftShader [M].

[rdp-136]: https://developer.chrome.com/blog/remote-debugging-port
[pwstore]: https://chromium.googlesource.com/chromium/src/+/HEAD/docs/linux/password_storage.md
[runtime-features]: https://chromium.googlesource.com/chromium/src/+/HEAD/content/child/runtime_features.cc
[loadext]: https://groups.google.com/a/chromium.org/g/chromium-extensions/c/1-g8EFx2BBY/m/S0ET5wPjCAAJ
[chrome-2wk]: https://developer.chrome.com/blog/chrome-two-week-release
[policies]: https://chromium.googlesource.com/chromium/src/+/HEAD/components/policy/resources/templates/policies.yaml

---

## 6. Games

**Conclusion.**
- **Steam works today and stays row 1.** SparkHome drives it exactly as GameSpark does.
- **Everything else is a per-user Flatpak install** away, once root installs `flatpak`.
- **Pin emulators to the X925 cores.**
- **Cloud gaming runs as browser tiles.**
- **Leave Switch emulation out.**

### 6.1 Steam today: launch, detect, exit, art

- **Measured performance (user):** Cyberpunk 2077 at 59–69 fps and RDR2 at about 75 fps [M].
- **Launch.**
  - GameSpark's `steam_launch <appid>` writes `-applaunch <appid>` to `~/snap/steam/common/.steam/steam.pipe`.
  - It confirms the launch on the `ExecCommandLine` line of `console_log.txt` and retries 4×, because Steam drops pipe
    commands while loading [D: GameSpark `lib/env.sh`].
  - At boot, Steam ignores the pipe until "Loading user data" finishes, so GameSpark's console script waits up to 180 s
    for the webhelper and the pipe, and then 20 s more [M].
- **Detect.**
  - `console_log.txt` logs `Game process added : AppID N` and `Game process removed: AppID N` [M].
  - Proton game windows have `WM_CLASS steam_app_<appid>`: documented in Valve's Wine [[D]][wine-wmclass], and seen for
    DOS2 on this box [M].
  - Big Picture is `steamwebhelper`/`steam`, titled "Steam Big Picture Mode" [M].
  - `registry.vdf` has no `RunningAppID` key here, so don't rely on it [M].
- **Exit.**
  - The user quits through the Steam overlay [I].
  - Forcing an exit means SIGTERM to the PIDs tracked in `gameprocess_log.txt`. That is untested, and unsaved progress
    is lost [I].
- **Art.**
  - `appcache/librarycache/<appid>/` holds art for every game in the library, not just installed ones [M].
  - It uses **two layouts**: hashed subfolders (`library_capsule.jpg`, `library_header.jpg`) and flat files
    (`library_600x900.jpg`, `header.jpg`). Glob both [M].
  - Local files are 1x only: hero 1920×620, capsule 300×450 (even when named 600x900), logo 640×360, header 460×215 [M].
  - The Steam CDN serves 2x files (`library_600x900_2x.jpg`, `logo_2x.png`, and `library_hero_2x.jpg` for some apps)
    [M].
- **Valve on ARM.**
  - Valve's CDN serves native `linuxarm64` client builds in lockstep with x86: 1788652215, 2026-09-05 [M].
  - Valve staff still say "Steam for Linux is not currently supported on ARM" [[D]][steam-arm].
  - Canonical's snap has been stable on arm64 since 2026-06-02 and names the DGX Spark [[D]][canonical-snap].
  - **Keep the snap** [I]. SparkHome's Steam adapter needs only the pipe and log paths, so a later switch is a config
    change.

### 6.2 Tiles to add (ranked) and how each behaves here

SparkHome's Games row already lists the first six. Each tile stays hidden until its Flatpak is installed. The quality
column is for 1080p60 on this TV. Nothing below was run on the Spark yet, so quality is inferred from the CPU class and
mature ARM64 JITs unless marked.

| # | Tile | Install (Flathub `--user` unless noted) | Expected | Notes |
|---|---|---|---|---|
| 1 | Steam Big Picture | already there (`steam://open/bigpicture`) | works [M] | Steam's own UI |
| 2 | **RetroArch** (NES → PS1, N64, GBA, DS, arcade) | `org.libretro.RetroArch` 1.22.2 [M] | excellent [I] | Controller-first menus. 226 nightly aarch64 cores (built 2026-10-08) [M]. UDP `QUIT` to 127.0.0.1:55355 after `network_cmd_enable = "true"` [[D]][ra-net] |
| 3 | **Dolphin** (GameCube/Wii) | `org.DolphinEmu.dolphin-emu` 2609 [M] | very good [I] | Flathub is the only arm64 build; apt has none [M] |
| 4 | **PPSSPP** (PSP) | `org.ppsspp.PPSSPP` 1.20.4 [M] | excellent [I] | ARM-first emulator |
| 5 | **Prism Launcher** (Minecraft Java) | `org.prismlauncher.PrismLauncher` 11.1.1 [M] | excellent [I] | Prism's metadata ships linux-arm64 LWJGL natives; Mojang's doesn't [M] |
| 6 | **SuperTuxKart** | `net.supertuxkart.SuperTuxKart` 1.5 [M] | excellent [I] | full gamepad support |
| 7 | **Moonlight** (stream from a gaming PC, if one exists) | `com.moonlight_stream.Moonlight` 6.2.0 [M] | excellent with a host [I] | GB10 exposes Vulkan Video decode [M]. Run with `--env=PREFER_VULKAN=1 --env=VULKAN_IS_SLOW=0 --env=GL_IS_SLOW=0` [[D]][moonlight-62] |
| 8 | Xbox Cloud, GeForce NOW, Luna | web tiles in the kiosk (§4.2) | 1080p for xCloud [R]; others unknown | Greenlight (`io.github.unknownskl.greenlight`, aarch64 on Flathub [M]) is an unofficial xCloud client [[D]][greenlight] |
| 9 | DuckStation (PS1) | official `DuckStation-arm64.AppImage`, extracted. The Flathub copy is EOL [M] | excellent [I] | Its CC-BY-NC-ND licence forbids repackaging, so download at install time [[D]][duckstation] |
| 10 | RPCS3 (PS3) | `net.rpcs3.RPCS3`, or the arm64 AppImage | good per title. Skate 3 at 1080p ~60 fps on another GB10 box [[R]][hothw] | some titles are broken on ARM64 [[D]][rpcs3-broken] |
| 11 | melonDS, Azahar (3DS), Flycast, xemu | Flathub aarch64 [M] | very good to excellent [I]; xemu Forza capped at 30 fps on a GB10 box [R] | need BIOS or dumps from hardware the user owns [D] |

**Don't ship by default:**
- **Switch emulators.** Nintendo's DMCA notice of 2026-02-12 targets them [[D]][dmca], and Eden's release repo now
  returns HTTP 451 [M].
- **x86_64-only software:** shadPS4, Cemu, the GeForce NOW app, Steam Link, Heroic and Lutris [M].

**Notes.**
- **Pinning.** The SparkHome tile pins RetroArch with `taskset -c 5-9,15-19`. GameSpark measured core placement at +10 %
  in Cyberpunk and −3 % in Tomb Raider [M], so measure per emulator before pinning the others.
- **Chrome cloud tiles** decode in software: there is no VA-API here. Moonlight is the only streaming path here that can
  use the GPU decoder [I].
- **Steam Input** can double-drive non-Steam emulators while Steam runs. Test it (§8).
- **Art for non-Steam tiles:** the Flathub appstream API (icons, 1920×1080 screenshots) [M]; libretro thumbnails for ROMs
  [[D]][ra-thumbs]; SteamGridDB with the user's own API key, cached locally and never redistributed [[D]][sgdb]/[I].

### 6.3 Install commands

Run as `spark` in the GNOME session. Stage 0 needs the user's sudo password once.

```bash
# Stage 0 (root, once)
sudo apt install -y flatpak libfuse2t64

# Stage 1 (per user; no root afterwards)
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
V=$(sed 's/\./-/g' /sys/module/nvidia/version)                 # 580-173-02 today
flatpak install --user -y flathub "org.freedesktop.Platform.GL.nvidia-$V//1.4"   # must match the host driver
flatpak install --user -y flathub org.libretro.RetroArch org.DolphinEmu.dolphin-emu org.ppsspp.PPSSPP \
  org.prismlauncher.PrismLauncher net.supertuxkart.SuperTuxKart com.moonlight_stream.Moonlight
for app in org.libretro.RetroArch org.DolphinEmu.dolphin-emu org.ppsspp.PPSSPP; do   # ROMs, read-only
  flatpak override --user --filesystem="$HOME/Games:ro" "$app"
done
# optional: net.rpcs3.RPCS3 net.kuribo64.melonDS org.azahar_emu.Azahar org.flycast.Flycast app.xemu.xemu \
#           io.github.unknownskl.greenlight  (add exec tiles for them in sparkhome.json)

# Stage 2: RetroArch cores (or use RetroArch's Online Updater). Confirm the cores path after the first launch [I].
CORES=$HOME/.var/app/org.libretro.RetroArch/config/retroarch/cores; mkdir -p "$CORES"; T=$(mktemp -d)
for c in snes9x mesen genesis_plus_gx mgba swanstation parallel_n64 melonds fbneo flycast ppsspp; do
  curl -fsSL "https://buildbot.libretro.com/nightly/linux/aarch64/latest/${c}_libretro.so.zip" -o "$T/c.zip" \
    && unzip -oq "$T/c.zip" -d "$CORES"
done; rm -rf "$T"

# Stage 3 (optional): DuckStation's official AppImage, extracted, so no FUSE is needed
mkdir -p ~/Applications && cd ~/Applications
curl -fLO https://github.com/stenzek/duckstation/releases/download/latest/DuckStation-arm64.AppImage
chmod +x DuckStation-arm64.AppImage && ./DuckStation-arm64.AppImage --appimage-extract >/dev/null \
  && rm -rf duckstation && mv squashfs-root duckstation && rm DuckStation-arm64.AppImage   # run duckstation/AppRun
```

- **Driver updates.** After apt moves the host driver to a new 580.x, run `flatpak update --user -y`. Otherwise Flatpak
  games fall back to software rendering [I]. Flathub already builds the next driver (580.178.04) for aarch64 [M].
- **Vulkan device check.** Run `flatpak run --command=sh org.libretro.RetroArch -c 'vulkaninfo --summary'`. It should list
  GB10, not llvmpipe. GameSpark saw RDR2 fall back to llvmpipe under FEX [M].
- **Snap fallback** (root): `sudo snap install retroarch supertuxkart` [M: arm64 channels]. The tiles' `flatpak run` argv
  would then need editing.

[wine-wmclass]: https://github.com/ValveSoftware/wine
[steam-arm]: https://github.com/ValveSoftware/steam-for-linux/issues/13451
[canonical-snap]: https://discourse.ubuntu.com/t/canonical-s-steam-snap-is-now-stable-on-arm64/83664
[ra-net]: https://docs.libretro.com/development/retroarch/network-control-interface/
[ra-thumbs]: https://docs.libretro.com/guides/roms-playlists-thumbnails/
[moonlight-62]: https://github.com/moonlight-stream/moonlight-qt/releases/tag/v6.2.0
[greenlight]: https://github.com/unknownskl/greenlight
[duckstation]: https://github.com/stenzek/duckstation
[hothw]: https://hothardware.com/news/dgx-spark-gaming-tests
[rpcs3-broken]: https://github.com/RPCS3/rpcs3/issues/16484
[dmca]: https://github.com/github/dmca/blob/master/2026/02/2026-02-12-nintendo.md
[sgdb]: https://github.com/SteamGridDB/node-steamgriddb

---

## 7. Voice, agent and computer use

**Conclusion.**
- **Voice is push-to-talk.** A resident whisper-server transcribes, a deterministic fast path catches common commands in
  milliseconds, and a small local model handles the rest.
- **Agents drive SparkHome through semantic tools**, not pixels. The same tools are exposed over MCP.
- **The measured parts add up to about 0.3–0.5 s from button release to action on the fast path**, and about 0.7–1.1 s
  when the model is needed.

### 7.1 Measured on this Spark

| What | Result | Evidence |
|---|---|---|
| whisper.cpp large-v3-turbo-q8_0 (CUDA) | Resident `whisper-server`: **0.16 s** per 2.69 s utterance after the first request (0.259 s); ready 0.52 s after start. A fresh `whisper-cli` takes ~0.91 s warm and 1.74 s the first time. Transcript exact. All runs contended | [M] |
| whisper-server API | `POST /inference` multipart `file`, `response_format=json`, `temperature=0.0` returns `{"text": …}`. Needs 16 kHz mono WAV; `--convert` needs ffmpeg, which is absent | [M] |
| Ollama context default | 0.32.5 gives the 4B model a **262,144-token context = 42.53 GB** (10.9 s load). With `num_ctx 8192`: **3.87 GB**, 3.1 s load. The `/v1` endpoint can't set `num_ctx` | [M]; [[D]][ollama-ctx] |
| `qwen3:4b-instruct-2507-q4_K_M` | Correct tool for **5 of 6** commands; "search youtube for lofi beats" needed a **two-turn loop** (open, then search; 1.95 s for three calls, contended). Warm tool call **0.40–0.60 s** uncontended (decode 65–78 tok/s); 0.49–0.85 s contended (47–50 tok/s). It answered a non-tool question in text | [M] |
| `qwen3-vl:8b-instruct` | Same tool choices, 0.7–1.2 s per call (contended). On a 1080p leanback screenshot it **located the focused button almost exactly** but **chose the wrong move**. First image prompt 6.75 s, 1.5 s when cached | [M] |
| Fast path | Regex templates plus a fuzzy title match: ~3 ms per query over 513 titles (stdlib difflib, measured on the Mac) | [M] |
| Piper TTS | 1.02 s wall for a 2.69 s phrase, including Python start-up (CPU) | [M] |

### 7.2 Latency budget (button release → action on the TV)

| Stage | Fast path | LLM path | Evidence |
|---|---|---|---|
| Release reaches the daemon | ~10–20 ms | same | [I] BLE connection intervals |
| Stop recording (tail so the last phoneme isn't clipped) | ~100 ms | same | [I] design knob |
| ASR, resident whisper-server | **0.16 s** | same | [M] (contended) |
| Intent | ~3 ms fast path | + **0.40–0.60 s** per tool call (Ollama 4B, warm) | [M] |
| Dispatch + render + TV processing | ~40–150 ms | same | [I] 1.5–3 frames; the TV's picture mode decides 15–100 ms |
| **Total** | **≈0.3–0.5 s** | **≈0.7–1.1 s** (two-step tasks ≈2 s) | [I] from the measured parts |

First-use costs:
- whisper-server starts lazily: ready in 0.52 s [M], and SparkHome waits up to 10 s for it.
- An evicted model reloads in 3.1 s [M]. SparkHome keeps it for 30 minutes (`keep_alive`).
- Leaving model "thinking" on would add 2–20 s, so SparkHome always sends `think:false` [I]/[[D]][nemotron-card].

For comparison:
- A browser DOM-agent step costs about 1–2.5 s [I].
- A pixel computer-use step costs **3.3–6.8 s** even for the best-optimised local model on a Spark (Holo3.1-35B-A3B, FP8
  vs NVFP4 plus NVIDIA's harness) [[D]][holo31].

### 7.3 Model choices

| Role | Choice | Why | Evidence |
|---|---|---|---|
| **Default agent model** | Ollama `qwen3:4b-instruct-2507-q4_K_M`, native `/api/chat`, `num_ctx` 8192, `think:false`, temperature 0 | Measured here (§7.1): small (3.87 GB), resident, 0.4–0.6 s. BFCL v4 Non-Live AST 87.88, Live 76.39 | [M]; [[D]][bfcl] |
| A/B upgrade | vLLM **Nemotron 3.5 Lightning 30B-A3B NVFP4** through `llm.api: "openai"`. Alternative: **Qwen3.6-35B-A3B NVFP4** | Nemotron 101.6 tok/s ([M] user); 81–124 tok/s on a Spark depending on speculative decoding [R]. At SparkHome's settings: 0.68–0.82 s per tool round with DSpark, but no prefix-cache reuse; the shipped unit drops DSpark so later rounds reuse the prompt (−10 % per request) ([M], below). Qwen3.6 77–108 tok/s [R]. Nemotron has **no published BFCL score**. Choose with a SparkHome eval set of 150–300 real phrasings, not leaderboards [I] | [[R]][classmethod-nemo] [[R]][classmethod-qwen] [[D]][nemotron-card] |
| Vision (grounding only) | `qwen3-vl:8b-instruct` (already pulled) | Localises well, plans badly [M] | [M] |
| Pixel computer-use | Not in v1. Later: Holo3.1-4B/9B or Qwen3.5-9B for background tasks | 3.3–6.8 s per step; collapses on D-pad UIs (§7.4) | [[D]][holo31] |
| ASR | whisper.cpp large-v3-turbo (installed). Upgrade: **Parakeet-TDT-0.6B-v2** (English WER 6.05 vs ~7.6–7.8 for turbo), on CPU via onnx-asr to stay off the GPU during games | Already measured here | [M]; [[R]][open-asr] |
| Live captions (optional) | Nemotron Speech Streaming EN 0.6B (its card lists DGX Spark among the test hardware) | Partial transcripts while the button is held | [[D]][nemotron-asr] |
| TTS | Piper (installed, CPU). Off the critical path, and never spoken while a game or video plays | No GPU contention | [M]; [[D]][piper] |
| Don't use | `gemma3:1b` (no tools); Porcupine (free tier gone) | — | [M]; [[R]][porcupine] |

#### Nemotron 3.5 Lightning at SparkHome's settings (measured 2026-10-09)

**Setup.** The unit is [deploy/sparkhome-llm-big.service](../deploy/sparkhome-llm-big.service): the model card's DGX Spark
recipe (vLLM v0.27.1, NVFP4 with the Marlin MoE backend), cut down to share the GB10, and **without the recipe's DSpark
speculative decoding**, so that the prefix cache works (see Findings).
- **Endpoint:** `127.0.0.1:8001` only, model id `nemotron-3.5-lightning`.
- **Context:** 16K, 8 sequences.
- **KV pool:** fixed at 4 GiB.
- **Not enabled at boot.** Start and stop it as the [README](../README.md#agents-and-mcp) shows.
- **Thinking off by default** (`--default-chat-template-kwargs '{"enable_thinking": false}'`).
- **Tools:** `--tool-call-parser qwen3_coder --reasoning-parser nemotron_v3`.
- **Prefix cache:** `--prefix-match-unit 16`, so a later round reuses the previous round's prompt.

**Test conditions.**
- The same request both ways: SparkHome's real system prompt, its 13 tool schemas and the launcher screen. That is 2,852
  prompt tokens for Nemotron and 2,477 for qwen3:4b.
- 8 phrasings. Divinity: Original Sin 2 and the Rockstar launcher were open on the Spark throughout.
- "Clean" means no other LLM traffic was running.
- The table measures the recipe's DSpark config. The shipped config is compared with it under Findings.

| What | Nemotron 3.5 Lightning, vLLM + DSpark | qwen3:4b, Ollama | Evidence |
|---|---|---|---|
| Cold start to `/health` | **271 s** first ever. **214–313 s** after that, even with the compile cache warm and the weights in page cache: loading the weights alone takes 108–158 s and is CPU-bound (the files read at 6 GB/s) | 3.1 s load (13.5 s for qwen3:30b) | [M] |
| Resident | **24.8–25.8 GiB** (nvidia-smi 25,413–26,419 MiB; it grows about 1 GiB under load; `free` "used" +29–36 GiB). At `--gpu-memory-utilization 0.30` with no KV cap it took **35.7 GiB** ("used" +41 GiB), with 14.7 GiB of KV = 734K tokens, 45× what 16K needs | 3.9 GB | [M] |
| Idle | 0 % SM, about 2.5 % of one CPU | — | [M] |
| One tool round, clean and warm (TTFT / done) | **0.52–0.66 s / 0.68–0.82 s** | — / **0.61–0.89 s** (Ollama doesn't stream tool calls) | [M] |
| Decode | **194–218 tok/s** on tool-call JSON (draft acceptance 72–95 %); **66–74 tok/s** on prose (about 30 %) | 58–62 tok/s | [M] |
| Whole command through `Agent._llm` (fake shell, 6 phrasings × 2, clean) | **1.29–3.75 s**, 2–5 rounds. Every round 0.62–0.85 s | **1.16–2.09 s**, 2–3 rounds. Later rounds 0.33–0.71 s | [M] |
| Thinking on (`enable_thinking: true`) | 55–219 output tokens instead of 13–26, and 1.6–4.7 s per round (contended). The reasoning arrives in `message.reasoning`, and `tool_calls` are still parsed | — | [M] |

**Findings.**
- **With DSpark, the prefix cache never hits at SparkHome's prompt sizes** [M]. That was the main latency gap: every
  round prefilled from scratch. Ollama reuses the previous round's prefix, so qwen3:4b's later rounds cost 0.33–0.71 s.
  - **Cause** [M]: two rules combine.
    1. The hybrid Mamba layers force a 4,240-token cache block ("Setting attention block size to 4240").
    2. DSpark counts as EAGLE-style speculative decoding, so vLLM drops the last matched block for the drafter.
  - So a hit needs **8,480 shared tokens** and then reuses 4,240. With identical prompts sent twice:
    - 6,333 tokens: cached 0.
    - 10,981 tokens: cached 4,240.
    - 15,629 tokens: cached 8,480.
  - SparkHome's 2.8–3.1K-token rounds never hit. The eval stream saw 0 hits in 296 rounds of about 4K tokens.
  - **Fix, now in the unit** [M]: no speculative decoding, plus `--prefix-match-unit 16`.
    - Later rounds then reuse nearly the whole previous prompt (cached 2,848 of 2,912 tokens).
    - A repeated 2,847-token prompt drops from 0.83 s to 0.15 s.
    - The price is decode speed: there are no draft tokens, so first rounds are slower.
    - The eval stream's 264-case A/B on the same code, against the DSpark config (contended):
      - Accuracy: 216 vs 213 passed. 7 cases flipped one way or the other, which is noise.
      - Per round, p50: 1,127 vs 1,444 ms (−22 %).
        - Later rounds: 864 vs 1,426 ms (−39 %), with 92 % of the prompt cached.
        - First rounds: 1,698 vs 1,478 ms (+15 %). Nothing is reused across requests, not even the system prompt.
      - Per request: p50 2,598 vs 2,893 ms (−10 %); p95 6,918 vs 8,451 ms (−18 %).
    - My own side-by-side (6 phrasings, first-time prompts, contended): whole commands 16.0 s vs 18.1 s (−12 %). Tool
      choices didn't change.
    - Resident: 24.4 GiB, since there's no drafter. The first start, on empty caches, took 327 s.
  - **Tried and failed:**
    - `--prefix-match-unit 16` with DSpark kills the engine on the first request: "SlidingWindowManager does not support
      fine-grained (partial) cache hits". The drafter uses 1,024-token sliding-window attention.
    - Built-in MTP instead of DSpark fails at start-up: "moe_backend='marlin' is not supported for unquantized MoE". The
      MTP layer is unquantized, and the GB10 needs Marlin for NVFP4.
- **Behaviour on the same 6 phrasings** (DSpark config; a smoke test, not an eval):
  - Both models opened the app first. qwen3:4b then always called `search` and stopped.
  - Nemotron used `search`, or typed into the page (`type_text`), then pressed play (`media`). That needed more rounds.
  - On "find the office on peacock", Nemotron took two extra rounds (`get_screen`, then `screenshot`) and ended with
    "Screenshot saved…".
  - At temperature 0 Nemotron's choice still varied between runs: "go back" gave `back` in one run and a sentence in
    another [M].
- **Server defaults.** `generation_config.json` makes temperature 1.0 and top_p 0.95 the server defaults, so a client
  must send `temperature: 0` (agent.py does).
- **No short thinking with DSpark.** In this image DSpark runs on vLLM's Model Runner V2, which doesn't support
  `thinking_token_budget`, per the start-up log [M]. So thinking is either off or full. It is untested on the shipped
  config [U].
- **qwen3:30b-a3b-instruct-2507 on Ollama** (pulled 18 GB; 18.3 GiB resident at `num_ctx` 8192):
  - Every run overlapped some other LLM traffic. At best: 0.69–1.0 s per tool round, 67–71 tok/s.
  - With heavier overlapping traffic: 20–37 tok/s, and 2.7–6.5 s per command.
  - It played the first search result with `click`. It invented a GPU temperature that the screen didn't show ("68°C";
    the fake shell reports 41).
- **Port 8000 conflict.** Another project's model scripts start the same model on port 8000 at 0.80 of memory.
  Never run both at once: the second instance asks for about 97 GiB.

### 7.4 Why semantic tools beat pixel computer-use (TVWorld)

TVWorld is the only benchmark of agents on TV-style D-pad interfaces [[R]][tvworld]. It uses Google-TV-derived screens
and the actions UP, DOWN, LEFT, RIGHT, OK, EXIT and HOME.

| Model | TVWorld-N task success |
|---|---|
| TVTheseus (Qwen3-VL-8B, fine-tuned) | 68.3 % |
| Gemini 3 Flash | 66.4 % |
| GPT-5 mini | 60.2 % |
| Qwen3-VL-32B | 39.0 % |
| Claude Haiku 4.5 | 25.4 % |
| Qwen3-VL-8B | 20.0 % |
| OpenCUA-7B | 5.0 % |
| UI-TARS-1.5-7B | 1.6 % |

General GUI agents fail on TV interfaces because they don't track focus or plan key sequences. Models trained on
pointers degrade sharply [R]. The lab saw the same thing (§7.1): a vision model found the focus but planned the wrong
move [M]. Leanback hides its focus from the DOM and the AX tree [M].

**What SparkHome does instead** [I]:
- It owns the launcher, so it exposes **semantic tools**: `get_screen`, `open_app`, `navigate`, `select`, `back`,
  `home`, `search`, `media`, `volume`, `type_text`, `click`, `screenshot`, `status` and `say`.
- Its own focus model is the ground truth.
- Web tiles get DOM text plus a list of clickable elements via CDP.
- Pixels (`screenshot`) are the last resort.

Home Assistant's Assist does the same: a local template matcher first, the LLM only for what it can't parse
[[D]][ha-local]. Keep risky actions out of the fast path [R].

### 7.5 MCP notes

- **Current spec revision: `2026-07-28`, which is stateless.** There is no `initialize` and no session ID. Every POST
  carries `MCP-Protocol-Version` and `Mcp-Method` (plus `Mcp-Name` for `tools/call`) and `params._meta` with the
  protocol version and client capabilities. Results carry `"resultType": "complete"`. `server/discover` is mandatory
  [[D]][mcp-spec].
- **Cacheable results need `ttlMs` and `cacheScope`.** The official Python SDK 2.3.0 client **rejects** a `tools/list`
  result without them [M].
- **Legacy revisions (2025-11-25, 2025-06-18, 2025-03-26)** use the `initialize` → `notifications/initialized`
  handshake [[D]][mcp-legacy]. One endpoint can serve both eras.
- **Errors.**
  - Header/body mismatch → 400 with −32020; unsupported version → 400 with −32022 listing the supported versions.
  - Unknown method → 404 with −32601.
  - Bad `Origin` → 403 (servers **must** validate it). GET/DELETE on a stateless server → 405.
  - Tool failures → 200 with `isError: true`, so the model can correct itself [[D]][mcp-spec].
- **SparkHome's server** is a port of the research's stdlib reference (`mcp_min.py`). That reference interoperates with
  the official Python SDK 2.3.0 and TypeScript SDK 2.3.1 clients in auto, pinned-modern and legacy modes [M].
- **Claude Code**'s v2 runtime negotiates 2026-07-28 when the server supports it [[D]][cc-mcp]. SparkHome serves `/mcp` to
  loopback only, so remote agents come in through an ssh tunnel:

  ```bash
  ssh -N -L 8780:127.0.0.1:8780 spark
  claude mcp add --transport http sparkhome http://127.0.0.1:8780/mcp
  ```

- **For third-party web UIs**, Playwright MCP can attach to the kiosk with `--cdp-endpoint` and drive it through
  accessibility snapshots [[D]][playwright-mcp]. Run it on the Spark: CDP stays localhost-only.

### 7.6 Sharing the GB10 with games

- **Memory.** One LPDDR5x pool (~208 GB/s measured, [M] user) serves CPU, GPU, games and models.
- **Bandwidth.** A 3B-active 4-bit model decoding at ~100 tok/s streams roughly 150 GB/s, which saturates the bus during
  a burst [I]. An idle resident model costs memory but almost no bandwidth [I].
- **No priority knob.** No cross-process GPU priority between a Vulkan game and CUDA inference was found for GB10 [U].
  Rely on gating, not scheduling.
- **SparkHome's policy:**
  - While a game or app is in front, only the fast path runs (`gate_in_app`), and `say` stays silent.
  - The 4B model is small enough to stay resident [I].
- **If vLLM is used**, cut `--gpu-memory-utilization` from the recipes' 0.85–0.91 to about 0.30, which is enough for a
  16k context [[D]][vllm-spark]/[I]. Also cap the KV pool: at 0.30 alone vLLM fills the share (Nemotron: 35.7 GiB), and
  with `--kv-cache-memory-bytes 4G` it uses about 25 GiB [M]. Never run two big vLLM containers at once.
- **Ollama knobs:** `keep_alive` per request, `/api/ps` to inspect, `ollama stop` to unload [[D]][ollama-faq].
- **Pin** CPU-side speech services to the A725 cores (0–4, 10–14) and leave the X925 cores to games [I].
- **Measure** frame-time p99 in a heavy game while firing 20 LLM calls [I]. No published study exists [U].

[ollama-ctx]: https://docs.ollama.com/context-length
[nemotron-card]: https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4
[holo31]: https://huggingface.co/blog/Hcompany/holo31
[bfcl]: https://gorilla.cs.berkeley.edu/leaderboard.html
[classmethod-nemo]: https://dev.classmethod.jp/articles/nvidia-nemotron-3-5-lightning-dspark-dflash-mtp-dgx-spark/
[classmethod-qwen]: https://dev.classmethod.jp/en/articles/dgx-spark-qwen3-6-35b-a3b-nvfp4-new-champion/
[open-asr]: https://arxiv.org/abs/2510.06961
[nemotron-asr]: https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b
[piper]: https://github.com/OHF-Voice/piper1-gpl
[tvworld]: https://arxiv.org/abs/2601.13142
[ha-local]: https://www.home-assistant.io/blog/2025/09/11/ai-in-home-assistant
[mcp-spec]: https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
[mcp-legacy]: https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
[cc-mcp]: https://code.claude.com/docs/en/mcp
[playwright-mcp]: https://github.com/microsoft/playwright-mcp
[vllm-spark]: https://vllm.ai/blog/2026-06-01-vllm-dgx-spark
[ollama-faq]: https://raw.githubusercontent.com/ollama/ollama/main/docs/faq.mdx

---

## 8. Session, windows and input on GNOME/Xorg

**Conclusion.**
- **Start SparkHome as a `systemd --user` service** bound to `graphical-session.target`.
- **Move focus explicitly**: activate the game and iconify the kiosk, never "always on top".
- **Harden GNOME for a TV.**
- **Leave short Guide presses to Steam.**

**Starting at login.**
- gnome-session uploads its environment to the user manager, so a user unit sees `DISPLAY` and `XAUTHORITY` [M]/[[D]][gnome-session].
- `graphical-session.target` becomes active only after GNOME Shell signals readiness [M]. `PartOf=`, `After=` and
  `WantedBy=graphical-session.target` is the pattern systemd prescribes [[D]][sd-special].
- **Move** `~/.config/autostart/gamespark-console.desktop` aside. Don't run GameSpark's `console-mode.sh disable`, which
  also restores the old GDM config and turns autologin off [D: GameSpark].

**TV hardening.**
- These measured values must change: `show-banners true`, `lock-enabled true`, `idle-activation-enabled true`,
  `overlay-key 'Super_L'`, `idle-dim true`, `automount-open true` [M]. `idle-delay 0` and `sleep-inactive-ac-type
  'nothing'` already suit a TV [M].
- Update-notifier, DejaDup and Evolution alarm pop-ups are silenced with user `Hidden=true` autostart overrides, which
  gnome-session honours [[D]][gnome-session].
- Apport crash dialogs need root to disable [M].
- Leave `power-button-action` alone: it also governs the Spark's own power button [M]/[I].

**Window focus and stacking in mutter 46.2** [[D]][mutter]:
- **Activation.** `_NET_ACTIVE_WINDOW` is honoured **only while GNOME Shell holds no grab**. The Overview, modal dialogs,
  Alt-Tab and open menus all take one.
- **Timestamp 0** skips the user-time check, unminimises the window, and raises it when the source is a pager (2) or
  `raise-on-click` is true (it is [M]). GameSpark's `tools/xinput.py` sends exactly that: source 2, timestamp 0 [M].
- **New windows can map behind the kiosk.** A window with user time 0 is stacked below the focused window, so the shell
  must activate game windows itself. Whether Proton windows take focus on their own is [U].
- **"Always on top" would hide games.** Fullscreen windows get no special layer: they are in the NORMAL layer. So never
  pin the kiosk on top.
- **Iconify the kiosk while a game runs.** Chrome keeps rendering, and playing video, when merely occluded
  [[D]][electron-vis]/[I].

**Global Home.**
- **X key grabs** work only while no client holds an active keyboard grab [[D]][xgrabkey]. Upstream Wine never grabs the
  keyboard, so Proton games probably leave them working [[D]][wine-x11]/[I].
- **The gamepad's Guide button** is not an X key. Read it with evdev: passive readers see every event unless someone
  `EVIOCGRAB`s the device [[D]][evdev].
- **Hold Guide (≥ 600 ms) for SparkHome Home inside a game.** Short presses stay with Steam's overlay [I].

**Chrome's Gamepad API.**
- It reads joydev. The Xbox BT mapping puts Guide at `buttons[16]` [[D]][gamepad-map], matching joydev b12 = `BTN_MODE` [M].
- It returns nothing until a button has been pressed on the page after boot [[D]][w3c-gamepad].
- Ignore pad input when `!document.hasFocus()`. Filter Steam's virtual pad (id contains `28de`) [I].

**Steam Input.**
- Its Desktop Layout can turn pad input into keys and mouse for non-Steam windows, double-driving the kiosk [[R]][steam-guide].
- Set the Desktop Layout to "Disable Steam Input" and turn Guide chords off [I]. Verify with a keydown logger on the TV
  (open question 11).

**Pausing games with SIGSTOP.** Possible, but risky [[D]][nyrna]/[I]:
- mutter's "not responding" check fires after 5000 ms [M].
- Wine keeps its pointer grab while stopped.
- Audio underruns.
- Anti-cheat may flag the game.

SparkHome doesn't pause games. Home just shows the launcher.

**Fallback session.** If GNOME Shell keeps intruding, `gnome-kiosk-script-session` 46.0 (Xorg and Wayland) is packaged
for Ubuntu 24.04 [[D]][gnome-kiosk]. GameSpark's GNOME-specific settings would then stop applying [I].

[gnome-session]: https://gitlab.gnome.org/GNOME/gnome-session/-/tree/46.0
[sd-special]: https://github.com/systemd/systemd/blob/v255/man/systemd.special.xml
[mutter]: https://gitlab.gnome.org/GNOME/mutter/-/tree/46.2
[electron-vis]: https://www.electronjs.org/docs/latest/api/browser-window#page-visibility
[wine-x11]: https://gitlab.winehq.org/wine/wine/-/tree/master/dlls/winex11.drv
[evdev]: https://github.com/torvalds/linux/blob/master/drivers/input/evdev.c
[gamepad-map]: https://github.com/chromium/chromium/blob/main/device/gamepad/gamepad_standard_mappings_linux.cc
[w3c-gamepad]: https://w3c.github.io/gamepad/
[steam-guide]: https://github.com/valvesoftware/steam-for-linux/issues/13185
[nyrna]: https://github.com/Merrit/nyrna
[gnome-kiosk]: https://packages.ubuntu.com/noble/gnome-kiosk-script-session

---

## 9. Launcher look: tvOS numbers

**Conclusion.** Apple publishes the grid and type numbers but not the focus constants. SparkHome uses the published
numbers and labelled estimates, to be calibrated against a 1080p capture of a real Apple TV 4K.

| Item | Value | Evidence |
|---|---|---|
| Safe area | 60 pt top and bottom, 80 pt left and right | [[D]][hig-layout] |
| Grid | 6 columns × 260 pt with 40 pt gaps = 1760 = 1920 − 2×80; 5 columns × 320 pt when "Large" is on. 5:3 icons, so 260×156; row pitch 256 | [[D]][hig-layout] [[D]][apple-6icons]; [I] |
| Focus scale | legacy spec 300 → 370 = ×1.233. SparkHome uses 1.2 | [[D]][xamarin-tvos]; [I] |
| Tilt and shift | ±0.05 rad (≈2.9°), ±2 pt shift, perspective 500 px: react-native-tvos's emulation, not Apple's constants | [[D]][rn-tvos] |
| Timing | focusing runs faster than unfocusing, and both speed up when focus moves fast. No fixed durations are published | [[D]][hig-focus] |
| Idle | "After a period of inactivity, out-of-focus content dims and the focused element expands" | [[D]][hig-focus] |
| Top Shelf | the top row is the Dock; the area above shows the focused Dock app's content; full-screen previews play and "soon the Dock slides away" | [[D]][hig-topshelf] |
| Type | SF Pro; body 29 pt, minimum 23 pt; Title 1 76 pt. SF Pro's licence forbids bundling it, so use system fonts or Inter | [[D]][hig-type] [[D]][sf-fonts] |
| Liquid Glass | blur plus luminosity; under clear glass on bright content, add a 35 % dark dimming layer | [[D]][hig-materials] |
| Focus engine | Directional search with no wrap. Down from a wide item lands on the centre item. The Norigin core scores with ≥20 % overlap adjacency | [[D]][hig-focus] [[D]][norigin] |
| 60 fps | Animate only `transform` and `opacity`; use `will-change` sparingly; batch layout reads | [[D]][web-compositor] |
| GPU compositing | Unverified in a headful window on this NVIDIA arm64 Xorg stack; headless used SwiftShader | [M]; [U] |

[hig-layout]: https://developer.apple.com/design/human-interface-guidelines/layout
[apple-6icons]: https://support.apple.com/en-us/guide/tv/atvb46af4f11/tvos
[xamarin-tvos]: https://learn.microsoft.com/en-us/previous-versions/xamarin/ios/tvos/app-fundamentals/icons-images
[rn-tvos]: https://unpkg.com/react-native-tvos@0.86.0-2/React/Views/RCTTVView.m
[hig-focus]: https://developer.apple.com/design/human-interface-guidelines/focus-and-selection
[hig-topshelf]: https://developer.apple.com/design/human-interface-guidelines/top-shelf
[hig-type]: https://developer.apple.com/design/human-interface-guidelines/typography
[sf-fonts]: https://developer.apple.com/fonts/
[hig-materials]: https://developer.apple.com/design/human-interface-guidelines/materials
[norigin]: https://github.com/NoriginMedia/Norigin-Spatial-Navigation
[web-compositor]: https://web.dev/articles/stick-to-compositor-only-properties-and-manage-layer-count

---

## 10. Contradictions between the reports, resolved

| # | Topic | One report says | Another says | Which evidence wins, and why |
|---|---|---|---|---|
| 1 | Netflix on Linux | lab: 720p (reported, not re-verified) | streaming: **1080p** for Chrome 117+ on Linux (help page fetched 2026-10-08) | **streaming**: it read the primary page on the same day, and the lab labelled its own claim unverified. Confirm signed in (open question 6) |
| 2 | Which `event*` nodes `spark` can read | games: all `/dev/input/event*` carry a `user:spark` ACL | lab, ui-session, controllers: only joystick nodes and `/dev/uinput`; keyboards, mice and remotes are `root:input` | **the three per-node `getfacl` measurements**. The games row over-generalised. Keyboard-type remotes need a udev rule (root) or X key grabs |
| 3 | Keyboard and mouse | lab (to 00:30): none attached | controllers, ui-session (later on 2026-10-08): a Genesys hub with a USB keyboard and a Logitech mouse | **both are measurements**; hardware was plugged in between the runs [I]. The later state is current. So USB-A dongles go in the hub, and the USB-C→A adapter apple-remotes listed is needed only if the hub is full |
| 4 | Primary local model | voice-agent: vLLM Nemotron 3.5 Lightning; "Ollama has only gemma3:1b" | lab: Ollama `qwen3:4b-instruct-2507` measured here: 5/6 correct, 0.4–0.6 s, 3.87 GB | **lab, for v1**: measured on this box with SparkHome-shaped tools. The vLLM containers weren't running during the lab, and the gemma3 note predates the pulls. Nemotron stays the A/B upgrade (`llm.api: "openai"`), judged on the same eval set |
| 5 | `tool_choice` | voice-agent: `"required"` (vLLM), plus reply and clarify tools | lab: Ollama's `/v1` doesn't support `tool_choice` [D]; the model correctly answered a non-tool question in text [M] | **SparkHome's choice**: native Ollama `/api/chat`, and `"auto"` on OpenAI-compatible servers. Fewer tools, and text answers stay possible. Revisit if the eval shows invalid calls |
| 6 | FLIRC long-press → F14 | SPEC: `record_lp` → F14 | apple-remotes: `record_lp` takes only named keys. The `flirc_util` 3.27.19 help text and key tables (checked again for this doc): F1–F12 only | **the binary's help text** [M] beats an untested plan. Alternatives in §2.2 |
| 7 | Crash-restore bubble | SPEC passes `--disable-session-crashed-bubble` | streaming: that switch is no longer in Chromium's list, and `--hide-crash-restore-bubble` is ChromeOS-only. ui-session: `--hide-crash-restore-bubble` is in `chrome_switches.h` | **unresolved by reading source.** Chrome ignores unknown switches [I], so the flag is harmless. The reported kiosk fix is to set `exited_cleanly: true` and `exit_type: "Normal"` in the profile's `Preferences` before launch [R]. Gate: a power-cut test (open question 14) |
| 8 | Autostart method | lab: a GNOME autostart `.desktop`, as proven by GameSpark's console mode | ui-session: a `systemd --user` unit bound to `graphical-session.target` | **ui-session**: the user manager already holds the display environment [M], and a unit gets `Restart=always` and journald logs; autostart has no clean restart semantics [D]. Gate: the sanctioned reboot |
| 9 | Cloud-game tiles | games: a separate Chrome per tile (`--app`, own profile), so "process exit = tile closed" | streaming and SPEC: one kiosk Chrome, one tab per service, driven by CDP | **SPEC**: CDP already tracks tabs, so separate profiles buy nothing and would split sign-ins |
| 10 | Ports | reports: CDP 9222/9333, backend 8730/8080, MCP 8765, whisper 8090/8971 | SPEC: 8780 (daemon and MCP), 8781 (whisper), 9223 (CDP) | **SPEC** is the contract. Any fixed non-zero CDP port keeps `navigator.webdriver` false [[D]][runtime-features] |
| 11 | Siri Remote and `hog` | voice-agent: both Linux Siri projects require `hog` off, which breaks BLE HID | apple-remotes: true for D-Bus clients; a hidraw path (E1) might keep `hog`; BlueZ ≥ 5.80 fixes it | **apple-remotes**, which read the BlueZ and kernel sources. E1 is untested, so until it passes, the conflict with the BLE Xbox pad stands |
| 12 | Wake-word runtime | voice-agent: `pymicro-wakeword` 2.4.1 (Debian, 2026-07) | controllers: 2.5.0 on PyPI (2026-09-17) with an aarch64 wheel | **controllers**: newer and checked directly. Both agree: phase 2, and avoid Porcupine |
| 13 | Disney+ quality | Disney+: browsers capped at 720p | LTT, on Linux: 480p, and support called it intentional | **expect 480p.** SparkHome's badge states both: "Up to 720p · 480p reported on Linux" |
| 14 | Whisper latency | voice-agent estimate: ~150–400 ms on GPU for 3 s | lab: 0.16 s per 2.69 s (resident server, contended) | **the measurement** |
| 15 | Pulse-Eight price | apple-remotes: ≈US$44–50 (undated cached pages) | controllers: ≈$48 at Pulse-Eight, £42.49 on Amazon UK | **consistent**: use ≈$48 |
| 16 | Remap targets | research: Power → `f17`, voice key → `f18` | SPEC `remotes.hwdb`: Power → `reserved`, voice → `f15` | **SPEC**: F15 (XF86Launch6) is the configured push-to-talk key, and `reserved` simply disables a dangerous key. Both are inert in GNOME [M] |
| 17 | A1962 identity | AppleDB/The Apple Wiki: gen-1 Rev A with a white Menu ring | Wikipedia lists A1962 with FCC ID BCGA1513 | **doesn't matter for SparkHome**: both readings are gen-1 BLE Siri Remotes [I] |
| 18 | Is `KEY_HOMEPAGE` safe? | controllers, ui-session: no, Chrome navigates on XF86HomePage, so remap it to F14 | SPEC: grab XF86HomePage on the root window | **both hold**: the grab is measured to succeed [M] and routes the key to SparkHome instead of Chrome [[D]][xgrabkey]. The hwdb remap is the fallback when the daemon isn't running |

---

## 11. Decisions and open questions

### 11.1 Decisions

1. **Shell.**
   - Chrome `--kiosk` with its own profile and CDP on `127.0.0.1:9223`, plus a Python 3.12 stdlib daemon on
     `127.0.0.1:8780`.
   - One browser: the launcher is tab 1, with up to 4 service tabs (§5).
2. **Start-up.**
   - A `systemd --user` `sparkhome.service` bound to `graphical-session.target`. The daemon supervises Chrome.
   - GameSpark's console autostart is moved aside; `console-mode.sh disable` is not run (§8).
3. **Windows.** Activate (source 2, timestamp 0) and iconify the kiosk while a game runs. Never always-on-top; don't fight
   focus otherwise (§8).
4. **Steam.** GameSpark's `steam_launch` over the pipe, lifecycle from `console_log.txt`, art from librarycache. Keep the
   Canonical snap (§6).
5. **Input.**
   - Keyboard keys through X11 root grabs (no root needed). The gamepad through evdev (uaccess) and the Gamepad API.
   - hwdb remaps are an optional root step.
   - Deferred: a grabbing evdev daemon for remotes, which would fix keys above 247 and the power key (§2.4).
6. **Remote.** FLIRC with the IR Apple remote, if that's what the user owns. A Siri Remote only after E1 passes
   (§0, §2).
7. **Voice.**
   - Push-to-talk, with a resident whisper-server started lazily on first use.
   - The fast path first, the local model second.
   - No wake word until a far-field mic and echo cancellation are measured (§7).
8. **Model.**
   - Ollama `qwen3:4b-instruct-2507-q4_K_M` through native `/api/chat` with `num_ctx` 8192 and `think:false`.
   - vLLM Nemotron through `llm.api: "openai"` is the A/B.
   - Only the fast path runs while an app is in front (§7.3, §7.6).
9. **Agents.** Semantic tools plus launcher state, and dual-era MCP on loopback; remote agents use an ssh tunnel. No
   pixel computer-use in v1 (§7.4, §7.5).
10. **Streaming.** Honest badges, no DRM spoofing, and a companion streamer for HD and surround. No 4K/HDR work (§4).
11. **TV control.** None in v1, since the GB10 has no CEC. The Pulse-Eight adapter and the thermal test come first (§3).

### 11.2 Open questions

Each one closes with a single action.

| # | Question | Why it matters | How to close it | Needs |
|---|---|---|---|---|
| 1 | **Which Apple remote does the user own?** A1156, A1294, or a Siri Remote (gen 1/2/3)? | It decides between FLIRC and E1 | Read the model number on the back, or use Apple's identify page [[D]][apple-identify] | the user |
| 2 | **What is the Sony TV's model and year?** | It decides between Bravia IP control and CEC, the menu paths, and whether the TV has built-in apps that could act as the companion | Check the back label, or Settings → About | the user |
| 3 | **Should the TV boot into SparkHome instead of GameSpark's Big Picture console?** | Steam Big Picture is what's on the TV today | `deploy/install.sh enable` switches it; `disable` reverts | the user's decision |
| 4 | **One sanctioned reboot**: does GDM autologin bring up SparkHome with no keyring prompt? | Autologin has never run at boot | Reboot. Check that `loginctl show-session` shows `Service=gdm-autologin`, the kiosk window `WM_CLASS=SparkHome` maps within ~10 s, and `journalctl --user -u sparkhome` is clean | the user's OK to reboot |
| 5 | **Headful playback and HDCP on the TV** | Headless used SwiftShader, and HDCP was measured with no display | In the kiosk, check `chrome://gpu`, `getStatusForPolicy({minHdcpVersion:'1.0'})`, and `getVideoPlaybackQuality()` for 1080p and 2160p60 AV1/VP9 | time on the TV |
| 6 | **Per-service signed-in quality matrix** | It replaces the [I]/[U] cells in §4.2 and settles the tile badges (e.g. Disney+ 480p vs 720p) | Sign in, play a known 1080p title for 2 minutes, then read the `video` sizes and `chrome://media-internals` | the user's accounts |
| 7 | **FLIRC long-press Home** | `record_lp` can't send F14 (§2.2) | Try `record_lp browser_home`, then `record_api 0 105` on a spare button, then `record_lp home` | the dongle |
| 8 | **Experiment E1** (Siri Remote only) | It is the only path that keeps the BLE Xbox pad working | Run `tools/siri_probe.py` against gates G1–G4 | the remote |
| 9 | **Which microphone?** A USB mic, a G20S Pro, the DualSense over USB, or an XVF3800 | There is no capture device, so voice can't work | Plug one in, then check `arecord -l`; set `settings.mic` if `default` isn't it | hardware |
| 10 | **Does the Sony pass its remote's keys over CEC? Do the fans keep running with the TV in standby?** | It would make the Sony remote a SparkHome remote and make "TV off" safe | Pulse-Eight: `cec-ctl -M`; then 30 minutes of standby while logging thermal zones | hardware |
| 11 | **Steam Input with the launcher focused** | It could double-drive the kiosk, or steal Guide | Log keydowns while pressing A and the D-pad with Steam running; adjust Steam's settings | time on the TV |
| 12 | **Do Proton windows take focus on their own, or map behind the kiosk?** | It decides whether the activation path is mandatory | Watch the first real launch from SparkHome | time on the TV |
| 13 | **Does `preventDefault()` on `BrowserBack`/`BrowserHome` stop Chrome's accelerators in kiosk mode?** | If not, the hwdb remaps are mandatory | Press the keys in a web tile | a remote |
| 14 | **Does a "Restore pages?" bubble appear after a power cut?** | See §10 #7 | Cut the power once | the user's OK |
| 15 | **Does `Target.activateTarget` visibly switch tabs in `--kiosk`?** | Web tiles depend on it | Open two tiles and switch | time on the TV |
| 16 | **Uncontended latencies, and game frame-time cost** | The lab's voice and LLM numbers were contended, and LLM bursts may stutter games | Re-run the voice and LLM measurements while idle; measure frame-time p99 in Cyberpunk while firing 20 LLM calls | an idle GPU |
| 17 | **Chrome's two-week updates** | Does the kiosk show an update or relaunch nag? SparkHome passes `--check-for-update-interval=31536000`, which these reports didn't research | Watch the first apt update | time |
