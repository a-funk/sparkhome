# SparkHome: controllers, voice hardware and TV control

Research date: 2026-10-08 (task brief dated 2026-10-07).
Target: NVIDIA DGX Spark `spark`: DGX OS 7.2.3 (Ubuntu 24.04.4 arm64), kernel 6.17.0-1029-nvidia, NVIDIA driver
580.173.02, GNOME 46 on Xorg (`DISPLAY=:1`, GDM autologin `spark`), Chrome 155.0.8059.39, BlueZ 5.72 on a MediaTek MT7925,
and a Sony TV on HDMI-0 at 1080p60.

## How to read this report

Evidence labels:

- **[M] measured.** I ran it on the Spark this session (`ssh spark`; read-only; nothing installed or changed), or
  computed it from a primary artifact that I fetched: the kernel source at torvalds/linux `master` (Makefile says
  **7.3-rc6**), PyPI JSON, or the Chromium source.
- **[D] documented.** Vendor, kernel or upstream documentation.
- **[R] reported.** A user, forum, press or retailer report that I did not reproduce.
- **[I] inferred.** My reasoning. Treat it as a hypothesis until it is tested.
- **[U] unknown.** Nobody has published it, or I could not find it.

Prices are 2025–2026 snapshots. They vary by seller and region.

Other reports in this folder. This report points to them instead of repeating them:

- `apple-remotes.md`: the user's Apple remote, FLIRC with an arm64 `flirc_util`, Siri Remote paths, and BlueZ HOG
  ownership.
- `ui-session.md`: GNOME and Chrome key grabs, the hwdb remap table (§C.5–C.6), and the kiosk session.
- `spark-lab.md`: whisper.cpp and Piper measurements, and the permissions inventory.
- `games.md`: games.

---

## 0. Decisions in one page

### 0.1 Buy-this-one-thing picks

| Need | Pick | Price | Why | Main risks |
|---|---|---|---|---|
| **(a) TV remote with voice** | **G20S Pro, 2.4 GHz USB-dongle version.** Not the Bluetooth-only "BT" listing. "Pro Plus" (2.4G + BT) is also fine if you use its dongle. | ≈US$16–30. Pro Plus listings run ≈$43–48 on Newegg (2026) [R]. | This is the only voice remote with a Linux report that its dongle shows up as a **standard USB Audio capture card**: "card 1: Device [USB Composite Device], device 0: USB Audio", recorded with `arecord -D hw:1,0 -f S16_LE -r 16000` [R: CoreELEC, 2026-04-06]. A dongle avoids Bluetooth, and the MT7925 radio sometimes wedges [M brief]. Buttons are plain USB HID. It also learns the TV's IR power code. | Its keycodes are unpublished [U]. Per one manual, the mic streams only while the button is held [R]. The one Linux mic report is a hard lock on an Amlogic box, probably platform-specific [R/I]. IR learning is often power-key-only, and it may not learn Sony SIRC [R/I]. Run the acceptance test in §3.6 when it arrives. |
| **(b) Gamepad** | **Buy nothing.** The Xbox Wireless Controller (BLE) is paired and sends input today [M], and so is the DualSense [M]. For a Bluetooth-free second pad, buy an **8BitDo Ultimate 2C Wireless** (2.4 GHz dongle, XInput). | 2C: $29.99 at launch, often $24–26 [R]. | In this kernel, `xpad` binds any 8BitDo (VID `2DC8`) Xbox-class interface [M: `modinfo`]. It needs no pairing and doesn't use the MT7925. | No gyro or back buttons over XInput [R]. Some dongle modes enumerate as `2dc8:6012` (DInput, hid-generic) [R]. |
| **(c) Hands-free voice** | **Seeed reSpeaker XVF3800 USB 4-Mic Array (with case)**, plus `pymicro-wakeword`, plus PipeWire's WebRTC echo-cancel on the HDMI sink monitor. This is phase 2: ship push-to-talk first. | $49.99–60.99 bare, $62.99 with case [R]. | It is UAC2 and needs no driver [D]. On-chip AEC, beamforming and noise suppression [D]. aarch64 host tools and a pyusb control script exist [M: repo]. The older XVF3000 v2.0 is end-of-life [R]. | TV audio leaves over HDMI, so the board's own AEC has no reference signal. Use PipeWire AEC in monitor mode instead [D/I]. The TV's audio latency is [U]. |
| **TV power and input** (strongly recommended) | **Pulse-Eight USB-CEC Adapter (P8-USBCECv1)**, inline between the Spark's HDMI and the TV's HDMI 1 | ≈$48 at Pulse-Eight, £42.49 on Amazon UK, ≈$30 used [R] | GB10 has no CEC (brief). The TV answers no network discovery: SSDP found no Sony endpoint [M]. The kernel `pulse8-cec` module, `inputattach --pulse8-cec` and RC passthrough are all present [M]. CEC gives TV on, standby and input switching, **and** lets the Sony remote drive SparkHome. | Whether this Sony forwards remote keys over CEC is [U]. Setup needs root once. `/dev/cec*` belongs to group `video`, and `spark` is not a member [M]. |

### 0.2 Build constraints found this session

1. **Xorg cannot deliver evdev keycodes above 247** [D]. Remote keys above that line: `KEY_OK` 352, `KEY_SELECT` 353
   (HID "Menu Pick", often the OK button), `KEY_INFO` 358, `KEY_VOICECOMMAND` 582, `KEY_ROOT_MENU` 618, and digits
   `KEY_NUMERIC_*` (512 and up) [M: header]. **CEC "Select" maps to `KEY_OK`** [M: rc-cec.c], so the TV remote's OK
   button can never reach Chrome through X. Read those devices with evdev in a helper process.
2. **GNOME grabs several remote keys**: XF86Search (`KEY_SEARCH`), XF86PowerOff, the media keys, XF86Tools (`KEY_CONFIG`
   and F13), XF86AudioMicMute (F20), touchpad toggles (F21–F23), XF86WWW, XF86Mail and XF86Calculator [M]. Chrome
   itself acts on XF86HomePage and XF86Back [D: `ui-session.md` §C.6]. `KEY_HOMEPAGE` and `KEY_BACK` are therefore not
   "safe" either.
3. **Power-key hazard.** Every key-capable input device is tagged as a logind `power-switch` [M: rule]. `gsd-media-keys`
   holds the `handle-power-key` inhibitor, and `power-button-action='interactive'` [M]. A remote's Power key (HID
   consumer 0x30, or CEC 0x40 → `KEY_POWER`) opens GNOME's power-off dialog. Outside GNOME, logind's default
   `HandlePowerKey=poweroff` would apply [M: config]. Remap that key or grab the device.
4. **Permissions.** `spark` is not in `input`, `video` or `dialout` [M]. That closes keyboard-class `/dev/input/event*`
   (every remote), `/dev/cec*` (group `video` [M]) and `/dev/ttyACM*` (group `dialout` [M]). Joystick nodes are open
   through uaccess ACLs [M]. `/dev/uinput` is open through Steam's udev rule [M].
5. **The Spark never sleeps.** DGX OS ships `AllowSuspend=no`; the kernel offers only `s2idle` and no hibernate [M].
   NVIDIA forum staff wrote "confirmed Spark does not support wake-on-lan" (2025-10-18) [R]. So "off" means the
   **TV in standby via CEC**, while the Spark stays on. It idles at roughly 37–40 W [R].
6. **Thermal risk on exactly this configuration.** A 2026 forum thread reports fans stopping when the display goes
   dark [R]. The original poster runs DGX OS 7.2.3, kernel 6.17.0-1029-nvidia, driver 580.173.02 and BIOS
   5.36_0ACUM018, the same versions as this unit [M]. A user tied it to `mtk_pcie_hotplug` removing an uncabled
   ConnectX-7 [R]. Here `mtk_pcie_hotplug` is loaded and no ConnectX-7 is on the PCIe bus [M]. Do not add display
   blanking. Measure temperatures with the TV in CEC standby before shipping any "TV off" flow (§9.3).
7. **USB.** The rear ports are USB-C only [D: `apple-remotes.md` §2]. A Genesys 4-port hub is already attached, with a
   keyboard and mouse on it [M]. Plug the USB-A dongles (remote, CEC adapter, gamepad, FLIRC) into that hub.

### 0.3 Input architecture

Run one small daemon, `sparkhome-inputd`, as a user service (no root at runtime):

- It reads evdev from the devices it owns (the remote dongle, the CEC remote device, optionally FLIRC). It takes each
  with `EVIOCGRAB`, so X11, GNOME, Chrome and logind never see those keys. That removes the >247 limit, the GNOME
  grabs and the power-key hazard in one step.
- It maps every device onto **one semantic action set**: `up/down/left/right/select/back/home/menu/play_pause/
  voice_start/voice_stop/vol_up/vol_down/power`. This is the same table as `apple-remotes.md` §9.
