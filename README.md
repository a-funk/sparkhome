# SparkHome

**An Apple TV-style home screen and voice assistant for the NVIDIA DGX Spark.** SparkHome turns the Spark under your
TV into the living-room computer: streaming apps, Steam games and your home-lab services on one launcher, driven by a
Siri Remote, a gamepad, your phone, your voice, or an AI agent. It stays a local-AI box too: the Spark keeps
generating tokens while you watch YouTube.

![SparkHome's Top Shelf: a game's art and its Play button over the Dock](https://a-funk.github.io/sparkhome/media/launch-badge-shelf.webp)

**See it:** the [release guide](https://a-funk.github.io/sparkhome/) has videos, stills and how each part works.

```
  Siri Remote ─┐                                   ┌─ Netflix, YouTube, Plex ... (web tiles, Chrome kiosk)
  Gamepad ─────┤                                   ├─ Steam games (found automatically, with their art)
  Keyboard/IR ─┼──▶ buttons ──┐                    ├─ native apps, emulators, Moonlight (exec tiles)
  Phone ───────┤              ├──▶  SparkHome  ────┼─ home-lab services, GPU/memory widgets
  Your plugin ─┘              │     daemon         └─ Settings: display, sound, voice, sleep, controllers
  Voice ──▶ whisper ──▶ words ┤                    
  Claude Code, any MCP agent ─┘──▶ 24 tools  ──▶  connectors: robot vacuum, Wake-on-LAN, your plugin
```

## What it does

- **A 10-foot launcher.** It is modelled on the tvOS Home Screen: a Dock with a Top Shelf, rows of tiles, an app
  switcher (press Home twice), a loading screen while a game starts, and sleep.
- **Streaming in a kiosk Chrome.** A TV skin makes desktop sites navigable with a remote. Tiles carry honest quality
  badges, because Linux Chrome gets software DRM (Netflix tops out at 1080p stereo).
- **Steam.** Installed games appear with Steam's own art, and a game started from its tile is held behind a loading
  screen until it has really drawn. With [GameSpark](https://github.com/a-funk/GameSpark), games get per-game tuning
  for the Spark.
- **A voice assistant that runs on the box.** Push-to-talk audio goes to whisper.cpp, then to a fast path for common
  commands (about 1 ms), then to a small local model (qwen3:4b on Ollama, 0.4–0.6 s a step). Knowledge questions and
  multi-step requests can escalate to a bigger local model. Spoken replies come from Kokoro or Piper. Try "play The
  Bear on Hulu", "search YouTube for lofi", "mute it, then go home", or "vacuum the kitchen".
- **Every input device.**
  - The gen-1 Siri Remote, natively: touch gestures with tvOS-style momentum and tilt, and its own microphone.
  - Xbox and PlayStation pads over Bluetooth.
  - Apple IR remotes through a FLIRC receiver.
  - Keyboards and mice.
  - A phone remote (PIN-paired) with a trackpad and dictation.
  - Anything else, through a [controller plugin](docs/EXTENDING.md#controllers).
- **An agent interface.** `/mcp` exposes 24 semantic tools (`get_screen`, `open_app`, `search`, `click`,
  `screenshot`…), so Claude Code or any MCP client can watch and drive the TV. SparkHome is also an MCP *client*: home
  devices with an MCP server join the voice assistant.
- **Safety rules for a shared room.**
  - Text on a web page can't command the TV: once page text reaches the model, actions need the person's own words.
  - Agents can't quit your game or change the resolution.
  - The phone pairs with a PIN.
  - `/mcp` is loopback-only.
  - No microphone stream is open between presses.

## Try it in two minutes (any Mac or Linux box)

```bash
git clone https://github.com/a-funk/sparkhome && cd sparkhome
python3 -m sparkhome --demo        # opens http://127.0.0.1:8780/ with fake games and stats
```

Use the arrow keys, Enter, Esc and Home; `/` opens the command bar, and `http://127.0.0.1:8780/remote` is the phone
remote. It needs Python 3.12 or later and nothing else: the code is standard-library Python and plain HTML/CSS/JS,
with no pip, no npm and no build step. Type commands into the command bar: common ones ("open YouTube", "go home")
work without a model, and the rest need Ollama.

```bash
python3 -m sparkhome --selftest    # every module's offline checks, about a minute
tests/run.sh                       # plus the shell scripts, tools and headless-Chrome UI checks
```

## Run it on a Spark

SparkHome runs in the Spark's GNOME session on Xorg, as a systemd user service that owns the TV:

```bash
rsync -a --exclude .git ./ spark:sparkhome/      # or clone it there
ssh spark 'cd ~/sparkhome && deploy/install.sh -n enable'   # dry run: prints every change
ssh spark 'cd ~/sparkhome && deploy/install.sh enable'      # takes over the TV now and at every login
```

`deploy/install.sh disable` gives the TV back and undoes each change it recorded. Optional parts:
- Ollama with `qwen3:4b-instruct-2507-q4_K_M`, for the assistant.
- whisper.cpp, for voice.
- Kokoro, for speech.
- `deploy/install.sh root`, run once, for the Siri Remote's access and microphone.

[docs/MANUAL.md](docs/MANUAL.md) is the full reference: install and update, every control, setting and guard,
the security model and troubleshooting.

## Make it yours

Most things are config. Copy `sparkhome.json` to `~/.config/sparkhome/sparkhome.json` and edit it:
- **Tiles:** any website (`web`), native app (`exec`) or Steam game (`steam`).
- **Rows:** the tiles' order and style.
- **Voice:** the mic, the speech recognizer, the models and the voice.
- **MCP servers:** home devices the assistant can use.

The rest are plugins: one Python file each, in `~/.config/sparkhome/plugins/` or `plugins/`.

| Plugin | Adds | Use it for |
|---|---|---|
| [`keymap`](plugins/keymap.py) | a controller | any Linux input device (IR receiver, presenter, macro pad, keyboard) mapped to SparkHome's buttons by config |
| [`flatpak`](plugins/flatpak.py) | a tile type | `{"type": "flatpak", "app": "org.libretro.RetroArch"}` |
| [`wol`](plugins/wol.py) | a connector | "wake the gaming pc", for Wake-on-LAN, with the same voice-safety rules as every connector |

**[docs/EXTENDING.md](docs/EXTENDING.md)** has the contract: the `home` handle a plugin gets, the button vocabulary
every controller shares, tile types, connector rules, voice backends and the MCP interface. Coding agents
should start with [AGENTS.md](AGENTS.md).

## How it works

A Python daemon (`python3 -m sparkhome`) runs one Chrome window in `--kiosk` and drives it over the Chrome DevTools
Protocol. It moves X11 windows with ctypes (libX11/XTest), reads controllers straight from evdev and hidraw, and serves
the launcher page, the phone remote and the API.

| Module | Role |
|---|---|
| [`server.py`](sparkhome/server.py) | HTTP API, the UI, SSE events, auth and Origin checks, `/mcp` |
| [`shell.py`](sparkhome/shell.py) | tiles, Steam discovery and launches, the Chrome supervisor and tabs, windows, the app switcher, sleep |
| [`agent.py`](sparkhome/agent.py) | the tool registry behind voice, `/api/action` and MCP; the fast path; the LLM loop; push-to-talk |
| [`router.py`](sparkhome/router.py) | picks the small or the big model per request |
| [`connectors.py`](sparkhome/connectors.py) | MCP client for home devices, and connector plugins, behind one word-gated guard |
| [`plugins.py`](sparkhome/plugins.py) | loads plugins; the `Home` handle; the button vocabulary |
| [`inputs.py`](sparkhome/inputs.py), [`siri.py`](sparkhome/siri.py) | gamepad buttons from evdev; the Siri Remote over hidraw, with gestures |
| [`settings.py`](sparkhome/settings.py) | Settings: display modes (mutter), sound (PipeWire), Bluetooth devices, voice, sleep, power |
| [`hcitap.py`](sparkhome/hcitap.py), [`siri_audio.py`](sparkhome/siri_audio.py) | the Siri Remote's microphone, tapped below BlueZ and decoded from Opus |
| [`ui/`](ui) | the launcher (`app.js`, `style.css`) and the phone remote (`remote.html`) |

[docs/RESEARCH.md](docs/RESEARCH.md) and [docs/research/](docs/research) hold the measurements behind the design: why
Xorg and a kiosk rather than gamescope, and why semantic MCP tools rather than pixel-clicking computer use (on the
TVWorld benchmark, the small vision models that fit beside a game score 1.6–20 %). They also hold the streaming quality matrix, the controller survey, the Siri
Remote protocol and the model evals.

## Status

SparkHome is the boot screen of one DGX Spark under a TV (DGX OS 7, Ubuntu 24.04 arm64, GNOME on Xorg, Chrome). It
is young software, built for that box first:
- Paths and defaults assume a DGX Spark. Other Linux PCs with an NVIDIA GPU and an Xorg GNOME session should work
  with config changes, but are untested.
- The plugin layer and its three plugins pass their offline checks; `keymap` hasn't been run on hardware yet.
- Streaming quality is capped by Linux Chrome's software DRM; see [docs/RESEARCH.md §4](docs/RESEARCH.md#4-streaming).

Issues and pull requests are welcome. Please run `tests/run.sh` first.

Related projects:
- [open-appleremote](https://github.com/a-funk/open-appleremote): the gen-1 Siri Remote on Linux, as a standalone
  library.
- [GameSpark](https://github.com/a-funk/GameSpark): makes Steam games run better on the DGX Spark.
- [sharkclean-mcp](https://github.com/a-funk/sharkclean-mcp): the robot-vacuum MCP server the assistant drives.

## License

[MIT](LICENSE). SparkHome isn't affiliated with NVIDIA, Apple, Valve or any streaming service; their names identify
the hardware and services it works with.
