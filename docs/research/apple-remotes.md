# Apple remotes as the SparkHome remote: research report

Research date: 2026-10-07/08. Target: NVIDIA DGX Spark, DGX OS 7.2.3 (Ubuntu 24.04.4 arm64), kernel
6.17.0-1029-nvidia, BlueZ 5.72, MediaTek MT7925 Bluetooth, GNOME on Xorg, Sony TV over HDMI at 1080p60.

Evidence labels used throughout:

- **[M] measured**: I ran it or fetched the artifact myself during this research (command or URL given).
- **[D] documented**: primary source (kernel/BlueZ source, vendor documentation, Apple support).
- **[R] reported**: community project or forum report, not reproduced here.
- **[I] inferred**: my reasoning from the above. Treat it as a hypothesis until tested.
- **[U] unknown**: nobody has published it, or I could not find it.

Nothing here was tested on the user's Spark or on a physical Apple remote. Every on-device step below is a
procedure with an evidence gate, not a verified result.

---

## 0. Summary (Problem → Evidence → Cause → Strategies → Decision → Remaining)

**Problem.** The user wants to drive an Apple-TV-style launcher on the Spark with a "Gen 1 Apple TV remote"
they own, possibly by "jailbreaking" it, and to have voice input.

**Evidence and cause.**

1. No jailbreak is needed, and none is practical. The IR Apple Remotes (A1156 white, A1294 aluminium) are
   fixed-function NEC-style IR transmitters with no updatable firmware. Any NEC-capable IR receiver can read
   them as they are [D: kernel `hid-appleir.c`, `ir-nec-decoder.c`]. The Siri Remotes are Bluetooth LE HID
   (HID-over-GATT) devices with a few Apple quirks. Linux can read every button, the touch surface and (on gen 3)
   the microphone from userspace after one "enable" write [R: two Linux projects, Yanndroid/SiriRemote-Linux and
   azais-corentin/siri-remote, plus ESP32 and macOS projects that agree on the protocol]. I found no public custom
   firmware [U], and none is needed.
2. The DGX Spark has **no built-in IR receiver**, and mainline Linux has **no kernel driver** for any Siri Remote
   [M: `hid-ids.h`/`hid-apple.c` on master contain no Siri Remote IDs; no udev-hid-bpf fixup exists].
3. The deepest technical obstacle for the Siri Remotes is ownership. BlueZ 5.72's `hog` plugin claims the HID
   service and hides it from D-Bus GATT clients [D: BlueZ 5.72 `src/device.c` and `src/gatt-client.c`]. The
   working Linux Siri Remote implementation (azais-corentin/siri-remote) therefore asks you to disable `hog`
   [R]. That would break the user's BLE Xbox Series controller, which needs `hog` [I: Xbox Series pads are BLE
   HID]. BlueZ ≥ 5.80 adds `[GATT] ExportClaimedServices = read-write` to resolve this [D: commit dbd6591bd1d0,
   2024-12-09], but Ubuntu 24.04 ships 5.72 [M].

**Strategies (ranked), and the decision.**

| Rank | Remote you own | Path | Voice | Effort | Confidence |
|---|---|---|---|---|---|
| 1 | A1156 or A1294 (IR) | **FLIRC USB** IR→USB-keyboard dongle, programmed once with `flirc_util` (arm64 build exists) | none | ~30 min, US$24.95 | high [D/M] |
| 2 | A1156 or A1294 (IR) | USB IR receiver with an rc-core driver (mceusb, ir_toy) plus an `ir-keytable` NEC32 keymap | none | 1–2 h | high [D] |
| 3 | Siri Remote gen 2/3 (A2540/A2854) | **Experiment E1**: keep BlueZ `hog`, send the enable report through `/dev/hidraw`, read reports 0xFA/0xFB/0xFC | yes (Opus) | 0.5–2 days | medium-low [I] |
| 4 | Siri Remote gen 2/3 | azais-corentin/siri-remote (Rust). Needs `hog` disabled on BlueZ 5.72, or BlueZ ≥ 5.80 with `ExportClaimedServices=read-write` | yes, PipeWire source | 1 day | medium [R] |
| 5 | Siri Remote gen 2/3 | ESP32-S3 bridge (Jakkumn/SiriRemoteESP, gen 3 tested). Today it outputs MQTT/ESPHome; a USB-HID+UAC firmware variant would make it a plug-in dongle | yes (on ESP) | 1–5 days | medium [R/I] |
| 6 | Siri Remote gen 1 (A1513/A1962) | Yanndroid/SiriRemote-Linux (Python, bluepy, root) for buttons and touch; E1 hidraw variant | not on Linux yet | 1–2 days | medium-low [R] |
| — | 2007 Apple TV box | Do not repurpose. FLIRC is cheaper and better (§8) | — | — | — |

**Decision (recommended).** Make **FLIRC plus an IR Apple Remote** the baseline remote. It does not depend on the
MT7925 radio, which the user has seen wedge, and the Spark sees it as a plain USB keyboard: arrow keys, Enter,
Escape, media keys. The Chrome kiosk, Steam Big Picture and GNOME all accept it with zero software. Keep the
**Xbox controller** for games. If the user's "Gen 1" remote is actually a **Siri Remote**, run **Experiment E1**
(§6.4, probe script in Appendix A) first. It is the only path that keeps BlueZ `hog` (the Xbox pad) and still gets
buttons, touch and voice. Fall back to rank 4 or 5 based on its result. Build the SparkHome input layer around
*semantic actions* (§9), so every remote maps onto the same 8–10 actions.

**Remaining unknowns.** Whether E1 works on a real Siri Remote; MT7925 BLE reliability with Apple remotes; the
gen-1 gyro/accelerometer format; gen-1 microphone on Linux; whether the Sony TV forwards remote keys over CEC (§11).

---

## 1. Which "Gen 1 Apple TV remote"? Identification

