#!/bin/bash
# make preflight: check the tools and ports the local fixture needs, before anything is cloned or started.
# Ports come from the overlay's published ports (OVERLAY overrides the path, for tests).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OVERLAY="${OVERLAY:-$ROOT/fixture/compose.overlay.yaml}"
fail() { echo "preflight: $1"; exit 1; }

command -v docker >/dev/null || fail "docker is not installed; install Docker Engine or Docker Desktop"
docker compose version 2>/dev/null | grep -q 'v\?[2-9]\.' || fail "docker compose v2 is missing or docker does not run; install the Docker Compose plugin or start Docker"
command -v git >/dev/null || fail "git is not installed; install git"
command -v curl >/dev/null || fail "curl is not installed; install curl"
command -v python3 >/dev/null || fail "python3 is not installed; install Python 3.11 or newer"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || fail "python3 is $(python3 -V 2>&1), need 3.11 or newer"

# The stack is ours when a container named jaeger exists (running or stopped): its ports are expected
# to be taken, so re-running setup works. Otherwise a busy port belongs to something else.
if [ -z "$(docker ps -aq --filter name=^jaeger$ 2>/dev/null)" ]; then
  for p in $(sed -n 's/^ *- "\([0-9]*\):[0-9]*".*/\1/p' "$OVERLAY"); do
    (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && fail "port $p is in use by another program; stop it (see: ss -ltnp | grep :$p) and re-run"
  done
fi
echo "preflight ok"
