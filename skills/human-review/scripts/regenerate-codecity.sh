#!/usr/bin/env bash
# Rebuild a repo's Code City page, and optionally refresh the coverage baseline it
# compares against.
#
# The generators are NOT here and not in the analysed repo either — they are a tool of
# their own, reusable on any Java checkout: https://github.com/victorrentea/code-city .
# This keeps a clone of them out of the way and runs it, so the repo under review holds
# only the DATA the run produced (a committed codecity.html, a committed baseline) and
# none of the machinery that produced it.
#
#   regenerate-codecity.sh --out docs/generated/codecity --title "Code City"
#   regenerate-codecity.sh --out … --baseline docs/generated/codecity/coverage.tsv
#   regenerate-codecity.sh --out … --baseline … --write-baseline
#
#   --repo DIR            checkout to analyse (default: git toplevel of $PWD)
#   --out DIR             where codecity.html is written, repo-relative
#   --title TEXT          page heading; pinned by the caller, never derived from the
#                         folder name, so a contributor whose checkout is called
#                         something else still regenerates a byte-identical page
#   --baseline PATH       repo-relative coverage baseline to COMPARE against: the page
#                         reads it out of git at the diff's base ref and draws the
#                         before/after of coverage and CRAP
#   --write-baseline      ALSO overwrite that file with this run's numbers. Off by
#                         default and deliberately so: run on a PR branch it would
#                         replace the base's numbers with the branch's own, which is
#                         both a meaningless diff and the end of the comparison. This
#                         belongs to a merge into the default branch, nowhere else
#   --no-pull             keep the vendored tool as it is (offline, or pinned)
set -euo pipefail

REPO="" ; OUT="" ; TITLE="Code City" ; BASELINE="" ; WRITE_BASELINE="" ; NO_PULL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --repo)           REPO="$2"; shift 2 ;;
    --out)            OUT="$2"; shift 2 ;;
    --title)          TITLE="$2"; shift 2 ;;
    --baseline)       BASELINE="$2"; shift 2 ;;
    --write-baseline) WRITE_BASELINE=1; shift ;;
    --no-pull)        NO_PULL=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$OUT" ] || { echo "--out is required" >&2; exit 2; }

REPO="$(cd "${REPO:-$(git rev-parse --show-toplevel)}" && pwd)"
TOOL_URL="${CODECITY_TOOL_URL:-https://github.com/victorrentea/code-city.git}"
TOOL_DIR="${CODECITY_TOOL_DIR:-$REPO/.human-review/.tools/codecity}"

if [ ! -d "$TOOL_DIR/.git" ]; then
  echo "cloning the Code City generators into $TOOL_DIR ..."
  mkdir -p "$(dirname "$TOOL_DIR")"
  git clone --depth 1 "$TOOL_URL" "$TOOL_DIR"
elif [ -z "$NO_PULL" ]; then
  echo "updating the Code City generators in $TOOL_DIR ..."
  git -C "$TOOL_DIR" pull --ff-only --quiet
fi

ABS_OUT="$REPO/$OUT"
mkdir -p "$ABS_OUT"

# CRAP and coverage need a JaCoCo report, which needs the tests to have RUN. Running them
# is the caller's business (human-review.json's `city.tests`, or your own hands) — the
# generators only read whatever report is already on disk, and drop both metrics when
# there is none rather than colouring every building "not measured".
CODECITY_TITLE="$TITLE" \
CODECITY_COVERAGE_BASELINE="$BASELINE" \
  "$TOOL_DIR/generate.sh" "$REPO" "$ABS_OUT"

# The baseline is this run's coverage, kept so the NEXT branch off here can draw its
# before/after without re-running anything. It is lifted OUT of the output folder before
# the cleanup below and put in place after, because the natural place to commit it is
# inside that same folder — where the cleanup would otherwise eat the file this script
# had just written, which is exactly what it did the first time it ran.
STASHED=""
if [ -n "$WRITE_BASELINE" ]; then
  [ -n "$BASELINE" ] || { echo "--write-baseline needs --baseline" >&2; exit 2; }
  if [ -f "$ABS_OUT/crap-per-file.tsv" ]; then
    STASHED="$(mktemp)"
    cp "$ABS_OUT/crap-per-file.tsv" "$STASHED"
  else
    # Loud: the page renders either way, and every PR off this branch then silently has
    # no before side for coverage, with nothing on it saying why.
    echo "WARNING: no coverage data in this run (no jacoco.xml found) — the baseline at" >&2
    echo "         $BASELINE was left untouched. Run the tests first." >&2
  fi
fi

# The tool also renders a 2-D view and leaves its .tsv inputs behind; only the city is
# published, and codecity.html is self-contained.
find "$ABS_OUT" -maxdepth 1 -type f ! -name codecity.html -delete

if [ -n "$STASHED" ]; then
  mkdir -p "$(dirname "$REPO/$BASELINE")"
  mv "$STASHED" "$REPO/$BASELINE"
  chmod 644 "$REPO/$BASELINE"        # mktemp is 0600; this file is committed and read by all
  echo "coverage baseline -> $BASELINE"
fi

echo
echo "open $ABS_OUT/codecity.html"