Two readings are most likely. The **remote that shipped with the 1st-generation Apple TV (2007, model A1218)** was
the **white Apple Remote A1156** [R: Apple's archived model page via search summary; Wikipedia]. The **1st-generation
Siri Remote** (2015, black glass top) is the other. Ask the user, or check the table.

| Model | Look | Buttons | Radio | Power | Shipped with / dates |
|---|---|---|---|---|---|
| **A1156** Apple Remote (white) | white plastic, 6 buttons | ring: Vol+ (up), Vol− (down), ⏮ (left), ⏭ (right); centre ⏯; Menu | **IR only**, 38 kHz, NEC-style | CR2032 | Front Row Macs, **Apple TV 1st gen**; Oct 2005–Oct 2009 [D: Wikipedia; R: listings] |
| **A1294** Apple Remote (aluminium, MC377LL/A) | thin aluminium, 7 buttons | ring up/down/left/right, centre Select, Menu, separate ⏯ | **IR only** | CR2032 (coin door) | Apple TV 2nd/3rd gen, Macs; from 20 Oct 2009 [D: Wikipedia, Apple "Identify your Apple TV remote" 103233] |
| **A1513** Siri Remote 1st gen | black aluminium, **glass touch surface** on the top third | Menu, TV/Home, Siri, ⏯, Vol± | **BLE 4.0** + IR blaster (TV volume) | Lightning, Li-Po 410 mAh | Apple TV HD (4th gen), Oct 2015 [D: Wikipedia; R: AppleDB] |
| **A1962** Siri Remote 1st gen Rev A | as A1513 plus a **white ring around Menu** | same | BLE + IR | Lightning | Apple TV 4K (2017) [R: AppleDB/The Apple Wiki; Wikipedia lists A1962 as the gen-1 model with FCC ID BCGA1513, which conflicts] |
| **A2540** Siri Remote 2nd gen | silver aluminium, **clickpad ring** (touch-enabled), power button top right, Siri on side | Back (‹), TV/Control Center, ⏯, Mute, Vol±, Power, Siri | BLE 5.0 + IR | Lightning | May 2021 [D: Wikipedia] |
| **A2854** Siri Remote 3rd gen | as A2540 | as A2540 | BLE + IR | **USB-C** | Nov 2022, current [D: Wikipedia] |

Sensor notes: gen 1 has an accelerometer and gyroscope (InvenSense part per Wikipedia). Gen 2/3 dropped both
[D: Wikipedia; R: MacStories]. Shared internals per Wikipedia: STMicro STM32L151QD (Cortex-M3) MCU and a Broadcom
BCM5976 touch controller on gen 1 [D: secondary].

---

## 2. Spark facts that matter for remotes

| Fact | Label | Source |
|---|---|---|
| Rear I/O: 4× USB-C (one is power, so **3 usable**), 1× HDMI 2.1a, 1× 10 GbE, 2× QSFP. **No USB-A, no IR receiver** | [D] | docs.nvidia.com/dgx/dgx-spark/hardware.html |
| Kernel `6.17.0-1029-nvidia` (Ubuntu archive build of the same version) ships modules `mceusb`, `ir_toy`, `rc-core`, `ir-nec-decoder`, `hid-appleir`, `hid-apple`, `hid-generic`, `uhid`, `pulse8-cec`, `snd-usb-audio`, `xpad`, `btusb`/`btmtk` | [M] | packages.ubuntu.com noble-updates arm64 `linux-modules-6.17.0-1029-nvidia` filelist |
| Kernel config: `CONFIG_INPUT_UINPUT=y`, `CONFIG_HIDRAW=y`, `CONFIG_UHID=m`, `CONFIG_HID_BPF=y`, `CONFIG_LIRC=y`, `CONFIG_RC_CORE=m`, `CONFIG_IR_NEC_DECODER=m`, `CONFIG_IR_MCEUSB=m`, `CONFIG_MEDIA_CEC_RC=y`, `CONFIG_USB_PULSE8_CEC=m`, `CONFIG_SERIO_SERPORT=m`, `CONFIG_BT_HCIBTUSB_MTK=y`, `CONFIG_HID_BATTERY_STRENGTH=y` | [M] | `linux-buildinfo-6.17.0-1029-nvidia_6.17.0-1029.29_arm64.deb` → `/usr/lib/linux/6.17.0-1029-nvidia/config` |
| Ubuntu 24.04 `ir-keytable` 1.26.1-4build3 ships `/usr/lib/udev/rules.d/70-infrared.rules` and `/etc/rc_maps.cfg` | [M] | packages.ubuntu.com noble/arm64/ir-keytable filelist |
| BlueZ 5.72 (Ubuntu 24.04 noble-updates `5.72-0ubuntu5.6`; daemon at `/usr/libexec/bluetooth/bluetoothd`). Latest upstream is 5.87 | [M] | user measurement; packages.ubuntu.com; kernel.org/pub/linux/bluetooth |
| `Privacy=device` and `JustWorksRepairing=always` already set; MT7925 occasionally wedges (power off/on fixes it) | [M] | task brief |
| User `spark` is not in `input`; no passwordless sudo | [M] | task brief |

On-device confirmation (one command, read-only):
`grep -E 'UINPUT|HIDRAW|UHID=|HID_BPF|IR_MCEUSB|IR_NEC|PULSE8|CEC_RC' /boot/config-$(uname -r); modinfo mceusb ir_toy pulse8-cec | grep -E '^(filename|alias)' | head`

---

## 3. IR Apple Remotes (A1156 white, A1294 aluminium)

### 3.1 Protocol

- **Physical layer:** NEC pulse-distance timing on a 38 kHz carrier: leader 9000/4500 µs, 0 = 560/560 µs,
  1 = 560/1690 µs, 32 data bits, LSB first [D: Wikipedia "Apple Remote"]. Holding a key sends NEC repeat frames
  [D: the LIRC configs below declare `repeat 9141 2157`; `hid-appleir.c` documents Apple receivers emitting
  key-repeat packets].
- **Data link (not NEC's checksum):** byte 1 = **0xEE**, byte 2 = **0x87** (Apple "custom code"), byte 3 =
  **command**, byte 4 = **remote ID** (0–255, the "pairing" byte). Bits 7..1 of the command byte are the key code.
  Bit 0 is a **parity bit over command and ID**, so the low bit of every command depends on the remote's ID
  [D: Wikipedia; corroborated by kernel `hid-appleir.c` comments, which list each key as "0x02 or 0x03", etc.].
- **Battery-low frames:** vendor byte 0xE0 instead of 0xEE (`25 87 e0 ..`) [D: `hid-appleir.c`].
- **A1294 two-frame keys:** **Centre** sends `0x5C|0x5D` and then `0x04|0x05`. **Play/Pause** sends `0x5E|0x5F` and
  then `0x04|0x05`. A decoder that keys on the second frame cannot tell them apart [D: `hid-appleir.c`
  "Bastien Nocera's remote … 5c followed by 05 gives you the middle button"; LIRC `MC377Z.lircd.conf` encodes
  `KEY_OK 0x3A 0xA0`, which is 0x5C/0x05 bit-reversed].

Key codes (command byte; the low bit is parity) [D: `drivers/hid/hid-appleir.c`]:

| Command byte | A1156 (white) button | A1294 (aluminium) button |
|---|---|---|
| 0x02/0x03 | Menu | Menu |
| 0x04/0x05 | centre ⏯ | (second frame of Centre and ⏯) |
| 0x06/0x07 | ⏭ (right) | Right |
| 0x08/0x09 | ⏮ (left) | Left |
| 0x0A/0x0B | Vol+ (up) | Up |
| 0x0C/0x0D | Vol− (down) | Down |
| 0x5C/0x5D | — | Centre (first frame) |
| 0x5E/0x5F | — | ⏯ (first frame) |

### 3.2 What Linux can do today

| Component | Status | Label |
|---|---|---|
| `hid-appleir` | Only for Apple's **own USB IR receivers** inside old Macs/Apple TV 1 (USB 05ac:8240/1440/8241/8242/8243). The Spark has none. Not usable. | [D] kernel source |
| `rc-core` + `ir-nec-decoder` | Decodes the Apple frames as **`nec32`** with any raw-IR USB receiver (mceusb, ir_toy, …). The comment in the source reads: *"NEC transport, but modified protocol, used by at least Apple and TiVo remotes"* | [D] `include/media/rc-core.h` `ir_nec_bytes_to_scancode()` |
| nec32 scancode layout | scancode = byte2<<24 \| byte1<<16 \| byte4<<8 \| byte3, so **Apple key = `0x87EE<ID><CMD>`** (e.g. ID 0x0E Up → `0x87ee0e0a`) | [D] docs.kernel.org rc-protos; [I] applied to Apple bytes |
| Edge case | If CMD == ~ID (CMD ^ ID == 0xFF), the kernel reclassifies that one key as `necx` with scancode `0xEE87<CMD>`. At most one key per remote ID can hit this | [D] same helper; [I] consequence |
| Shipped keymaps | **No Apple keymap** in the kernel (`drivers/media/rc/keymaps/`) or in v4l-utils `rc_keymaps/` (156 TOML files, master). `tivo.toml` is a working example of `variant = "nec32"` | [M] gh api + git.linuxtv.org listing, 2026-10-07 |
| LIRC | `lirc-remotes` has `apple/A1156.lircd.conf`, `A1294.lircd.conf`, `MC377Z.lircd.conf`, `macmini.lircd.conf` (2014–2015) | [M] sourceforge.net/p/lirc-remotes |
| FLIRC | Learns any IR remote and replays it as a USB HID keyboard (and consumer keys). Bluetooth remotes unsupported | [D] flirc.tv product page |

### 3.3 Path A (recommended): FLIRC USB → keyboard

Facts:
- **Price:** US$24.95 [M: flirc.tv/products/flirc-usb-receiver, 2026-10-07].
- **Linux arm64 build exists:** `http://apt.flirc.tv/arch/arm64/flirc.latest.arm64.tar.gz` → `Flirc-3.27.19-arm64/`
  containing `flirc_util`, `Flirc` (Qt5 GUI), `irtools` (all ELF aarch64, glibc ≥ 2.17) and `flirc_3.27.19_arm64.deb`.
  Last-Modified 2025-08-19; 20,185,904 bytes; sha256 `d88f5563e0b5b656f4c73ac749a66ad419406111971e7ce97196f556e147355e`
  [M]. The download is **plain HTTP**, so check the hash.
- `flirc_util` needs only `libusb-1.0-0` and `libhidapi-hidraw0` [M: ELF strings + bundled Readme]. The `.deb`
  also installs `/etc/udev/rules.d/99-flirc.rules` (MODE 0666 on 20a0:0001/0004/0006) and pulls in Qt5 for the GUI [M].
- Programming is a one-time step and can be done on any OS (the user's Mac works). The mapping is stored **on the
  dongle**, so the Spark needs no software [D: flirc.tv].
- Feature set from `flirc_util` help text [M: strings of the arm64 binary]:
  - `record <key>`: valid names are `a-z, return, enter, escape, backspace, delete, tab, space, F1–F12,
    printscreen, scroll, pause, insert, home, pageup, pagedown, end, right, left, down, up, wake, eject, vol_up,
    vol_down, mute, play/pause, stop, rewind, fast_forward, next_track, prev_track, suspend`.
  - `record_lp <key>`: **long press**. *"a short press of that button will send out the first function, and holding
    down the button for half a second will send out the second function."* Record the normal key first.
  - `record_macro <key>`: several keys per button. `record_api '<modifier> <HID usage>'`: any HID key with
    modifiers (e.g. Ctrl=1, Shift=2, Alt=4, GUI=8, ORed).
  - `interkey_delay 0-6`, `noise_canceler enable|disable`, `sleep_detect enable|disable`, `format`, `delete`,
    `saveconfig <file>`, `loadconfig <file>`, `wait`. Built-in ROM tables are `firetv`, `kodi` and `streacom`
    (**no Apple table**).
- Hold behaviour: [I] while IR repeat frames keep arriving, FLIRC keeps the key down, so X11 autorepeat scrolls tile
  rows. Verify with `xev`/`evtest` and tune `interkey_delay`.

Parts: FLIRC USB (USB-A) **plus a USB-C→USB-A adapter**, or a short USB-C-male→USB-A-female cable so the dongle can
see the remote. One buyer reported passive USB extenders dropping signals [R: eBay review via search].

Programming (on the Spark, CLI only, run from the extracted folder; sudo only because the udev rule is not installed):

```bash
sudo apt install libusb-1.0-0 libhidapi-hidraw0
mkdir -p ~/opt/flirc && cd ~/opt/flirc
curl -LO http://apt.flirc.tv/arch/arm64/flirc.latest.arm64.tar.gz
echo "d88f5563e0b5b656f4c73ac749a66ad419406111971e7ce97196f556e147355e  flirc.latest.arm64.tar.gz" | sha256sum -c   # value as of 2026-10-07
tar xzf flirc.latest.arm64.tar.gz && cd Flirc-3.27.19-arm64
sudo ./flirc_util version
sudo ./flirc_util format                 # start clean
# A1294 (aluminium). Each command waits for one button press aimed at the dongle.
sudo ./flirc_util record up              # ring up
sudo ./flirc_util record down            # ring down
sudo ./flirc_util record left            # ring left
sudo ./flirc_util record right           # ring right
sudo ./flirc_util record return          # centre (Select)
sudo ./flirc_util record escape          # Menu  -> Back
sudo ./flirc_util record_lp home         # hold Menu 0.5 s -> Home (see §9)
sudo ./flirc_util record play/pause      # Play/Pause
sudo ./flirc_util saveconfig sparkhome-a1294.fcfg   # commit this file to the repo
```

For the **A1156 (white)**: up=Vol+, down=Vol−, left=⏮, right=⏭, `return`=centre ⏯ plus
`record_lp play/pause` on the same button, `escape`=Menu plus `record_lp home`.

Verify: `sudo evtest` (or `xev -event keyboard`) and press each button. Expect `KEY_UP/DOWN/LEFT/RIGHT/ENTER/ESC/
HOME/PLAYPAUSE` from a device named like "flirc.tv flirc". Gate: every button gives exactly one expected key. On the
A1294, check that **Centre and Play/Pause stay distinct** (two-frame issue, §3.1) [U: no FLIRC+A1294 test found;
one RoPieee user could not map an "Apple Gen 2 remote", cause unknown, R: community.roonlabs.com].

Caveats: line-of-sight IR; CR2032 batteries; any nearby Mac or Apple TV will also react to the remote. If the remote's
ID changes (Menu+⏯ hold, §3.6), every code changes and the dongle must be re-learned [I from §3.1].

### 3.4 Path B: raw IR receiver + `ir-keytable` (no FLIRC)

Use a receiver with an **rc-core** driver: `mceusb` (Windows MCE "eHome" transceivers) or `ir_toy`. Both are in the
Spark kernel [M]. Check the USB ID against the `mceusb.c` table before buying. Clones vary, and some "IR receivers"
(Inteset IReTV, FLIRC) are plain HID keyboards [R: hifi-remote.com thread]. Marketplace prices were US$15–40 in
2025 snapshots [R, unverified].

```bash
sudo apt install ir-keytable
sudo ir-keytable                      # shows rcN, driver mceusb, enabled protocols
sudo ir-keytable -s rc0 -c -p nec -t  # press every button and note "scancode 0x87eeXXYY (nec32)"
```

Keymap template. **XX = your remote ID, and the low bit of each command must come from the `-t` output**:

```toml
# /etc/rc_keymaps/apple_a1294.toml
[[protocols]]
name = "apple_a1294"
protocol = "nec"
variant = "nec32"
[protocols.scancodes]
0x87eeXX0a = "KEY_UP"         # or ..0b (parity)
0x87eeXX0c = "KEY_DOWN"       # or ..0d
0x87eeXX09 = "KEY_LEFT"       # or ..08
0x87eeXX06 = "KEY_RIGHT"      # or ..07
0x87eeXX03 = "KEY_ESC"        # Menu, or ..02
0x87eeXX5c = "KEY_ENTER"      # Centre first frame, or ..5d
0x87eeXX5e = "KEY_PLAYPAUSE"  # Play/Pause first frame, or ..5f
# Leave 0x87eeXX04/05 unmapped: both Centre and Play/Pause send it as their second frame.
```

Worked example to sanity-check the byte order [I: derived]. The LibreELEC thread's A1156 capture (LIRC/AnalysIR
bit order) `0x77E15070, 0x77E13070, 0x77E19070, 0x77E16070, 0x77E1A070, 0x77E1C070` bit-reverses per byte to remote
ID 0x0E. In rc-core nec32 form that is Up `0x87ee0e0a`, Down `0x87ee0e0c`, Left `0x87ee0e09`, Right `0x87ee0e06`,
Centre ⏯ `0x87ee0e05`, Menu `0x87ee0e03`.

Load it at boot by adding `mceusb  *  /etc/rc_keymaps/apple_a1294.toml` to `/etc/rc_maps.cfg`. Test with
`sudo ir-keytable -s rc0 -c -w /etc/rc_keymaps/apple_a1294.toml && sudo ir-keytable -s rc0 -t`. The rc device shows
up as an ordinary keyboard to Xorg, so no `input` group is needed. Autorepeat delay and period can be set with
`ir-keytable -D <ms> -P <ms>`. For the A1156 use 0x02/03 Menu, 0x04/05 centre, 0x06/07 ⏭, 0x08/09 ⏮, 0x0a/0b Vol+,
0x0c/0d Vol−. A Ubuntu user did the same with an A1156 on an Intel NUC's built-in CIR [R:
einar.codeberg.page; LibreELEC forum thread 21048 recommends `ir-keytable -p all -t` to read real scancodes].

### 3.5 Path C: DIY IR bridge

An ESP32-S3 or RP2040 with a TSOP38238 receiver, decoding NEC and emitting USB HID (TinyUSB). No turnkey project was
found for this. The closest is the RP2040 "TTVKTR" clicker firmware [R: CNX Software]. It is only worth doing if FLIRC
is unavailable. Skip it.

### 3.6 "Pairing" and IDs for IR remotes

Pairing happens on the receiver side: a Mac or Apple TV stores the remote's ID byte and ignores others. On Linux nothing
pairs. FLIRC learns the full code, and ir-keytable needs the ID in the keymap.
- Mac pairing: hold **Menu + Right** (7-button) or **Menu + Next** (6-button) about 5 s [D: Apple Mac Help mchlp2927].
- Change the remote's ID: **Menu + Play/Pause** increments the UID [R: atv-bootloader wiki "AppleIR_LIRC";
  OSMC forum reports vary on hold time]. Useful when a nearby Mac or Apple TV reacts to the same remote.

