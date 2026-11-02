# SparkHome research: streaming services on the DGX Spark (Chrome 155, Linux arm64) and how a 10-foot launcher should open and drive them

- Research run: 2026-10-08 (task date 2026-10-07). Author: streaming research subagent.
- Scope: (1) Chrome for Linux ARM64 and Widevine, (2) per-service playback on this box, (3) 10-foot web UIs and navigation, (4) Chrome kiosk + DevTools Protocol (CDP) control plane, (5) higher-quality alternatives (Kodi, Waydroid, companion streamer), (6) deep links for a voice/agent harness.
- Out of scope here: the Apple TV Gen 1 remote, Bluetooth controllers, the voice stack, games (covered by other research tracks).

## Evidence labels

| Label | Meaning |
|---|---|
| **[measured]** | Observed in this run. "Spark" = run over ssh on the user's DGX Spark (`spark`, Chrome 155.0.8059.39, throwaway headless profiles under `/tmp`, deleted afterwards). "Mac" = headless Chromium (Playwright build) on the research Mac, used only for server-side behaviour such as user-agent routing. |
| **[documented]** | Vendor help page, official blog, or Chromium/protocol source code. URL given. |
| **[reported]** | Press, forum posts, third-party repos. URL and date given where known. |
| **[inferred]** | Reasoning from the above. Not verified. |

Probes on the Spark were read-only commands plus headless Chrome runs with temporary profiles. Nothing was shown on the TV, nothing was installed, and no settings were changed. The probe scripts are kept in `scratchpad/tools/` (see section 10) so build agents can rerun them.

---

## 0. Bottom line

1. **This box gets Widevine L3 only, and Chrome reports that it cannot meet any HDCP version.** [measured, Spark] `requestMediaKeySystemAccess('com.widevine.alpha')` succeeds only for `SW_SECURE_CRYPTO` and `SW_SECURE_DECODE`. Every `HW_SECURE_*` level fails with `NotSupportedError`. `MediaKeys.getStatusForPolicy({minHdcpVersion})` returns `output-restricted` for HDCP 1.0, 1.4, 2.0, 2.2 and 2.3. This was measured headless; Chromium source says the headed result should be the same (section 2.4).
2. **Expected quality per service in Chrome on this box (sections 3 and 4):**
   - **Netflix:** up to 1080p, stereo. [documented]
   - **YouTube (non-DRM):** fine, with VP9/AV1 software decode. [measured]
   - **Spotify Web:** 256 kbps AAC. [documented]
   - **Twitch, Plex Web, Jellyfin Web:** fine. Plex and Jellyfin must transcode HEVC and Dolby audio because Chrome here lacks those decoders. [measured]
   - **Prime Video:** SD only. [documented]
   - **Disney+:** 720p stereo at best on any browser [documented], and 480p observed on Linux in July 2026. [reported]
   - **Hulu:** needs HDCP verification for 720p [documented], so expect 480p or less. [inferred]
   - **Peacock:** officially Windows/Mac only, and reported not to play on Linux. [documented/reported]
   - **HBO Max, Paramount+, Apple TV, Crunchyroll, Luna:** Linux is not officially listed. Status is unknown and needs an on-device test.
3. **Spoofing the user agent does not lift DRM caps.** Chrome on arm64 already sends `X11; Linux x86_64` in its UA [measured]. The Widevine license request carries its own client and capability data (HDCP, platform verification) that the page cannot change [documented proto]. A Disney+ user who spoofed a Windows UA plus client hints stayed at 480p [reported].
4. **A real 10-foot UI is only available for the YouTube family.** `https://www.youtube.com/tv` (leanback) and `https://www.youtube.com/tv/upg` (YouTube TV live) render their TV UIs when Chrome presents a TV or console user agent [measured]. With a PS4/Cobalt UA, `#/search?q=…` opened results [measured, Mac]. `#/watch?v=…` played on the Spark's Chrome 155 with zero dropped frames [measured, Spark]. Netflix, Disney+, Prime, Spotify, Twitch and Plex have no browser-reachable TV UI [measured: 404 or unchanged pages]. Cloud gaming web apps (Xbox Cloud, GeForce NOW, Luna) are already gamepad-driven.
5. **Recommended control plane: one Chrome in `--kiosk` with a dedicated profile, driven over CDP on a fixed localhost port.**
   - The SparkHome launcher is tab 1, a local web page. Each service opens in its own tab.
   - The controller can override the UA per tab, inject a gamepad/remote bridge script, receive "Home" via `Runtime.addBinding`, pause media, and capture screenshots or the accessibility tree for the agent.
   - Widevine licensing and playback work under CDP control. [measured, Spark: Shaka Widevine test stream played]
   - A fixed `--remote-debugging-port` does not set `navigator.webdriver`. `--remote-debugging-pipe`, `--enable-automation` and port `0` do. [documented source; measured false with a fixed port]
   - The source also maps `--headless`, yet our Chrome 155 `--headless=new` runs reported `false`. That does not matter for the headed kiosk.
6. **Use a companion streamer for premium services in HD.** For Disney+/Hulu, Prime Video, Peacock, HBO Max, Paramount+ and Apple TV, a Google TV Streamer, Apple TV 4K or Fire TV on another HDMI input gives 1080p plus Dolby audio on this 1080p TV [inferred]. SparkHome can still drive it: `androidtvremote2`/adb (Google TV), `pyatv` (Apple TV), and the Sony Bravia REST API for input switching [documented]. The Spark has no HDMI-CEC device [measured: no `/dev/cec*`].
7. **Kodi does not help.** The CastagnaIT Netflix add-on suspended development in June 2024 and documents SD-only playback [documented README]. Other add-ons hit the same Linux L3 caps. **Waydroid does not help either.** NVIDIA's proprietary driver gets software rendering only, and playback is L3 at about 480–540p [reported].

---

## 1. Measured facts about this box that matter for streaming

All rows below were measured on the Spark on 2026-10-08.

| Item | Value |
|---|---|
| Arch / Chrome | `aarch64`; `Google Chrome 155.0.8059.39` |
| User-Agent string sent | `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36`. The headless run showed `HeadlessChrome/155.0.0.0`; headed Chrome shows `Chrome/` [inferred]. The UA says **x86_64** even on arm64. |
| `navigator.platform` | `Linux x86_64` |
| UA Client Hints (high entropy) | `architecture: "arm"`, `bitness: "64"`, `platform: "Linux"`, `fullVersionList: Google Chrome 155.0.8059.39` |
| `navigator.webdriver` with fixed `--remote-debugging-port` | `false` (headless=new run) |
| Widevine CDM | Bundled at `/opt/google/chrome/WidevineCdm`, manifest `version 4.10.3112.0`. Platforms `linux/x64` and `linux/arm64`. `x-cdm-codecs: vp8,vp09,avc1,av01` (no HEVC). `x-cdm-persistent-license-support: false`. Schemes `cenc`, `cbcs`. |
| Widevine component updater | `~/.config/google-chrome/WidevineCdm/latest-component-updated-widevine-cdm` = `{"LastBundledVersion":"4.10.3112.0","Path":"/opt/google/chrome/WidevineCdm"}`. The component updater is active, so do not pass `--disable-component-update`. |
| EME robustness accepted | `SW_SECURE_CRYPTO` ok, `SW_SECURE_DECODE` ok. `HW_SECURE_CRYPTO`, `HW_SECURE_DECODE`, `HW_SECURE_ALL`, `150`, `2000`, `3000` all `NotSupportedError`. This means **L3**. |
| EME HDCP policy (`getStatusForPolicy`) | `output-restricted` for `minHdcpVersion` 1.0, 1.4, 2.0, 2.2, 2.3 (headless, `http://127.0.0.1` origin) |
| MSE `isTypeSupported` | H.264 `avc1.640028` **true**; HEVC `hvc1…` **false**; VP9 **true**; AV1 **true**; AAC **true**; Opus **true**; AC-3 **false**; E-AC-3 **false** |
| Widevine under CDP control | Shaka Player 5.2.11 played Google's Widevine test asset `angel-one-widevine` (license proxy `cwip-shaka-proxy.appspot.com/no_auth`) under CDP: `keySystem: com.widevine.alpha`, VP9 768×576, 12 s played, not paused |
| YouTube leanback under CDP, Chrome 155 | PS4/Cobalt UA. `#/watch?v=` played. An ad decoded at 1920×1080 with 0/371 dropped frames. Big Buck Bunny 4K60 sat at 1280×720 for the 50 s test with 0/2,968 dropped frames. That is likely ABR ramp in headless, not a hard cap. |
| Page size | `4096` (relevant to older ARM Widevine builds that needed 4K pages) |
| HDMI-CEC | no `/dev/cec*` device |
| `nvidia_drm` params | `modeset`/`fbdev` exist but are not readable by `spark` (value unknown) |
| Chrome policies | `/etc/opt/chrome/policies/managed` does not exist yet |
| Flatpak | not installed (`snap` present) |
| Gamepad device access | `/usr/lib/udev/rules.d/70-uaccess.rules` tags joysticks `uaccess`. `/dev/input/js1` carries an ACL, so the seat user, and therefore Chrome's Gamepad API, can read controllers without the `input` group. |

