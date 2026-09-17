#!/usr/bin/env bash
# Build and run the container image for a snapshot on this machine, without waiting for CI.
#
# The `demo image` workflow already publishes an image for every snapshot committed under
# demo/, but it can only ever show what has been published: a run that finished two minutes
# ago is not in any image until it has been staged with publish-demo.sh, committed and
# pushed. This script closes that gap — it points the *same* packaging at a local
# .human-review/ directory, so what you demo is the run you just did.
#
# It deliberately reuses .github/snapshot.Dockerfile and .github/snapshot.nginx.conf rather
# than writing its own. A local image that differs from the published one is worse than no
# local image: the whole point of serving the page is that it behaves exactly as it will
# for the reader, and two definitions drift the moment one of them is fixed.
set -euo pipefail

DEFAULT_SRC=".human-review"
DEFAULT_PORT=8642
DEFAULT_REPO="$HOME/workspace/human-review"

usage() {
  cat <<'USAGE'
serve-image.sh — run a /human-review snapshot in a container, from this machine

Usage:
  serve-image.sh [source-dir] [--port N] [--tag NAME] [--build-only]
  serve-image.sh --help

Arguments:
  source-dir    The snapshot directory to serve (default: .human-review), the one
                holding review.html, content.json and assets/.

Options:
  --port N      Host port to publish on (default: 8642). Nginx listens on 80 inside.
  --tag NAME    Image tag to build (default: human-review:local).
  --build-only  Build the image and stop; do not start a container.

Environment:
  HUMAN_REVIEW_REPO   Checkout of victorrentea/human-review holding the packaging files
                      (.github/snapshot.Dockerfile and .github/snapshot.nginx.conf).
                      Default: the checkout this script lives in, else ~/workspace/human-review
USAGE
}

src=""
port="$DEFAULT_PORT"
tag="human-review:local"
build_only=""

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help|help) usage; exit 0 ;;
    --port) port="${2-}"; shift 2 ;;
    --tag) tag="${2-}"; shift 2 ;;
    --build-only) build_only=1; shift ;;
    -*) echo "serve-image: unknown option: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [ -n "$src" ]; then
        echo "serve-image: more than one source directory given: '$src' and '$1'" >&2
        exit 2
      fi
      src="$1"; shift ;;
  esac
done
src="${src:-$DEFAULT_SRC}"

case "$port" in
  ""|*[!0-9]*) echo "serve-image: --port wants a number, got '$port'" >&2; exit 2 ;;
esac

if [ ! -d "$src" ]; then
  echo "serve-image: source directory not found: $src" >&2
  exit 1
fi

# Either kind of snapshot is servable — a full review is review.html, a gallery published by
# publish-demo-shots.sh is index.html — and the nginx config names both as directory
# indexes. Refusing a directory that is neither is worth it: the alternative is an image
# that builds, runs, and answers 403 on its own front page.
if [ ! -f "$src/review.html" ] && [ ! -f "$src/index.html" ]; then
  echo "serve-image: $src does not look like a snapshot (no review.html or index.html)" >&2
  exit 1
fi

# The packaging files live in the repository, not in the installed skill, so a plugin
# install has to be told where the checkout is rather than silently building something
# else. Look beside this script first — that covers developing in the checkout itself.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo=""
for candidate in "$here/../../.." "${HUMAN_REVIEW_REPO-}" "$DEFAULT_REPO"; do
  [ -n "$candidate" ] || continue
  [ -f "$candidate/.github/snapshot.Dockerfile" ] || continue
  repo="$(cd "$candidate" && pwd)"
  break
done
if [ -z "$repo" ]; then
  echo "serve-image: cannot find .github/snapshot.Dockerfile" >&2
  echo "             set HUMAN_REVIEW_REPO to your victorrentea/human-review checkout" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "serve-image: the Docker daemon is not answering — start Docker Desktop first" >&2
  exit 1
fi

slug="$(basename "$(cd "$src" && pwd)")"
slug="${slug#.}"
slug="$(printf '%s' "$slug" | tr -c 'A-Za-z0-9_-' '-')"
[ -n "$slug" ] || slug="snapshot"

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

# The stage reproduces the layout the shared Dockerfile expects — a snapshot under demo/
# and the nginx config under .github/ — so the build context differs from CI's only in
# holding one snapshot instead of all of them.
mkdir -p "$stage/demo/$slug" "$stage/.github"
cp "$repo/.github/snapshot.nginx.conf" "$stage/.github/"

# The same two exclusions publish-demo.sh makes, for the same reasons: everything a run
# keeps for itself wears a leading dot and is nobody else's business, and *.raw.webm is an
# un-narrated build intermediate nothing on the page plays. An image that carried them
# would not be the image a reader gets.
for entry in "$src"/*; do
  [ -e "$entry" ] || continue
  cp -R "$entry" "$stage/demo/$slug/"
done
find "$stage/demo/$slug" -name '*.raw.webm' -delete

echo "Building $tag from $src ..."
docker build -f "$repo/.github/snapshot.Dockerfile" \
  --build-arg "SNAPSHOT=demo/$slug" -t "$tag" "$stage"

if [ -n "$build_only" ]; then
  echo
  echo "Built $tag. Run it with:"
  echo "  docker run --rm -p $port:80 $tag"
  exit 0
fi

name="human-review-$slug"
docker rm -f "$name" >/dev/null 2>&1 || true

# Docker's own failure line is kept and its "Run 'docker run --help'" tail is not: a port
# that is already taken is by far the likeliest reason to land here, and burying the one
# useful sentence under a usage pointer is how a two-second fix turns into a detour.
if ! err="$(docker run -d --name "$name" -p "$port:80" "$tag" 2>&1 >/dev/null)"; then
  printf '%s\n' "$err" | grep -v "docker run --help" >&2 || true
  echo "serve-image: could not start the container — is port $port already taken?" >&2
  echo "             try: serve-image.sh $src --port $((port + 1))" >&2
  exit 1
fi

echo
echo "  http://localhost:$port"
echo
echo "Stop it with:  docker rm -f $name"
