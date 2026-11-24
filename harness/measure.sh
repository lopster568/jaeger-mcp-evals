#!/usr/bin/env bash
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

# usage: measure.sh <jaeger-checkout> <scenario> [--base <ref>] [--n <per-arm>] [--client goose|claude|codex] [--yes] [--dry-run]
#
# Measures a Jaeger change: builds the checkout's HEAD and its base, writes a pre-registered pair
# file, runs the scenario on each image in turn, and judges the pair (Fisher p < 0.05 on PASS).
# PAID: 2 x n agent trials plus a grader call each. --dry-run builds, swaps and runs nothing.
# The agent is CLIENT from fixture.env (--client overrides it): Goose (GOOSE_PROVIDER and GOOSE_MODEL), claude (the
# Claude Code CLI on CLAUDE_MODEL, billing ANTHROPIC_API_KEY; cli is read as claude) or codex (CODEX_MODEL, experimental).
# Exit codes: 0 judged PASS, 1 judged FAIL, 2 refused or a step failed.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../fixture/lib.sh  # sourced by a computed path
. "$HERE/../fixture/lib.sh"

die() { echo "measure: $*" >&2; exit 2; }
USAGE="usage: $0 <jaeger-checkout> <scenario> [--base <ref>] [--n <per-arm>] [--client goose|claude|codex] [--yes] [--dry-run]"
BASE_REF="" N=10 YES=0 DRY=0 SRC=CLIENT POS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE_REF="${2:-}"; shift ;;
    --n) N="${2:-}"; shift ;;
    --client) CLIENT="${2:-}"; SRC=--client; shift ;;
    --yes) YES=1 ;;
    --dry-run) DRY=1 ;;
    -*) die "unknown flag: $1 ($USAGE)" ;;
    *) POS+=("$1") ;;
  esac
  shift
done
[ "${#POS[@]}" -eq 2 ] || die "$USAGE"
REPO="$(cd "${POS[0]}" 2>/dev/null && pwd)" || die "no such directory: ${POS[0]}"
SCENARIO="${POS[1]}"
case "$N" in ''|*[!0-9]*|0) die "--n must be a positive integer" ;; esac
CLIENT=${CLIENT-goose}  # fixture.env's CLIENT, already exported by lib.sh; empty is refused below
CLIENT=$(norm_client "$CLIENT")
case "$CLIENT" in
  goose) case "${GOOSE_PROVIDER:-}" in anthropic|openrouter) ;; *) die "GOOSE_PROVIDER must be anthropic or openrouter; fix: set GOOSE_PROVIDER and GOOSE_MODEL in fixture.env, or pass --client claude" ;; esac
         [ -n "${GOOSE_MODEL:-}" ] || die "GOOSE_MODEL is empty; fix: set it in fixture.env to a full model id of $GOOSE_PROVIDER, or pass --client claude"
         KEYVAR=ANTHROPIC_API_KEY; [ "$GOOSE_PROVIDER" = openrouter ] && KEYVAR=OPENROUTER_API_KEY
         [ -n "${!KEYVAR:-}" ] || die "$KEYVAR is empty; fix: set it in fixture.env (the key of GOOSE_PROVIDER=$GOOSE_PROVIDER), or pass --client claude"
         MODEL=$GOOSE_MODEL ;;
  claude) MODEL=${CLAUDE_MODEL:-}; [ -n "$MODEL" ] || die "CLAUDE_MODEL is empty; fix: set it in fixture.env to a full model id"
          [ -n "${ANTHROPIC_API_KEY:-}" ] || die "ANTHROPIC_API_KEY is empty; measure on claude bills the API key, not your claude.ai login, as every published result did; fix: set ANTHROPIC_API_KEY in fixture.env" ;;
  codex) MODEL=${CODEX_MODEL:-}; [ -n "$MODEL" ] || die "CODEX_MODEL is empty; fix: set it in fixture.env to a full model id" ;;
  *) die "$SRC is '$CLIENT', not goose, claude or codex" ;;
