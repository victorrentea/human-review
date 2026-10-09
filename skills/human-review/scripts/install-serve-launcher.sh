#!/bin/bash
# Install `serve-launcher.py` as a LaunchAgent, so the Serve chip on a review page opened
# from disk starts the review server itself and reloads the tab from it — instead of
# copying a terminal command for the reader to paste.
#
# Why a listener and not a URL scheme like install-drawio-url-handler.sh: Chrome asks
# "Open <app>?" on every custom-scheme click from a file:// page, and a page cannot tell
# whether a scheme handler is installed at all. See serve-launcher.py.
#
#   ./install-serve-launcher.sh              # install / re-install (and restart)
#   ./install-serve-launcher.sh --check      # say whether it answers, change nothing
#   ./install-serve-launcher.sh --uninstall  # stop it and remove the agent
set -euo pipefail

LABEL="ro.victorrentea.human-review-serve-launcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/serve-launcher.py"
PORT=7653
LOG="$HOME/Library/Logs/human-review-serve-launcher.log"

if [[ "${1:-}" == "--check" ]]; then
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/ping"; then echo; exit 0; fi
  echo "not answering on :$PORT — run this script with no arguments"; exit 1
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$PLIST"; echo "removed $LABEL"; exit 0
fi

# The interpreter matters twice over: serve-review.py runs its refreshes with its own
# sys.executable, and those need Pygments/PyYAML/Playwright — the python.org 3.12, not
# Homebrew's. Same for PATH: launchd gives an agent /usr/bin:/bin and nothing else, and a
# served page's buttons run git, docker, npm and the producers' `#!/usr/bin/env python3`.
PY=""
for c in /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 /usr/local/bin/python3 "$(command -v python3)"; do
  if [[ -x "$c" ]] && "$c" -c 'import pygments' 2>/dev/null; then PY="$c"; break; fi
done
[[ -n "$PY" ]] || { echo "no python3 with Pygments found — the review build needs it" >&2; exit 1; }
PYBIN="$(dirname "$PY")"
AGENT_PATH="$PYBIN:/usr/local/bin:/opt/homebrew/bin:$PATH:/usr/bin:/bin:/usr/sbin:/sbin"
# De-duplicated, first occurrence wins.
AGENT_PATH="$(printf %s "$AGENT_PATH" | awk -v RS=: '!seen[$0]++ && length($0)' | paste -sd: -)"

esc() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<<"$1"; }
mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
cat >"$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(esc "$PY")</string>
    <string>$(esc "$SCRIPT")</string>
    <string>--port</string><string>$PORT</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(esc "$AGENT_PATH")</string>
    <key>LANG</key><string>en_US.UTF-8</string>
    <key>PYTHONIOENCODING</key><string>utf-8</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <!-- The review servers it starts detach (setsid) and must outlive a restart of this. -->
  <key>AbandonProcessGroup</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$(esc "$LOG")</string>
  <key>StandardErrorPath</key><string>$(esc "$LOG")</string>
</dict>
</plist>
PLIST

launchctl bootstrap "gui/$(id -u)" "$PLIST"
for _ in $(seq 1 30); do
  curl -fsS --max-time 1 "http://127.0.0.1:$PORT/ping" >/dev/null 2>&1 && break
  sleep 0.2
done
if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/ping" >/dev/null; then
  echo "serve launcher answering on 127.0.0.1:$PORT ($LABEL, log: $LOG)"
else
  echo "installed $PLIST but nothing answers on :$PORT — see $LOG" >&2; exit 1
fi
