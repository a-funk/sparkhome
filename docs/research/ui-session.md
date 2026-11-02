# SparkHome — tvOS Home Screen spec, launcher prior art, and the GNOME/Xorg session design

Research for the SparkHome build: an Apple TV-style launcher on the user's NVIDIA DGX Spark (`spark`, DGX OS 7.2.3 = Ubuntu 24.04.4 arm64, GNOME on Xorg, Sony TV on HDMI-0 at 1920×1080@60).
Date: 2026-10-07/08 (US Pacific). Author: UI/session research subagent.

It covers three parts:

- **(A)** A cited design spec of the tvOS Home Screen (tvOS 18 and tvOS 26 "Liquid Glass") for an HTML/CSS recreation at 1920×1080.
- **(B)** Prior art: Linux 10-foot launchers, web spatial-navigation libraries, and 60 fps practice in Chromium.
- **(C)** Session, window and input management on GNOME (mutter 46.2) on Xorg with the NVIDIA proprietary driver and GDM autologin.

A sibling lab report, `spark-lab.md`, covers Widevine, CDP control, the local agent model and voice. This report does not repeat those.

### Evidence labels

| label | meaning |
|---|---|
| **[M]** | Measured on the Spark during this research. All commands were read-only over `ssh spark` and are listed in Appendix A. |
| **[M-prov]** | Measured earlier and provided by the orchestrator. |
| **[D]** | Documented by a primary source: vendor documentation, or source code at a cited tag. |
| **[R]** | Reported by a secondary source, such as press, forums or third-party docs. |
| **[I]** | My inference. Verify it before relying on it. |

Unknowns are stated as unknown. No configuration was changed on the Spark, and nothing was installed.

---

## 0. Decisions in one page

1. **Build the shell as a local web app shown by Chrome 155 in `--kiosk`, with a small local backend.** The backend does Steam launches, X11 window activation, Home-button watching and the agent API.
   - The tvOS look depends on compositor-friendly 3D transforms, blur materials and video, which Chromium does well.
   - The agent harness gets a DOM, ARIA and CDP (screenshots, trusted key events) for free. The sibling lab measured `isTrusted:true` keys over CDP.
   - No existing launcher combines arm64 + X11/NVIDIA + a tvOS look + an agent API (§B). [I, from B]
2. **Start both processes as `systemd --user` services bound to `graphical-session.target`.**
   - The user manager already holds `DISPLAY=:1` and `XAUTHORITY=/run/user/1000/gdm/Xauthority` [M], because gnome-session uploads its environment [D].
   - **Delete only** `~/.config/autostart/gamespark-console.desktop`. GameSpark's `console-mode.sh disable` also restores the old GDM config, which turns autologin off.
3. **Never pin the kiosk "always on top". Move focus explicitly.**
   - mutter 46.2 honours `_NET_ACTIVE_WINDOW` with timestamp 0 unconditionally, unless GNOME Shell holds a stage grab [D: source].
   - After a launch, activate the window whose `WM_CLASS` is `steam_app_<appid>`. This was measured for DOS2 on this box [M].
   - On Home, activate the kiosk.
   - GameSpark already ships this call in `tools/xinput.py activate` [M].
3a. **Track the game lifecycle from Steam's log.** `console_log.txt` prints `Game process added/updated/removed: AppID N` [M].
4. **Watch for Home/Guide with an evdev reader, not X key grabs.**
   - Joystick nodes already carry a `user:spark` ACL (uaccess) [M].
   - Keyboard-type remotes do not [M]. Each one needs a udev rule numbered below 73 [M/D].
   - Remap remote keys with hwdb so they never trigger GNOME or Chrome accelerators. XF86Search opens GNOME search, XF86HomePage triggers Chrome Home, XF86Back triggers Chrome Back, and F13 maps to XF86Tools, which opens GNOME Settings [M/D].
5. **Harden the desktop for TV use.**
   - Turn off banners, the lock screen, the Super key, update and report popups, and the Evolution alarm and DejaDup notifiers.
   - Run Chrome with `--password-store=basic`. Autologin leaves the GNOME keyring locked. Autologin has not happened yet on this box, so this prompt has never been seen [M-sibling].
6. **Use the tvOS numbers in §A.**
   - Where Apple publishes nothing (icon corner radius, exact focus scale, shadow, durations), use the labelled estimates.
   - Calibrate them against a 1080p capture of a real Apple TV 4K running tvOS 26 or 27.

---

## Part A — Apple TV Home Screen design spec (tvOS 18 → tvOS 26/27)

Primary sources are the Apple HIG pages. They render client-side, so I read them through Apple's DocC JSON endpoints: `https://developer.apple.com/tutorials/data/design/human-interface-guidelines/<page>.json`. A converter is at `…/scratchpad/scripts/docc2txt.py` (usage: `python3 -I docc2txt.py page.json`). Older revisions came from the Wayback Machine.

### A.1 Canvas, units, safe area

| item | value | label / source |
|---|---|---|
| Asset scales | tvOS uses @1x and @2x | [D] HIG Images |
| HD canvas | 1920×1080 pt at @1x. 1 pt = 1 CSS px at devicePixelRatio 1 | [D] HIG Images + [I] |
| The Spark's TV | 1920×1080@60. EDID lists 1080p modes only [M-prov]. Xorg screen 1920×1080, HDMI-0 modes 60.00*, 59.94, 23.98 [M] | |
| Safe area | Inset primary content **60 pt top/bottom and 80 pt left/right**, for overscan and TV compatibility settings | [D] HIG Layout › tvOS |
| Viewing distance | "often 8 feet or more" | [D] HIG Designing for tvOS |
| Focus padding | Focused elements grow, so leave room so they do not overlap important information | [D] HIG Layout |

### A.2 Home Screen grid

- **Icons per row:** six in the top row by default; five when the appearance option "Increase the size of onscreen items" is set to **Large** [D: Apple Support, "Change appearance settings on Apple TV 4K": "When Large is selected, the top row of the Home Screen shows five icons instead of six"]. tvOS 17 moved the row from five to six [R: AppleInsider tvOS 17 overview; Sound & Vision].
- **HIG grid table** [D: HIG Layout › tvOS › Grids]:

| columns | unfocused content width | horizontal spacing | min vertical spacing |
|---|---|---|---|
| 5 | 320 pt | 40 pt | 100 pt |
| **6** | **260 pt** | 40 pt | 100 pt |
| 7 / 8 / 9 | 217 / 184 / 160 pt | 40 pt | 100 pt |

  Check: 6×260 + 5×40 = 1760 = 1920 − 2×80, and 5×320 + 4×40 = 1760. The grid fills the safe width exactly (computed).
- **Icon aspect:** 5:3. The asset is 400×240 @1x / 800×480 @2x [D]. A six-column icon is therefore **260×156** and a five-column icon **320×192** [I].
- **Row pitch:** 156 + 100 = **256 px** for six columns, 292 px for five [I]. The extra 100 also holds the app name, which appears only under the focused icon [I: observed tvOS behaviour; not stated in the HIG].
- HIG guidance [D]: keep spacing consistent; add extra vertical space for titled rows; keep partly hidden off-screen content symmetrical on both sides.

### A.3 App icons and layered images

| item | value | label / source |
|---|---|---|
| Home Screen asset | 400×240 @1x, 800×480 @2x. App Store icon 1280×768 | [D] HIG App icons (archived 2024-02-23). The current HIG (change log June 8, 2026) lists an 800×480 px layout size |
| Legacy focus geometry | Actual 400×240; safe zone 370×222; **unfocused 300×180; focused 370×222** (focus scale **≈1.233**) | [D-secondary] Microsoft Xamarin tvOS docs, which reproduce Apple's 2015–17 tvOS HIG |
| Shape | Rectangle with system-applied rounded corners ("concentric edges"). **Do not bake in rounded corners**; the system masks the layers, and pre-masked layers break the specular highlights | [D] HIG App icons |
| Corner radius | **Not published (unknown).** Start at about 8–10 % of icon height (12–16 px at 156 px) and calibrate | [I] |
| Layers | 2–5 layers, with an opaque background layer. Upper layers scale and shift more and are cropped more, so keep a safe zone. Keep text on the top layer | [D] HIG Images › Layered images; HIG App icons |
| tvOS 26 icons | "Bringing depth and detail to the edge of every icon" (Liquid Glass edge highlight) | [D] Apple Support, "What's new in tvOS 26"; [R] AppleInsider, MacStories ("specular highlights", "subtle three-dimensionality … app icon border") |

### A.4 Focus states and the focus effect

**States** [D: HIG Focus and selection › tvOS]. tvOS has up to five visually distinct states:

| state | look (HIG wording, condensed) |
|---|---|
| Unfocused | Less prominent. Small drop shadow, appears very close to the content behind it |
| **Focused** | "Elevation to the foreground, illumination, and animation". Larger, with a drop shadow that makes it appear farther away |
| Highlighted (pressed) | Instant feedback. **Same size as unfocused**, shadow a little farther away; a button may briefly invert its colours |
| Selected | Chosen or activated (for example, a filled heart) |
| Unavailable | No shadow, rests directly on the content, low-contrast text |

**Parallax** [D: HIG Images › tvOS]: "As an element comes into focus, the system elevates it to the foreground, gently swaying it while applying illumination that makes the element's surface appear to shine. **After a period of inactivity, out-of-focus content dims and the focused element expands.**"

