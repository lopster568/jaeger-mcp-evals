#!/bin/bash
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

# Keep ssh port forwards to a remote fixture alive: the ports bench.py and fixture_cli.py call on FIXTURE_HOST
# (JAEGER_UI_PORT, OFREP_PORT, and PHOENIX_PORT with --phoenix). FIXTURE_HOST must be a loopback alias or localhost.
#   fixture/forward.sh [--phoenix] SSH_TARGET   restart `ssh -N -L ...` until Ctrl-C; each restart is logged to stderr
#   fixture/forward.sh [--phoenix] --check      probe each port once; exit 1 if any is down (gate a batch on it)
# SSH_TARGET is anything ssh accepts (user@host, a Host alias) and may differ from FIXTURE_HOST.
# shellcheck source=lib.sh  # sourced by a computed path
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
die() { echo "forward.sh: $1" >&2; exit 2; }
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) forward.sh: $1" >&2; }

# The ports to forward, one per line.
fwd_ports() { echo "$JAEGER_UI_PORT"; echo "$OFREP_PORT"; [ -n "$PHOENIX" ] && echo "$PHOENIX_PORT"; }
# The -L arguments, bound on FIXTURE_HOST; ssh takes an IPv6 bind address only in brackets.
fwd_args() {
  local p h=$FIXTURE_HOST; [[ $h == *:* ]] && h="[$h]"
  for p in $(fwd_ports); do printf -- '-L\n%s:%s:127.0.0.1:%s\n' "$h" "$p" "$p"; done
}
loopback() { [[ $1 =~ ^(localhost|127\.[0-9.]+|::1)$ ]]; }

[ "${BASH_SOURCE[0]}" = "$0" ] || return 0  # sourced by the unit test: functions only
PHOENIX='' CHECK='' TARGET=''
for a in "$@"; do
  case $a in --phoenix) PHOENIX=1;; --check) CHECK=1;; -*) die "unknown option $a";; *) TARGET=$a;; esac
done
loopback "$FIXTURE_HOST" || die "FIXTURE_HOST is $FIXTURE_HOST; a forward needs a loopback alias (127.0.0.2) or localhost"

if [ -n "$CHECK" ]; then
  rc=0
  for p in $(fwd_ports); do
    if (exec 3<>"/dev/tcp/$FIXTURE_HOST/$p") 2>/dev/null; then echo "ok    $FIXTURE_HOST:$p"; else echo "DOWN  $FIXTURE_HOST:$p"; rc=1; fi
  done
  exit $rc
fi

[ -n "$TARGET" ] || die "usage: forward.sh [--phoenix] SSH_TARGET | --check"
L=(); while IFS= read -r x; do L+=("$x"); done < <(fwd_args)  # no mapfile: macOS bash 3.2; no word split: [::1] would glob
trap 'kill "$pid" 2>/dev/null; log "stopped"; exit 0' INT TERM
while :; do
  log "starting ssh -N to $TARGET: $(fwd_ports | tr '\n' ' ')"
  # shellcheck disable=SC2086  # SSH_OPTS is a list of options, split on purpose
  ssh $SSH_OPTS -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=8 -N "${L[@]}" "$TARGET" &
  pid=$!
  wait "$pid"
  log "ssh exited ($?), restarting in ${FORWARD_SLEEP:-3}s"
  sleep "${FORWARD_SLEEP:-3}" & pid=$!; wait "$pid"
done
