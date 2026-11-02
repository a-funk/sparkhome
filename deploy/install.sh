#!/bin/bash
# Turn the DGX Spark into a SparkHome TV box, and back. Run it ON the Spark from the repo checkout, as your user.
# Usage: deploy/install.sh [-n] enable | disable | status | root [undo]
#   enable   sparkhome.service (systemd user unit: restarted now, so code and config updates take effect, and started
#            at every graphical login); GameSpark's Steam autostart moved aside (GDM autologin stays on); TV-mode
#            gsettings; pop-up autostarts hidden; autologin checked
#   disable  undo enable: stop SparkHome and close its kiosk, remove the unit, put the GameSpark autostart back,
#            restore the saved gsettings, remove the autostart overrides enable created
#   status   read-only: unit state, daemon /api/state, kiosk CDP /json/version, active window, what enable changed
#   root     optional system files, via passwordless sudo or GameSpark's privileged-container trick (docker group):
#            Chrome managed policy (autoplay on, translate off), deploy/remotes.hwdb as a udev hwdb,
#            deploy/71-sparkhome-siri.rules (SparkHome can read the Siri Remote) and, once the remote is paired, its
#            microphone (deploy/sparkhome-siri-mic.service, a system service). 'root undo' reverses exactly that.
#   -n       dry run: print each change instead of making it
# Every change is recorded under ~/.local/state/sparkhome/; disable and root undo restore what was there from it.
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
STATE=$HOME/.local/state/sparkhome
UNIT=$HOME/.config/systemd/user/sparkhome.service
AUTOSTART=$HOME/.config/autostart
GS_ENTRY=gamespark-console.desktop
HIDE="update-notifier ubuntu-advantage-notification ubuntu-report-on-upgrade org.gnome.DejaDup.Monitor org.gnome.Evolution-alarm-notify"
POLICY=/etc/opt/chrome/policies/managed/sparkhome.json
HWDB=/etc/udev/hwdb.d/61-sparkhome-remotes.hwdb
SIRI_RULES=/etc/udev/rules.d/71-sparkhome-siri.rules
MIC_UNIT=/etc/systemd/system/sparkhome-siri-mic.service
MIC_TAP=/usr/local/lib/sparkhome/hcitap.py   # root-owned: the code that gets CAP_NET_RAW isn't the user's to change
# Build hwdb.bin in /usr as Ubuntu's udev.postinst does; one in /etc would shadow every later distro update
HWDB_OUT=--usr; [ ! -e /etc/udev/hwdb.bin ] || HWDB_OUT=--root=/   # an admin's existing /etc copy is kept current
TV_SETTINGS="org.gnome.desktop.notifications show-banners false
org.gnome.desktop.screensaver lock-enabled false
org.gnome.desktop.screensaver idle-activation-enabled false
org.gnome.desktop.lockdown disable-lock-screen true
org.gnome.mutter overlay-key ''
org.gnome.settings-daemon.plugins.power idle-dim false
org.gnome.desktop.media-handling automount-open false
org.gnome.desktop.session idle-delay uint32 0"

# Reach the logged-in session from ssh too (same defaults as GameSpark's lib/env.sh). DISPLAY is the session's own (:1
# after a password login, likely :0 after autologin). XDG_CURRENT_DESKTOP makes 'gsettings get' show Ubuntu's defaults.
uid=$(id -u); me=$(id -un)
: "${XDG_RUNTIME_DIR:=/run/user/$uid}" "${DBUS_SESSION_BUS_ADDRESS:=unix:path=/run/user/$uid/bus}" "${XAUTHORITY:=/run/user/$uid/gdm/Xauthority}"
export XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS XAUTHORITY   # systemctl --user below needs the first two
: "${DISPLAY:=$(systemctl --user show-environment 2>/dev/null | sed -n 's/^DISPLAY=//p')}" "${DISPLAY:=:1}"
: "${XDG_CURRENT_DESKTOP:=$(systemctl --user show-environment 2>/dev/null | sed -n 's/^XDG_CURRENT_DESKTOP=//p')}"
export DISPLAY XDG_CURRENT_DESKTOP

