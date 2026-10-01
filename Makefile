# Shortcuts for fixture/FIXTURE.md, which stays the manual. Local mode only: the demo runs
# on this machine. With a remote fixture host, run these targets on that host.
# Settings come from fixture.env, then fixture.env.example; an environment variable wins.

SHELL := bash
.DEFAULT_GOAL := help

SMOKE := harness/experiments/smoke-paymentfailure.json
# Load settings, refuse remote mode, then cd into FIXTURE_DEMO_DIR (absolute or relative to ~).
DEMO := set -e; . fixture/lib.sh; \
	[ -z "$$FIXTURE_SSH_USER" ] || { echo "FIXTURE_SSH_USER is set: run make on the fixture host"; exit 1; }; cd ~
COMPOSE := docker compose --env-file .env --env-file .env.override \
	-f compose.yaml -f compose.full.yaml -f compose.observability.yaml -f overlay/compose.overlay.yaml

# Any Python 3.11+ interpreter, e.g. make setup PYTHON=python3.12
PYTHON ?= python3

.PHONY: help setup preflight fixture up phoenix smoke down clean test verify

help:
	@echo "make setup      fixture + up + phoenix: everything except the paid smoke trial"
	@echo "make preflight  check docker, compose v2, git, curl, python 3.11 and the fixture ports"
	@echo "make fixture    clone the OpenTelemetry Demo 3.0.0 into FIXTURE_DEMO_DIR and install the overlay"
	@echo "make up         bring the demo up and wait (about 10 min at most) until the smoke dry run passes pre-flight"
	@echo "make phoenix    start the optional Phoenix trajectory store and wait until it answers"
	@echo "make smoke      dry run, then ONE paid agent trial of $(SMOKE) (cap 2 USD)"
	@echo "make down       stop the demo containers, keeping their data"
	@echo "make clean      remove the demo containers and network (Jaeger traces are lost; volumes and images stay)"
	@echo "make test       offline unit tests"
	@echo "make verify     re-score every trajectory under RUNS_DIR and check records/INDEX.md"

# Everything free, in order. The paid trial stays a separate step.
setup: preflight fixture up phoenix
	@echo "setup done; next: make smoke (one paid agent trial) or bench.py run <experiment>.json"

preflight:
	@PYTHON=$(PYTHON) bash fixture/preflight.sh

# FIXTURE.md, Install. Never overwrites fixture.env or the pristine flag file.
fixture:
	[ -f fixture.env ] || cp fixture.env.example fixture.env
	$(DEMO); \
	[ -d "$$D" ] || git clone --branch 3.0.0 --depth 1 https://github.com/open-telemetry/opentelemetry-demo "$$D"; \
	cd "$$D"; \
	case "$$(git rev-parse HEAD)" in 1755859a*) ;; \
	  *) echo "$$PWD is at $$(git rev-parse HEAD), not tag 3.0.0 (1755859a); if it is a partial clone, remove $$PWD and re-run make fixture"; exit 1;; esac; \
	mkdir -p overlay; \
	for f in env.override:.env.override compose.overlay.yaml:overlay/compose.overlay.yaml \
	  jaeger-config.yml:overlay/jaeger-config.yml otelcol-config-extras.yml:overlay/otelcol-config-extras.yml; do \
	  src="$(CURDIR)/fixture/$${f%%:*}"; dst="$${f#*:}"; \
	  if [ -f "$$dst" ] && ! cmp -s "$$src" "$$dst"; then echo "replacing $$PWD/$$dst (old copy kept as $$dst.bak)"; cp "$$dst" "$$dst.bak"; fi; \
	  cp "$$src" "$$dst"; \
	done; \
	[ -f "$$FIXTURE_PRISTINE_FLAG_FILE" ] || cp "$$FIXTURE_FLAG_FILE" "$$FIXTURE_PRISTINE_FLAG_FILE"

# FIXTURE.md, Bring up. The wait reuses bench.py's own pre-flight (containers, jaeger image,
# flag at default, fresh payment traces, no flag in telemetry) through the smoke dry run.
up:
	$(DEMO); cd "$$D"; $(COMPOSE) up -d
	for i in $$(seq 20); do \
	  $(PYTHON) harness/bench.py run $(SMOKE) --dry-run >/dev/null 2>&1 && { echo "fixture ready"; exit 0; }; \
	  echo "not ready yet ($$i/20), retrying in 30s"; sleep 30; \
	done; $(PYTHON) harness/bench.py run $(SMOKE) --dry-run || { . fixture/lib.sh; \
	  echo "still not ready after 10 min. Inspect: cd $$(cd ~; cd $$D; pwd) && $(COMPOSE) ps   and   $(COMPOSE) logs <service>"; exit 1; }

# FIXTURE.md, Trajectory store. Phoenix takes one to two minutes to answer.
phoenix:
	$(DEMO); cd "$$D"; $(COMPOSE) --profile store up -d --no-deps phoenix
	. fixture/lib.sh; for i in $$(seq 60); do \
	  [ "$$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://localhost:$$PHOENIX_PORT/")" = 200 ] && { echo "Phoenix up on port $$PHOENIX_PORT"; exit 0; }; \
	  sleep 5; \
	done; echo "Phoenix did not answer 200 on port $$PHOENIX_PORT within 5 minutes"; exit 1

# Makes ONE paid agent call (Claude Code CLI, claude-sonnet-5-5, effort high, budget cap 2 USD).
smoke: preflight
	$(PYTHON) harness/bench.py run $(SMOKE) --dry-run
	@$(PYTHON) -c 'import json,sys; e=json.load(open(sys.argv[1])); r=e["run"]; print("about to spend: arm %s, client %s, model %s, n %d, cap %s USD; Ctrl-C to cancel" % (",".join(e["arms"]), r["client"], r["model"], r["n_per_arm"], r["max_budget_usd"]))' $(SMOKE)
	@sleep 5
	$(PYTHON) harness/bench.py run $(SMOKE)

# Stop without deleting: containers and volumes stay, `make up` starts them again.
down:
	$(DEMO); cd "$$D"; $(COMPOSE) --profile store stop

# Remove containers and the network. Volumes (phoenix-data), pulled images and the demo checkout stay.
clean:
	@echo "WARNING: removing the demo containers; Jaeger's in-memory traces are lost"
	$(DEMO); cd "$$D"; $(COMPOSE) --profile store down

test:
	$(PYTHON) -B -m unittest discover -s harness/tests

verify:
	$(PYTHON) harness/bench.py verify
