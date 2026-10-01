#!/usr/bin/env bash
# Voxwire — always-on macOS service (launchd LaunchAgent).
#
# macOS does NOT use systemd; it uses launchd. This installs a per-user
# LaunchAgent so Voxwire starts at login and stays running, and you arm the mic
# whenever you need it (menubar, or the web UI at http://127.0.0.1:8123).
#
#   ./scripts/service-macos.sh install            # menubar app, always-on (default)
#   ./scripts/service-macos.sh install --headless # server only (headless Mac / Mac mini)
#   ./scripts/service-macos.sh status
#   ./scripts/service-macos.sh restart
#   ./scripts/service-macos.sh logs               # tail the log
#   ./scripts/service-macos.sh uninstall
#
# A LaunchAgent (not a LaunchDaemon) runs inside your GUI login session, so the
# macOS permission prompts (Microphone, Accessibility, Input Monitoring) appear
# and stick to the venv's python. Grant them the first time you arm/dictate.
set -euo pipefail

LABEL="io.voxwire"
# The old scripts/service.sh installed the same app as io.voxwire.agent. Left in
# place it starts a second copy at login, so install/uninstall retire it.
LEGACY_LABEL="io.voxwire.agent"
here="$(cd "$(dirname "$0")/.." && pwd)"
app="$here/voxwire"
py="$app/.venv/bin/python"
agents="$HOME/Library/LaunchAgents"
plist="$agents/$LABEL.plist"
legacy_plist="$agents/$LEGACY_LABEL.plist"
log="$HOME/Library/Logs/voxwire.log"
# launchd appends forever and has no per-user rotation, so (re)starts rotate it.
log_max_bytes=$((10 * 1024 * 1024))
domain="gui/$(id -u)"
port=8123
# A Voxwire-specific endpoint, so another process holding the port can't pass as us.
probe="http://127.0.0.1:$port/api/dictation/status"