DRY=
say() { printf '%s\n' "$*"; }
die() { echo "error: $*" >&2; exit 1; }
run() {   # make one change, or print it under -n
  if [ -z "$DRY" ]; then "$@"; return; fi
  local a out="    would run:"
  for a in "$@"; do case $a in '' | *[[:space:]]*) out+=" \"$a\"" ;; *) out+=" $a" ;; esac; done
  say "$out"
}
write() {  # FILE CONTENT
  if [ -n "$DRY" ]; then say "    would write $1:"; printf '%s\n' "$2" | sed 's/^/      | /'
  else mkdir -p "$(dirname "$1")" && printf '%s\n' "$2" > "$1"; fi
}
record() { # STATE_FILE LINE: remember one change for disable / root undo
  if [ -n "$DRY" ]; then say "    would record in $STATE/$1: $2"
  else mkdir -p "$STATE" && printf '%s\n' "$2" >> "$STATE/$1"; fi
}
saved() { awk -v s="$1" -v k="$2" '$1 == s && $2 == k { f = 1 } END { exit !f }' "$STATE/gsettings.bak" 2>/dev/null; }
autologin_ok() {
  grep -Eqi '^[[:space:]]*AutomaticLoginEnable[[:space:]]*=[[:space:]]*(true|1)[[:space:]]*$' /etc/gdm3/custom.conf 2>/dev/null &&
    grep -Eq "^[[:space:]]*AutomaticLogin[[:space:]]*=[[:space:]]*${me}[[:space:]]*\$" /etc/gdm3/custom.conf
}
siri_mac() {  # the paired gen-1 Siri Remote's address (Apple, product 0x026D), if bluetoothctl knows one
  local m
  for m in $(bluetoothctl --timeout 5 devices Paired 2>/dev/null | awk '$1 == "Device" { print $2 }'); do
    [[ $m =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]] &&
      bluetoothctl --timeout 5 info "$m" 2>/dev/null | grep -q 'Modalias: bluetooth:v004Cp026D' && { echo "$m"; return; }
  done
}
# From GameSpark lib/env.sh: root via passwordless sudo, else a privileged container (docker group). stdin is closed
# so neither can eat a caller's 'while read' input.
as_root() {
  if sudo -n true 2>/dev/null; then sudo "$@" </dev/null
  else docker run --rm --privileged --pid=host alpine nsenter -t 1 -m -u -i -n -p -- "$@" </dev/null; fi
}

