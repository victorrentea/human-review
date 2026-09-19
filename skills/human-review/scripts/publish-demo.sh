#!/usr/bin/env bash
# Copy a finished /human-review snapshot into the human-review repo's demo/ folder, from
# where the `pages`, `demo-zip` and `demo image` workflows publish it.
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
  publish-demo.sh [slug] [source-dir] [--card] [--push]
  publish-demo.sh --help

Arguments:
  slug          Directory name under demo/ to publish as, e.g. demo.
                Letters, digits, dashes and underscores only. Defaults to the name of
                the project the snapshot belongs to — the source directory's parent.
  source-dir    The snapshot directory to copy (default: .human-review), the one
                holding review.html, content.json and assets/.

Options:
  --src DIR     The source directory, when you want the slug left to default.
  --card        Write this snapshot's card into demo/index.html, rendered from the
                snapshot itself. Implied by --push.
  --push        Commit the snapshot and the landing page, and push.

Environment:
  HUMAN_REVIEW_REPO   Checkout of victorrentea/human-review to copy into.
                      Default: ~/workspace/human-review

Without --push the script only copies files and prints the git commands to run once
you have looked at the result.
USAGE
}

slug=""
src=""
card=""
push=""

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help|help) usage; exit 0 ;;
    --src) src="${2-}"; shift 2 ;;
    --card) card=1; shift ;;
    --push) push=1; card=1; shift ;;
    -*) echo "publish-demo: unknown option: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [ -z "$slug" ]; then slug="$1"
      elif [ -z "$src" ]; then src="$1"
      else echo "publish-demo: unexpected argument: $1" >&2; exit 2
      fi
      shift ;;
  esac
done

src="${src:-$DEFAULT_SRC}"
repo="${HUMAN_REVIEW_REPO:-$DEFAULT_REPO}"

# The slug defaults to the project the snapshot belongs to — the source directory's parent
# — since that is the name a reader recognises, not "human-review". Same rule as
# publish-demo-shots.sh, so the two publishers land a change under the same name.
if [ -z "$slug" ] && [ -d "$src" ]; then
  slug="$(basename "$(dirname "$(cd "$src" && pwd)")")"
  slug="$(printf '%s' "$slug" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9-]/-/g; s/--*/-/g; s/^-//; s/-$//')"
fi

case "$slug" in
  "")
    echo "publish-demo: could not derive a slug; pass one as the first argument" >&2
    exit 2
    ;;
  *[!A-Za-z0-9_-]*)
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

# Re-rendering the card of a snapshot already published is a normal thing to want — the
# numbers on it come out of review.html and a hand-written card goes stale — and it must
# not begin by deleting the snapshot it is about to read.
if [ -d "$dest" ] && [ "$(cd "$src" && pwd)" = "$(cd "$dest" && pwd)" ]; then
  echo "Source is demo/$slug itself; leaving it alone and only refreshing what is asked."
