# Working on SparkHome (for coding agents and humans)

SparkHome is a TV home screen and voice assistant for the NVIDIA DGX Spark. Read [README.md](README.md) for what it
is, and [docs/EXTENDING.md](docs/EXTENDING.md) before adding a controller, tile, connector or voice backend.
[docs/MANUAL.md](docs/MANUAL.md) is the behaviour reference. Code comments that cite `README "Section"` mean a
section of the manual.

## Develop and check

```bash
python3 -m sparkhome --demo         # the whole UI with fake Chrome, X11, Steam and stats; works on macOS
python3 -m sparkhome --selftest     # every module's _selftest(), offline, about a minute
python3 -m sparkhome.plugins        # one module alone (each has a _selftest)
tests/run.sh                        # all of the above, plus tools, shell scripts and headless-Chrome UI checks
```

A change is done when `tests/run.sh` prints `all checks passed`, and when anything a person sees has been looked at
in `--demo` or on the TV. Run `tests/run.sh` on your workstation, not on the Spark beside the TV: its Chrome checks
start a browser. They skip themselves when the kiosk's DevTools port (9223) answers, or when `SPARKHOME_NO_CHROME` is
set.

## Where things go

1. **Config first.** New tiles, rows, MCP servers and voice backends are entries in `sparkhome.json`, no code.
2. **Then a plugin.** New controllers, tile types and in-process connectors go in `plugins/NAME.py`, with the
   contract in [docs/EXTENDING.md](docs/EXTENDING.md):
   - A plugin reaches SparkHome only through its `home` handle and the shared button vocabulary (`plugins.BUTTONS`).
   - Give it a docstring with its settings, and a `_selftest()`. `python3 -m sparkhome --selftest` runs every
     shipped plugin's.
3. **The core last.** Change the core when a plugin can't do the job, and widen `Home` rather than handing plugins
   the Shell. The modules are listed in the README's "How it works".

## House rules

- **Standard library only.** No pip and no npm in the core, and no build step. An optional dependency (libopus,
  Kokoro) is loaded lazily, and its absence degrades one feature with a clear log line.
- **Every module checks itself.** Non-trivial logic gets an assert in its module's `_selftest()`. Name the behaviour
  it pins, not just the function.
- **A broken part never takes the TV down.** Plugins, connectors, the agent and a bad user config are caught,
  logged, and skipped or replaced by the default.
- **Comments say why.** They say what was measured, which race a lock prevents, and which simplification was chosen.
  `ponytail:` marks a deliberate shortcut with its known limit and its upgrade path. In docs, tag claims `[M]`
  measured, `[D]` documented, `[R]` reported or `[I]` inferred.
- **Keep the docs in step.** A change in behaviour updates the manual section that describes it. Some checks read
  the manual's text: see `tests/run.sh` and `tests/switcher_check.py`.

## Rules that protect the people in the room

Don't weaken any of these without the owner's say-so, and keep the selftest that pins each one.

- **Words decide, not pages.** Once web-page text or a tool's reply has reached the model, a request can only look.
  Opening an app, changing the volume or running a connector action needs the person's own words. A connector action
  needs its verb, its device and every argument said aloud (`Connectors.guard`).
- **Some things are the person's alone.** Agents (MCP, `/api/action`) can't quit a game or app, change the display
  mode, or restart or shut down the box. They can't drive the app switcher or Settings while either is open.
- **Access.**
  - `/mcp` answers on loopback only.
  - LAN clients pair with the PIN shown on the TV.
  - POSTs carry an Origin check, and the Host header is checked against DNS rebinding.
  - Bearer-token files must be readable by their owner alone.
- **The microphone stays closed between presses.** Recordings live in `$XDG_RUNTIME_DIR` and are deleted once
  transcribed.
- **A press that wakes the screen does only that.**

## Rules that keep the TV responsive

- The CDP event thread must never wait for the daemon's lock: it is the only thread that answers JavaScript dialogs.
  Breaking this caused measured 20 s freezes.
- Shell actions run one at a time under `Shell.lock`, a fair lock: Home must not wait behind a burst of agent calls.
- Any Chrome started on the Spark needs `--password-store=basic`. Without it, GNOME's keyring prompt takes the TV's
  focus.

## On a live Spark

- The TV belongs to the people watching it. Don't take over the display, restart `sparkhome.service`, switch the
  boot target or reboot without the owner's OK.
- Deploy to `~/sparkhome.next`, run its selftest there, then swap it in, keeping `~/sparkhome.prev`. The manual's
  "Install on the Spark" section has the exact commands.
- `sparkhome.service` uses `KillMode=process`. Steam and the games it started outlive a restart; `systemctl --user
  kill` would end them.

## Privacy

This repo is public. Before you commit, check that the diff has none of these:
- hostnames, usernames, or home paths other than `~`;
- IP addresses (use `192.168.1.x` or `100.64.0.x`);
- Bluetooth or Wi-Fi MAC addresses (use `02:00:00:00:00:xx`). A gen-1 Siri Remote's Bluetooth name is its serial
  number;
- device serials, PINs, tokens, account ids, or the names of people, pets or devices in someone's home.

Captures and logs from real hardware are the usual leak: trim them, as `tests/` and `siri.py`'s fixtures are.