---

## 2. Google Chrome for Linux ARM64

### 2.1 When it shipped

- **2026-03-12:** Google's blog post "Bringing Chrome to ARM64 Linux Devices" announced the launch for **Q2 2026** [documented]. It names **NVIDIA DGX Spark** and says Google is "partnering with NVIDIA" to ease installation. Other distributions install from chrome.com/download. https://blog.google/chromium/bringing-chrome-to-arm64-linux-devices/
- **Late July 2026:** arm64 `.deb`/`.rpm` packages appeared in Google's repos as **Chrome 150 stable** [reported]. The download page still served amd64; testers swapped `amd64` for `arm64` in the URL (`google-chrome-stable_current_arm64.deb`). Installing the package adds Google's apt repo.
  - OMG! Ubuntu, July 2026, updated 2026-08-17: https://www.omgubuntu.co.uk/2026/07/chrome-arm64-linux-available
  - AlternativeTo, 2026-07-28: https://alternativeto.net/news/2026/7/google-chrome-launches-for-arm64-linux-with-native-widevine-drm/
- **System requirements page (fetched 2026-10-08):** Linux needs "64-bit Ubuntu 18.04+, Debian 10+, openSUSE 15.5+, or Fedora Linux 39+" and "An Intel Pentium 4 processor or later that's SSE3 capable, or an ARM processor" [documented]. https://support.google.com/chrome/a/answer/7100626
- **Two-week release cycle since Chrome 153 (2026-09-08).** 154 shipped 2026-09-22 and 155 on 2026-10-06 [documented]. The Spark's 155.0.8059.39 matches.
  - https://developer.chrome.com/blog/chrome-two-week-release
  - https://developer.chrome.com/blog/chrome-two-week-start
  - Consequence [inferred]: the kiosk will see a new major version about every 14 days via apt. SparkHome needs a post-update smoke test and a scheduled Chrome restart (section 5.9).

### 2.2 How Widevine is delivered

- Widevine is **bundled in the Chrome package** at `/opt/google/chrome/WidevineCdm/_platform_specific/linux_arm64/`. The manifest lists both x64 and arm64 sub-packages. It is **also component-updatable**: Chrome writes a `latest-component-updated-widevine-cdm` pointer in the profile [measured; also reported by OMG! Ubuntu].
- Before Chrome arm64 existed, aarch64 Linux used Widevine extracted from ChromeOS images (Asahi `widevine-installer`, Kodi `inputstreamhelper`) [reported]. An open issue asks Asahi to switch to Chrome's arm64 CDM (2026-08-09): https://github.com/AsahiLinux/widevine-installer/issues/21. Kodi's helper first gained an aarch64 CDM from a ChromeOS beta in 2023: https://github.com/emilsvennesson/script.module.inputstreamhelper/issues/530.

### 2.3 Security level

- **L3 (software) only** [measured]. Linux desktop browsers are outside Widevine's Verified Media Path (VMP) program, which covers Windows and macOS [reported: Wikipedia Widevine]. Services therefore treat Linux Chrome as their lowest browser tier.
- The license request carries device information the page cannot change [documented]. The Widevine `ClientIdentification` message includes `client_info` name/value pairs, `ClientCapabilities.max_hdcp_version` (`HDCP_NONE` … `HDCP_V2_3`), analog-output capabilities and `resource_rating_tier`. The license side has a `PlatformVerificationStatus` (`PLATFORM_UNVERIFIED`, `…_SOFTWARE_VERIFIED`, `…_HARDWARE_VERIFIED`, `…_NO_VERIFICATION`). Source: `license_protocol.proto` in https://github.com/devine-dl/pywidevine. That is why UA spoofing does not raise DRM tiers [inferred; consistent with the July 2026 Disney+ report in section 3].

### 2.4 HDCP implications

- **Chromium source.** On non-ChromeOS platforms, `OutputProtectionProxy::QueryStatus` returns success with `link_mask=0`, `protection_mask=0`, and `EnableProtection()` is `NOTIMPLEMENTED()` and returns false [documented]. https://chromium.googlesource.com/chromium/src/+/HEAD/chrome/browser/media/output_protection_proxy.cc
- **Spark.** `getStatusForPolicy` returns `output-restricted` for every HDCP version [measured].
- **Effect.** Any service that requires verified HDCP for HD will cap or refuse:
  - Hulu documents 720p only "if HDCP 1.4 is enabled" or no external display is connected [documented].
  - HBO Max troubleshooting points to HDMI/HDCP for its "There's a problem with your device setup" error [documented].
  - HBO Max needs HDCP 2.2 for 4K [documented].
- The HDCP probe ran headless. A headed check on the TV session is in the test plan (section 9). Source code suggests the same result [inferred].

---

## 3. Per-service results

### 3.1 Playback table (Chrome 155, Linux arm64, 1080p Sony TV)

"Expected max" means the best realistic picture on this box, not the service's best anywhere. The TV EDID only lists 1080p modes, so 4K/HDR is moot for every row [measured by orchestrator].

| Service | Plays on Linux Chrome? | Official Linux status | Expected max video | HDR | Audio (this box) | Confidence |
|---|---|---|---|---|---|---|
| Netflix | Yes, expected | "some computers with GNU/Linux"; no support for Linux [documented] | **1080p** (Chrome 117+ on Linux, help page as of 2026-10-08) [documented] | No | Stereo. 5.1 is "supported only on Windows 10 or later … through the Edge browser or the Netflix app" [documented]. AAC 5.1 possible via unofficial extension [reported]. No DD+ because E-AC-3 is unsupported [measured]. | Med-high |
| Disney+ (incl. Hulu content) | Yes, unofficially | Not listed. Browsers listed only for Windows 10+, macOS 11+, ChromeOS [documented] | **480p observed** on Linux, July 2026 [reported]. 720p is the browser ceiling: "computer browsers … are limited to 720p HD" [documented] | No | Stereo [documented: web browser "Audio quality: Stereo"] | Medium |
| Hulu (hulu.com) | Unknown, probably | Computer OS list excludes Linux [documented]. App content folding into Disney+ during 2026 [reported] | 720p needs verifiable HDCP 1.4; otherwise lower (Firefox gets 480p) [documented]. Expect **≤480p** here [inferred from measured HDCP] | No | Stereo [inferred] | Low-med |
| HBO Max | Unknown | Computers: "PC, Mac, or Google Chromebook" with latest Chrome/Firefox/Edge(Win)/Safari [documented] | Unknown. If it plays, expect ≤720p [inferred] | No | Unknown | Low |
| Prime Video | Yes | "Prime Video is available … on Windows, macOS, Chrome OS, or Linux" [documented] | **SD**: "other than Windows or macOS, playback is restricted to standard definition" [documented]. About 480–540p measured by bitrate in April 2026 [reported] | No | Stereo [documented] | High |
| Apple TV (tv.apple.com) | Unknown | "A web browser" with no OS named [documented]. Apple: "Chrome, Firefox, or Edge on a Mac, Windows PC, or Android … up to 1080p" [reported quote] | Linux reports ≤720p (older) [reported] | No | Unknown | Low |
| Paramount+ | Unknown | Help pages list Windows/Mac only and are stale [documented] | Unknown | No | Unknown | Low |
| Peacock | **Likely no** | Browsers: "Chrome 125+ (Windows/Mac)…" only [documented] | Reported not to play on Linux (LTT thread 2026-07-19; Ubuntu MATE forum) [reported] | — | — | Medium |
| YouTube | **Yes** [measured] | "Ubuntu 10+" listed on an old YouTube page [documented] | Desktop offers VP9/AV1 to 4K+. Leanback caps to display size (VacuumTube has an "Unlock Resolution" setting) [reported]. **1080p** on this TV [inferred] | No (Xorg, SDR TV) | Opus/AAC stereo | High |
| YouTube TV | Probably | "latest version of Chrome, Firefox, or Safari", no OS restriction [documented] | ≤1080p [inferred]. Needs location access for the home area [documented] | No | Stereo [inferred] | Medium |
| Spotify Web | Yes | Desktop: Chrome/Firefox/Edge/Opera/Safari, no OS limit; needs Widevine [documented] | — | — | **AAC 256 kbit/s** (Premium). Quality is not adjustable and there is no lossless on web [documented] | High |
| Twitch | Yes (no DRM) | — | Source quality, commonly up to 1080p60 [inferred] | No | Stereo | High |
| Plex Web | Yes | — | Direct play for H.264/VP9/AV1 + AAC/Opus. **HEVC, AC-3, E-AC-3 transcode** [measured codec support]. Plex HTPC app is x86_64-only [documented] | No | Stereo (or transcoded) | High |
| Jellyfin Web | Yes | — | Same codec constraints as Plex. Jellyfin Desktop exists for aarch64 [documented] | No | Same | High |
| Crunchyroll | Unknown | Help lists browser versions (Chrome/Firefox/Edge 123+), no OS [reported] | Unknown | No | Unknown | Low |
| Amazon Luna | Unknown | OS list excludes Linux; browsers Chrome/Edge/Safari [documented-secondary]. Service restructured April 2026 [reported] | Cloud stream | — | — | Low |
| GeForce NOW (web) | Probably | Browser officially Windows/macOS/ChromeOS. Native Linux app is **x86_64 only** (Ubuntu 24.04, NVIDIA 580.126.07+, Xorg) [documented] | Linux browser reportedly held to 1080p60 without a Windows UA (Sept 2024) [reported] | — | — | Medium |
| Xbox Cloud Gaming | Probably | Official browser table has no Linux row [documented] | 1080p. 1440p for Game Pass Ultimate on "supported browsers"; Linux unconfirmed [reported] | — | — | Medium |

