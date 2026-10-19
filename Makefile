# Shortcuts for fixture/FIXTURE.md, which stays the manual. Local mode only: the demo runs
# on this machine. With a remote fixture host, run these targets on that host.
# Settings come from fixture.env, then fixture.env.example; an environment variable wins.
# The steps live in harness/fixture_cli.py; fixture/preflight.sh runs first, before any Python.

.DEFAULT_GOAL := help

# Any Python 3.11+ interpreter, e.g. make setup PYTHON=python3.12 (fixture/preflight.sh checks the version)
PYTHON ?= python3
CLI := $(PYTHON) harness/fixture_cli.py
PREFLIGHT := PYTHON=$(PYTHON) bash fixture/preflight.sh

.PHONY: help setup preflight fixture up phoenix smoke down clean test verify

help:
	@echo "Set up"
	@echo "  make setup      preflight, fixture, up and phoenix: everything free, in order"
	@echo "  make preflight  check docker, compose v2, git, curl, claude (logged in), python 3.11 and the ports"
	@echo "  make fixture    clone the OpenTelemetry Demo 3.0.0 into FIXTURE_DEMO_DIR, install the overlay"
	@echo "  make up         pull missing images, start the demo, wait (10 min at most) until pre-flight passes"
	@echo "  make phoenix    start the optional Phoenix trajectory store"
	@echo "Run"
	@echo "  make smoke      PAID: dry run, then one agent trial (about 50k tokens)"
	@echo "  $(PYTHON) harness/bench.py run harness/experiments/<name>.json [--dry-run]   PAID without --dry-run"
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

# Makes ONE paid agent call (Claude Code CLI, claude-sonnet-5-5, effort high, budget cap in the experiment file).
smoke:
	@$(PREFLIGHT) && $(CLI) smoke

verify:
	@$(PYTHON) harness/bench.py verify
