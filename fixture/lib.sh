# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
# shellcheck shell=bash

# Sourced by the fixture scripts. Loads fixture.env, then fixture.env.example (its #KEY=value lines too);
# a variable already in the environment wins, and within one file the last line for a key wins (as in harness/config.py).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for f in "$ROOT/fixture.env" "$ROOT/fixture.env.example"; do
  [ -f "$f" ] || continue
  mine=" "  # keys this file has set, so its later line can replace its earlier one but never a variable from the environment
  hash=""; [ "${f##*/}" = fixture.env.example ] && hash="#"  # "#KEY=value" there is a default, shown commented out
  while IFS='=' read -r k v; do
    k=${k#"$hash"}
    if [[ $k =~ ^[A-Z_][A-Z0-9_]*$ ]] && { [ -z "${!k+x}" ] || [[ $mine == *" $k "* ]]; }; then
      mine="$mine$k "
      v=${v%%[[:space:]]#*}; v=${v%"${v##*[![:space:]]}"}  # a " #" starts a comment, as in dotenv
      v=${v#[\"\']}; export "$k=${v%[\"\']}"
    fi
  done < "$f"
done
norm_client() { [ "$1" = cli ] && echo claude || echo "$1"; }  # cli, the old name, is claude
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new"
TARGET="$FIXTURE_SSH_USER@$FIXTURE_HOST"
# shellcheck disable=SC2034  # read by the scripts that source this file
D="${FIXTURE_DEMO_DIR#\~/}"  # absolute, or relative to the home directory

# Commands start in the home directory on the fixture: this machine when FIXTURE_SSH_USER is
# empty, else the ssh host. fx_line CMD prints the shell line that runs CMD there; fx runs it.
fx_line() {
  if [ -z "$FIXTURE_SSH_USER" ]; then printf 'cd ~ && %s' "$1"
  else
    local bind=""  # quoted, so a stray space or ; in fixture.env cannot run on the fixture host
    [ -n "$FIXTURE_BIND" ] && printf -v bind 'export FIXTURE_BIND=%q; ' "$FIXTURE_BIND"
    printf 'ssh %s %s %q' "$SSH_OPTS" "$TARGET" "$bind$1"
  fi
}
fx() { bash -c "$(fx_line "$1")"; }
# cp_line SRC... DEST prints the cp (or scp) line that copies local files to DEST on the fixture.
cp_line() {
  local dest="${!#}"
  if [ -z "$FIXTURE_SSH_USER" ]; then printf 'cd ~ && cp %s %s' "${*:1:$#-1}" "$dest"
  else printf 'scp -q %s %s %s:%s' "$SSH_OPTS" "${*:1:$#-1}" "$TARGET" "$dest"; fi
}

# The compose invocation the fixture runs under (FIXTURE.md); run it from the demo dir.
# shellcheck disable=SC2034  # read by the scripts that source this file
COMPOSE='docker compose --env-file .env --env-file .env.override -f compose.yaml -f compose.full.yaml -f compose.observability.yaml -f overlay/compose.overlay.yaml'
