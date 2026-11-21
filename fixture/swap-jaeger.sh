#!/usr/bin/env bash
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

# usage: swap-jaeger.sh <image-tag>
#
# Points the fixture's jaeger container at <image-tag> (FIXTURE.md, "Switch the Jaeger image").
# Sets JAEGERTRACING_IMAGE in .env.override; recreates only the jaeger container (--no-deps)
# and prints the image it now runs. Touches nothing else on the fixture.
# The trace store starts empty: let the load generator refill it before a batch.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh  # sourced by a computed path
. "$HERE/lib.sh"

TAG="${1:-}"
if [ $# -ne 1 ] || [ -z "$TAG" ]; then echo "usage: $0 <image-tag>" >&2; exit 1; fi
case "$TAG" in *[!A-Za-z0-9./:_-]*) echo "swap-jaeger: bad image tag: $TAG" >&2; exit 1 ;; esac

fx "cd '$D' && sed -i.bak 's#^JAEGERTRACING_IMAGE=.*#JAEGERTRACING_IMAGE=$TAG#' .env.override && rm -f .env.override.bak && $COMPOSE up -d --no-deps --force-recreate jaeger >&2" || exit 4
fx "docker inspect -f '{{.Config.Image}}' jaeger"
