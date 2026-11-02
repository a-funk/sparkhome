#!/bin/bash
# Pair a Bluetooth remote or gamepad by name. Reuses GameSpark's controller/pair.sh, which is already general: it
# scans up to 120 s for an unpaired device whose advertised name matches, pairs it with a NoInputNoOutput agent
# (bonded), trusts and connects it, and leaves devices that are already paired alone.
# Usage: deploy/pair.sh [--reset] "<name regex>" [MAC]
#   --reset  power-cycle the radio first: the Spark's MT7925 sometimes stops hearing anything (this drops connected
#            devices too)
#   MAC      re-pair that device instead of searching by name
# Put the device in pairing mode first:
#   Siri Remote gen 2/3: hold Back (<) + Volume Up for about 5 s. Keep it within about 50 cm. Its address changes
#     about every 20 s, so pair it by name: deploy/pair.sh 'Siri Remote'. Then try experiment E1:
#     tools/siri_probe.py --list
#   Siri Remote gen 1 (Menu + Volume Up): it's named after its serial number, so no name finds it; pair it by address
#     in bluetoothctl (README, "Siri Remote (gen 1)").
#   Xbox controller: hold the pair button until the logo flashes fast. DualSense: hold Create + PS until the light
#     double-flashes.
#   IR Apple remotes don't pair over Bluetooth: see deploy/flirc-apple-remote.md.
# Needs Privacy = device and JustWorksRepairing = always in /etc/bluetooth/main.conf (GameSpark controller/README.md).
set -u
GS=${GAMESPARK:-$HOME/gamespark}/controller/pair.sh
[ -x "$GS" ] || { echo "error: needs GameSpark's $GS (deploy GameSpark to ~/gamespark, or set GAMESPARK=<checkout>)" >&2; exit 1; }
R=; [ "${1:-}" = --reset ] && { R=--reset; shift; }
[ -n "${1:-}" ] || { sed -n '2,/^set -u/{/^#/s/^# \{0,1\}//p;}' "$0"; exit 2; }
if [ -n "${2:-}" ]; then   # GameSpark takes one target: an uppercase MAC (re-pair) or a name regex
  MAC=$(printf '%s' "$2" | tr a-f A-F)
  [[ $MAC =~ ^([0-9A-F]{2}:){5}[0-9A-F]{2}$ ]] || { echo "error: not a Bluetooth address: $2" >&2; exit 2; }
  exec "$GS" ${R:+"$R"} "$MAC"
fi
exec "$GS" ${R:+"$R"} "$1"
