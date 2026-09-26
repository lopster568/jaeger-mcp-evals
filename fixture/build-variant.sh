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
#   b) build a linux/amd64 static binary with the placeholder UI (no Node:
#      without `make build-ui` the embedded UI falls back to the placeholder)
#   -- only with --remote --
#   c) copy the binary and Dockerfiles to ~/variant-build/<name>/ on the fixture
#   d) build the base image (same recipe as make's create-baseimg, tagged
#      locally instead of pushed to the CI-only localhost:5000 registry) and the
#      jaeger image, tagged jaeger-mcp-evals/jaeger:<name>-<short sha>
#   e) print the tag and local image ID; write harness/experiments/images/<name>.json
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
IMAGES_DIR="$ROOT/harness/experiments/images"

die() { echo "build-variant: $*" >&2; exit 1; }

REMOTE=0
NAME=""
for a in "$@"; do
  case "$a" in
    --remote) REMOTE=1 ;;
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
BRANCH_SHA=$(git rev-parse HEAD)
SHORT_SHA=$(git rev-parse --short "$BRANCH_SHA")

# ---- (b) linux/amd64 static binary, placeholder UI ---------------------------
BUILD_DIR="/tmp/variant-build-$NAME-$SHORT_SHA"
mkdir -p "$BUILD_DIR/base"
echo "== building linux/amd64 binary ==" >&2
if ! GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -o "$BUILD_DIR/jaeger-linux-amd64" ./cmd/jaeger/ > "$BUILD_DIR/go-build.log" 2>&1; then
  cat "$BUILD_DIR/go-build.log" >&2
  echo "build-variant: go build FAILED" >&2
  exit 2
fi
cp cmd/jaeger/Dockerfile cmd/jaeger/sampling-strategies.json "$BUILD_DIR/"
cp scripts/build/docker/base/Dockerfile "$BUILD_DIR/base/Dockerfile"

BASE_IMAGE_TAG="evals-baseimg-alpine:latest"
BASE_IMAGE_RECIPE="scripts/build/docker/base/Dockerfile (FROM alpine:3.24.1@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b) - same recipe make's create-baseimg target uses for its default BASE_IMAGE, built locally instead of pushed to the CI-only localhost:5000 registry"
IMAGE_TAG="jaeger-mcp-evals/jaeger:${NAME}-${SHORT_SHA}"
RD="variant-build/$NAME"
REMOTE_STEPS=(
  "$(fx_line "mkdir -p $RD/base")"
  "$(cp_line "$BUILD_DIR/jaeger-linux-amd64 $BUILD_DIR/Dockerfile $BUILD_DIR/sampling-strategies.json" "$RD/")"
  "$(cp_line "$BUILD_DIR/base/Dockerfile" "$RD/base/Dockerfile")"
  "$(fx_line "docker build -t $BASE_IMAGE_TAG $RD/base")"
  "$(fx_line "docker build --platform linux/amd64 --target release --build-arg base_image=$BASE_IMAGE_TAG --build-arg debug_image=$BASE_IMAGE_TAG --build-arg TARGETARCH=amd64 -t $IMAGE_TAG $RD")"
)

stop_after_local() {
  echo "build-variant: $1" >&2
  echo "build-variant: build context ready at $BUILD_DIR; $BRANCH is at $BRANCH_SHA." >&2
  echo "build-variant: resume with: bash fixture/build-variant.sh $NAME --remote   or by hand:" >&2
  printf '  %s\n' "${REMOTE_STEPS[@]}" >&2
  exit 3
}
[ "$REMOTE" -eq 1 ] || stop_after_local "stopping after the local build: --remote was not given, nothing sent to the fixture."
fx true >&2 || stop_after_local "stopping after the local build: fixture $TARGET unreachable."

# ---- (c, d) copy and build on the fixture host -------------------------------
for step in "${REMOTE_STEPS[@]}"; do
  echo "+ $step" >&2
  bash -c "$step" >&2 || { echo "build-variant: fixture step failed: $step" >&2; exit 4; }
done

# ---- (e) record it -------------------------------------------------------------
# Built on the fixture and never pushed, so the local image ID stands in for a digest.
IMAGE_ID=$(fx "docker inspect --format='{{.Id}}' $IMAGE_TAG") \
  || { echo "build-variant: could not inspect the built image" >&2; exit 4; }
echo "build-variant: IMAGE TAG: $IMAGE_TAG"
echo "build-variant: IMAGE ID (local docker image ID, never pushed): $IMAGE_ID"
mkdir -p "$IMAGES_DIR"
python3 -c "
import json, sys, datetime
keys = ['name', 'tag', 'digest', 'branch', 'branch_commit', 'base_image', 'base_image_recipe']
doc = dict(zip(keys, sys.argv[1:8]))
doc['digest_type'] = 'local docker image ID (sha256 of image config) - never pushed to a registry, so no registry digest exists'
doc['built_at_utc'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
doc['built_on'] = 'fixture host'
order = keys[:3] + ['digest_type'] + keys[3:] + ['built_at_utc', 'built_on']
with open(sys.argv[8], 'w') as f:
    json.dump({k: doc[k] for k in order}, f, indent=2)
    f.write(chr(10))
" "$NAME" "$IMAGE_TAG" "$IMAGE_ID" "$BRANCH" "$BRANCH_SHA" "$BASE_IMAGE_TAG" "$BASE_IMAGE_RECIPE" "$IMAGES_DIR/$NAME.json"
echo "build-variant: wrote $IMAGES_DIR/$NAME.json; the running fixture was not touched." >&2