---

## 4. Siri Remote 1st generation (A1513 / A1962)

### 4.1 Radio and protocol [R unless noted]

- **BLE HID-over-GATT (HOGP)** plus Apple quirks. It also has an IR blaster that Apple TVs use for TV volume/power. It
  does **not** send Apple-NEC navigation codes, so FLIRC cannot use it [I; FLIRC: "Bluetooth remotes are unsupported", D].
- GATT layout from one unit (handles can shift between firmware versions, so discover by UUID and Report Reference)
  [R: Yanndroid/SiriRemote-Linux README]:

```
0x0001 Generic Access 1800 | 0x0006 Generic Attribute 1801 | 0x000a Device Information 180a
0x0015 HID 1812: 2a4a HID Info (0x17), 2a4b Report Map (0x19), 2a4c Control Point (0x1b),
        2a4d Report (val 0x1d, +Report Reference, no CCCD)      <- "enable" report
        2a4d Report (val 0x20, +Report Reference)
        2a4d Report (val 0x23, CCCD 0x24, +Report Reference)    <- buttons/touch/audio arrive here
0x0026 Battery 180f: 2a19 level (0x28, CCCD 0x29), 2a1a power state (0x2b, CCCD 0x2c: 0xAB charging, 0xAF discharging, 0xBB plugged)
0x002d Bond Management 181e
0x0032 Apple custom 8341f2b4-c013-4f04-8197-c4cdb42e26dc: 9fbf120d-6301-42d9-8c58-25e699a21dbd,
        2bdcaebe-8746-45df-a841-96b840980fb7, 2bdcaebe-...-fb8, 30e69638-3752-4feb-a3aa-3226bcd05ace (notify)   [purpose U]
```

