#!/usr/bin/env bash
# usage: build-variant.sh <experiment-name> [--remote]
#
# Builds a Jaeger binary and Docker image with the MCP tool descriptions from
# harness/experiments/descriptions/desc-change.json baked in, for an A/B
# experiment. Never touches the running fixture: the fixture host only ever
# gets new files under ~/variant-build/<name>/ and plain `docker build`s; no
# `docker compose`, no container recreate or restart.
#
# --remote defaults off, so a human decides when to build in the fixture's
# docker (this machine when FIXTURE_SSH_USER is empty, else the ssh host).
# Without --remote this does the local steps and prints the exact fixture
# commands instead of running them.
#
# Steps:
#   a) on $JAEGER_SRC's evals/desc-change branch, apply the descriptions
#      (fixture/apply-descriptions.py, which refuses a DRAFT file) and commit
#   b-e) fixture/build-image.sh builds the image from that commit and records it
#        (see its header for the steps and the exit codes)
#
# Host, user and paths come from fixture.env via fixture/lib.sh.
# Exit codes: 0 image built and recorded (--remote only); 1 refused (bad args,
# dirty jaeger repo, missing branch, DRAFT or unappliable descriptions) or a git
# step failed; 2 local build failed; 3 stopped after
# the local build (no --remote, or fixture unreachable); 4 fixture copy or build failed.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib.sh"

JAEGER_REPO="$(cd "$ROOT" && cd "$JAEGER_SRC" 2>/dev/null && pwd)" || { echo "build-variant: no such directory: JAEGER_SRC=$JAEGER_SRC" >&2; exit 1; }
BRANCH="evals/desc-change"
DESC_JSON="$ROOT/harness/experiments/descriptions/desc-change.json"
SERVER_GO_REL="cmd/jaeger/internal/extension/jaegerquery/internal/mcptools/server.go"

die() { echo "build-variant: $*" >&2; exit 1; }

REMOTE_ARG=()
NAME=""
for a in "$@"; do
  case "$a" in
    --remote) REMOTE_ARG=(--remote) ;;
    -*) die "unknown flag: $a (usage: $0 <experiment-name> [--remote])" ;;
    *) [ -z "$NAME" ] || die "unexpected extra argument: $a"; NAME="$a" ;;
  esac
done
[ -n "$NAME" ] || die "usage: $0 <experiment-name> [--remote]"
case "$NAME" in
  *[!A-Za-z0-9_-]*) die "experiment name must be alnum/dash/underscore, got: '$NAME'" ;;
esac
[ -f "$DESC_JSON" ] || die "no such file: $DESC_JSON"
[ -d "$JAEGER_REPO/.git" ] || die "no such git repo: $JAEGER_REPO"

# ---- (a) apply the descriptions and commit ----------------------------------
cd "$JAEGER_REPO" || die "cannot cd to $JAEGER_REPO"
[ -z "$(git status --porcelain)" ] || die "$JAEGER_REPO has uncommitted changes; refusing to touch it"
git rev-parse --verify "$BRANCH" >/dev/null 2>&1 || die "branch $BRANCH does not exist in $JAEGER_REPO; create it from v2.20.0 first"

ORIGINAL_BRANCH=$(git branch --show-current)
restore_branch() {
  if [ -n "$ORIGINAL_BRANCH" ] && [ "$ORIGINAL_BRANCH" != "$BRANCH" ]; then
    git checkout "$ORIGINAL_BRANCH" >&2 2>&1 || echo "build-variant: WARNING - could not restore branch '$ORIGINAL_BRANCH'" >&2
  fi
}
git checkout "$BRANCH" >&2 || die "could not check out $BRANCH"
trap restore_branch EXIT

if ! python3 "$HERE/apply-descriptions.py" "$DESC_JSON" "$SERVER_GO_REL" >&2; then
  echo "build-variant: apply-descriptions.py failed or refused; reverting $SERVER_GO_REL" >&2
  git checkout -- "$SERVER_GO_REL" 2>/dev/null
  exit 1
fi
git add "$SERVER_GO_REL"
if git diff --cached --quiet; then
  echo "build-variant: the descriptions already match server.go; using the branch HEAD" >&2
else
  git commit -m "evals: tool descriptions for $NAME" >&2 || die "git commit failed"
fi

# ---- (b-e) build and record: fixture/build-image.sh, from the commit just made --------
bash "$HERE/build-image.sh" "$JAEGER_REPO" "$BRANCH" "$NAME" ${REMOTE_ARG[@]+"${REMOTE_ARG[@]}"}
exit $?
