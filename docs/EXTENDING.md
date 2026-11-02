# Extending SparkHome

SparkHome is built from four kinds of parts. Most new things need only config; the rest are one Python file each.
You can also ask the TV: "add a tile for PBS" has a bigger local model build and test it there, and keep it when you
press Keep ([the builder](MANUAL.md#the-builder)).

| You want to add | It is a | Config only | Plugin |
|---|---|---|---|
| A streaming service, a local web app, a native app, a Steam shortcut | **tile** | a `web`, `exec` or `steam` tile | a **tile type**, when many tiles share one recipe |
| A remote, gamepad, IR receiver, macro pad, keyboard, MIDI or network controller | **controller** | the `keymap` plugin, for any Linux input device | `start(home)`, for anything else |
| A smart-home device or service the voice assistant can use | **connector** | an MCP server in `mcp_servers` | `TOOLS` + `call()`, in-process |
| A microphone, speech recognizer, voice or language model | **voice backend** | `mic`, `asr`, `tts`, `llm`, `llm_big` | `home.ask(text)`, for a recognizer of your own |

The fifth part, the **agent interface**, is already there: any MCP client (Claude Code, an agent framework) drives the
TV through 25 tools at `/mcp`. See [Agents](#agents-drive-the-tv-over-mcp).

Everything below runs in `--demo` on a laptop, so you can build a plugin before you have the hardware.

## Plugins

A plugin is one Python file. Name it in `settings.plugins` with its settings, or `true` for none:

```json
"plugins": {"keymap": {"match": "flirc", "map": {"KEY_UP": "up"}}, "flatpak": true, "wol": {"devices": {}}}
```

SparkHome looks for `NAME.py` in `~/.config/sparkhome/plugins/` first, then in this repo's `plugins/`. Names are
lowercase letters, digits and single underscores. A plugin that doesn't load, or that raises, is logged and skipped:
the TV keeps going. Restart to load a change: `systemctl --user restart sparkhome`, or re-run `--demo`.

A plugin module may define any of these. One file can do all three.

| Name | Kind | Contract |
|---|---|---|
| `start(home)` | controller, background job | Runs once, at daemon start, on its own daemon thread. Loop forever in it. |
| `TILE_TYPE`, `tile(t)` | tile type | `tile(t)` gets a tile of this type and returns the keys of a built-in tile (`web`, `exec` or `steam`). It raises `ValueError` for a bad tile, which is then skipped. |
| `TOOLS`, `call(home, tool, args)` | connector | `TOOLS` is a list of MCP tool definitions. `call()` returns a `str` or a JSON-able `dict`, or raises to fail. |
| `WORDS` | connector | A tuple of single words that bring the connector's tools into a request. Its name always does. |
| `_selftest()` | any | Asserts. `python3 -m sparkhome --selftest` runs the ones in `plugins/`. |

### `home`: what a plugin can do

Each plugin gets its own `Home` ([sparkhome/plugins.py](../sparkhome/plugins.py)):

| Member | What it does |
|---|---|
| `home.press(button)` | One press of a [button](#buttons). Returns the action's result, `{"ok": ...}`. |
| `home.voice(pressed)` | Push-to-talk: `True` starts recording on the configured mic, `False` stops it and runs the request. |
| `home.ask(text)` | Words a person said, handled as if spoken into the remote. Returns `{"ok", "reply", ...}`. |
| `home.say(text)` | A message on the TV, spoken aloud when speech is on. |
| `home.toast(text, kind)` | A short message on the TV; `kind` is `"info"` or `"error"`. |
| `home.pointer(dx, dy, click, scroll_x, scroll_y)` | Moves the TV's real pointer. `click` is `"left"`, `"right"` or `"middle"`. |
| `home.type_text(text)` | Types into the focused field of the web app in front. |
| `home.open(tile_id)` | Opens a tile, or brings it back to the front. |
| `home.state()` | `{"surface": "launcher" \| "web" \| "app", "active", "title", "running", ...}` |
| `home.config` | The plugin's own `settings.plugins` entry, a dict. |
| `home.demo` | True in `--demo`: don't touch hardware. |
| `home.log` | A `logging.Logger` named `sparkhome.plugin.NAME`. |

### Buttons

Every controller speaks the same buttons. Each runs the action a Siri Remote, a gamepad or the phone runs:

| Button | Action |
|---|---|
| `up` `down` `left` `right` | Move the focus |
| `select` | OK |
| `back` | Back |
| `home` | Home. Twice within 0.3 s opens the app switcher |
| `apps` | The app switcher |
| `playpause` `play` `pause` `next` `previous` `stop` | Playback in the app in front |
| `volume_up` `volume_down` `mute` | The TV's volume |

Two rules keep every controller consistent:
- **A press that wakes the sleeping screen does only that**, so nothing opens by accident in the dark.
- **While a game or app is in front, only Home, the app switcher, playback and volume reach SparkHome.** The game
  reads its own controls.

## Controllers

### Any Linux input device, by config

The [`keymap`](../plugins/keymap.py) plugin turns any evdev device (an IR receiver such as FLIRC, a presenter clicker, a
macro pad, a USB keyboard) into a controller:

```json
"plugins": {"keymap": {"match": "flirc", "grab": true, "map": {
  "KEY_UP": "up", "KEY_DOWN": "down", "KEY_LEFT": "left", "KEY_RIGHT": "right", "KEY_ENTER": "select",
  "KEY_BACKSPACE": "back", "KEY_HOMEPAGE": "home", "KEY_PLAYPAUSE": "playpause", "KEY_VOICECOMMAND": "voice"}}}
```

- `match` is part of the device's name. To list names and key codes, run `python3 -m sparkhome.inputs --probe`.
- `map` takes key names from `sparkhome/inputs.py`, or the numbers `--probe` prints. `"voice"` is push-to-talk.
- `grab` (default true) gives the device's keys to SparkHome alone, so the window in front doesn't also get them.
  Turn it off for a keyboard you also type on.

### Gamepads (Xbox, DualSense, DualShock)

Pair a pad over Bluetooth and it already works. The launcher and web tiles read it through Chrome's Gamepad API, and
SparkHome reads its Guide button from evdev (`settings.gamepad`: Home, a hold in games, and an optional push-to-talk
button). Tested with the Xbox Wireless Controller and the DualSense. Other pads Chrome maps to the standard layout
should work the same; a DualShock 4 isn't tested. The [manual's Controls table](MANUAL.md#controls) has the full map.

### Anything else: write `start(home)`

A controller plugin reads its device and calls `home.press()`. This one listens for UDP datagrams (`up`, `select`, …),
so any script, microcontroller or home-automation rule on the LAN can press buttons:

```python
# ~/.config/sparkhome/plugins/udp_remote.py    settings: "plugins": {"udp_remote": {"port": 8799}}
"""A LAN remote: each UDP datagram is one button name."""
import socket


def start(home):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", int(home.config.get("port", 8799))))
    while True:
        button = s.recv(64).decode(errors="replace").strip()
        if button == "talk":
            home.voice(True)
        elif button == "done":
            home.voice(False)
        else:
            home.press(button)
```

Test it with `echo -n select | nc -u -w0 127.0.0.1 8799`.

Before you ship a controller like this, decide who may press. This one trusts anyone on the LAN, which is fine on a
home network but not on a shared one. The phone remote, by comparison, pairs with a PIN.

Some notes for controller plugins:
- **Threads.** `home.press()` runs on your thread and can take a moment while a web page is busy. If your device
  streams events (touch, motion), read on one thread and queue presses to a second, as
  [sparkhome/siri.py](../sparkhome/siri.py) does.
- **Gestures.** For touch surfaces, reuse `siri.Gestures` (swipes, momentum, tilt, tuned on a real remote) rather
  than writing your own.
- **Device access.** Joystick nodes are readable by the logged-in user through udev's `uaccess` tag. Keyboard-type
  nodes (`/dev/input/event*` of remotes, IR receivers, keyboards) are `root:input`. Don't add yourself to `input`: that
  lets every process read every keyboard. Add a udev rule for your device alone, like
  [deploy/71-sparkhome-siri.rules](../deploy/71-sparkhome-siri.rules):

  ```
  # /etc/udev/rules.d/71-my-remote.rules, then: sudo udevadm control --reload && sudo udevadm trigger
  SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="*FLIRC*", TAG+="uaccess"
  ```

## Tiles

### By config

Add the tile under `tiles` and its id to a row's `tiles` in `~/.config/sparkhome/sparkhome.json`. Start from a copy of
the repo's [sparkhome.json](../sparkhome.json).

```json
"tiles": {
  "kodi":    {"type": "exec", "title": "Kodi", "color": "#17B2E7", "argv": ["kodi", "--standalone"], "window_class": "Kodi"},
  "grafana": {"type": "web", "title": "Grafana", "color": "#F46800", "url": "http://127.0.0.1:3000/"},
  "portal":  {"type": "steam", "title": "Portal 2", "appid": "620"}
}
```

- **`web`**: a page in the kiosk's Chrome, in its own tab. `search` takes a URL with `{q}` for voice search. `nav`
  sets how the gamepad drives the page: `"pointer"` (the default), `"keys"` (TV-style web apps) or `"pad"` (pages that
  read the controller themselves). `skin: true` makes a desktop site navigable with a remote, and `zoom` draws it
  bigger. `open_in` plays the site's video pages in another tile, such as YouTube's TV player
  ([the manual](MANUAL.md#configuration) has the fields).
- **`exec`**: a native app. `window_class` ties its windows to the tile, so the app switcher can show and close it.
  `"game": false` stops the bigger model from treating it as a game.
- **`steam`**: a Steam game by `appid`, or a `steam://` `uri`. Installed games also appear on their own in the Games
  row.
- **`widget`**: one of the launcher's built-in panels (`gpu`, `memory`, `models`, `settings`).

A local service's tile hides while its port is closed. [The manual](MANUAL.md#configuration) lists every field.

### A tile type

When many tiles share one recipe, a tile-type plugin turns a short tile into a built-in one. The tile keeps its other
keys (`title`, `color`, `cpus`…), and the app switcher, close and force quit work as for any built-in tile. See
[`plugins/flatpak.py`](../plugins/flatpak.py):

```python
TILE_TYPE = "flatpak"

def tile(t):                          # {"type": "flatpak", "app": "org.libretro.RetroArch"}
    return {"type": "exec", "argv": ["flatpak", "run", t["app"]], "window_class": t["app"]}
```

## Connectors

A connector gives the voice assistant tools for something outside the TV.

### An MCP server, by config

SparkHome is an MCP client too. Add any MCP server that speaks Streamable HTTP:

```json
"mcp_servers": [{"name": "shark", "url": "http://127.0.0.1:8765/mcp", "token_file": "~/.config/shark-mcp/bearer",
                 "read_only": ["list_robots", "get_status", "list_rooms"], "words": ["vacuum"], "timeout_s": 8}]
```

- `name` is the tools' namespace: `shark.clean_rooms`.
- `token_file` holds a bearer token on one line. It must be readable by you alone (`chmod 600`).
- `read_only` lists tools that only read; tools the server annotates `readOnlyHint` count too.
- `words` brings the server's tools into a request that says one of them; its name always does.

[sharkclean-mcp](https://github.com/a-funk/sharkclean-mcp) (Shark robot vacuums) is the tested example, with a fast
path for "vacuum the kitchen". The manual's [Connectors](MANUAL.md#connectors-home-devices-over-mcp) section covers it.

### A connector plugin

For a device with a simple local API, a plugin skips the server. See [`plugins/wol.py`](../plugins/wol.py)
(Wake-on-LAN, "wake the gaming pc"):

```python
WORDS = ("pc", "computer")
TOOLS = [
    {"name": "list_devices", "description": "The computers this can wake.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}},
    {"name": "wake_device", "description": "Wake a computer on the LAN by name.",
     "inputSchema": {"type": "object", "properties": {"device": {"type": "string"}}, "required": ["device"]}},
]

def call(home, tool, args):
    ...                               # return a short sentence or a dict; raise ValueError("why") to fail
```

### The rules every connector gets

These hold for plugins and MCP servers alike, in [sparkhome/connectors.py](../sparkhome/connectors.py):
- **The model sees a connector's tools only when the request names it**, by its name or a word in `words`/`WORDS`.
  Every other request keeps the short prompt, which keeps the small model accurate and the prompt cache warm.
- **Read-only tools run freely.**
- **Any other tool runs only when the person's own words ask for it.** The words must say the tool's verb (the first
  word of its name: `wake_device` → "wake"), name the connector, and say every argument. A question ("is the pc on?")
  never acts. Neither does text from a web page or another tool's reply. So name action tools verb-first, and keep
  their arguments things a person says aloud.
- **Every action shows on the TV** with its result, and goes in the journal.

## Voice

The assistant is push-to-talk: record, transcribe, act, reply. Each stage is config. [The manual's
Speech](MANUAL.md#speech) and [Agents](MANUAL.md#agents-and-mcp) sections have the details.

| Stage | Setting | Default |
|---|---|---|
| Microphone | `mic`: an ALSA device for `arecord` | `default`. A Siri Remote records with its own mic once its helper is installed |
| Speech to text | `asr`: a whisper.cpp `server_cmd` (started lazily, kept resident) or `cli` | whisper large-v3-turbo |
| Understanding | `llm`: `api` `ollama` or `openai` (any OpenAI-compatible server), `base_url`, `model` | qwen3:4b-instruct on Ollama |
| Harder questions | `llm_big`: the same keys; knowledge, recommendations and multi-step requests go here | optional |
| Spoken replies | `tts`: a `server_cmd` (Kokoro, [tools/tts_server.py](../tools/tts_server.py)) or a `cmd` writing `{out}` (Piper) | off |

Common commands never reach a model: a fast path handles "pause", "open Netflix" or "volume down" in about a
millisecond.

A plugin with its own recognizer (a wake word, a phone's dictation, a room microphone array) skips the first two
stages: it calls `home.ask("open netflix")` with the words. They count as the owner's commands, so only pass words a
person in the room said.

## Agents: drive the TV over MCP

Any MCP client drives the TV through the same 25 tools the voice assistant uses: `get_screen`, `open_app`, `navigate`,
`select`, `search`, `media`, `click`, `screenshot`, `close_app` and more. `/mcp` answers on loopback only; from
another machine, use an ssh tunnel:

```bash
ssh -N -L 8780:127.0.0.1:8780 spark &
claude mcp add --transport http sparkhome http://127.0.0.1:8780/mcp
```

Then ask your agent "what's on the TV?". Some things stay with the person at the TV:
- Quitting a game.
- Changing the resolution.
- Restarting or shutting down.

Agents get a message that says so. The tools are defined in [sparkhome/agent.py](../sparkhome/agent.py) (`TOOLS`).

## Sharing a plugin

A plugin that others can use belongs in `plugins/`, with:
- a docstring giving its settings, with an example;
- a `_selftest()` that runs offline;
- no dependencies beyond the standard library. If it needs one, import it inside `start()` or `call()`, and say so
  in the docstring.

Then run `tests/run.sh`. [AGENTS.md](../AGENTS.md) has the rest of the house rules.