enable() {
  local unit schema key value old n f
  say "SparkHome enable: repo $ROOT"
  if [ ! -f "$ROOT/sparkhome/__main__.py" ]; then
    [ -n "$DRY" ] || die "$ROOT is not a SparkHome checkout (no sparkhome/__main__.py)"
    say "warning: no sparkhome/ package in $ROOT (dry run continues)"
  elif [ -z "$DRY" ]; then   # never point the TV at a launcher that can't pass its own checks
    (cd "$ROOT" && python3 -m sparkhome --selftest >/dev/null 2>&1) ||
      die "python3 -m sparkhome --selftest fails in $ROOT (run it to see why; tests/run.sh runs on the Mac, not" \
          "here: it can start a Chrome beside the TV); the service was not (re)started"
  fi

  say "[1/5] user service $UNIT"
  unit="# Written by SparkHome deploy/install.sh; remove with: deploy/install.sh disable
[Unit]
Description=SparkHome TV launcher (daemon + Chrome kiosk)
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart=/usr/bin/python3 -m sparkhome
WorkingDirectory=$ROOT
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=2
# A restart (crash, update) replaces only the daemon: it re-adopts the running kiosk Chrome over CDP, and Steam or a
# game it started keeps running. 'deploy/install.sh disable' also closes the kiosk.
KillMode=process

[Install]
WantedBy=graphical-session.target"
  [ ! -f "$UNIT" ] || grep -q '^# Written by SparkHome deploy/install.sh' "$UNIT" ||
    die "$UNIT exists and was not written by this script; move it away first"
  if [ "$(cat "$UNIT" 2>/dev/null)" = "$unit" ]; then say "    unit unchanged"
  else say "    write the unit"; write "$UNIT" "$unit"; run systemctl --user daemon-reload; fi
  run systemctl --user enable sparkhome.service
  if ! systemctl --user is-active --quiet graphical-session.target; then
    say "    no graphical session right now: SparkHome starts at the next login"
  else say "    (re)start it, so code and config changes take effect"; run systemctl --user restart sparkhome.service; fi

  say "[2/5] GameSpark's Steam autostart (GDM autologin stays on)"
  if [ -f "$AUTOSTART/$GS_ENTRY" ]; then
    say "    move $AUTOSTART/$GS_ENTRY aside (disable puts it back)"
    run mkdir -p "$STATE"; run mv -f "$AUTOSTART/$GS_ENTRY" "$STATE/"
  elif [ -f "$STATE/$GS_ENTRY" ]; then say "    already moved aside"
  else say "    none installed"; fi

  say "[3/5] TV-mode gsettings (prior values kept in $STATE/gsettings.bak)"
  command -v dconf >/dev/null || die "needs dconf to save the prior values (apt install dconf-cli)"
  while read -r schema key value; do
    if [ "$(gsettings writable "$schema" "$key" 2>/dev/null)" != true ]; then
      say "    skip $schema $key: missing or locked here"; continue
    fi
    say "    $schema $key: $(gsettings get "$schema" "$key") -> $value"
    if ! saved "$schema" "$key"; then
      old=$(dconf read "/${schema//.//}/$key")   # the user's own value; empty = the schema default ('-')
      record gsettings.bak "$schema $key ${old:--}"
    fi
    run gsettings set "$schema" "$key" "$value"
    [ -n "$DRY" ] || [ "$(gsettings get "$schema" "$key")" = "$value" ] ||
      say "    warning: $key did not stick (is the GNOME session running? DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS)"
  done <<<"$TV_SETTINGS"

  say "[4/5] pop-up autostarts hidden with Hidden=true user overrides"
  for n in $HIDE; do
    f=$AUTOSTART/$n.desktop
    if [ ! -f "/etc/xdg/autostart/$n.desktop" ]; then say "    $n: not installed"
    elif grep -qxF "$n" "$STATE/autostart-hidden" 2>/dev/null; then say "    $n: already hidden"
    elif [ -e "$f" ]; then say "    $n: you have your own $f; left alone"
    else
      say "    $n: hide"
      write "$f" "[Desktop Entry]
Type=Application
Name=$n
Hidden=true"
      record autostart-hidden "$n"
    fi
  done

  say "[5/5] GDM autologin"
  if autologin_ok; then say "    ok: $me logs in automatically"
  else
    say "    NOT set: after a reboot the TV shows the login screen, not SparkHome. Fix it with either:"
    say "      ~/gamespark/system/console-mode.sh enable && $0 enable   (this re-run moves its Steam autostart aside again)"
    say "      or, as root, under [daemon] in /etc/gdm3/custom.conf: AutomaticLoginEnable=true and AutomaticLogin=$me"
  fi
  say "done. Logs: journalctl --user -u sparkhome -f   Check: $0 status   Undo: $0 disable"
}