esac
if [ ! -f "$ROOT/harness/scenarios/$SCENARIO.json" ]; then
  names=""; for f in "$ROOT"/harness/scenarios/*.json; do f=${f##*/}; names="$names ${f%.json}"; done
  die "no such scenario: $SCENARIO; scenarios:$names"
fi
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || die "not a git repo: $REPO"
[ -z "$(git -C "$REPO" status --porcelain)" ] || die "$REPO has uncommitted changes; commit them first (the build is from HEAD)"

HEAD_SHA=$(git -C "$REPO" rev-parse HEAD)
if [ -z "$BASE_REF" ]; then
  BASE_REF=$(git -C "$REPO" merge-base HEAD upstream/main 2>/dev/null) \
    || BASE_REF=$(git -C "$REPO" merge-base HEAD origin/main 2>/dev/null) \
    || die "no upstream/main or origin/main to take the base from; pass --base <ref>"
fi
BASE_SHA=$(git -C "$REPO" rev-parse --verify "$BASE_REF^{commit}" 2>/dev/null) || die "no such ref: $BASE_REF"
[ "$BASE_SHA" != "$HEAD_SHA" ] || die "base and HEAD are the same commit; nothing to measure"
HS=${HEAD_SHA:0:8} BS=${BASE_SHA:0:8}
EXP="measure-$SCENARIO-$HS"
EXP_FILE="$ROOT/harness/experiments/$EXP.json"
CHANGE_NAME="measure-change" BASE_NAME="measure-base"  # one record per arm; the tag carries the sha
CHANGE_TAG="jaeger-mcp-evals/jaeger:$CHANGE_NAME-$HS" BASE_TAG="jaeger-mcp-evals/jaeger:$BASE_NAME-$BS"

# Wait for load-generator traffic to refill Jaeger's in-memory store after a container swap.
SETTLE=300
run() { echo "+ $*"; [ "$DRY" -eq 1 ] || "$@"; }

GO_LINE="go: not needed, both images exist"
echo "measure: scenario $SCENARIO, base $BS ($BASE_REF), head $HS, n=$N per arm"
for t in "$BASE_NAME $BS $BASE_TAG" "$CHANGE_NAME $HS $CHANGE_TAG"; do
  # shellcheck disable=SC2086  # $t is three space-free words, split on purpose
  set -- $t
  if grep -qF "\"tag\": \"$3\"" "$ROOT/harness/experiments/images/$1.json" 2>/dev/null && { [ "$DRY" -eq 1 ] || fx "docker image inspect $3" >/dev/null 2>&1; }; then
    echo "measure: image record $1 exists, reusing $3"
  else
    if command -v go >/dev/null 2>&1; then GO_LINE="go: $(go version | cut -d' ' -f3) found"
    elif [ "$DRY" -eq 1 ]; then GO_LINE="WARN go is not on PATH; a real run stops here (install the Go version in Jaeger's go.mod)"
    else die "Go (the version in Jaeger's go.mod) is needed to build $1, and go is not on PATH; install it first"; fi
    run bash "$HERE/../fixture/build-image.sh" "$REPO" "$2" "$1" --remote || die "build-image failed for $1"
  fi
done

# Pair file, written before any run (pre-registration). An existing one must match.
"${PYTHON:-python3}" - "$EXP_FILE" "$EXP" "$SCENARIO" "$N" "$BASE_TAG" "$CHANGE_TAG" "$BS" "$HS" "$DRY" "$CLIENT" "${GOOSE_PROVIDER:-}" "$MODEL" <<'PY' || exit 2
import json, os, sys, datetime
path, name, scenario, n, base, change, bs, hs, dry, client, provider, model = sys.argv[1:]
# goose: no effort, so no GOOSE_THINKING_EFFORT, as the Jaeger demo's scripts/ai-sidecar/goose
run = {"client": client, "provider": provider if client == "goose" else None, "model": model,
       "effort": None if client == "goose" else "high", "max_turns": 30, "n_per_arm": int(n), "seed": None}