### 3.2 Per-service notes and sources

**Netflix**
- **Supported browsers (fetched 2026-10-08 with curl).** Linux: "Chrome 117 or later – Up to Full HD (1080p)", same for Firefox 129+, Edge 118+, Opera 92+ [documented]. Netflix "can't guarantee Netflix will work on devices with a Linux OS" and support will not troubleshoot Linux. https://help.netflix.com/en/node/23742 (also node/100)
- **History.** Older copies of the page and a July 2026 article still said 720p on Linux [reported]. The live page says 1080p; the date of the change is unknown.
- **5.1 audio.** "5.1 surround sound is supported only on Windows 10 or later computers through the Edge browser or the Netflix app" [documented]. https://help.netflix.com/en/node/14163
- **Unofficial 5.1/1080p extensions.** The original "Netflix 1080p" (2021) is reported broken. The "New Netflix 1080p" fork (v1.33.0, 2025-09-26) claims AAC 5.1 [reported]. It is unverified on Linux and a ToS grey area.
- **Keyboard shortcuts** [documented]: Enter/Space = play/pause, F = fullscreen, Esc = exit fullscreen, Left/Right = ∓10 s, Up/Down = volume, M = mute, S = skip intro. https://help.netflix.com/en/node/24855

**Disney+**
- **Browser requirements** list only Windows 10+, macOS 11+ and ChromeOS [documented, rendered 2026-10-08]. https://help.disneyplus.com/article/disneyplus-computer-browser-requirements
- **Quality.** "4K streaming is not available on computer browsers, which are limited to 720p HD". Web browser Automatic setting = "Video quality: 720p HD, Audio quality: Stereo" [documented]. https://help.disneyplus.com/article/disneyplus-data-usage
- **Linux report (LTT, 2026-07-19, Fedora + official Google Chrome).**
  - `chrome://media-internals` showed 854×480 H.264 even though the playback API advertised `max 1280x720`.
  - The Bitmovin Widevine demo played 1080p on the same machine.
  - Spoofing a Windows UA and client hints changed nothing.
  - Disney support said the restriction is intentional.
  - https://linustechtips.com/topic/1640895-disney-intentionally-limits-linux-browsers-to-480p-confirmed-by-support-my-investigation/ [reported]

**Hulu**
- **Requirements.** "Mac OS X 10.13 or above, Microsoft Windows 10 or above, and the latest version of Chrome OS" [documented].
- **720p rule.** "720p is enabled on Chrome (Windows), Edge (Windows), and Safari (MacOS) if HDCP 1.4 is enabled … Firefox … max video quality is 480p … Rights restrictions and digital rights management policies prevent Hulu from streaming in 720p unless the device as configured can be verified as HDCP compliant." https://help.hulu.com/article/hulu-supported-computers
- **App wind-down.** Disney announced in 2025 that the standalone Hulu app would be retired during 2026, with content inside Disney+ [reported]. https://www.tvline.com/news/hulu-app-disney-plus-combining-schedule-pricing-1235485863
- **SparkHome.** Treat Hulu as a Disney+ deep link or tile.

**HBO Max**
- **Computers.** "You can stream on your PC, Mac, or Google Chromebook at HBOMax.com … latest version of Chrome, Firefox, Microsoft Edge for Windows, or Safari" [documented, rendered]. https://help.hbomax.com/us/Answer/Detail/000002506
- **4K requirements.** HDCP 2.2, Premium plan, supported 4K device [documented]. https://help.hbomax.com/us/Answer/Detail/000002523
- **Linux history.** Blocked by Widevine policy in 2020 (Ars Technica via Slashdot). Loading-screen hangs reported in 2024 [reported].
- **Ownership.** The help center shows a banner about "Warner Bros. Discovery and Paramount coming together as Skydance" [documented]. HBO Max and Paramount+ apps or URLs may change.

**Prime Video**
- **Requirements** [documented, rendered 2026-10-08]: "available via a web browser on a computer running Windows, macOS, Chrome OS, or Linux … If you're running an operating system other than Windows or macOS, playback is restricted to standard definition." Web browsers: "up to HD", "Stereo". https://www.primevideo.com/help?nodeId=GUX9FYHU5D8LC9EJ
- **LinuxAdictos, 2026-04-18.** Firefox on Linux used about 1.2 GB/h, roughly 480–540p. A Waydroid Android app looked worse [reported]. https://en.linuxadictos.com/My-latest-failure-trying-to-play-Prime-Video-at-1080p-on-Linux.html

**Apple TV**
- **Web playback.** "Go to tv.apple.com to subscribe to and watch Apple TV in a web browser". You may need to verify a credit card, and ad blockers can break autoplay [documented]. https://support.apple.com/guide/tvapp/get-the-apple-tv-app-and-sign-in-atvbf947803a/1.0/web/1.0
- **1080p quote.** "In the latest version of Google Chrome, Mozilla Firefox, or Microsoft Edge on a Mac, Windows PC, or Android … compatible content will play in up to 1080p (HD)." Quoted around 2023 at https://fabianwoedl.com/apple-tv-looks-bad/ [reported].

**Paramount+**
- Help pages list Windows 8/10 and Mac OS X with very old browser minimums, so they are clearly stale [documented].
  - https://help.paramountplus.com/s/article/PD-Which-browsers-are-supported-by-Paramount
  - https://help.paramountplus.com/s/article/PD-What-desktop-operating-systems-and-browser-types-are-supported-for-streaming-on-paramountplus-com

**Peacock**
- **Web browsers:** "Chrome 125+ (Windows/Mac), Firefox 127+ (Windows/Mac), MS Edge 125+ (Windows/Mac), Safari 17.3+ (Mac)" [documented]. https://www.peacocktv.com/help/article/what-devices-and-platforms-are-supported-by-peacock
- **Linux reports.** "Peacock doesn't work at all on Linux" (LTT, 2026-07-19). Every Linux browser was refused (Ubuntu MATE forum) [reported].

**YouTube / YouTube TV**
- **YouTube** [measured: plays on the Spark]. See section 4.1 for leanback details.
- **YouTube TV.** "YouTube TV is supported on the latest version of Chrome, Firefox, or Safari" [documented]. https://support.google.com/youtubetv/answer/7129767
- **YouTube TV 10-foot UI.** `https://www.youtube.com/tv/upg` with a TV UA renders "YouTube TV | Member sign-in" [measured, Mac]. Playback there is untested.

**Spotify**
- **Web player.** Desktop "baseline widely available versions" of Chrome/Firefox/Edge/Opera/Safari. Widevine must be allowed [documented]. https://support.spotify.com/us/article/web-player-help/
- **Web player quality.** Free AAC 128 kbit/s, Premium AAC 256 kbit/s, "You can't adjust music quality on the web player" [documented]. https://support.spotify.com/us/article/audio-quality/
- **Native client.** The Spotify Linux client on Flathub is x86_64-only [documented: Flathub API `arches=['x86_64']`].
- **Connect receiver.** `librespot` (v0.8.0, 2025-11-10) and `spotifyd` (v0.4.2) run on arm64 as Spotify Connect targets [reported]. They are unofficial; weigh ToS.

**Plex / Jellyfin**
- **Plex HTPC / Plex Desktop** are x86_64-only on Flathub [documented].
- **Jellyfin Desktop** (`org.jellyfin.JellyfinDesktop`, renamed from Jellyfin Media Player) ships aarch64 and x86_64 [documented, Flathub API].
- **Jellyfin web** has a TV layout option. Users report focus/scroll issues in TV mode [reported]. https://forum.jellyfin.org/t-using-jellyfin-on-tv
- **Codecs.** Chrome on this box cannot decode HEVC, AC-3 or E-AC-3 [measured], so the servers will transcode those.
- **Optional hardware decode.** `nvidia-vaapi-driver` 0.0.18 (2026-08-31) added a Chromium-compatible export path [documented README]. Requirements and flags:
  - Chrome flags: `LIBVA_DRIVER_NAME=nvidia google-chrome --enable-features=AcceleratedVideoDecodeLinuxGL,VaapiOnNvidiaGPUs --ignore-gpu-blocklist --use-gl=angle --use-angle=gl`
  - Requires `nvidia-drm.modeset=1`.
  - The README says it is "specifically designed to be used by Firefox".
  - Untested on GB10/arm64. Optional; software decode handled 720p60 with zero drops [measured].
  - https://github.com/elFarto/nvidia-vaapi-driver
  - https://ubuntuhandbook.org/index.php/2026/09/nvidia-va-api-driver-0-0-18-chromium/