disable() {
  local schema key value n want bad=''
  say "[1/4] user service"
  if [ -f "$UNIT" ]; then
    say "    stop, disable and remove sparkhome.service"
    run systemctl --user disable --now sparkhome.service
    # KillMode=process leaves the kiosk running after the daemon stops; Steam and games stay too. Match only argv in the
    # kiosk's shape (shell.py launches chrome --kiosk --class=SparkHome --user-data-dir=…), not a shell that quotes it.
    pgrep -u "$uid" -f -- '^[^ ]+ --kiosk --class=SparkHome --user-data-dir=' >/dev/null &&
      { say "    close the kiosk Chrome"; run pkill -u "$uid" -f -- '^[^ ]+ --kiosk --class=SparkHome --user-data-dir='; }
    run rm -f "$UNIT"; run systemctl --user daemon-reload
  else say "    not installed"; fi

  say "[2/4] GameSpark's Steam autostart"
  if [ ! -f "$STATE/$GS_ENTRY" ]; then say "    nothing saved"
  elif [ -e "$AUTOSTART/$GS_ENTRY" ]; then say "    $AUTOSTART/$GS_ENTRY is back already; saved copy left in $STATE"
  else
    say "    put back (Steam Big Picture opens at the next login)"
    run mkdir -p "$AUTOSTART"; run mv "$STATE/$GS_ENTRY" "$AUTOSTART/"
  fi

  say "[3/4] gsettings"
  if [ -f "$STATE/gsettings.bak" ]; then
    while read -r schema key value; do
      if [ "$value" = - ]; then want=; say "    $schema $key: back to the default"; run gsettings reset "$schema" "$key"
      else want=$value; say "    $schema $key: back to $value"; run gsettings set "$schema" "$key" "$value"; fi
      # gsettings exits 0 even when dconf can't commit (no session bus), so read back before dropping the only backup
      [ -n "$DRY" ] || [ "$(dconf read "/${schema//.//}/$key")" = "$want" ] ||
        { say "    warning: $key was not restored; gsettings.bak kept, so disable can be re-run"; bad=1; }
    done < "$STATE/gsettings.bak"
    [ -n "$bad" ] || run rm -f "$STATE/gsettings.bak"
  else say "    nothing saved"; fi

  say "[4/4] autostart overrides"
  if [ -f "$STATE/autostart-hidden" ]; then
    while read -r n; do say "    $n: unhide"; run rm -f "$AUTOSTART/$n.desktop"; done < "$STATE/autostart-hidden"
    run rm -f "$STATE/autostart-hidden"
  else say "    none created"; fi
  say "done. Root files (if any) stay until: $0 root undo"
}

status() {
  local cfg port cdp win
  cfg=$HOME/.config/sparkhome/sparkhome.json; [ -f "$cfg" ] || cfg=$ROOT/sparkhome.json
  read -r port cdp < <(python3 -c 'import json, sys
try: s = json.load(open(sys.argv[1]))["settings"]
except Exception: s = {}
print(s.get("port", 8780), s.get("cdp_port", 9223))' "$cfg" 2>/dev/null || echo 8780 9223)
  say "unit:      $(systemctl --user is-enabled sparkhome.service 2>/dev/null), $(systemctl --user is-active sparkhome.service 2>/dev/null)"
  say "daemon:    $(curl -fsS -m 2 "http://127.0.0.1:$port/api/state" 2>/dev/null || echo "no answer on 127.0.0.1:$port")"
  say "kiosk CDP: $(curl -fsS -m 2 "http://127.0.0.1:$cdp/json/version" 2>/dev/null |
    sed -n 's/.*"Browser": *"\([^"]*\)".*/\1/p' | grep . || echo "no answer on 127.0.0.1:$cdp")"
  win=$(xprop -root _NET_ACTIVE_WINDOW 2>/dev/null | awk '{print $NF}')
  case $win in
    0x*[1-9a-f]*) say "active:    $(xprop -id "$win" WM_CLASS _NET_WM_NAME 2>/dev/null | sed 's/^[^=]*= //' | paste -sd ' ' -)" ;;
    *) say "active:    no active window (or no access to $DISPLAY)" ;;
  esac
  if [ -f "$STATE/gsettings.bak" ]; then say "gsettings: TV mode ($(grep -c '' "$STATE/gsettings.bak") prior values saved)"
  else say "gsettings: untouched"; fi
  if [ -f "$STATE/$GS_ENTRY" ]; then say "gamespark: Steam autostart moved aside"
  elif [ -f "$AUTOSTART/$GS_ENTRY" ]; then say "gamespark: Steam autostart active (Big Picture at login)"
  else say "gamespark: no Steam autostart"; fi
  say "hidden:    $(paste -sd ' ' "$STATE/autostart-hidden" 2>/dev/null || echo none)"
  if autologin_ok; then say "autologin: on for $me"; else say "autologin: OFF (enable prints the fix)"; fi
  say "root:      chrome policy $([ -f "$POLICY" ] && echo present || echo absent), remote hwdb $([ -f "$HWDB" ] && echo present || echo absent)," \
    "siri rule $([ -f "$SIRI_RULES" ] && echo present || echo absent)," \
    "siri mic $(if [ -f "$MIC_UNIT" ]; then systemctl is-active sparkhome-siri-mic.service 2>/dev/null; else echo absent; fi)"
}