if client == "claude":
    run.update(max_budget_usd=2, bill_api_key=True)  # claude only; goose bills the provider's key itself and has no budget cap
doc = {
  "name": name, "version": 2, "scenario": scenario,
  "hypothesis": "On %s, Jaeger %s (arm variant) and its base %s (arm baseline) differ in how often %s reaches the cause. Decision rule: two-sided Fisher p < 0.05 on PASS at n=%s/arm." % (scenario, hs, bs, model if client == "claude" else model + (" on Goose" if client == "goose" else " on Codex"), n),
  "run": run,
  "arms": {"baseline": {"prompt": "neutral", "image": base, "tools": True},
           "variant": {"prompt": "neutral", "image": change, "tools": True}},
  "baseline_arm": "baseline", "thresholds": {"fisher_p_max": 0.05},
  "status": "generated by measure.sh " + datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
}
if os.path.exists(path):
    old = json.load(open(path))
    # run.client_version in an existing file is the exact-re-run pin; it is not a difference.
    def norm(d):
        d = {k: v for k, v in d.items() if k != "status"}
        d["run"] = {k: v for k, v in d["run"].items() if k != "client_version"}
        d["run"]["client"] = {"cli": "claude"}.get(d["run"]["client"], d["run"]["client"])  # an older pair file says cli
        return d
    if norm(old) != norm(doc):
        sys.exit("measure: %s exists and differs from this request; delete it or change the arguments" % path)
    print("measure: pair file %s exists and matches, keeping it (a run.client_version in it is kept as the exact re-run pin)" % path)
elif dry == "1":
    print("+ would write %s:\n%s" % (path, json.dumps(doc, indent=2)))
else:
    json.dump(doc, open(path, "w"), indent=2); open(path, "a").write("\n")
    print("measure: wrote %s" % path)
PY

# The plan: everything a contributor decides on before spending, in a dry run and before the y/N.
BILL=""; [ "$CLIENT" = claude ] && BILL=", bills ANTHROPIC_API_KEY"
echo "measure: client $CLIENT, model $MODEL, $N trials per arm, 2 arms, PAID$BILL"
if [ "$CLIENT" = claude ]; then echo "measure: cost: 0.04 to 0.24 USD agent per trial (measured), $(awk -v n=$((2 * N)) 'BEGIN{printf "%.1f to %.1f", 0.04 * n, 0.24 * n}') USD for $((2 * N)) trials, plus one grader call each"
elif [ "$CLIENT" = codex ]; then echo "measure: cost: Codex's cost per trial is not measured; it counts against your Codex login, plus one grader call per trial"
else echo "measure: cost: Goose's cost per trial is not measured yet; each trial records it (cost_usd), plus one grader call per trial"; fi
# 12 to 83 s: recorded cell durations of the 251 cells under records/ (2026-11-23), not a promise
echo "measure: time: two image builds (skipped when the image exists), 2 x $((SETTLE / 60)) min settle, then $((2 * N)) trials (recorded trials: 12 to 83 s each)"
GRADER=$("${PYTHON:-python3}" -c 'import sys; sys.path.insert(0, sys.argv[1]); import config, grade; c, p, m = grade.settings(config.load(), sys.argv[2]); print(" ".join(x for x in (c, "" if c == "claude" else p, m) if x))' "$HERE" "$CLIENT") || die "cannot read the grader settings"
[ "$CLIENT" = codex ] && GRADER="$GRADER (Codex has no grader client)"
echo "measure: grader: $GRADER"
echo "measure: $GO_LINE"
if [ "$N" -lt 10 ]; then
  echo "measure: n=$N per arm is rank only (a pass rate needs 10 or more); smallest significant variant PASS count per baseline count:"
  "${PYTHON:-python3}" "$HERE/bench.py" power "$N" | sed '1d; s/^/  /'
fi
case "$MODEL" in *:free) echo "measure: WARN $MODEL is a free model; a free OpenRouter key covers a few trials a day, not $((2 * N)); use a key with credits or a paid model" ;; esac
if [ "$DRY" -eq 0 ] && [ "$YES" -eq 0 ]; then
  read -r -p "measure: continue? [y/N] " ans
  [ "$ans" = y ] || [ "$ans" = Y ] || die "not confirmed; nothing was committed; images are built and the pair file is at $EXP_FILE"