**Numbers.** Apple publishes no constants for scale, tilt, shadow or timing:

| parameter | best available number | label |
|---|---|---|
| Focus scale (icons) | Legacy 300→370 = **×1.233**. Top Shelf items 500→570 = ×1.14 and 782→852 = ×1.09 | [D] Xamarin and HIG Top Shelf sizes (computed) |
| Tilt | ±**0.05 rad (≈2.9°)** about X and Y, perspective `m34 = 1/500` (CSS `perspective: 500px`) | [D] react-native-tvos 0.86.0-2 `RCTTVView.m`. This is the defaults of RN-tvOS's emulation of the tvOS parallax for custom views, not Apple's constants |
| Shift | ±**2 pt** in x and y; magnification 1.0; pressDuration 0.3 s | [D] same source |
| Shine / specular | atvImg (2015 web recreation): white linear gradient up to α 0.4, rotated with the pointer; scale 1.07; perspective = 3 × element width | [D] github.com/drewwilson/atvImg source |
| Shadow | atvImg focused: `0 45px 100px rgba(14,21,47,.4), 0 16px 40px rgba(14,21,47,.4)` | [D] same |
| Timing | Apple: "focusing animations typically run faster than unfocusing animations, to create a trail effect" and "when focus is moving quickly, the timing of the animations are sped up". Never assume a fixed duration | [D] UIKit `UIFocusAnimationCoordinator` |
| Focus sound | tvOS plays a focus-movement sound. WWDC17 session 224 covers custom focus sounds | [R] wwdcnotes index; [I] supply your own short tick |

**Starting values for SparkHome** [I — calibrate them]:

| parameter | value |
|---|---|
| Icon focus scale | 1.20 (range 1.15–1.23) |
| Focus-in | 160 ms, ease-out |
| Focus-out | 280 ms |
| Key-repeat | Durations collapse to about 90 ms |
| Max tilt | 3° |
| Layer shift | 2 px per depth step |
| Shadow | Pre-rendered pseudo-element; animate its opacity |
| Specular | Pseudo-element radial gradient, α ≤ 0.35 |
| Idle "sway" | ±1.5° over a 4–6 s loop |
| Idle dim | Unfocused tiles dim after about 5 s of no input. Use one black overlay animated in opacity (to about 0.4), not `filter: brightness()` |

### A.5 Focus engine behaviour to replicate

- **Directional search** [D: UIKit "About focus interactions for Apple TV"]: focus moves to "the next focusable item in the direction". If none is found, focus stays put (`movementDidFailNotification`). Only one item has focus. Only the user moves focus directionally; the app redirects through preferred focus environments.
- **No implicit wrap** [I]: this follows from "stays in focus" when nothing lies in that direction.
- **Gaps and rows of different widths** [D: SwiftUI `focusSection()`]: if nothing is directly in the direction of travel, default movement does nothing.
  - A **focus section** (or a UIKit `UIFocusGuide`) makes a container accept focus and hand it to its nearest focusable descendant in the direction of travel.
  - Apple's example: buttons at the upper left cannot reach buttons at the lower right without a section.
- **Geometry** [R: Apple Developer Forums threads 772417 and 772210]: the engine measures from the **centre** of the focused view. When the item above spans the full width, Down lands on the **centre** item of the next row.
- **Row memory** [D: `UICollectionView.remembersLastFocusedIndexPath`, default false]: when enabled, focus re-entering a collection returns to the last focused index path. The Apple TV app's shelves behave like this.
- **The Home grid is uniform**, so Up/Down keeps the column. If the row below is shorter, the geometrically nearest item wins [I].

**Web algorithm.** Norigin Spatial Navigation core 4.1.2 [D: source]:

1. Keep candidates that lie in the direction of travel.
2. Call a candidate "adjacent" if its overlap on the orthogonal axis is at least 20 % of the reference size.
3. Score `total = primary×5 + secondary`, then `priority = (total+1)/(adjacent ? 5 : 1)`. For non-adjacent candidates the two axes are swapped. Lowest priority wins.
4. Default distance basis is `corners`, with `center` and `edges` also available.
5. Add per-row "last focused" memory and section boundaries.

A 60-line custom engine with these rules is enough for a home grid plus horizontal shelves [I].

### A.6 Top Shelf and the Dock

- **Terms** [D: HIG Top Shelf]: the top row of apps is the **Dock**. **Top Shelf** is the area above it, which shows the focused Dock app's content.
- **Full-screen behaviour** [D]: "when people select Apple TV in the Dock, full-screen previews immediately begin playing and soon the Dock slides away". People swipe through the previews and pick **Play** or **More Info**.
- **Templates** [D]:

| template | description |
|---|---|
| Carousel actions | Full-screen video or images, a title and optional subtitle, and two buttons (Play, More Info) |
| Carousel details | Adds a summary, cast and metadata. The title sits near the top, with an optional context phrase above it |
| Sectioned content row | One focusable row. A label appears on focus, and small touch movements animate the image |
| Scrolling inset banner | 3–8 near-full-width banners that auto-scroll on a timer until focused |

- **Carousel item** [D: TVServices `TVTopShelfCarouselItem`]: shows a static image, or a silent looping `cinemagraphURL` while it loads. It plays `previewVideoURL` when focus stops on it.
- **Sizes** [D: HIG Top Shelf, 2026]:

| asset | actual | focused / safe | unfocused |
|---|---|---|---|
| Static fallback | **2320×720 pt**. tvOS "flips and blurs the image" to fill 1920 px at 16:9 | — | — |
| Poster 2:3 | 404×608 | 380×570 | 333×570 as printed (the legacy spec says **333×500**, which fits 2:3; treat 570 as a typo) |
| Square 1:1 | 608×608 | 570×570 | 500×500 |
| 16:9 | 908×512 | 852×479 | 782×440 |
| Inset banner | 1940×692 (legacy 1940×624) | 1740×620 | 1740×560 |

  Mixed rows scale up to the tallest item; for example, 16:9 items become 500 high next to posters [D].
- **Vertical layout at 1080p** [I — calibrate]:
  - Top Shelf art runs full-bleed behind, from y = 0 to about 700.
  - The Dock row's icons start at about y = 720 (260×156 + name label).
  - The next row's icons peek in from about y = 980.
  - Focusing any lower row scrolls the page so the Top Shelf leaves the screen.
- **tvOS 26** [D: HIG Materials › tvOS]: "Liquid Glass appears throughout navigation elements and system experiences such as **Top Shelf and Control Center**. Certain interface elements, like image views and buttons, adopt Liquid Glass when they gain focus." Press reports describe a see-through Dock [R: AppleInsider].

### A.7 Typography

- **System font** [D: HIG Typography]: SF Pro is the tvOS system font, and apps may also use NY. The tvOS **default size is 29 pt and the minimum 23 pt**.
- **tvOS text styles** [D: HIG Typography, table "tvOS built-in text styles"]:

| style | weight | size | leading | emphasized |
|---|---|---|---|---|
| Title 1 | Medium | 76 | 96 | Bold |
| Title 2 | Medium | 57 | 66 | Bold |
| Title 3 | Medium | 48 | 56 | Bold |
| Headline | Medium | 38 | 46 | Bold |
| Subtitle 1 | Regular | 38 | 46 | Medium |
| Callout | Medium | 31 | 38 | Bold |
| Body | Medium | 29 | 36 | Bold |
| Caption 1 | Medium | 25 | 32 | Bold |
| Caption 2 | Medium | 23 | 30 | Bold |

- **tvOS tracking** [D: same page, excerpt]:

| size (pt) | tracking (1/1000 em) |
|---|---|
| 23 | −4 |
| 25 | +6 |
| 29 | +14 |
| 31 | +13 |
| 38 | +10 |
| 48 | +8 |

- **Do not ship SF Pro.** Its licence limits use to mock-ups of UIs for software running on Apple operating systems, and forbids bundling [D/R: developer.apple.com/fonts licence, as quoted on Apple Developer Forums, e.g. thread 733267].
- **Fallback: Inter 4.1** (SIL OFL 1.1) [D: rsms.me/inter]. It has an optical-size axis from "text" to "display". Use display optical sizing at 38 pt and above, `font-feature-settings:"tnum"` for clocks, and tracking roughly per the table [I].

### A.8 Colour and materials

- **Dark mode** is the TV norm. The DGX OS session already uses `color-scheme 'prefer-dark'` through an NVIDIA gsettings override [M]. On tvOS, avoid using colour alone to show focus; scale and animation are primary [D: HIG Color › tvOS].
- **System colours (2025+ palette), dark-mode values** [D: HIG Color › Specifications]:

| colour | RGB |
|---|---|
| red | 255,66,69 |
| orange | 255,146,48 |
| yellow | 255,214,0 |
| green | 48,209,88 |
| mint | 0,218,195 |
| teal | 0,210,224 |
| cyan | 60,211,254 |
| blue | 0,145,255 |
| indigo | 109,124,255 |
| purple | 219,52,242 |
| pink | 255,55,95 |
| brown | 183,138,102 |

- **Standard materials on tvOS** [D: HIG Materials › tvOS]:

| material | use |
|---|---|
| ultraThin | Full-screen views needing a light scheme |
| thin | Overlays needing a light scheme |
| regular | Overlays |
| thick | Overlays needing a dark scheme |

