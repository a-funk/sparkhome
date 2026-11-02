# SparkHome — Games rows: emulators, native arm64 games, cloud/streamed play, Valve on ARM, launch/detect/exit/art

Research for the SparkHome launcher (tvOS-style 10-foot shell on an NVIDIA DGX Spark, `spark`, user `spark`).
Researched 2026-10-07/08. Nothing was installed or changed on the Spark: every on-box check below was a read-only
command over ssh (`apt-cache policy`, `snap info`, `vulkaninfo`, `xprop`, log reads). Sibling reports in this folder
already cover the UI/session design (`ui-session.md`) and Steam library art (`spark-lab.md` §7); this report links to
them instead of repeating them.

### Evidence labels

| label | meaning |
|---|---|
| **[M]** measured | observed directly on 2026-10-07/08: a read-only command on the Spark, or a live query of Flathub/GitHub/GitLab/Valve/NVIDIA servers from the Mac |
| **[D]** documented | vendor/project docs, source code, release notes, official statements |
| **[R]** reported | press, community wikis, issue comments, third-party tests |
| **[I]** inferred | my reasoning from the above; not tested |

---

## 0. Decisions in one page

1. **Three Games rows besides Steam**: *Retro & Consoles* (emulators), *Play Native* (arm64 Linux games), *Stream & Cloud*.
   Steam stays row 1, driven exactly as GameSpark already does it (pipe `-applaunch`, `console_log.txt`).
2. **Install channel order for arm64**: Flathub `--user` (aarch64 builds exist for most picks [M]) → the project's own
   arm64 AppImage (extracted, so no FUSE2 is needed) → snap → Ubuntu apt (old versions). One interactive `sudo` is needed
   once (`apt install flatpak libfuse2t64`); after that every install/update is per-user and needs no root.
3. **Flatpak + NVIDIA works on this box**: Flathub publishes `org.freedesktop.Platform.GL.nvidia-580-173-02` for
   **aarch64** [M], matching the running kernel module `580.173.02` [M]. After any host driver update run `flatpak update`
   or Flatpak games fall back to software rendering [I].