die() { echo "✗ $*" >&2; exit 1; }
# install stages the new plist (and a copy of the old one) beside the real one;
# whatever is left over when the script exits is scratch.
staged=""; backup=""
trap 'rm -f "$staged" "$backup"' EXIT
# Escape XML metacharacters so a repo path containing & < > can't break the plist.
xml_escape() { printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'; }

[ "$(uname -s)" = "Darwin" ] || die "this installer is macOS-only (launchd). Always-on for Linux or a Pi isn't packaged yet: run 'cd voxwire && ./run.sh' under your own service manager."

loaded() { launchctl print "$domain/$1" >/dev/null 2>&1; }

# Stop a job and confirm launchd really dropped it. bootout's exit status is not
# trustworthy either way (it can fail while the job lingers, or report an error
# after tearing it down), so judge by `launchctl print` and give it a few seconds.
stop_job() {
  local label="$1" i
  loaded "$label" || return 0
  launchctl bootout "$domain/$label" 2>/dev/null || true
  for i in 1 2 3 4 5 6 7 8 9 10; do
    loaded "$label" || return 0
    sleep 0.5
  done
  return 1
}

retire_legacy() {
  if loaded "$LEGACY_LABEL"; then
    stop_job "$LEGACY_LABEL" \
      || die "the old $LEGACY_LABEL job is still loaded after bootout; left $legacy_plist in place. Try: launchctl bootout $domain/$LEGACY_LABEL"
    echo "  stopped the old $LEGACY_LABEL job (from scripts/service.sh)"
  fi
  if [ -f "$legacy_plist" ]; then
    rm -f "$legacy_plist"
    echo "  removed $legacy_plist"
  fi
}

write_plist() {
  # Writes and validates the plist into $staged; nothing live is touched.
  # $1 = entry script (menubar.py | server.py), $2 = ProcessType
  local script="$1" ptype="$2"
  mkdir -p "$agents"
  # Dot-prefixed and not *.plist, so launchd never loads a half-written file.
  staged=$(mktemp "$agents/.$LABEL.new.XXXXXX")
  local e_py e_prog e_app e_log
  e_py=$(xml_escape "$py"); e_prog=$(xml_escape "$app/$script")
  e_app=$(xml_escape "$app"); e_log=$(xml_escape "$log")
  cat > "$staged" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$e_py</string>
        <string>$e_prog</string>
    </array>
    <key>WorkingDirectory</key><string>$e_app</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key>
    <dict><key>SuccessfulExit</key><false/></dict>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>ProcessType</key><string>$ptype</string>
    <key>LimitLoadToSessionType</key><string>Aqua</string>
    <key>StandardOutPath</key><string>$e_log</string>
    <key>StandardErrorPath</key><string>$e_log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
PLIST
  plutil -lint "$staged" >/dev/null \
    || die "generated plist failed validation; nothing was changed."
}

# The log can hold transcripts and agent output: keep it owner-only (launchd
# appends to an existing file, keeping its mode) and bounded to one rotation.
# Called while the job is stopped or about to be restarted, so it reopens fresh.
prepare_log() {
  mkdir -p "$(dirname "$log")"
  if [ -f "$log" ] && [ "$(wc -c < "$log")" -gt "$log_max_bytes" ]; then
    mv -f "$log" "$log.1"
    echo "  rotated the log (> $((log_max_bytes / 1024 / 1024)) MB) → $log.1"
  fi
  (umask 077; : >> "$log")
  chmod 600 "$log"
  [ ! -f "$log.1" ] || chmod 600 "$log.1"
}

# Swap the staged plist in and load it. If launchd refuses it, put back what was
# running before (the previous install, else the legacy agent) rather than leave
# nothing running. The caller has already stopped both jobs.
activate() {
  if [ -f "$plist" ]; then
    backup=$(mktemp "$agents/.$LABEL.prev.XXXXXX")
    cp -p "$plist" "$backup"
  fi
  mv -f "$staged" "$plist"; staged=""
  # RunAtLoad starts it; no kickstart -k here, which would kill the fresh
  # process mid-startup (or mid macOS permission prompt).
  launchctl bootstrap "$domain" "$plist" && return 0
  local restore=""
  if [ -n "$backup" ]; then
    mv -f "$backup" "$plist"; backup=""; restore="$plist"
  else
    rm -f "$plist"
    [ -f "$legacy_plist" ] && restore="$legacy_plist"
  fi
  if [ -n "$restore" ] && launchctl bootstrap "$domain" "$restore" 2>/dev/null; then
    die "launchctl bootstrap rejected the new plist; restored the previous service ($restore)."
  fi
  die "launchctl bootstrap rejected the new plist and nothing could be restored; Voxwire is not running."
}

load() {
  launchctl bootstrap "$domain" "$plist"   # RunAtLoad starts it
}

case "${1:-}" in
  install)
    [ -x "$py" ] || die "venv not found at $py — run ./scripts/install.sh first."
    if [ "${2:-}" = "--headless" ]; then
      script="server.py"; ptype="Background"
      echo "→ installing Voxwire (headless server) as a launchd LaunchAgent…"
    else
      script="menubar.py"; ptype="Interactive"
      # Without rumps the menubar app dies at import and KeepAlive would relaunch
      # it every ThrottleInterval, so stop here instead of installing a crash loop.
      if ! "$py" -c "import rumps" >/dev/null 2>&1; then
        command -v uv >/dev/null 2>&1 \
          || die "the menubar app needs rumps and uv isn't available to install it. Install the [macos] extra into $py, or use --headless."
        echo "  installing rumps (menubar UI)…"
        uv pip install --python "$py" rumps >/dev/null \
          || die "installing rumps with uv failed. Install the [macos] extra, or use --headless."
        "$py" -c "import rumps" >/dev/null 2>&1 \
          || die "rumps still doesn't import in $py. Install the [macos] extra, or use --headless."
      fi
      echo "→ installing Voxwire (menubar app) as a launchd LaunchAgent…"
    fi
    # Stage + validate first: a bad plist must not cost the user a working service.
    write_plist "$script" "$ptype"
    current_was_loaded=""
    loaded "$LABEL" && current_was_loaded=1
    stop_job "$LABEL" \
      || die "$LABEL is still loaded after bootout, so it can't be reloaded; nothing was changed. Try: launchctl bootout $domain/$LABEL"
    # Stop the legacy job now (it holds :$port) but keep its plist until the new
    # job is loaded, so activate can fall back to it. If it won't stop, put the
    # current job back as it was (its live plist is untouched until activate)
    # rather than fail with Voxwire left down.
    if loaded "$LEGACY_LABEL"; then
      if ! stop_job "$LEGACY_LABEL"; then
        stuck="the old $LEGACY_LABEL job is still loaded after bootout"
        [ -n "$current_was_loaded" ] || die "$stuck; nothing was changed. Try: launchctl bootout $domain/$LEGACY_LABEL"
        launchctl bootstrap "$domain" "$plist" 2>/dev/null \
          || die "$stuck, and $LABEL could not be started again; run: $0 restart"
        die "$stuck; restarted $LABEL unchanged. Try: launchctl bootout $domain/$LEGACY_LABEL"
      fi
      echo "  stopped the old $LEGACY_LABEL job (from scripts/service.sh)"
    fi
    prepare_log
    activate
    retire_legacy
    echo "✓ installed → $plist"
    echo "  starts at login + now; logs: $log"
    echo "  arm the mic from the menubar (🎙) or the web UI: http://127.0.0.1:8123"
    ;;
  uninstall)
    # Never report "uninstalled" (or delete the plist) while the job, and with it
    # possibly an armed mic, is still running.
    stop_job "$LABEL" \
      || die "$LABEL is still loaded after bootout; NOT uninstalled ($plist kept). Try: launchctl bootout $domain/$LABEL"
    rm -f "$plist"
    retire_legacy
    echo "✓ uninstalled ($LABEL removed; log kept at $log)"
    ;;
  restart)
    [ -f "$plist" ] || die "not installed ($plist is missing). Run: $0 install"
    # kickstart needs a loaded job; after a manual bootout or an interrupted
    # install, load it again instead.
    prepare_log
    if loaded "$LABEL"; then
      launchctl kickstart -k "$domain/$LABEL"
    else
      load
    fi
    echo "✓ restarted $LABEL"
    ;;
  status)
    echo "== launchd =="
    launchctl print "$domain/$LABEL" 2>/dev/null | grep -E 'state =|pid =|last exit' || echo "  not loaded"
    if loaded "$LEGACY_LABEL"; then
      echo "  ! the old $LEGACY_LABEL job is loaded too; re-run install to retire it"
    fi
    echo "== http =="
    # Bounded, so a hung listener can't hang status itself.
    if body=$(curl -fsS --max-time 3 "$probe" 2>/dev/null) && [[ "$body" == *'"hotkey"'* ]]; then
      echo "  ✓ Voxwire responding on http://127.0.0.1:$port"
    elif curl -fsS --max-time 3 -o /dev/null "http://127.0.0.1:$port/" 2>/dev/null; then
      echo "  ✗ something else is serving :$port (it doesn't answer $probe like Voxwire)"
    else
      echo "  ✗ Voxwire not responding on :$port (see $log)"
    fi
    ;;
  logs)
    exec tail -n 50 -f "$log"
    ;;
  *)
    grep -E '^# ' "$0" | sed -E 's/^# ?//'
    exit 2
    ;;
esac