mkdir_root() { # DIR: create it and any missing parents as root, recording each for root undo
  [ -d "$1" ] && return
  mkdir_root "$(dirname "$1")"
  run as_root mkdir -m 0755 "$1" || die "mkdir $1 failed"
  record root.log "mkdir $1"
}
put_root() {   # SRC DST: install SRC as root-owned DST (0644), backing up a pre-existing DST once. 1 = already there
  if cmp -s "$1" "$2"; then say "    already in place"; return 1; fi
  if ! grep -qxF -e "new $2" -e "replaced $2" "$STATE/root.log" 2>/dev/null; then
    if [ -e "$2" ]; then
      say "    back up the existing file to $STATE/root/"
      run cp "$2" "$STATE/root/" || die "cannot back up $2"
      record root.log "replaced $2"
    else
      mkdir_root "$(dirname "$2")"
      record root.log "new $2"
    fi
  fi
  say "    install $2"
  run as_root install -m 0644 "$1" "$2" || die "installing $2 failed"
}

root_apply() {
  local mac new=
  say "SparkHome root: system files (backups and a change log in $STATE)"
  [ -n "$DRY" ] || as_root true || die "root needs passwordless sudo or membership in the docker group"
  run mkdir -p "$STATE/root"
  say "[1/4] Chrome managed policy $POLICY (applies to every Chrome profile on this machine)"
  write "$STATE/chrome-policy.json" '{"AutoplayAllowed": true, "TranslateEnabled": false, "CommandLineFlagSecurityWarningsEnabled": false}'
  put_root "$STATE/chrome-policy.json" "$POLICY"
  say "[2/4] remote key remaps $HWDB (from deploy/remotes.hwdb)"
  if put_root "$ROOT/deploy/remotes.hwdb" "$HWDB"; then
    run as_root systemd-hwdb update "$HWDB_OUT" || die "systemd-hwdb update failed"
    if grep -q '^evdev:' "$ROOT/deploy/remotes.hwdb"; then
      # systemd's 60-keyboard.hwdb: "If in doubt, simply use /dev/input/event* to reload all input rules"
      say "    apply to connected devices (keyboards and mice re-initialise for a moment)"
      run as_root udevadm trigger --action=change /dev/input/event*
    else say "    no active entries in deploy/remotes.hwdb yet: nothing to apply"; fi
  fi
  say "[3/4] Siri Remote access $SIRI_RULES (from deploy/71-sparkhome-siri.rules: its hidraw node, for you)"
  if put_root "$ROOT/deploy/71-sparkhome-siri.rules" "$SIRI_RULES"; then
    run as_root udevadm control --reload || die "udevadm control --reload failed"
    say "    apply to a connected remote"
    run as_root udevadm trigger --action=change --subsystem-match=hidraw
  fi
  say "[4/4] Siri Remote microphone $MIC_UNIT (from deploy/sparkhome-siri-mic.service) running $MIC_TAP"
  mac=$(siri_mac)
  if [ -z "$mac" ]; then
    say "    no paired Siri Remote (bluetoothctl): skipped; pair it (README: Siri Remote), then run root again"
  else
    say "    for the remote $mac, as $me, with CAP_NET_RAW only"
    write "$STATE/sparkhome-siri-mic.service" "$(sed -e "s/@USER@/$me/g" -e "s/@UID@/$uid/g" -e "s/@REMOTE@/$mac/g" \
      "$ROOT/deploy/sparkhome-siri-mic.service")"
    put_root "$ROOT/sparkhome/hcitap.py" "$MIC_TAP" && new=1
    put_root "$STATE/sparkhome-siri-mic.service" "$MIC_UNIT" && new=1
    if [ -n "$new" ]; then
      say "    (re)start it; it also starts with your systemd user instance (at login, or at boot when lingering)"
      run as_root systemctl daemon-reload || die "systemctl daemon-reload failed"
      run as_root systemctl enable sparkhome-siri-mic.service || die "enabling sparkhome-siri-mic failed"
      run as_root systemctl restart sparkhome-siri-mic.service || die "starting sparkhome-siri-mic failed"
    else run as_root systemctl enable --now sparkhome-siri-mic.service || die "enabling sparkhome-siri-mic failed"; fi
    [ -n "$DRY" ] || { sleep 1; systemctl is-active --quiet sparkhome-siri-mic.service; } ||
      die "sparkhome-siri-mic isn't running: journalctl -u sparkhome-siri-mic"
  fi
  say "done. Undo: $0 root undo"
}

