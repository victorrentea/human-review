#!/usr/bin/env bash
# Re-implement a demo PR from its ticket, with demo/slop/<demo>.Q&A.md handed to the agent
# as the human's answers: the planted decisions of README.md go in at the start, so the
# reviewers see them and the agent has to decline them by citing Q&A.md. Demo runs only:
# nothing under skills/ knows this folder exists (test_demo_slop_guard.py).
#
#   run.sh <demo> --checkout DIR --branch NAME [--replace] [--model M] [--dry-run]
#       1. DIR: cloned when missing, else it must be clean
#       2. NAME: cut fresh from origin/main. An existing NAME (local or on origin) is
#          refused, unless --replace: then its tip(s) are tagged slop-backup/NAME-<stamp>
#          and pushed before anything moves
#       3. commit Q&A.md alone, as the branch's first commit, and push NAME
#       4. claude -p: implement the ticket, with Q&A.md final           (spends money)
#       5. claude -p --resume: /record-review + the Q&A.md decline rule  (spends money)
#       6. run-review.py --no-serve: /human-review                       (spends money)
#       7. check.py: which planted rows reached the page
#
#   run.sh promote <demo> --checkout DIR --branch NAME --to TARGET [--dry-run]
#       Point TARGET (the PR's branch) at NAME's pushed tip: TARGET's old tip is tagged
#       slop-backup/TARGET-<stamp> and pushed first, then --force-with-lease onto it.
#
# --dry-run prints every command and runs only read-only ones (ls-remote, status).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
HR=$(cd "$HERE/../.." && pwd)
REMOTE=https://github.com/victorrentea/petclinic.git
STAMP=$(date +%Y%m%d-%H%M)
MODEL=opus
DRY= REPLACE= CO= BRANCH= TO= MODE=run

die() { echo "run.sh: $*" >&2; exit 2; }
x() { printf '+ %s\n' "$*"; [[ -n $DRY ]] || "$@"; }

[[ ${1:-} == promote ]] && { MODE=promote; shift; }
DEMO=${1:-}; shift || true
case $DEMO in
  demo2) ISSUE=52 PR_BRANCH=visits-paginated ;;   # PR #53
  demo)  ISSUE=37 PR_BRANCH=test-pr ;;            # PR #49
  *) sed -n '2,25p' "$0"; exit 2 ;;
esac
QA="$HERE/$DEMO.Q&A.md"
[[ -f $QA ]] || die "no $QA"

while (($#)); do
  case $1 in
    --checkout) CO=$2; shift 2 ;;
    --branch) BRANCH=$2; shift 2 ;;
    --to) TO=$2; shift 2 ;;
    --model) MODEL=$2; shift 2 ;;
    --replace) REPLACE=1; shift ;;
    --dry-run) DRY=1; shift ;;
    *) die "unknown argument $1" ;;
  esac
