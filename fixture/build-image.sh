#!/usr/bin/env bash
# usage: build-image.sh <jaeger-checkout> <git-ref> <name> [--remote]
#
# Builds a Jaeger binary and Docker image from <git-ref> of a Jaeger checkout. The ref is built in a
# temporary `git worktree add --detach`, removed afterwards: the checkout's branch and files are never
# touched. Never touches the running fixture: the fixture host only ever gets new files under
# ~/variant-build/<name>/ and plain `docker build`s; no `docker compose`, no container recreate.
#
# --remote defaults off, so a human decides when to build in the fixture's docker (this machine when
# FIXTURE_SSH_USER is empty, else the ssh host). Without --remote this stops after the local build
# and prints the exact fixture commands instead of running them.
#
# Steps:
#   b) build a linux/amd64 static binary with the placeholder UI (no Node:
#      without `make build-ui` the embedded UI falls back to the placeholder)
#   -- only with --remote --
#   c) copy the binary and Dockerfiles to ~/variant-build/<name>/ on the fixture
#   d) build the base image (same recipe as make's create-baseimg, tagged locally instead of pushed
#      to the CI-only localhost:5000 registry) and the jaeger image, tagged
#      jaeger-mcp-evals/jaeger:<name>-<short sha>
#   e) print the tag and local image ID; write harness/experiments/images/<name>.json
#
# Host, user and paths come from fixture.env via fixture/lib.sh.
# Exit codes: 0 image built and recorded (--remote only); 1 refused (bad args, no such checkout or
# ref) or a git step failed; 2 local build failed; 3 stopped after the local build (no --remote, or
# fixture unreachable); 4 fixture copy or build failed.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib.sh"
IMAGES_DIR="$ROOT/harness/experiments/images"
USAGE="usage: $0 <jaeger-checkout> <git-ref> <name> [--remote]"

die() { echo "build-image: $*" >&2; exit 1; }

REMOTE=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --remote) REMOTE=1 ;;
    -*) die "unknown flag: $a ($USAGE)" ;;
    *) ARGS+=("$a") ;;
  esac
done
[ "${#ARGS[@]}" -eq 3 ] || die "$USAGE"
REPO="$(cd "${ARGS[0]}" 2>/dev/null && pwd)" || die "no such directory: ${ARGS[0]}"
REF="${ARGS[1]}"
NAME="${ARGS[2]}"
case "$NAME" in
  *[!A-Za-z0-9_-]*) die "name must be alnum/dash/underscore, got: '$NAME'" ;;
esac
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || die "no such git repo: $REPO"
SHA=$(git -C "$REPO" rev-parse --verify "$REF^{commit}" 2>/dev/null) || die "no such ref in $REPO: $REF"
SHORT_SHA=$(git -C "$REPO" rev-parse --short=8 "$SHA")

# ---- (b) linux/amd64 static binary, placeholder UI, from a throwaway worktree ----
BUILD_DIR="/tmp/variant-build-$NAME-$SHORT_SHA"
mkdir -p "$BUILD_DIR/base"
WT="$BUILD_DIR/src"
git -C "$REPO" worktree add --detach "$WT" "$SHA" >&2 || die "git worktree add failed"
trap 'git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1' EXIT
echo "== building linux/amd64 binary ($REF = $SHA) ==" >&2
if ! (cd "$WT" && GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -o "$BUILD_DIR/jaeger-linux-amd64" ./cmd/jaeger/) > "$BUILD_DIR/go-build.log" 2>&1; then
  cat "$BUILD_DIR/go-build.log" >&2
  echo "build-image: go build FAILED" >&2
  exit 2
fi
cp "$WT/cmd/jaeger/Dockerfile" "$WT/cmd/jaeger/sampling-strategies.json" "$BUILD_DIR/"
cp "$WT/scripts/build/docker/base/Dockerfile" "$BUILD_DIR/base/Dockerfile"

BASE_IMAGE_TAG="evals-baseimg-alpine:latest"
BASE_FROM=$(grep -m1 '^FROM' "$BUILD_DIR/base/Dockerfile" | sed 's/ AS .*//')
BASE_IMAGE_RECIPE="scripts/build/docker/base/Dockerfile at $SHORT_SHA ($BASE_FROM) - same recipe make's create-baseimg target uses for its default BASE_IMAGE, built locally instead of pushed to the CI-only localhost:5000 registry"
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
  echo "build-image: $1" >&2
  echo "build-image: build context ready at $BUILD_DIR; $REF is at $SHA." >&2
  echo "build-image: resume with: bash fixture/build-image.sh $REPO $REF $NAME --remote   or by hand:" >&2
  printf '  %s\n' "${REMOTE_STEPS[@]}" >&2
  exit 3
}
[ "$REMOTE" -eq 1 ] || stop_after_local "stopping after the local build: --remote was not given, nothing sent to the fixture."
fx true >&2 || stop_after_local "stopping after the local build: fixture $TARGET unreachable."

# ---- (c, d) copy and build on the fixture host -------------------------------
for step in "${REMOTE_STEPS[@]}"; do
  echo "+ $step" >&2
  bash -c "$step" >&2 || { echo "build-image: fixture step failed: $step" >&2; exit 4; }
done

# ---- (e) record it -------------------------------------------------------------
# Built on the fixture and never pushed, so the local image ID stands in for a digest.
IMAGE_ID=$(fx "docker inspect --format='{{.Id}}' $IMAGE_TAG") \
  || { echo "build-image: could not inspect the built image" >&2; exit 4; }
echo "build-image: IMAGE TAG: $IMAGE_TAG"
echo "build-image: IMAGE ID (local docker image ID, never pushed): $IMAGE_ID"
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
" "$NAME" "$IMAGE_TAG" "$IMAGE_ID" "$REF" "$SHA" "$BASE_IMAGE_TAG" "$BASE_IMAGE_RECIPE" "$IMAGES_DIR/$NAME.json"
echo "build-image: wrote $IMAGES_DIR/$NAME.json; the running fixture was not touched." >&2