4. **Pin emulators to the Cortex-X925 cores** `5-9,15-19` [M] (`taskset -c 5-9,15-19 …`, or GameSpark's `fast_cpus()`).
   Canonical recommends the same set for Steam games on the Spark [D]. Emulators are single-thread bound [I].
5. **Lifecycle**: SparkHome owns the child process for every non-Steam tile (process exit = game exit). For Steam use the
   pipe plus `console_log.txt` / `gameprocess_log.txt` lines [M], and the Proton window class `steam_app_<appid>` [D][M].
6. **Ranked picks** (detail in §1): Steam › RetroArch › Dolphin › PPSSPP › DuckStation › Minecraft Java (Prism) ›
   SuperTuxKart › RPCS3 › Moonlight (only if a gaming PC exists) › Xbox Cloud (Chrome/Greenlight) › melonDS / Azahar /
   Flycast / xemu › Doom/Quake source ports › Factorio ARM64 (paid) › ARMSX2 (PS2, experimental).
7. **Do not ship by default**: Switch emulators (Nintendo DMCA of Feb 2026, keys required) [D]; shadPS4 (x86-64 only) [M];
   Cemu (no arm64 binary) [M]; GeForce NOW native app (x86_64-only repo) [M]; Boosteroid app ("no desktop ARM version") [D];
   Steam Link Flatpak (x86_64 only) [M]; Heroic (no arm64 release) [M].
8. **Art**: Steam `librarycache` + CDN 2x (see `spark-lab.md` §7) [M]; Flathub appstream icons/1080p screenshots for
   Flathub games [M]; libretro thumbnails for ROMs [D]; SteamGridDB API (user's own key) only for what is left [D].
9. **Valve on ARM**: Valve publishes native `linuxarm64` Steam client builds on its CDN in lockstep with x86 [M], plus FEX,
   ARM64 Proton 11 and SLR 4.0 arm64, but Valve staff say Steam for Linux "is not currently supported on ARM" [D]. Keep the
   Canonical snap (works, tuned by GameSpark); keep the Steam adapter thin so a later switch is cheap.

---

## 1. Ranked "Games" rows (what to put on the shelf)

Expected quality is for 1080p60 on this TV. Unless a row says [R], the quality is **inferred** from the CPU class (X925
at 3.9 GHz [M]), mature ARM64 JITs and the GB10 Vulkan 1.4 driver [M]; nothing below was run on the Spark yet.

| # | Tile / row | Install on Ubuntu 24.04 arm64 | Version seen (date) | Expected on Spark | Why this rank |
|---|---|---|---|---|---|
| 1 | **Steam library** (Cyberpunk, RDR2, RotTR, DOS2 installed [M]) | existing snap + GameSpark | snap 1.0.0.85 rev 245 [M] | measured by user: Cyberpunk 59–69 fps, RDR2 ~75 fps | already works; GameSpark profiles |
| 2 | **RetroArch** (NES→PS1, N64, GBA, DS, arcade) | `flatpak install --user flathub org.libretro.RetroArch` | 1.22.2 [M] (+226 nightly aarch64 cores dated 2026-10-08 [M]) | excellent [I] | one app, controller-first menus, playlists + free thumbnails, UDP quit control |
| 3 | **Dolphin** (GameCube/Wii) | `flatpak … org.DolphinEmu.dolphin-emu` | 2609 (2026-09-24) [M] | very good [I] | mature ARM64 JIT; huge library |
| 4 | **PPSSPP** (PSP) | `flatpak … org.ppsspp.PPSSPP` | 1.20.4 (2026-05-16) [M] | excellent [I] | ARM-first emulator, controller UI |
| 5 | **DuckStation** (PS1, Big Picture UI) | official `DuckStation-arm64.AppImage` (Flathub copy is EOL [M]) | "latest" 2026-10-06 [M] | excellent [I] | best PS1 UX; licence forbids repackaging (§3.6) |
| 6 | **Minecraft Java** via Prism Launcher | `flatpak … org.prismlauncher.PrismLauncher` | 11.1.1 [M] | excellent [I] | native Java; Prism meta ships linux-arm64 LWJGL natives [M] |
| 7 | **SuperTuxKart** | `flatpak … net.supertuxkart.SuperTuxKart` | 1.5 [M] | excellent [I] | family-friendly, full gamepad support |
| 8 | **RPCS3** (PS3) | `flatpak … net.rpcs3.RPCS3` or arm64 AppImage | 0.0.43-20159 Flathub / -20246 AppImage (2026-10-08) [M] | good per title; Skate 3 1080p ~60 fps on a GB10 box [R] | flagship "wow" tile; some titles broken on ARM64 [D] |
| 9 | **Moonlight** → gaming PC (only if one exists) | `flatpak … com.moonlight_stream.Moonlight` | 6.2.0 (2026-10-03) [M] | excellent if host exists; GB10 exposes Vulkan Video H.264/H.265/AV1 decode [M] | full PC library, native-quality streaming |
| 10 | **Xbox Cloud Gaming** | Chrome kiosk `https://www.xbox.com/play`, or `flatpak … io.github.unknownskl.greenlight` | Greenlight 2.4.2 [M] | good, 1080p [R]; hour caps from Nov 2026 [D] | Game Pass without local install |
| 11 | **melonDS** (DS) | `flatpak … net.kuribo64.melonDS` | 1.1 [M] | excellent [I] | small, reliable |
| 12 | **Azahar** (3DS) | `flatpak … org.azahar_emu.Azahar` (AppImage is x86_64 [M]) | 2126.1.1 [M] | very good [I] | needs decrypted dumps |
| 13 | **Flycast** (Dreamcast/NAOMI) | `flatpak … org.flycast.Flycast` | v2.7 [M] | excellent [I] | |
| 14 | **xemu** (original Xbox) | `flatpak … app.xemu.xemu` | 0.8.136 [M] | playable; Forza Motorsport 1080p capped 30 fps on GB10 box [R] | needs BIOS/MCPX/HDD dumps |
| 15 | **Doom / Quake / Quake II** source ports | `flatpak … org.zdoom.UZDoom org.yamagi.YamagiQ2 net.sourceforge.quakespasm.Quakespasm`; `apt install freedoom` | UZDoom 5.0.3, Yamagi 8.5.1 [M] | excellent [I] | use data the user owns on Steam, or Freedoom |
| 16 | **Factorio 2.1 (ARM64)** | standalone tarball from factorio.com/download/experimental (login) | 2.1 experimental (2026-09-18) [D] | good; renderer is OpenGL [D] | only notable paid native arm64 Linux game found |
| 17 | **ARMSX2** (PS2) | nightly `…Linux-arm64-4K-pages.AppImage` | nightly 2026-10-07 [M] | unknown, experimental [I] | upstream PCSX2 has no arm64 Linux build [M] |
| 18 | **chiaki-ng** → PS5/PS4 Remote Play (if a console exists) | `flatpak … io.github.streetpea.Chiaki4deck` | 1.10.0 [M] | good [I] | |
| 19 | Others for a "More games" shelf | Flathub aarch64 [M]: Luanti, Xonotic, SRB2, Ring Racers, OpenTTD, 0 A.D. R28, OpenRCT2, ScummVM (+free games via apt), OpenMW, DevilutionX, OpenRA, Warzone 2100, Mindustry, Veloren, Wesnoth, SuperTux, Endless Sky | see §5 | excellent [I] | mostly mouse-centric; pick controller-friendly ones for the front row |

---

## 2. Measured platform facts that shape the Games rows

All read-only, `ssh spark` (host resolved via `spark.local`), 2026-10-07/08.

| fact | value | why it matters |
|---|---|---|
| CPU topology | X925 = CPUs **5–9, 15–19** (capacity 997–1024, 3.9 GHz); A725 = 0–4, 10–14 (718–731, 2.808 GHz) [M] | pin emulators to `5-9,15-19` |
| CPU features | `atomics lrcpc sve uscat ilrcpc flagm sve2 i8mm bf16` [M] | RPCS3 arm64 needs ARMv8.1 LSE atomics [D]; Valve's ARM64 Steam client builds newer than mid-April 2026 (client 1776387948) reportedly need LSE too [R] — both satisfied |
| Page size | 4096 [M] | pick ARMSX2 "4K-pages" AppImage; RPCS3/FEX 16K issues do not apply |
| GPU / Vulkan | `NVIDIA GB10`, `PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU`, apiVersion 1.4.312, driver 580.173.2.0 [M] | |
| Vulkan Video | `VK_KHR_video_decode_h264/h265/av1`, `…encode_h264/h265/av1`, `video_maintenance1/2` [M] | Moonlight Vulkan-Video hardware decode should be possible [I] |
| Driver packages | `nvidia-driver-580-open 580.173.02-0ubuntu0.24.04.1`, `/sys/module/nvidia/version` = 580.173.02 [M] | Flatpak GL extension must be `nvidia-580-173-02` |
| Other Vulkan ICDs | `lvp, nouveau, intel, radeon, asahi, broadcom, freedreno, panfrost, virtio, gfxstream` JSONs present [M] | a game that picks a device itself can land on llvmpipe; GameSpark sets `VK_LOADER_DRIVERS_SELECT=*nvidia*` for its FEX Steam [M] |
| Flatpak | not installed; apt candidate `flatpak 1.14.6-1ubuntu0.1+esm1` [M] | one `sudo apt install flatpak` |
| AppImage FUSE | `fuse3` installed, **`libfuse2t64` not installed** [M] | older AppImages will not mount; extract them or install libfuse2t64 |
| User namespaces | `kernel.apparmor_restrict_unprivileged_userns = 1` [M] | Electron AppImages need `--no-sandbox`; prefer the Flathub builds of Electron apps [I] |
| X11 tools | `xprop`, `xwininfo`, `xrandr`, `gdbus`, `gamemoderun` present; **`xdotool`, `wmctrl` absent**; python3 `gi` Wnck 3.0 typelib present; python-xlib absent [M] | window find/activate via GameSpark `tools/xinput.py` (ctypes) or Wnck |
| Input ACLs | `/dev/input/event*` and `/dev/uinput` carry `user:spark:rw-` (uaccess) although spark is not in `input` [M] | SDL-based emulators can read pads in the session |
| Steam snap plugs | `joystick`, `uinput`, `steam-support`, `bluez` connected [M] | Steam Input can create virtual pads → possible double input in non-Steam emulators (see `ui-session.md` §C.8) |
| Java | default `java` is OpenJDK 1.8.0_492 [M] | Prism downloads its own Java 21/25 runtimes [I] |
| FEX on host | `fex-emu-armv8.4 2610-1~n`, `fex-emu-wine 2610-1~n` installed [M] | Windows-on-ARM64EC Wine route exists for non-Steam games (advanced, §5.3) |
| Steam logs | `console_log.txt`, `gameprocess_log.txt` under `~/snap/steam/common/.local/share/Steam/logs/` [M] | lifecycle events (§8.2) |
| Steam installed apps | 1091500 Cyberpunk 2077, 1174180 RDR2, 391220 RotTR, 435150 DOS2; tools 1070560, 1493710, 228980, 4183110 (SLR 4.0) [M] | filter tool appids out of the row |
| apt (noble) game versions | retroarch 1.18.0, supertuxkart 1.4, 0ad 0.0.26, openttd 13.4, minetest 5.6.1, openrct2 0.4.10, openmw 0.48, scummvm 2.8.0, ioquake3 1.36, yamagi-quake2 8.30, quakespasm 0.96.1, dhewm3 1.5.2, freedoom 0.13.0, minigalaxy 1.2.6, lutris 0.5.14, box64 0.2.6, wine 9.0; **dolphin-emu and gamescope: no arm64 candidate**; ppsspp, pcsx2, moonlight-qt, gzdoom not in apt [M] | apt is mostly old; use Flathub |
| snap (arm64 channel map from the Spark) | retroarch 1.22.2 (edge git 2026-10-06), supertuxkart 1.5, 0ad 0.28.0 (edge 0.29-dev), openttd 15.0 (edge 15.3), luanti 5.17.0, gzdoom 4.14.0 (third-party publisher), prismlauncher edge-only 9.4; **no** arm64 snap for ppsspp/dolphin/moonlight/duckstation/rpcs3/melonds/pcsx2/cemu/azahar/xemu/flycast/es-de/heroic/steamlink [M] | snaps are a fallback only |

---

## 3. Prerequisites and NVIDIA-on-arm64 graphics notes

### 3.1 Flatpak with the NVIDIA driver on aarch64

- Flathub builds the NVIDIA GL extension for `x86_64` and `aarch64` (`flathub.json`: `"only-arches": ["x86_64","aarch64"]`) [D],
  and `data/nvidia-580.173.02-aarch64.data` exists [M]. The Flathub API reports
  `org.freedesktop.Platform.GL.nvidia-580-173-02` with arches `x86_64, aarch64`, branch `1.4`, ~332 MB download [M].
  The next driver, `580.178.04`, is also already built for aarch64 [M].
- The extension must match the kernel module exactly. Flatpak normally picks it automatically on install/update [I];
  install it explicitly in provisioning so the first launch is not software-rendered:

```bash
sudo apt install -y flatpak                       # once (needs the user's sudo password)
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
V=$(sed 's/\./-/g' /sys/module/nvidia/version)    # 580-173-02 today
flatpak install --user -y flathub "org.freedesktop.Platform.GL.nvidia-$V//1.4"
```

- **Driver-update hazard** [I]: when apt moves the host to a new 580.x, Flatpak apps keep the old userspace until
  `flatpak update --user -y` pulls the matching extension. SparkHome should compare `/sys/module/nvidia/version`
  with `flatpak list --user --runtime | grep GL.nvidia` at boot and show a warning tile (cheap, prevents "why is Dolphin 5 fps").
- ROM folders: Flatpak apps only see what they are granted. Grant read-only access once per emulator, e.g.
  `flatpak override --user --filesystem=$HOME/Games:ro org.DolphinEmu.dolphin-emu` [D: flatpak override].

### 3.2 AppImages

- `libfuse2t64` is missing [M]. The runtime headers of the DuckStation, PPSSPP, Prism and ES-DE arm64 AppImages contain
  fuse3/static-runtime strings and no `libfuse.so.2` [M]; Vita3K, ARMSX2, RPCS3 and xemu could not be classified [M].
  Whether any of them runs without libfuse2 on this box is untested [I].
- Simplest robust path: **extract once, run `AppRun`** (no FUSE, faster start, a stable PID to wait on) [I]:

```bash
mkdir -p ~/Applications && cd ~/Applications
curl -fLO https://github.com/stenzek/duckstation/releases/download/latest/DuckStation-arm64.AppImage
chmod +x DuckStation-arm64.AppImage && ./DuckStation-arm64.AppImage --appimage-extract >/dev/null \
  && rm -rf duckstation && mv squashfs-root duckstation      # run: ~/Applications/duckstation/AppRun
```

  Alternative: `sudo apt install libfuse2t64` once and run AppImages directly.
- Electron AppImages (Greenlight, Heroic) need Chromium's sandbox, which Ubuntu's userns restriction [M] blocks unless run
  with `--no-sandbox` [I] → use their Flathub builds where they exist [I]. (OpenNOW-Qt is a Qt app, not Electron.)

### 3.3 Vulkan device selection

- The loader sees GB10 plus many Mesa ICDs [M]. GB10 reports `INTEGRATED_GPU`; llvmpipe reports CPU type, so apps that take
  the first device normally get GB10 [I]. GameSpark measured RDR2 falling onto llvmpipe (8 fps) under FEX when Mesa drivers
  were visible [M, GameSpark FINDINGS], hence `VK_LOADER_DRIVERS_SELECT=*nvidia*` for host-launched (non-Flatpak) games [I].
- Inside Flatpak the runtime's Mesa (llvmpipe) is also present; check each emulator's adapter dropdown on first run [I].
- No GB10- or DGX-Spark-specific bug was found in the issue trackers of RPCS3, DuckStation, PPSSPP, xemu, Azahar, RetroArch,
  PCSX2, ARMSX2, Vita3K, Flycast, melonDS, Moonlight or Prism (GitHub search, 2026-10-08) [M]. Absence of reports is not
  evidence of absence: GB10 desktop users are few [I].
- Prior art on other NVIDIA/ARM: RPCS3 hung under Vulkan on a Raspberry Pi 5 and was tested on OpenGL instead (Broadcom, not
  NVIDIA) [R: It's FOSS on the RPCS3 arm64 announcement]. Moonlight on Jetson needs L4T-specific packages [D] — neither
  applies to DGX OS's desktop driver [I].

### 3.4 CPU placement

- `taskset -c 5-9,15-19 <launch>` puts emulators on the X925 cores [M topology]. Canonical's snap maintainer recommends the
  same list as a per-game Steam launch option on the Spark ("roughly a 45 FPS performance boost" in the Cyberpunk benchmark
  on his Spark, 2026-07-07; the snap's default affinity was `0,2,4,…,18`) [D/R]. On this box GameSpark measured a smaller,
  per-game effect from core placement (+10 % Cyberpunk, −3 % Tomb Raider) [M: GameSpark README] — so measure per emulator.
- Affinity is inherited across `exec`/`fork`, so wrapping `flatpak run …` works [I]. GameSpark's `fast_cpus()` computes the
  list from `cpu_capacity` — reuse it rather than hard-coding [M: lib/env.sh].
- `gamemoderun` is installed [M]; its value on GB10 is untested [I].

### 3.5 Controllers

- Pads are readable by the session user via uaccess ACLs [M]; SDL emulators work without the `input` group [I].
- With Steam running, Steam Input's Desktop Layout can turn pad input into keyboard/mouse for non-Steam apps — the sibling
  report recommends "Disable Steam Input" on the Desktop Layout and Guide chords off (`ui-session.md` §C.8) [R/I].
  Test: launch Dolphin from SparkHome with Steam running and confirm a single input source in its controller config.
- Every emulator needs a "quit to SparkHome" path that works from the pad; SparkHome's own Guide long-press (evdev,
  `ui-session.md` §C.5) plus the per-kind quit in §8.4 covers it [I].

### 3.6 Legal and licence constraints (affects what SparkHome may ship)

- BIOS/firmware/keys must be dumped from hardware the user owns: PS1/PS2 BIOS (DuckStation, ARMSX2) [D], Xbox MCPX/BIOS/HDD
  (xemu) [D], PS3 firmware is downloadable from Sony (RPCS3) [D], Switch `prod.keys` + firmware [D].
- **Nintendo DMCA, 2026-02-12**: notice against Switch emulator repos (Eden, Citron AppImage, Ryujinx/Ryubing forks, Sudachi,
  Suyu, yuzu…), alleging TPM circumvention via `prod.keys` [D]. `eden-emulator/Releases` now returns HTTP 451 [M].
  Azahar (3DS) was not listed [D]. → keep Switch out of the default install.
- **DuckStation** is CC-BY-NC-ND: unmodified releases may be redistributed, "pre-configured settings and packages are considered
  modifications" [D]. SparkHome must download the official AppImage at install time and must not bundle a preconfigured copy.

---

## 4. Emulators (research item 1)

### 4.1 Master table

Availability columns are **[M]** (Flathub API `summary`/`appstream`, GitHub/GitLab release assets, ELF header bytes of
the AppImages, Flathub EOL list), all on 2026-10-08.

| System | Emulator | Flathub aarch64 | Official arm64 binary | Recommended on the Spark | 10-foot UX | GB10 / ARM evidence |
|---|---|---|---|---|---|---|
| Multi (8/16-bit, PS1, N64, GBA, DS, arcade) | RetroArch | yes, 1.22.2 (verified) | buildbot nightly `linux/aarch64` cores (226, built 2026-10-08); stable 1.22.2 buildbot has only x86/x86_64 Linux dirs; PPA `libretro/stable` has retroarch 1.22.2 for noble arm64; snap 1.22.2 | Flathub + nightly cores | excellent (Ozone/XMB, pad-first) [D] | none GB10-specific; cores are light [I] |
| GameCube/Wii | Dolphin | yes, 2609 (2026-09-24) | none besides Flathub; Ubuntu apt has no arm64 candidate | Flathub | desktop Qt UI; boot games directly [I] | long-standing AArch64 JIT (Android port) [I]; GB10 untested |
| PS2 | PCSX2 upstream | **no** (x86_64 only, v2.8.2) | none for Linux arm64 (nightly v2.9.111 assets are x64 Linux only) | — | — | ARMSX2 README: upstream's ARM64 build is interpreter-only; JITs are x86-64 [D] |
| PS2 | **ARMSX2** (PCSX2 fork) | no | nightly `Linux-arm64-4K-pages.AppImage`, `-16K-pages`, libretro core, SDL build (2026-10-07) | nightly 4K AppImage | PCSX2-style Big Picture UI [I] | initial Linux release 2026-07-06 "for testing" [R]; GB10 untested |
| PS2 | Play! | yes, 0.74 | — | fallback | basic [I] | lower compatibility [I] |
| PS2 | LRPS2 (`pcsx2_libretro`) | via RetroArch | in aarch64 nightly list [M] | experimental | RetroArch | arm64 recompiler fixes merged upstream [D: libretro/ps2 PR #170, #154] |
| PS3 | **RPCS3** | yes, 0.0.43-20159 | `rpcs3-binaries-linux-arm64` AppImage 0.0.43-20246 (2026-10-08) | Flathub (or AppImage for newest) | Qt UI; `--no-gui --fullscreen` boot [D] | Skate 3 1080p ~60 fps on MSI EdgeXpert (GB10) [R: HotHardware/ETA Prime, Nov 2025]; ARM64-broken list incl. MGS4, GT5, Heavenly Sword [D: issue #16484, updated 2026-04-13]; needs ARMv8.1 [D: #16392] |
| PS1 | **DuckStation** | EOL ("no longer maintained on Flathub"; last 2025-08-12) | `DuckStation-arm64.AppImage` (latest 2026-10-06; ELF aarch64) | AppImage (extracted) | Big Picture UI [D] | AArch64 recompiler [D] |
| PSP | **PPSSPP** | yes, 1.20.4 | `PPSSPP-v1.20.4-anylinux-aarch64.AppImage` | Flathub | controller UI [I] | ARM-native project [I] |
| 3DS | **Azahar** | yes, 2126.1.1 | GitHub `azahar.AppImage` is **x86_64** (ELF) | Flathub (project's recommended Linux format [D]) | Qt UI [I] | — |
| Wii U | Cemu | **no** (x86_64, 2.6) | none (releases v2.6 2025-02-06, x64 only) | skip, or build from source | — | AArch64 recompiler merged 2025-07 [D: PR #1556]; no Linux arm64 binary |
| Original Xbox | **xemu** | yes, 0.8.136 | `xemu-0.8.136-aarch64.AppImage` | Flathub | ImGui UI, pad navigable [I] | Forza Motorsport 1080p, game-capped 30 fps on GB10 box [R] |
| DS | **melonDS** | yes, 1.1 | `melonDS-1.1-appimage-aarch64.zip` | Flathub | Qt UI; `-f` fullscreen [D] | — |
| Switch | Eden | yes, 0.2.1 (2026-06-01, "Eden Contributors") | GitHub release repo DMCA-blocked (HTTP 451) | **not by default** (§3.6) | Qt UI | legal risk; GB10 untested |
| Switch | Ryujinx (Ryubing) | yes, 1.3.3 (2025-10-11); `org.ryujinx.Ryujinx` EOL | — | not by default | | |
| Switch | Citron | not on Flathub | `pkgforge-dev/Citron-AppImage` named in DMCA notice [D] | no | | |
| Dreamcast/NAOMI | **Flycast** | yes, v2.7 | GitHub AppImage x86_64 only | Flathub | ImGui UI [I] | — |
| PS4 | shadPS4 | **no** (x86_64, 0.19.0) | Linux zips are x86_64 [I from names] | **no** | — | runs PS4 x86-64 code natively, so needs an x86 host [I] |
| PS Vita | Vita3K | not on Flathub | `Vita3K-aarch64.AppImage` + `ubuntu-aarch64-latest.zip` (continuous 2026-10-07) | AppImage, optional | ImGui LiveArea UI [I] | experimental [I] |
| N64 (standalone) | RMG / simple64 | RMG v0.9.0 aarch64; simple64 aarch64 (2024-12) | — | optional; RetroArch `parallel_n64` covers N64 | | `mupen64plus_next` core is **absent** from the aarch64 nightly list [M] |
| Arcade | MAME | yes, 0.289 | — | optional | | |
| DOS/adventure | DOSBox Staging 0.83.0, ScummVM 2026.3.0 | yes | — | optional | | |
| Metroid Prime | PrimeHack 1.0.9 | yes | — | optional | | |

RetroArch aarch64 nightly cores include (subset, [M]): `snes9x, bsnes*, mesen, nestopia, fceumm, genesis_plus_gx,
picodrive, mgba, gambatte, sameduck, swanstation, pcsx_rearmed, mednafen_psx_hw, mednafen_saturn, yabasanshiro, kronos,
parallel_n64, melonds, melondsds, desmume, citra, ppsspp, dolphin, flycast, pcsx2 (LRPS2), play, fbneo, mame,
mame2003_plus, stella, opera, neocd, same_cdi, puae, vice_x64, bluemsx, cap32, scummvm, dosbox_pure, prboom, tyrquake,
vitaquake2, vitaquake3, ecwolf, nxengine, cannonball, mrboom, tic80, retro8, 2048`.

### 4.2 Per-emulator launch lines (verified flags)

Paths assume `R=$HOME/Games` and Flathub installs. Prefix every line with `taskset -c 5-9,15-19` (§3.4).

| Emulator | Command | Flag source |
|---|---|---|
| RetroArch | `flatpak run org.libretro.RetroArch -f -L "$HOME/.var/app/org.libretro.RetroArch/config/retroarch/cores/snes9x_libretro.so" "$R/snes/Game.sfc"` | `-L` core / content [D]; cores path is the Flatpak config dir [I — confirm after first core download] |
| Dolphin | `flatpak run org.DolphinEmu.dolphin-emu -b -C Dolphin.Display.Fullscreen=True -e "$R/gc/Game.rvz"` | `-b` batch (exit with game), `-e` exec, `-C` config override [D: `CommandLineParse.cpp` options `-u -m -e -n -C -s -d -l -b -c -v -a`] |
| PPSSPP | `flatpak run org.ppsspp.PPSSPP "$R/psp/Game.iso"` (set `FullScreen = True` in ppsspp.ini once) | path argument [D]; fullscreen flag not verified → use ini [I] |
| DuckStation | `~/Applications/duckstation/AppRun -batch -fullscreen -- "$R/ps1/Game.chd"` | `-batch -fullscreen --` [D: `qthost.cpp`; also `-bigpicture`, `-nogui`, `-resume`, `-state`] |
| RPCS3 | `flatpak run net.rpcs3.RPCS3 --no-gui --fullscreen "$R/ps3/GAME/PS3_GAME/USRDIR/EBOOT.BIN"` | `--no-gui`, `--fullscreen` (only with no-gui) [D: `rpcs3.cpp`] |
| xemu | `flatpak run app.xemu.xemu -full-screen -dvd_path "$R/xbox/Game.xiso.iso"` | `-dvd_path` [D: `system/vl.c`], `-full-screen` QEMU option [D] |
| Azahar | `flatpak run org.azahar_emu.Azahar -f "$R/3ds/Game.3ds"` | `-f/--fullscreen` [D: `citra_qt.cpp`] |
| melonDS | `flatpak run net.kuribo64.melonDS -f "$R/nds/Game.nds"` | `-f/--fullscreen` [D: `CLI.cpp`] |
| Flycast | `flatpak run org.flycast.Flycast "$R/dc/Game.chd"` (fullscreen in its settings) | path argument [I] |
| ARMSX2 | `~/Applications/armsx2/AppRun -batch -fullscreen -- "$R/ps2/Game.iso"` | PCSX2 flags; **unverified for the fork** [I] |
| Vita3K | `~/Applications/vita3k/AppRun -r <TITLE_ID>` | `--installed-path,-r` [D: `config.cpp`] |
| Eden (opt-in) | `flatpak run dev.eden_emu.eden -f -g "$R/switch/Game.nsp"` | yuzu-lineage flags [I] |

### 4.3 Frontends (optional)

- **ES-DE 3.5.0** (2026-09-30) ships `ES-DE_aarch64.AppImage` (ELF aarch64) [M]; not on Flathub [M]. Good as a single
  "Retro" tile if the user wants a full game-browser; it locates Flatpak/AppImage emulators through its find rules [I — not
  re-checked against ES-DE docs in this session].
- **Pegasus**: Flathub aarch64 0.15 alpha (2025-03-31) [M]; GitHub continuous has only Pi/odroid static builds and an amd64
  .deb [M] → stale, skip.
- **RetroDECK**, **Steam ROM Manager**: Flathub x86_64 only [M] → not usable.
- **Ponytail option (recommended)** [I]: no second frontend. Let RetroArch scan ROMs into playlists; SparkHome reads the
  `.lpl` JSON (`items[].path, label, core_path, core_name, crc32, db_name`) and renders its own tvOS rows; art comes from
  RetroArch's `thumbnails/<playlist>/Named_Boxarts|Named_Snaps|Named_Titles/<label>.png` (the characters `` &*/:`<>?\| `` in a label become `_`) [D].

### 4.4 NVIDIA-proprietary Vulkan on arm64 — what is known

| item | status |
|---|---|
| Driver userspace in Flatpak | available for 580.173.02 aarch64 [M] |
| Vulkan 1.4 + video extensions on GB10 | present [M] |
| GB10-specific emulator bugs | none found (search) [M]; untested [I] |
| Device-selection pitfall (llvmpipe) | seen by GameSpark under FEX [M]; mitigate with `VK_LOADER_DRIVERS_SELECT=*nvidia*` outside Flatpak and the adapter dropdown inside [I] |
| Shader-compile stutter | expect first-run stutter in Dolphin/RPCS3/xemu (pipeline caches build per game) [I] |
| OpenGL | NVIDIA ships desktop GL/GLX on arm64; Azahar's 2024 "arm64 has GL when unsupported" issue (#124, closed) concerned other GPUs [D/I] |

---

## 5. Native arm64 games (research item 2)

### 5.1 Open-source and data-driven ports

All Flathub entries below report `aarch64` [M] (version/date from Flathub appstream).

| Game | Install | Version | Needs | Controller fit | Expected |
|---|---|---|---|---|---|
| SuperTuxKart | `net.supertuxkart.SuperTuxKart` (snap 1.5; apt 1.4) | 1.5 | — | excellent | excellent [I] |
| Minecraft Java | `org.prismlauncher.PrismLauncher` (or `PrismLauncher-Linux-aarch64.AppImage`) | 11.1.1 | Microsoft account; Prism fetches Java | good with Controlify-type mods; vanilla is KB/M [I] | excellent [I] |
| Luanti (Minetest) | `org.luanti.luanti` (snap 5.17.0) | 5.17.0 | — | ok | excellent [I] |
| 0 A.D. Release 28 | `com.play0ad.zeroad` (snap 0.28.0) | 0.28.0 | — | poor (RTS) | good [I] |
| UZDoom | `org.zdoom.UZDoom` (GZDoom Flatpak is EOL → "switch to UZDoom") | 5.0.3 | IWAD: `apt install freedoom` or owned DOOM/DOOM II WADs | good | excellent [I] |
| Yamagi Quake II | `org.yamagi.YamagiQ2` | 8.5.1 | owned Quake II data | good | excellent [I] |
| Quakespasm (Quake) | `net.sourceforge.quakespasm.Quakespasm` (apt 0.96.1); ioquake3 apt 1.36 | — | owned Quake/Quake III data | good | excellent [I] |
| dhewm3 (Doom 3) | `org.dhewm3.Dhewm3` | 1.5.4 | owned Doom 3 data | ok | excellent [I] |
| OpenRCT2 | `io.openrct2.OpenRCT2` | 0.5.5 | RCT2 data (Steam/GOG) | poor | excellent [I] |
| OpenTTD | `org.openttd.OpenTTD` | 15.3 | — (OpenGFX auto) | poor | excellent [I] |
| OpenMW | `org.openmw.OpenMW` | 0.51.0 | Morrowind data | ok | good [I] |
| DevilutionX | `org.diasurgical.DevilutionX` | — | Diablo data (or shareware) | good | excellent [I] |
| Xonotic | `org.xonotic.Xonotic` | 0.8.6 | — | ok | excellent [I] |
| SRB2 / Ring Racers | `org.srb2.SRB2`, `org.kartkrew.RingRacers` | 2.2.15 / 2.4 | — | excellent | excellent [I] |
| ScummVM + free games | `org.scummvm.ScummVM`; data: `apt install beneath-a-steel-sky flight-of-the-amazon-queen lure-of-the-temptress drascula` | 2026.3.0 | — | good | excellent [I] |
| Others | OpenRA, fheroes2, Warzone 2100 4.7.0, Mindustry v160.5, Veloren (Airshipper 0.17.0), Wesnoth 1.18.8, Hedgewars 1.0.3, SuperTux 0.7.0, Endless Sky 0.11.2, Extreme Tux Racer, Space Cadet Pinball, DOSBox-X | | | mixed | good [I] |
| Not arm64 | ET: Legacy, Zandronum, Doomsday, Sonic 3 A.I.R. (Flathub x86_64 only) [M] | | | | |

Minecraft details:
- Latest release `26.3` requires Java 25 and LWJGL 3.4.3 [M: piston-meta]. Mojang's own metadata lists no linux-arm64 LWJGL
  natives (only netty `linux-aarch_64`) [M], so the official launcher path does not cover this box [I].
- Prism's meta has `linux-arm64` natives for LWJGL 3.3.1–3.4.3 and for LWJGL 2.9.4 [M] → modern and old versions should
  run natively [I]. Launch a prepared instance: `flatpak run org.prismlauncher.PrismLauncher --launch "<instance-id>"`
  (`-l/--launch`, also `--server`, `--world`, `--profile`, `--offline`) [D: `Application.cpp`].
- Bedrock alternative: `io.mrarm.mcpelauncher` (aarch64, v1.8.4) runs the Android build; needs a Google Play purchase [M/D]; unofficial.

### 5.2 Commercial native arm64

- **Factorio**: "we are pleased to make this port available to all ARM64 Linux users today" (FFF #446, 2026-09-18) [D].
  Standalone (incl. headless) on factorio.com's experimental download page; on Steam via the experimental branch (2.1),
  where one depot ships both binaries and a script picks by host architecture; moved to Steam Linux Runtime 4.0 because only
  4.0 got ARM64 [D]. Native ARM64 ran ~11 % faster than x86-via-FEX on Steam Frame, ~35 % on Asahi [D].
  On this box use the **standalone tarball**: inside Canonical's snap the whole Steam container is x86 under FEX, so the
  script would likely choose the x86 binary [I]. Renderer is OpenGL and was the bottleneck on Steam Frame [D].
- **Half-Life: Alyx** got an ARM64 port for Steam Frame (VR-only, irrelevant to a TV) [R].
- Native ARM64 Linux commercial games remain rare; Steam Frame "Verified" (200+ titles by Oct 2026) mostly means
  FEX/Proton, not ARM binaries [R: GamingOnLinux].

### 5.3 Stores beyond Steam (GOG, itch, Epic, Heroic)

| tool | arm64 status |
|---|---|
| Heroic | releases ship Linux **x86_64 only** (v2.22.3, 2026-09-16) [M]; Flathub x86_64 only [M]; a maintainer says Heroic runs natively on aarch64 and CI builds arm64 artifacts that are not published (2026-06-27) [R: issue #4611]; arch-aware Wine/Proton download PR #5722 was closed [M] |
| Minigalaxy (GOG) | apt 1.2.6 (Python) [M]; Flathub x86_64 only [M]; GOG Linux games are x86_64 → would run under FEX [I] |
| itch app | Flathub x86_64 only [M] |
| Lutris | Flathub x86_64 only [M]; apt 0.5.14 [M] |
| Windows games outside Steam | Wine arm64 + FEX ARM64EC is reported to work for at least one old title on a Spark [R: "DGX Spark as a daily driver", 2026-07]; this box already has `fex-emu-wine 2610` [M]. Advanced, not a launcher tile in v1 [I] |

---

## 6. Cloud and streamed gaming (research item 3)

| Service | arm64 Linux status | How to run on the Spark | Expected | Notes |
|---|---|---|---|---|
| **Moonlight** (stream from own PC with Sunshine/Apollo or NVIDIA host) | Flathub aarch64 6.2.0 [M]; GitHub AppImage x86_64 only [M]; snap not offered on arm64 [M]; Cloudsmith arm64 debs for noble exist but target V4L2 SBC decoders [D] | `flatpak run --env=PREFER_VULKAN=1 --env=VULKAN_IS_SLOW=0 --env=GL_IS_SLOW=0 com.moonlight_stream.Moonlight stream <host> "<App>" --1080 --fps 60 --display-mode fullscreen --quit-after` | excellent if a host exists [I] | v6.0 added Vulkan Video decode (H.264/HEVC/AV1) on Linux, `PREFER_VULKAN=1` forces the Vulkan renderer [D]; v6.2 assumes non-x86 GPUs are slow unless `GL_IS_SLOW=0/VULKAN_IS_SLOW=0` [D]; GB10 has the decode extensions [M]; software decode fallback on X925 [I]. CLI actions `stream/list/quit/pair`, options incl. `--quit-after`, `--video-decoder`, `--video-codec` [D] |
| **Xbox Cloud Gaming** | browser; Linux not in Microsoft's browser table [R]; Chrome on Linux works per community since 2021–22 [R] | `google-chrome --user-data-dir=$HOME/.local/share/sparkhome/profiles/xcloud --kiosk --app=https://www.xbox.com/play --password-store=basic --no-first-run` | good, 1080p [R] | Linux sessions looked softer until UA spoofed to Edge/Windows (2022 reports) [R]; better-xcloud wiki: Edge-on-Windows UA, add `smarttv` for the TV layout (Jan 2024) [R]. Template UA (inferred, keep major current): `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36 Edg/155.0.0.0`. **Hour caps from November 2026**: Ultimate 15 h, Premium 10 h, Essential 5 h per month; extra time purchasable [D: Xbox Wire 2026-09-03] |
| Xbox via Greenlight / XStreaming | Flathub aarch64: Greenlight 2.4.2, XStreamingDesktop 1.10.2 [M] | `flatpak run io.github.unknownskl.greenlight --fullscreen --connect=xcloud_<title>` (console: `--connect=F000…`) [D] | good [I] | unofficial clients; also stream from an owned Xbox (xHome) [D]; gamepad + rumble [D] |
| **GeForce NOW** | native app: NVIDIA's Flatpak repo contains only `app/com.nvidia.geforcenow/x86_64/master` [M]; requirements list x86/x64 CPU, Ubuntu 24.04, Xorg for NVIDIA, driver ≥ 580.126.07, Vulkan Video decode [D]; browser list: Chrome on Windows/macOS/Chromebook only — Linux absent [D] | try Chrome kiosk `https://play.geforcenow.com` with a ChromeOS/Windows UA [I]; unofficial: `io.github.astralvixen.geforce-infinity` (Flathub aarch64 1.2.2) [M], OpenNOW (MIT, nightly `Linux-arm64.AppImage/.deb`) [M] | unknown → test | native Linux app GA 2026-08-13 [R: GamingOnLinux]; on Linux AV1 never offered, HEVC instead (user comments) [R]. Unofficial clients may breach ToS [I] |
| **Amazon Luna** | Chrome on PC/Mac/Chromebook; "other devices… may not work as expected"; Linux not listed [D/R] | Chrome kiosk `https://luna.amazon.com` | unknown | April 2026: purchased games and third-party subscriptions removed after 2026-06-10 [R: Wikipedia]; low priority |
| **Boosteroid** | help centre: "64-bit distribution (x86-64); there is no desktop ARM version" [D]; browser support on Linux not stated [D] | Chrome kiosk `https://cloud.boosteroid.com` | unknown | low priority |
| **Steam Remote Play / Steam Link** | Steam Link Flatpak x86_64 only [M]; Raspberry Pi package is Pi-specific [R]; the snap's x86 client would run the streaming client under FEX — untested [I]; Valve's native ARM64 client does Remote Play with V4L2 hardware decode on Qualcomm [R: steam-for-linux #13689] | — | unknown | prefer Moonlight |
| **PS5/PS4 Remote Play** | chiaki-ng Flathub aarch64 1.10.0 [M]; GitHub `chiaki-ng.AppImage_arm64` [M] | `flatpak run io.github.streetpea.Chiaki4deck` | good [I] | only if the user owns a PlayStation |

Chrome note [I]: give every cloud tile its **own `--user-data-dir`**. Chrome hands a second launch with an already-running
profile to the existing process and exits immediately, which would break "process exit = tile closed". Keep the SparkHome
shell's profile separate too (`ui-session.md` §0). Chrome on this box has no VA-API [given], so browser streams are
CPU-decoded [I]; Moonlight is the only path here that can use the GPU decoder [I].

---

## 7. Valve and ARM (research item 4)

| date | fact | label |
|---|---|---|
| 2025-11 | Steamworks SDK adds `linuxarm64` (and `androidarm64`) libraries | [R: GamingOnLinux] |
| 2025-11-12 | Steam Frame announced (Snapdragon 8 Gen 3, SteamOS); FEX for x86, Proton for Windows, Lepton (Waydroid fork) for Android | [R: Wikipedia] |
| 2026-01 | Canonical's arm64 Steam snap call for testing: x86 Steam client under FEX | [D: Ubuntu Discourse] |
| mid-April 2026 | Valve ARM64 client builds newer than client 1776387948 reportedly require Armv8.1 LSE (SIGILL on Cortex-A57); the reporter calls the native client "not released officially" | [R: steam-for-linux #13288, opened 2026-06-03] |
| 2026-06-02 | Canonical snap **stable on arm64**; lists "NVIDIA DGX Spark and associated GB10 devices"; DGX Spark tested with GL+Vulkan thunking; "not a Valve-supported project"; plan to rebase on Valve's native client once Proton 11 leaves beta | [D] |
| 2026-07-07 | Proton 11.0-1 stable; 11.0-2 (2026-08-21) updates FEX to 2607 for ARM64EC builds | [D: ValveSoftware/Proton releases] |
| 2026-07-26 / 08-24 / 10-04 | Valve staff (kisak-valve) close ARM reports: "Steam for Linux is not currently supported on ARM based hardware (#4061)" — including one from a user running the native `steamrtarm64` client from `steam_client_steamdeck_stable_linuxarm64` | [D: #13451, #13544, #13689] |
| 2026-08-03 | FEX ("translates x86 compiled games to Arm64") and Lepton (Android) described as Steam Frame compatibility layers; SteamDB lists `proton-arm64ec-4` / `proton-arm64ec-experimental`; outlets disagree whether the tools were public yet | [R: GamingOnLinux, TechSpot, gHacks] |
| 2026-09-18 | Steam Frame released (US$1,059 / 1,299) | [R: Wikipedia] |
| 2026-10-08 | Valve's CDN serves `steam_client_linuxarm64` and `steam_client_steamdeck_stable_linuxarm64` at version **1788652215** (2026-09-05) — the same version as the x86 `steam_client_ubuntu12` stable manifest — and `steam_client_publicbeta_linuxarm64` 1791415817 (2026-10-07); `steam-runtime-steamrt-arm64.tar.xz` (52 MB) on repo.steampowered.com | [M] |
| 2026-10-08 | Snap on the Spark: stable 1.0.0.85 rev 245 (2026-06-02), edge rev 280 (2026-09-21); no sign yet of the native-client rebase | [M] |

What the native client needs and does (community packaging, not Valve docs): the tree runs from `steamrtarm64/steam`;
x86 Linux games and x86 Proton go through Valve's FEX tool downloaded as a Steam app; Windows games through ARM64 Proton
(ARM64EC Wine with FEX inside); apps required: FEX, Steam Linux Runtime 4.0 arm64, an ARM64 Proton [R: steam-arm64-nix
README, checked 2026-09-23]. Remote Play, overlay, Big Picture "unverified" there [R].

**Is there SteamOS for generic ARM?** No public image found. Collabora published an experimental "Holo Core" AArch64 Arch
port for Steam Frame in July 2026 [R: Wikipedia]. Nothing targets DGX Spark [I].

**Recommendation** [I]: keep Canonical's snap (it works here, it is what GameSpark tuned, and Canonical lists the Spark).
Track the snap's eventual rebase onto Valve's native client. SparkHome's Steam adapter only needs the pipe path and the
log path, so a switch to the native client (or to GameSpark's system-FEX Steam) is a two-path config change.

---

## 8. Launch, detect, exit and art (research item 5)

### 8.1 Three launch kinds are enough (ponytail)

| kind | covers | launch |
|---|---|---|
| `steam` | everything in the Steam library | GameSpark `steam_launch <appid>` → writes `-applaunch <appid>` to `~/snap/steam/common/.steam/steam.pipe`, confirms on `ExecCommandLine: "-applaunch <appid> ` in `console_log.txt`, retries 4× because Steam drops pipe commands while loading [D: GameSpark `lib/env.sh`] |
| `exec` | Flatpak, extracted AppImage, apt/snap binaries, Moonlight, Greenlight | `setsid taskset -c 5-9,15-19 <argv…>`; SparkHome keeps the PID |
| `url` | Xbox Cloud, GeForce NOW web, Luna, Boosteroid | `google-chrome --user-data-dir=<per-tile> --kiosk --app=<url> [--user-agent=…]` (an `exec` with a fixed argv) |

Steam Big Picture as a tile = `steam_cmd steam://open/bigpicture` [D: GameSpark `tools/steam-console.sh`].

### 8.2 Detecting start and exit

| kind | start | exit |
|---|---|---|
| steam | `console_log.txt`: `Game process added : AppID N "…reaper SteamLaunch AppId=N -- …"` [M]; `gameprocess_log.txt`: `AppID N adding PID X as a tracked process` [M]; process `reaper SteamLaunch AppId=N` exists [M] | `console_log.txt`: `Game process removed: AppID N …` [M]; `gameprocess_log.txt`: `AppID N no longer tracking PID X, exit code Y` then `Remove N from running list` [M] |
| exec | `fork` returned; window maps (below) | `waitpid` on the child. `flatpak run` stays the parent of the sandbox for the app's lifetime [I]; an extracted AppImage's `AppRun` execs the binary [I] |
| url | Chrome window with the tile's profile maps | Chrome process for that `--user-data-dir` exits |

Note: `registry.vdf` on this box has **no** `RunningAppID` key [M], so do not rely on it.

### 8.3 Finding the window (for focus only)

- Steam/Proton games: Proton's Wine sets `WM_CLASS` to `steam_app_<SteamAppId>` (`steam_proton` if unset) [D:
  ValveSoftware/wine `dlls/winex11.drv/window.c`]; measured for DOS2 on this box by the sibling session [M: `ui-session.md` §0].
  The PIDs in `gameprocess_log.txt` give a second match key [M].
- Flatpak apps run in their own PID namespace (`flatpak-run.c` passes `--unshare-pid`) [D], so a Flatpak window's
  `_NET_WM_PID` is not a host PID [I]. Match by `WM_CLASS` instead, learned on first launch (record the class of the first
  window that maps after the launch) and stored in the tile manifest [I]. `flatpak ps` lists instances [D].
- Tools on the box: `xwininfo -root -tree` prints id, title and `("instance" "class")`; `xprop -id <id> _NET_WM_PID`;
  activate with GameSpark `python3 -I tools/xinput.py activate 0xID` (`_NET_ACTIVE_WINDOW`) [M/D]. python3-gi Wnck also
  lists windows with PID/class/fullscreen and can activate them [M: listed `steam steamwebhelper "Steam Big Picture Mode"
  fullscreen=True`]. Focus/stacking rules for mutter 46: `ui-session.md` §C.4.

### 8.4 Exit / "Home" per kind

| kind | graceful | forced |
|---|---|---|
| RetroArch | UDP `QUIT` (or `CLOSE_CONTENT`, `SAVE_STATE`, `PAUSE_TOGGLE`, `MENU_TOGGLE`, `GET_STATUS`) to `127.0.0.1:55355` after `network_cmd_enable = "true"`; `echo -n QUIT \| nc -u -w1 127.0.0.1 55355` or `retroarch --command QUIT` [D] | as Flatpak |
| other Flatpak | the emulator's own quit hotkey mapped to a pad combo [I] | `flatpak kill <app-id>` ("Stop a running application") [D] |
| AppImage / native | SIGTERM to the process group (`setsid` at launch) [I] | SIGKILL after ~5 s [I] |
| Moonlight | `moonlight quit <host>` ends the host app; the client exits at stream end [D] | SIGTERM |
| url (Chrome) | close the window (`_NET_CLOSE_WINDOW`) [I] | SIGTERM the profile's Chrome |
| steam | the user exits via the Steam overlay / Big Picture "Exit game" [I] | SIGTERM the PIDs tracked in `gameprocess_log.txt` for that AppID [I — test on DOS2 first; unsaved progress is lost] |

Pausing instead of quitting (SIGSTOP) has real risks; see `ui-session.md` §C.9.

### 8.5 Art sources (in order)

1. **Steam games**: `appcache/librarycache/<appid>/**/{library_hero,library_capsule|library_600x900,library_header|header,logo}.*`
   (two layouts: hashed sub-folders and flat) and CDN 2x files — full measurements in `spark-lab.md` §7. Static CDN paths
   still answer 200 here: `https://cdn.cloudflare.steamstatic.com/steam/apps/1091500/{library_600x900.jpg,library_hero.jpg,logo.png,header.jpg}`
   and `https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/1091500/…` [M]; the store API returns hashed
   URLs (`…/apps/1091500/<sha1>/header.jpg?t=…`) [M].
2. **Flathub games/emulators**: `https://flathub.org/api/v2/appstream/<app-id>` → `icon` (128 px) and `screenshots[].sizes[]`
   with 1920×1080 originals, e.g. SuperTuxKart [M]. Project-provided, safe to cache [I].
3. **ROMs**: RetroArch downloads libretro thumbnails from `https://thumbnails.libretro.com/` into
   `thumbnails/<playlist>/Named_Boxarts|Named_Snaps|Named_Titles/` [D].
4. **Everything else: SteamGridDB API v2** [D: official client `SteamGridDB/node-steamgriddb`]
   - Base `https://www.steamgriddb.com/api/v2`, header `Authorization: Bearer <key>`; key from the user's
     `https://www.steamgriddb.com/profile/preferences` (API tab; Steam login) [D].
   - `GET /search/autocomplete/{term}` → game id; `GET /grids|/heroes|/logos|/icons/{game|steam}/{id}` with
     `dimensions, styles, mimes, types, nsfw, humor, epilepsy, page` [D]. Results carry `url`, `thumb`, `author` [D].
   - Terms: the ToS page is client-rendered and could not be read by fetch or curl [M: 2.5 KB shell page]. Treat the key as
     the user's personal key (not shipped in SparkHome), cache images locally for personal use, keep `author` for credit,
     never redistribute [I].

### 8.6 Minimal tile manifest (what the agent harness and UI both read)

```json
[
  {"id": "steam:1091500", "row": "Steam", "title": "Cyberpunk 2077", "kind": "steam", "appid": 1091500},
  {"id": "ra:snes:super-metroid", "row": "Retro & Consoles", "title": "Super Metroid", "kind": "exec",
   "argv": ["flatpak", "run", "org.libretro.RetroArch", "-f", "-L",
            "/home/spark/.var/app/org.libretro.RetroArch/config/retroarch/cores/snes9x_libretro.so",
            "/home/spark/Games/snes/Super Metroid (USA).sfc"],
   "cpus": "5-9,15-19", "window_class": "retroarch", "quit": "udp:127.0.0.1:55355:QUIT",
   "art": {"poster": "thumbnails/Nintendo - Super Nintendo Entertainment System/Named_Boxarts/Super Metroid (USA).png"}},
  {"id": "url:xcloud", "row": "Stream & Cloud", "title": "Xbox Cloud Gaming", "kind": "url",
   "url": "https://www.xbox.com/play", "profile": "xcloud"}
]
```

`window_class` is learned on first run when absent [I]. The agent never needs pixels to launch: `launch(id)`,
`quit_foreground()`, `list(row)` map onto these entries [I].

---

## 9. Install script (staged, copy-paste)

Run as `spark` in the GNOME session. Stage 0 needs the user's sudo password once.

```bash
# --- Stage 0 (root, once) ------------------------------------------------------------
sudo apt install -y flatpak libfuse2t64
sudo apt install -y freedoom beneath-a-steel-sky flight-of-the-amazon-queen lure-of-the-temptress drascula  # optional free data

# --- Stage 1: Flathub, per user, no root afterwards ---------------------------------
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
V=$(sed 's/\./-/g' /sys/module/nvidia/version)
flatpak install --user -y flathub "org.freedesktop.Platform.GL.nvidia-$V//1.4"
flatpak install --user -y flathub \
  org.libretro.RetroArch org.DolphinEmu.dolphin-emu org.ppsspp.PPSSPP net.rpcs3.RPCS3 \
  net.kuribo64.melonDS org.azahar_emu.Azahar org.flycast.Flycast app.xemu.xemu \
  org.prismlauncher.PrismLauncher net.supertuxkart.SuperTuxKart org.zdoom.UZDoom \
  org.yamagi.YamagiQ2 net.sourceforge.quakespasm.Quakespasm org.scummvm.ScummVM \
  com.moonlight_stream.Moonlight io.github.unknownskl.greenlight
# optional: com.play0ad.zeroad org.luanti.luanti org.xonotic.Xonotic org.srb2.SRB2 org.kartkrew.RingRacers \
#           io.openrct2.OpenRCT2 org.openttd.OpenTTD org.openmw.OpenMW org.diasurgical.DevilutionX \
#           io.github.streetpea.Chiaki4deck org.purei.Play io.github.astralvixen.geforce-infinity
for app in org.libretro.RetroArch org.DolphinEmu.dolphin-emu org.ppsspp.PPSSPP net.rpcs3.RPCS3 \
           net.kuribo64.melonDS org.azahar_emu.Azahar org.flycast.Flycast app.xemu.xemu; do
  flatpak override --user --filesystem="$HOME/Games:ro" "$app"
done

# --- Stage 2: RetroArch cores (or use RetroArch's Online Updater) ----------------------
CORES=$HOME/.var/app/org.libretro.RetroArch/config/retroarch/cores   # [I] confirm path after first launch
mkdir -p "$CORES" && T=$(mktemp -d)
for c in snes9x mesen genesis_plus_gx mgba swanstation parallel_n64 melonds fbneo flycast ppsspp; do
  curl -fsSL "https://buildbot.libretro.com/nightly/linux/aarch64/latest/${c}_libretro.so.zip" -o "$T/c.zip" \
    && unzip -oq "$T/c.zip" -d "$CORES"
done; rm -rf "$T"

# --- Stage 3: official arm64 AppImages, extracted (no FUSE needed) ---------------------
mkdir -p ~/Applications && cd ~/Applications
fetch() { curl -fL -o "$1.AppImage" "$2" && chmod +x "$1.AppImage" && ./"$1.AppImage" --appimage-extract >/dev/null \
          && rm -rf "$1" && mv squashfs-root "$1" && rm "$1.AppImage"; }
fetch duckstation https://github.com/stenzek/duckstation/releases/download/latest/DuckStation-arm64.AppImage
fetch vita3k      https://github.com/Vita3K/Vita3K/releases/download/continuous/Vita3K-aarch64.AppImage     # optional
# ARMSX2 nightly names change daily; resolve the newest 4K-pages AppImage:
URL=$(python3 -I -c 'import json,urllib.request as u; r=json.load(u.urlopen("https://api.github.com/repos/ARMSX2/ARMSX2/releases?per_page=10"));print(next(a["browser_download_url"] for x in r if x["tag_name"].startswith("nightly") for a in x["assets"] if a["name"].endswith("Linux-arm64-4K-pages.AppImage")))')
fetch armsx2 "$URL"                                                                                      # optional, experimental
# ES-DE (optional frontend): https://gitlab.com/es-de/emulationstation-de/-/releases  -> ES-DE_aarch64.AppImage

# --- Stage 4 (manual): Factorio ARM64 standalone from https://factorio.com/download/experimental (login) ---
```

Snap fallbacks with arm64 channels (root): `sudo snap install retroarch supertuxkart 0ad openttd luanti` [M].

---

## 10. Verification plan (smallest experiments, in order)

| # | experiment | pass gate |
|---|---|---|
| 1 | Stage 0–1; `flatpak run --command=sh org.libretro.RetroArch -c 'ls /usr/lib/*/GL; vulkaninfo --summary'` (or RetroArch's info screen) | Vulkan device = NVIDIA GB10 inside Flatpak, not llvmpipe |
| 2 | RetroArch + snes9x + one homebrew/owned ROM via §4.2 line; send UDP `QUIT` | 60 fps; process exits on `QUIT` within 2 s |
| 3 | Dolphin `-b -e` one GC title; `taskset` vs no `taskset` | full speed; record fps with/without pinning |
| 4 | DuckStation, PPSSPP, melonDS, Flycast, Azahar one title each | full speed, pad works, Guide long-press returns to SparkHome |
| 5 | RPCS3 one light + one heavy owned title (`--no-gui --fullscreen`) | compare against [R] Skate 3 ~60 fps; note shader stutter |
| 6 | Moonlight to a PC host with `PREFER_VULKAN=1 VULKAN_IS_SLOW=0` | overlay (Ctrl+Alt+Shift+S) shows hardware/Vulkan decode; < 10 ms decode |
| 7 | xCloud in Chrome kiosk with and without Edge UA; then Greenlight | 1080p stream, pad recognised; record which UA/client looks sharper |
| 8 | GeForce NOW web in Chrome (ChromeOS UA) and GeForce Infinity | does the service accept Linux arm64 at all? |
| 9 | With Steam running: launch Dolphin from SparkHome | exactly one controller input source (no Steam Desktop Layout keys) |
| 10 | Driver-update drill: compare `/sys/module/nvidia/version` vs installed GL extension | warning tile appears on mismatch |
| 11 | Steam forced-exit: SIGTERM tracked PIDs for DOS2 | `Remove 435150 from running list` logged; Steam stays healthy |

---

## 11. Open questions, unknowns, obstacles hit

- **No emulator was run on the Spark** (authority: research only, and Big Picture was on the TV). Every "expected" figure
  except RPCS3/xemu [R] is inferred.
- RPCS3/xemu GB10 numbers come from ETA Prime on an MSI EdgeXpert (same GB10), relayed by HotHardware/PCGH; PCGH notes
  occasional stutter [R]; native vs translated build not stated [R].
- Whether Chrome-on-Linux-arm64 is accepted by GeForce NOW, Luna and Boosteroid web clients: unknown.
- Whether Steam Remote Play client works inside the snap (x86 under FEX): unknown; Valve's native ARM64 client's decode path
  on NVIDIA: unknown.
- When (or whether) Canonical rebases the snap onto Valve's native ARM64 client now that Proton 11 is stable: unknown.
- ARMSX2 CLI flags and PS2 quality on GB10: unknown. Cemu arm64 Linux from source: untested.
- SteamGridDB ToS text: not retrievable (JS-only page); RPCS3's own arm64 blog post returned HTTP 403 to the fetcher
  (summarised via It's FOSS instead).
- Steam Input double-input with emulators while Steam runs: reported elsewhere, untested here.
- Exact install paths of RetroArch cores inside the Flatpak and of the Factorio ARM64 binary inside its tarball: confirm on install.

---

## 12. Sources

Valve / Steam / Proton / Canonical
- https://discourse.ubuntu.com/t/canonical-s-steam-snap-is-now-stable-on-arm64/83664
- https://github.com/ValveSoftware/steam-for-linux/issues/13451 · /13544 · /13689 · /13288 · /4061
- https://client-update.steamstatic.com/steam_client_linuxarm64 · https://client-update.steamstatic.com/steam_client_steamdeck_stable_linuxarm64 · https://client-update.fastly.steamstatic.com/steam_client_publicbeta_linuxarm64 · https://repo.steampowered.com/steamrt3c/images/latest-public-beta/steam-runtime-steamrt-arm64.tar.xz
- https://github.com/Mr-Banana-Egg/steam-arm64-nix
- https://github.com/ValveSoftware/Proton/releases
- https://github.com/ValveSoftware/wine (dlls/winex11.drv/window.c, `steam_app_%s`)
- https://en.wikipedia.org/wiki/Steam_Frame
- https://www.gamingonlinux.com/2026/08/lepton-and-fex-get-prepared-for-the-steam-frame-release/
- https://www.gamingonlinux.com/2025/11/steamworks-sdk-adds-support-for-linux-arm-and-android-and-it-seems-we-know-the-first-android-game-on-steam/
- https://www.gamingonlinux.com/2026/10/theres-now-over-200-steam-frame-verified-games/
- https://factorio.com/blog/post/fff-446 · https://factorio.com/download/experimental

Flathub / packaging
- https://flathub.org/api/v2/summary/<app-id> · https://flathub.org/api/v2/appstream/<app-id> · https://flathub.org/api/v2/eol/message · https://flathub.org/api/v2/eol/rebase
- https://github.com/flathub/org.freedesktop.Platform.GL.nvidia (versions.sh, data/nvidia-580.173.02-aarch64.data, flathub.json)
- https://docs.flatpak.org/en/latest/flatpak-command-reference.html · https://github.com/flatpak/flatpak/blob/main/common/flatpak-run.c
- https://api.launchpad.net/1.0/~libretro/+archive/ubuntu/stable (noble arm64 binaries)

Emulators
- https://buildbot.libretro.com/nightly/linux/aarch64/latest/ · https://docs.libretro.com/development/retroarch/network-control-interface/ · https://docs.libretro.com/guides/roms-playlists-thumbnails/
- https://github.com/dolphin-emu/dolphin (Source/Core/UICommon/CommandLineParse.cpp)
- https://github.com/PCSX2/pcsx2/releases · https://github.com/ARMSX2/ARMSX2 · https://www.gamingonlinux.com/2026/07/playstation-2-emulator-for-arm-devices-armsx2-gets-an-initial-linux-release/
- https://github.com/libretro/ps2 (issues/PRs 154, 170)
- https://github.com/RPCS3/rpcs3-binaries-linux-arm64/releases · https://github.com/RPCS3/rpcs3/issues/16484 · https://github.com/RPCS3/rpcs3/issues/16392 · https://rpcs3.net/blog/2024/12/09/introducing-rpcs3-for-arm64/ (via https://itsfoss.com/news/ps3-games-raspberrypi-5)
- https://hothardware.com/news/dgx-spark-gaming-tests · https://www.pcgameshardware.de/Kuenstliche-Intelligenz-Hardware-279517/News/Nvidia-DGX-Spark-Gaming-Tests-Cyberpunk-2077-1485922/
- https://github.com/stenzek/duckstation (README, qthost.cpp) · https://github.com/hrydgard/ppsspp/releases · https://github.com/azahar-emu/azahar
- https://github.com/cemu-project/Cemu/pull/1556 · https://github.com/cemu-project/Cemu/releases
- https://github.com/xemu-project/xemu/releases · https://github.com/melonDS-emu/melonDS/releases · https://github.com/flyinghead/flycast/releases
- https://github.com/shadps4-emu/shadPS4/releases · https://github.com/Vita3K/Vita3K/releases/tag/continuous
- https://github.com/github/dmca/blob/master/2026/02/2026-02-12-nintendo.md
- https://gitlab.com/es-de/emulationstation-de/-/releases · https://github.com/mmatyas/pegasus-frontend

Native games
- https://github.com/PrismLauncher/PrismLauncher/releases · https://meta.prismlauncher.org/v1/org.lwjgl3/index.json · https://piston-meta.mojang.com/mc/game/version_manifest_v2.json
- https://github.com/UZDoom/UZDoom/releases · https://zdoom.org/downloads
- https://github.com/Heroic-Games-Launcher/HeroicGamesLauncher/issues/4611 · https://github.com/Heroic-Games-Launcher/HeroicGamesLauncher/releases
- https://daniel.lawrence.lu/blog/2026-07-15-dgx-spark-as-daily-driver/

Cloud / streaming
- https://www.nvidia.com/en-us/geforce-now/download/ · https://www.nvidia.com/en-us/geforce-now/system-reqs/ · https://international.download.nvidia.com/GFNLinux/flatpak/geforcenow.flatpakrepo
- https://www.gamingonlinux.com/2026/08/nvidia-geforce-now-cloud-gaming-for-linux-levels-up-and-leaves-beta/
- https://github.com/OpenCloudGaming/OpenNOW · https://github.com/AstralVixen/GeForce-Infinity
- https://news.xbox.com/en-us/2026/09/03/xbox-cloud-gaming-changes/ · https://www.gamingonlinux.com/2022/11/microsoft-upgrades-xbox-cloud-gaming-for-linux-and-chromeos/ · https://github.com/redphx/better-xcloud/wiki/User%E2%80%90Agent
- https://github.com/unknownskl/greenlight · https://github.com/Geocld/XStreamingDesktop
- https://en.wikipedia.org/wiki/Amazon_Luna · https://help.boosteroid.com/en/content/general-requirements-for-your-digital-environment
- https://github.com/moonlight-stream/moonlight-qt/releases/tag/v6.2.0 · https://github.com/moonlight-stream/moonlight-qt/releases/tag/v6.0.0 · https://github.com/moonlight-stream/moonlight-docs/wiki/Installing-Moonlight-Qt-on-ARM%E2%80%90based-Single-Board-Computers
- https://github.com/chiaki-ng/chiaki-ng/releases

Art
- https://github.com/SteamGridDB/node-steamgriddb (README, src/index.ts) · https://www.steamgriddb.com/profile/preferences

Sibling reports: `ui-session.md` (§0, §C.4 focus, §C.5 Guide/evdev, §C.8 Steam Input, §C.9 SIGSTOP, §C.10 session design),
`spark-lab.md` (§7 Steam library art measurements). GameSpark repo: `lib/env.sh` (`steam_launch`, `fast_cpus`),
`tools/steam-console.sh`, `tools/xinput.py`, `docs/FINDINGS.md`.

Raw query outputs kept in `research/games-data/` (Flathub arches/EOL JSON, GeForce NOW repo summary, libretro core list,
GitHub release asset lists, helper scripts `fh_arches.sh`, `fh_search.py`, `gh_rel.sh`).