- **Liquid Glass** [D: HIG Materials]:
  - **Regular** blurs and adjusts luminosity; use it for text-heavy elements.
  - **Clear** is highly translucent; use it over rich media.
  - Under clear glass on bright content, "consider adding a dark dimming layer of **35 % opacity**".
  - Don't use Liquid Glass in the content layer, and use it sparingly.
- **Web approximation** [R: LogRocket, ekino/Medium, buildmvpfast 2026]:
  - Regular glass: `backdrop-filter: blur() saturate()` plus a 1 px inner highlight.
  - Refraction: `backdrop-filter: url(#svgFilter)` with `feDisplacementMap`, which works only in Chromium.
  - Refraction is costly: keep it to small elements (the Dock pill, focused tile rim), use at most 3–4 glass layers, and never animate blur radius.
  - For big areas, use pre-blurred static images [I].

### A.9 tvOS 26 and 27 — what changed

| item | detail | label / source |
|---|---|---|
| tvOS 26 design | New Liquid Glass design, "real-time rendering", focus on what's playing | [D] Apple Newsroom 2025-06-09 |
| Control Center and playback | Redesigned | [D] same |
| Profiles | Picker can appear when Apple TV wakes | [D] same; Apple Support "What's new" |
| Hardware | Apple TV 4K (2nd gen) and later | [D] same |
| Release | 2025-09-15 | [R] AppleInsider |
| Extent of change | Restrained. Glass mostly in Control Center, player controls, the Dock and focused elements | [R] PCWorld, MacStories |
| tvOS 27 | Released 2026-09-14. No Home Screen redesign reported. Notes cover AutoMix, the Podcasts redesign and Dynamic Text | [R] MacRumors 2026-09-25; BGR |

### A.10 Tokens and a CSS sketch (calibrate before shipping) [I]

```css
:root{
  --safe-x:80px; --safe-y:60px;                      /* [D] HIG Layout */
  --cols:6; --tile-w:260px; --tile-h:156px; --gap:40px; --row-pitch:256px; /* [D]+[I] */
  --radius:14px;                                     /* [I] unknown — calibrate */
  --focus-scale:1.2;                                 /* [I] legacy spec 1.233 */
  --t-in:160ms; --t-out:280ms; --ease:cubic-bezier(.2,.8,.2,1);   /* [I] in < out per Apple */
  --bg:#000; --label:#fff; --label-2:rgba(235,235,245,.6);
}
.tile{position:relative;width:var(--tile-w);height:var(--tile-h);border-radius:var(--radius);
  transform:perspective(500px) rotateX(var(--rx,0deg)) rotateY(var(--ry,0deg)) scale(var(--s,1));
  transition:transform var(--t-out) var(--ease)}
.tile::before{content:"";position:absolute;inset:0;border-radius:inherit;z-index:-1;   /* shadow: opacity only */
  box-shadow:0 22px 44px rgba(0,0,0,.55);opacity:0;transition:opacity var(--t-out) var(--ease)}
.tile::after{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;  /* specular */
  background:radial-gradient(120% 90% at var(--sx,50%) var(--sy,0%),rgba(255,255,255,.35),transparent 60%);
  opacity:0;transition:opacity var(--t-out)}
.tile.focused{--s:var(--focus-scale);z-index:2;transition-duration:var(--t-in)}
.tile.focused::before,.tile.focused::after{opacity:1;transition-duration:var(--t-in)}
.tile > img{width:100%;height:100%;border-radius:inherit;object-fit:cover}
```

- Drive `--rx/--ry/--sx/--sy` from a slow idle sway or the right stick, clamped to ±3°.
- Change only `transform` and `opacity` during focus moves (§B.5).

### A.11 Calibration gate for Part A

1. Capture a tvOS 26/27 Home Screen at 1080p, ideally with screen recording through Xcode's Devices window on the user's own Apple TV 4K.
2. Overlay it on SparkHome's render at 50 % opacity.
3. Tune radius, scale, shadow, label size and Top Shelf height.
4. Measure the focus durations frame by frame from a 60 fps recording.

---

## Part B — Prior art

### B.1 Launchers on Linux arm64 + NVIDIA proprietary + Xorg