fi

# Commit the pair file before any trial (docs/USAGE.md, Protocol: the rule is in history first). The image
# records stay local (git-ignored): each batch's manifest records the image tag, id and Jaeger commit.
PRE=("harness/experiments/$EXP.json")
if [ "$DRY" -eq 1 ]; then echo "+ git commit ${PRE[*]}"
else
  git -C "$ROOT" add -- "${PRE[@]}" || die "git add failed for the pair file"
  if git -C "$ROOT" diff --cached --quiet -- "${PRE[@]}"; then echo "measure: pair file already committed"
  else
    git -C "$ROOT" commit -q -s -m "measure: pre-register $EXP" -- "${PRE[@]}" || die "git commit of the pair file failed"
    echo "measure: pre-registered in commit $(git -C "$ROOT" rev-parse --short HEAD)"
  fi
fi

cd "$ROOT" || exit 2
if [ "$DRY" -eq 0 ]; then
  if ! ORIG=$(fx "docker inspect -f '{{.Config.Image}}' jaeger") || [ -z "$ORIG" ]; then die "cannot read the fixture's running jaeger image"; fi
else ORIG="<current fixture image>"; fi
echo "measure: fixture runs $ORIG now; it is restored at the end"
[ "$DRY" -eq 1 ] || trap 'bash "$HERE/../fixture/swap-jaeger.sh" "$ORIG" >/dev/null' EXIT

arm_run() {  # arm tag: swap, wait, run; sets BATCH
  run bash "$HERE/../fixture/swap-jaeger.sh" "$2" || die "swap to $2 failed"
  if [ "$DRY" -eq 1 ]; then echo "+ sleep $SETTLE"; else sleep "$SETTLE"; fi
  # No colour, so the Batch line parses without stripping escapes (GNU sed only); FORCE_COLOR would win over NO_COLOR.
  local bench=(env FORCE_COLOR= NO_COLOR=1 "${PYTHON:-python3}" harness/bench.py run "$EXP_FILE" --no-export)
  if [ "$DRY" -eq 1 ]; then echo "+ ${bench[*]}"; BATCH="<$1 batch dir>"; return; fi
  local out; out=$(mktemp)
  "${bench[@]}" 2>&1 | tee "$out"  # bench.py's screen log, Batch line included, is on stderr
  [ "${PIPESTATUS[0]}" -eq 0 ] || die "bench.py run failed for the $1 arm; the pair is discarded (see docs/USAGE.md, Protocol)"
  BATCH=$(sed -n 's/^Batch  *\([^ ]*\).*/\1/p' "$out" | tail -1)
  rm -f "$out"
  BATCH=$([ -n "$BATCH" ] && cd "$ROOT" && cd "$BATCH" 2>/dev/null && pwd) || die "bench.py run left no batch directory for the $1 arm"
}
arm_run baseline "$BASE_TAG"; B_DIR=$BATCH
arm_run variant "$CHANGE_TAG"; V_DIR=$BATCH
run "${PYTHON:-python3}" harness/judge.py "$B_DIR" "$V_DIR"; RC=$?
# Both batches go to Phoenix once the pair is judged; a failure is a warning, never the pair's result.
for d in "$B_DIR" "$V_DIR"; do
  run "${PYTHON:-python3}" harness/bench.py export "$d" || echo "measure: WARN not exported to Phoenix; the trials are in $d; once fixed: ${PYTHON:-python3} harness/bench.py export $d" >&2
done
# The verdict's direction last: the pair file's rule is two-sided, so EXPERIMENT PASS alone does not say better or worse.
if [ "$DRY" -eq 0 ] && [ "$RC" -le 1 ]; then sed -n 's/^direction: /measure: /p' "$ROOT/records/$EXP/RESULT.md"; fi
exit "$RC"