- **Enable:** write `0xAF` to the Report at value handle 0x1d, then enable notifications (CCCD 0x24). The remote then
  sends notifications of 2, 13, 20 or 101 bytes on 0x23. One project notes the enable report is an **Output** report on
  gen 1/2 [R: azais-corentin/siri-remote `src/bluez/hid.rs` comment].
- **Buttons:** byte 1 bitmask: `0x01` TV/Home (Yanndroid labels it "AirPlay"), `0x02` Vol+, `0x04` Vol−, `0x08` ⏯,
  `0x10` Siri, `0x20` Menu, `0x80` touch-surface click.
- **Touch (13 bytes for one finger, 20 for two):** byte0 finger count, byte1 buttons, byte2 always 0x32 (50),
  `x = d[6] + 255*(d[7] & 7)` (8 "zones"), `y = d[8]` (signed-looking: 188..255 then 0..38, low resolution),
  `pressure = d[11]`, second finger in d[13..19].
- **Product ID:** one "black glass" unit reported PID **0x026D**, hardware revision 0A00, vendor 0x004C. VibeRemote warns
  not to generalise it [R: VibeRemote `RemoteGeneration.swift`].
- **Identity MAC prefixes seen:** `48:A9:1C:`, `60:BE:C4:` [R: Yanndroid].
- **Gyro/accelerometer:** **no public decoder** [U]. The hardware exists [D: Wikipedia]. Expect another vendor report
  or an enable step.

### 4.2 Microphone (gen 1)