| project | latest (as of 2026-10) | arm64 build | runs on this stack? | tvOS-look fit | agent / API | reuse |
|---|---|---|---|---|---|---|
| **Flex Launcher** (C, SDL2) | v2.2, 2024-11-24; repo pushed 2026-07 [D: GitHub] | `flex-launcher_2.2_arm64.deb` (made for Raspberry Pi; Debian Bullseye+) [D] | Likely: SDL2 on X11/GLX [I] | Low: flat tiles from an INI config, no parallax or Top Shelf. "Mature, no feature releases planned" [D] | none | Config-driven app list; remotes = OS keyboard events (no IR/CEC decoding) [D] |
| big-launcher (same author, C++) | WIP, pushed 2025-08 [D] | no releases | unknown | aims for a "Smart TV-like" UI [D] | none | watch only |
| **KDE Plasma Bigscreen** | Back in Plasma **6.7** (stable 2026-06-16); Qt6/KF6 port; Mycroft voice removed [R: Phoronix, OMG! Ubuntu, Arch packages 6.7.x] | via distros | **No** on DGX OS 7: Ubuntu 24.04 ships Plasma **5.27.11** [D: packages.ubuntu.com], and Bigscreen 6.7 needs a Plasma 6.7 session (KWin, most likely Wayland [I]) | Medium (Android-TV-like) | KDE APIs; no agent API | design reference only |
| **OpenGamepadUI** (Godot 4, GPL-3) | v0.46.1, 2026-09-03 [D] | aarch64 tar.gz / rpm / sysext [D] | Unproven under GNOME/X11. Its dedicated sessions are gamescope-based, and input remapping and power tools go through the InputPlumber and PowerStation daemons [D: README, docs]; these run as system services [I] | Low (Steam-Deck style) | plugin system | input/overlay ideas |
| **Kodi** | 21.3 Omega stable; **22 "Piers" Beta 2** 2026-09-05 [R: kodi.tv]. Ubuntu 24.04 ships **20.5** [D] | `kodi_20.5+dfsg-1build2_arm64.deb` in noble [D] | Probably X11/GLX [I]. No VA-API driver on this box [M-prov] | Medium: skin "AppTV" mimics Apple TV (Omega) [R: kodi.wiki] | **JSON-RPC API** [D: kodi.wiki] | Good media centre to launch *as a tile* |
| **Steam Big Picture** | already in use; GameSpark console mode measured working [M-prov] | x86 client under FEX (snap) | yes | Steam's own look | Steam pipe / URLs | **game launcher backend** (pipe `-applaunch`) |
| **gamescope-session** | gamescope 3.16.15 aarch64 packaged by Arch Linux ARM [D] | **No Ubuntu noble package** (packages.ubuntu.com search: "no results") [D] | Would replace the GNOME session. NVIDIA 580-series crash reports with scaling filters [R: NVIDIA forum] | n/a | n/a | not recommended now |
| **Pegasus** (Qt5/QML) | "continuous" 2026-09-15, still alpha16-based [D] | x11-static (x86_64), RPi/Odroid static; no generic aarch64 desktop build [D] | would need a source build [I] | Medium (themes) | none | none |
| **ES-DE** | 3.4.1, 2026-04-10. Official but **experimental** Linux AArch64 AppImage [D: es-de.org; R: DroiX] | yes | likely [I] | Low (emulation frontend) | none | only if emulation is added |
| **Jellyfin Desktop** (formerly Jellyfin Media Player) | 2.0.0, Dec 2025; Qt6 + Qt WebEngine + libmpv [D] | **Flathub aarch64** [D: Flathub API] | likely [I] | n/a (client app) | MPRIS [D] | launch as a tile |
| **GNOME Kiosk** | 46.0 in noble; `gnome-kiosk-script-session` ships **Xorg and Wayland** session files [D: packages.ubuntu.com] | yes | yes [I]; a mutter-based single-app session | n/a (it's a session, not a UI) | n/a | Fallback session if GNOME Shell keeps interfering (§C.10) |

### B.2 Why a custom web shell, and when it would be wrong

**Why** [I, with the cited support]:

1. **Visual fidelity.** The tvOS look needs per-tile 3D tilt, specular gradients, blurred glass and video Top Shelf previews.
   - These map directly to CSS transforms, opacity, `backdrop-filter` and `<video>`.
   - None of the native launchers above ship that look on this stack.
2. **Agent harness.** A local tool-calling model needs structured state, and a computer-use model needs pixels and input.
   - Chrome gives structured state through the DOM, ARIA and the shell's own JSON focus graph.
   - It gives pixels and input through CDP `Page.captureScreenshot` and `Input.dispatchKeyEvent`, which the sibling lab measured delivering `isTrusted:true` keys.
   - Chrome 136+ honours `--remote-debugging-port` only with a non-default `--user-data-dir` [D: developer.chrome.com/blog/remote-debugging-port]. A dedicated SparkHome profile is needed anyway.
   - Kodi's JSON-RPC is the only comparable API among the native options [D].
3. **Streaming tiles are web apps.** They run in the same Chrome with Widevine L3 [M-sibling]. This keeps a single runtime.
4. **Proven pieces already exist.** GameSpark has `steam_launch` over the pipe, `xinput.py` (XTest plus EWMH activation) and `gamepad.py` (uinput) [M].

**When it would be wrong:**
- If GPU compositing in Chrome on this NVIDIA arm64 X11 stack turned out to be disabled or slow. This is **unverified**: headless runs used SwiftShader [M-sibling]. Gate: `chrome://gpu` in a headful window, then a 60 fps focus-move trace.
- If the user wanted a pure media-centre (Kodi) experience.

### B.3 Web spatial-navigation libraries

| library | status (GitHub, 2026-10) | notes |
|---|---|---|
| **Norigin Spatial Navigation** | Active. Core `@noriginmedia/norigin-spatial-navigation-core` 4.1.2 (2026-09-25); React package 3.3.0 (2026-07-30); MIT [D] | Framework-agnostic core. Options include `saveLastFocusedChild`, `preferredChildFocusKey`, `isFocusBoundary`, `autoRestoreFocus`, `forceFocus`, `useGetBoundingClientRect`, `distanceCalculationMethod` (corners/center/edges) and `throttle` [D: source]. Used on Tizen, webOS, VIDAA and Chromium STBs [D: README] |
| BBC **LRUD** | **Archived** (last push 2023-07) [D] | tree-based; don't adopt |
| **js-spatial-navigation** (luke-chang) | Low activity (pushed 2024-05), MPL-2.0 [D] | old, DOM-focus based |
| WICG **spatial-navigation** polyfill | **Archived** [D] | historical |
| Chromium `--enable-spatial-navigation` | switch exists in `content_switches.cc` [D] | moves DOM focus with arrow keys; no tvOS-style control |
| react-tv-space-navigation (BAM) | pushed 2025-10, MIT [D] | React/React Native TV |

**Recommendation** [I]:
- The Home grid plus shelves needs about 60 lines of custom code: the Norigin scoring from A.5 plus row memory.
- Adopt Norigin core only if nested sections, modals and virtualised lists multiply.
- Either way, keep your own focus model rather than DOM `:focus`, because the agent API needs to read and set it.
- If you measure with `getBoundingClientRect`, note that transforms scale it. Measure unscaled layout, e.g. `offset*` or Norigin's default `measureLayout`.

### B.4 Agent-harness notes from prior art

- **Kodi JSON-RPC** is the model [D]: `Input.Left/Right/Select/Back`, `GUI.ActivateWindow`, introspection.
- Expose the same for SparkHome: `state()` returns rows, tiles, focus and running app; then `move(dir)`, `select()`, `back()`, `home()`, `launch(tileId)`, `search(text)`.
- Screenshots and keys go over CDP for the computer-use path.
- Ground truth for "what is focused" comes from the shell's model, not from vision. Vision misplans moves [M-sibling].

### B.5 60 fps TV UI in Chromium — practices

| practice | detail | source |
|---|---|---|
| Animate only compositor-only properties | "only two properties … `transform`s and `opacity`." Never animate `box-shadow`, `filter: blur()`, `width/height/top/left` | [D] web.dev "Stick to compositor-only properties…" |
| Promote sparingly | Use `will-change: transform` on the few moving layers. "Every layer you create requires memory and management"; don't promote everything | [D] web.dev; MDN `will-change` ("last resort … toggle via script") |
| No layout thrash | Batch reads (`getBoundingClientRect`) before writes, use a cached geometry model for focus search, and never read layout inside rAF after writes | [D] web.dev "Avoid large, complex layouts and layout thrashing" |
| Small paint areas | Fixed-size tiles; `contain: layout paint` on rows | [D] web.dev "Simplify paint complexity…" |
| Off-screen rows | `content-visibility: auto` with `contain-intrinsic-size` on rows below the fold | [D] web.dev "content-visibility" |
| Decode images before they appear | `await img.decode()` (or `createImageBitmap`) before inserting a tile; pre-size images to display size (260×156 / 2× for 4K) | [D] MDN `HTMLImageElement.decode()` |
| Shadow and blur trick | Shadow as a pseudo-element with opacity animation; blur via pre-blurred art except for 1–2 small glass elements | [I] + [R] Liquid Glass write-ups |
| Video previews | Muted looping H.264/VP9 `<video>` at most 1080p30. Software decode only (no VA-API [M-prov]); the sibling lab measured MediaCapabilities `smooth=true powerEfficient=false` headless | [M-sibling] |
| Measure | Chrome DevTools Performance panel (frames, layers, paint flashing) via CDP `Tracing`; gate on 60 fps during repeated focus moves | [D] Chrome DevTools docs |
| Background cost | An occluded but not minimised Chrome window stays "visible" on Linux, so rAF and video keep running behind a game. **Iconify the kiosk while a game runs** | [D] Electron BrowserWindow "Page visibility" (Chromium-based) + [I] for Chrome itself |

---

## Part C — Session, window and input management on the Spark

### C.1 Measured session facts (2026-10-08, read-only)

| item | value |
|---|---|
| Versions [M] | GNOME Shell 46.0 (`gnome-shell 46.0-0ubuntu6~24.04.14`); **mutter 46.2** (`libmutter-14-0 46.2-1ubuntu0.24.04.16`); gnome-session 46.0; gdm3 46.2; systemd/udev 255.4; Xorg 21.1.12; Chrome 155.0.8059.39; ubuntu-dock 90ubuntu3; update-notifier and apport installed (apport `enabled=1`) |
| Session [M] | GNOME session is systemd-managed: `gnome-session-manager@ubuntu.service`, `org.gnome.Shell@x11.service` (`Type=notify`, `Before=gnome-session-initialized.target`). `gnome-session.target` `BindsTo=graphical-session.target` / `Before=graphical-session.target`, so graphical-session.target becomes active **after** gnome-shell has signalled readiness |
| User manager env [M] | `DISPLAY=:1`, `XAUTHORITY=/run/user/1000/gdm/Xauthority`, `XDG_SESSION_TYPE=x11`, `XDG_CURRENT_DESKTOP=ubuntu:GNOME` (`systemctl --user show-environment`) |
| How env gets there [D] | gnome-session 46.0 `main.c` calls `gsm_util_export_activation_environment()` (D-Bus) and `gsm_util_export_user_environment()`. The latter sends its whole environment to systemd's `UnsetAndSetEnvironment` (`gsm-util.c`; `DISPLAY`/`XAUTHORITY` are in the unset-then-set list) |
| Autostart in GNOME 46 [M/D] | XDG autostart entries are spawned by gnome-session-binary into `app-gnome-<name>-<pid>.scope` (e.g. `app-gnome-flow-6652.scope`), **not** via systemd's xdg-autostart generator. gnome-session honours `Hidden=true` and `X-GNOME-Autostart-enabled=false` (`gsm-autostart-app.c: is_disabled`) |
| User autostart [M] | a third-party dictation app (push-to-talk); `gamespark-console.desktop` (Steam BPM after 5 s) |
| Effective gsettings [M] (with `XDG_CURRENT_DESKTOP=ubuntu:GNOME`; without it the Ubuntu overrides don't apply) | `idle-delay 0`; `lock-enabled true`; `ubuntu-lock-on-suspend true`; `disable-lock-screen false`; **`show-banners true`**; `sleep-inactive-ac-type 'nothing'`; `idle-dim true`; `power-button-action 'interactive'`; `disable-overview-on-startup true` (Ubuntu override); `enable-hot-corners false`; **`overlay-key 'Super_L'`**; **`check-alive-timeout 5000`**; `focus-new-windows 'smart'`; `raise-on-click true`; `com.ubuntu.update-notifier no-show-notifications false`; `automount-open true`; `event-sounds true` |
| X server [M] | Screen saver timeout 0; DPMS enabled with standby/suspend/off = 0; XTEST, DPMS, Present, RANDR and XInputExtension present |
| GNOME static media keys [M] | XF86Search→search, XF86Tools→Settings, XF86Explorer→home folder, XF86WWW→browser, XF86Mail→email, XF86PowerOff→power, XF86Sleep→suspend, XF86AudioMedia→media player, XF86Calculator. **XF86HomePage and XF86Back are not bound by GNOME** |
| XKB mapping [M] (`/usr/share/X11/xkb/symbols/inet`) | KEY_HOMEPAGE→XF86HomePage, KEY_BACK→XF86Back, KEY_SEARCH→XF86Search, **KEY_F13→XF86Tools**, KEY_F14..F18→XF86Launch5..9, KEY_F20→XF86AudioMicMute, KEY_F21..23→touchpad toggles, KEY_PROG1/2→XF86Launch1/2 |
| uaccess rules [M] | `/usr/lib/udev/rules.d/70-uaccess.rules` tags **joysticks** (`SUBSYSTEM=="input", ENV{ID_INPUT_JOYSTICK}=="?*"`) and `ENV{ID_REMOTE_CONTROL}=="1"` devices, but **not keyboards or mice**. ACLs are applied by `73-seat-late.rules` (`TAG=="uaccess" … RUN{builtin}+="uaccess"`). `60-steam-input.rules` (from the steam-launcher package) adds uaccess to `/dev/uinput` and to Valve/Sony/Nintendo/etc. hidraw (Microsoft is absent) |
| Actual ACLs [M] | `event7`/`js0` (Xbox, 045e:0b13 BT) and `event8`/`js1` (DualSense) have `user:spark:rw-`; `/dev/uinput` has `user:spark:rw-`; **keyboard `event11–13` (SEM USB Keyboard + Consumer/System Control) and mouse `event14` have no ACL**; DualSense motion and touchpad nodes have no ACL |
| Xbox BT capabilities [M] | KEY bitmap covers BTN_SOUTH..BTN_THUMBR (304–318, including **BTN_MODE 316 = Guide**) and KEY_RECORD (167 = Share). Steam's own SDL mapping in `config.vdf` for `050000005e040000130b000009050000` reads `guide:b12, back:b10, start:b11` (joydev indices) |
| Running windows [M] | Proton game: `WM_CLASS "steam_app_435150"`, fullscreen+focused. Big Picture: `WM_CLASS "steamwebhelper","steam"`, title "Steam Big Picture Mode", fullscreen |
| Steam log [M] | `console_log.txt` lines: `Game process added : AppID 435150 …`, `Game process updated : …`, `Game process removed: AppID 435150 …` |
| Steam cgroup [M] | The current Steam instance runs in `/user.slice/user-1000.slice/session-1387.scope`, a login-session scope. This instance was started from an ssh session by other GameSpark work; started by the backend unit, it would live in that unit's cgroup [I]. Either way Steam and its games share one cgroup, so cgroup-freezing a game would freeze Steam too |
| Tools [M] | Present: `xprop`, `xwininfo`, `xset`, `xdpyinfo`, `getfacl`. Missing: `xdotool`, `wmctrl`, `glxinfo`. The sibling lab found `xdotool 1:3.20160805.1-5build1` and `wmctrl 1.07-7ubuntu3` available in noble/universe arm64 |

### C.2 Starting the kiosk: GNOME autostart vs a systemd user unit

| | GNOME autostart `.desktop` | **systemd --user unit (recommended)** |
|---|---|---|
| Env (DISPLAY/XAUTHORITY) | inherited from gnome-session | from the user manager, already populated [M/D] |
| Ordering | `X-GNOME-Autostart-Phase` / `-Delay` | `After=graphical-session.target`. Shell is ready by then [M: unit graph] |
| Restart on crash | `X-GNOME-AutoRestart` exists, but its semantics involve session-client registration (`gsm-manager.c`) [D] | `Restart=always`, `RestartSec=` |
| Logs | none by default | `journalctl --user -u sparkhome-kiosk` |
| Stops with session | yes | `PartOf=graphical-session.target` (as systemd.special(7) prescribes) [D] |
| Proven here | yes (GameSpark console mode) | not yet; gate below |

Unit files [I — the build agent writes the final versions]:

```ini
# ~/.config/systemd/user/sparkhome-backend.service
[Unit]
Description=SparkHome backend (UI server, Steam launcher, window + input control, agent API)
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart=/usr/local/bin/node %h/sparkhome/backend/server.mjs
Restart=always
RestartSec=1

[Install]
WantedBy=graphical-session.target
```

```ini
# ~/.config/systemd/user/sparkhome-kiosk.service
[Unit]
Description=SparkHome kiosk (Chrome)
PartOf=graphical-session.target
After=graphical-session.target sparkhome-backend.service
Wants=sparkhome-backend.service

[Service]
ExecStart=/usr/bin/google-chrome-stable --kiosk --class=SparkHome \
  --user-data-dir=%h/.local/share/sparkhome/chrome \
  --no-first-run --no-default-browser-check --hide-crash-restore-bubble --noerrdialogs \
  --password-store=basic --autoplay-policy=no-user-gesture-required \
  --remote-debugging-port=9222 http://127.0.0.1:8730/
Restart=always
RestartSec=2

[Install]
WantedBy=graphical-session.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now sparkhome-backend.service sparkhome-kiosk.service
rm -f ~/.config/autostart/gamespark-console.desktop   # NOT console-mode.sh disable (that also turns autologin off)
```

All switches above are verified in Chromium source [D]:

| switch | defined in |
|---|---|
| `kiosk` | `chrome_switches.h` `kKioskMode` |
| `class` | `kWmClass`: "Overrides the WM_CLASS window property" |
| `hide-crash-restore-bubble`, `no-first-run`, `no-default-browser-check`, `user-data-dir`, `app` | `chrome_switches.h` |
| `noerrdialogs` | `base_switches.h` |
| `password-store` | `password_manager_switches.cc` (values: basic, gnome-libsecret, kwallet*) |
| `autoplay-policy=no-user-gesture-required` | `media_switches.cc` |
| `remote-debugging-port` | non-default `--user-data-dir` required since Chrome 136 |

`--class=SparkHome` gives the window a stable `WM_CLASS` for activation.

**Evidence gate:**
1. Reboot once into autologin (never exercised yet [M-sibling]).
2. Confirm `loginctl show-session` reports `Service=gdm-autologin`.
3. Confirm the kiosk window maps within N seconds with `xprop WM_CLASS == SparkHome`.
4. Confirm no keyring prompt appears.
5. Confirm `journalctl --user -u sparkhome-kiosk` is clean.

### C.3 Removing desktop distractions (each change needs user approval)

```sh
# Lock, idle and power
gsettings set org.gnome.desktop.screensaver lock-enabled false
gsettings set org.gnome.desktop.screensaver ubuntu-lock-on-suspend false
gsettings set org.gnome.desktop.lockdown disable-lock-screen true
gsettings set org.gnome.settings-daemon.plugins.power idle-dim false
# Leave power-button-action at 'interactive' [M]: gsd-power applies it to EVERY power key, including the
# Spark's own ACPI button. Neutralise only the remote's power key with hwdb (C.6).
# Shell UI that can cover the kiosk
gsettings set org.gnome.desktop.notifications show-banners false
gsettings set org.gnome.mutter overlay-key ''            # empty = Super no longer opens the Overview [R/I]; the Overview also blocks activation (C.4)
gsettings set org.gnome.desktop.media-handling automount-open false
gsettings set org.gnome.desktop.sound event-sounds false
gsettings set com.ubuntu.update-notifier no-show-notifications true
# Only if the SIGSTOP pause (C.9) is used:
gsettings set org.gnome.mutter check-alive-timeout 0     # [D] schema: "Using 0 will disable the alive check completely"
```

- Already suitable [M]: `idle-delay 0`, `sleep-inactive-ac-type 'nothing'`, `enable-hot-corners false`, `disable-overview-on-startup true`, X DPMS timers 0.

Disable pop-up windows from system autostart entries. Use a user override with `Hidden=true`, which gnome-session honours [D]; no root is needed:

```sh
for f in update-notifier ubuntu-advantage-notification ubuntu-report-on-upgrade org.gnome.DejaDup.Monitor org.gnome.Evolution-alarm-notify; do
  printf '[Desktop Entry]\nType=Application\nName=%s\nHidden=true\n' "$f" > ~/.config/autostart/$f.desktop
done
```

- **apport.** Crash dialogs come from apport (`enabled=1` [M]) and the failed user unit `update-notifier-crash` [M-sibling]. Disabling them needs root: `/etc/default/apport` `enabled=0`.
- **GNOME keyring.** With autologin the login keyring stays locked. Chrome's default libsecret store would raise a Shell modal prompt, which also blocks window activation (C.4). `--password-store=basic` avoids it [D switch; I behaviour]. Steam's webhelper already runs with `--password-store=basic` [M: process args].
- **Desktop flash at login** (optional) [I]: set a black background (`org.gnome.desktop.background picture-uri ''`, `primary-color '#000000'`), or accept a 1–2 s flash.

### C.4 Window stacking and focus in mutter 46.2 (from source)

**Activation path** [D: mutter 46.2]:
- `src/x11/window-x11.c`: a `_NET_ACTIVE_WINDOW` ClientMessage is handled **only if `meta_display_windows_are_interactable()`**. That function returns FALSE whenever GNOME Shell holds a Clutter stage grab (`src/core/display.c`).
  - [I] Which Shell UI takes such a grab: Overview, modal dialogs (keyring/polkit/end-session), Alt-Tab, the screenshot UI and open panel menus.
- Data: `l[0]` = source indication, `l[1]` = timestamp. A **0 timestamp** is replaced by `display->current_time`, which is `META_CURRENT_TIME` (0) for ClientMessage events (`src/x11/events.c event_get_time`).
- `meta_window_activate_full()` (`src/core/window.c`):
  - **Non-zero** timestamps older than `last_user_time` are refused; the window only "demands attention".
  - **Zero** skips that check, unminimizes the window, raises it if `raise-on-click` is on (true [M]) **or** the source is a pager (2), then focuses it.
  - A window on another workspace with timestamp 0 only demands attention. The kiosk and games share one workspace.

**New windows (focus-stealing prevention)** [D: `intervening_user_event_occurred`, `window_state_on_map`]:
- A new window whose `_NET_WM_USER_TIME` is **0** "explicitly requested no focus". If its user time is older than the focused window's, it is mapped **without focus and stacked just below the focused window**, i.e. behind the kiosk.
- A window with no user-time information takes focus (`focus-new-windows 'smart'` [M]).
- Wine master [D: `dlls/winex11.drv/window.c`]: when mapping, Wine deletes `_NET_WM_USER_TIME` ("try forcing activation") if the window is foreground or fullscreen, and otherwise sets it to 0. So fullscreen game windows usually take focus, but launchers or dialogs may open **behind the kiosk**.
- Proton's Wine fork may differ [I]. The shell must activate the game window itself.

**Layers** [D: `meta_window_get_default_layer`]:
- `_NET_WM_STATE_ABOVE` puts a window in the TOP layer **only if it is not maximised**.
- Fullscreen windows have **no special layer** in 46.2; they are NORMAL. `MetaStackLayer` (`src/meta/common.h`) has only DESKTOP, BOTTOM, NORMAL, TOP (= DOCK) and OVERRIDE_REDIRECT.
- So "always on top" would also hide every game behind the kiosk. Use activation instead.

**Tools compared** [D: sources]:

| tool | message sent | under mutter 46.2 |
|---|---|---|
| `wmctrl -a` (1.07 `main.c`) | `_NET_ACTIVE_WINDOW` with **source 0, timestamp 0**, then `XMapRaised` | works (raised because raise-on-click=true) |
| `xdotool windowactivate` (`xdo.c`) | **source 2 (pager), `CurrentTime`** | works; always raised |
| GameSpark `tools/xinput.py activate` [M] | ctypes Xlib, **source 2, timestamp 0**, sent to the root with SubstructureRedirect\|Notify masks | works; **reuse it** (no new package, no root) |

None of them works while the Shell holds a grab. Detect this case: after activating, read `_NET_ACTIVE_WINDOW`; if it is unchanged after 300 ms, close the Overview (send Escape via XTest) and retry [I].

**Launch and return choreography** [I, built on the facts above]:

1. **Launch:** backend `steam_launch APPID` (GameSpark, confirmed on `ExecCommandLine`).
2. Watch `_NET_CLIENT_LIST` (`PropertyNotify` on the root) for a window with `WM_CLASS steam_app_<APPID>`. Then **activate** it, and **iconify** the kiosk with `XIconifyWindow`, i.e. a `WM_CHANGE_STATE` IconicState message. This stops Chrome's rendering and video behind the game (B.5).
3. Also surface any new `steam`/Proton dialog window that maps during the launch. It may land behind the kiosk.
4. **Home pressed** (evdev, C.5): activate the kiosk (this unminimises it). Optionally pause the game (C.9).
5. **Resume:** activate the `steam_app_<APPID>` window, then iconify the kiosk again.
6. **Game exit:** `Game process removed: AppID N` in `console_log.txt` [M], or the window's DestroyNotify, triggers kiosk activation.

### C.5 Global Home/Guide: X key grabs vs evdev

**XGrabKey** [D: Xlib]: a passive grab fires only if "the keyboard is not grabbed" and the grab window is an ancestor of the focus window (or contains the pointer). So any client holding an active keyboard grab defeats global hotkeys.
- Upstream Wine's winex11 has `XGrabPointer` for cursor clipping and **no `XGrabKeyboard`** [D: `mouse.c`/`window.c` grep]. Proton games may therefore leave root key grabs working [I].
- Gamepad buttons are not X keys at all, so X grabs cannot see the Xbox Guide button anyway.

**evdev** [D: kernel `drivers/input/evdev.c`]: every open client receives every event unless one client holds `EVIOCGRAB`. In that case only the grabber receives them, and the grab is device-wide through `input_grab_device`.
- So a passive reader sees the Guide button (BTN_MODE 316 on `event7`) and remote keys alongside X, Chrome and Steam.

**Permissions:**

| device | readable without the input group? | how to change it |
|---|---|---|
| Joysticks (Xbox, DualSense) | **yes**, ACL via uaccess [M] | — |
| `/dev/uinput` (for virtual pads or keys) | **yes**, ACL [M] | — |
| Keyboards, remotes that enumerate as keyboard/consumer-control, mice | **no** [M] | a udev rule per device (below), or add `spark` to `input` (equivalent to reading every keyboard; worse) |

Example rule [I; attribute names measured with `udevadm info -a` on this box]:

```udev
# /etc/udev/rules.d/70-sparkhome-remote.rules — must sort BEFORE 73-seat-late.rules, which applies uaccess ACLs
SUBSYSTEM=="input", KERNEL=="event*", ATTRS{id/bustype}=="0005", ATTRS{id/vendor}=="xxxx", ATTRS{id/product}=="yyyy", TAG+="uaccess"
```

Load it with `udevadm control --reload && udevadm trigger -s input`, both as root. sysfs `id/*` values are **lowercase** hex [M].

- **Minimal reader:** `struct input_event` on 64-bit is 24 bytes: timeval 16 + type u16 + code u16 + value s32. Python stdlib `struct` plus `select` is enough; GameSpark's `controller/xbox-watch.py` reads joydev the same way [M]. A `python3-evdev` dependency is not needed.
- Treat **Guide long-press (≥ 600 ms)** or a **Guide+Start chord** as SparkHome Home while a game is focused. Short Guide is left to Steam's overlay (C.8) [I].

### C.6 Remapping remote keycodes with hwdb

**Format** [D: systemd v255 `hwdb.d/60-keyboard.hwdb` header]:
- Match `evdev:input:bZZZZvYYYYpXXXXeWWWW-*`. Bus, vendor, product and version are **4-digit uppercase hex**; bus 0005 = Bluetooth, 0003 = USB.
- Or match `evdev:name:<name>:phys:<phys>:ev:<ev>:*`.
- Entries are `KEYBOARD_KEY_<hex scancode lowercase>=<keycode name>`. HID scancodes are usage-page<<16 | usage: keyboard page `7xxxx`, consumer page `c0xxx`. Example in the file: `evdev:input:b0005v046DpB305*` → `KEYBOARD_KEY_c0183=media`.
- Local file: `/etc/udev/hwdb.d/61-keyboard-local.hwdb`. Apply with `systemd-hwdb update` and `udevadm trigger /dev/input/eventXX`.
- `60-evdev.rules` imports the keyboard builtin when hwdb properties exist [M].
- Find scancodes by reading `EV_MSC/MSC_SCAN` with a small reader. `evtest` is not installed [M-sibling].

**What to remap and why:**

| remote key (typical HID usage) | default path on this box | problem | remap to [I] |
|---|---|---|---|
| Home (consumer `c0223`, KEY_HOMEPAGE) | XF86HomePage → Chrome `IDC_HOME` (Linux accelerator table) [D] | kiosk navigates "home" | `f14` (XF86Launch5; unbound in GNOME [M]); the backend acts on the evdev code |
| Back (`c0224`, KEY_BACK) | XF86Back → Chrome `IDC_BACK` [D] | history navigation in the SPA | `esc` (the page handles Escape as Back) |
| Search (`c0221`, KEY_SEARCH) | XF86Search → GNOME search [M] | opens the Overview over the kiosk | `f15` |
| Settings/Config key (`c0183` KEY_CONFIG → `<I179>`) and KEY_F13 (`<FK13>`); both map to XF86Tools [M] | XF86Tools → GNOME Settings (`control-center-static`) [M] | Settings window pops up | `f16`. KEY_MENU maps to XF86MenuKB, which GNOME leaves unbound [M]; it can stay |
| Power (`c0030` or system `10081`) | GNOME power dialog (`power-button-action interactive` [M]) | dialog over the TV | `f17` (the backend sends DPMS off / CEC standby) |
| Volume/Mute | GNOME (`volume-*-static`) with OSD | — | **keep** (global volume works even in games) |

Example file [I; replace `VVVV`/`PPPP` with the remote's uppercase vendor and product IDs, and confirm each scancode with an MSC_SCAN read first]:

```
# /etc/udev/hwdb.d/61-sparkhome-remote.hwdb   (property lines MUST start with one space)
evdev:input:b0005vVVVVpPPPP*
 KEYBOARD_KEY_c0223=f14
 KEYBOARD_KEY_c0224=esc
 KEYBOARD_KEY_c0221=f15
 KEYBOARD_KEY_c0183=f16
 KEYBOARD_KEY_c0030=f17
```

Apply as root: `systemd-hwdb update && udevadm trigger -s input`. Then check with `udevadm info -q property -n /dev/input/eventN | grep KEYBOARD_KEY`.

- Whether `preventDefault()` on a `BrowserBack`/`BrowserHome` keydown suppresses Chrome's accelerator is **unverified**. hwdb avoids the question.
- Alternative: the backend `EVIOCGRAB`s the remote and re-injects through `/dev/uinput` (ACL present). That is more control, but more code; keep it for later [I].

### C.7 Chrome Gamepad API on Linux

- **Backend** [D: Chromium `device/gamepad/gamepad_device_linux.cc`]: buttons and axes are read from **joydev** (`/dev/input/jsN`). evdev is opened for special keys and force feedback; hidraw for some pads (e.g. DualShock/DualSense). The joydev nodes are readable here [M].
- **Xbox Series over BT** (045e:0b13) [D: `gamepad_standard_mappings_linux.cc`]: `MapperXboxSeriesXBluetooth` maps joydev b12 to `buttons[16]` (META/Guide) and b15 to `buttons[17]` (Share), with standard mapping for the rest.
  - This matches the measured joydev order (b12 = BTN_MODE) and Steam's SDL line (`guide:b12`) [M].
- **DualSense**: mapping relies on hid-playstation patches (kernel ≥ 5.12) [D: same file]. Its touchpad is also a libinput pointer [M: `ID_INPUT_TOUCHPAD`], so it can move the X cursor over the kiosk.
- **Unlock** [D: W3C Gamepad]: `getGamepads()` "returns an empty list before a gamepad user gesture has been seen". The gesture counts only while the document has user attention. Press any button once after boot.
- **Background** [D/I]: a non-minimised kiosk stays "visible" on Linux (B.5), so it keeps receiving gamepad data while a game is in front. **Ignore pad input when `!document.hasFocus()`**, or rely on iconifying the kiosk.
- **Duplicates** [I + M]: while Steam Input is active, a virtual "Steam Virtual Gamepad" (`03000000de280000ff11…`, i.e. 28de:11ff) appears alongside the real pad. Filter it by `id` string.

### C.8 Steam and the Guide button

- **The installed client's strings** [M: `steamui_english-json.js`] include:
  - "Enable Steam Input for Xbox controllers" / "…for Playstation controllers" / "…generic controllers"
  - "**Desktop Layout**" and "Non-Game Controller Layouts"
  - "**Enable Guide Button Chords for controllers**" and "Guide Button Chord Layout"
  - "Enable the Steam Overlay while in-game"
  - "Use the Big Picture Overlay when using a controller"
  - No "Guide Button Focuses Steam" string exists in this build.
- **Reported behaviour** [R: Steam forums; Microsoft Q&A; steam-for-linux #13185]:
  - While Steam runs, its **Desktop Layout** turns pad input into keyboard and mouse events for non-Steam apps. That would double-drive the kiosk (Gamepad API plus synthetic keys).
  - Fixes: "Disable Steam Input" on the Desktop Layout, or turn off "Enable Steam Input for Xbox controllers". Turn off Guide chords if they still fire.
  - Reports conflict, and some say only quitting Steam helps.
- **Recommended settings** [I]:
  1. Desktop Layout: **Disable Steam Input**.
  2. Guide Button Chords: **off**.
  3. Keep the in-game overlay on if the user wants it, and use Guide long-press for SparkHome Home.
- **Verify on the TV:**
  1. With Steam running and the kiosk focused, press A and the D-pad. Chrome should get Gamepad API events but **no** synthetic keys; check with a `keydown` logger.
  2. Press Guide (short) with the kiosk focused. Steam's window must not take focus.

### C.9 Pausing a game with SIGSTOP/SIGCONT

Prior art: **Nyrna** (X11/Windows) [D: GitHub source]:
- Suspends the **child processes recursively, then the parent**, with SIGSTOP. It resumes the same way with SIGCONT.
- It can minimise the window first, with a short delay.
- Its README warns that suspending "comes with the possibility that the application will crash".

**Risks:**

| risk | mechanism | mitigation |
|---|---|---|
| "Not responding" dialog | mutter pings windows and treats a missed reply as frozen after `check-alive-timeout` = 5000 ms [M/D] | set it to 0 [D], or never focus or close the stopped window |
| Stuck pointer grab | Wine grabs the pointer for ClipCursor (`XGrabPointer`) [D]. A stopped client keeps its X grabs. Wine releases clipping only when it processes FocusOut / foreground loss [D: `event.c`] | **activate the kiosk first, wait about 300–500 ms, then SIGSTOP** [I] |
| Senders hang | Wine/Proton processes talk through wineserver and window messages. Stopping only some processes can block others that send to a stopped thread [I] | stop the whole game process tree (the children under Steam's `reaper`/pressure-vessel for that AppID), not just the main exe |
| Audio | A stopped PipeWire client stops feeding its stream, which underruns into silence. A burst or desync on resume is possible [I] | test per game; fade volume in SparkHome before stopping |
| Network and anti-cheat | Peers and servers time out; anti-cheat may flag the process [I] | **pause only offline single-player titles** flagged in the GameSpark profile |
| GPU and swapchain | In-flight work completes. DXVK/VKD3D behaviour after a long stop is untested [I] | test on DOS2/RDR2 offline |
| cgroup freezer instead | `systemctl --user freeze` exists (systemd 255 `freeze`/`thaw` [M]) but freezes a whole unit. Steam and its games share one scope [M], so it would also freeze Steam | only viable if Steam runs in its own delegated service with per-game sub-cgroups [I] |

### C.10 Recommended session design

```
power on → GDM autologin (spark) → GNOME on Xorg (mutter 46.2)
  graphical-session.target
    ├─ sparkhome-backend.service  (Node 22, 127.0.0.1:8730)
    │    ├─ serves the tvOS-style UI + art cache (Steam librarycache, CDN 2x)
    │    ├─ steam: pipe launch (GameSpark steam_launch) + tail console_log.txt (added/removed)
    │    ├─ x11: activate / iconify / watch _NET_CLIENT_LIST (no new deps: extend GameSpark's ctypes xinput.py, run as a helper)
    │    ├─ input: evdev reader (js/event for pads via uaccess; remote via udev rule) → Home / long-press Guide
    │    └─ agent API: state(), move(), select(), back(), home(), launch(); CDP to the kiosk for screenshots/keys
    └─ sparkhome-kiosk.service    (Chrome --kiosk --class=SparkHome, own profile, CDP on localhost)
  Steam (snap) started by the backend at login, windowed; the backend re-activates the kiosk after Steam's window maps
```

**Fallback if GNOME Shell keeps intruding** (grabs, OSDs, overview): switch GDM to **GNOME Kiosk's Xorg script session**.
- Ubuntu 24.04 packages it as `gnome-kiosk-script-session` 46.0 [D]. It is mutter-based with no panels, docks or Shell UI.
- Cost: GameSpark's GNOME-specific console mode and gsettings no longer apply, and window-policy differences need testing [I].

### C.11 Incremental delivery with evidence gates

| step | outcome | gate (evidence) |
|---|---|---|
| 1 | Static tvOS-style grid page (6×260×156, safe 80/60) served locally | 1080p screenshot via CDP; overlay diff against a tvOS reference |
| 2 | Focus engine + keyboard/gamepad input in page | CDP-scripted move sequence → expected focus path; DevTools trace ≥ 58 fps during 30 rapid moves |
| 3 | Kiosk unit + backend unit, autostart on reboot | `loginctl` shows `gdm-autologin`; kiosk window `WM_CLASS=SparkHome` within 10 s; no keyring prompt |
| 4 | Desktop hardening (C.3) | trigger a notification (`notify-send`), press Super, press remote keys: nothing covers the kiosk |
| 5 | Steam launch + activate game + iconify kiosk | `_NET_ACTIVE_WINDOW` = `steam_app_<id>` within 30 s; kiosk `WM_STATE` Iconic |
| 6 | Home via evdev (Guide long-press / remote Home) | kiosk active within 300 ms; Steam overlay not opened (if chords off) |
| 7 | Game exit → kiosk | `Game process removed` → kiosk active within 2 s |
| 8 | (Optional) pause offline games | 10 pause/resume cycles in DOS2 without crash, audio returns, no "not responding" |

---

## Open questions

1. Exact tvOS 26/27 Home Screen metrics (icon corner radius, focus scale, shadow, label size, Top Shelf height, animation durations). Apple doesn't publish them, so they need a 1080p capture from a real Apple TV 4K.
2. Does Chrome 155 on NVIDIA 580 arm64 + Xorg run GPU compositing and rasterisation by default? Check `chrome://gpu` in a headful window, which needs the TV. Headless used SwiftShader.
3. Does `preventDefault()` on BrowserBack/BrowserHome keydown block Chrome's Linux accelerators in kiosk mode? (If not, hwdb remaps are mandatory.)
4. With "Enable Steam Input for Xbox controllers" off, does the snap Steam client still react to the Guide button, and does its Desktop Layout still inject keys?
5. Does the Xbox controller over BLE actually emit BTN_MODE on this kernel? The capability is present; press it once and read `event7`.
6. Do Proton (not upstream Wine) game windows launched through the pipe take focus over a focused kiosk, or map behind it?
7. Is the Chrome kiosk window also flagged maximised? That affects the `_NET_WM_STATE_ABOVE` layer rule, if "above" is ever used for an overlay.
8. How do SIGSTOP/SIGCONT affect PipeWire, Wine audio and DXVK/VKD3D swapchains on resume, per game?
9. Which remote will be used? It determines the hwdb match and scancodes; another agent is researching this.

---

## Appendix A — commands run on the Spark (all read-only)

`H='ssh -o BatchMode=yes spark'`
(The ssh config's `HostName spark` doesn't resolve from the Mac; `spark.local` → 192.168.1.20 via mDNS.)

| purpose | command |
|---|---|
| Versions | `gnome-shell --version; systemctl --version; dpkg-query -W mutter-common gnome-shell gnome-session-bin gdm3 systemd udev google-chrome-stable xserver-xorg-core libmutter-14-0 gnome-shell-extension-ubuntu-dock update-notifier apport` |
| Session units and env | `systemctl --user show-environment`; `systemctl --user list-units --type=target`; `systemctl --user cat graphical-session.target gnome-session.target gnome-session@ubuntu.target gnome-session-x11@ubuntu.target gnome-session-initialized.target org.gnome.Shell.target org.gnome.Shell@x11.service`; `systemctl --user list-units --type=scope` |
| Rules and ACLs | `cat /usr/lib/udev/rules.d/{70-uaccess,73-seat-late,60-evdev,60-steam-input}.rules`; `getfacl -p /dev/uinput /dev/input/event*`; `udevadm info -q property -n /dev/input/eventN`; `udevadm info -a -n /dev/input/event8`; `cat /proc/bus/input/devices` |
| gsettings | `XDG_CURRENT_DESKTOP=ubuntu:GNOME gsettings get …` (keys in C.1); `gsettings list-recursively org.gnome.settings-daemon.plugins.media-keys`; `cat /usr/share/glib-2.0/schemas/{10_ubuntu-dock,50_nvidia-settings}.gschema.override` |
| X | `xset q`; `xdpyinfo -queryExtensions`; `xrandr --current`; `xprop -root _NET_CLIENT_LIST _NET_ACTIVE_WINDOW`; `xprop -id <w> WM_CLASS _NET_WM_NAME _NET_WM_STATE _NET_WM_PID` |
| XKB | `grep … /usr/share/X11/xkb/symbols/inet /usr/share/X11/xkb/keycodes/evdev` |
| Steam | `grep -i guide …/Steam/config/config.vdf`; `grep -o "Settings_Controller…" …/steamui/localization/steamui_english-json.js`; `grep "Game process" …/logs/console_log*.txt`; `cat /proc/<steam pid>/cgroup` |
| Autostart | `ls /etc/xdg/autostart ~/.config/autostart`; `grep enabled /etc/default/apport` |

## Appendix B — sources

**Apple (primary)**
- HIG pages (rendered / JSON):
  - focus-and-selection, layout, app-icons, images, top-shelf, typography, materials, color, motion, designing-for-tvos, lockups, dark-mode
  - Pages: `https://developer.apple.com/design/human-interface-guidelines/<page>`
  - JSON: `https://developer.apple.com/tutorials/data/design/human-interface-guidelines/<page>.json`
- Archived app icons (tvOS size table): https://web.archive.org/web/20240223230641/https://developer.apple.com/tutorials/data/design/human-interface-guidelines/app-icons.json
- UIKit, About focus interactions for Apple TV: https://developer.apple.com/documentation/uikit/about-focus-interactions-for-apple-tv
- UIFocusAnimationCoordinator: https://developer.apple.com/documentation/uikit/uifocusanimationcoordinator
- remembersLastFocusedIndexPath: https://developer.apple.com/documentation/uikit/uicollectionview/rememberslastfocusedindexpath
- SwiftUI focusSection(): https://developer.apple.com/documentation/swiftui/view/focussection()
- TVTopShelfCarouselItem: https://developer.apple.com/documentation/tvservices/tvtopshelfcarouselitem
- Apple Support, appearance (six vs five icons): https://support.apple.com/en-us/guide/tv/atvb46af4f11/tvos
- Apple Support, What's new in tvOS 26: https://support.apple.com/guide/tv/whats-new-atvb457e13ae/26.0/tvos/26.0
- Apple Newsroom tvOS 26: https://www.apple.com/newsroom/2025/06/apple-tv-brings-a-beautiful-redesign-and-enhanced-home-entertainment-experience/
- Apple fonts: https://developer.apple.com/fonts/
- Forum thread on SF licence and the web: https://developer.apple.com/forums/thread/733267
- Focus geometry threads: https://developer.apple.com/forums/thread/772417 , https://developer.apple.com/forums/thread/772210

**tvOS secondary**
- Xamarin tvOS icons and images (legacy sizes): https://learn.microsoft.com/en-us/previous-versions/xamarin/ios/tvos/app-fundamentals/icons-images
- react-native-tvos `RCTTVView.m` / `TVViewPropTypes.js`: https://unpkg.com/react-native-tvos@0.86.0-2/React/Views/RCTTVView.m
- atvImg: https://github.com/drewwilson/atvImg
- AppleInsider tvOS 26 review: https://appleinsider.com/articles/25/09/14/tvos-26-review-high-on-polish-light-on-new-features-for-apple-tv
- MacStories tvOS 26 overview: https://www.macstories.net/news/tvos-26-the-macstories-overview/
- MacRumors tvOS 27 notes: https://www.macrumors.com/2026/09/25/tvos-27-release-notes/
- Liquid Glass on the web:
  - https://blog.logrocket.com/how-create-liquid-glass-effects-css-and-svg/
  - https://medium.com/ekino-france/liquid-glass-in-css-and-svg-839985fcb88d
  - https://www.buildmvpfast.com/blog/liquid-glass-css-backdrop-filter-recipes-2026
- Inter: https://rsms.me/inter/

**Prior art**
- Flex Launcher: https://github.com/complexlogic/flex-launcher
- big-launcher: https://github.com/complexlogic/big-launcher
- OpenGamepadUI: https://github.com/ShadowBlip/OpenGamepadUI ; docs: https://opengamepadui.readthedocs.io/
- Plasma Bigscreen / Plasma 6.7:
  - https://www.phoronix.com/news/KDE-Plasma-6.7-Released
  - https://www.omgubuntu.co.uk/2025/07/plasma-bigscreen-revival-kde-tv
  - https://archlinux.org/packages/extra/x86_64/plasma-bigscreen/
- Ubuntu noble packages:
  - https://packages.ubuntu.com/noble/plasma-desktop
  - https://packages.ubuntu.com/noble/kodi
  - https://packages.ubuntu.com/noble/gnome-kiosk-script-session
- Kodi:
  - 22 Beta 2: https://kodi.tv/article/kodi-22-piers-beta-2/
  - AppTV skin: https://kodi.wiki/view/Add-on:AppTV
  - JSON-RPC: https://kodi.wiki/view/JSON-RPC_API
- gamescope: https://github.com/ValveSoftware/gamescope ; https://archlinuxarm.org/packages/aarch64/gamescope ; https://forums.developer.nvidia.com/t/valve-gamescope-crashing-when-scaling-filters-are-applied-on-580-105-08/354533
- Pegasus: https://github.com/mmatyas/pegasus-frontend
- ES-DE: https://www.es-de.org/
- Jellyfin Desktop: https://github.com/jellyfin/jellyfin-desktop ; https://flathub.org/apps/org.jellyfin.JellyfinDesktop
- GNOME Kiosk: https://gitlab.gnome.org/GNOME/gnome-kiosk
- Spatial navigation:
  - https://github.com/NoriginMedia/Norigin-Spatial-Navigation
  - https://github.com/bbc/lrud
  - https://github.com/luke-chang/js-spatial-navigation
  - https://github.com/WICG/spatial-navigation
  - https://github.com/bamlab/react-tv-space-navigation
- Performance:
  - https://web.dev/articles/stick-to-compositor-only-properties-and-manage-layer-count
  - https://web.dev/articles/avoid-large-complex-layouts-and-layout-thrashing
  - https://web.dev/articles/simplify-paint-complexity-and-reduce-paint-areas
  - https://web.dev/articles/content-visibility
  - https://developer.mozilla.org/en-US/docs/Web/CSS/will-change
  - https://developer.mozilla.org/en-US/docs/Web/API/HTMLImageElement/decode
  - https://developer.chrome.com/docs/devtools/performance/reference
- CDP: https://chromedevtools.github.io/devtools-protocol/ ; https://developer.chrome.com/blog/remote-debugging-port

**Session, window and input (primary sources and code)**
- mutter 46.2 (src/x11/window-x11.c, src/x11/events.c, src/core/window.c, src/core/display.c, data/org.gnome.mutter.gschema.xml.in): https://gitlab.gnome.org/GNOME/mutter/-/tree/46.2
- gnome-session 46.0 (gnome-session/main.c, gsm-util.c, gsm-autostart-app.c, gsm-manager.c): https://gitlab.gnome.org/GNOME/gnome-session/-/tree/46.0
- systemd v255:
  - `man/systemd.special.xml`: https://github.com/systemd/systemd/blob/v255/man/systemd.special.xml
  - `hwdb.d/60-keyboard.hwdb`: https://github.com/systemd/systemd/blob/v255/hwdb.d/60-keyboard.hwdb
- xdotool `xdo.c`: https://github.com/jordansissel/xdotool/blob/master/xdo.c
- wmctrl 1.07 `main.c`: https://sources.debian.org/src/wmctrl/1.07-7/main.c/
- Xlib XGrabKey: https://tronche.com/gui/x/xlib/input/XGrabKey.html
- Wine winex11 (window.c, event.c, mouse.c): https://gitlab.winehq.org/wine/wine/-/tree/master/dlls/winex11.drv
- Linux evdev: https://github.com/torvalds/linux/blob/master/drivers/input/evdev.c
- Chromium:
  - gamepad (`device/gamepad/gamepad_standard_mappings_linux.cc`, `gamepad_device_linux.cc`)
  - switches (`chrome/common/chrome_switches.h`, `base/base_switches.h`, `media/base/media_switches.cc`, `components/password_manager/core/browser/password_manager_switches.cc`, `content/public/common/content_switches.cc`)
  - accelerators (`chrome/browser/ui/accelerator_table.cc`)
  - All via https://github.com/chromium/chromium (main, 2026-10)
- W3C Gamepad: https://w3c.github.io/gamepad/
- Electron page visibility: https://www.electronjs.org/docs/latest/api/browser-window#page-visibility
- NixOS wiki Steam (`steam_app_` WM_CLASS): https://wiki.nixos.org/wiki/Steam
- Steam forums on Desktop Layout and Guide: https://steamcommunity.com/discussions/forum/1/2299597907724592000 ; https://github.com/valvesoftware/steam-for-linux/issues/13185
- Nyrna: https://github.com/Merrit/nyrna
