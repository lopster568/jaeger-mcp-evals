# The fixture

OpenTelemetry Demo 3.0.0 (tag `3.0.0`, commit `1755859a`) with Jaeger 2.23.0 (the bootstrap pin, `jaeger_query.ai.mcp: {}`) in place of the demo's 2.19.0; every image `make measure` builds from a main-lineage checkout takes the same config. MCP endpoint: `http://localhost:16686/jaeger/ui/api/ai/mcp/` (streamable HTTP, 9 tools including `read_skill`).
Faults are the demo's own feature flags in `src/flagd/demo.flagd.json`, the only file a run edits.
By default the demo runs on this machine and every command below runs locally; for a separate host see Remote host.
The top-level Makefile runs Install (`make fixture`), Bring up (`make up`) and the trajectory store (`make phoenix`) for the local case; `make setup` runs all three.

## The four overlay files

| File here | Goes to (in the demo checkout) | What it changes |
|---|---|---|
| `env.override` | `.env.override` | Pins `DEMO_VERSION=3.0.0` (the demo's `.env` leaves it at `latest`) and `JAEGERTRACING_IMAGE=quay.io/jaegertracing/jaeger:2.23.0` |
| `compose.overlay.yaml` | `overlay/compose.overlay.yaml` | Fixed host ports (16686 Jaeger, 8013 and 8016 flagd); mounts `jaeger-config.yml` and the collector config below; higher memory limits for product-catalog, checkout, payment, astronomy-db, load-generator, ad, quote, fraud-detection, flagd; switches off six unused services (below) |
| `jaeger-config.yml` | `overlay/jaeger-config.yml` | The demo's `src/jaeger/config.yml` plus `jaeger_query.ai.mcp: {}` |
| `otelcol-config-extras.yml` | `overlay/otelcol-config-extras.yml` | Fixed `memory_limiter` limit (the percentage form can crash the collector at start on some Docker hosts), the de-flagging processors, and no exporters or extension for the six switched-off services |

`fixture_overlay_sha256` in every run record hashes these files as mounted (`sha256sum overlay/* .env.override`), so leave their bytes alone; old comments in them do not affect behaviour.
The memory limits in `compose.overlay.yaml` were tuned on a 24 GB host and may need raising or lowering on yours.
The ports it publishes must match `JAEGER_UI_PORT` and `OFREP_PORT` in fixture.env.

## What is switched off

The overlay disables six of the demo's services with the Compose profile `unused`, which is never enabled: opensearch, prometheus, grafana, opamp-server, flagd-ui and telemetry-docs. The benchmark reads traces from Jaeger only, and flags are flipped by editing the flag file and checked through flagd's OFREP, so none of the six is read or written.
That leaves 22 containers (23 with Phoenix); the demo itself runs 28 without the overlay. The overlay replaces the `depends_on` lists of frontend-proxy and otel-collector that named them, which needs Docker Compose 2.24 or newer, and the collector config drops their exporters and the opamp extension so it does not retry dead endpoints. frontend-proxy still routes /grafana, /opamp, /telemetry and /feature; those clusters resolve by DNS and have no backend.

Upgrading a stack that already runs: `make clean && make setup`. `up -d` alone keeps the six running and does not reload the collector config, which is a bind-mounted file.

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

`FIXTURE_PRISTINE_FLAG_FILE`, a constant in harness/config.py, names the pristine copy.

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

`make down` stops the containers (`docker compose stop`), and Jaeger's in-memory store is lost, so avoid it between batches; `make clean` removes the containers and network. Give the load generator a few minutes to fill Jaeger.

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
docker ps -q | wc -l                                    # at least MIN_CONTAINERS (19, a constant in harness/config.py)
curl -s http://localhost:16686/jaeger/ui/api/v3/services   # the demo's services, not an empty list
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

Confirm through OFREP (the restore check polls it up to 5 times, 2 s apart). `bench.py run` restores in a `finally` block and records the result in `restore.json`.

## De-flagging

`feature_flag.evaluation` span events, flagd's spans and flagd client spans inside the services all name the active flag and variant. The `filter/deflag` and `transform/deflag` processors in `otelcol-config-extras.yml` strip them in the collector; application spans, status, messages, exceptions and timing are untouched.

```
bash fixture/apply-deflag.sh
```

It validates the config in a throwaway collector, recreates only otel-collector, rolls back if it does not stay up, then runs `harness/fixture_leak.py --live`. `bench.py run` repeats that scan before and during the fault and refuses a fixture whose telemetry names a flag.

productCatalogFailure is the one deliberate exception: a demo flag with no scenario here, whose own error text names a feature flag. The rewrite statement is in the config, commented out.

## Switch the Jaeger image for a variant

`<variant-tag>` is the `tag` in `harness/experiments/images/<name>.json`; an experiment arm runs only while the fixture serves its image.

```
bash fixture/swap-jaeger.sh <variant-tag>
```

It sets `JAEGERTRACING_IMAGE` in `.env.override`, recreates only the jaeger container (`--no-deps`), over ssh in remote mode, and prints the image it now runs.

`make fixture` rewrites `.env.override`, so redo the swap after it.

Switch back with `quay.io/jaegertracing/jaeger:2.23.0`. `bench.py run` reads the running image via `docker inspect jaeger`, and the arms record different `fixture_overlay_sha256` by design.
Either direction empties the trace store: let the load generator refill it (or run `bench.py soak`) before a batch.

## Build a variant

```
bash fixture/build-image.sh <jaeger-checkout> <git-ref> <name>            # local steps only
bash fixture/build-image.sh <jaeger-checkout> <git-ref> <name> --remote   # then build in the fixture's Docker
```

Needs Go and a Jaeger checkout; the ref should be a main-lineage commit (a 2.20.0 build no longer boots on this fixture).
It builds a static linux/amd64 binary from the ref in a temporary worktree.
`--remote` builds `jaeger-mcp-evals/jaeger:<name>-<short sha>` in the fixture's Docker and writes `harness/experiments/images/<name>.json`, a local build record (the `measure-*` ones are git-ignored; the batch manifest records the image).
It never restarts a running container. Exit codes: 0 built and recorded, 1 refused or a git step failed, 2 local build failed, 3 stopped after the local build, 4 fixture step failed.
`make measure` runs this for you.

## Gotchas

- After a Docker restart the stack may stay down despite `restart: unless-stopped`; check the container count and run `make up` (wait a minute if the engine is still starting).
- After a host reboot run `make up`: it restarts the demo and, when its container exists, Phoenix.
- flagd can hang at its memory cap (the demo's 75M; the overlay raises it to 200M): OFREP accepts connections but never answers, and `docker stats` shows flagd at high CPU near its limit. Recreate only flagd: `... up -d --no-deps --force-recreate flagd`.
- Jaeger's Dockerfile needs `--build-arg debug_image=...` even for `--target release`; build-image.sh passes it.
- Jaeger's storage is in memory: any restart of Jaeger, Docker or the host empties it until the load generator refills it.
- Jaeger's HTTP API ignores `lookback=` unless `start` and `end` are also given; always pass explicit microsecond `start` and `end`.
- Never `pkill -f` over ssh: the pattern matches the remote shell's own command line and kills the session.

## Remote host

Two machines: the fixture host runs the demo, the agent machine runs the harness and the client. Set `FIXTURE_SSH_USER` and `FIXTURE_HOST` in fixture.env on the agent machine to reach the host over key-based ssh. The harness then runs the commands in this page over ssh, with
`FIXTURE_DEMO_DIR` relative to that user's home, copies files with scp, and `localhost`
in the health checks becomes `FIXTURE_HOST`.

### Remote layout

On the fixture host (clone this repo there too), set `FIXTURE_HOST_ONLY=1` and leave `FIXTURE_SSH_USER` empty in fixture.env, then run `make fixture`, `make up` and optionally `make phoenix`. `FIXTURE_HOST_ONLY=1` skips the agent client check in preflight and the Goose image pull, since no agent runs there; `make up` waits on the fixture alone (`MIN_CONTAINERS` running, Jaeger answering) everywhere.
On the machine that runs the agent client (Goose in Docker with a provider key, or the logged-in Claude Code CLI), set `FIXTURE_HOST` to a loopback alias such as `127.0.0.3`, `FIXTURE_SSH_USER` to the ssh user, and `FIXTURE_HOST_ONLY=0`. The harness runs ssh to `FIXTURE_SSH_USER@FIXTURE_HOST`, so give the alias a `Host 127.0.0.3` entry with the host's real `HostName` in `~/.ssh/config`. `bench.py run` and `make smoke` enforce the client checks there; `bench.py run ... --dry-run` is the readiness check for the whole chain.
Both machines need the same `JAEGER_UI_PORT`, `OFREP_PORT`, `PHOENIX_PORT` and `FIXTURE_BIND`.

The overlay publishes its four ports on `FIXTURE_BIND`, 127.0.0.1 when it is empty (the demo's own compose files at 1755859a publish jaeger's and flagd's container ports, and others, with no host port, so Docker maps each to a random host port on every interface; those are not narrowed, and the fixture is not loopback-only), so the agent machine reaches them through `fixture/forward.sh SSH_TARGET`, run in its own terminal for the whole batch (`SSH_TARGET` is what `ssh` takes, and may differ from `FIXTURE_HOST`). It forwards `JAEGER_UI_PORT` and `OFREP_PORT` (add `--phoenix` for `PHOENIX_PORT`, which `bench.py export` posts to), restarts `ssh -N` with `ExitOnForwardFailure`, `ServerAliveInterval=15` and `ServerAliveCountMax=8` whenever it exits, logs each restart with a UTC timestamp to stderr, and exits 0 on Ctrl-C. Gate a batch on `fixture/forward.sh --check`, which exits 1 if any port is down. A plain `ssh -fN` forward died mid-batch once and left 4 of 10 trials invalid. The harness does not call flagd's port 8013.

To skip the forward, set `FIXTURE_BIND=0.0.0.0` on both machines and `FIXTURE_HOST` on the agent machine to the fixture host's address (no `~/.ssh/config` alias needed). Jaeger's MCP endpoint, flagd and Phoenix have no authentication, so do this only behind a firewall that admits the agent machine alone. Goose under Docker Desktop needs this route for a remote fixture: its container reaches the agent machine's loopback only as `host.docker.internal`, which is not the alias `forward.sh` listens on.