- **Codec:** Opus, **16 kHz mono** decode [R: Jack-R1/SiriRemoteVoiceDecoder `main.m`, "identified by looking at the
  call the Apple TV Remote iOS app uses"]. VibeRemote states **CELT-only wideband, 20 ms, TOC byte 0xB8**. 0xB8 decodes
  to Opus config 23 = CELT-only WB 20 ms, mono, one frame [R + I: RFC 6716 TOC layout].
- **Framing:** 101-byte notifications on the same report as buttons. Payload is a length byte, then an Opus packet
  starting with 0xB8 [R: Jack-R1 parser: "voice data begins at B8 … byte before B8 is the length"]. Jack-R1 detects
  start and end from 2-byte ATT notifications on handle 0x0023: value `00 10` means voice start (Siri bit 0x10 set),
  and `10 00` means voice end. Audio flows **only while Siri is held**, and the firmware gates it [R: VibeRemote,
  SiriRemoteForge].
- Newer gen-1 firmware also exposes a dedicated Consumer/0x04 ("microphone") audio collection, report ID 0xFA
  [R: VibeRemote].
- **Linux status: not working publicly.** Yanndroid could not receive the 101-byte notifications with bluepy ("BlueZ
  can't handle more than 20 bytes"). That symptom matches a default ATT MTU of 23, which BlueZ's own GATT client
  avoids by negotiating MTU 517 [I]. An experimental `dev` branch uses `HCI_CHANNEL_USER` (exclusive adapter access;
  bluetoothd must be stopped) [R/M: repo source].

### 4.3 Linux projects for gen 1

| Project | What it does | Activity / licence | Notes |
|---|---|---|---|
| [Yanndroid/SiriRemote-Linux](https://github.com/Yanndroid/SiriRemote-Linux) | bluepy GATT client: enable, buttons, touch → uinput (volume, media, mouse) | created 2023-05-17, last push 2024-09-10; 67★; **no licence file** (all rights reserved by default) | root needed; bluepy unmaintained (issue #4 asks for bleak); `gen-3` branch (2023-11) covers gen 2/3 |
| [Jack-R1/SiriRemoteVoiceDecoder](https://github.com/Jack-R1/SiriRemoteVoiceDecoder), [SiriRemoteVoiceControl](https://github.com/Jack-R1/SiriRemoteVoiceControl) | macOS: PacketLogger capture → Opus 16 kHz → WAV / voice control | 2020; MIT | the original reverse engineering |
| [Jack-R1/SiriRemoteDriver](https://github.com/Jack-R1/SiriRemoteDriver) | Windows KMDF filter driver | 2022–2023; MIT | — |
| [mmmmmmarcus/VibeRemote](https://github.com/mmmmmmarcus/VibeRemote) | macOS: buttons, touch and mic for gen 1 and gen 2/3 | 2026-07 → 2026-10; MIT | best current description of gen-1 audio framing |
| [wiz78/SiriRemote-HomeAssistant](https://github.com/wiz78/SiriRemote-HomeAssistant) | gen-1 events → Home Assistant | 2023; no licence | — |

### 4.4 Recommended path (gen 1)

1. Pair with bluetoothctl (§6.2), then run Experiment E1 (§6.4) with payload `af` sent to **Output** reports. If
   0x23-style reports flow through hidraw, write a small daemon: hidraw → semantic actions plus uinput (§9).
2. If E1 fails: Yanndroid-style raw-ATT client. Rewrite it rather than vendoring it (no licence), using the
   bitmasks/offsets above as facts. Check coexistence with bluetoothd's auto-reconnect [U].
3. Voice on gen 1 is **research-grade**. Plan voice on a USB mic or a gen-3 remote instead.

---

## 5. Siri Remote 2nd/3rd gen (A2540 / A2854)

### 5.1 Protocol [R unless noted]

- BLE HID with **eight Report characteristics (UUID 0x2A4D)** told apart only by their Report Reference descriptors
  [R: azais README].
- **Report IDs:** **0xFA** microphone (99-byte payload), **0xFB** buttons (2-byte little-endian mask), **0xFC** touch
  (11 bytes for one finger, 18 for two; 7-byte trailer per contact with x/y, pressure, ellipse major/minor and an
  orientation index) [R: azais-corentin/siri-remote; SiriRemoteForge `docs/mic-reverse-engineering.md`].
- **0xFB bitmask** (agreed by three projects): `0x0001 TV/Control Center`, `0x0002 Vol+`, `0x0004 Vol−`,
  `0x0008 Select (centre click)`, `0x0010 Power`, `0x0020 Siri`, `0x0040 Back`, `0x0080 Mute`, `0x0100 ⏯`,
  `0x0200 Up`, `0x0400 Right`, `0x0800 Down`, `0x1000 Left` (ring clicks) [R: SiriRemoteESP `report_decoder.h`,
  azais `decoder.rs`, Yanndroid `gen-3`].
- **Enable write, two variants that each work for their authors:**
  (a) `0xAF` to **every writable non-Input report**; gen 3 has only Feature reports [R: azais];
  (b) `0xF0 0x00` to the **first Report characteristic without notify** [R: SiriRemoteESP, Yanndroid `gen-3`].
- **Mic packet (99 bytes):** 2 prefix bytes, little-endian sequence number, **length byte at offset 4**, Opus packet
  from offset 5 (TOC 0xB8 = CELT-only WB 20 ms), zero padding. A zero-length packet marks the end. Decode at 16 kHz
  (320 samples per frame; SiriRemoteESP) or 48 kHz (960; azais). Both are valid for an Opus decoder [R + I]. Audio only
  while Siri is held [R].
- **Product IDs:** gen 3 = **0x0315 (789)** [R: SiriRemoteForge, macOS IORegistry]. Apple's macOS driver special-cases
  788/789, so gen 2 is probably **0x0314** [I]. Vendor 0x004C.
- **Advertising:** Apple manufacturer data starting `07 0D 02 15 03 02 <6-byte identity address> 4F 50 50` plus HID
  UUID 0x1812. The **RPA rotates about every 20 s**, so pair promptly after entering pairing mode [R: azais].
- **Security:** Just Works (NoInputNoOutput), bonding, LE Secure Connections, key distribution ENC+ID. Apple re-encrypts
  the link a second time 750–1200 ms after reconnect. Touching CCCDs before that drops the buffered wake press
  [R: SiriRemoteESP `siri_ble.c`].
- Gen 2 vs gen 3: same layout per VibeRemote and Yanndroid `gen-3`. azais lists gen 2 as out of scope (untested)
  [R]. Treat gen 2 as probably identical [I].

### 5.2 Linux implementation: azais-corentin/siri-remote

- Rust CLI, GPL-3.0, created 2026-05-12, last push 2026-08-30, 7★. Subcommands `pair`, `events`, `view` (TUI),
  `dump`, `mic` (**publishes the mic as a PipeWire `Audio/Source`**, 48 kHz mono S16LE, with Opus PLC on gaps),
  `gamepad` (virtual pad via `/dev/uinput`, configurable mapping; defaults TV→BTN_MODE, Select→A, Back→B, ring→hat,
  ⏯→Start), `unpair` [R/M: repo source].
- Uses its own BlueZ `Agent1` (NoInputNoOutput). The README says *not* to pair with bluetoothctl. RSSI gate −55 dBm
  (hold the remote within about 50 cm) [R].
- **Requires the BlueZ `hog` plugin to be off** on 5.72-era BlueZ. Otherwise writes fail with
  `org.bluez.Error.NotAuthorized`. Its AGENTS.md says `bluetoothd --noplugin=input,hog`. The README's
  `DisablePlugins=hogp` main.conf key does **not exist in upstream BlueZ** [M: grep of 5.72 and master `src/main.c`];
  it looks like a NixOS module option. On Ubuntu use a systemd drop-in (§6.3, option O2).
- Build on the Spark: Rust edition 2024 (rustup, not Ubuntu's rustc), `libopus-dev libdbus-1-dev libpipewire-0.3-dev
  libclang-dev pkg-config` [I from Cargo.toml/flake].

### 5.3 ESP32 bridge: Jakkumn/SiriRemoteESP

- ESP-IDF + ESPHome, MIT, created 2026-05-01, last push 2026-10-04. Target: Seeed **XIAO ESP32-S3** (US$7.49, BLE 5.0;
  8 MB flash / 8 MB PSRAM per README) [M: seeedstudio.com; R].
- Gen 3 tested. NimBLE central, structural handle discovery cached in NVS, bonded reconnect, **Opus decode on the ESP**
  (`siri_audio`, 16 kHz). Outputs ESPHome events/voice_assistant or MQTT (`siri_remote/event`, …). Wake presses can be
  lost after about 30 s disconnected, so it stays connected by default (more battery drain). Swipes are not reported [R].
- SparkHome integration as-is: run mosquitto on the Spark and subscribe [I]. **Better long-term variant [I]:** fork it
  so the ESP32-S3 enumerates over native USB as **HID keyboard + consumer control (+ UAC microphone)**. Linux would then
  see a standard USB keyboard and mic, independent of BlueZ, `hog` and the MT7925. Effort: a few days of firmware work.

### 5.4 Recommended path (gen 2/3)

E1 (hidraw, keeps `hog`) → if it fails: azais with **O3** (BlueZ ≥ 5.80 `ExportClaimedServices=read-write`) or **O2**
(disable `hog` and connect the Xbox pad over USB or the Xbox Wireless Adapter) → or the ESP32 USB-dongle variant.

---

## 6. BlueZ 5.72 / Ubuntu 24.04 / MT7925 procedures

### 6.1 Baseline configuration (keep the user's settings)

`/etc/bluetooth/main.conf` (already applied: `Privacy = device`, `JustWorksRepairing = always`). The second matters
here: a Siri Remote that was reset or re-paired to an Apple TV loses its bond and must be able to re-pair silently.
Leave `[GATT] ExchangeMTU` at its default (517), so 99/101-byte notifications fit [D: BlueZ main.conf]. `input.conf`
options (`UserspaceHID`, `ClassicBondedOnly`, `LEAutoSecurity`, `IdleTimeout`) do not affect LE HOG remotes [D: 5.72
`profiles/input/input.conf`].

### 6.2 Pairing a Siri Remote with bluetoothctl (for E1 / HOG paths)

Button combos (enter pairing / advertise):

| Remote | Pair / advertise | Reset or restart | Source |
|---|---|---|---|
| Siri gen 1 (A1513/A1962) | **Menu + Vol+**, hold about 2–5 s, close to the Spark | not documented for non-Apple hosts [U]. Re-enter pairing mode instead | [D] Apple HT201254 ("Back (or Menu) + Volume Up for five seconds"); [R] Yanndroid, Jack-R1 |
| Siri gen 2/3 (A2540/A2854) | **Back (‹) + Vol+**, about 5 s | **TV/Control Center + Vol−**, 5 s (Apple: "Resetting a remote unpairs it") | [D] Apple tvOS guide atvbc9953e63, HT201254; [R] SiriRemoteESP |

```text
$ bluetoothctl
[bluetooth]# power on
[bluetooth]# agent NoInputNoOutput      # forces Just Works, like the remote's own IO capability
[bluetooth]# default-agent
[bluetooth]# menu scan
[bluetooth]# transport le
[bluetooth]# back
[bluetooth]# scan on
   (hold the combo; the device may show as "Siri Remote"/"Apple TV Remote" or its serial number)
[bluetooth]# pair <ADDR>               # within ~20 s: the RPA rotates
[bluetooth]# trust <ADDR>
[bluetooth]# scan off
[bluetooth]# info <ADDR>               # Paired: yes, Bonded: yes; UUIDs incl. 00001812 (HID)
```

Checks: `cat /proc/bus/input/devices` (a new uhid device, `Bus=0005 Vendor=004c`), `ls /sys/class/hidraw/*/device/uevent`,
`sudo evtest`. Out of the box, expect little or nothing until the enable write: "the remote stays silent until
userspace writes 0xAF" [R: azais]; "even then it doesn't work on its own" [R: Yanndroid]. Volume keys may work as
standard consumer reports, as they do on macOS without helpers [I from Jack-R1 step 4]. BlueZ's battery plugin should
expose `org.bluez.Battery1.Percentage` for the remote, which the UI can show [I: BAS 0x180F is standard].

### 6.3 The HOG ownership problem

[D] BlueZ 5.72 `src/device.c`: *"Claim attributes of internal profiles … gatt_db_service_set_claimed(attr, true)"*.
`src/gatt-client.c` then skips exporting claimed services over D-Bus (`if (gatt_db_service_get_claimed(attr)) return;`).
So D-Bus GATT clients (bleak, btleplug, azais' zbus code) cannot write the HID Report characteristics while `hog` runs.

| Option | How | Keeps the Xbox BLE pad? | Risk |
|---|---|---|---|
| **O1 – hidraw (E1)** | Leave `hog` alone. Send the enable report through `/dev/hidrawN` (HIDIOCSFEATURE / write()); read raw reports | **yes** | Untested. Depends on the remote accepting the write via HOG's mapping (§6.4) |
| **O2 – disable hog** | `sudo systemctl edit bluetooth` → `[Service]` `ExecStart=` `ExecStart=/usr/libexec/bluetooth/bluetoothd -P hog`. The plugin is named `hog` [D: `BLUETOOTH_PLUGIN_DEFINE(hog,…)`]. azais' AGENTS.md uses `--noplugin=input,hog`, but `input` covers only classic-BT HID, so `-P hog` alone should release the LE HID service [I] | **no**: every BLE HID device stops producing input (`-P input,hog` also kills classic-BT HID) | Breaks the BLE Xbox controller. OK only if the pad goes USB or Xbox Wireless Adapter (xone) |
| **O3 – BlueZ ≥ 5.80** | `[GATT] ExportClaimedServices = read-write` in main.conf (5.80+; default `read-only` returns `NotAuthorized` on writes) [D: commit dbd6591bd1d0; 5.79 lacks it, 5.80–5.87 have it, M] | yes | Means replacing Ubuntu's bluez 5.72 (source build or backport); DGX OS updates may fight it; both HOG and the app see events, so filter duplicates |
| **O4 – raw ATT socket** | bluepy-style L2CAP ATT client next to bluetoothd (Yanndroid) | probably | Coexistence with bluetoothd auto-reconnect [U]; root/CAP_NET_RAW |
| **O5 – external bridge** | ESP32-S3 owns the BLE link (§5.3) | yes | firmware work; one more device |

### 6.4 Experiment E1: hidraw enable with `hog` kept (the key unknown)

Why it should work [D]:
- BlueZ 5.72 `profiles/input/hog-lib.c` enables the CCCD of **every Input report** (`ccc_read_cb` → `write_ccc`).
  It forwards `UHID_SET_REPORT` (hidraw `HIDIOCSFEATURE`) and `UHID_OUTPUT` (hidraw `write()`) to the GATT Report
  characteristic matched by **(type, report ID)** from the Report Reference descriptors (`find_report_by_rtype`,
  `set_report`, `forward_report`), and strips the report-ID byte for numbered reports.
- Kernel `hid-core.c` `hid_report_raw_event()` passes a report to hidraw **only if its report ID is declared in the
  report descriptor** (`hid_get_report()`; otherwise it returns before `hidraw_report_event`). macOS exposes 0xFA/0xFB
  (gen 3) as HID interfaces, which implies the descriptor declares them [R/I].
- The kernel passes the full raw notification to hidraw (`hidraw_report_event(hid, data, size)`), so 99/101-byte
  audio reports fit (uhid max 4096) [D].

Procedure (needs the remote; the probe script is in Appendix A and saved as `research/siri_hidraw_probe.py`):

```bash
python3 -I siri_hidraw_probe.py --selftest                       # parser check, runs anywhere
sudo python3 -I siri_hidraw_probe.py --list                      # /dev/hidrawN, product id, report IDs by type
sudo python3 -I siri_hidraw_probe.py /dev/hidrawN --seconds 20   # baseline: press buttons, no enable
sudo python3 -I siri_hidraw_probe.py /dev/hidrawN --enable af   --seconds 30   # azais variant
sudo python3 -I siri_hidraw_probe.py /dev/hidrawN --enable f000 --seconds 30   # SiriRemoteESP/Yanndroid gen-3 variant
```

Evidence gates:
- **G1** (pass/fail): after an enable, 0xFB (gen 2/3) or 2/13/20-byte (gen 1) reports appear for every button.
  The script prints decoded gen-3 button names.
- **G2**: touching the surface or ring yields 0xFC (gen 2/3) or 13/20-byte 0x32-marked (gen 1) reports at about 60 Hz.
- **G3**: holding Siri yields about 50 reports/s of 0xFA (gen 2/3) or 101-byte (gen 1), each with 0xB8 at the Opus
  offset. Decode with libopus (`opusdec`/`opuslib`) at 16 kHz, then run whisper.cpp on the WAV.
- **G4**: after 5 min idle, the first button press reconnects and is delivered (measure the latency).
- If G1 fails: run `sudo btmon` during the enable. If an ATT Write Request to the right handle appears with no
  notifications after it, the payload or report is wrong. If no write appears, HOG did not map the (type, ID).
  Then fall back to O3, O5 or O2.

### 6.5 Permissions (no `input` group needed)

```udev
# /etc/udev/rules.d/71-sparkhome-remotes.rules
# Siri Remotes via BlueZ uhid (bus 0005, Apple 004C): let the logged-in seat user open hidraw
SUBSYSTEM=="hidraw", KERNELS=="0005:004C:*", TAG+="uaccess"
# If a SparkHome daemon must synthesize keys/gamepad: prefer a dedicated group over uaccess
KERNEL=="uinput", SUBSYSTEM=="misc", GROUP="sparkhome", MODE="0660", OPTIONS+="static_node=uinput"
```

`uinput` access lets a process type into any app, so give it to one small system service, not the browser. FLIRC and
rc-core devices need no rule: Xorg already reads keyboards.

### 6.6 MT7925 resilience

The radio sometimes wedges [M, task brief]. For a primary remote, add a watchdog. When
`bluetoothctl show | grep -q 'Powered: yes'` fails, or the remote's last-seen time is stale while the adapter claims
it is up, run `btmgmt power off; sleep 1; btmgmt power on` and log it. Keep a **non-Bluetooth input path**
(FLIRC/IR or CEC) as a fallback for when BLE is down [I]. Apple-remote behaviour on MT7925 is [U].

---

## 7. "Jailbreak": what it would mean, and why not to

- **IR remotes (A1156/A1294):** nothing to jailbreak. The only state is the ID byte, and it is user-changeable (§3.6).
  Any NEC receiver reads them [D].
- **Siri Remotes:** firmware updates are delivered by an Apple TV (tvOS) over BLE. Paired only to a Linux host, the
  remote's firmware stays as it is [I: common knowledge, not verified here]. No
  public custom firmware, exploit or SWD dump was found [U: searched GitHub/forums 2026-10]. The MCU is an
  STM32L151-class Cortex-M3 [D: Wikipedia]; its readout-protection state is [U]. Reflashing a certified radio device
  risks bricking it and may void its FCC authorization [D: 47 CFR §15.21 requires the warning that "changes or
  modifications not expressly approved by the party responsible for compliance could void the user's authority to
  operate the equipment"]. **It is unnecessary**: every input (buttons, touch, mic) is readable over standard BLE
  after a one-byte enable write (§4–5).
- "Use the remote to run the SparkHome OS": the remote cannot run anything. SparkHome runs on the Spark, and the remote
  only sends input events.
- **Legal (brief, not legal advice):** using a remote you own with your own computer by receiving its standard IR/BLE
  signals involves no circumvention. In the US, 17 U.S.C. §1201(f) also permits circumvention "for the sole purpose of
  identifying and analyzing those elements of the program … necessary to achieve interoperability" [D: LII]. Firmware
  dumping or reflashing is where the grey areas (DMCA, FCC) begin, and it isn't needed here.

---

## 8. If "Gen 1" means the 2007 Apple TV box (A1218)

- **Hardware:** an underclocked Pentium M (Dothan, 32-bit only), 256 MB RAM, GeForce Go 7300 (64 MB). An optional
  Broadcom Crystal HD mini-PCIe decoder (replaces Wi-Fi) enables 1080p; its driver is out-of-tree [R:
  eloydegen.com/blog/posts/apple-tv1 (2021)].
- **Historical "jailbreaks":** Firecore **aTV Flash** (commercial plugins/XBMC/Boxee; [D] firecore.com/atvflash),
  **atv-bootloader** (boots Linux via the Apple EFI; GPL-2.0, fork github.com/lemonjesus/atv-bootloader 2022),
  **atvusb-creator "Patchstick"**, **Crystalbuntu** (Ubuntu + XBMC + Crystal HD), and **OSMC**, which dropped ATV1
  support on **2017-09-24** because "new versions of Kodi now require more resources" [D: my.osmc.tv].
  [Evinyatar/atvclient](https://github.com/Evinyatar/atvclient) (GPL-2.0, 2009–2016) forwarded its built-in IR
  receiver's events to XBMC.
- **As an IR bridge for the Spark:** possible in principle. Its internal Apple USB IR receiver works with
  `hid-appleir` [D], and an evdev→network forwarder would complete it. It is a bad trade: a 2007, 32-bit, 256 MB box
  with an ageing hard disk, versus a US$25 FLIRC [I]. **Recommendation: do not use it.** Keep its white A1156 remote and
  use FLIRC (§3.3).

---

## 9. Expected UX: one semantic action map for every remote

SparkHome should consume **semantic actions** (Up, Down, Left, Right, Select, Back, Home, PlayPause, Voice
push-to-talk, VolUp/VolDown), not device codes. In the browser they arrive as `keydown` events or the Gamepad API.
From BLE helper daemons they arrive over a local WebSocket.

| Action | A1294 + FLIRC | A1156 + FLIRC | IR + ir-keytable | Siri gen 1 (daemon) | Siri gen 2/3 (daemon) | Xbox pad (already works) |
|---|---|---|---|---|---|---|
| Up/Down/Left/Right | ring → `KEY_UP/DOWN/LEFT/RIGHT` | Vol+/Vol−/⏮/⏭ → arrows | same via keymap | swipe gesture on touch → arrows (threshold + repeat) | ring clicks (bits 0x0200/0800/1000/0400) → arrows; swipe optional | D-pad / left stick |
| Select | centre → `KEY_ENTER` | centre ⏯ → `KEY_ENTER` | `KEY_ENTER` | touch click (0x80) → Enter | centre (0x0008) → Enter | A |
| Back | Menu → `KEY_ESC` | Menu → `KEY_ESC` | `KEY_ESC` | Menu (0x20) → Esc | Back (0x0040) → Esc | B |
| Home | hold Menu 0.5 s (`record_lp home`) → `KEY_HOME` | same | long-press logic in app (rc-core repeats) | TV (0x01) → Home | TV (0x0001) → Home | Guide (BTN_MODE) |
| PlayPause | ⏯ → `KEY_PLAYPAUSE` | hold centre (`record_lp play/pause`) | `KEY_PLAYPAUSE` | ⏯ (0x08) | ⏯ (0x0100) | Start/Menu |
| Voice PTT | — (no mic) | — | — | Siri (0x10) while held; audio research-grade | Siri (0x0020) while held + 0xFA Opus stream → whisper.cpp | (map a button + USB mic) |
| Volume | TV remote / CEC | Vol± are navigation here | — | Vol± → `KEY_VOLUMEUP/DOWN` | Vol± / Mute | — |
| Power | — | — | — | — | Power (0x0010) → TV off/standby via CEC or display sleep | — |

GNOME/Xorg notes [I]: arrows, Enter and Escape reach a focused Chrome kiosk unchanged. `KEY_PLAYPAUSE` and volume keys
are taken by gnome-settings-daemon. Volume then changes system volume (fine); play/pause goes to MPRIS, so pages should
implement the Media Session API. Pick the Home key deliberately. In-app `KEY_HOME` is fine. For a **global** Home
(while Steam BP or a game has focus), add a GNOME custom shortcut that runs `sparkhome-cli home`, bound to a combo
FLIRC can send with `record_api` (e.g. Ctrl+Alt+Home = `record_api 5 74`; HID usage 0x4A = Home). `record_lp` takes
only the named keys in §3.3, so a modifier combo can only be a button's short-press function [M: help text]. Avoid
F13: xkb maps it to XF86Tools, which GNOME binds to Settings (verify with `xev`).

---

## 10. Parts list

| Item | Price | Status | For |
|---|---|---|---|
| FLIRC USB (gen 2) | US$24.95 | [M] flirc.tv 2026-10-07 | IR Apple remotes (recommended) |
| USB-C (male) → USB-A (female) adapter or short cable | a few US$ | [U] price not checked | FLIRC/dongles on the Spark's USB-C-only rear |
| Aluminium Apple Remote A1294 (if the user only has a white A1156 and wants separate Select and ⏯) | used market | [U] | nicer 7-button layout |
| mceusb-class USB IR transceiver (alternative to FLIRC) | ≈US$15–40 | [R] 2025 marketplace snapshots | Path B |
| Seeed XIAO ESP32-S3 | US$7.49 | [M] seeedstudio.com | Siri gen 3 bridge (Path 5) |
| Pulse-Eight USB-CEC adapter (P8-USBCECv1) | ≈US$44–50 | [R] cached store pages, undated | fallback: Sony TV remote over HDMI-CEC |
| Siri Remote (USB-C, A2854) if buying new | US$59 at 2021 launch for gen 2; current price not captured | [D] Wikipedia | Apple-TV-like feel + voice |

---

## 11. Fallback ranking (whole input stack)

1. **FLIRC + IR Apple Remote**: robust, zero-software keyboard, independent of Bluetooth. Baseline remote. [D/M]
2. **Xbox Series controller (BLE, already paired)**: primary for games and fine for tile navigation (Gamepad API,
   Steam BP). Keep `hog` enabled for it. [M]
3. **HDMI-CEC with the Sony TV remote** via a Pulse-Eight USB-CEC adapter. The kernel driver `pulse8-cec` +
   `inputattach --pulse8-cec /dev/ttyACM0` (udev/systemd units per kernel doc) + `cec-ctl --playback`. Remote keys
   arrive as input events (`CONFIG_MEDIA_CEC_RC=y`), and the Spark can also wake or standby the TV
   [D: kernel `Documentation/admin-guide/media/cec.rst`]. Whether the Spark's HDMI exposes CEC natively is [U]; assume
   not. Whether this Sony forwards keys to a playback device is [U] (Bravia Sync usually does, I).
4. **Siri Remote gen 2/3** via E1 → azais (O3/O2) → ESP32 dongle. Best Apple-TV feel plus a microphone, but the most
   engineering. [R/I]
5. **Siri Remote gen 1**: buttons and touch are feasible (E1 or a raw-ATT rewrite); voice is not proven on Linux. [R]
6. **Bluetooth "keyboard-mode" mini controllers** (e.g. 8BitDo Micro, K switch position: D-pad sends arrows, buttons
   remappable up to F24 via the 8BitDo mobile app). Linux not explicitly tested [R/I].
7. **Phone as a remote**: SparkHome serves a small web remote over the LAN (WebSocket → semantic actions), zero
   hardware [I].

Voice-specific ranking: a **USB microphone or USB speakerphone** near the couch (UAC, works with `snd-usb-audio` and
PipeWire, most robust) > **Siri gen-3 mic** (azais PipeWire source or E1) > 2.4 GHz "voice air-mouse" dongles
(G20S Pro class). Their mic is USB audio, but the only Linux report is a hard lock on an Amlogic CoreELEC box
[R: discourse.coreelec.org thread 58460].

---

## 12. Open questions

1. Which remote does the user actually own (A1156 vs a Siri Remote generation)? This decides FLIRC vs E1.
2. E1 result on real hardware: do Apple's enable writes pass through BlueZ 5.72 HOG via hidraw, and do 0xFA/0xFB/0xFC
   reach `/dev/hidraw`?
3. Siri Remote reconnect latency and dropped first presses with MT7925 + BlueZ auto-connect.
4. Gen-1 gyro/accelerometer report format and enable (no public decoder).
5. Gen-1 mic on Linux (101-byte notifications via HOG/hidraw, untested).
6. FLIRC with the A1294's two-frame Centre/⏯: do they stay distinct?
7. Does the Sony TV forward remote keys over CEC to a Pulse-Eight "playback device"?
8. If BlueZ ≥ 5.80 is ever adopted (O3): how to ship it on DGX OS without breaking vendor updates.

---

## 13. Sources (accessed 2026-10-07/08)

Kernel / BlueZ / Linux (primary):
- https://raw.githubusercontent.com/torvalds/linux/master/drivers/hid/hid-appleir.c (key table, two-frame codes, device IDs)
- https://raw.githubusercontent.com/torvalds/linux/master/drivers/media/rc/ir-nec-decoder.c and include/media/rc-core.h (`ir_nec_bytes_to_scancode`)
- https://docs.kernel.org/userspace-api/media/rc/rc-protos.html (nec-32 scancode layout)
- https://raw.githubusercontent.com/torvalds/linux/master/drivers/hid/hid-ids.h, hid-apple.c (no Siri Remote IDs), hid-input.c (consumer/GD usage → keycodes), hid-core.c (`hid_report_raw_event`)
- https://raw.githubusercontent.com/torvalds/linux/master/Documentation/admin-guide/media/cec.rst (Pulse-Eight + inputattach)
- https://git.linuxtv.org/v4l-utils.git/tree/utils/keytable/rc_keymaps (no Apple keymap); tivo.toml; rc_maps.cfg
- https://packages.ubuntu.com/noble-updates/arm64/linux-modules-6.17.0-1029-nvidia/filelist ; http://ports.ubuntu.com/ubuntu-ports/pool/main/l/linux-nvidia-6.17/ (buildinfo config)
- https://packages.ubuntu.com/noble/arm64/ir-keytable/filelist
- https://github.com/bluez/bluez at tag 5.72: src/device.c, src/gatt-client.c, profiles/input/{hog.c,hog-lib.c,input.conf,manager.c}, src/main.conf; master main.conf (`ExportClaimedServices`), commit dbd6591bd1d0
- https://www.kernel.org/pub/linux/bluetooth/ (latest bluez 5.87)
- https://gitlab.freedesktop.org/libevdev/udev-hid-bpf (no Apple/Siri fixups in src/bpf/{stable,testing})

Apple / vendors:
- https://support.apple.com/en-us/HT201254 (pair: Back/Menu + Vol+ 5 s; restart: TV/Control Center + Vol−)
- https://support.apple.com/guide/tv/atvbc9953e63/tvos (reset: Vol− + TV ≥5 s; pair: Vol+ + Back/Menu 2 s)
- https://support.apple.com/en-us/103233 (identify your Apple TV remote); Apple Mac Help mchlp2927 (IR remote pairing)
- https://flirc.tv/products/flirc-usb-receiver ; http://apt.flirc.tv/arch/arm64/flirc.latest.arm64.tar.gz
- https://docs.nvidia.com/dgx/dgx-spark/hardware.html
- https://www.seeedstudio.com/XIAO-ESP32S3-p-5627.html
- https://support.pulse-eight.com/support/solutions/articles/30000053023 (libcec on Linux)
- https://www.law.cornell.edu/uscode/text/17/1201 ; https://www.law.cornell.edu/cfr/text/47/15.21

Projects / reverse engineering:
- https://github.com/Yanndroid/SiriRemote-Linux (master, gen-3, dev branches)
- https://github.com/azais-corentin/siri-remote
- https://github.com/Jakkumn/SiriRemoteESP
- https://github.com/Jack-R1/SiriRemoteVoiceDecoder , /SiriRemoteVoiceControl , /SiriRemoteDriver
- https://github.com/mmmmmmarcus/VibeRemote (fork: mengdream/VibeRemote)
- https://github.com/HOLODATA-COM/SiriRemoteForge (docs/mic-reverse-engineering.md); fork tevriqorg/SiriRemoteForge
- https://github.com/deanxizian/SiriRemote , https://github.com/lcq110/codex-siri-remote (macOS, gen 3)
- https://github.com/wiz78/SiriRemote-HomeAssistant
- https://sourceforge.net/p/lirc-remotes/code/ci/master/tree/remotes/apple/ (A1156, A1294, MC377Z, macmini)
- https://github.com/lemonjesus/atv-bootloader/wiki/AppleIR_LIRC ; https://www.hadess.net/2012/05/notes-on-apple-ir-remotes-reverse.html
- https://forum.libreelec.tv/thread/21048-using-apple-remote-apple-a1156/ ; https://einar.codeberg.page/how-to-use-an-apple-remote-with-kodi-on-ubuntu-on-an-intel-nuc.html
- https://community.roonlabs.com/t/flirc-and-apple-gen-2-remote-help/181759
- https://my.osmc.tv/saying-goodbye-to-apple-tv/ ; https://eloydegen.com/blog/posts/apple-tv1/ ; https://github.com/Evinyatar/atvclient
- https://en.wikipedia.org/wiki/Apple_Remote ; https://en.wikipedia.org/wiki/Siri_Remote
- https://discourse.coreelec.org/t/ugoos-sk1-s928x-hard-lockup-when-using-usb-microphone-g20s-pro/58460

---

## Appendix A: `siri_hidraw_probe.py` (Experiment E1)

Saved next to this report. The parser self-test passes (`python3 -I siri_hidraw_probe.py --selftest` → `selftest ok`,
run 2026-10-08 on macOS). The hidraw and ioctl parts are **untested on hardware**.

```python
#!/usr/bin/env python3
"""Probe an Apple Siri Remote that BlueZ's HOG plugin already owns, via /dev/hidraw.

UNTESTED ON HARDWARE. It checks the hypothesis in apple-remotes.md section 6.4:
"with HOG left enabled, writing Apple's enable byte as a Feature/Output report through
hidraw unlocks the button (0xFB), touch (0xFC) and mic (0xFA) reports".

  python3 -I siri_hidraw_probe.py --selftest
  sudo python3 -I siri_hidraw_probe.py --list
  sudo python3 -I siri_hidraw_probe.py /dev/hidrawN --enable af --seconds 30
  sudo python3 -I siri_hidraw_probe.py /dev/hidrawN --enable f000 --seconds 30
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
    ap = argparse.ArgumentParser()
    ap.add_argument("dev", nargs="?")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--enable", help="hex payload sent to every Feature and Output report, e.g. af or f000")
    ap.add_argument("--seconds", type=float, default=20)
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.list or not a.dev:
        for path, product, name, desc in apple_hidraw():
            print(f"{path} product=0x{product:04x} name={name!r} ids={report_ids(desc)}")
        return 0
    desc = next((d for p, _, _, d in apple_hidraw() if p == a.dev), None)
    if desc is None:
        sys.exit(f"{a.dev} is not an Apple Bluetooth hidraw node")
    ids = report_ids(desc)
    fd = os.open(a.dev, os.O_RDWR)
    if a.enable:
        payload = bytes.fromhex(a.enable)
        for rid in sorted(ids["feature"]):
            try:
                fcntl.ioctl(fd, hidioc_rw(0x06, 1 + len(payload)), bytearray([rid]) + payload)  # HIDIOCSFEATURE
                print(f"feature 0x{rid:02x} <- {a.enable}: ok")
            except OSError as e:
                print(f"feature 0x{rid:02x} <- {a.enable}: {e}")
        for rid in sorted(ids["output"]):
            try:
                os.write(fd, bytes([rid]) + payload)
                print(f"output  0x{rid:02x} <- {a.enable}: ok")
            except OSError as e:
                print(f"output  0x{rid:02x} <- {a.enable}: {e}")
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
```