**Cloud gaming**
- **GeForce NOW requirements** [documented]: https://www.nvidia.com/en-us/geforce-now/system-reqs/
  - Linux tab: "Ubuntu 24.04 LTS", "Xorg (X11) for NVIDIA GPUs", "Dual core x86 or x64 CPU", NVIDIA driver 580.126.07+.
  - Browser tab: "Chrome browser 77.x or later (Windows, macOS, or Chromebook)".
  - The Windows tab mentions "NVIDIA Grace CPU or Qualcomm Snapdragon X ARM-based processor (Beta)", which is Windows-on-Arm only.
  - Native app GA on Linux, 2026-08-13 [reported, tech-insider.org]. x86_64 only, so the Spark must use the browser.
- **Xbox Cloud Gaming browsers** (Windows, ChromeOS, Android, iOS/iPadOS, macOS; no Linux) [documented]. https://support.xbox.com/en-US/help/games-apps/cloud-gaming/supported-browsers
  - Linux Chrome works per 2021–2022 GamingOnLinux tests [reported].
  - 1440p for Ultimate reached supported browsers in 2025–26 [reported].
- **Amazon Luna.** Wikipedia (edited 2026-09-28):
  - Officially supported browsers are Chrome, Edge and Safari; other browsers can bypass the unsupported-browser error with UA spoofing.
  - In April 2026 Amazon removed previously purchased games and third-party subscriptions after 2026-06-10.
  - [documented-secondary] https://en.wikipedia.org/wiki/Amazon_Luna

---

## 4. 10-foot web UIs and navigation

### 4.1 youtube.com/tv (leanback): user agents that work

UA routing for `https://www.youtube.com/tv`, measured on the Mac on 2026-10-08 (US IP, signed out):

| UA presented | Result |
|---|---|
| `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36` (desktop) | Redirect to `https://www.youtube.com/?app=desktop&sbb=1&…` (desktop site) |
| `Mozilla/5.0 (PS4; Leanback Shell) Cobalt/25.lts.40.1035033; compatible; SparkHome/0.1` | **"YouTube on TV"** (`#/`) |
| `Mozilla/5.0 (PS4; Leanback Shell) Cobalt/19.lts.0-qa; compatible; SparkHome/0.1` | **"YouTube on TV"** |
| `Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.0) AppleWebKit/537.36 (KHTML, like Gecko) 85.0.4183.93/6.0 TV Safari/537.36` | **"YouTube on TV"** |
| `Mozilla/5.0 (Web0S; Linux/SmartTV) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36 WebAppManager` | **"YouTube on TV"** |
| Android TV Cobalt UA (`… Cobalt/25.lts.30… Starboard/15, Sony_ATV_…`) | **"YouTube on TV"** |
| `Mozilla/5.0 (Windows NT 10.0; Win64; x64; Xbox; Xbox Series X) … Edge/120.0.0.0` | **"YouTube on TV"** |

Playback and deep links:
- **Spark, Chrome 155 (with Widevine).** Both PS4 UAs played `#/watch?v=<id>` with zero dropped frames [measured].
- **Mac headless shell (no Widevine).** The Cobalt/25 UA showed "This video format is not supported", while Cobalt/19 played. This matches VacuumTube's comment that a client-side `Cobalt/19.lts.0-qa` UA keeps YouTube from assuming Widevine.
- **Recommendation [inferred].** On the Spark's Chrome either works. Use the **VacuumTube pair**: request header UA `…Cobalt/25.lts.40.1035033…` and `navigator.userAgent` `…Cobalt/19.lts.0-qa…`. If only one override is possible (`Emulation.setUserAgentOverride` sets both), Cobalt/25 is fine on the Spark [measured].
- **Search.** `https://www.youtube.com/tv#/search?q=<urlencoded>` opens results directly, even signed out [measured].
- **Watch.** `https://www.youtube.com/tv#/watch?v=<id>` first shows the account picker ("Add account / Add a kid account / Watch as guest") on a fresh profile. After choosing (two `ArrowDown` + `Enter` via CDP `Input.dispatchKeyEvent`), the watch route plays [measured]. Sign in once with the TV-code flow so the picker no longer interrupts deep links [inferred].
- **Other endpoints** (TV UA):
  - `https://www.youtube.com/tv/upg` gives the YouTube TV (live) member UI [measured].
  - `https://www.youtube.com/tv/kids` says "YouTube Kids has moved" into the main app [measured].
  - `https://music.youtube.com/tv` says "Your browser is deprecated" [measured].

**Gamepad and remote input for leanback** [documented in VacuumTube source, 2026-10-05]:
- Leanback consumes Starboard key codes. **Gamepad button *i* maps to `keyCode 32768 + i`** (standard mapping).
- VacuumTube polls `navigator.getGamepads()` every animation frame and dispatches `new Event('keydown')`/`keyup` with `keyCode` set. Synthetic, untrusted events are accepted.
- Axes are mapped to virtual buttons: left stick up/down/left/right → 17/18/19/20, right stick → 21–24, threshold 0.5. Repeat delay is 500 ms, then every 100 ms.
- Arrow keys, Enter and Escape/Backspace also work. Account-picker navigation with CDP `Input.dispatchKeyEvent` was [measured].
- Source: https://github.com/shy1132/VacuumTube (`src/index.js`, `src/preload/modules/controller-support.js`, `src/preload/util/controller.js`, `src/preload/modules/identification.js`).

**VacuumTube as an alternative YouTube tile** [documented]:
- Electron wrapper of leanback, v1.8.2, 2026-07-30.
- Ships `VacuumTube-arm64.deb`, `VacuumTube-arm64.AppImage`, and Flathub `rocks.shy.VacuumTube` (aarch64).
- Features: controller support, DIAL (phone YouTube app discovery), a deep-link CLI (`vacuumtube --fullscreen 'v=<id>'`), "Unlock Resolution", and `flags.txt` for Chromium flags.
- It also bundles ad-block and SponsorBlock modules. Those are a YouTube ToS consideration; they can be disabled.
- Electron's Widevine is not needed for YouTube.

### 4.2 Other services: is a TV web UI reachable by UA?

Measured on the Mac with a Tizen TV UA:
- `tv.twitch.tv`: DNS error.
- `app.plex.tv/tv/`: 404.
- `open.spotify.com/tv`: "Page not found".
- `crunchyroll.com/tv`: redirect to a premium error page.
- `primevideo.com`: normal desktop site.
- `netflix.com/tv`: NSES-404.

Conclusion: only the YouTube family exposes a browser-reachable TV UI.

UIs already built for controllers [reported/inferred]:
- **Xbox Cloud** (`xbox.com/play`): "Play with controller" shown on game pages [measured text].
- **GeForce NOW web** and **Luna**: the same web apps used on TVs and handhelds.
- **Jellyfin web**: TV layout setting.

### 4.3 Making desktop sites (Netflix, Disney+, Prime, Max…) usable from a couch

Options from lowest to highest effort:

1. **Pointer (air-mouse) remote plus documented keyboard shortcuts** [inferred]. This is the practical path for Netflix, Disney+, Prime Video, HBO Max and Peacock.
   - These are mouse-first desktop UIs.
   - Netflix shortcuts cover the player (section 3.2).
   - Inject a cursor auto-hide and larger-scale CSS per site (`--force-device-scale-factor` or per-tab `Emulation.setPageScaleFactor`).
2. **Chrome's built-in spatial navigation** [documented]. The `--enable-spatial-navigation` switch (`content/public/common/content_switches.cc`) lets arrow keys move focus between focusable elements on any page.
   - Expect partial results on complex SPAs [inferred]. Many tiles are not focusable, and focus can land in hidden rows.
   - Worth a 10-minute test per service.
3. **JS spatial navigation libraries.** These are for SparkHome's own launcher UI, not for third-party sites. Repo status from the GitHub API, 2026-10-08 [documented]:
   - WICG `spatial-navigation` polyfill: **archived** (pushed 2026-03-23).
   - `luke-chang/js-spatial-navigation`: last push 2024-05.
   - `NoriginMedia/Norigin-Spatial-Navigation` (React hooks): active, 2026-09-25.
   - `bbc/lrud-spatial`: active, 2026-07-09. `bbc/lrud`: archived.
4. **Agent-assisted navigation.** A local computer-use or accessibility-tree agent clicks on the user's behalf ("play Severance"). See section 5.7.

---

## 5. Chrome kiosk on Linux + CDP control plane

### 5.1 Recommended launch

Run from a GNOME autostart `.desktop` file or a systemd user unit under the GDM autologin session (`DISPLAY=:1`):

```bash
google-chrome-stable \
  --user-data-dir="$HOME/.local/share/sparkhome/chrome" \
  --kiosk --class=SparkHome \
  --no-first-run --no-default-browser-check \
  --password-store=basic \
  --autoplay-policy=no-user-gesture-required \
  --remote-debugging-port=9222 \
  --noerrdialogs \
  http://127.0.0.1:8080/        # SparkHome launcher page (tab 1)
```

Flag reference, from the Chromium switch list mirrored at peter.sh (fetched 2026-10-08) and the cited docs:

| Flag | Purpose | Source |
|---|---|---|
| `--kiosk` | Fullscreen, no browser UI. "Please note this is not Chrome OS kiosk mode." | `chrome/common/chrome_switches.h` [documented] |
| `--app=URL` (alternative) | Chromeless app window. Pair with `--start-fullscreen`. Gives more WM control but makes tab switching harder. | [documented] |
| `--user-data-dir` (non-default) | **Required** for remote debugging since Chrome 136: "These switches will no longer be respected if attempting to debug the default Chrome data directory." | https://developer.chrome.com/blog/remote-debugging-port [documented] |
| `--password-store=basic` | Avoids the "unlock your login keyring" prompt under GDM autologin, where the keyring is never unlocked. Values: `gnome-libsecret`, `kwallet{,5,6}`, `basic`. | https://chromium.googlesource.com/chromium/src/+/HEAD/docs/linux/password_storage.md [documented] |
| `--no-first-run`, `--no-default-browser-check` | Suppress first-run dialogs and the default-browser bar. | [documented] |
| `--autoplay-policy=no-user-gesture-required` | Allow autoplay of the next episode or video. Also available as policy `AutoplayAllowed`. | `media/base/media_switches.cc` [documented] |
| `--remote-debugging-port=9222` | CDP over HTTP/WebSocket on 127.0.0.1. Use a **fixed** port: port `0` sets `navigator.webdriver=true`. | `content/child/runtime_features.cc` [documented] |
| `--noerrdialogs` | Suppress error dialogs. | `base/base_switches.h` [documented] |
| `--class=SparkHome` | Sets X11 `WM_CLASS` for GNOME matching. | [documented] |
| `--force-device-scale-factor=1.25…1.5` (optional) | Bigger UI on desktop sites for 10-foot viewing. | [documented switch; value is a guess, so tune it] |
| `--enable-spatial-navigation` (optional) | Arrow-key focus navigation on all pages. | [documented switch] |

The `--password-store=basic` trade-off [inferred]:
- Cookies and passwords are encrypted with a fixed, well-known key instead of a keyring secret. Anyone with read access to the profile can lift streaming-service sessions.
- This is acceptable for a single-user TV box that already autologins.
- Pick the store once. Switching later invalidates the encrypted data.

**Do not use:**
- `--enable-automation`: shows the automation infobar and sets `navigator.webdriver`.
- `--remote-debugging-pipe` (sets `navigator.webdriver=true`), unless you accept that.
- `--remote-debugging-port=0`.
- `--headless`.
- `--disable-component-update`: freezes Widevine updates.
- `--remote-debugging-address=0.0.0.0`.
- `--disable-web-security`.
- `--disable-session-crashed-bubble`: no longer listed in Chromium's switches. `--hide-crash-restore-bubble` is documented as ChromeOS-specific.

To avoid the "Restore pages?" bubble after power loss, the common kiosk technique is to set `"exited_cleanly": true` and `"exit_type": "Normal"` in `<profile>/Default/Preferences` before launch [reported].

### 5.2 Managed policies

Policies live in `/etc/opt/chrome/policies/managed/sparkhome.json`. Creating the directory needs root once: `spark` is in the `sudo` group but has no passwordless sudo [measured].

Example:

```json
{
  "AutoplayAllowed": true,
  "TranslateEnabled": false,
  "DefaultBrowserSettingEnabled": false,
  "BackgroundModeEnabled": false,
  "CommandLineFlagSecurityWarningsEnabled": false,
  "HardwareAccelerationModeEnabled": true
}
```

All names above exist in Chromium's `components/policy/resources/templates/policies.yaml` (checked 2026-10-08) [documented]. That file also lists:
- `ExtensionInstallForcelist`, `ExtensionSettings`, `ExtensionDeveloperModeSettings`
- `RemoteDebuggingAllowed`: must stay unset or true for CDP.
- `DeveloperToolsAvailability`, `DefaultGeolocationSetting`, `FullscreenAllowed`
- `AudioCaptureAllowed`/`AudioCaptureAllowedUrls`: for a future in-browser mic.
- `LocalNetworkAccessAllowedForUrls`

There is **no** `GeolocationAllowedForUrls`. For YouTube TV's location check, grant per origin over CDP with `Browser.grantPermissions({permissions:['geolocation'], origin:'https://tv.youtube.com'})` [inferred from the CDP Browser domain], or set `DefaultGeolocationSetting`.

`CommandLineFlagSecurityWarningsEnabled: false` hides the yellow "unsupported command-line flag" bar that some flags trigger. Never let it appear on the TV.

### 5.3 Security model of the CDP port

- Anything that can connect to `127.0.0.1:9222` can drive the browser and read cookies for every signed-in service. There is no authentication [documented by the Chrome 136 rationale: attackers used remote debugging to steal cookies].
- Since Chrome 111, `--remote-allow-origins` gates WebSocket connections that carry an `Origin` header, so web pages cannot connect [documented switch]. Local non-browser processes can.
- **Decision for this single-user box [inferred]:** a fixed localhost port is acceptable. Do not proxy it to the LAN. Consider `--remote-debugging-pipe` (only the parent process can talk) if the box becomes multi-user. It makes `navigator.webdriver` true; no service is known to block on that, but this is **unknown**.

### 5.4 Extensions: `--load-extension` is gone in branded Chrome

| Option | Status | Source |
|---|---|---|
| `--load-extension` | **Removed in branded Chrome 137.** Still works in Chromium and Chrome for Testing. | PSA 2025-04-04 [documented]: https://groups.google.com/a/chromium.org/g/chromium-extensions/c/1-g8EFx2BBY/m/S0ET5wPjCAAJ |
| `--disable-extensions-except`, `--extensions-on-chrome-urls` | Removed in branded Chrome 139. One reply on 2025-09-26 says it still worked in 140 [reported]. | https://groups.google.com/a/chromium.org/g/chromium-extensions/c/FxMU1TvxWWg/m/daZVTYNlBQAJ |
| `chrome://extensions` → Developer mode → Load unpacked | Works. Persists in the profile. The officially suggested alternative. | PSA [documented] |
| **Policy force-install, self-hosted CRX** (`ExtensionInstallForcelist: ["<id>;http://127.0.0.1:8080/updates.xml"]` or `ExtensionSettings` with `override_update_url`) | **Allowed on Linux without enterprise enrollment.** The off-store block `ShouldBlockForceInstalledOffstoreExtension` / `IsLowTrustEnforcementActive()` applies only under `#if BUILDFLAG(IS_WIN) \|\| BUILDFLAG(IS_MAC)` and returns `false` elsewhere. | Chromium HEAD `chrome/browser/extensions/extension_management.cc` [documented]. Linux policy guide: https://support.google.com/chrome/a/answer/7517525 |
| CDP `Extensions.loadUnpacked(path)` | Exists. Per CDP docs it is "available if the client is connected using the --remote-debugging-pipe flag and the --enable-unsafe-extension-debugging flag is set". Puppeteer `browser.installExtension()` uses it. | Selenium CDP v147 docs [documented]; protocol JSON [documented] |

Recommended:
- **Simplest:** no extension. Do everything through CDP (5.5).
- **If an extension is wanted:** for example to survive controller restarts, or to use `declarativeNetRequest` `modifyHeaders` for UA headers. Force-install a self-hosted CRX by policy, served by the SparkHome backend. Keep the signing key stable so the ID never changes [reported: meziantou.net].

### 5.5 CDP cookbook for the launcher and agent

All commands below exist in the current protocol (`browser_protocol.json`/`js_protocol.json`, fetched 2026-10-08) [documented].

**Open a service tab:**
1. Browser endpoint: `GET http://127.0.0.1:9222/json/version` → `webSocketDebuggerUrl`.
2. `Target.createTarget({url:'about:blank'})` → `targetId`.
   - `newWindow` defaults to false, so it opens as a tab. In `--kiosk` the tab strip is hidden.
   - That visible switching works this way in kiosk mode is **unverified**; see the test plan.
3. `Target.attachToTarget({targetId, flatten:true})` → `sessionId`.
4. `Emulation.setUserAgentOverride({userAgent, platform, userAgentMetadata})`. Only for services that need it, such as YouTube leanback.
   - Overrides live for the duration of the session [inferred]. Keep the session attached, and re-apply after a controller restart.
5. `Page.addScriptToEvaluateOnNewDocument({source: bridgeJs, runImmediately:true})`. The bridge does gamepad→key mapping, the Home/Back hook and cursor hiding.
6. `Runtime.addBinding({name:'sparkhome'})`. The page calls `window.sparkhome(JSON.stringify({...}))`, and the controller receives `Runtime.bindingCalled`. No network request is needed, so Chrome 142+ **Local Network Access** prompts (public site → localhost) are avoided [documented binding semantics; LNA shipped in Chrome 142 per third-party admin notes].
7. `Page.navigate({url})`.

**Show a tab:**
- `Target.activateTarget({targetId})`. Fallback: `Page.bringToFront()` in that session.

**Before switching away:**
- Pause media. Generic: `Runtime.evaluate("document.querySelectorAll('video,audio').forEach(m=>m.pause())")`.
- Netflix: send Space with `Input.dispatchKeyEvent` [documented shortcut].