root_undo() {
  local op path hw='' rules='' mic='' bad=''
  [ -f "$STATE/root.log" ] || { say "root undo: nothing recorded in $STATE/root.log"; return; }
  say "SparkHome root undo (from $STATE/root.log, newest first)"
  [ -n "$DRY" ] || as_root true || die "root needs passwordless sudo or membership in the docker group"
  if [ -e "$MIC_UNIT" ] && grep -qxF -e "new $MIC_UNIT" -e "replaced $MIC_UNIT" "$STATE/root.log"; then
    say "    stop and disable sparkhome-siri-mic (while its unit file is still there)"
    run as_root systemctl disable --now sparkhome-siri-mic.service || bad=1
  fi
  while read -r op path; do
    case $op in
      new) say "    remove $path"; run as_root rm -f "$path" || bad=1 ;;
      replaced) say "    restore $path"; run as_root cp "$STATE/root/${path##*/}" "$path" || bad=1 ;;
      mkdir) [ ! -d "$path" ] || { say "    remove dir $path"; run as_root rmdir "$path" || say "    kept $path (not empty)"; } ;;
    esac
    [ "$path" != "$HWDB" ] || hw=1
    [ "$path" != "$SIRI_RULES" ] || rules=1
    [ "$path" != "$MIC_UNIT" ] || mic=1
  done < <(tac "$STATE/root.log")
  if [ -n "$hw" ]; then
    run as_root systemd-hwdb update "$HWDB_OUT" || bad=1
    say "    remaps already applied to a connected remote last until it is re-plugged (or the Spark reboots)"
  fi
  if [ -n "$rules" ]; then
    run as_root udevadm control --reload || bad=1
    say "    a connected Siri Remote stays readable until it reconnects"
  fi
  [ -z "$mic" ] || run as_root systemctl daemon-reload || bad=1
  [ -z "$bad" ] || die "some steps failed; $STATE/root.log is kept, so 'root undo' can be re-run"
  run rm -rf "$STATE/root" "$STATE/root.log" "$STATE/chrome-policy.json" "$STATE/sparkhome-siri-mic.service"
  say "done"
}

args=()
for a in "$@"; do case $a in -n) DRY=1 ;; *) args+=("$a") ;; esac; done
[ "$uid" != 0 ] || die "run this as your user, not root: the root steps use sudo -n or docker by themselves"
[ -z "$DRY" ] || say "dry run: nothing will be changed"
case "${args[*]:-}" in
  enable) enable ;;
  disable) disable ;;
  status) status ;;
  root) root_apply ;;
  "root undo") root_undo ;;
  *) sed -n '2,/^set -u/{/^#/s/^# \{0,1\}//p;}' "$0"; exit 2 ;;
esac
