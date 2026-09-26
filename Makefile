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

.PHONY: help setup fixture up phoenix smoke test verify

help:
	@echo "make setup    fixture + up + phoenix: everything except the paid smoke trial"
	@echo "make fixture  clone the OpenTelemetry Demo 3.0.0 into FIXTURE_DEMO_DIR and install the overlay"
	@echo "make up       bring the demo up and wait (about 10 min at most) until the smoke dry run passes pre-flight"
	@echo "make phoenix  start the optional Phoenix trajectory store and wait until it answers"
	@echo "make smoke    dry run, then ONE paid agent trial of $(SMOKE)"
	@echo "make test     offline unit tests"
	@echo "make verify   re-score every trajectory under RUNS_DIR and check records/INDEX.md"

# Everything free, in order. The paid trial stays a separate step.
setup: fixture up phoenix
	@echo "setup done; next: make smoke (one paid agent trial) or bench.py run <experiment>.json"

# FIXTURE.md, Install. Never overwrites fixture.env or the pristine flag file.
fixture:
	[ -f fixture.env ] || cp fixture.env.example fixture.env
	$(DEMO); \
	[ -d "$$D" ] || git clone --branch 3.0.0 --depth 1 https://github.com/open-telemetry/opentelemetry-demo "$$D"; \
	cd "$$D"; \
	case "$$(git rev-parse HEAD)" in 1755859a*) ;; \
	  *) echo "$$PWD is at $$(git rev-parse HEAD), not tag 3.0.0 (1755859a)"; exit 1;; esac; \
	mkdir -p overlay; \
	cp "$(CURDIR)/fixture/env.override" .env.override; \
	cp "$(CURDIR)/fixture/compose.overlay.yaml" "$(CURDIR)/fixture/jaeger-config.yml" \
	   "$(CURDIR)/fixture/otelcol-config-extras.yml" overlay/; \
	[ -f "$$FIXTURE_PRISTINE_FLAG_FILE" ] || cp "$$FIXTURE_FLAG_FILE" "$$FIXTURE_PRISTINE_FLAG_FILE"

# FIXTURE.md, Bring up. The wait reuses bench.py's own pre-flight (containers, jaeger image,
# flag at default, fresh payment traces, no flag in telemetry) through the smoke dry run.
up:
	$(DEMO); cd "$$D"; $(COMPOSE) up -d
	for i in $$(seq 20); do \
	  python3 harness/bench.py run $(SMOKE) --dry-run >/dev/null 2>&1 && { echo "fixture ready"; exit 0; }; \
	  echo "not ready yet ($$i/20), retrying in 30s"; sleep 30; \
	done; python3 harness/bench.py run $(SMOKE) --dry-run

# FIXTURE.md, Trajectory store. Phoenix takes one to two minutes to answer.
phoenix:
	$(DEMO); cd "$$D"; $(COMPOSE) --profile store up -d --no-deps phoenix
	. fixture/lib.sh; for i in $$(seq 60); do \
	  [ "$$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://localhost:$$PHOENIX_PORT/")" = 200 ] && { echo "Phoenix up on port $$PHOENIX_PORT"; exit 0; }; \
	  sleep 5; \
	done; echo "Phoenix did not answer 200 on port $$PHOENIX_PORT within 5 minutes"; exit 1

# Makes ONE paid agent call (Claude Code CLI, sonnet, effort xhigh, budget cap 2 USD).
smoke:
	python3 harness/bench.py run $(SMOKE) --dry-run
	python3 harness/bench.py run $(SMOKE)

test:
	python3 -B -m unittest discover -s harness/tests

verify:
	python3 harness/bench.py verify
