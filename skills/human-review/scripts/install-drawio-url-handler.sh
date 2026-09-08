#!/bin/bash
# Register a `drawio://` URL scheme on this Mac, so a link in the review page can open a
# diagram in the draw.io desktop app.
#
# Why this exists at all: draw.io.app declares `CFBundleDocumentTypes` and *no*
# `CFBundleURLTypes`, so there is no scheme a browser can hand it a file through. A
# `file://` link is not a substitute — Chrome renders a `.drawio.png` as a picture, and
# a download opens a copy in ~/Downloads rather than the file the reviewer has to edit.
#
# So we install the smallest possible shim: an AppleScript app that answers `drawio://`
# and shells out to `open -a draw.io <path>`. It has no UI and no dock icon.
#
#   ./install-drawio-url-handler.sh            # install / re-install
#   ./install-drawio-url-handler.sh --check    # say what is registered, change nothing
#
# `drawio-diff.py` writes the links; this makes them do something.
set -euo pipefail

APP="$HOME/Applications/Drawio Opener.app"
BUNDLE_ID="ro.victorrentea.drawio-opener"
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister

if [[ "${1:-}" == "--check" ]]; then
  if [[ -d "$APP" ]]; then
    echo "installed: $APP"
    /usr/libexec/PlistBuddy -c "Print :CFBundleURLTypes:0:CFBundleURLSchemes:0" \
      "$APP/Contents/Info.plist" | sed 's/^/  scheme: /'
  else
    echo "not installed — run this script with no arguments"
    exit 1
  fi
  exit 0
fi

command -v osacompile >/dev/null || { echo "osacompile is missing" >&2; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# The AppleScript half is deliberately three lines: everything that needs quoting
# discipline lives in the shell script it calls, where quoting is ordinary.
cat >"$work/handler.applescript" <<'APPLESCRIPT'
on open location this_URL
	set opener to POSIX path of (path to resource "open-drawio.sh")
	do shell script quoted form of opener & " " & quoted form of this_URL
end open location
APPLESCRIPT

rm -rf "$APP"
mkdir -p "$(dirname "$APP")"
osacompile -o "$APP" "$work/handler.applescript"

# Percent-decoding without a Python dependency: `%2F` → `\x2F`, which `printf %b` reads.
# Safe because the only backslashes that can reach here are the ones this line writes —
# `drawio-diff.py` percent-encodes everything outside the URL-safe set, a literal `\`
# included.
cat >"$APP/Contents/Resources/open-drawio.sh" <<'OPENER'
#!/bin/bash
set -euo pipefail
url="${1:-}"
path="${url#drawio://}"
path="$(printf '%b' "${path//%/\\x}")"
[[ -e "$path" ]] || { osascript -e "display notification \"No file at $path\" with title \"draw.io\""; exit 1; }
open -b com.jgraph.drawio.desktop "$path" 2>/dev/null || open -a "draw.io" "$path"
OPENER
chmod +x "$APP/Contents/Resources/open-drawio.sh"

plist="$APP/Contents/Info.plist"
p() { /usr/libexec/PlistBuddy -c "$1" "$plist" >/dev/null; }
# osacompile writes no CFBundleIdentifier at all, and does write CFBundleName — so each
# scalar is set the only way that works for both: add it, then set it.
put() { /usr/libexec/PlistBuddy -c "Add :$1 $2 $3" "$plist" >/dev/null 2>&1 || true
        p "Set :$1 $3"; }
put CFBundleIdentifier string "$BUNDLE_ID"
put CFBundleName string "Drawio Opener"
put LSUIElement bool true
p "Add :CFBundleURLTypes array"
p "Add :CFBundleURLTypes:0 dict"
p "Add :CFBundleURLTypes:0:CFBundleURLName string $BUNDLE_ID"
p "Add :CFBundleURLTypes:0:CFBundleURLSchemes array"
p "Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string drawio"

# Without this, LaunchServices does not learn the scheme until something happens to
# rescan ~/Applications, and the first click reports "no application can open drawio://".
"$LSREGISTER" -f -R -trusted "$APP"

echo "registered drawio:// → $APP"
echo "test it with:  open 'drawio://$(printf %s "$PWD" | sed 's/ /%20/g')'"