**Keys:**
- `Input.dispatchKeyEvent({type:'rawKeyDown'|'keyUp', key, code, windowsVirtualKeyCode})`. Trusted key events [measured working on leanback].

**Agent observation:**
- `Accessibility.getFullAXTree` / `queryAXTree`: compact, cheap for small models.
- `Page.captureScreenshot({format:'jpeg', quality:60})`.
- Whether DRM video frames appear in screenshots on Linux (no protected video path) is **unverified** [inferred].

**Popups / OAuth windows:**
- `Target.setDiscoverTargets({discover:true})`. Track new `page` targets and close or activate as needed.

**Close:**
- `Target.closeTarget({targetId})`.

### 5.6 Automation detection and Widevine under CDP

- `navigator.webdriver` is enabled by `--enable-automation`, `--headless`, `--remote-debugging-pipe`, or `--remote-debugging-port=0` exactly. A specific port leaves it unset "to ensure the browser behaves as it does when not under automation control" [documented]. https://chromium.googlesource.com/chromium/src/+/HEAD/content/child/runtime_features.cc
- In our Chrome 155 `--headless=new` runs with a fixed port, `navigator.webdriver` was `false` [measured]. The `--headless` mapping apparently does not reach renderers in new-headless mode [inferred]. The production kiosk is headed, so this does not matter.
- Widevine license plus decrypt plus playback works with Chrome under CDP on the Spark (Shaka test, section 1) [measured].
- No evidence was found that Netflix, Disney+, Prime Video or others check `navigator.webdriver` or CDP presence. This is **unknown**, not "safe".

### 5.7 Agent harness shape [inferred]

**Tier 1: deterministic tools** (fast, reliable, good for small models):
- `open_service(service)`, `search(service, query)` using the URL templates in section 7.
- `play_pause()`, `seek(±s)`, `back()`, `home()`.
- `youtube_play(videoId)`.
- `launch_on_companion(service, deepLink)`.

**Tier 2: AX-tree actions.**
- Find a node by role and name, then click via `DOM.getBoxModel` + `Input.dispatchMouseEvent`.
- For services without a search URL (Disney+, HBO Max) or for picking a title from results.

**Tier 3: computer-use fallback.** Screenshot plus click/keys.

**Optional, later:** Chrome's **WebMCP** (`navigator.modelContext.registerTool`). Origin trial from Chrome 149; a CDP `WebMCP` handler exists in `chrome_devtools_session.cc` [documented/reported]. It could let SparkHome's own launcher page expose its tiles as tools. Not needed now: SparkHome's backend can expose tools directly.

### 5.8 GNOME / Xorg session hygiene [inferred; standard GNOME keys]

```bash
gsettings set org.gnome.desktop.session idle-delay 0
gsettings set org.gnome.desktop.screensaver lock-enabled false
gsettings set org.gnome.desktop.interface enable-hot-corners false   # air-mouse in top-left would open Activities
gsettings set org.gnome.desktop.notifications show-banners false
```

### 5.9 Operations

- **Chrome updates every 2 weeks** [documented].
  - Have the launcher daemon compare `google-chrome-stable --version` with the running browser (`/json/version`) and restart Chrome when idle.
  - Rerun the smoke tests in section 9.
  - `apt-mark hold google-chrome-stable` is possible but delays security fixes.
- **Never disable component updates.** Widevine revocations and updates come through the component updater [measured pointer file].

---

## 6. Higher-quality alternatives

### 6.1 Kodi + inputstream.adaptive + Widevine

| Add-on | Status | Quality on Linux |
|---|---|---|
| Netflix (CastagnaIT `plugin.video.netflix`) | **Development suspended** (README warning added 2024-06-04): "THE VIDEO PLAYBACK, WHEN IT WORKS, IS LIMITED TO SD QUALITY AND ONLY ON SOME LINUX/ANDROID DEVICES". Issue #1627 open since 2023-09-20, updated 2026-06-15. Last release v1.23.5, 2025-08-24. | SD [documented] https://github.com/CastagnaIT/plugin.video.netflix |
| Amazon VOD (`plugin.video.amazon-test`) | Working again per a 2026-07-30 Spanish guide [reported]. Readme limits HD to Android/Fire TV devices [reported]. | SD-720p at best on Linux L3 [inferred] |
| SlyGuy (Disney+, HBO Max, Paramount+ …) | Third-party repo, still active per 2026 roundups [reported]. Not in the official Kodi repo. Dependency updates have broken other add-ons [reported]. | Same Widevine L3 + Linux client caps [inferred] |

Verdict [inferred]:
- No quality gain over Chrome, because the cap is the Widevine L3 Linux client policy and not the player.
- Extra ToS and fragility risk.
- Kodi itself is available for aarch64 on Flathub [documented].
- Kodi is still useful for local media and live TV (PVR) as a tile.

### 6.2 Waydroid (Android in a container)

- **GPU.** NVIDIA's proprietary driver is "non-functional" for Waydroid hardware rendering (Arch Wiki, April 2026 snapshot). The workaround is SwiftShader software rendering (`ro.hardware.egl=swiftshader`) [reported].
- **Session.** Waydroid needs a Wayland compositor; the Spark runs Xorg. It could run nested (weston/cage), but that is extra moving parts.
- **DRM.** Widevine L3 only, so Netflix gets about 480–540p [reported]. Prime Video in Waydroid looked worse than Firefox (2026-04-18) [reported]. Many apps also gate on Play Integrity [inferred].
- **Verdict:** do not pursue.

### 6.3 Companion streamer on another HDMI input, driven by SparkHome

What it buys on this **1080p** TV [inferred, from device capabilities and the per-service caps above]:

| Service | Spark Chrome (expected) | Companion (Google TV Streamer / Apple TV 4K / Fire TV) |
|---|---|---|
| Netflix | 1080p, stereo | 1080p, DD+ 5.1/Atmos (needs AVR/soundbar) |
| Disney+ / Hulu | ~480p (≤720p), stereo | 1080p, 5.1/Atmos |
| Prime Video | SD, stereo | 1080p, DD+ |
| HBO Max / Paramount+ / Apple TV | unknown, ≤720p | 1080p, Atmos where offered |
| Peacock | likely not playable | plays |

(4K and HDR would need a different TV.)

How SparkHome drives it:
- **Google TV / Android TV (e.g. Google TV Streamer):**
  - `androidtvremote2` implements the Android TV Remote v2 protocol. It needs no ADB or developer mode: one-time pairing, then key events and **"send URLs to open apps registered to handle them"**. It can also send voice audio (PCM 16-bit mono 8 kHz) [documented]. https://github.com/tronikos/androidtvremote2
  - Alternative: adb over network (developer options), e.g. `adb shell am start -a android.intent.action.VIEW -d "https://www.netflix.com/title/<id>"` [inferred]. Discover package names with `adb shell pm list packages`.
- **Apple TV 4K:** `pyatv` Companion protocol. `apps.launch_app("com.netflix.Netflix")` or `launch_app(<URL>)` for deep links ("tvOS allows deep linking into apps"). `atvremote … app_list` enumerates bundle IDs. Also power on/off and remote keys [documented].
  - https://pyatv.dev/development/apps/
  - https://pyatv.dev/documentation/atvremote/
- **Fire TV:** Fire OS (Android-based) models accept adb. Newer Vega OS models are not Android; the control path is unknown.
- **Switching the TV input:**
  - Sony Bravia REST API: `POST http://<tv>/sony/avContent`, method `setPlayContent`, `uri: "extInput:hdmi?port=N"`, header `X-Auth-PSK`. Requires IP control and a Pre-Shared Key.
  - Launch TV apps with `appControl.setActiveApp` using a URI from `getApplicationList` [documented].
    - https://pro-bravia.sony.net/develop/integrate/rest-api/spec/service/avcontent/v1_0/setPlayContent/
    - https://pro-bravia.sony.net/develop/integrate/rest-api/spec/service/appcontrol/v1_0/setActiveApp/
  - Some consumer models need Pro mode for PSK, and EU models may require HTTPS [reported].
  - The streamer's own HDMI-CEC "one touch play" switches to it when woken [inferred].
  - To return to the Spark, use the Sony REST API: the Spark has no CEC [measured]. A Pulse-Eight USB-CEC adapter plus `libcec` would add CEC [documented]. https://support.pulse-eight.com/support/solutions/articles/30000053023-cec-adapter-setting-up-libcec-on-linux
- **Check first:** if the Sony TV is itself an Android TV or Google TV set, its built-in apps are the companion, controllable by the same Sony REST `setActiveApp` or `androidtvremote2`. The TV model is **unknown**; ask the user.

---

## 7. Deep links and search URLs (voice and agent harness)

Signed-out behaviour measured from the Mac on 2026-10-08 with a desktop Chrome UA. "Signed-in behaviour" must be verified on the Spark.

