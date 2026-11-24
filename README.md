# jaeger-ai-evals

A harness for evaluating the MCP tools and skills Jaeger serves to AI agents, as [jaegertracing/jaeger#9135](https://github.com/jaegertracing/jaeger/issues/9135) asks. Each trial breaks one service in the OpenTelemetry Demo with a feature flag, lets an agent investigate with only Jaeger's MCP tools, and scores its JSON verdict against the known cause. The records show which tools the agent called, whether it named the cause, and what could change the result. There is no leaderboard.

## Quick Start

You need make, Python 3.11 or newer, git, curl, and Docker with Compose 2.24 or newer. The demo takes about 2.9 GB RAM and 6 busy cores. `make measure` also needs Go (the version in your Jaeger `go.mod`). `make preflight` checks all of this.

1. Copy the settings and set `OPENROUTER_API_KEY` in `fixture.env`:

   ```bash
   cp fixture.env.example fixture.env
   ```

2. Start the demo. This is free and waits until Jaeger answers at http://localhost:16686/jaeger/ui:

   ```bash
   make setup
   ```

3. Check that one agent trial works. `DRY=1` checks everything and spends nothing; without it the trial is paid:

   ```bash
   make smoke DRY=1
   make smoke
   ```

4. Measure your Jaeger change. `JAEGER` is your Jaeger clone with the change committed, and `SCENARIO` is a file name in `harness/scenarios/` without `.json` (an unknown name prints the list). It runs 10 trials on your commit and 10 on its base, and asks before it spends:

   ```bash
   make measure JAEGER=~/jaeger SCENARIO=recommendationCacheFailure DRY=1
   make measure JAEGER=~/jaeger SCENARIO=recommendationCacheFailure
   ```

   Pick the scenario from Scenarios below. On the Claude baselines below, only recommendationCacheFailure (0/10) can show an improvement at 10 trials; the others can only catch a regression.

   The last line says whether your commit is better, worse, or not significantly different. Below 10 trials per arm a result only ranks the two.

   With the default `CLIENT=goose` each answer is graded by Goose on the agent's own model, which is not a validated grader ([docs/TUNING.md](docs/TUNING.md)). Set `GRADER_CLIENT=claude` in `fixture.env` to grade with the validated one, the Claude Code CLI on `claude-fable-5-1`; it needs a Claude login.

## Scenarios

| Scenario | What breaks | Recorded PASS (Claude CLI) | At N=10 it can show |
|---|---|---|---|
| `recommendationCacheFailure` | a cache grows until the service OOM-restarts; the cause has no error span | 0/10 (all PARTIAL) | better (5/10 or more from 0/10, 8/10 from 2/10); worse is not possible |
| `adFailure` | ad fails about 1 in 10 requests at random, by design | 6/10 | worse only (0/10) |
| `cartFailure` | cart's EmptyCart cannot reach its store; checkout hides the error | 8/10 | worse only (2/10 or below) |
| `emailMemoryLeak` | email is OOM-killed and restarted; callers see dropped connections | 9/10 | worse only (3/10 or below) |
| `paymentFailure` | payment rejects every charge with "invalid token" | 10/10 | worse only (5/10 or below) |
| `paymentUnreachable` | checkout cannot resolve payment's address | 10/10 | worse only (5/10 or below) |

Baselines: Jaeger main image main-d22c65b9, Claude Code CLI, claude-sonnet-5-5, 10 trials, 2026-11-05/06. A measure needs no recorded baseline, because its own baseline arm is the comparison. The last column holds for these recorded Claude baselines only: on another client or model the scenario's baseline may differ. [docs/SCENARIOS.md](docs/SCENARIOS.md) describes each scenario and how to choose one.

## Commands

| Target | What it does | Cost |
|---|---|---|
| `make setup` | runs preflight, fixture, up and phoenix in order, and pulls the Goose image | free |
| `make preflight` | checks the tools, versions and ports | free |
| `make fixture` | clones the OpenTelemetry Demo and installs the overlay | free |
| `make up` | starts the demo and waits for it (10 minutes at most) | free |
| `make phoenix` | starts the optional Phoenix trajectory store | free |
| `make smoke` | one agent trial end to end | PAID; free with `DRY=1` |
| `make measure` | your Jaeger commit against its base, judged by Fisher's exact test | PAID; free with `DRY=1` |
| `make run` | your own experiment file | PAID; free with `DRY=1` |
| `make test` | offline unit tests | free |
| `make lint` | license headers and line endings; it adds a missing header in place, as Jaeger's does, and `make fmt` also fixes line endings; CI also runs shellcheck | free |
| `make test-ci` | the same unit tests under coverage, writing `.cover/cover.xml`; run `make install-test-tools` once first, in a virtual environment if your Python refuses pip installs | free |
| `make verify` | re-scores every trajectory under `RUNS_DIR` and checks `records/INDEX.md` | free |
| `make down` | stops the containers; Jaeger's traces are lost, Phoenix keeps its own | free |
| `make clean` | removes the containers and network, keeping volumes and images | free |

Everything above goes through make, and `make help` lists it. `python3 harness/bench.py` adds analysis subcommands for maintainers (`verify`, `band`, `export`, `pack`, `index`, `power`, `leak`, `readiness`, `oracle`, `soak`); see `bench.py --help`.

## Options

Variables go on the make line (`make smoke CLIENT=claude`). Lower case is refused. Every target takes `PYTHON=` for another interpreter. `DRY=1` and `YES=1` mean on; `DRY=0` is off.

| Variable | Targets | Default | Meaning |
|---|---|---|---|
| `CLIENT` | smoke, measure | `goose` | agent client: `goose`, `claude` (Claude Code) or `codex` (experimental) |
| `JAEGER`, `SCENARIO` | measure | required | your Jaeger checkout, the scenario name |
| `N` | measure | `10` | trials per arm |
| `BASE` | measure | merge-base with `upstream/main`, else `origin/main` | the commit to compare against |
| `DRY` | smoke, measure, run | off | `1` checks and prints every step and spends nothing |
| `YES` | measure | off | `1` skips the y/N question before the pair file is committed |
| `EXPERIMENT` | run | required | a file name under `harness/experiments/` or a path |
| `FIRST` | run | off | `1` runs one cell only |
| `GOOSE_MODEL`, `CLAUDE_MODEL`, `CODEX_MODEL` | smoke, measure | in `fixture.env.example` | the model for that client, by full id; set in `fixture.env` |

With `CLIENT=claude`, `make smoke` uses your claude.ai login and `make measure` bills `ANTHROPIC_API_KEY`, as every published result did. Other keys (provider, grader, ports, `RUNS_DIR`) are in `fixture.env.example`.

## Layout and documentation

`Makefile` and `fixture/` bring the demo up. `harness/` holds `bench.py`, `measure.sh`, the scenarios, prompts and experiment files. `records/` holds checked-in results, `runs/` the raw trajectories (git-ignored and not published, so a published batch cannot be re-scored from this repository). `docs/` holds the manual; `docs/results/` the written-up experiment results.

- [docs/USAGE.md](docs/USAGE.md): stop and restart, review a result, where results land, glossary, the experiment file, protocol
- [docs/SCENARIOS.md](docs/SCENARIOS.md): the scenarios, choosing one, adding one; [docs/RECORD.md](docs/RECORD.md): every recorded key
- [docs/TUNING.md](docs/TUNING.md): factors that change a result; [fixture/FIXTURE.md](fixture/FIXTURE.md): the demo overlay, remote host
- [records/INDEX.md](records/INDEX.md): every recorded batch; [CHANGELOG.md](CHANGELOG.md): scenario versions; [CONTRIBUTING.md](CONTRIBUTING.md); [LICENSE](LICENSE) (Apache 2.0)
