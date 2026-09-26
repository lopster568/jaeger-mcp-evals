#!/usr/bin/env bash
# Apply the de-flagging collector config to the fixture, safely.
# 1. copy the config  2. VALIDATE it in a throwaway collector with the same image, env and
# mounts  3. only if valid, recreate the otel-collector container  4. wait, then scan what
# the fixture now serves with harness/fixture_leak.py.
# A bad OTTL statement would crash-loop the collector and take tracing down, so step 2 gates
# step 3. Rollback: restore overlay/otelcol-config-extras.yml.bak on the fixture and recreate.
# Host, user and demo dir come from fixture.env (see fixture.env.example); with
# FIXTURE_SSH_USER empty every step runs on this machine, else over ssh.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/lib.sh"
COMPOSE='docker compose --env-file .env --env-file .env.override -f compose.yaml -f compose.full.yaml -f compose.observability.yaml -f overlay/compose.overlay.yaml'
# Must match the running collector's own arguments (docker inspect otel-collector), or
# validation fails for unrelated reasons: the demo's profiles pipeline needs this gate.
CFGS='--config=/etc/otelcol-config.yml --config=/etc/otelcol-config-full.yml --config=/etc/otelcol-config-observability.yml --config=/etc/otelcol-config-extras.yml --feature-gates=service.profilesSupport'

fx "cd '$D'" || { echo "apply-deflag: fixture unreachable or no demo dir: $D"; exit 2; }
fx "cd '$D' && cp -n overlay/otelcol-config-extras.yml overlay/otelcol-config-extras.yml.bak; true"
bash -c "$(cp_line "$HERE/otelcol-config-extras.yml" "$D/overlay/otelcol-config-extras.yml")" || exit 2

echo "== validating in a throwaway collector =="
out=$(fx "cd '$D' && $COMPOSE run --rm --no-deps -T otel-collector validate $CFGS" 2>&1); rc=$?
printf '%s\n' "$out" | tail -15
if [ "$rc" != "0" ]; then
  echo "apply-deflag: VALIDATION FAILED (rc=$rc). Restoring the previous config; the running collector was not touched."
  fx "cd '$D' && cp overlay/otelcol-config-extras.yml.bak overlay/otelcol-config-extras.yml"
  exit 1
fi
echo "apply-deflag: config valid"

echo "== recreating otel-collector only =="
fx "cd '$D' && $COMPOSE up -d --force-recreate --no-deps otel-collector 2>&1 | tail -2; sleep 20; docker inspect otel-collector --format 'collector: {{.State.Status}} restarts={{.RestartCount}}'"
st=$(fx "docker inspect otel-collector --format '{{.State.Status}} {{.RestartCount}}'")
if [ "$st" != "running 0" ]; then
  echo "apply-deflag: collector not healthy ($st). Rolling back."
  fx "cd '$D' && cp overlay/otelcol-config-extras.yml.bak overlay/otelcol-config-extras.yml && $COMPOSE up -d --force-recreate --no-deps otel-collector >/dev/null 2>&1"
  exit 1
fi

echo "== waiting 4 minutes for fresh traces, then scanning what the fixture serves =="
sleep 240
python3 "$HERE/../harness/fixture_leak.py" --live --lookback-s 200