| Service | Home / launch | Search URL template | Title / play URL pattern | Measured signed-out behaviour |
|---|---|---|---|---|
| Netflix | `https://www.netflix.com/browse` | `https://www.netflix.com/search?q={q}` | `https://www.netflix.com/title/{id}` (public), `…/watch/{id}` [reported] | search → `/login?nextpage=…search%3Fq%3D…`, so the target is preserved; `/title/80057281` renders publicly |
| Disney+ | `https://www.disneyplus.com/` | **none found**: `/search`, `/browse/search`, `/en-us/search?q=` all 404 signed out | `…/browse/entity-{uuid}` [reported, unverified] | 404 for in-app routes when signed out. Use the in-app search box via the agent. |
| Hulu | `https://www.hulu.com/hub/home` | `https://www.hulu.com/search?q={q}` | `…/watch/{id}`, `…/series/{slug}` [reported] | redirected to `/hub/tv?q=…` (marketing) signed out |
| HBO Max | `https://play.hbomax.com/` | `https://play.hbomax.com/search` (no query param kept) | play.hbomax.com paths [reported, unverified] | login redirect kept `returnUrl=/search` but **dropped `q`** |
| Prime Video | `https://www.primevideo.com/` | `https://www.primevideo.com/search/ref=atv_nb_sug?phrase={q}` | `https://www.primevideo.com/detail/{ASIN}` [reported] | search results render publicly |
| Apple TV | `https://tv.apple.com/` | `https://tv.apple.com/us/search?term={q}` | `https://tv.apple.com/us/show/{slug}/{id}` [reported] | results render publicly ("Severance TV Show …") |
| Paramount+ | `https://www.paramountplus.com/` | `https://www.paramountplus.com/search/?q={q}` | `/shows/{slug}/` [unverified: `/shows/yellowstone/` redirected home] | search page renders; whether `q` pre-fills is unverified |
| Peacock | `https://www.peacocktv.com/watch/home` | `https://www.peacocktv.com/watch/search?q={q}` | public `https://www.peacocktv.com/stream-tv/{slug}` | sign-in redirect preserves `return=/watch/search?q=…` |
| YouTube (TV) | `https://www.youtube.com/tv` + TV UA | `https://www.youtube.com/tv#/search?q={q}` | `https://www.youtube.com/tv#/watch?v={id}` | search works signed out; watch needs the account picker once |
| YouTube (desktop) | `https://www.youtube.com/` | `https://www.youtube.com/results?search_query={q}` | `https://www.youtube.com/watch?v={id}` | works |
| YouTube TV | `https://www.youtube.com/tv/upg` + TV UA | — | `https://tv.youtube.com/watch/{id}` [reported] | `tv.youtube.com/search?q=` → marketing/welcome page signed out |
| Spotify | `https://open.spotify.com/` | `https://open.spotify.com/search/{q}` | `https://open.spotify.com/{track\|album\|playlist\|artist}/{id}` | search renders publicly |
| Twitch | `https://www.twitch.tv/` | `https://www.twitch.tv/search?term={q}` | `https://www.twitch.tv/{channel}` | works |
| Plex | `https://app.plex.tv/desktop/` | `https://app.plex.tv/desktop/#!/search?query={q}` | — | works; adds `pivot=top` |
| Jellyfin | `http://<server>:8096/web/` | `http://<server>:8096/web/#/search.html?query={q}` [reported, verify on the user's version] | — | not measured |
| Crunchyroll | `https://www.crunchyroll.com/` | `https://www.crunchyroll.com/search?q={q}` | `/series/{id}/{slug}`, `/watch/{id}/{slug}` [reported] | search renders |
| Amazon Luna | `https://luna.amazon.com/` | `https://luna.amazon.com/search?query={q}` | game pages [reported] | works; adds `&sort=relevance` |
| GeForce NOW | `https://play.geforcenow.com/` | — | `?game-id={uuid}` [reported] | redirected to `/mall/#/loginwall` signed out |
| Xbox Cloud | `https://www.xbox.com/play` | `https://www.xbox.com/en-US/play/search?q={q}` | `https://www.xbox.com/en-US/play/games/{slug}/{productId}` | both render publicly ("Play with controller") |

Practical harness pattern [inferred]:
- **Voice → intent → title resolution.** Resolve "watch The Bear" via `tv.apple.com` search results (public, structured) or the service's own search URL, then open in the right service.
- **Services with a search URL** (Netflix, Prime, Apple TV, Peacock, YouTube, Spotify, Twitch, Plex, Crunchyroll, Luna, Xbox): open the search URL, then pick the first result via the AX tree.
- **Disney+ / HBO Max:** open home, focus the search box, type the query (AX tree), then pick a result.

---

## 8. Recommendations

1. **Control plane.** Chrome `--kiosk` with a dedicated profile and fixed-port CDP, plus a small SparkHome controller (Node 22 or Python 3.12 are already on the box). One browser, many tabs, launcher in tab 1.
   - Per-service adapters are a JSON config holding: launch URL, search template, UA override, bridge script, nav mode, pause method, and quality note.
   - Example adapter entry:
     ```json
     {"youtube": {"launch": "https://www.youtube.com/tv",
                  "ua": "Mozilla/5.0 (PS4; Leanback Shell) Cobalt/25.lts.40.1035033; compatible; SparkHome/0.1",
                  "search": "https://www.youtube.com/tv#/search?q={q}",
                  "watch": "https://www.youtube.com/tv#/watch?v={id}",
                  "nav": "leanback"}}
     ```
2. **Tiles that live in Chrome:** YouTube (leanback), YouTube TV (`/tv/upg`), Netflix (1080p), Spotify Web, Twitch, Plex/Jellyfin web, Xbox Cloud, GeForce NOW web, Luna.
3. **Tiles for Disney+/Hulu, Prime, HBO Max, Paramount+, Peacock and Apple TV.** Offer both modes:
   - **(a) open in Chrome**, with an honest quality badge ("SD on this device");
   - **(b) "play on companion"**: deep link to a Google TV Streamer or Apple TV, plus a Sony REST input switch.
   - Ask the user whether the Sony TV has built-in apps, and its model.
4. **YouTube alternative.** VacuumTube arm64 is a drop-in tile with controller and DIAL support. Use it if Chrome-tab leanback proves fiddly.
5. **Input.**
   - Chrome's Gamepad API already sees controllers via `uaccess` [measured].
   - For desktop sites, plan an **air-mouse style remote** (pointer + D-pad + OK/Back/Home).
   - Map the remote's Home to a `Runtime.addBinding` call that returns to the launcher.
6. **Avoid:** Kodi Netflix, Waydroid, UA spoofing to beat DRM (it does not work and violates ToS), `--load-extension`, `--disable-component-update`.

---

## 9. On-device test plan

Each step has an evidence gate.

1. **Kiosk + CDP sanity.** Launch the 5.1 command, then `curl -s 127.0.0.1:9222/json/version`.
   - *Gate:* returns `"Browser": "Chrome/155…"`.
2. **Tab switching in kiosk.** Create two targets and call `Target.activateTarget` alternately.
   - *Gate:* the TV shows the right tab each time. If not, try `Page.bringToFront`; if that also fails, fall back to separate `--app` windows.
3. **Headed EME probe.** Run `scratchpad/tools/emeprobe.html` from `http://127.0.0.1:<port>/` inside the kiosk session.
   - *Gate:* records `getStatusForPolicy` and robustness on the real display. Expect L3 and `output-restricted`.
4. **Per-service quality matrix.** Sign in, play a known 1080p title for 2 minutes, then via CDP `Runtime.evaluate`:
   ```js
   [...document.querySelectorAll('video')].map(v => ({w: v.videoWidth, h: v.videoHeight, t: v.currentTime, q: v.getVideoPlaybackQuality()}))
   ```
   Also read `chrome://media-internals` (resolution, codec, `video_decoder`).
   - *Gate:* a filled table for Netflix, Disney+, Prime, HBO Max, Apple TV, Paramount+, Peacock, Crunchyroll, YouTube TV, Luna, GeForce NOW, Xbox Cloud.
5. **YouTube leanback.**
   - Sign in via the TV code at youtube.com/activate.
   - Test the gamepad bridge (keyCode 32768+i) and `#/watch` deep links after sign-in.
   - Test "Link with TV code" from a phone.
   - *Gate:* the deep link plays without the picker, and the controller navigates.
6. **Autoplay, keyring, crash bubble.** Reboot and power-cut tests.
   - *Gate:* no keyring prompt, no restore bubble, the launcher appears.
7. **Optional hardware decode.** Build and install `nvidia-vaapi-driver` 0.0.18 and launch with its Chrome flags.
   - *Gate:* `chrome://media-internals` shows a VA-API decoder and lower CPU at 1080p; no regressions with DRM.
8. **Companion path (if the user buys or has a device).**
   - Pair `androidtvremote2` or `pyatv`; send a Netflix deep link; switch the TV input via Sony REST.
   - *Gate:* one SparkHome action leads to the title playing on the companion.

---

## 10. Artifacts kept for later agents

All under `<scratchpad>/tools/`:

| File | What it does |
|---|---|
| `emeprobe.html` | UA, UA-CH, `navigator.webdriver`, Widevine robustness levels, HDCP `getStatusForPolicy`, MSE codec support. Writes `out` (JSON) and sets `out.done`. Must be served from an `http://127.0.0.1` origin, not `file://` ("EME use is not allowed on unique origins") [measured]. |
| `spark_probe.mjs` | Run on the Spark with Node 22. Starts `google-chrome-stable --headless=new` with a temporary profile and fixed CDP port, loads a URL, waits in real time for `out.done`, prints JSON, cleans up. `EXTRA="--flag …"` adds Chrome flags. |
| `uatest.mjs` / `uatest_spark.mjs` | Per-tab `Emulation.setUserAgentOverride` + navigation + scripted keys (`KEYS="hash=#/watch?v=…,wait6000,ArrowDown,Enter"`) + custom `EXPR` evaluation. Mac version uses Playwright's headless shell; Spark version uses Chrome 155. |
| `urltest.mjs` | Opens a list of URLs in parallel tabs and prints final URL / title / text snippet (deep-link probing). |
| `wvtest.html` | Shaka Player 5.2.11 + Google's Widevine test stream. Proves license + decrypt + playback. |
| `innertext.mjs` | Renders JS-heavy help centres (Salesforce/Next.js) and prints `innerText`. |

---

## 11. Sources

Chrome, Chromium and Widevine:
- Google blog, 2026-03-12: https://blog.google/chromium/bringing-chrome-to-arm64-linux-devices/
- OMG! Ubuntu (Chrome arm64 + Widevine, July/Aug 2026): https://www.omgubuntu.co.uk/2026/07/chrome-arm64-linux-available
- AlternativeTo (2026-07-28): https://alternativeto.net/news/2026/7/google-chrome-launches-for-arm64-linux-with-native-widevine-drm/
- Asahi widevine-installer issue #21 (2026-08-09): https://github.com/AsahiLinux/widevine-installer/issues/21
- Chrome system requirements: https://support.google.com/chrome/a/answer/7100626
- Two-week releases: https://developer.chrome.com/blog/chrome-two-week-release and https://developer.chrome.com/blog/chrome-two-week-start
- Remote debugging change (Chrome 136): https://developer.chrome.com/blog/remote-debugging-port
- `--load-extension` removal PSA (Chrome 137): https://groups.google.com/a/chromium.org/g/chromium-extensions/c/1-g8EFx2BBY/m/S0ET5wPjCAAJ
- Chrome 139 flag removals PSA: https://groups.google.com/a/chromium.org/g/chromium-extensions/c/FxMU1TvxWWg/m/daZVTYNlBQAJ
- Chromium source (HEAD, fetched 2026-10-08):
  - `chrome/browser/extensions/extension_management.cc`
  - `chrome/browser/extensions/standard_management_policy_provider.cc`
  - `chrome/browser/media/output_protection_proxy.cc`
  - `content/child/runtime_features.cc`
  - `content/public/common/content_switches.cc`
  - `chrome/browser/devtools/protocol/extensions_handler.cc`
  - `chrome/browser/devtools/chrome_devtools_session.cc`
  - `docs/linux/password_storage.md`
  - `components/policy/resources/templates/policies.yaml`
  - all via https://chromium.googlesource.com/chromium/src/+/HEAD/…
- Off-store force-install commit (2024-04-03): https://chromium.googlesource.com/chromium/src/+/f13b7dbf238cae8f45f09ee4ecd45911dc64f055
- UA reduction: https://www.chromium.org/updates/ua-reduction/
- CDP protocol JSON: https://github.com/ChromeDevTools/devtools-protocol (`json/browser_protocol.json`, `json/js_protocol.json`)
- Extensions.loadUnpacked availability note (CDP v147): https://www.selenium.dev/selenium/docs/api/dotnet/webdriver/OpenQA.Selenium.DevTools.V147.Extensions.ExtensionsAdapter.html
- Chromium switch list mirror: https://peter.sh/experiments/chromium-command-line-switches/
- Widevine license proto (ClientIdentification): https://github.com/devine-dl/pywidevine
- nvidia-vaapi-driver: https://github.com/elFarto/nvidia-vaapi-driver and https://ubuntuhandbook.org/index.php/2026/09/nvidia-va-api-driver-0-0-18-chromium/
- WebMCP origin trial: https://developer.chrome.com/blog/ai-webmcp-origin-trial
- Local Network Access (Chrome 142, third-party admin notes):
  - https://support.vertigis.com/hc/en-us/articles/32555952561682-Local-Network-Access-permissions-in-Chrome-and-Edge
  - https://support.beyondidentity.com/hc/en-us/articles/34457691192215-Managing-the-Chrome-v142Local-Network-Access-Prompt

Services:
- Netflix:
  - https://help.netflix.com/en/node/23742
  - https://help.netflix.com/en/node/100
  - https://help.netflix.com/en/node/14163
  - https://help.netflix.com/en/node/24855
- Disney+:
  - https://help.disneyplus.com/article/disneyplus-computer-browser-requirements
  - https://help.disneyplus.com/article/disneyplus-data-usage
- LTT Disney+ Linux 480p (2026-07-19): https://linustechtips.com/topic/1640895-disney-intentionally-limits-linux-browsers-to-480p-confirmed-by-support-my-investigation/
- Hulu:
  - https://help.hulu.com/article/hulu-supported-computers
  - https://help.hulu.com/article/hulu-supported-devices
  - https://www.tvline.com/news/hulu-app-disney-plus-combining-schedule-pricing-1235485863
- HBO Max:
  - https://help.hbomax.com/us/Answer/Detail/000002506
  - https://help.hbomax.com/us/Answer/Detail/000002523
  - https://help.hbomax.com/us/Answer/Detail/000002595
- Prime Video:
  - https://www.primevideo.com/help?nodeId=GUX9FYHU5D8LC9EJ
  - https://en.linuxadictos.com/My-latest-failure-trying-to-play-Prime-Video-at-1080p-on-Linux.html
- Apple TV:
  - https://support.apple.com/guide/tvapp/get-the-apple-tv-app-and-sign-in-atvbf947803a/1.0/web/1.0
  - https://fabianwoedl.com/apple-tv-looks-bad/
- Paramount+: https://help.paramountplus.com/s/article/PD-Which-browsers-are-supported-by-Paramount
- Peacock: https://www.peacocktv.com/help/article/what-devices-and-platforms-are-supported-by-peacock
- YouTube:
  - https://support.google.com/youtube/answer/78358
  - YouTube TV: https://support.google.com/youtubetv/answer/7129767
- Spotify:
  - https://support.spotify.com/us/article/web-player-help/
  - https://support.spotify.com/us/article/audio-quality/
- GeForce NOW: https://www.nvidia.com/en-us/geforce-now/system-reqs/
- Xbox Cloud Gaming: https://support.xbox.com/en-US/help/games-apps/cloud-gaming/supported-browsers
- Amazon Luna: https://en.wikipedia.org/wiki/Amazon_Luna
- GFN on Linux browsers:
  - UA-spoof history, 2020: https://www.gamingonlinux.com/2020/09/nvidia-geforce-now-on-linux-can-run-without-user-agent-spoofing-in-a-browser
  - 1080p60 Linux cap plus Windows-UA workaround: r/linux_gaming post `1fn973k`, Sept 2024, seen via a Redlib mirror (`nyc1.lr.ggtyler.dev/r/linux_gaming/comments/1fn973k/comment/lohotg0`) [reported]
- Xbox Cloud on Linux:
  - https://gamingonlinux.com/2022/11/microsoft-upgrades-xbox-cloud-gaming-for-linux-and-chromeos
  - https://gigazine.net/gsc_news/en/20220707-xcloud-quality-linux
- VacuumTube: https://github.com/shy1132/VacuumTube (main pushed 2026-10-05; v1.8.2 2026-07-30)
- Flathub API, arches (2026-10-08): `https://flathub.org/api/v2/summary/{rocks.shy.VacuumTube,org.jellyfin.JellyfinDesktop,tv.kodi.Kodi,com.moonlight_stream.Moonlight,tv.plex.PlexHTPC,com.spotify.Client}`
- Kodi Netflix add-on: https://github.com/CastagnaIT/plugin.video.netflix
- Waydroid on NVIDIA: https://wiki.archlinux.org/title/Waydroid and https://forums.developer.nvidia.com/t/full-gpu-acceleration-on-waydroid/332769
- Companion control:
  - https://github.com/tronikos/androidtvremote2
  - https://pyatv.dev/development/apps/
  - https://pyatv.dev/documentation/atvremote/
  - https://pro-bravia.sony.net/develop/integrate/rest-api/spec/service/avcontent/v1_0/setPlayContent/
  - https://pro-bravia.sony.net/develop/integrate/rest-api/spec/service/appcontrol/v1_0/setActiveApp/
  - https://support.pulse-eight.com/support/solutions/articles/30000053023-cec-adapter-setting-up-libcec-on-linux
- Spatial navigation repos:
  - https://github.com/WICG/spatial-navigation
  - https://github.com/NoriginMedia/Norigin-Spatial-Navigation
  - https://github.com/bbc/lrud-spatial
  - https://github.com/luke-chang/js-spatial-navigation
- librespot / spotifyd: https://github.com/librespot-org/librespot and https://github.com/Spotifyd/spotifyd