- It publishes actions to the launcher over the launcher's own WebSocket.
- It owns push-to-talk audio (the remote's UAC mic → whisper.cpp) and CEC (TV on, standby, input).
- When a game or another app has focus, it re-injects keys through `/dev/uinput`, which `spark` can already write [M].
- The phone PWA and the local agent harness send the **same** semantic actions.
- Gamepads stay ungrabbed. Chrome reads them through the Gamepad API, and Steam reads them directly.

hwdb remaps (`ui-session.md` §C.6) are needed only for keys that must work in apps where the daemon is not running.

---

## 1. Measured baseline (2026-10-08, read-only over `ssh spark`)

| Item | Value | Command |
|---|---|---|
| Platform | `NVIDIA_DGX_Spark`, board P4242, BIOS AMI `5.36_0ACUM018` (2025-08-06) | `/sys/class/dmi/id/*` |
| Suspend policy | `/etc/systemd/sleep.conf.d/disable_suspend.conf`: `[Sleep] AllowSuspend=no` | `cat` |
| Kernel sleep states | `/sys/power/state` = `freeze mem`; `mem_sleep` = `[s2idle]` (no `deep`); no `/sys/power/disk`, so no hibernate | `cat` |
| NVIDIA suspend services | `nvidia-suspend`/`resume`/`hibernate` enabled; `PreserveVideoMemoryAllocations: 1`; `EnableS0ixPowerManagement: 0` | `systemctl is-enabled`; `/proc/driver/nvidia/params` |
| NICs | `enP7s7` = Realtek RTL8127 10 GbE (`r8127`), **no carrier**, `power/wakeup=enabled`. `wlP9s9` = MT7925 Wi-Fi, **in use, 5 GHz ch 157 (5785 MHz)**, −53 dBm. No ConnectX-7 on PCIe (`lspci` shows no Mellanox device), yet `mlx5_core` and `mtk_pcie_hotplug` are loaded | `ip -br link`, `iw dev wlP9s9 link`, `lspci`, `lsmod` |
| Display | `HDMI-0 connected 1920x1080`, 1600 mm × 900 mm. X screensaver timeout 0; DPMS enabled, all timers 0, "Monitor is On". GNOME `idle-delay 0` | `xrandr`, `xset q`, `gsettings` |
| TV EDID | Manufacturer `SNY`, product `0xEA01`, "week 1 of 2010", EDID 1.3, name "SONY TV", **HDMI physical address 1.0.0.0** (Spark is on TV HDMI 1). The linuxhw EDID database has 5 identical `SNYEA01` dumps with the same date and size, so "2010" and 160×90 cm look like Sony placeholders. **The TV model and year are unknown** | `xrandr --verbose` EDID decoded; [github.com/linuxhw/EDID SNYEA01](https://github.com/linuxhw/EDID/tree/master/Digital/Sony/SNYEA01) |
| TV on the network? | SSDP `ssdp:all` from the Spark: **no Sony/BRAVIA/IRCC/ScalarWebAPI device answered** | Python SSDP M-SEARCH + UPnP description fetch |
| Input devices now | Xbox Wireless Controller (BLE, uhid `0005:045E:0B13`; `event7`, `js0`); DualSense (BT, `054C:0CE6`; `event8`, `js1`, plus motion `event9` and touchpad `event10`/`mouse0`) | `/proc/bus/input/devices` |
| Xbox capabilities | KEY bitmap = `BTN_SOUTH…BTN_THUMBR` (304–318), **Guide = `BTN_MODE` (316)**, **Share = `KEY_RECORD` (167)** | decoded `B: KEY=` |
| DualSense capabilities | `BTN_SOUTH, EAST, NORTH, WEST, TL, TR, TL2, TR2, SELECT, START, MODE (PS), THUMBL, THUMBR` | decoded `B: KEY=` |
| ACLs | `event7`/`event8`/`js0`/`js1` = `user:spark:rw` (uaccess, joystick). `/dev/uinput` = `user:spark:rw` (from `/usr/lib/udev/rules.d/60-steam-input.rules`). Other `event*` = `root:input 0660` | `ls -l`, `getfacl` |
| Groups | `spark`: adm, sudo, audio, dip, plugdev, users, lpadmin, ollama, docker. **Not** input, video or dialout. `video` = {ollama}; `dialout` and `input` are empty | `id`, `getent` |
| Default udev groups | `SUBSYSTEM=="cec", GROUP="video"`; `tty[A-Z]*` → `dialout` | `/usr/lib/udev/rules.d/50-udev-default.rules` |
| Power key | `70-power-switch.rules` tags **every** `ID_INPUT_KEY` event device `power-switch`. logind `HandlePowerKey` is the default (`poweroff`). `gsd-media-keys` holds `handle-power-key:handle-suspend-key:handle-hibernate-key` (block). `power-button-action='interactive'` | `cat`, `systemd-inhibit --list`, `gsettings` |
| GNOME static key grabs | XF86Search, XF86PowerOff, XF86Sleep, XF86AudioPlay/Pause/Stop/Next/Prev/Rewind/Forward/Media/MicMute, volume keys, XF86Tools, XF86Explorer, XF86WWW, XF86Mail, XF86Calculator, XF86ScreenSaver, XF86Eject, XF86Battery, XF86Bluetooth/WLAN/RFKill, XF86TouchpadToggle/On/Off. **XF86HomePage, XF86Back, XF86MenuKB and XF86Launch5–9 are not grabbed** | `gsettings list-recursively org.gnome.settings-daemon.plugins.media-keys` |
| XKB function keys | X keycode 191 (F13) = XF86Tools; 192–196 (F14–F18) = XF86Launch5–9; 197 (F19) = none; 198 (F20) = XF86AudioMicMute; 199–201 (F21–F23) = touchpad keys; 202 (F24) = none. 147 (`KEY_MENU`) = XF86MenuKB; 135 (`KEY_COMPOSE`) = Menu | `xmodmap -pke` |
| Audio | PipeWire 1.0.5. Only sink: "Built-in Audio Digital Stereo (HDMI)". **No sources** (`arecord -l` is empty). `libspa-aec-webrtc.so` and `libpipewire-module-echo-cancel.so` are present | `wpctl status`, `ls` |
| CEC and IR in this kernel | `CONFIG_CEC_CORE=m`, `MEDIA_CEC_RC=y`, `USB_PULSE8_CEC=m`, `USB_RAINSHADOW_CEC=m`. `pulse8-cec.ko` is on disk. **`inputattach` 1:1.8.1 is installed and supports `--pulse8-cec`**. rc-core with NEC, **Sony**, RC5, RC6, JVC, Sanyo, Sharp, XMP, iMON and RCMM decoders; `mceusb`, `ir_toy`, `iguanair`, `redrat3` and `streamzap` modules | `/boot/config-*`, `find /lib/modules`, `inputattach --help` |
| Gamepad and HID drivers | `hid-nintendo` (Switch 1 IDs only, up to `057e:201e`), `hid-playstation` (incl. `0CE6`/`0DF2`), `hid-steam` (`28DE`: 1102/1142/1205 only, **no 2026 Steam Controller**), `hid-microsoft` (Xbox BLE 0B13/0B20/0B22), `xpad` (incl. vendor-wide `2DC8` 360/One classes) | `modinfo -F alias` |
| apt candidates (not installed) | `cec-utils`/`libcec6` 6.0.2-1ubuntu6, `v4l-utils` and `ir-keytable` 1.26.1-4build3, `lirc` 0.10.2, `evtest` 1.35, `python3-evdev` 1.7.0, `libinput-tools` 1.25.0. **`python3-libevdev` 0.5-3 is installed** | `apt-cache policy` |
| USB | Genesys hub (`05e3:0610` + `05e3:0625`) with a USB keyboard (`1a2c:40e3`) and a Logitech mouse (`046d:c077`). MT7925 BT on an internal bus | `lsusb`, `lsusb -t` |
| Thermals (display on, idle-ish) | `acpitz` zones at 40.8–42.4 °C. No fan tachometer in hwmon | `/sys/class/thermal` |

---

## 2. Linux input facts that decide which remote works

### 2.1 The X11 keycode ceiling (Xorg session)

The X core protocol carries keycodes 8–255, and the X keycode is the evdev code plus 8. So **evdev codes ≥ 248 never
reach any X client** [D: [xorg list 2010](https://lists.freedesktop.org/archives/xorg/2010-April/049675.html);
[Arch FS#59024](https://bugs.archlinux.org/task/59024)]. Chrome on this Xorg session cannot see them. A helper can still
read them from `/dev/input/event*`.

| Key (value) [M: `input-event-codes.h`] | Reaches Chrome under Xorg? |
|---|---|
| `KEY_ENTER` 28, `KEY_ESC` 1, `KEY_BACKSPACE` 14, arrows 103/105/106/108 | yes |
| `KEY_COMPOSE` 127 | yes (`ContextMenu`) |
| `KEY_MENU` 139 | yes as XF86MenuKB, but Chromium has no DOM code for it (`code` is empty) [M: dom_code_data.inc] |
| `KEY_BACK` 158, `KEY_HOMEPAGE` 172 | yes (`BrowserBack`, `BrowserHome`), but Chrome navigates on them [D: `ui-session.md` C.6] |
| `KEY_SEARCH` 217 | X yes (`BrowserSearch`), but **GNOME grabs it** [M] |
| `KEY_POWER` 116 | X yes, but **GNOME grabs it and opens the power dialog** [M] |
| `KEY_F14`–`KEY_F18` 184–188 | yes (`F14`–`F18`); not grabbed by GNOME [M]. **Use these for custom remote keys** |
| `KEY_F13` 183, `KEY_F20`–`F23` 190–193 | yes, but GNOME grabs them (Tools, MicMute, touchpad) [M] |
| `KEY_OK` 352, `KEY_SELECT` 353, `KEY_INFO` 358, `KEY_EPG` 365, `KEY_TV` 377, `KEY_RED` 398, `KEY_CHANNELUP` 402, `KEY_NUMERIC_0` 512, `KEY_VOICECOMMAND` 582, `KEY_ASSISTANT` 583, `KEY_DICTATE` 586, `KEY_ROOT_MENU` 618 | **no** (≥ 248) |

### 2.2 HID consumer usages that TV remotes send, and what Linux makes of them

The mapping comes from `drivers/hid/hid-input.c` on master [M]. The Chrome `code` column comes from Chromium
`ui/events/keycodes/dom/dom_code_data.inc` [M].

| HID usage (consumer page 0x0C) | Linux key | X11-visible | Chrome `code` | Notes |
|---|---|---|---|---|
| 0x0030 Power | `KEY_POWER` | yes | `Power` | GNOME power dialog, power-switch device [M] |
| 0x0040 Menu | `KEY_MENU` | yes | (none) | |
| 0x0041 Menu Pick (**OK on many remotes**) | `KEY_SELECT` | **no** | — | remap to `enter` or read via evdev |
| 0x0042–0x0045 Menu Up/Down/Left/Right | arrows | yes | arrows | |
| 0x0046 Menu Escape | `KEY_ESC` | yes | `Escape` | |
| 0x0060 Data On Screen | `KEY_INFO` | no | — | |
| 0x0069–0x006C colour keys | `KEY_RED/GREEN/BLUE/YELLOW` | no | — | |
| 0x0077–0x007A (app buttons on the Onn remote) | `KEY_CAMERA_ACCESS_*`, `KEY_KBDILLUMUP/DOWN` | mixed | — | Linux does not treat these as app buttons |
| 0x0089 / 0x008D | `KEY_TV` / `KEY_PROGRAM` | no | — | |
| 0x0096 | `KEY_TAPE` | no | — | the Onn "Settings" button |
| 0x009C / 0x009D | `KEY_CHANNELUP/DOWN` | no | — | |
| 0x00B0 / B1 / B5 / B6 / B7 / CD | play, pause, next, previous, stop, play/pause | yes | `Media*` | GNOME grabs these and forwards them to MPRIS [M] |
| 0x00CF Voice Command | `KEY_VOICECOMMAND` | **no** | — | a plausible mic-button usage [I] |
| 0x00D8 Dictate | `KEY_DICTATE` | no | — | |
| 0x00E2 / E9 / EA | mute, volume up, volume down | yes | `AudioVolume*` | GNOME changes system volume (HDMI) [M] |
| 0x0221 AC Search (**the Assistant/mic button on ATVV remotes** [D: onn-remote-linux]) | `KEY_SEARCH` | yes | `BrowserSearch` | **GNOME grabs it** and opens search over the kiosk [M] |
| 0x0223 AC Home | `KEY_HOMEPAGE` | yes | `BrowserHome` | Chrome "Home" accelerator |
| 0x0224 AC Back | `KEY_BACK` | yes | `BrowserBack` | Chrome back navigation |
| 0x022A AC Bookmarks | `KEY_BOOKMARKS` | yes | — | |
| keyboard page 0x07 0x28 / 0x29 / 0x65 / 0x80 / 0x81 | Enter / Esc / Compose / volume up / volume down | yes | `Enter` / `Escape` / `ContextMenu` / volume | Many air mice send OK as Enter and Back as Esc [I] |

### 2.3 What GNOME and logind do with remote keys

Measured facts are in §1. Consequences:

- Mic button sends `KEY_SEARCH`: the GNOME Activities search opens over the TV.
- Remote sends `KEY_CONFIG` or `KEY_F13`: GNOME Settings opens.
- Remote sends `KEY_POWER`: the GNOME power-off dialog opens. [I] It counts down and powers off if nobody answers.
  In any session without `gsd-media-keys` (a bare X or cage kiosk), logind powers off immediately (default
  `HandlePowerKey=poweroff`) [M: config].
- The CEC remote device emits `KEY_POWER` for CEC 0x40 [M: rc-cec.c]. It is also key-capable, so it is
  power-switch-tagged [I].

Fix it in one place. Grab the remote devices in `sparkhome-inputd`; a grabbed device is invisible to logind and X
[I: `EVIOCGRAB` semantics]. If the daemon is not running, the hwdb fallback maps Power to `f17` (`ui-session.md` C.6).

### 2.4 Permissions and udev (root once)

- Keyboard-class event nodes (any remote dongle) are `root:input 0660` [M]. Do **not** add `spark` to `input`: that
  would let any process read every keyboard. Tag the specific remote with uaccess instead. The rule must sort before
  `73-seat-late.rules` [D: `ui-session.md` C.5]:

```udev
# /etc/udev/rules.d/70-sparkhome-input.rules
# 2.4 GHz voice remote dongle (fill VID/PID from lsusb), USB bus 0003
SUBSYSTEM=="input", KERNEL=="event*", ATTRS{idVendor}=="VVVV", ATTRS{idProduct}=="PPPP", TAG+="uaccess"
# FLIRC (Clay Logic 20a0:0006) if used
SUBSYSTEM=="input", KERNEL=="event*", ATTRS{idVendor}=="20a0", ATTRS{idProduct}=="0006", TAG+="uaccess"
# CEC adapter control node and the CEC remote input device (pulse8-cec)
SUBSYSTEM=="cec", TAG+="uaccess"
```

- The CEC remote input device appears under rc-core. Find its `event*` with `ir-keytable` or `/proc/bus/input/devices`
  and tag it the same way (match on its `name`) [I: exact name U].
- The UAC microphone of the remote, the XVF3800 or the DualSense needs nothing extra. `spark` is in `audio` [M], and
  PipeWire runs in the user session.
- `/dev/uinput` is already writable by `spark` [M]. It is enough for re-injection and for the agent's "computer use"
  keystrokes.

### 2.5 `sparkhome-inputd` sketch (stdlib only; no new dependencies)

```python
# ponytail: one device per thread is enough for ~4 devices; switch to selectors if you add more.
import fcntl, os, struct
EVIOCGRAB = 0x40044590                 # _IOW('E', 0x90, int)
EV = struct.Struct('llHHi')            # struct input_event on aarch64: 24 bytes
EV_KEY, EV_MSC, MSC_SCAN = 1, 4, 4

def run(path, keymap, emit):           # keymap: {linux_code: "select"|"back"|...}; scancodes also arrive
    fd = os.open(path, os.O_RDONLY)
    fcntl.ioctl(fd, EVIOCGRAB, 1)      # exclusive: X11, GNOME, Chrome and logind stop seeing this device
    scan = None
    while True:
        _, _, t, code, val = EV.unpack(os.read(fd, EV.size))
        if t == EV_MSC and code == MSC_SCAN: scan = val          # HID usage, e.g. 0xc0221
        elif t == EV_KEY and val in (0, 1, 2):                   # release/press/repeat
            action = keymap.get(code) or keymap.get(('scan', scan))
            if action: emit(action, val)                         # e.g. voice_start on 1, voice_stop on 0
```

Push-to-talk: on `voice_start`, start
`arecord -D plughw:CARD=<remote>,DEV=0 -f S16_LE -r 16000 -c 1 -t raw` (or `pw-record`). On `voice_stop`, stop it and
send the clip to whisper.cpp (`spark-lab.md` §5 has the measured STT setup).

---

## 3. TV-style remotes with a microphone

### 3.1 2.4 GHz USB-dongle "voice air mice" (G20S / G10S / MX3 / WeChip class)

| Model (as sold) | Link | Voice | IR learning | Linux evidence |
|---|---|---|---|---|
| **G20S Pro** (backlit, gyro, voice) | 2.4 GHz dongle | yes | power key (some listings: 2 keys) [R] | Dongle mic = UAC capture card "USB Composite Device", `S16_LE` 16 kHz [R: CoreELEC 2026-04]. That user's Amlogic box hard-locked while recording; a `usbcore.autosuspend=-1` workaround helped only the first recording [R] |
| **G20S Pro BT** | Bluetooth 5.0 (some units also have 2.4G) | over BT: ATVV (§3.2) | power [R] | Over BT, BlueZ lists the ATVV service `ab5e0001-5a21-4f05-bc7d-af01f617b664` and PipeWire sees no mic [R: Arch forum 285325, 2023–2025] |
| **G20S Pro Plus** | 2.4 GHz + BT 5.0 | yes | 2 keys [R] | Not reported; use the dongle [I] |
| G20S (no backlight) / G20 | 2.4 GHz | yes | power [R] | Keys work on LibreELEC/CoreELEC [R] |
| G10S / G10S Pro | 2.4 GHz | yes | power [R] | "Worked as soon as the USB dongle was attached" on CoreELEC 19.3 [R] |
| MX3 / MX3 Pro (QWERTY back) | 2.4 GHz | Pro: yes | Power + 4 colour keys, **NEC protocol only** [R: MythTV wiki] | "Recognized as a keyboard and a mouse", no configuration [R]; mic on Linux [U] |
| Rii MX6 | 2.4 GHz | yes | ? | Voice tied to Android in listings [R]; Linux mic [U] |

What is known about the Linux behaviour:

- **Buttons.** Standard USB HID keyboard, mouse and consumer interfaces; they work without drivers [R]. Several air
  mice send different codes with the air-mouse pointer on or off. One remote's OK worked only in mouse mode [R:
  LibreELEC]. Test both modes.
- **Microphone.** On the G20S Pro dongle it is a USB Audio Class capture interface [R]. Nvidia Shield (pre-2019)
  compatibility in listings fits that [I]. **Not all dongle remotes work this way.** Some chipsets send ADPCM audio
  inside vendor HID reports [R: Nordic DevZone], and those would need a custom decoder [I].
- **Voice behaviour.** One G20S Pro manual says the voice channel stays open only while the key is held [R]. Plan for
  push-to-talk, not wake word.
- **Keycodes [U].** I found no evtest dump for the G20S Pro family. The voice button is probably consumer 0x221
  (`KEY_SEARCH`, grabbed by GNOME) or 0x0CF (`KEY_VOICECOMMAND`, invisible to X11) [I]. OK is probably Enter (keyboard
  0x28) or 0x41 (`KEY_SELECT`). Either way, `sparkhome-inputd` handles it.
- **IR learning.** Usually only the Power key [R]. MX3 learning is NEC-only [R]. Sony uses SIRC, so learning the Sony
  power code may fail [I]. CEC (§8) is the reliable TV power path.
- **Variant confusion.** "G20S Pro", "G20S Pro BT" and "G20S Pro Plus" are different radios [R]. Buy a listing that
  says "2.4G" and "USB receiver".
- **Platform risk.** The CoreELEC lock-up (the remote's `bInterval 1` polling was a guess there) was on an Amlogic
  S928X [R]. GB10 xHCI behaviour is [U]. The §3.6 test includes a 60-second recording.

### 3.2 Bluetooth LE voice remotes: Google "ATV Voice over BLE" (ATVV)

Protocol, as documented by working open-source code [D: onn-remote-linux README]:

| Item | Value |
|---|---|
| Service | `ab5e0001-5a21-4f05-bc7d-af01f617b664` |
| TX, write (`ab5e0002-…`) | `GET_CAPS` `0a 00 04 00 01`, `MIC_OPEN` `0c 00 01`, `MIC_CLOSE` `0d` |
| RX, notify (`ab5e0003-…`) | 134-byte frames. Header: sequence [0:2], stream id [2], predictor int16 BE [3:5], step index [5]. Then 128 bytes of IMA ADPCM, high nibble first = 256 samples **@ 8 kHz** on the Onn remote |
| CTL, notify (`ab5e0004-…`) | `08` assistant pressed, `04` audio start, `0a` sync, `00` audio end, `0b` caps reply |
| Limits | Stream cut after ≈15 s. The Onn remote reports no Assistant-key release |
| 16 kHz | The Xiaomi Bluetooth Remote 2 Pro (RC003) negotiates **16 kHz IMA/DVI ADPCM** [D: nijez/open-voice-bridge] |

Linux status:

- BlueZ has no native ATVV support. Issue #1086 (Feb 2025, Ugoos UR02 clone) is open with no maintainer reply [D].
- Working userspace implementations exist:
  - `FloMaetschke/onn-remote-linux`: Python, bleak, MIT, 3 commits. Reads keys and voice, and provides a virtual
    PipeWire mic and a systemd user unit. The kernel rejects the Onn HID descriptor (`hid-generic … item fetching
    failed`), so the keys are also read over GATT [D].
  - The GitHub `atvv` topic lists 5 repos, all from 2026, mostly Xiaomi RC003 bridges with local Whisper. One is a
    Linux port: `layneyu/remote-mic-app` (BlueZ, ATVV, PipeWire) [D].
- [I] On BlueZ 5.72 the HOG plugin claims the HID service and hides it from D-Bus GATT clients (`apple-remotes.md`
  §6.3). The ATVV voice service is separate, so it should stay reachable. Reading *keys* over GATT, as the Onn code
  does, may need BlueZ ≥ 5.80 `ExportClaimedServices`.

Verdict: feasible, with real engineering. You need a BLE daemon (bleak), ADPCM decode (stdlib `audioop` is gone in
Python 3.13; Python 3.12 still has it [I]), reconnect handling and MT7925 resilience. Pick this path only if you want
a true TV-remote feel and accept 8–16 kHz audio. Candidates: Xiaomi Bluetooth Remote 2 Pro RC003 (16 kHz [D]),
Onn/Google TV remotes (8 kHz [D]), G20S Pro BT (ATVV [R]). Prices were not checked [U].

### 3.3 Amazon Fire TV (Alexa) Voice Remote

- It pairs with `bluetoothctl` (hold Home + Select; it shows up as "AR"). On a LibreELEC S905 box the navigation,
  Select and Back keys worked after pairing; on an OSMC Vero 4K+ no keys arrived [R: OSMC forum].
- Voice uses Amazon's own protocol, not ATVV. I found no Linux implementation [U]. **Not recommended.**

### 3.4 Apple remotes (the user's "Gen 1")

See `apple-remotes.md`. In short:

- No jailbreak is needed or possible.
- **White Apple Remote (A1156**, shipped with the 1st-gen Apple TV) **and aluminium A1294** are IR only [D: Apple
  103233]. Use FLIRC (US$24.95). An arm64 `flirc_util` exists: [M] HEAD `http://apt.flirc.tv/arch/arm64/flirc.latest.arm64.tar.gz`
  → 200, 20,185,904 bytes, Last-Modified 2025-08-19. Another option is an mceusb receiver with `ir-keytable`.
- **Siri Remote 1st gen (A1513/A1962)** is BT 4.0 + IR [D]. Buttons and touch work through the userspace project
  `Yanndroid/SiriRemote-Linux` (bluepy, root). The Siri mic sends Opus in 101-byte packets, which that project does
  not decode [R]. LibreELEC: "not a standard Bluetooth remote" (2019–2024) [R].
- No Apple remote gives voice on Linux without significant work.

### 3.5 Making the TV's own remote drive the Spark

| Path | Hardware | How keys arrive | Notes |
|---|---|---|---|
| **HDMI-CEC** (preferred) | Pulse-Eight USB-CEC | rc-core input device; CEC UI codes become keys via the `rc-cec` keymap [M]. `cec-ctl` passes RC by default (`--no-rc-passthrough` turns it off) [D] | Select → `KEY_OK` (invisible to X11) and Exit/Return → `KEY_EXIT`. Read it in `sparkhome-inputd`. Whether this Sony forwards keys to a playback device is [U]; Bravia Sync usually does [I] |
| IR, learned | FLIRC (US$24.95) | Chosen keyboard keys (arrows, Enter, Esc, F14–F18…) | Program it once, on the Mac or with `flirc_util` on the Spark. The TV also sees every IR press, so use keys the TV ignores on an HDMI input [I: test] |
| IR, decoded | mceusb receiver + `ir-keytable -p sony` | rc-core keys from your TOML keymap | Measure codes with `ir-keytable -t` (Sony decoder present [M]). The same device can blast IR with `ir-ctl` (§8.4) |

### 3.6 Acceptance test for any new remote (run on arrival; ≈10 minutes)

Run each line, then check its expectation:

1. `lsusb; lsusb -t`. Expect a new VID:PID, `usbhid` interfaces, and an `snd-usb-audio` interface for a voice dongle.
2. `cat /proc/bus/input/devices` (find the new `event*` nodes; dongles often create 2–4).
3. Read events without root, using the installed `python3-libevdev`, or the §2.5 reader, or `sudo evtest` after
   `apt install evtest`. Press every button in **both** air-mouse modes. Record `MSC_SCAN` and `KEY_*` for each.
4. Press the mic button and note: which key it sends, whether a release event arrives, and whether audio flows only
   while it is held.
5. `arecord -l`, then `arecord -D plughw:CARD=<name>,DEV=0 -f S16_LE -r 16000 -c 1 -d 60 /tmp/remote.wav` while
   holding the mic. Check for no system stall and an intelligible recording.
6. Feed the WAV to whisper.cpp (`spark-lab.md` §5). Compare the word error rate with the same phrase recorded on a
   phone.
7. Learn the Sony power code into the remote's IR key, then check that the TV toggles from the couch.
8. Gate: every button gives a stable, unique code; the mic works for 60 s without stalls; nothing triggers GNOME
   dialogs once the device is grabbed.

---

## 4. Mini keyboards with touchpads

| Model | Link | Linux | Price | Verdict |
|---|---|---|---|---|
| **Logitech K400 Plus** | Unifying 2.4 GHz receiver | Works as a standard keyboard and touchpad. Two-finger scrolling reported fine on a Linux HTPC. F1–F12 need Fn [R]. RTINGS says "partially" compatible with Linux [R]. Solaar pairs and manages Unifying receivers; this model's full Solaar support is uncertain [R] | €24–50 (2026 EU snapshots) [R]; US [U] | Good couch admin keyboard. Not a remote |
| **Rii i8+** | 2.4 GHz nano receiver | Riitek USB IDs `1997:0409` and `1997:2433` ("wireless mini keyboard with touchpad") are in `usb.ids` [M]. Plain HID [I]. Sold for Raspberry Pi/Kodi [R] | [U] | Fine, small |
| **Rii i4** | BT 4.0 + 2.4 GHz (Fn+Tab = BT, Fn+Caps = 2.4G) | "Works with Linux" vendor copy only [R] | [U] | Use 2.4G mode on this Spark (MT7925) [I] |

None of these has a mic. They solve text entry: search, Wi-Fi passwords, Steam login. The phone PWA (§6) also covers
text entry for free.

---

## 5. Gamepads (games and launcher UI)

| Pad | Status on this Spark | UI navigation | Mic | Notes |
|---|---|---|---|---|
| **Xbox Wireless Controller** (Series, BLE, `045E:0B13`) | **Paired, connected, sends input** [M]. Needs BlueZ `Privacy=device` + `JustWorksRepairing=always` [M: GameSpark README]. USB-C uses `xpad` [D] | Gamepad API in Chrome (joystick ACL [M]). Guide = `BTN_MODE`, Share = `KEY_RECORD` [M] | none [I] | $64.99 list since May 2025; deals ≈$44 [R] |
| **DualSense** (`054C:0CE6`) | **Paired over BT** [M] (`hid-playstation`). Touchpad is a separate mouse device (`mouse0`) [M]. It moves the X pointer [I] | Gamepad API; PS = `BTN_MODE` [M] | **USB only.** Over USB it is a standard UAC device with a mic [R: kernel maintainers, reviews]. Over BT, audio is a proprietary compressed stream with no Linux support (maintainer, June 2025) [R] | $74.99 list [R]. **Zero-cost PTT mic for prototyping:** plug it in by USB-C [I] |
| **8BitDo Ultimate 2C Wireless** | Not owned. XInput dongle → `xpad`. This kernel's `xpad` matches vendor `2DC8` on Xbox 360 and One interface classes [M]. Mainline adds IDs 0x310a (2C) and 0x310b (Ultimate 2 Wireless) [D] | Gamepad API | none | $29.99 launch, $24–26 deals [R] |
| 8BitDo Ultimate 2 Wireless | Same family: xpad in XInput; DInput dongle mode can show `2dc8:6012`, bound by hid-generic [R] | | none | Gyro and back buttons via Steam Input with firmware updates [R] |
| 8BitDo Pro 3 | Dongle = S-input/X-input; D-input needs BT or wired [R]. 8BitDo's Steam page says to use D-input or BT for Steam [D] | | none | 8BitDo's web firmware updater (Chromium WebHID, beta Aug 2026) can run from the Spark's Chrome [R] |
| **Steam Controller (2026)** | $99, released 2026-05-04 [R]. 2.4 GHz "Puck" (up to 4 pads; USB-C), BLE, USB-C [R]. **Kernel 6.17 has no driver for it** (hid-steam aliases 1102/1142/1205 only [M]). Mainline 7.3 adds "HID: steam: Initial 2026 Steam Controller support" (Vicki Pfau, commit `0a80b4e`, 2026-08-14) [M]. On this Spark it works through **Steam Input only** (hidraw uaccess for `28de` exists [M]). Without Steam, the firmware's "lizard mode" is keyboard + mouse [R] | Through Steam's desktop layout only | none | Great for Steam games; poor for a Chrome launcher until DGX OS ships kernel ≥ 7.3 [I] |
| Switch 2 Pro Controller (`057e:2069`) | **Not supported.** Mainline 7.3-rc6 `hid-ids.h` has no Switch 2 IDs [M]. The "HID: nintendo: Add preliminary Switch 2 controller driver" series reached v13 (Aug 2026), USB-only, unmerged [R]. BlueZ plugin patches (Mar–Apr 2026) unmerged [R]. Userspace `switch2-controllers-linux` claims BLE support [R] | — | — | Don't buy for this box |

Launcher notes:

- Steam (snap) running with Steam Input may add virtual pads and intercepts Guide/PS [I: `ui-session.md` C.8]. Gate
  launcher gamepad handling on `document.hasFocus()` so a game's input never moves tiles [I].
- Chrome exposes a pad to a page only after a button press on that page (Gamepad API privacy rule) [I].

---

## 6. Phone as a remote

| Option | Works here? | Voice | Effort | Verdict |
|---|---|---|---|---|
| **Custom PWA served by the launcher** (D-pad, swipe touchpad, keyboard field, voice) | Yes. Plain HTTP on the LAN works for buttons and text. iOS resolves `spark.local` [I] | (1) **iOS keyboard dictation** into a text field works over plain http: it is an OS keyboard feature, independent of the page [I, high confidence; test]. (2) Raw audio (`getUserMedia` → whisper) **needs a secure context**. On http, `navigator.mediaDevices` is `undefined` [D: MDN] | Small; it reuses the semantic-action WebSocket | **Recommended.** Same contract as the remotes and the agent |
| HTTPS for (2) | `tailscale0` exists on the Spark [M]. `tailscale serve --bg localhost:<port>` gives an HTTPS URL on the tailnet with an auto-provisioned certificate [D]. `tailscale cert` gives Let's Encrypt files (90 days; renewal is yours) [D]. Machine names are published in Certificate Transparency logs [D] | getUserMedia then works | The phone must run Tailscale | Do this only if dictation is not enough |
| **KDE Connect (iOS 0.5.6) + GSConnect** (GNOME extension) | App Store lists virtual touchpad and keyboard, remote commands, presentation remote, clipboard [D]. iOS app is foreground-only [D]. GSConnect must not be installed alongside the KDE desktop app [R]. Xorg is the easier case for synthetic input [R/I] | none | Install and pair | OK as a free stopgap; no TV semantics |
| Unified Remote | Server 3.14 has an **ARM64 .deb** (built for Raspberry Pi 64-bit) and a portable archive [D] | app-level | Proprietary | Not needed |

---

## 7. Hands-free voice (far-field mic + wake word)

### 7.1 Mic hardware

| Device | Interface | Linux | AEC with the TV playing | Price | Verdict |
|---|---|---|---|---|---|
| **Seeed reSpeaker XVF3800 USB 4-Mic Array** | USB-C, UAC2. VID:PID `2886:001A` [D] | Driverless; `arecord -D plughw:N,0 -c 2 -r 16000 -f S16_LE` [D]. Firmware v2.1.1: default, `16k6ch`, `48k2ch` (repo updated 2026-09-29) [M]. Host tool binaries for `rpi_64bit` and `jetson` (aarch64), plus `python_control/xvf_host.py` (pyusb; `LED_COLOR`, `DOA_VALUE`, `AUDIO_MGR_OP_*` channel routing, `SAVE_CONFIGURATION`) [M] | On-chip AEC uses **audio played to the device itself** as reference (categories "Reference…" in the routing table) [D]. A user reports AEC never engaging when playback bypassed the reference input (ESP32/I2S case) [R]. With TV audio on HDMI, either mirror the HDMI stream to the XVF3800's USB playback, or use PipeWire AEC (§7.2) [I] | $49.99–60.99; $62.99 with case [R] | **Pick** |
| reSpeaker USB Mic Array v2.0 (XVF3000) | USB, UAC1; 1-ch or 6-ch firmware [D] | Works; DFU scripts are Python 2-era [D] | Same reference problem | old stock | XMOS EOL: last buy 2025-03-31, last ship 2025-09-30 [R]. Avoid |
| reSpeaker Lite (USB, XU316, 2 mics) | USB, UAC2, 16 kHz max [D] | Driverless [D] | AEC on USB firmware disputed on Seeed's forum [R] | ≈$25–29 [R] | Budget option |
| Home Assistant Voice Preview Edition | Wi-Fi (ESP32-S3 + XU316) | Needs Home Assistant (ESPHome API); on-device microWakeWord [D] | Own speaker only | $59 [R] | Only if you also run HA |
| Jabra Speak2 40 / Speak 510, Anker PowerConf S330 | USB speakerphones (UAC) | Jabra has Linux firmware tooling (Xpress, 2025) [D]. Anker sells it as plug and play [R]. PipeWire reports thin [R] | AEC cancels **their own speaker** only [I] | ≈$60–150 [U] | Good when replies play from the puck; weak against TV audio |

### 7.2 Echo cancellation against TV audio (software, no purchase)

PipeWire's `libpipewire-module-echo-cancel` (WebRTC AEC, present [M]) creates a capture/source pair and a sink/playback
pair. With **`monitor.mode = true`** it takes the **default sink's monitor** (the HDMI output) as the reference, so
apps need no re-routing [D]. Wake word and STT then read "Echo Cancellation Source". The TV adds its own audio delay
[U]. Test with a 10-minute show playing at normal volume and count false and missed wakes.

```ini
# ~/.config/pipewire/pipewire.conf.d/60-sparkhome-aec.conf   (user-level; no root)
context.modules = [
  { name = libpipewire-module-echo-cancel
    args = {
      library.name = aec/libspa-aec-webrtc
      monitor.mode = true
      capture.props = { node.name = "sparkhome.aec.capture" }    # set target.object to the XVF3800 if needed
      source.props  = { node.name = "sparkhome.aec.source" media.class = "Audio/Source" }
    }
  }
]
```

Also duck or pause media on wake. That is cheap and fixes most residual echo [I].

### 7.3 Wake-word engines (aarch64, Python 3.12)

| Engine | Version / date | aarch64 | Models | License | Verdict |
|---|---|---|---|---|---|
| **pymicro-wakeword** (OHF) | 2.5.0, 2026-09-17 [M: PyPI] | `manylinux_2_35_aarch64` wheel [M] (Ubuntu 24.04 glibc 2.39 is OK) | Built-in `okay_nabu`, `hey_jarvis`, `alexa`, `hey_mycroft` [M: repo] | Apache-2.0 | **Pick.** 16 kHz mono, 10 ms frames; `arecord … | python3 -m pymicro_wakeword --model okay_nabu` [D] |
| pyopen-wakeword (rhasspy) | 1.1.0, 2025-10-28 [M] | aarch64 wheel [M] | `okay_nabu`, `hey_jarvis`, `alexa`, `hey_mycroft`, `hey_rhasspy` [M] | Apache-2.0 | Second engine or ensemble |
| openWakeWord (dscripka) | 0.6.0, 2024-02-11 [M] | pure Python. `tflite-runtime` has **no cp312 aarch64 wheel** → use `onnxruntime` 1.30.0 (cp312 aarch64 wheel) [M] | Pre-trained models are **CC BY-NC-SA 4.0** [R: HF]; custom models train from Piper synthetic data (1–2 h, community estimate) [R] | code Apache-2.0 | For custom wake words |
| Porcupine (Picovoice) | `pvporcupine` 4.0.3, 2026-06-25 [M] | Pi/Jetson builds [D] | Custom via Console | Needs an AccessKey checked at startup. **Free-tier keys were to stop working after 2026-06-30, "no non-commercial tier planned"** [R: HA forum quoting Picovoice, 2026-06-04] | Avoid |
| OHF `linux-voice-assistant` | experimental | aarch64 supported [D] | openWakeWord or microWakeWord | Apache-2.0 | Requires Home Assistant (ESPHome API, port 6053) [D]. Not needed |

Pipeline: XVF3800 (or the remote's UAC mic for PTT) → PipeWire AEC source → `pymicro-wakeword` → VAD/endpoint
(Silero VAD or a simple energy gate [I]) → whisper.cpp (installed) → intent → Piper TTS (installed).

---

## 8. TV control

### 8.1 What this TV offers

- GB10 has no HDMI-CEC (brief). The Spark has no `/dev/cec*`, no `/sys/class/cec` and no `/sys/class/rc` today
  [M: `ls`].
- The TV gives no network answer: no SSDP or UPnP reply from any Sony device [M]. Either it is not networked, it is
  in standby with networking off, or it is a pre-2013 model. Home Assistant supports "almost all Sony Bravia TV 2013
  and newer" and points older TVs to HDMI-CEC or Broadlink [D].
- So **CEC is the primary path.** The other options are an IR blaster (§8.4) and Sony IP control if the TV turns out
  to be networked (§8.3).

### 8.2 Pulse-Eight USB-CEC adapter on Linux arm64

| Item | Detail |
|---|---|
| Hardware | P8-USBCECv1, inline HDMI. Pass-through **up to 4K60 4:2:0 8-bit**; 4K@120 and some HDR not supported (hardware limit) [D: Pulse-Eight]. **This TV's EDID is 1080p-only [M], so no limit applies.** Alternative: CEC-only on a spare TV HDMI port [D]. USB IDs `2548:1001` / `2548:1002` [D: kernel doc] |
| Stack A (recommended): kernel CEC | `pulse8-cec.ko` [M] + `inputattach --pulse8-cec /dev/ttyACMx` (installed [M]) creates `/dev/cecN`. Control with `cec-ctl` (`apt install v4l-utils` [M candidate]). The rc-core remote input is built in (`CONFIG_MEDIA_CEC_RC=y` [M]). `persistent_config=1` stores settings in the adapter's EEPROM [D] |
| Stack B: libcec | `cec-utils` 6.0.2 [M candidate]; `cec-client` console commands `on`, `standby`, `as`, `is`, `p`, `pa`, `osd`, `volup`, `voldown`, `mute`, `pow` [M: source]. libcec opens `/dev/ttyACM*` itself (group `dialout` [M]). **Don't run A and B together**: `inputattach` holds the tty [I] |

Stack A setup (root once; based on the kernel admin guide):

```ini
# /etc/systemd/system/pulse8-cec-inputattach@.service
[Unit]
Description=inputattach for pulse8-cec device on %I
[Service]
Type=simple
ExecStart=/usr/bin/inputattach --pulse8-cec /dev/%I
```

```udev
# /etc/udev/rules.d/70-pulse8-cec.rules
SUBSYSTEM=="tty", KERNEL=="ttyACM[0-9]*", ATTRS{idVendor}=="2548", ATTRS{idProduct}=="1002", ACTION=="add", TAG+="systemd", ENV{SYSTEMD_WANTS}+="pulse8-cec-inputattach@%k.service"
SUBSYSTEM=="tty", KERNEL=="ttyACM[0-9]*", ATTRS{idVendor}=="2548", ATTRS{idProduct}=="1001", ACTION=="add", TAG+="systemd", ENV{SYSTEMD_WANTS}+="pulse8-cec-inputattach@%k.service"
SUBSYSTEM=="cec", TAG+="uaccess"
```

Runtime (as `spark` once the uaccess ACL applies). [D] marks commands from the kernel guide or man page. [I] marks
commands to confirm with `cec-ctl --help-all`:

```bash
cec-ctl -d0 -p 1.0.0.0 --playback --osd-name SparkHome   # claim a Playback address at the EDID's 1.0.0.0 [D/I]
cec-ctl -d0 -S                                           # topology: the TV should appear at 0.0.0.0 [D]
cec-ctl -d0 --to 0 --image-view-on                       # wake the TV [D]
cec-ctl -d0 --to 15 --active-source phys-addr=1.0.0.0    # switch the TV to the Spark's input [I]
cec-ctl -d0 --to 0 --standby                             # TV off [I]
cec-ctl -d0 -M                                           # monitor; press TV-remote keys to see what Sony sends [D]
```

TV remote keys through CEC: RC passthrough is on by default [D]. The `rc-cec` keymap maps UI command → key:

| CEC code | Key |
|---|---|
| 0x00 Select | `KEY_OK` |
| 0x01–0x04 | arrows |
| 0x09 Root Menu | `KEY_ROOT_MENU` |
| 0x0A Setup | `KEY_SETUP` |
| 0x0B Contents Menu | `KEY_MENU` |
| 0x0D Exit | `KEY_EXIT` |
| 0x20–0x29 | `KEY_NUMERIC_0–9` |
| 0x40 Power | `KEY_POWER` |
| 0x41–0x43 | volume, mute |
| 0x44 Play | `KEY_PLAYCD` |
| 0x45 Stop | `KEY_STOPCD` |
| 0x46 Pause | `KEY_PAUSECD` |
| 0x48 / 0x49 | rewind / fast-forward |
| 0x4B / 0x4C | Forward / Backward → `KEY_FORWARD` / `KEY_BACK` |

Source: [M: rc-cec.c]. Read this device in `sparkhome-inputd`: OK, Root Menu and the digits are invisible to X11, and
Power is a hazard. Turn on the TV's HDMI control setting ("BRAVIA Sync" / "Control for HDMI"; the menu path depends
on the model [U]).

The flow this enables [I]:

- TV off, remote Home pressed → daemon sends Image View On + Active Source → TV wakes on HDMI 1 → launcher shows Home.
- Idle timeout → CEC Standby (only after the thermal check in §9.3).

### 8.3 Sony BRAVIA IP control (if the TV is in fact networked; 2013+ models)

- Enable it: Settings → Network → Home network setup → IP control → Authentication "Normal and Pre-Shared Key", set
  the PSK [D: Sony getting started; HA]. Power-on from standby requires "Remote Start" (network standby). Home
  Assistant notes ≈15 W standby while polled [D].
- Power: `POST http://<tv>/sony/system` with header `X-Auth-PSK: <psk>`, body
  `{"method":"setPowerStatus","id":55,"params":[{"status":true}],"version":"1.0"}` [D].
- Input: `POST /sony/avContent` with `{"method":"setPlayContent","id":101,"params":[{"uri":"extInput:hdmi?port=1"}],"version":"1.0"}`
  [D/R: Sony walkthrough uses port=2 for HDMI 2].
- IRCC (SOAP) and Simple IP Control (TCP, fixed-size messages) also exist [D: pro-bravia overview]. Exact message
  formats were not captured: Sony's documentation site now redirects subpages [U].
- Check with:
  `curl -s -X POST http://<tv>/sony/system -d '{"method":"getPowerStatus","id":1,"params":[],"version":"1.0"}'` and
  `curl http://<tv>/sony/system` (an error JSON means the API exists).

### 8.4 IR blaster fallback (no CEC)

- An mceusb transceiver with emitter jacks, plus `ir-ctl` [D]. `ir-ctl -S sony12:0x10015` sends Sony SIRC device 1,
  command 0x15 (rc-core packs device into bits 16–20 and function into bits 0–6) [D: rc-protos]. That code is the TV
  power toggle in common IR databases [R, unverified]. SIRC TVs usually need the frame repeated about 3 times [I].
  Capture the real codes first with `ir-keytable -p sony -t` [I].
- Broadlink RM4 mini (Wi-Fi IR blaster, local `python-broadlink`). Home Assistant suggests it for pre-2013 Sony TVs
  [D]. Price not checked [U].
- The remote's own IR-learn key handles TV power from the couch, if it learns SIRC (§3.1).
- [I] If a Chromecast is plugged into the TV, casting can wake the TV over
  CEC, but it switches the TV to the Chromecast's input. It is not a substitute.

---

## 9. Spark power states, wake sources and thermals

### 9.1 Suspend and hibernate

- Suspend is disabled by policy (`AllowSuspend=no` [M]). The kernel offers only `s2idle`, and there is no hibernate
  [M].
- The forum answer to "is sleep/suspend disabled by default?" says yes, and that it can be overridden [R]. A
  separate thread traced unexpected idle "standby" to GDM's own auto-suspend settings [R].
- NVIDIA's porting guide rates suspend/resume support as basic [R: search summary].
- Don't build on suspend [I].

### 9.2 Wake from off (WoL, USB, Bluetooth)

- **WoL is not supported**: NVES, NVIDIA forum, 2025-10-18: "confirmed Spark does not support wake-on-lan" [R].
- Users still report `ethtool enP7s7` showing `Supports Wake-on: pumbg` and `Wake-on: g`, yet magic packets don't wake
  it [R]. Here `enP7s7` has no carrier at all [M].
- USB or Bluetooth wake from S5: no evidence; assume no [I].
- **Power-on workaround:** leave the BIOS power-restore behaviour at its default "auto boot" and use a smart plug.
  Shut down cleanly, cut power, restore power → boots [R]. Idle draw ≈37–40 W [R].

### 9.3 Display-off thermal risk (test before any "TV off" feature)

- Forum thread (Aug–Sep 2026) [R]: fans stop when the display blanks (DPMS) or the unit runs headless, and the
  chassis gets too hot to touch. NVIDIA called it "a genuine safety concern", escalated it, and listed interim
  workarounds: disable DPMS, keep a VNC session with an active app, or downgrade the EC.
- A user's root-cause theory: `mtk_pcie_hotplug` removes an uncabled ConnectX-7 ≈16 s after boot, and the lower idle
  power lets the EC stop the fans. Blacklisting the module kept the fans running at ≈17 W extra [R].
- **This unit matches**: same OS, kernel, driver and BIOS as the thread's original poster; `mtk_pcie_hotplug` is
  loaded; no ConnectX-7 on PCIe [M]. Today the screen never blanks (DPMS timers 0, `idle-delay 0`) [M].
- Gate before shipping CEC standby: put the TV in standby for 30 minutes and log
  `cat /sys/class/thermal/thermal_zone*/temp` every 30 s. Watch for a steady rise without plateau versus the
  display-on baseline (40.8–42.4 °C at idle-ish [M]).
- Whether TV standby changes the Spark's HDMI state (hotplug, DPMS) on this NVIDIA driver is [U]. Another thread
  reports monitors not waking after idle on USB-C DisplayPort, no root cause [R].

---

## 10. Ranked shopping list (2026)

| Rank | Item | Price | Out of the box | Needs | For |
|---|---|---|---|---|---|
| 1 | **G20S Pro voice air mouse, 2.4 GHz dongle** | ≈$16–30 [R] | keys, pointer, UAC mic [R] | udev uaccess + `sparkhome-inputd` mapping; acceptance test §3.6 | couch remote + push-to-talk |
| 2 | **Pulse-Eight USB-CEC adapter P8-USBCECv1** | ≈$48 (store), £42.49 (UK) [R] | kernel driver + inputattach present [M] | root once: service + udev; `apt install v4l-utils` | TV on/off/input + TV remote passthrough |
| 3 | **reSpeaker XVF3800 USB 4-Mic Array (with case)** | $62.99 (bare $49.99–60.99) [R] | UAC2 capture [D] | PipeWire AEC config; `pymicro-wakeword` venv | hands-free wake word (phase 2) |
| 4 | 8BitDo Ultimate 2C Wireless | $24–30 [R] | `xpad` in XInput mode [M/D] | none | Bluetooth-free second pad (optional) |
| 5 | FLIRC USB v2 | $24.95 [M: `apple-remotes.md`] | USB keyboard (`20a0:0006`) | program once (Mac or arm64 `flirc_util`) | Apple IR remote or the Sony remote without CEC |
| 6 | Logitech K400 Plus | €24–50 [R] | HID | none | text entry/admin (optional) |
| — | USB-C→USB-A adapter (if the hub runs out) | a few $ [U] | — | — | dongles on the USB-C-only rear |
| — | Smart plug | [U] | — | BIOS auto power-on (default) [R] | remote cold boot |

Already owned, use first: Xbox Wireless Controller (BLE, working), DualSense (BT; USB for the mic), iPhone (PWA
remote + dictation), Apple remote (with FLIRC; see `apple-remotes.md`).

Don't buy for this box (2026-10):

- Switch 2 Pro: no mainline driver [M].
- Steam Controller 2026 as a launcher remote: no kernel driver before 7.3 [M].
- Porcupine: free tier ended [R].
- Fire TV remote: no Linux voice [U].
- Onn/Google TV remotes as a primary remote: the kernel rejects the descriptor, so a BLE daemon is required [D].
- XVF3000 v2.0: end of life [R].

---

## 11. Works out of the box vs. needs configuration

| Device | Works now / on plug-in | Needs |
|---|---|---|
| Xbox controller (BLE) | ✅ [M] | Keep the BlueZ `Privacy=device` + `JustWorksRepairing=always` settings; `pair.sh --reset` when the MT7925 wedges [M: GameSpark] |
| DualSense | ✅ BT input [M]; mic only on USB [R] | USB-C cable for the mic |
| G20S Pro dongle | keys + pointer + mic [R] | uaccess rule, grab and mapping in the daemon; avoid `KEY_SEARCH`/`KEY_POWER` leaking to GNOME |
| Pulse-Eight | driver present [M] | root setup (§8.2); TV-side HDMI control on |
| XVF3800 | UAC2 [D] | AEC config (§7.2); optional firmware/routing via `xvf_host.py` (pyusb) |
| 8BitDo Ultimate 2C | ✅ XInput via `xpad` [M/D] | — |
| FLIRC | ✅ as a keyboard | one-time learning; choose keys from §2.1's "safe" list (F14–F18, arrows, Enter, Esc) |
| K400 Plus / Rii | ✅ HID [R/I] | — |
| Phone PWA | — (your code) | HTTPS only for raw mic capture |
| Steam Controller 2026 | Steam Input only [M/D] | Steam running; kernel ≥ 7.3 for a native driver |
| ATVV BLE remote | keys maybe; voice no | BLE daemon (bleak) + ADPCM decode |

---

## 12. Setup recipes (root steps flagged)

`spark` has no passwordless sudo. GameSpark's scripts fall back to a privileged container for root [M: README]. Use
the same mechanism, with the user's approval.

```bash
# [root] packages for CEC and debugging (all have arm64 candidates [M])
apt install v4l-utils evtest            # v4l-utils provides cec-ctl
apt install ir-keytable                 # separate package; only for an IR receiver or rc keymaps

# [root] udev rules from §2.4 and §8.2, then:
udevadm control --reload && udevadm trigger -s input -s cec -s tty

# [user] check the ACLs applied
getfacl -p /dev/input/event* /dev/cec* 2>/dev/null | grep -B3 'user:spark'

# [user] CEC smoke test
cec-ctl -d0 -p 1.0.0.0 --playback --osd-name SparkHome && cec-ctl -d0 -S && cec-ctl -d0 --to 0 --image-view-on

# [user] wake-word smoke test (venv; aarch64 wheel exists)
python3 -m venv ~/.venvs/wake && ~/.venvs/wake/bin/pip install pymicro-wakeword
arecord -D <aec-or-mic> -r 16000 -c 1 -f S16_LE -t raw | ~/.venvs/wake/bin/python -m pymicro_wakeword --model okay_nabu
```

hwdb fallback (only for keys that must work when `sparkhome-inputd` is not running). Use the table in
`ui-session.md` §C.6 (Home→`f14`, Search→`f15`, Config→`f16`, Power→`f17`, Back→`esc`) and add:

```
# /etc/udev/hwdb.d/61-sparkhome-remote.hwdb   (property lines start with ONE space)
evdev:input:b0003vVVVVpPPPP*
 KEYBOARD_KEY_c0041=enter        # Menu Pick (OK) -> Enter   (KEY_SELECT 353 is invisible to X11)
 KEYBOARD_KEY_c00cf=f18          # Voice Command -> F18      (KEY_VOICECOMMAND 582 is invisible to X11)
```

Apply with `systemd-hwdb update && udevadm trigger -s input` [root].

---

## 13. Open questions (each closes with one measurement)

1. G20S Pro button codes in both air-mouse modes, mic-button key/release behaviour, and a 60 s recording stability
   test on GB10 xHCI (§3.6).
2. Does this Sony forward arrows, OK and Return over CEC to a Playback device, and which UI codes does it send?
   `cec-ctl -M` while pressing keys.
3. Sony TV model and year (back label or Settings → About). Decides Bravia IP control, and whether IR learning or the
   CEC menu paths apply.
4. Thermal behaviour with the TV in CEC standby (§9.3). Also: does TV standby or wake cause HDMI re-detection and an X
   layout change?
5. Does PipeWire AEC (monitor mode) hold the wake-word false-accept rate at an acceptable level with TV audio at
   normal volume? Is TV audio latency within WebRTC AEC's range?
6. Does Steam Input (snap) create duplicate virtual pads or capture Guide while the launcher has focus?
7. Chrome kiosk behaviour on `BrowserBack`/`BrowserHome` if they leak (`ui-session.md` says Chrome navigates).
   Moot if the daemon grabs the remote.
8. ATVV on MT7925 + BlueZ 5.72: reconnect latency, MTU, and whether HOG coexists with the voice service.
9. Can the G20S Pro learn Sony SIRC power? Is the Pro Plus's second learnable key usable for input switching?
10. Exact `cec-ctl` option spelling for `--active-source`/`--standby` on v4l-utils 1.26.1 (`cec-ctl --help-all`).

---

## 14. Sources (accessed 2026-10-07/08)

Remotes and ATVV:

- CoreELEC, G20S Pro USB mic on Ugoos SK1 (2026-04):
  https://discourse.coreelec.org/t/ugoos-sk1-s928x-hard-lockup-when-using-usb-microphone-g20s-pro/58460
- Arch forum, G20S PRO BT ATVV UUID, no mic: https://bbs.archlinux.org/viewtopic.php?id=285325
- onn-remote-linux (ATVV protocol, Python): https://github.com/FloMaetschke/onn-remote-linux
- BlueZ issue #1086: https://github.com/bluez/bluez/issues/1086
- GitHub topic atvv: https://github.com/topics/atvv
- open-voice-bridge (16 kHz ADPCM, RC003): https://github.com/nijez/open-voice-bridge
- remote-mic-app (Linux port): https://github.com/layneyu/remote-mic-app
- G20S Pro BT review (2022): https://androidpctv.com/g20s-pro-bt-review/
- MythTV MX3 wiki: https://wiki.mythtv.org/wiki/MX3_Air_Mouse_Remote
- G20S Pro listings:
  - https://www.amazon.com/Backlit-Wireless-Learning-Projector-Pro/dp/B0CYBX2HGY
  - https://www.neweggbusiness.com/dupad-story-g20s-pro-plus-nvidia-shield-pc-projector-smart-tv-android-tv-box/p/9B-17A-00Y5-003C2
  - https://www.newegg.com/greva-g20s-pro-plus/p/17A-00Y5-001X0
- Nordic DevZone, voice over HID ADPCM:
  https://devzone.nordicsemi.com/f/nordic-q-a/25002/android-tv-voice-input-with-smart-remote-3/98469
- OSMC, Fire TV remote: https://discourse.osmc.tv/t/amazon-alexa-remote-control/78724
- LibreELEC, G20S keys: https://forum.libreelec.tv/thread/23842-some-buttons-dont-work-on-usb-remote-g20s-after-installing-libreelec-rpi4-arm-9/
- CoreELEC, G10S: https://discourse.coreelec.org/t/g10s-dont-know-where-the-remote-conf-is/17092
- Apple, identify your remote: https://support.apple.com/en-us/103233
- SiriRemote-Linux: https://github.com/Yanndroid/SiriRemote-Linux
- LibreELEC, Siri Remote: https://forum.libreelec.tv/thread/14857-apple-tv-remote-4th-generation-siri-remote/
- FLIRC arm64 tarball (HEAD measured): http://apt.flirc.tv/arch/arm64/flirc.latest.arm64.tar.gz
- Kodi wiki, FLIRC: https://kodi.wiki/view/FLIRC

Keyboards:

- RTINGS, K400 Plus: https://www.rtings.com/keyboard/reviews/logitech/k400-plus
- K400 Plus price: https://www.tomshw.it/offerte/logitech-k400-plus-al-46-laffare-per-gestire-pc-e-tv-dal-divano
- K400 Plus price comparison: https://www.comparateur-gamer.fr/prix/logitech-k400-plus-black-5099206059276
- Pi Hut, Rii i8+: https://thepihut.com/products/rii-i8-plus-mini-wireless-keyboard-with-touchpad
- usb.ids: http://www.linux-usb.org/usb.ids

Gamepads:

- 8BitDo Ultimate 2 xpad commit: https://github.com/torvalds/linux/commit/22cd66a5db56a07d9e621367cb4d16ff0f6baf56
- 8BitDo 2C patch: https://lkml.iu.edu/hypermail/linux/kernel/2410.1/10849.html
- 8BitDo Ultimate 2 gist (2dc8:6012): https://gist.github.com/barraIhsan/783a82bcf32bed896c85d27dbb8018a5
- 8BitDo 2C price at launch: https://www.techradar.com/gaming/the-8bitdo-ultimate-2c-could-be-the-cheap-pc-controller-youre-looking-for-when-it-launches-next-month
- 8BitDo Pro 3: https://www.gamingonlinux.com/2025/07/8bitdo-pro-3-bluetooth-announced-with-swappable-magnetic-abxy/
- 8BitDo Steam page: https://www.8bitdo.com/steam/
- 8BitDo web updater: https://gamingonlinux.com/2026/08/8bitdo-announce-ultimate-software-online-to-update-controllers-in-your-browser-to-help-linux-gamers
- Steam Controller (2026), GamingOnLinux: https://www.gamingonlinux.com/2026/05/the-new-steam-controller-from-valve-is-out-now-some-early-thoughts/
- Steam Controller, Linux 7.3 driver: https://hwbusters.com/news/linux-7-3-gives-the-2026-steam-controller-a-real-kernel-driver-steam-client-not-required/
- Steam Controller kernel commit: https://github.com/torvalds/linux/commit/0a80b4e8ec6a6e40937c7abdf8fb2ebe6cc7c1e5
- GamersNexus latency: https://gamersnexus.net/handheld-pcs-peripherals/valve-steam-controller-review-latency-benchmarks-battery-life
- Switch 2 driver v13: https://ratatoskr.run/linux-input/2026/08/17399481/t
- Switch 2 BlueZ patches: https://ratatoskr.run/linux-bluetooth/2026/04/9705357/t
- Switch 2 on omarchy: https://github.com/omacom/omarchy-pkgs/issues/719
- Switch 2 userspace: https://github.com/PiersIdk/switch2-controllers-linux
- `hid-ids.h` on master: https://github.com/torvalds/linux/blob/master/drivers/hid/hid-ids.h
- DualSense audio jack handling: https://phoronix.com/news/Sony-DualSense-Audio-Handling
- DualSense audio jack patch v2: https://lkml.rescloud.iu.edu/2507.0/04244.html
- DualSense on Linux review: https://boilingsteam.com/dualsense-controller-on-linux-works-good-with-some-limitations/
- Xbox controller price: https://www.gematsu.com/2025/05/xbox-series-global-price-increase-announced-for-consoles-controllers-and-games
- DualSense price: https://thecontrollerpeople.com/ps5-controller-pricing-in-2026/

Phone:

- MDN getUserMedia: https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia
- Tailscale HTTPS: https://tailscale.com/kb/1153/enabling-https
- Tailscale serve: https://tailscale.com/kb/1242/tailscale-serve
- KDE Connect iOS (App Store): https://apps.apple.com/app/id1580245991
- KDE Connect iOS source: https://github.com/KDE/kdeconnect-ios
- Unified Remote downloads: https://www.unifiedremote.com/download

Voice:

- XVF3800 wiki: https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/
- XVF3800 repo: https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY
- XVF3800 Python control: https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/blob/master/python_control/readme.md
- XVF3800 store: https://www.seeedstudio.com/ReSpeaker-XVF3800-USB-Mic-Array-p-6488.html
- XVF3800 with case: https://www.seeedstudio.com/ReSpeaker-XVF3800-USB-4-Mic-Array-With-Case-p-6490.html
- CNX Software, XVF3800 launch: https://www.cnx-software.com/2025/07/29/respeaker-xmos-xvf3800-4-mic-array-board-features-esp32-s3-module-works-over-usb/
- XVF3800 AEC issue #36: https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/issues/36
- XVF3000 FAQ: https://wiki.seeedstudio.com/respeaker_xvf3000_faq/
- reSpeaker Lite: https://wiki.seeedstudio.com/reSpeaker_usb_v3/
- reSpeaker Lite AEC thread: https://forum.seeedstudio.com/t/respeaker-lite-aec-support/283114?page=2
- HA Voice PE launch: https://cnx-software.com/2024/12/20/59-voice-preview-edition-adds-an-offline-smart-speaker-to-your-home-assistant-server
- Jabra Xpress for Linux: https://www.jabra.com/Support/release-notes/release-note-jabra-xpress-for-linux
- Anker PowerConf S330: https://uk.ankerwork.com/products/a3308
- PipeWire echo-cancel: https://docs.pipewire.org/page_module_echo_cancel.html
- pymicro-wakeword: https://pypi.org/project/pymicro-wakeword/ and https://github.com/OHF-Voice/pymicro-wakeword
- pyopen-wakeword: https://pypi.org/project/pyopen-wakeword/
- openWakeWord: https://pypi.org/project/openwakeword/
- openWakeWord model license: https://huggingface.co/davidscripka/openwakeword
- onnxruntime: https://pypi.org/project/onnxruntime/
- linux-voice-assistant: https://github.com/OHF-Voice/linux-voice-assistant
- Picovoice free tier ending: https://community.home-assistant.io/t/fyi-picovoice-confirmed-free-tier-accesskeys-will-stop-working-after-june-30-2026/1012744
- pvporcupine: https://pypi.org/project/pvporcupine/

TV control:

- Pulse-Eight product: https://www.pulse-eight.com/p/104/usb-hdmi-cec-adapter
- Pulse-Eight manual: https://support.pulse-eight.com/support/solutions/articles/30000048842-usb-cec-adapter-user-manual
- Pulse-Eight 4K@120 limit: https://support.pulse-eight.com/support/solutions/articles/30000053069-cec-adapter-4k-120hz-hdr-and-signal-issues-at-high-resolutions
- Pulse-Eight on Amazon UK: https://www.amazon.co.uk/Pulse-Eight-na-USB-CEC-Adapter/dp/B005JU6LWM
- Kernel CEC admin guide: https://www.kernel.org/doc/html/latest/admin-guide/media/cec.html
- CEC RC passthrough flag: https://www.kernel.org/doc/html/latest/userspace-api/media/cec/cec-ioc-adap-g-log-addrs.html
- cec-ctl man page: https://man.archlinux.org/man/cec-ctl.1.en
- rc-cec keymap: https://github.com/torvalds/linux/blob/master/drivers/media/rc/keymaps/rc-cec.c
- libcec: https://github.com/Pulse-Eight/libcec
- Home Assistant braviatv: https://www.home-assistant.io/integrations/braviatv/
- Sony setPowerStatus: https://pro-bravia.sony.net/develop/integrate/rest-api/spec/service/system/v1_0/setPowerStatus/
- Sony getting started: https://pro-bravia.sony.net/develop/integrate/rest-api/spec/getting-started/
- Classmethod BRAVIA REST walkthrough: https://dev.classmethod.jp/articles/bravia-rest-api/
- ir-ctl man page: https://man.archlinux.org/man/ir-ctl.1.en
- rc protocols: https://www.kernel.org/doc/html/latest/userspace-api/media/rc/rc-protos.html
- linuxhw EDID SNYEA01: https://github.com/linuxhw/EDID/tree/master/Digital/Sony/SNYEA01

Spark power and thermals:

- WoL: https://forums.developer.nvidia.com/t/cant-get-wake-on-lan-to-work/348168
- WoL: https://forums.developer.nvidia.com/t/why-doesn-t-the-dgx-spark-support-wake-on-lan-wol/360492
- Suspend default: https://forums.developer.nvidia.com/t/is-sleep-suspend-disabled-by-default/377582
- Standby: https://forums.developer.nvidia.com/t/configure-dgx-so-it-does-not-go-into-power-standby-mode/375767
- Fans stop: https://forums.developer.nvidia.com/t/dgx-spark-fans-stop-when-the-screen-goes-dark-or-running-from-ssh-box-gets-too-hot-to-touch-fire-hazard/378945
- Display wake: https://forums.developer.nvidia.com/t/dgx-spark-doesnt-wake-up-display-monitors-after-being-idle-for-few-minutes/358484

Input internals:

- hid-input.c: https://github.com/torvalds/linux/blob/master/drivers/hid/hid-input.c
- input-event-codes.h: https://github.com/torvalds/linux/blob/master/include/uapi/linux/input-event-codes.h
- hid-steam.c: https://github.com/torvalds/linux/blob/master/drivers/hid/hid-steam.c
- Chromium dom_code_data.inc: https://github.com/chromium/chromium/blob/main/ui/events/keycodes/dom/dom_code_data.inc
- X11 keycode limit: https://lists.freedesktop.org/archives/xorg/2010-April/049675.html
- xkeyboard-config range bug: https://bugs.archlinux.org/task/59024
