# SparkHome — on-device lab report (DGX Spark `spark`)

Lab run: 2026-10-07 23:49 → 2026-10-08 00:30, over `ssh -o BatchMode=yes -o ConnectTimeout=8 spark`.
Author: lab subagent for the SparkHome build (an Apple TV-style 10-foot launcher on the DGX Spark).

Every result below is **measured** on the user's Spark unless it is labelled otherwise
(**documented** means a primary source says so, **reported** means a secondary source says so, and **inferred** means it is my reasoning).
The command used is given with each result. The lab scripts are in
`<scratchpad>/lab/`
(`lab.mjs` = CDP client and Chrome experiments, `tools.mjs` = tool-call bench through the OpenAI-compatible endpoint, `tools2.mjs` = native-API bench with `num_ctx`, a two-turn loop and the vision test, `shots/` = screenshots copied back from the Spark).

## 0. Ground rules followed, and what changed on the Spark

- I never touched DISPLAY=:1 or the TV. Chrome ran `--headless=new` with `DISPLAY`, `WAYLAND_DISPLAY`, `XAUTHORITY` and `DBUS_SESSION_BUS_ADDRESS` unset (so no GNOME keyring prompt could appear), plus `--mute-audio` (so YouTube previews could not play sound on the TV's HDMI audio) and `--password-store=basic`.
- I made no sudo calls, apt installs, service restarts, config edits, Bluetooth actions or docker start/stop.
- I killed only my own processes, by PID: Chrome 3236420 and its children, and whisper-server 3267344. Nothing of mine was left running (checked with `pgrep`/`ss`).
- **Models pulled (they are still on disk, which the task allowed):** `qwen3:4b-instruct-2507-q4_K_M` (2.5 GB, ID 0edcdef34593) and `qwen3-vl:8b-instruct` (6.1 GB, ID 0533d74300e4), **8.6 GB total**. The Ollama store went from 778 MB to 8.9 GB. To remove them: `ollama rm qwen3:4b-instruct-2507-q4_K_M qwen3-vl:8b-instruct`. Both were unloaded afterwards (`keep_alive:0`; `/api/ps` is empty).
- `/tmp/sparkhome-lab` was deleted at the end (see §10).
- **Load during the lab.** From about 00:00, another session doing GameSpark work (`tools/steam-config.sh launch-options 435150 … launch-swap.sh`, then `steam-console.sh`) launched **Divinity: Original Sin 2** (`EoCApp.exe`) several times. It ran continuously from about 00:06 to the end of the lab, at about 20–27 % GPU, with its audio streams active on the HDMI sink. **Every GPU number below is labelled "uncontended" or "contended (DOS2 running)".**
  - The first Ollama run (§4.3, about 00:04) had no `EoCApp.exe`, though Steam was being reconfigured at the time.
  - The Chrome experiments (23:55–00:00) used software rendering only.

## 1. Headline findings

| # | Finding | Kind |
|---|---|---|
| 1 | Chrome 155.0.8059.39 arm64 in headless mode **loads the bundled Widevine CDM 4.10.3112.0** and produces a real `license-request` (1701 bytes, 99 ms after `createMediaKeys`). | measured |
| 2 | Widevine on this box is **L3 only**: `SW_SECURE_CRYPTO` and `SW_SECURE_DECODE` are OK for avc1, vp9, vp09 and av01. `HW_SECURE_DECODE` and `HW_SECURE_ALL` are rejected. HEVC is rejected at every robustness level. Persistent licenses are unsupported (the manifest says `x-cdm-persistent-license-support:false`). | measured |
| 3 | MediaCapabilities reports av1, vp9 and h264 at 1080p60 and 2160p60 as `supported=true smooth=true powerEfficient=false`, both clear and with Widevine `SW_SECURE_DECODE`. HEVC is `supported=false`. In headless mode the GPU process is SwiftShader (`video_decode: disabled_software`), so this says nothing yet about real 4K60 playback in a headful window. | measured |
| 4 | The UA string says **`X11; Linux x86_64`** on arm64 (UA reduction), while UA-CH reports `architecture:"arm", bitness:"64", platform:"Linux"`. Headless adds `HeadlessChrome/155.0.0.0`. | measured |
| 5 | CDP over Node 22's global `WebSocket` works **without `--remote-allow-origins`**, because Node sends no `Origin` header. A foreign `Origin` gets `403 Rejected an incoming WebSocket connection from the https://evil.example origin…`. | measured |
| 6 | **The YouTube leanback TV UI (`ytlr-app`, title "YouTube on TV") loads** in Chrome with a Cobalt, Tizen or webOS UA set through `Emulation.setUserAgentOverride`. The desktop UA redirects to `https://www.youtube.com/?app=desktop…`. Synthetic `ArrowDown` from `Input.dispatchKeyEvent` moved the leanback focus from "Get started" to "Terms & Privacy" (I checked this visually on the screenshots). | measured |
| 7 | Netflix `/browse` without a login redirects to `/login` with no unsupported-browser message, under both the headful-equivalent UA and the raw `HeadlessChrome` UA. Playback was not tested because I had no credentials. | measured |
| 8 | `Page.addScriptToEvaluateOnNewDocument` works, and `Input.dispatchKeyEvent` delivers ArrowRight/Down, Enter, Escape, Backspace and MediaPlayPause with `isTrusted=true`. | measured |
| 9 | Ollama 0.32.5 gave the 4B model a **262,144-token context by default, which took 42.53 GB** of unified memory and 10.9 s to load. With `num_ctx 8192` it took 3.87 GB and loaded in 3.1 s. The `/v1` endpoint cannot set `num_ctx`. | measured (+ documented: VRAM ≥48 GiB → 256k default) |
| 10 | `qwen3:4b-instruct-2507-q4_K_M` picked the correct tool for 5 of 6 test commands. It finished "search youtube for lofi beats" correctly in a **two-turn loop** (open_app → search). Uncontended warm tool-call latency was **0.40–0.60 s**, with native decode at 65–78 tok/s. | measured |
| 11 | `qwen3-vl:8b-instruct` made the same tool choices, at 0.7–1.2 s per call while DOS2 ran (about 29 tok/s). On a 1920×1080 YouTube-TV screenshot it **located the focused button almost exactly** (bbox [54,863,183,927] against my reading of [56,867,184,931]). But it **chose the wrong move** (`move down ×2` when the target was above). Its first image prompt took 6.75 s; the same image again took 1.5 s. | measured (contended) |
| 12 | whisper.cpp large-v3-turbo-q8_0 on CUDA transcribed the Piper phrase **exactly**. A resident `whisper-server` took **0.16 s** per 2.69 s utterance; a fresh `whisper-cli` process took about 0.91 s warm. Both runs were contended. | measured |
| 13 | The voice stack was moved from `~/` to `~/code/voice/` and two **absolute paths are now broken**: the `piper` entry point's shebang points at `/home/spark/voice-venv/bin/python3`, and the whisper.cpp binaries have `RUNPATH=/home/spark/whisper.cpp/build/bin`. `python -m piper` and `LD_LIBRARY_PATH=…/build/bin` work around both. | measured |
| 14 | The user `spark` can already read the gamepad (`/dev/input/js0` and `event7` carry uaccess ACLs `user:spark:rw-`) and **can write `/dev/uinput`** (an ACL from steam-launcher's `60-steam-input.rules`), without being in the `input` group. | measured |
| 15 | No capture device exists (`arecord -l` lists none, and PipeWire has no sources). The only sink is "Built-in Audio Digital Stereo (HDMI)". | measured |
| 16 | The local Steam art is 1x only (hero 1920×620, capsule 300×450, logo 640×360, header 460×215), in **two different folder layouts**. 2x art is on the Steam CDN (600×900 capsule, 1280×720 logo, and 3840×1240 hero for some apps). | measured |
| 17 | GDM autologin was configured at 2026-10-07 09:19, but the running seat0 session was started at 2026-10-06 20:05 by `gdm-password`. **Autologin at boot has not yet happened on this system.** | measured |

---

## 2. Experiment 1 — Widevine / EME / MediaCapabilities (headless Chrome 155 arm64)

### 2.1 Launch (my process; PID 3236420)
```bash
cd /tmp/sparkhome-lab
env -u DISPLAY -u WAYLAND_DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS nohup setsid google-chrome --headless=new \
  --user-data-dir=/tmp/sparkhome-lab/chrome --remote-debugging-port=9333 --mute-audio --password-store=basic \
  --no-first-run --no-default-browser-check about:blank </dev/null >/tmp/sparkhome-lab/chrome.log 2>&1 &
echo $! > chrome.pid      # wrapper does `exec -a "$0" "$HERE/chrome"`, so $! is the browser PID
curl -s http://127.0.0.1:9333/json/version
```
`/json/version` answered within about 1 s:
`"Browser": "Chrome/155.0.8059.39"`, `"Protocol-Version": "1.3"`, `"V8-Version": "15.5.35.20"`,
`"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/155.0.0.0 Safari/537.36"`.
The port was bound to `127.0.0.1:9333` only (`ss -ltnp`).

CDM on disk (`/opt/google/chrome/WidevineCdm/manifest.json`): version **4.10.3112.0**, `x-cdm-codecs: vp8,vp09,avc1,av01`,
`x-cdm-persistent-license-support: false`, encryption schemes `cenc` and `cbcs`, platforms linux x64 and **linux arm64**
(`_platform_specific/linux_arm64/libwidevinecdm.so`, 20,041,928 bytes, dated Oct 5).
After the run, the profile's `WidevineCdm/latest-component-updated-widevine-cdm` read
`{"LastBundledVersion":"4.10.3112.0","Path":"/opt/google/chrome/WidevineCdm"}`, so the bundled CDM was the one used.

### 2.2 Probe (`node lab.mjs eme`, run on https://example.com, `isSecureContext=true`)

`requestMediaKeySystemAccess('com.widevine.alpha', …)`. The audio capability was `mp4a.40.2` with `SW_SECURE_CRYPTO`; the webm row had no audio.

| robustness \ video | mp4 avc1.640028 | webm vp9 | mp4 vp09.00.10.08 | mp4 av01.0.08M.08 | mp4 hvc1.1.6.L120.90 |
|---|---|---|---|---|---|
| SW_SECURE_CRYPTO | OK | OK | OK | OK | NotSupportedError |
| SW_SECURE_DECODE | OK | OK | OK | OK | NotSupportedError |
| HW_SECURE_DECODE | NotSupportedError | NotSupportedError | NotSupportedError | NotSupportedError | NotSupportedError |
| HW_SECURE_ALL | NotSupportedError | NotSupportedError | NotSupportedError | NotSupportedError | NotSupportedError |
| "" (empty) | OK | OK | OK | OK | NotSupportedError |

- Every granted configuration came back with `persistentState=not-allowed` and `distinctiveIdentifier=not-allowed`.
- `sessionTypes:['persistent-license']` gave NotSupportedError.
- Other key systems: `org.w3.clearkey` was OK. `com.microsoft.playready.recommendation`, `com.microsoft.playready`, `com.apple.fps.1_0` and `com.widevine.alpha.experiment` were all NotSupportedError.
- **The CDM really runs.** `createMediaKeys()` took 13 ms. `createSession('temporary').generateRequest('cenc', <synthetic Widevine PSSH, key_id 0x11×16>)` fired a `message` event with `messageType=license-request`, 1701 bytes, 99 ms after `createMediaKeys` started.
- `MediaKeys.getStatusForPolicy({minHdcpVersion:'1.0'})` returned **`output-restricted`**. This was in headless mode, where no display exists. **It has to be re-measured in a headful window on the HDMI TV before anyone concludes anything about HDCP.**

`navigator.mediaCapabilities.decodingInfo({type:'media-source', video:{…, framerate:60}})`. The encrypted rows add
`keySystemConfiguration:{keySystem:'com.widevine.alpha', video:{robustness:'SW_SECURE_DECODE'}}`.

| codec (contentType) | 1080p60 clear | 1080p60 Widevine SW_SECURE_DECODE | 2160p60 clear | 2160p60 Widevine |
|---|---|---|---|---|
| AV1 `av01.0.09M.08` / `av01.0.13M.08` | supported, smooth, !powerEfficient | same | same | same |
| VP9 `vp09.00.41.08` / `vp09.00.51.08` (webm) | supported, smooth, !powerEfficient | same | same | same |
| H.264 `avc1.64002A` / `avc1.640034` | supported, smooth, !powerEfficient | same | same | same |
| HEVC `hvc1.1.6.L123.B0` / `L153.B0` | **supported=false** | false | false | false |

`MediaSource.isTypeSupported` agreed: av1, vp9 and h264 true; hevc false.

**UA and client hints**:
- `navigator.userAgent` = `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/155.0.0.0 Safari/537.36`. The headful string will be the same with `Chrome/` in place of `HeadlessChrome/`, and the platform is frozen at `x86_64`.
- `userAgentData.getHighEntropyValues` = `architecture:"arm"`, `bitness:"64"`, `platform:"Linux"`, `platformVersion:""`, `model:""`, `formFactors:["Desktop"]`, `wow64:false`, and `fullVersionList` Google Chrome / Chromium 155.0.8059.39.

**GPU in headless** (`SystemInfo.getInfo`): ANGLE on **SwiftShader** (`gl=egl-angle,angle=swiftshader`). `gpu_compositing`, `rasterization`, `video_decode` and `video_encode` are all `disabled_software`, and `videoDecoding: []`. Headless did not use the GB10 GPU. The log also says "Automatic fallback to software WebGL has been deprecated".
Consequence (inferred): smooth/powerEfficient here are software-path claims only. Headful playback performance on the TV, including CPU load for 4K60 AV1 and dropped frames, is **unmeasured**.

Background (documented/reported):
- Google announced ARM64 Linux Chrome for Q2 2026 and named DGX Spark ("Google is partnering with NVIDIA to make it easier for DGX Spark users to install Chrome"), in a blog post dated 2026-03-12.
- Secondary reports from July–August 2026 say arm64 builds ship a native `linux_arm64` Widevine. They also say Linux stays at software-secure DRM, so services such as Netflix are limited to about 720p/1080p with no 4K/HDR.
- Netflix's help page has listed Chrome on Linux at 720p (reported; it is not re-verified here).

---

## 3. Experiment 2 — CDP control feasibility

### 3.1 WebSocket and Origin (`node lab.mjs origin`)
- I logged the upgrade request that Node 22.23.1's global `WebSocket` (undici) sends to a local test server. It carries **no `Origin` header**; the `user-agent` is `node`, and the other headers are host, connection, upgrade, sec-websocket-key/version/extensions, accept, accept-language, sec-fetch-mode, pragma, cache-control and accept-encoding.
- Connecting to `ws://127.0.0.1:9333/devtools/browser/<id>` with no flags **worked**.
- The same connection with `new WebSocket(url, {headers:{Origin:'https://evil.example'}})` was **rejected**. A raw HTTP upgrade probe returned
  `403 Rejected an incoming WebSocket connection from the https://evil.example origin. Use the command line flag --remote-allow-origins=https://evil.example to allow connections from this origin or --remote-allow-origins=* to allow all origins.`
- So a launcher backend written in Node or Python can drive Chrome without `--remote-allow-origins`, and web pages cannot reach the port by default. **Do not add `--remote-allow-origins=*`.** (inferred recommendation)
- Since Chrome 136, `--remote-debugging-port` is ignored on the **default** profile directory, so a non-default `--user-data-dir` is required (documented: developer.chrome.com/blog/remote-debugging-port, 2025-03-17). A dedicated SparkHome profile satisfies this.

### 3.2 YouTube leanback under TV UAs (`node lab.mjs tv`)
Method: open a new target with `Target.createTarget` and `Target.attachToTarget{flatten:true}`, then `Emulation.setDeviceMetricsOverride` (1920×1080, DPR 1) and `Emulation.setUserAgentOverride`. Navigate to `https://www.youtube.com/tv`, wait for the load event, then wait 12 s.

| UA label | UA string | final URL | title | `ytlr-*` elements | screenshot |
|---|---|---|---|---|---|
| cobalt | `Mozilla/5.0 (X11; Linux aarch64) Cobalt/25.lts.30.1034943-gold (unlike Gecko) v8/8.8.278.17-jit gles Starboard/15, SparkHome_GB10_2026/1.0 (SparkHome, Spark, Wired)` | `https://www.youtube.com/tv#/` | YouTube on TV | 56 (+53 `yt-*`), `ytlr-app` present | 1920×1080 PNG, 966,389 B; animated 3-D art background |
| tizen | `Mozilla/5.0 (SMART-TV; LINUX; Tizen 7.0) AppleWebKit/537.36 (KHTML, like Gecko) 94.0.4606.31/7.0 TV Safari/537.36` | same | YouTube on TV | 53, `ytlr-app` present | 54,137 B; plain black background |
| webos | `Mozilla/5.0 (Web0S; Linux/SmartTV) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.128 Safari/537.36 WebAppManager` | same | YouTube on TV | 56, `ytlr-app` present | 54,137 B; plain black background |
| desktop control | `Mozilla/5.0 (X11; Linux x86_64) … Chrome/155.0.0.0 Safari/537.36` | `https://www.youtube.com/?app=desktop&sbb=1&feature=…` | YouTube | 0 `ytlr`, 78 `ytd` | desktop site |

- Signed out, all three TV UAs showed the onboarding screen: "Watch the latest videos, just for you / Anyone can add an account to get their YouTube favorites / Get started / Terms & Privacy".
- **Key navigation works.** I sent `ArrowDown` (vk 40) and then `ArrowRight` ×2 (vk 39) through `Input.dispatchKeyEvent` (`rawKeyDown` + `keyUp`). The white focus pill moved from **Get started** to **Terms & Privacy** (compare `shots/small-yt-cobalt.png` with `small-yt-cobalt-after-keys.png`, and the tizen pair). My DOM probe (`.zylon-focus, [focused], :focus`) only ever matched `ytlr-app`, so **leanback focus is not exposed as DOM focus**. Use screenshots or vision, or keep your own focus model.
- Accessibility tree (`Accessibility.getFullAXTree`, cobalt UA): 78 nodes. The only non-ignored roles were RootWebArea 1, generic 26, StaticText 3, button 1 ("Get started") and InlineTextBox 5. "Terms & Privacy" is focusable but **has no button role**. The AX tree alone is therefore not enough for an agent to drive leanback.
- Screenshot latency at 1920×1080 in headless/SwiftShader: `Page.captureScreenshot` PNG took 250–273 ms (5 runs), and JPEG q70 took 39–51 ms.
- I did not attempt to sign in.

### 3.3 Netflix without a login (`node lab.mjs netflix`)
| UA | final URL | title | visible text | unsupported-browser text |
|---|---|---|---|---|
| headful-equivalent `Mozilla/5.0 (X11; Linux x86_64) … Chrome/155.0.0.0 Safari/537.36` | `https://www.netflix.com/login?nextpage=https%3A%2F%2Fwww.netflix.com%2Fbrowse&serverState=…` | Netflix | "Enter your info to sign in … This page is protected by Google reCAPTCHA…" | **none** |
| raw `HeadlessChrome/155.0.0.0` | same redirect | Netflix | same | **none** |

The page took 1.9–2.0 s to load. I could not measure playback, the M7xxx error codes or the resolution cap without an account.

### 3.4 Injection and synthetic keys (`node lab.mjs inject`)
I registered the following with `Page.addScriptToEvaluateOnNewDocument`, then navigated to https://example.com. The result was `window.__sh.injected === true`.
```js
window.__sh={injected:true,keys:[]};addEventListener('keydown',e=>window.__sh.keys.push([e.key,e.code,e.keyCode,e.isTrusted]),true);
```
Events delivered by `Input.dispatchKeyEvent` (`rawKeyDown`/`keyUp` with `windowsVirtualKeyCode`):
`["ArrowRight","ArrowRight",39,true]`, `["ArrowDown","ArrowDown",40,true]`, `["Enter","Enter",13,true]`,
`["Escape","Escape",27,true]`, `["Backspace","Backspace",8,true]`, `["MediaPlayPause","MediaPlayPause",179,true]`. All six had **`isTrusted: true`**.

### 3.5 Minimal CDP client used (Node 22, no dependencies)
```js
const v = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();
const ws = new WebSocket(v.webSocketDebuggerUrl);            // no Origin sent → accepted
// send(method, params, sessionId) → {id, method, params, sessionId}; match replies on id; events carry sessionId
// page: Target.createTarget{url:'about:blank'} → Target.attachToTarget{targetId, flatten:true} → Page.enable, Runtime.enable
// eval: Runtime.evaluate{expression, awaitPromise:true, returnByValue:true}
```
The full file is `lab.mjs` (subcommands `eme|origin|tv|netflix|inject|ax`).

---

## 4. Experiment 3 — Ollama tool calling (local agent harness)

### 4.1 State and pulls
- `ollama --version` gave **0.32.5** (`/api/version` agrees). It runs as the `ollama.service` system service on `127.0.0.1:11434`, and its environment does not set `OLLAMA_CONTEXT_LENGTH`.
- Before the lab, `ollama list` showed only `gemma3:1b` (815 MB). It cannot do tools: `/v1/chat/completions` with `tools` returned HTTP 400 `registry.ollama.ai/library/gemma3:1b does not support tools`.
- Tags chosen from ollama.com/library (checked on 2026-10-08):
  - `qwen3:4b-instruct-2507-q4_K_M` (2.5 GB, 256K context). The `qwen3` tag list also has `-q8_0` (4.3 GB) and `-fp16` (8.1 GB).
  - `qwen3-vl:8b-instruct` (6.1 GB). The library page lists **vision, tools, thinking**, says it requires Ollama ≥0.12.7, and claims GUI-agent ability.
- `ollama pull qwen3:4b-instruct-2507-q4_K_M` took 74.3 s wall. `ollama show` reports qwen3, 4.0B, Q4_K_M, context 262144, capabilities tools/thinking/completion, temperature 0.7, top_k 20, top_p 0.8.
- `ollama pull qwen3-vl:8b-instruct` took 201.7 s wall. `ollama show` reports qwen3vl, 8.8B, Q4_K_M, context 262144, capabilities completion/vision/tools, temperature 1, top_k 20, top_p 0.95.

### 4.2 Harness used
The six tools were:
- `open_app(name)`
- `move(direction ∈ {up,down,left,right}, count ≥1)`
- `select()`
- `back()`
- `home()`
- `search(query)`, described as "inside the app that is currently open"

System prompt (about 540 prompt tokens including the tools):
"You are the remote-control assistant for SparkHome, a TV home screen. Tiles: Netflix, YouTube, Disney+, Prime Video, Plex, Spotify, Steam, Cyberpunk 2077, Red Dead Redemption 2, Rise of the Tomb Raider, Divinity: Original Sin 2. Carry out the request with tool calls, several in order when needed. If no tool fits, answer in one short sentence without calling tools."

All requests used temperature 0, `reasoning_effort:"none"` on `/v1` and `think:false` on `/api/chat`; neither was rejected. TTFT is the time to the first SSE chunk carrying content or `tool_calls`. **Ollama sends a tool call as one complete chunk, so for tool calls TTFT equals total latency.**

### 4.3 Results — `qwen3:4b-instruct-2507-q4_K_M` via `/v1/chat/completions` (default context), **uncontended**
`node tools.mjs qwen3:4b-instruct-2507-q4_K_M 3`
- Cold load: the model was unloaded, then loaded with an empty `/api/generate`. That took **10,880 ms** wall; `load_duration` came back as 0, so wall time is the number.
- `/api/ps`: **size_vram 42.53 GB, context_length 262144**.

| command | tool call(s) (identical across 3 reps) | correct | rep1 ms | warm median ms (reps 2–3) |
|---|---|---|---|---|
| open netflix | `open_app{"name":"Netflix"}` | 3/3 | 669 | 445 |
| go back | `back{}` | 3/3 | **3951** (outlier with no isolated cause; at that moment another session was running `steam-config.sh launch-options`/`steam-console.sh`) | 400 |
| play cyberpunk | `open_app{"name":"Cyberpunk 2077"}` | 3/3 | 549 | 579 |
| search youtube for lofi beats | `open_app{"name":"YouTube"}` only | 0/3 single-turn (no `search`) | 493 | 502 |
| move right twice | `move{"direction":"right","count":2}` | 3/3 | 538 | 600 |
| what's on my GPU? | no tool; text "I cannot provide information about what's on your GPU as that requires system-level access…" | 3/3 | TTFT 156 / total 583 | TTFT 203 / total 631 |

Native `/api/chat` (`think:false`), one call per command, same session:
- prompt_eval_count 537–541 tokens with prompt_eval 17–24 ms. That is most likely prefix-cache reuse (inferred).
- eval 15–31 tokens with **decode 64.9–77.6 tok/s**.
- Wall time 418–696 ms.

### 4.4 `qwen3:4b-instruct-2507-q4_K_M` via native `/api/chat` with `options.num_ctx=8192`, **contended (DOS2 running)**
`node tools2.mjs qwen3:4b-instruct-2507-q4_K_M 8192`
- Cold load: **3,116 ms**. `/api/ps`: **3.87 GB, context 8192**. That is 11× less memory than the default.
- Wall time per command over 3 reps:
  - open netflix: 776/621/588
  - go back: 525/495/486
  - play cyberpunk: 757/750/772
  - search youtube…: 613/608/605
  - move right twice: 726/718/744
  - GPU question: 850/846/819
- The tool choices were identical to §4.3. Decode was **47–50 tok/s**, against 65–78 uncontended. The drop is probably GPU contention from DOS2 (inferred; I did not isolate it).
- **Two-turn loop** for "search youtube for lofi beats". The tool result fed back was "YouTube is now open and showing its home screen."
  - step 0: `open_app{"name":"YouTube"}` (615 ms)
  - step 1: `search{"query":"lofi beats"}` (729 ms)
  - step 2: text "Search for "lofi beats" on YouTube is complete." (603 ms)
  - The complete task took 1.95 s over three model calls.

### 4.5 `qwen3-vl:8b-instruct`, native `/api/chat` `num_ctx=8192`, **contended (DOS2 running at about 22 % GPU)**
`node tools2.mjs qwen3-vl:8b-instruct 8192 /tmp/sparkhome-lab/yt-cobalt-after-keys.png`
- Cold load: **4,001 ms**. `/api/ps`: **6.54 GB, context 8192**.
- Tool choices were the same as the 4B model on all six commands, and its GPU answer was "I can't check your GPU status as there is no tool available for that."
- Wall time per command over 3 reps:
  - open netflix: 1171/914/930
  - go back: 708/748/758
  - play cyberpunk: 1187/1156/1154
  - search youtube: 895/940/875
  - move right twice: 1102/1080/1107
  - GPU question: 852/799/832
- Decode: **28.5–29.3 tok/s**.
- Two-turn loop: open_app(YouTube) in 903 ms, then search("lofi beats") in 980 ms, then a text answer in 991 ms.
- **Vision on the 1920×1080 leanback screenshot** (focus on "Terms & Privacy"):

| test | run | wall ms | prompt tokens / prompt-eval ms | output | correct? |
|---|---|---|---|---|---|
| grounding ("which button has the focus pill? JSON + 0-1000 bbox") | 1 | 6750 | 2113 / 5363 | `{"label":"Terms & Privacy","bbox":[54,863,183,927]}` | **yes**; my reading from pixels is about [56,867,184,931] |
| grounding | 2 (same image) | 1512 | 2113 / 32 (cached) | identical | yes |
| next action ("Goal: press Get started", tools move/select/back/home) | 1 | 3784 | 2383 / 2659 | `move{"direction":"down","count":2}` | **no**; Get started is *above*, so `move up` is right |
| next action | 2 | 1195 | 2383 / 33 | identical | no |

### 4.6 Context-length behaviour (documented and measured)
- docs.ollama.com/context-length: "< 24 GiB VRAM: 4k context; 24-48 GiB VRAM: 32k context; >= 48 GiB VRAM: 256k context". The override is `OLLAMA_CONTEXT_LENGTH=… ollama serve`.
- The GB10 reports 124,609 MiB "VRAM" (unified), so every model gets 256k by default. That cost 42.5 GB for a 4B model, as measured.
- The OpenAI-compatible `/v1` endpoint takes no `num_ctx`, and its `tool_choice` is listed as unsupported (documented: docs.ollama.com/api/openai-compatibility).
- Three ways to bound memory:
  - (a) Use native `/api/chat` with `options.num_ctx`. This needs no system change, but every request must pass the same value or the model reloads.
  - (b) Build a Modelfile variant (`FROM qwen3:4b-instruct-2507-q4_K_M` + `PARAMETER num_ctx 8192`).
  - (c) Set `OLLAMA_CONTEXT_LENGTH` in the service. That is a system config change and needs the user's go-ahead.

---

## 5. Experiment 4 — voice: Piper TTS → whisper.cpp STT

### 5.1 Broken paths (measured)
- `~/code/voice/voice-venv/bin/piper` fails with "cannot execute: required file not found". Its shebang is `#!/home/spark/voice-venv/bin/python3`, and `~/voice-venv` does not exist.
- `whisper-cli` fails with "error while loading shared libraries: libwhisper.so.1". `readelf -d` shows `RUNPATH [/home/spark/whisper.cpp/build/bin:]`, and `~/whisper.cpp` does not exist; the libraries are in `~/code/voice/whisper.cpp/build/bin`.
- Workarounds used: `~/code/voice/voice-venv/bin/python -m piper …` and `LD_LIBRARY_PATH=$HOME/code/voice/whisper.cpp/build/bin`. Permanent fixes:
  - Rebuild whisper.cpp in its new location, or build with `-DCMAKE_BUILD_RPATH`/`$ORIGIN`.
  - Re-create the venv entry points (`pip install --force-reinstall piper-tts` inside the venv).

### 5.2 Synthesis (CPU)
- Versions: piper-tts **1.6.0**, onnxruntime 1.28.0 (providers: Azure and CPU only, no CUDA). The voice is `en_US-lessac-medium` at 22,050 Hz.
- `echo "open netflix and search for stranger things" | ~/code/voice/voice-venv/bin/python -m piper -m ~/code/voice/voice-voices/en_US-lessac-medium.onnx -f tts22k.wav` took **1.02 s wall**, including Python start and model load. It produced 2.69 s of 16-bit mono 22,050 Hz audio.
- I resampled to **16 kHz mono** with stdlib `audioop.ratecv` (Python 3.12; ffmpeg and sox are not installed). The result is 86,236 bytes and 2.69 s long.

### 5.3 Transcription (GPU, CUDA0 GB10 cc 12.1; build `CUDA ARCHS = 1210 | BLACKWELL_NATIVE_FP4 = 1`, flash-attn on by default) — **contended (DOS2 running at about 21 % GPU)**
`LD_LIBRARY_PATH=… whisper-cli -m ggml-large-v3-turbo-q8_0.bin -f tts16k.wav -l en -nt -np`

| run | wall | transcript |
|---|---|---|
| 1 (first) | 1.739 s | "Open Netflix and search for Stranger Things." |
| 2 | 0.907 s | same |
| 3 | 0.918 s | same |
| 4 | 0.928 s | same |

Internal timings from one run without `-np`:

| model size | load | mel | encode | decode | batchd | total |
|---|---|---|---|---|---|---|
| 873.55 MB | 228.18 ms | 3.54 ms | 200.34 ms | 4.72 ms | 37.12 ms / 47 runs | **506.62 ms** |

**Resident server.** I started `whisper-server -m … --host 127.0.0.1 --port 8971 -l en` (my PID 3267344, killed afterwards). It was ready in 0.52 s. Each request was `curl -F file=@tts16k.wav -F response_format=json -F temperature=0.0 http://127.0.0.1:8971/inference`:

| request | time |
|---|---|
| 1 | 0.259 s |
| 2 | 0.161 s |
| 3 | 0.160 s |
| 4 | 0.159 s |

Every response was `{"text":" Open Netflix and search for Stranger Things.\n"}`. The server log gave 147.94 ms per encode. Its `--convert` option (non-WAV input) needs ffmpeg, which is not installed.

### 5.4 Models present
| file | size |
|---|---|
| `~/code/voice/whisper.cpp/models/ggml-large-v3-turbo-q8_0.bin` | 874,188,075 B |
| `for-tests-*.bin` | under 1 MB each; test fixtures only (tiny/base/small/medium/large stubs, parakeet-tdt stubs, silero-v6.2.0 VAD stub) |

- No real VAD model and no Parakeet model is present, although this whisper.cpp 1.9.2 build includes `parakeet-cli` and `libparakeet.so.1.9.2`.
- Also on disk (not whisper): another app's model files, about 88 GB.

---

## 6. Experiment 5 — inventory (read-only)

### 6.1 Binaries (`command -v`)
| present | missing |
|---|---|
| xprop, xwininfo, xinput, xrandr, xset, arecord, node (`/usr/local/bin/node` → `/opt/node-v22.23.1-linux-arm64/bin/node`, a tarball install; npm 10.9.8), google-chrome(-stable) → `/opt/google/chrome/google-chrome` 155.0.8059.39, firefox (snap 154.0-1), ollama, docker, steam, python3 3.12.3, getfacl | ffmpeg, xdotool, wmctrl, evtest, jstest, piper (on PATH), ydotool, wtype, gamescope, cage, sway, sox, pactl |

- `apt-cache policy` (none installed): python3-evdev 1.7.0+dfsg-1build1, xdotool 1:3.20160805.1-5build1, wmctrl 1.07-7ubuntu3, ydotool 0.1.8-3build1, all candidates from noble/universe arm64.
- `~/gamespark/tools/xinput.py` already provides XTest key, click and window activation through ctypes (libX11/libXtst).
- `~/gamespark/tools/gamepad.py` already provides a uinput virtual Xbox 360 pad driven through a FIFO.

### 6.2 Input devices and permissions
- Bluetooth devices from `/proc/bus/input/devices`: "Xbox Wireless Controller" (Bus 0005, 045e:0b13, via uhid, handlers `kbd event7 js0`).
- The rest are ACPI Power Button (event0), Lid Switch (event1), Sleep Button (event2) and NVIDIA HDMI/DP jacks (event3–6). **No keyboard or mouse is attached.**
- Paired (`bluetoothctl devices Paired`): **DualSense Wireless Controller 02:00:00:00:00:5F** and **Xbox Wireless Controller 02:00:00:00:00:B2**.
- `getfacl` results:

| node | ACL |
|---|---|
| event0–6 | `root:input` 0660, no ACL. spark cannot read the power button. |
| **event7, js0** (Xbox) | `user:spark:rw-` (uaccess; udev tags `uaccess … seat`; `ID_INPUT_JOYSTICK=1`). js0 is also `other::r--`. |
| **/dev/uinput** | `crw-rw----+ root root`, **`user:spark:rw-`** (rule `KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess"` from `/lib/udev/rules.d/60-steam-input.rules`, package **steam-launcher**). |
| /dev/hidraw0 | root only |

- `/etc/bluetooth/main.conf` (non-default lines): `Privacy = device`, `JustWorksRepairing = always`, `[LE] MinConnectionInterval=7`, `MaxConnectionInterval=9`, `ConnectionLatency=0`, `[Policy] AutoEnable=true`. `/etc/bluetooth/input.conf` has only `[General]`, all defaults.
- `bluetoothctl show`: controller 02:00:00:00:00:FD, manufacturer 0x0046 (MediaTek), roles central and peripheral, 16 advertising instances, secondary channels 1M/2M/Coded, Pairable: no, Discoverable: no.
- Kernel 6.17.0-1029-nvidia config:
  - `CONFIG_RC_CORE=m`, `LIRC=y`, `IR_NEC_DECODER=m`, `IR_RC6_DECODER=m`, `IR_MCEUSB=m`, `IR_STREAMZAP=m`, `IR_IGUANA=m`, `IR_IMON=m`, `IR_TOY=m`
  - `HID_APPLEIR=m` (Apple's USB IR receiver), `HID_APPLE=m`, `UHID=m`, `INPUT_UINPUT=y`, `HID_PLAYSTATION=m`, `HID_NINTENDO=m`, `HID_STEAM=m`, `JOYSTICK_XPAD=m`, `BT_HIDP=m`, `HID_BPF=y`
  - There are 138 rc keymap modules and **none of them is an Apple remote keymap**. `ir-keytable`/v4l-utils is not installed (no `/lib/udev/rc_keymaps`).
  - Loaded modules include hid_playstation, xpad, hid_microsoft, uhid, hidp, joydev and btusb/btmtk, plus mt7925e for Wi-Fi.

### 6.3 Desktop and session
- gsettings (`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`):

| key | value |
|---|---|
| org.gnome.desktop.session idle-delay | `uint32 0` |
| org.gnome.desktop.screensaver lock-enabled | `true` |
| org.gnome.desktop.screensaver idle-activation-enabled | `true` |
| org.gnome.desktop.notifications show-banners | **`true`** (banners would pop over a kiosk) |
| org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type | `'nothing'` |
| …sleep-inactive-ac-timeout | 3600 |
| …idle-dim | true |
| org.gnome.desktop.a11y.applications screen-keyboard-enabled | false |
| org.gnome.mutter experimental-features | `@as []` |
| org.gnome.desktop.interface enable-animations | true |

- **Session**: seat0 session 5, `Type=x11`, `Service=gdm-password`, started Tue 2026-10-06 20:05:35. `/etc/gdm3/custom.conf` has `AutomaticLoginEnable=true` and `AutomaticLogin=spark`, last modified **2026-10-07 09:19**. That is later than the session start, so autologin has not been exercised by a boot yet.
- DRM sysfs: `card0-Unknown-1 connected modes=1920x1080` (one mode).
- Audio (`wpctl status`): the only sink is `46. Built-in Audio Digital Stereo (HDMI)` at volume 0.88. **There are no sources.** `arecord -l` lists no capture hardware, and `pactl` is not installed.
- Running user services (names only):
  - at-spi-dbus-bus, dbus, dconf, evolution-addressbook-factory, evolution-calendar-factory, evolution-source-registry, filter-chain
  - **gamespark-governor**
  - gcr-ssh-agent, gnome-keyring-daemon, gnome-session-manager@ubuntu, gnome-session-monitor, gnome-terminal-server
  - gvfs-afc/daemon/goa/gphoto2/metadata/mtp/udisks2 volume monitors
  - obex, org.freedesktop.IBus.session.GNOME, 15× org.gnome.SettingsDaemon.* (A11ySettings, Color, Datetime, Housekeeping, Keyboard, MediaKeys, Power, PrintNotifications, Rfkill, ScreensaverProxy, Sharing, Smartcard, Sound, Wacom, XSettings), org.gnome.Shell@x11
  - pipewire, pipewire-pulse, snap.snapd-desktop-integration, speech-dispatcher, tracker-miner-fs-3
  - wireplumber, the xdg-desktop-portal(-gnome/-gtk), xdg-document-portal, xdg-permission-store
  - Failed: update-notifier-crash.
- GNOME autostart (`~/.config/autostart`):
  - a third-party dictation app
  - `gamespark-console.desktop` (`env GAMESPARK_STEAM=snap /home/spark/gamespark/tools/steam-console.sh`, `X-GNOME-Autostart-Delay=5`)
- **GameSpark is deployed** at `~/gamespark` (a real directory) with `lib/env.sh` (4,690 B, dated Oct 7 21:27). Its helpers can be reused:
  - `steam_cmd` (writes to `$STEAM_PIPE`)
  - `steam_launch APPID` (confirms through the `ExecCommandLine` line in `console_log.txt`, retries 4×)
  - `fast_cpus` (taskset list of the X925 cores)
  - `as_root` (sudo, or a privileged docker nsenter)
  - The default `DISPLAY=:1` and `XAUTHORITY=/run/user/1000/gdm/Xauthority`
- `steam-console.sh` waits up to 180 s for steamwebhelper and the pipe, then sleeps 20 s because "Steam ignores pipe commands until 'Loading user data' finishes", then sends `steam://open/bigpicture`.
- Steam pipe: `prw------- spark ~/snap/steam/common/.steam/steam.pipe`.
- `controller/README.md` records the Xbox BLE fix and its quirks: holding the pair button wipes the bond, the MT7925 sometimes stops hearing devices until power off/on, and USB-C always works.
- Docker: one unrelated postgres container was running. The vLLM containers were not running.
- Snaps: bare 1.0, core22 20260824, core24 20260410, firefox 154.0-1, firmware-updater, gaming-graphics-core24 25.3.1~kisak1 (kisak-fresh/stable), gnome-42-2204, gnome-46-2404, gtk-common-themes, mesa-2404 25.2.8, snap-store, snapd 2.77.1, snapd-desktop-integration, **steam 1.0.0.85 rev 245**, thunderbird 154.0-2.

---

## 7. Experiment 6 — Steam library art

Installed games, from `appmanifest_*.acf` `name` and `SizeOnDisk`:

| appid | name | size on disk |
|---|---|---|
| 1091500 | Cyberpunk 2077 | 91.2 GB |
| 1174180 | Red Dead Redemption 2 | 128.3 GB |
| 391220 | Rise of the Tomb Raider | 28.4 GB |
| 435150 | Divinity: Original Sin 2 | 63.1 GB |

Tools also installed: 1070560 SLR 1.0, 1493710 Proton Experimental, 228980 Steamworks Redist, 4183110 SLR 4.0. There is one library folder, `/home/spark/snap/steam/common/.local/share/Steam`.

`~/snap/steam/common/.local/share/Steam/appcache/librarycache/` has an appid folder for every game in the library, so art is cached for all of them, not just installed games. Pixel sizes below were read from the JPEG SOF and PNG IHDR headers with python3 (`imgsize.py`).

| appid | file (relative to librarycache) | px | bytes |
|---|---|---|---|
| 1091500 | `1091500/cf8cec80…2652/library_hero.jpg` | 1920×620 | 222,935 |
| 1091500 | `1091500/cf8cec80…2652/library_hero_blur.jpg` | 192×62 | 2,874 |
| 1091500 | `1091500/37680a27…d845/logo.png` | 640×360 | 83,453 |
| 1091500 | `1091500/6399de67…a235/library_capsule.jpg` | 300×450 | 59,559 |
| 1091500 | `1091500/812a216d…a9c/library_header.jpg` | 460×215 | 33,120 |
| 1091500 | `1091500/6897c384…39f9.jpg` (icon) | 32×32 | 1,243 |
| 1174180 | `1174180/library_hero.jpg` | 1920×620 | 370,310 |
| 1174180 | `1174180/library_hero_blur.jpg` | 192×62 | 2,845 |
| 1174180 | `1174180/logo.png` | 640×360 | 74,388 |
| 1174180 | `1174180/library_600x900.jpg` | **300×450** | 75,653 |
| 1174180 | `1174180/header.jpg` | 460×215 | 51,987 |
| 1174180 | `1174180/5106abd9…6ec.jpg` (icon) | 32×32 | 1,671 |
| 391220 | `391220/library_hero.jpg` | 1920×620 | 346,869 |
| 391220 | `391220/library_hero_blur.jpg` | 192×62 | 2,365 |
| 391220 | `391220/logo.png` | **640×109** | 31,275 |
| 391220 | `391220/library_600x900.jpg` | 300×450 | 61,478 |
| 391220 | `391220/header.jpg` | 460×215 | 29,146 |
| 391220 | `391220/0b8a37f3…97.jpg` (icon) | 32×32 | 983 |
| 435150 | `435150/65a4c7d8…d9ca/library_hero.jpg` | 1920×620 | 347,713 |
| 435150 | `435150/65a4c7d8…d9ca/library_hero_blur.jpg` | 192×62 | 2,895 |
| 435150 | `435150/logo.png` | 640×360 | 262,681 |
| 435150 | `435150/17d2c60b…9a/library_capsule.jpg` | 300×450 | 63,772 |
| 435150 | `435150/3f6ee788…876f/library_header.jpg` | 460×215 | 46,547 |
| 435150 | `435150/519a99ca…f3.jpg` (icon) | 32×32 | 1,075 |

**There are two layouts.** One uses hashed subfolders with `library_capsule.jpg` and `library_header.jpg` (1091500, 435150). The other is flat, with `library_600x900.jpg` and `header.jpg` (1174180, 391220), and 435150 mixes both. A resolver has to glob `<appid>/**/{library_hero,library_capsule,library_600x900,library_header,header,logo}.*`. The "600x900" file is actually 300×450 locally.

**The Steam CDN has 2x art** (I checked with curl from the Mac on 2026-10-08):
- `https://shared.steamstatic.com/store_item_assets/steam/apps/<appid>/library_600x900_2x.jpg` is 600×900 (1091500: 187,627 B; 435150: 202,388 B).
- `…/library_hero_2x.jpg` is **3840×1240** for 1091500 (837,930 B). For 435150 it returns **404**, on both shared.steamstatic.com and cdn.cloudflare.steamstatic.com.
- `…/logo_2x.png` is 1280×720 (1091500: 159,277 B; 435150: 845,805 B).
- `…/library_hero.jpg` (1920×620), `…/logo.png` (640×360) and `…/header.jpg` (460×215) also return 200.

---

## 8. What this means for the SparkHome build (inferred unless marked)

1. **Shell and runtime.** Run a Chrome 155 `--app`/`--kiosk` window with a dedicated `--user-data-dir` on the existing GNOME/Xorg session, launched by a GNOME autostart entry. This is the same path GameSpark console mode uses, and it worked here. Drive it from a local backend over CDP (`127.0.0.1` only, no `--remote-allow-origins`).
   - Widevine L3 is present, so web streaming services can play at the resolution they allow for software DRM on Linux. That is usually 720p–1080p, never 4K/HDR (reported).
   - HEVC-only content will not play.
2. **YouTube tile.** Open `https://www.youtube.com/tv` in its own target or window with a TV UA (Cobalt, Tizen and webOS all worked). Map remote and gamepad buttons to `Input.dispatchKeyEvent` arrows, Enter and Escape (`isTrusted:true` was measured). Leanback focus is not in DOM focus or in the AX tree, so the agent's "where am I" has to come from screenshots or vision, or from the launcher's own state.
3. **Agent harness.** Expose the tile UI as **structured tools** (open_app, move, select, back, home, search) backed by the launcher's own focus graph, and run an **agent loop** (call, feed back the result, repeat). That loop is how "search youtube for lofi beats" succeeded. Use `qwen3:4b-instruct-2507-q4_K_M` resident through **native `/api/chat` with `num_ctx` around 8k** and `keep_alive` long. Cost: about 3.9 GB, 0.4–0.6 s per action uncontended, and a 3–11 s cold load if it is evicted.
   - Use the VL model only for grounding ("what is focused, and where") and for checking third-party UIs. It localises well but planned the wrong move in this test.
4. **Voice.** Keep `whisper-server` resident (about 0.16 s STT for a 2.7 s phrase) rather than spawning `whisper-cli` (about 0.9 s). Fix the broken RUNPATH and shebang first.
   - A microphone is required, and none is attached. A USB mic is the simplest option. A BT remote with a mic needs its own audio path, which another agent is researching.
   - Piper TTS is fast enough for confirmations (about 1 s cold on CPU, less once resident).
5. **Input.** No new group membership is needed:
   - Gamepads: read `/dev/input/js*` or `event*` through the uaccess ACLs, or use the browser Gamepad API inside Chrome.
   - Synthetic input to non-browser apps: write to `/dev/uinput` (ACL present) or use XTest (`gamespark/tools/xinput.py`).
   - Steam: launch titles with `steam_launch APPID` from `gamespark/lib/env.sh`.
6. **TV hygiene.**
   - `show-banners=true` will let GNOME notifications draw over the launcher. Turn it off in TV mode (a user-level gsettings change, with user approval).
   - `idle-delay 0` and `sleep-inactive-ac-type 'nothing'` already suit a TV.
   - Autologin to the launcher at boot is configured but has not been observed. Verify it with one reboot when the user allows.
7. **Art.** Use the local librarycache with the two-layout resolver for offline tiles, and upgrade to the CDN `_2x` assets when online. The hero is not always available in 2x.

## 9. Open questions and how to close them

| question | why it is open | exact next step |
|---|---|---|
| Real headful playback: CPU load and dropped frames for 4K60 AV1/VP9 with software decode, and whether ANGLE/GL uses the NVIDIA GPU | Headless used SwiftShader, and the TV was off-limits | With the user's OK, open a Chrome window on :1 with the SparkHome profile, play a 2160p60 AV1 YouTube clip, and read `video.getVideoPlaybackQuality()` and `chrome://gpu` |
| HDCP status in headful mode | Headless returned `output-restricted` for HDCP 1.0 | Same window: `mk.getStatusForPolicy({minHdcpVersion:'1.0'})` |
| Per-service resolution and support (Netflix, Disney+, Max, Prime) | Needs logins | Sign in from the TV session, then check the stream stats (Netflix Ctrl+Alt+Shift+D) |
| Uncontended latency for the VL 8B model and the 4B model at 8k | DOS2 was running for those runs | `node tools2.mjs qwen3-vl:8b-instruct 8192 <png>` and `node tools2.mjs qwen3:4b-instruct-2507-q4_K_M 8192` while the GPU is idle |
| Autologin and autostart behaviour at boot | No reboot is allowed in the lab | One sanctioned reboot; check `loginctl show-session` shows `Service=gdm-autologin` |
| Microphone and wake-word path | No capture device | Attach a USB mic, then check `arecord -l` and `wpctl status` |
| Apple TV remote and IR | No rc keymap for Apple remotes in the kernel, and no IR receiver attached | Covered by the remote-research agent; the kernel side (rc-core, LIRC, mceusb, NEC decoder, HID_APPLEIR) is available as modules |

## 10. Cleanup log
- Chrome PID 3236420: `kill -TERM`. The browser exited, port 9333 closed, and no `--user-data-dir=/tmp/sparkhome-lab` processes were left. The crashpad handler (3236431) also exited.
- whisper-server PID 3267344: killed and confirmed stopped.
- Ollama: both test models unloaded (`/api/ps` → `{"models":[]}`). The pulled models remain on disk (§0).
- `/tmp/sparkhome-lab` was removed with `rm -rf /tmp/sparkhome-lab`. At 00:30, `ls -ld` returned "No such file or directory". Both PIDs were confirmed gone, ports 9333 and 8971 were closed, and `/api/ps` was empty.
- I tried once to get uncontended re-runs, polling every 3 s for up to 5 min for `EoCApp.exe` to exit. At 00:29:22 the game was still running, so I skipped them (see §9).

## 11. Sources
- Chrome remote-debugging change (Chrome 136, non-default `--user-data-dir`): https://developer.chrome.com/blog/remote-debugging-port (documented, 2025-03-17)
- Google, "Bringing Chrome to ARM64 Linux devices" (Q2 2026, DGX Spark partnership): https://blog.google/chromium/bringing-chrome-to-arm64-linux-devices/ (documented, 2026-03-12)
- NVIDIA forum, Chrome available for DGX Spark: https://forums.developer.nvidia.com/t/now-available-google-chrome-as-browser-option/380528 (reported)
- Chrome arm64 Linux with native Widevine (secondary): https://alternativeto.net/news/2026/7/google-chrome-launches-for-arm64-linux-with-native-widevine-drm/ and https://www.opensourceforu.com/2026/07/google-ends-widevine-workarounds-on-arm64-linux/ (reported)
- Netflix supported browsers and system requirements: https://help.netflix.com/en/node/23742 (reported 720p for Chrome on Linux in earlier versions; not re-verified)
- Ollama context length defaults: https://docs.ollama.com/context-length (documented)
- Ollama OpenAI compatibility (`reasoning_effort`, no `tool_choice`, no `num_ctx`): https://docs.ollama.com/api/openai-compatibility (documented)
- Ollama qwen3 tags: https://ollama.com/library/qwen3/tags; qwen3-vl: https://ollama.com/library/qwen3-vl and https://ollama.com/library/qwen3-vl/tags (documented)
- Steam CDN asset URLs: probed directly (measured)
