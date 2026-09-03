#!/usr/bin/env bash
# Screenshot an already-generated /human-review report, tab by tab, and publish the images
# as a static gallery under demo/<slug>/ in the human-review repo — from where the `pages`
# workflow serves them at https://victorrentea.github.io/human-review/<slug>/.
#
# This is the cheap sibling of publish-demo.sh. That one copies the whole live snapshot —
# 1.5 MB of interactive HTML whose vscode:// links only resolve on the machine that built
# it. This one produces something a stranger can look at: one full-page image per tab, in
# light and dark, plus the feature video, which no screenshot can stand in for.
#
# Everything expensive happens in shoot-review.py; the shell here is provisioning, copying
# and git.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO="$HOME/workspace/human-review"
DEFAULT_SRC="./.human-review/review.html"
VENV="${HUMAN_REVIEW_SHOTS_VENV:-$HOME/.cache/human-review/shots-venv}"
PAGES_BASE="https://victorrentea.github.io/human-review"

usage() {
  cat <<'USAGE'
publish-demo-shots.sh — publish screenshots of a /human-review report to GitHub Pages

Usage:
  publish-demo-shots.sh [<review.html|snapshot-dir>] [options]
  publish-demo-shots.sh --help

Arguments:
  review.html | dir   The finished report, or the directory holding it.
                      Default: ./.human-review/review.html

Options:
  --slug NAME     Folder under demo/ to publish as. Default: the name of the project
                  directory the snapshot sits in, lowercased to [a-z0-9-].
  --title "..."   Override the title (otherwise read from content.json).
  --no-push       Build and stage the files, but do not commit or push.
  --help          This text.

Environment:
  HUMAN_REVIEW_REPO         Checkout of victorrentea/human-review to publish into.
                            Default: ~/workspace/human-review
  HUMAN_REVIEW_SHOTS_VENV   Where to keep the Playwright virtualenv this script
                            provisions on first run. Default: ~/.cache/human-review/shots-venv

Result:
  demo/<slug>/index.html   a gallery: one full-page shot per tab, light and dark,
                           the feature video, and a note that it is a static snapshot
  demo/<slug>/light|dark/  the PNGs
  demo/index.html          gains a card for <slug> inside its managed marker region
USAGE
}

SRC=""
SLUG=""
TITLE=""
PUSH=1

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help|help) usage; exit 0 ;;
    --slug) SLUG="${2-}"; shift 2 ;;
    --title) TITLE="${2-}"; shift 2 ;;
    --no-push) PUSH=0; shift ;;
    --) shift; break ;;
    -*) echo "publish-demo-shots: unknown option: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [ -n "$SRC" ]; then
        echo "publish-demo-shots: more than one source given: $SRC and $1" >&2
        exit 2
      fi
      SRC="$1"; shift ;;
  esac
done

SRC="${SRC:-$DEFAULT_SRC}"
REPO="${HUMAN_REVIEW_REPO:-$DEFAULT_REPO}"

# Accept either the file or the directory that holds it.
if [ -d "$SRC" ]; then
  SRC_DIR="$SRC"
  SRC_FILE="$SRC/review.html"
else
  SRC_FILE="$SRC"
  SRC_DIR="$(dirname "$SRC")"
fi

if [ ! -f "$SRC_FILE" ]; then
  echo "publish-demo-shots: no review.html at $SRC" >&2
  echo "                    generate one with /human-review first" >&2
  exit 1
fi

SRC_DIR="$(cd -- "$SRC_DIR" && pwd)"
SRC_FILE="$SRC_DIR/$(basename "$SRC_FILE")"

if [ ! -d "$REPO/.git" ]; then
  echo "publish-demo-shots: not a git checkout: $REPO" >&2
  echo "                    set HUMAN_REVIEW_REPO to your human-review clone" >&2
  exit 1
fi

# The slug defaults to the project the snapshot belongs to — .human-review's parent — since
# that is the name a reader recognises, not "human-review".
if [ -z "$SLUG" ]; then
  SLUG="$(basename "$(dirname "$SRC_DIR")")"
fi
SLUG="$(printf '%s' "$SLUG" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9-]/-/g; s/--*/-/g; s/^-//; s/-$//')"
if [ -z "$SLUG" ]; then
  echo "publish-demo-shots: could not derive a slug; pass --slug NAME" >&2
  exit 2
fi

# ---------------------------------------------------------------------------- playwright
# Python Playwright is not something a training laptop has lying around, and installing it
# into the system interpreter is not ours to do. One throwaway venv, provisioned once and
# reused on every later run.
if [ ! -x "$VENV/bin/python" ]; then
  echo "publish-demo-shots: provisioning Playwright in $VENV (first run only)…" >&2
  mkdir -p "$(dirname "$VENV")"
  python3 -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet playwright
fi
if ! "$VENV/bin/python" -c "import playwright" >/dev/null 2>&1; then
  "$VENV/bin/python" -m pip install --quiet playwright
