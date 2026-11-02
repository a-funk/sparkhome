#!/bin/bash
# Offline checks: Python self-tests, shell syntax, install.sh's gsettings restore guard, plus shellcheck, node --check
# and the TV skin in headless Chrome, each when installed. Needs no Spark, X11 or network. Exits non-zero on any failure,
# else prints "all checks passed".
set -u
cd "$(dirname "$0")/.." || exit 1
fail=0
check() {  # NAME CMD...: run quietly; on failure show the output
  local name=$1 out; shift
  if out=$("$@" 2>&1); then echo "ok   $name"; else echo "FAIL $name"; printf '%s\n' "$out" | sed 's/^/     /'; fail=1; fi
}
check "python3 -m sparkhome --selftest" python3 -m sparkhome --selftest
check "tools/siri_probe.py --selftest" python3 -I tools/siri_probe.py --selftest
# as deploy/sparkhome-siri-mic.service runs its root-owned copy: alone, standard library only
check "sparkhome/hcitap.py --selftest, as the mic unit runs it (python3 -I -S)" python3 -I -S sparkhome/hcitap.py --selftest
check "tools/mic_eval.py --selftest" python3 -I tools/mic_eval.py --selftest
check "tools/mic_watch.py --selftest" python3 -I tools/mic_watch.py --selftest
check "tools/eval.py --selftest" python3 -I tools/eval.py --selftest
h=$(mktemp -d) || exit 1; mkdir -p "$h/.cache/sparkhome"  # ...beside a daemon refreshing the host's connector cache
touch -t 203001010000 "$h/.cache/sparkhome/mcp-shark.json"  # (newer than any run: it checks its own cache, not that)
check "tools/eval.py --selftest beside a daemon's cache refresh" env HOME="$h" python3 -I tools/eval.py --selftest
rm -rf "$h"
check "tools/router_exp.py --selftest" python3 -I tools/router_exp.py --selftest
check "tools/tts_server.py --selftest" python3 -I tools/tts_server.py --selftest
for f in deploy/*.sh tests/*.sh; do check "bash -n $f" bash -n "$f"; done
# disable must keep gsettings.bak when a restore doesn't stick (gsettings exits 0 when dconf can't commit). The temp HOME
# has no unit, so only these stubs run: gsettings does nothing, dconf read answers the TV value.
t=$(mktemp -d) || exit 1; mkdir -p "$t/bin" "$t/.local/state/sparkhome"
echo "org.gnome.mutter overlay-key 'Super_L'" > "$t/.local/state/sparkhome/gsettings.bak"
cat > "$t/bin/dconf" <<'EOF'
#!/bin/sh
[ "$1" != read ] || echo "''"
EOF
cp "$t/bin/dconf" "$t/bin/gsettings"; chmod +x "$t/bin/dconf" "$t/bin/gsettings"
# shellcheck disable=SC2317  # run through check
kept() { HOME=$t PATH=$t/bin:$PATH deploy/install.sh disable && [ -f "$t/.local/state/sparkhome/gsettings.bak" ]; }
check "install.sh disable keeps gsettings.bak when a restore fails" kept
rm -rf "$t"
# enable refuses a checkout whose selftest fails, says only that the service wasn't (re)started (the files are this
# checkout's already: never "nothing changed"), and never sends the Spark's operator to tests/run.sh there (its
# headless Chrome would start beside the TV)
t=$(mktemp -d) || exit 1; mkdir -p "$t/deploy" "$t/sparkhome"; cp deploy/install.sh "$t/deploy/"
touch "$t/sparkhome/__init__.py"; echo 'raise SystemExit(1)' > "$t/sparkhome/__main__.py"
# shellcheck disable=SC2317  # run through check
hint() {
  local out; ! out=$(HOME=$t "$t/deploy/install.sh" enable 2>&1) &&
    [[ $out == *"--selftest fails"*"tests/run.sh runs on the Mac, not"*"the service was not (re)started"* ]]
}
check "install.sh enable's selftest failure keeps tests/run.sh on the Mac" hint
rm -rf "$t"
# root's mic step finds the paired Siri Remote (not the keyboard beside it) and fills in the unit: you, your uid, it
t=$(mktemp -d) || exit 1; mkdir -p "$t/bin"
cat > "$t/bin/bluetoothctl" <<'EOF'
#!/bin/sh
case "$*" in
  *"devices Paired") printf 'Device 11:22:33:44:55:66 Keyboard\nDevice AA:BB:CC:DD:EE:0F C00000000000\n' ;;
  *"info AA:BB:CC:DD:EE:0F") printf '\tPaired: yes\n\tModalias: bluetooth:v004Cp026Dd0000\n' ;;
  *) printf '\tModalias: usb:v046DpC52Bd1211\n' ;;
esac
EOF
chmod +x "$t/bin/bluetoothctl"
# shellcheck disable=SC2317  # run through check
mic() {
  local out; out=$(HOME=$t PATH=$t/bin:$PATH deploy/install.sh -n root 2>&1) &&
    [[ $out == *"| User=$(id -un)"* && $out == *"| BindPaths=/run/user/$(id -u)"* && $out != *@USER@* &&
       $out == *"| ExecStart=/usr/bin/python3 -I -S /usr/local/lib/sparkhome/hcitap.py --remote AA:BB:CC:DD:EE:0F"* &&
       $out == *"| WantedBy=user@$(id -u).service"* && $out != *@UID@* && $out != *@REMOTE@* &&
       $out == *"would run: as_root systemctl restart sparkhome-siri-mic.service"* ]]
}
check "install.sh -n root renders the Siri Remote mic unit for the paired remote" mic
rm -rf "$t"
# README's security model names every route that takes its caller as the person (server.py's selftest drives each):
# /api/voice and /api/pointer, and with SparkHome's Origin /api/close and /api/action's navigate and select
# shellcheck disable=SC2317  # run through check
person() {
  local s; s=$(sed -n '/^- \*\*Agents never quit/,/^- \*\*/p' docs/MANUAL.md)
  [[ $s == *"/api/voice"* && $s == *"/api/pointer"* && $s == *"/api/close"* && $s == *'`navigate`'* &&
     $s == *'`select`'* ]]
}
check "the manual's security model names every route that acts as the person" person
if command -v shellcheck >/dev/null; then check "shellcheck" shellcheck -x -S warning deploy/*.sh tests/*.sh
else echo "skip shellcheck (not installed)"; fi
if command -v node >/dev/null; then
  check "node --check ui/app.js" node --check ui/app.js
  j=$(mktemp -d) || exit 1   # the phone remote's inline script, in a .js file (node won't --check other extensions)
  python3 -I -c 'import re, sys; sys.stdout.write(re.search(r"<script>(.*)</script>", open(sys.argv[1]).read(), re.S)[1])' \
    ui/remote.html > "$j/remote.js"
  check "node --check ui/remote.html script" node --check "$j/remote.js"
  rm -rf "$j"
else echo "skip node --check (node not installed)"; fi
# The TV skin and page tools, and the app switcher and Settings on the demo daemon, in a real headless Chrome (its own
# temp profile and a free port), when one is installed (never beside the TV's kiosk or when SPARKHOME_NO_CHROME is set:
# they start no Chrome then, as checked first)
t=$(mktemp -d) || exit 1; printf '#!/bin/sh\ntouch "%s/ran"\n' "$t" > "$t/chrome"; chmod +x "$t/chrome"
# shellcheck disable=SC2317  # run through check
nochrome() { SPARKHOME_NO_CHROME=1 python3 "$1" "$t/chrome" && [ ! -e "$t/ran" ]; }
check "tests/skin_check.py starts no Chrome when told not to" nochrome tests/skin_check.py
check "tests/switcher_check.py starts no Chrome when told not to" nochrome tests/switcher_check.py
check "tests/settings_check.py starts no Chrome when told not to" nochrome tests/settings_check.py
rm -rf "$t"
chrome=$(command -v google-chrome || command -v chromium || ls "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" 2>/dev/null)
if [ -n "$chrome" ]; then check "tests/skin_check.py (headless Chrome, unless the kiosk's is up)" python3 tests/skin_check.py "$chrome"
  check "tests/switcher_check.py (headless Chrome, unless the kiosk's is up)" python3 tests/switcher_check.py "$chrome"
  check "tests/settings_check.py (headless Chrome, unless the kiosk's is up)" python3 tests/settings_check.py "$chrome"
else echo "skip skin, switcher and settings checks (no Chrome)"; fi
[ $fail = 0 ] && echo "all checks passed"
exit $fail
