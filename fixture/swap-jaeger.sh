#!/usr/bin/env bash
# usage: swap-jaeger.sh <image-tag>
#
# Points the fixture's jaeger container at <image-tag> (FIXTURE.md, "Switch the Jaeger image").
# Sets JAEGERTRACING_IMAGE in .env.override and JAEGER_CONFIG_FILE=jaeger-config-main.yml, or
# removes that line for a quay.io/jaegertracing/jaeger:2.* tag; recreates only the jaeger
# container (--no-deps) and prints the image it now runs. Touches nothing else on the fixture.
# The trace store starts empty: let the load generator refill it before a batch.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib.sh"

TAG="${1:-}"
[ $# -eq 1 ] && [ -n "$TAG" ] || { echo "usage: $0 <image-tag>" >&2; exit 1; }
case "$TAG" in *[!A-Za-z0-9./:_-]*) echo "swap-jaeger: bad image tag: $TAG" >&2; exit 1 ;; esac

case "$TAG" in
  quay.io/jaegertracing/jaeger:2.*) CFG="sed -i '/^JAEGER_CONFIG_FILE=/d' .env.override" ;;
  *) CFG="sed -i -e '/^JAEGER_CONFIG_FILE=/d' -e '\$a\\' .env.override && echo JAEGER_CONFIG_FILE=jaeger-config-main.yml >> .env.override" ;;
esac
fx "cd '$D' && sed -i 's#^JAEGERTRACING_IMAGE=.*#JAEGERTRACING_IMAGE=$TAG#' .env.override && $CFG && $COMPOSE up -d --no-deps --force-recreate jaeger >&2" || exit 4
fx "docker inspect -f '{{.Config.Image}}' jaeger"
