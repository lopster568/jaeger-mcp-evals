# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

# Shortcuts for fixture/FIXTURE.md, which stays the manual. Local mode only: the demo runs
# on this machine. With a remote fixture host, run these targets on that host.
# On a fixture host with no agent set FIXTURE_HOST_ONLY=1 in fixture.env: preflight skips its agent client check and setup skips the Goose image pull (FIXTURE.md, Remote host).
# Settings come from fixture.env, then fixture.env.example; an environment variable wins.
# The steps live in harness/fixture_cli.py; fixture/preflight.sh runs first, before any Python.

.DEFAULT_GOAL := help

# make variables are case-sensitive: client=cli would be ignored and the default client would run
$(foreach v,client jaeger scenario python,$(if $(filter command line,$(origin $v)),$(error $v=$($v) is ignored: make variables are upper case; use $(shell echo $v | tr a-z A-Z)=$($v))))

# Any Python 3.11+ interpreter, e.g. make setup PYTHON=python3.12 (fixture/preflight.sh checks the version)
PYTHON ?= python3
CLI := $(PYTHON) harness/fixture_cli.py
export PYTHON
PREFLIGHT := bash fixture/preflight.sh

.PHONY: help setup preflight fixture up phoenix smoke measure down clean test verify

help:
	@echo "Set up"
	@echo "  make setup      preflight, fixture, up (and the Goose image) and phoenix: everything free, in order"
	@echo "  make preflight  check docker, Docker Compose 2.24 or newer, git, curl, python 3.11 and the ports (claude is noted, needed only for the cli client, the Claude Code CLI)"
	@echo "  make fixture    clone the OpenTelemetry Demo 3.0.0 into FIXTURE_DEMO_DIR, install the overlay"
	@echo "  make up         pull missing images, start the demo, wait (10 min at most) until it answers"
	@echo "  make phoenix    start the optional Phoenix trajectory store"
	@echo "Run"
	@echo "  make smoke      PAID: dry run, then one agent trial on Goose (GOOSE_PROVIDER, GOOSE_MODEL); CLIENT=cli for the Claude Code CLI (about 50k tokens); CLIENT=codex for Codex (gpt-5.6-terra)"
	@echo "  $(PYTHON) harness/bench.py run harness/experiments/<name>.json [--dry-run]   PAID without --dry-run"
	@echo "  make measure JAEGER=<checkout> SCENARIO=<name> [CLIENT=cli]   PAID: A/B your Jaeger commit against its base, judge by Fisher p"
	@echo "Check"
	@echo "  make test       offline unit tests"
	@echo "  make verify     re-score every trajectory under RUNS_DIR and check records/INDEX.md"
	@echo "Stop"
	@echo "  make down       stop the demo containers, keeping their data"
	@echo "  make clean      remove the demo containers and network (Jaeger traces are lost)"

setup:
	@$(PREFLIGHT) && $(CLI) setup

preflight:
	@$(PREFLIGHT) && $(CLI) tools

fixture up phoenix down clean test:
	@$(CLI) $@

# Makes ONE paid agent call (Goose with GOOSE_PROVIDER and GOOSE_MODEL; CLIENT=cli: Claude Code CLI, claude-sonnet-5-5; CLIENT=codex: Codex, gpt-5.6-terra; effort high, budget cap in the experiment file).
smoke:
	@export FIXTURE_HOST_ONLY=0 $(if $(CLIENT),SMOKE_CLIENT=$(CLIENT)); $(PREFLIGHT) && $(CLI) smoke

# PAID: builds two images, swaps the fixture's jaeger twice and runs 2 x 10 trials; asks first (harness/measure.sh).
measure:
	@test -n "$(JAEGER)" -a -n "$(SCENARIO)" || { echo "usage: make measure JAEGER=<jaeger-checkout> SCENARIO=<name> [CLIENT=cli]"; exit 2; }
	@bash harness/measure.sh "$(JAEGER)" "$(SCENARIO)" $(if $(CLIENT),--client $(CLIENT))

verify:
	@$(PYTHON) harness/bench.py verify