else
  rm -rf "$dest"
  mkdir -p "$dest"
  # Only what a reader is meant to open. Everything a run keeps for itself wears a leading
  # dot — the per-step stamps, the ledger (written mode 600), the privacy verdicts, the
  # session id, the vendored .tools/ — and a demo directory is a public repository, so the
  # split the run already makes by naming is the one to publish along.
  for entry in "$src"/*; do
    cp -R "$entry" "$dest/"
  done

  # The un-narrated capture is a build intermediate: nothing on the page plays it, and it is
  # the single largest file in a snapshot. It would sit in git history forever.
  find "$dest" -name '*.raw.webm' -delete

  skipped=$(cd "$src" && ls -A | grep '^\.' | tr '\n' ' ' || true)
  size=$(du -sh "$dest" | cut -f1)
  echo "Copied $src -> $dest ($size)"
  if [ -n "$skipped" ]; then
    echo "Left behind (the run's own bookkeeping): $skipped"
  fi
fi

# ------------------------------------------------------------------ the landing page card
# Hand-editing demo/index.html once per snapshot is how that file rots — the card that
# stood there before this was written claimed ±33 files against a review.html that said
# ±38, because the page had been rebuilt and the card had not. So the card is rendered from
# the snapshot: its headline numbers are the scope chips review.html itself draws, and the
# title, subtitle and verdict are the ones content.json carries. Cards live between two
# markers and only the block for this slug is rewritten; everything else on the page,
# including the other snapshots' cards, is left exactly where it was.
if [ -n "$card" ]; then
  python3 - "$repo/demo/index.html" "$dest" "$slug" <<'PY'
import json, re, sys
from pathlib import Path

landing, snap, slug = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]

BEGIN, END = "<!-- live-snapshots:begin -->", "<!-- live-snapshots:end -->"


def inner(html, start):
    """Inner HTML of the tag opening at `start`, tracking nesting of that same tag name."""
    tag = re.match(r"<(\w+)", html[start:]).group(1)
    i = html.index(">", start) + 1
    depth, j = 1, i
    pat = re.compile(rf"</?{tag}\b", re.I)
    while depth:
        m = pat.search(html, j)
        if not m:
            return html[i:], len(html)
        depth += -1 if html[m.start():m.start() + 2] == "</" else 1
        j = html.index(">", m.start()) + 1
    return html[i:j - len(f"</{tag}>")], j


page = (snap / "review.html").read_text(encoding="utf-8")
content = json.loads((snap / "content.json").read_text(encoding="utf-8"))

stats = []
try:
    bar, _ = inner(page, page.index('<div class="scopebar">'))
except ValueError:
    bar = ""
for m in re.finditer(r'<(?:span|a) class="(chip[^"]*)"', bar):
    # branch and base are links back into the reviewed repository, not measurements; a card
    # that repeated them would spend two of its five slots saying what the subtitle says.
    if "refchip" in m.group(1):
        continue
    body, _ = inner(bar, m.start())
    # The payload sits inside a wrapper carrying only a tooltip, and the tooltip is a
    # sentence about the reviewed repo that has no business on a landing-page card.
    w = re.match(r"\s*<span[^>]*>", body)
    if w:
        body, _ = inner(body, w.start())
    body = re.sub(r'\s*data-tip="[^"]*"', "", body)
    body = re.sub(r"<a\b[^>]*>|</a>", "", body)
    # `sub` is the landing page's muted-paragraph class and means something else there;
    # dropping it leaves an attribute-less <span>, which is then worth unwrapping too.
    body = re.sub(r'\s*class="sub"', "", body)
    body = re.sub(r"<span>(.*?)</span>", r"\1", body)
    stats.append(body.strip())

verdict = (content.get("verdict") or {}).get("label")
if verdict:
    stats.append(f"verdict <b>{verdict}</b>")

title = content.get("title") or slug
subtitle = content.get("subtitle") or ""
# Same reason the chips lose theirs: the card is an <a>, and an <a> inside an <a> does
# not nest — the parser closes the card at the inner link and re-parents everything
# after it, leaving the stats outside the card they describe.
subtitle = re.sub(r"</?a\b[^>]*>", "", subtitle)
zipurl = f"https://github.com/victorrentea/human-review/releases/download/demo/human-review-{slug}.zip"
image = f"ghcr.io/victorrentea/human-review:{slug}"

block = [f"  <!-- snapshot:{slug} -->",
         f'  <a class="card" href="{slug}/review.html">',
         f"    <h3>{title}</h3>"]
if subtitle:
    block.append(f'    <p class="sub">{subtitle}</p>')
if stats:
    block.append('    <ul class="stats">')
    block += [f"      <li>{s}</li>" for s in stats]
    block.append("    </ul>")
block += [
    "  </a>",
    f'  <p class="download">Or <a href="{zipurl}">download it as a zip</a> and open',
    "    <code>review.html</code> off your own disk, or serve it exactly as it is here:",
    f"    <code>docker run --rm -p 8642:80 {image}</code>, then open",
    "    <a href=\"http://localhost:8642\">localhost:8642</a>.</p>",
    "  <!-- /snapshot -->",
]
block = "\n".join(block)

text = landing.read_text(encoding="utf-8")

if BEGIN not in text or END not in text:
    # First run: open the region where the cards already are — above the screenshot
    # galleries if those exist, else above the caveat that closes the page.
    for anchor in ("  <!-- shot-galleries:begin -->", "  <h2>One caveat</h2>", "  <h2>"):
        if anchor in text:
            text = text.replace(anchor, f"{BEGIN}\n{END}\n\n{anchor}", 1)
            break
    else:
        raise SystemExit("publish-demo: cannot find anywhere in demo/index.html to put the cards")

start, stop = text.index(BEGIN) + len(BEGIN), text.index(END)
region = text[start:stop]

marker = f"<!-- snapshot:{slug} -->"
if marker in region:
    head = region[:region.index(marker)].rstrip("\n ")
    rest = region[region.index(marker):]
    tail = rest[rest.index("<!-- /snapshot -->") + len("<!-- /snapshot -->"):]
    region = f"{head}\n{block}{tail}"
    verb = "refreshed"
else:
    region = f"{region.rstrip()}\n{block}\n"
    verb = "added"

landing.write_text(text[:start] + region + text[stop:], encoding="utf-8")
print(f"Card {verb} in demo/index.html ({len(stats)} stats).")
PY
fi

if [ -n "$push" ]; then
  git -C "$repo" add "demo/$slug" demo/index.html
  if git -C "$repo" diff --cached --quiet; then
    echo "publish-demo: nothing changed; no commit made"
  else
    git -C "$repo" commit --quiet -m "demo: publish $slug snapshot"
    git -C "$repo" push --quiet
    echo "publish-demo: committed and pushed"
  fi
else
  echo
  echo "Next steps:"
  echo "  cd $repo"
  echo "  git add demo/$slug demo/index.html"
  echo "  git commit -m 'demo: publish $slug snapshot'"
  echo "  git push"
fi

echo
echo "Note: vscode://file/... links inside the snapshot resolve only on the machine"
echo "      that generated it. Everything else is self-contained."