done
[[ -n $CO && -n $BRANCH ]] || die "--checkout and --branch are required"
CO=${CO/#\~/$HOME}
for b in "$BRANCH" "$TO"; do
  [[ $b == main || $b == master ]] && die "refusing to touch $b"
done

remote_tip() { git ls-remote --heads "$REMOTE" "$1" | awk '{print $1}'; }

if [[ $MODE == promote ]]; then
  [[ -n $TO ]] || die "promote needs --to TARGET"
  [[ $TO == "$PR_BRANCH" ]] || die "$DEMO's PR branch is $PR_BRANCH, not $TO"
  NEW=$(remote_tip "$BRANCH"); OLD=$(remote_tip "$TO")
  [[ -n $NEW ]] || die "$BRANCH is not on origin"
  [[ -n $OLD ]] || die "$TO is not on origin"
  echo "promote: $TO $OLD -> $NEW ($BRANCH)"
  x git -C "$CO" fetch origin "$TO" "$BRANCH"
  x git -C "$CO" tag "slop-backup/$TO-$STAMP" "$OLD"
  x git -C "$CO" push origin "refs/tags/slop-backup/$TO-$STAMP"
  x git -C "$CO" push --force-with-lease="$TO:$OLD" origin "$NEW:refs/heads/$TO"
  echo "done: the PR on $TO now shows $BRANCH; its old tip is tag slop-backup/$TO-$STAMP"
  exit 0
fi

command -v claude >/dev/null || die "claude is not on PATH"
command -v jq >/dev/null || die "jq is not on PATH"
LOG="${XDG_STATE_HOME:-$HOME/.local/state}/human-review-slop/$DEMO-$STAMP"
echo "demo $DEMO (#$ISSUE) -> $CO on $BRANCH, model $MODEL, logs $LOG${DRY:+ (dry run)}"

# 1. the checkout
if [[ ! -d $CO ]]; then
  x git clone "$REMOTE" "$CO"
else
  git -C "$CO" rev-parse --git-dir >/dev/null 2>&1 || die "$CO is not a git checkout"
  [[ $(git -C "$CO" remote get-url origin) == "$REMOTE" ]] || die "$CO: origin is not $REMOTE"
  [[ -z $(git -C "$CO" status --porcelain --untracked-files=no) ]] \
    || die "$CO has uncommitted changes: commit or move them first"
  x git -C "$CO" fetch origin --prune
fi

# 2. the branch: never moved without --replace, and tagged before it is
RTIP=$(remote_tip "$BRANCH")
LTIP=$([[ -d $CO ]] && git -C "$CO" rev-parse -q --verify "refs/heads/$BRANCH" || true)
if [[ -n $RTIP || -n $LTIP ]]; then
  [[ -n $REPLACE ]] || die "branch $BRANCH exists (local ${LTIP:-none}, origin ${RTIP:-none}): pick another name, or --replace (its tips get tagged first)"
  tags=()
  [[ -n $LTIP ]] && { x git -C "$CO" tag "slop-backup/$BRANCH-$STAMP" "$LTIP"; tags+=("refs/tags/slop-backup/$BRANCH-$STAMP"); }
  if [[ -n $RTIP && $RTIP != "$LTIP" ]]; then
    x git -C "$CO" tag "slop-backup/$BRANCH-$STAMP-origin" "$RTIP"
    tags+=("refs/tags/slop-backup/$BRANCH-$STAMP-origin")
  fi
  x git -C "$CO" push origin "${tags[@]}"
  x git -C "$CO" switch -C "$BRANCH" origin/main
else
  x git -C "$CO" switch -c "$BRANCH" --no-track origin/main
fi
x git -C "$CO" config core.hooksPath .githooks

# 3. the human's answers, first commit of the branch
x cp "$QA" "$CO/Q&A.md"
x git -C "$CO" add -- 'Q&A.md'
x git -C "$CO" commit --only -m "Q&A: the human's answers to #$ISSUE's open questions" -- 'Q&A.md'
if [[ -n $RTIP ]]; then
  x git -C "$CO" push --force-with-lease="$BRANCH:$RTIP" -u origin "HEAD:refs/heads/$BRANCH"
else
  x git -C "$CO" push -u origin "HEAD:refs/heads/$BRANCH"
fi
x mkdir -p "$LOG"

# 4. implement
IMPL="Implement victorrentea/petclinic#$ISSUE (gh issue view $ISSUE). The human's answers \
to your open questions are in Q&A.md at the repository root: they are final, build to them \
exactly and do not reopen them. This run is unattended: run every command in the \
foreground and never end your turn waiting for anything. Commit the implementation; do \
not push and do not open a pull request."
claude_p() { env -u CLAUDE_CODE_SESSION_ID claude -p --model "$MODEL" \
  --dangerously-skip-permissions --output-format json "$@"; }
echo "+ (cd $CO && claude -p '<implement #$ISSUE, Q&A.md final>') > $LOG/implement.json"
if [[ -n $DRY ]]; then SID=DRY-RUN; else
  (cd "$CO" && claude_p "$IMPL") > "$LOG/implement.json"
  SID=$(jq -r .session_id "$LOG/implement.json")
  [[ -n $SID && $SID != null ]] || die "the implementation run left no session id ($LOG/implement.json)"
  [[ $(git -C "$CO" rev-list --count "origin/main..HEAD") -ge 2 ]] \
    || die "the implementation run committed nothing ($LOG/implement.json)"
fi

# 5. record the review, in the same conversation
RR="$(cat "$HR/skills/record-review/prompt.md")

RR = $HR/skills/record-review/record-review.py

One more rule for this run: Q&A.md holds the human's decisions. A finding against one of \
them is declined: it goes under Ignored, with a why: that cites the line it rests on \
(Q&A.md:<line>). A Q&A.md decision no reviewer attacked goes under Assumptions with \
source: human, anchored on the code that carries it."
echo "+ (cd $CO && claude -p --resume $SID '<record-review prompt + Q&A.md decline rule>') > $LOG/record-review.json"
if [[ -z $DRY ]]; then
  (cd "$CO" && claude_p --resume "$SID" "$RR") > "$LOG/record-review.json"
  git -C "$CO" log -1 --format=%s --grep='\[auto-fix\]' | grep -q . \
    || die "no [auto-fix] commit: the review was not recorded ($LOG/record-review.json)"
fi

# 6. /human-review
echo "+ (cd $CO && run-review.py --no-serve)"
[[ -n $DRY ]] || (cd "$CO" && env -u CLAUDE_CODE_SESSION_ID \
  python3 "$HR/skills/human-review/scripts/run-review.py" --no-serve) 2>&1 | tee "$LOG/run-review.out"

# 7. what reached the page
x python3 "$HERE/check.py" "$DEMO" --checkout "$CO" || true
cat <<EOF

next, by hand:
  look at the page:   $CO/.human-review/review.html
  move the PR:        $0 promote $DEMO --checkout $CO --branch $BRANCH --to <the PR's branch>
  publish:            $HR/skills/human-review/scripts/publish-demo.sh --push $DEMO=$CO/.human-review
EOF
