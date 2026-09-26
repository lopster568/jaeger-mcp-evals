# The fixture

OpenTelemetry Demo 3.0.0 (tag `3.0.0`, commit `1755859a`) with Jaeger 2.20.0 in place of the demo's 2.19.0, since 2.20.0 is the first release serving MCP tools in-process (`jaeger_query.ai.enable_mcp`) with the built-in skills. MCP endpoint: `http://localhost:16686/jaeger/ui/api/ai/mcp/` (streamable HTTP, 9 tools including `read_skill`).
Faults are the demo's own feature flags in `src/flagd/demo.flagd.json`, the only file a run edits.
By default the demo runs on this machine and every command below runs locally; for a separate host see Remote host.
The top-level Makefile runs Install (`make fixture`), Bring up (`make up`) and the trajectory store (`make phoenix`) for the local case.

## The four overlay files

| File here | Goes to (in the demo checkout) | What it changes |
|---|---|---|
| `env.override` | `.env.override` | Pins `DEMO_VERSION=3.0.0` (the demo's `.env` leaves it at `latest`) and `JAEGERTRACING_IMAGE=quay.io/jaegertracing/jaeger:2.20.0` |
| `compose.overlay.yaml` | `overlay/compose.overlay.yaml` | Fixed host ports (16686 Jaeger, 8013 and 8016 flagd, 4000 flagd-ui); mounts the two configs below; higher memory limits for product-catalog, checkout, payment, grafana, prometheus, astronomy-db |
| `jaeger-config.yml` | `overlay/jaeger-config.yml` | The demo's `src/jaeger/config.yml` plus `jaeger_query.ai.enable_mcp: true` |
| `otelcol-config-extras.yml` | `overlay/otelcol-config-extras.yml` | Fixed `memory_limiter` limit (the percentage form can crash the collector at start on some Docker hosts), plus the de-flagging processors |

`fixture_overlay_sha256` in every run record hashes these files as mounted (`sha256sum overlay/* .env.override`), so leave their bytes alone; old comments in them do not affect behaviour.
The memory limits in `compose.overlay.yaml` were tuned on a 24 GB host and may need raising or lowering on yours.
The ports it publishes must match `JAEGER_UI_PORT` and `OFREP_PORT` in fixture.env.

## Install

Clone into `FIXTURE_DEMO_DIR` (default `~/otel-demo-3.0.0`) and add the overlay:

```
git clone --branch 3.0.0 --depth 1 https://github.com/open-telemetry/opentelemetry-demo ~/otel-demo-3.0.0
cd ~/otel-demo-3.0.0
git rev-parse HEAD      # starts with 1755859a
mkdir overlay
cp <this repo>/fixture/env.override .env.override
cp <this repo>/fixture/compose.overlay.yaml <this repo>/fixture/jaeger-config.yml \
   <this repo>/fixture/otelcol-config-extras.yml overlay/
cp src/flagd/demo.flagd.json overlay/demo.flagd.json.pristine
```

fixture.env names the pristine copy `FIXTURE_PRISTINE_FLAG_FILE`.

## Update the overlay

```
cp fixture/env.override ~/otel-demo-3.0.0/.env.override
cp fixture/compose.overlay.yaml fixture/jaeger-config.yml fixture/otelcol-config-extras.yml ~/otel-demo-3.0.0/overlay/
(cd ~/otel-demo-3.0.0 && sha256sum overlay/* .env.override)
```

Compare against `sha256sum` of the repo copies; the demo listing also includes `overlay/demo.flagd.json.pristine`. A comment-only change needs no restart.

## Bring up

```
cd ~/otel-demo-3.0.0
docker compose --env-file .env --env-file .env.override \
  -f compose.yaml -f compose.full.yaml -f compose.observability.yaml -f overlay/compose.overlay.yaml \
  up -d
```

No `--remove-orphans`: whether it removes a running Phoenix container (profile `store`, not active here) depends on the Compose version, and removing the store silently is worse than leaving an orphan.

The same flags with `down` stop it, but `down` empties Jaeger's store, so avoid it between batches. Give the load generator a few minutes to fill Jaeger.

## Trajectory store (optional)

Phoenix runs from the same overlay under the Compose profile `store`, so the bring-up above
never starts it. Start only Phoenix, leaving every demo container as it is:

```
docker compose --env-file .env --env-file .env.override \
  -f compose.yaml -f compose.full.yaml -f compose.observability.yaml -f overlay/compose.overlay.yaml \
  --profile store up -d --no-deps phoenix
```

Phoenix answers about one to two minutes after start (database migrations, then app startup).
UI and OTLP HTTP share host port 16006 (`PHOENIX_PORT` in fixture.env); its data lives in the
named volume `phoenix-data`. The UI is at http://localhost:16006 on the machine running the
demo. After a batch, `python3 harness/bench.py export <batch_dir>` sends
its trajectories there (docs/USAGE.md, Export to Phoenix).

## Health checks

```
docker ps -q | wc -l                                    # at least MIN_CONTAINERS (default 25)
curl -s http://localhost:16686/jaeger/ui/api/services   # the demo's services, not an empty list
curl -s -X POST -H 'Content-Type: application/json' -d '{"context":{}}' \
  http://localhost:8016/ofrep/v1/evaluate/flags/paymentFailure   # "variant":"off"
python3 harness/tools.py capture --out /tmp/tools.json          # prints the tool list sha256
```

A full container count can hide orders that never complete, so `bench.py run` also requires fresh baseline traces from the root-cause service before a flip.
`bench.py soak <scenario>` watches load, unhealthy containers and that service's trace rate over time.

## Flip a flag and confirm it

flagd reloads on write; `bench.py run` flips for you. By hand:

```
cd ~/otel-demo-3.0.0 && python3 - <<PY
import json
p = "src/flagd/demo.flagd.json"; d = json.load(open(p))
d["flags"]["paymentFailure"]["defaultVariant"] = "100%"
json.dump(d, open(p, "w"), indent=2); open(p, "a").write("\n")
PY
```

Repeat the OFREP call and expect the new variant; never trust an unconfirmed flip. `bench.py` supports `defaultVariant` activation only and refuses targeting-rule scenarios.

## Restore pristine

```
cd ~/otel-demo-3.0.0 && cp overlay/demo.flagd.json.pristine src/flagd/demo.flagd.json
```

Confirm through OFREP. `bench.py run` restores in a `finally` block and records the result in `restore.json`.

## De-flagging

`feature_flag.evaluation` span events, flagd's spans and flagd client spans inside the services all name the active flag and variant. The `filter/deflag` and `transform/deflag` processors in `otelcol-config-extras.yml` strip them in the collector; application spans, status, messages, exceptions and timing are untouched.

```
bash fixture/apply-deflag.sh
```

It validates the config in a throwaway collector, recreates only otel-collector, rolls back if it does not stay up, then runs `harness/fixture_leak.py --live`. `bench.py run` repeats that scan before and during the fault and refuses a fixture whose telemetry names a flag.

productCatalogFailure is the one deliberate exception, because its own error text names a feature flag and is the scenario's signal. The rewrite statement is in the config, commented out.

## Switch the Jaeger image for a variant

`<variant-tag>` is the `tag` in `harness/experiments/images/<name>.json`; an experiment arm runs only while the fixture serves its image.

```
cd ~/otel-demo-3.0.0
sed -i "s#^JAEGERTRACING_IMAGE=.*#JAEGERTRACING_IMAGE=<variant-tag>#" .env.override
docker compose --env-file .env --env-file .env.override \
  -f compose.yaml -f compose.full.yaml -f compose.observability.yaml -f overlay/compose.overlay.yaml \
  up -d --no-deps --force-recreate jaeger
```

Switch back with `quay.io/jaegertracing/jaeger:2.20.0`. `bench.py run` reads the running image via `docker inspect jaeger`, and the arms record different `fixture_overlay_sha256` by design.
Either direction empties the trace store: let the load generator refill it (or run `bench.py soak`) before a batch.

## Build a variant

```
bash fixture/build-variant.sh <experiment-name>            # local steps only
bash fixture/build-variant.sh <experiment-name> --remote   # then build in the fixture's Docker
```

Needs Go and a Jaeger checkout at `JAEGER_SRC` with branch `evals/desc-change` cut from `v2.20.0`.
It applies `harness/experiments/descriptions/desc-change.json` via `apply-descriptions.py` (refuses a `status` containing `DRAFT`), commits, and builds a static linux/amd64 binary.
`--remote` builds `jaeger-mcp-evals/jaeger:<name>-<short sha>` in the fixture's Docker and writes `harness/experiments/images/<name>.json`.
It never restarts a running container. Exit codes: 0 built and recorded, 1 refused or a git step failed, 2 local build failed, 3 stopped after the local build, 4 fixture step failed.

## Gotchas

- After a Docker restart the stack may stay down despite `restart: unless-stopped`; check the container count and rerun `up -d` (wait a minute if the engine is still starting).
- flagd can hang at its memory cap: OFREP accepts connections but never answers, and `docker stats` shows flagd at high CPU near its limit. Recreate only flagd: `... up -d --no-deps --force-recreate flagd`.
- Jaeger's Dockerfile needs `--build-arg debug_image=...` even for `--target release`; build-variant.sh passes it.
- Jaeger's storage is in memory: any restart of Jaeger, Docker or the host empties it until the load generator refills it.
- Jaeger's HTTP API ignores `lookback=` unless `start` and `end` are also given; always pass explicit microsecond `start` and `end`.
- Never `pkill -f` over ssh: the pattern matches the remote shell's own command line and kills the session.

## Remote host

Set `FIXTURE_SSH_USER` and `FIXTURE_HOST` in fixture.env to run the demo on another machine
with key-based ssh. Every command in this page then runs over ssh from the harness, with
`FIXTURE_DEMO_DIR` relative to that user's home, files are copied with scp, and `localhost`
in the health checks becomes `FIXTURE_HOST`.