fi
# `install` is idempotent and returns in about a second when the browser is already in the
# shared ms-playwright cache, so it is cheaper to always run it than to guess whether the
# revision this playwright wants happens to be the one on disk.
"$VENV/bin/python" -m playwright install chromium >&2

# ---------------------------------------------------------------------------- the shots
DEST="$REPO/demo/$SLUG"
MANIFEST="$(mktemp -t human-review-shots)"
trap 'rm -f "$MANIFEST"' EXIT

echo "publish-demo-shots: shooting $SRC_FILE -> $DEST" >&2
"$VENV/bin/python" "$SCRIPT_DIR/shoot-review.py" "$SRC_FILE" \
  --out "$DEST" --slug "$SLUG" ${TITLE:+--title "$TITLE"} \
  --manifest "$MANIFEST" >/dev/null

if [ ! -s "$MANIFEST" ]; then
  echo "publish-demo-shots: the screenshot pass produced no manifest" >&2
  exit 1
fi

# GitHub Pages refuses to serve a file over 100 MB and chokes long before that.
if find "$DEST" -type f -size +50M | grep -q .; then
  echo "publish-demo-shots: refusing — these files exceed 50 MB:" >&2
  find "$DEST" -type f -size +50M >&2
  exit 1
fi

# --------------------------------------------------------------- the landing page's list
# Hand-editing demo/index.html once per snapshot is how that file rots. The cards this
# script owns live between two markers and are rewritten wholesale; anything above the
# markers is Victor's prose and is never touched.
LANDING="$REPO/demo/index.html"
python3 - "$LANDING" "$SLUG" "$MANIFEST" <<'PY'
import html, json, re, sys
from pathlib import Path

landing, slug, manifest_path = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

BEGIN = "<!-- shot-galleries:begin -->"
END = "<!-- shot-galleries:end -->"

page = landing.read_text(encoding="utf-8")
if BEGIN not in page or END not in page:
    # First run: open the region just before the caveat, so the galleries read as a second
    # list under the same heading and the hand-written cards above stay where they are.
    anchor = "  <h2>One caveat</h2>"
    if anchor not in page:
        anchor = "</main>"
    page = page.replace(anchor, f"  {BEGIN}\n  {END}\n\n{anchor}", 1)

start = page.index(BEGIN) + len(BEGIN)
end = page.index(END)
region = page[start:end]

cards = dict(re.findall(r'(?s)<!-- gallery:([a-z0-9-]+) -->(.*?)<!-- /gallery -->', region))

subtitle = manifest.get("subtitle") or ""
tabs = manifest.get("tabs") or []
schemes = manifest.get("schemes") or ["light"]
title = html.escape(manifest.get("title") or slug)
themes = "light + dark" if len(schemes) > 1 else "one theme"

cards[slug] = f"""
  <a class="card" href="{slug}/">
    <h3>{title} — screenshots</h3>
    <p class="sub">{subtitle}</p>
    <ul class="stats">
      <li>tabs <b>{len(tabs)}</b></li>
      <li>screenshots <b>{manifest.get('shots', 0)}</b></li>
      <li>themes <b>{themes}</b></li>
      <li>video <b>{'yes' if manifest.get('videos') else 'no'}</b></li>
    </ul>
  </a>
"""

rebuilt = ["\n  <h2>Screenshot galleries</h2>\n",
           '  <p>Static, click-free snapshots — every tab as one full-page image, plus the '
           'feature video. Nothing to install, no <code>vscode://</code> links to be '
           'disappointed by.</p>\n']
for key in sorted(cards):
    rebuilt.append(f"  <!-- gallery:{key} -->{cards[key]}  <!-- /gallery -->\n")

landing.write_text(page[:start] + "".join(rebuilt) + page[end:], encoding="utf-8")
print(f"landing page: {len(cards)} gallery card(s)", file=sys.stderr)
PY

# ---------------------------------------------------------------------------------- git
SHOTS=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["shots"])' "$MANIFEST")
TABS=$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["tabs"]))' "$MANIFEST")
SIZE=$(du -sh "$DEST" | cut -f1)

if [ "$PUSH" -eq 1 ]; then
  git -C "$REPO" add "demo/$SLUG" "demo/index.html"
  if git -C "$REPO" diff --cached --quiet; then
    echo "publish-demo-shots: nothing changed; no commit made" >&2
  else
    git -C "$REPO" commit --quiet -F - <<COMMIT
demo: publish $SLUG as a screenshot gallery

$TABS tabs of the review page, shot full-page in light and dark, with the feature
video alongside — the one part of the report an image cannot carry.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QUxuEKkUCdX7PbxH7871GY
COMMIT
    git -C "$REPO" push --quiet
    echo "publish-demo-shots: committed and pushed" >&2
  fi
else
  git -C "$REPO" add --intent-to-add "demo/$SLUG" "demo/index.html" >/dev/null 2>&1 || true
  echo "publish-demo-shots: --no-push, so nothing was committed" >&2
fi

echo
echo "$SHOTS screenshots across $TABS tabs ($SIZE)"
echo "$PAGES_BASE/$SLUG/"
