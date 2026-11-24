#!/bin/bash
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

# make preflight: check the tools and ports the local fixture needs, before anything is cloned or started.
# Ports come from the overlay's published ports (OVERLAY overrides the path, for tests).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OVERLAY="${OVERLAY:-$ROOT/fixture/compose.overlay.yaml}"
# shellcheck source=lib.sh  # sourced by a computed path
. "$ROOT/fixture/lib.sh"  # FIXTURE_HOST_ONLY=1 (fixture.env or environment): a fixture host with no agent, so no agent client check
fail() { echo "preflight: $1"; exit 1; }

# FIXTURE_SSH_USER set: the fixture runs on another host (FIXTURE.md, Remote layout), so Compose and the demo's
# ports are that host's, checked there by its own make up. This machine still runs Goose and the Goose grader in
# Docker, so it needs docker unless neither the agent client (CLIENT) nor the grader is goose, or it has no agent.
# The grader is GRADER_CLIENT, or when that is empty the agent's own client if it is claude, else goose; cli reads as claude.
C=$(norm_client "${CLIENT:-goose}")
GR=$(norm_client "${GRADER_CLIENT:-}")
[ -n "$GR" ] || { [ "$C" = claude ] && GR=claude || GR=goose; }
REMOTE=; [ -n "$FIXTURE_SSH_USER" ] && REMOTE=1
if [ -z "$REMOTE" ] || { [ "$FIXTURE_HOST_ONLY" != 1 ] && { [ "$C" = goose ] || [ "$GR" = goose ]; }; }; then
  command -v docker >/dev/null || fail "docker is not installed; install Docker Engine or Docker Desktop (the goose client and grader run in it)"
  info=$(docker info 2>&1 >/dev/null) || case $info in
    *"permission denied"*) fail "docker is installed but this user cannot use it (permission denied); run: sudo usermod -aG docker \$USER, then log out and in again" ;;
    *) fail "docker is installed but not running; start Docker (Docker Desktop or the docker service) and re-run" ;;
  esac
fi
if [ -z "$REMOTE" ]; then
  # the overlay's !override needs Compose 2.24 or newer
  cv=$(docker compose version --short 2>/dev/null || docker compose version 2>/dev/null)
  cv=$(printf '%s' "$cv" | grep -oE '[0-9]+\.[0-9]+' | head -1)
  [ -n "$cv" ] || fail "docker compose v2 is missing or docker does not run; install Docker Compose 2.24 or newer or start Docker"
  { [ "${cv%%.*}" -gt 2 ] || { [ "${cv%%.*}" -eq 2 ] && [ "${cv#*.}" -ge 24 ]; }; } || fail "docker compose $cv is too old; the overlay needs Docker Compose 2.24 or newer"
fi
command -v git >/dev/null || fail "git is not installed; install git"
command -v curl >/dev/null || fail "curl is not installed; install curl"
# The claude grader runs GRADER_BIN (default claude): a failure. The claude agent runs claude: a note.
logged_in() { command -v "$1" >/dev/null && env -u ANTHROPIC_API_KEY "$1" auth status >/dev/null 2>&1; }
if [ "$FIXTURE_HOST_ONLY" != 1 ]; then
  GB=${GRADER_BIN:-claude}
  if [ "$GR" = claude ] && ! logged_in "$GB"; then
    if ! command -v "$GB" >/dev/null; then
      [ "$GB" = claude ] && [ -x "$HOME/.local/bin/claude" ] && fail "claude is installed in ~/.local/bin but that is not on your PATH; open a new shell or add it to PATH (the claude grader needs it)"
      fail "$GB (the Claude Code CLI) is not installed; the claude grader needs it (GRADER_BIN)"
    fi
    fail "$GB is not logged in; run $GB, then /login (the claude grader uses this login)"
  fi
  if [ "$C" = claude ] && ! logged_in claude; then
    echo "preflight: note: claude (the Claude Code CLI) not found or not logged in: needed for CLIENT=claude"
  fi
fi
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || fail "$PY is not installed; install Python 3.11 or newer, or point make at one: make setup PYTHON=python3.12"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || fail "$PY is $("$PY" -V 2>&1), need 3.11 or newer; install it, or point make at one: make setup PYTHON=python3.12"

# The stack is ours when a container named jaeger exists (running or stopped): its ports are expected
# to be taken, so re-running setup works. Otherwise a busy port belongs to something else.
if [ -z "$REMOTE" ] && [ -z "$(docker ps -aq --filter name=^jaeger$ 2>/dev/null)" ]; then
  # shellcheck disable=SC2013  # the overlay's host ports, digits only, after any bind address
  for p in $(sed -nE 's/^ *- "([^"]*:)?([0-9]+):[0-9]+".*/\2/p' "$OVERLAY"); do
    (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && fail "port $p is in use by another program; stop it (see: ss -ltnp | grep :$p, or on macOS lsof -iTCP:$p -sTCP:LISTEN) and re-run"
  done
fi
exit 0
