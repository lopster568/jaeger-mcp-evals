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
# Docker, so it needs docker unless the agent client (SMOKE_CLIENT) and GRADER_CLIENT are both cli, or it has no agent.
REMOTE=; [ -n "$FIXTURE_SSH_USER" ] && REMOTE=1
if [ -z "$REMOTE" ] || { [ "$FIXTURE_HOST_ONLY" != 1 ] && { [ "$SMOKE_CLIENT" != cli ] || [ "$GRADER_CLIENT" != cli ]; }; }; then
  command -v docker >/dev/null || fail "docker is not installed; install Docker Engine or Docker Desktop (the goose client and grader run in it)"
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
# claude is needed only for run.client cli and GRADER_CLIENT=cli (empty means goose): a failure, else one note.
if [ "$FIXTURE_HOST_ONLY" != 1 ]; then
  if command -v claude >/dev/null && env -u ANTHROPIC_API_KEY claude auth status >/dev/null 2>&1; then :
  elif [ "$GRADER_CLIENT" = cli ]; then
    command -v claude >/dev/null || fail "claude (the Claude Code CLI) is not installed; GRADER_CLIENT=cli needs it"
    fail "claude is not logged in; run claude, then /login (GRADER_CLIENT=cli uses this login)"
  else echo "preflight: note: claude (the Claude Code CLI) not found or not logged in: needed only for run.client cli or GRADER_CLIENT=cli"
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
    (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && fail "port $p is in use by another program; stop it (see: ss -ltnp | grep :$p) and re-run"
  done
fi
exit 0
