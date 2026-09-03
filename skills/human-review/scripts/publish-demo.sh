#!/usr/bin/env bash
# Copy a finished /human-review snapshot into the human-review repo's demo/ folder, from
# where the `pages` workflow publishes it to GitHub Pages.
#
# The snapshot is copied verbatim: review.html is a faithful artifact of the run that
# produced it and must not be rewritten here.
set -euo pipefail

DEFAULT_SRC=".human-review"
DEFAULT_REPO="$HOME/workspace/human-review"

usage() {
  cat <<'USAGE'
publish-demo.sh — stage a /human-review snapshot for GitHub Pages

Usage:
  publish-demo.sh <slug> [source-dir]
  publish-demo.sh --help

Arguments:
  slug          Directory name under demo/ to publish as, e.g. petclinic-visit-vet.
                Letters, digits, dashes and underscores only.
  source-dir    The snapshot directory to copy (default: .human-review), the one
                holding review.html, content.json and assets/.

Environment:
  HUMAN_REVIEW_REPO   Checkout of victorrentea/human-review to copy into.
                      Default: ~/workspace/human-review

The script only copies files; it never commits or pushes. It prints the git commands
to run once you have looked at the result.
USAGE
}

case "${1-}" in
  -h|--help|help)
    usage
    exit 0
    ;;
esac

if [ $# -lt 1 ]; then
  usage >&2
  exit 2
fi

slug="$1"
src="${2:-$DEFAULT_SRC}"
repo="${HUMAN_REVIEW_REPO:-$DEFAULT_REPO}"

case "$slug" in
  *[!A-Za-z0-9_-]*|"")
    echo "publish-demo: slug must be letters, digits, dashes or underscores: '$slug'" >&2
    exit 2
    ;;
esac

if [ ! -d "$src" ]; then
  echo "publish-demo: source directory not found: $src" >&2
  exit 1
fi

if [ ! -f "$src/review.html" ]; then
  echo "publish-demo: $src does not look like a snapshot (no review.html)" >&2
  exit 1
fi

if [ ! -d "$repo/.git" ]; then
  echo "publish-demo: not a git checkout: $repo" >&2
  echo "             set HUMAN_REVIEW_REPO to your human-review clone" >&2
  exit 1
fi

dest="$repo/demo/$slug"

# A large asset silently blows past what GitHub Pages will serve; catch it before the push.
if find "$src" -type f -size +50M | grep -q .; then
  echo "publish-demo: refusing — these files exceed 50 MB:" >&2
  find "$src" -type f -size +50M >&2
  exit 1
fi

rm -rf "$dest"
mkdir -p "$dest"
# Trailing slash on the source copies its contents, not the directory itself.
cp -R "$src"/. "$dest"/

size=$(du -sh "$dest" | cut -f1)
echo "Copied $src -> $dest ($size)"
echo
echo "Next steps:"
echo "  cd $repo"
echo "  git add demo/$slug"
echo "  git commit -m 'demo: publish $slug snapshot'"
echo "  git push"
echo
echo "Note: vscode://file/... links inside the snapshot resolve only on the machine"
echo "      that generated it. Everything else is self-contained."
