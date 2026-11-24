# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

# Shortcuts for fixture/FIXTURE.md, which stays the manual. Local mode only: the demo runs
# on this machine. With a remote fixture host, run these targets on that host.
# On a fixture host with no agent set FIXTURE_HOST_ONLY=1 in fixture.env: preflight skips its agent client check and setup skips the Goose image pull (FIXTURE.md, Remote host).
# Settings come from fixture.env, then fixture.env.example; an environment variable wins.
# The steps live in harness/fixture_cli.py; fixture/preflight.sh runs first, before any Python.

.DEFAULT_GOAL := help

# make variables are case-sensitive: client=claude would be ignored and the default client would run
$(foreach v,client jaeger scenario python n base dry yes experiment first,$(if $(filter command line,$(origin $v)),$(error $v=$($v) is ignored: make variables are upper case; use $(shell echo $v | tr a-z A-Z)=$($v))))

# Any Python 3.11+ interpreter, e.g. make setup PYTHON=python3.12 (fixture/preflight.sh checks the version)
PYTHON ?= python3
CLI := $(PYTHON) harness/fixture_cli.py
export PYTHON
PREFLIGHT := bash fixture/preflight.sh
# Tracked files that carry the copyright and SPDX header (as Jaeger's Makefile)
LIST_LICENSE_SRC = git ls-files '*.py' '*.sh' '*.yaml' '*.yml' 'Makefile'
FMT_LOG=.fmt.log

.PHONY: help setup preflight fixture up phoenix smoke measure run down clean test test-ci install-test-tools verify lint lint-license lint-line-endings fmt

help:
	@echo "Set up"
	@echo "  make setup      preflight, fixture, up (and the Goose image) and phoenix: everything free, in order"
	@echo "  make preflight  check docker, Docker Compose 2.24 or newer, git, curl, python 3.11 and the ports (the claude grader, GRADER_BIN, must be logged in; a claude agent is noted)"
	@echo "  make fixture    clone the OpenTelemetry Demo 3.0.0 into FIXTURE_DEMO_DIR, install the overlay"
	@echo "  make up         pull missing images, start the demo, wait (10 min at most) until it answers"
	@echo "  make phoenix    start the optional Phoenix trajectory store"
	@echo "Run (PAID unless DRY=1, which checks everything and spends nothing)"
	@echo "  make smoke   [CLIENT=goose|claude|codex] [DRY=1]   one agent trial end to end (Goose: GOOSE_PROVIDER, GOOSE_MODEL; claude: CLAUDE_MODEL; codex: CODEX_MODEL, experimental)"
	@echo "  make measure JAEGER=<checkout> SCENARIO=<name> [CLIENT=] [N=10] [BASE=<ref>] [DRY=1] [YES=1]"
	@echo "                  A/B your Jaeger commit against its base, judged by Fisher p"
	@echo "  make run     EXPERIMENT=<name or path> [DRY=1] [FIRST=1]   run your own experiment file"
	@echo "Check"
	@echo "  make test       offline unit tests"
	@echo "  make test-ci    the same unit tests under coverage; writes .cover/cover.xml (needs make install-test-tools)"
	@echo "  make install-test-tools  pip install the pinned coverage package (CI and maintainers only; the harness itself needs no packages)"
	@echo "  make verify     re-score every trajectory under RUNS_DIR and check records/INDEX.md"
	@echo "  make lint       license headers and line endings; adds a missing header in place, like Jaeger (shellcheck runs in CI)"
	@echo "  make fmt        add missing license headers and fix line endings in place"
	@echo "Stop"
	@echo "  make down       stop the containers; Jaeger's traces are lost (in-memory store), Phoenix keeps its own"
	@echo "  make clean      remove the demo containers and network (volumes and images kept)"
	@echo "Models: CLAUDE_MODEL, CODEX_MODEL, GOOSE_MODEL in fixture.env. Analysis: $(PYTHON) harness/bench.py --help"

setup:
	@$(PREFLIGHT) && $(CLI) setup

preflight:
	@$(PREFLIGHT) && $(CLI) tools

fixture up phoenix down clean test:
	@$(CLI) $@

# Makes ONE paid agent call (Goose with GOOSE_PROVIDER and GOOSE_MODEL; CLIENT=claude: Claude Code, CLAUDE_MODEL; CLIENT=codex: Codex, CODEX_MODEL; effort high, budget cap in the experiment file). DRY=1 stops after the dry run.
# Options reach the recipes through $(if ...), not implicit export, so make 3.81 (macOS) behaves the same. $(filter 1,...): DRY=0 is not a dry run.
smoke:
	@export FIXTURE_HOST_ONLY=0 $(if $(CLIENT),CLIENT=$(CLIENT)); $(PREFLIGHT) && $(CLI) smoke$(if $(filter 1,$(DRY)), --dry-run)

# PAID: builds two images, swaps the fixture's jaeger twice and runs 2 x N trials (10 by default); asks first unless YES=1 (harness/measure.sh).
measure:
	@test -n "$(JAEGER)" -a -n "$(SCENARIO)" || { echo "usage: make measure JAEGER=<jaeger-checkout> SCENARIO=<name> [CLIENT=] [N=] [BASE=] [DRY=1] [YES=1]"; exit 2; }
	@$(if $(CLIENT),CLIENT=$(CLIENT) )bash harness/measure.sh "$(JAEGER)" "$(SCENARIO)" $(if $(N),--n $(N)) $(if $(BASE),--base $(BASE)) $(if $(filter 1,$(DRY)),--dry-run) $(if $(filter 1,$(YES)),--yes)

# PAID without DRY=1: bench.py run on an experiment file, by name under harness/experiments/ or by path (one with a / or ending .json; bench.py has its own pre-flight).
run:
	@test -n "$(EXPERIMENT)" || { echo "usage: make run EXPERIMENT=<name or path> [DRY=1] [FIRST=1]"; exit 2; }
	@$(PYTHON) harness/bench.py run $(if $(findstring /,$(EXPERIMENT))$(filter %.json,$(EXPERIMENT)),$(EXPERIMENT),harness/experiments/$(EXPERIMENT).json) $(if $(filter 1,$(DRY)),--dry-run) $(if $(filter 1,$(FIRST)),--first-only)

verify:
	@$(PYTHON) harness/bench.py verify

lint: lint-license lint-line-endings

lint-license:
	@echo Verifying that all files have license headers
	@$(PYTHON) scripts/lint/updateLicense.py $(shell $(LIST_LICENSE_SRC)) > $(FMT_LOG)
	@[ ! -s "$(FMT_LOG)" ] || (echo "License check failures, run 'make fmt'" | cat - $(FMT_LOG) && false)

lint-line-endings:
	@echo Verifying that all files use Unix line endings, have no trailing whitespace, and end with a final newline at EOF
	@$(PYTHON) scripts/lint/check-line-endings.py

fmt:
	@echo Running updateLicense.py on LICENSE_SRC ...
	@$(PYTHON) scripts/lint/updateLicense.py $(shell $(LIST_LICENSE_SRC))
	@echo Running check-line-endings on all files ...
	@$(PYTHON) scripts/lint/check-line-endings.py -u

# Coverage is a CI and maintainer tool: the harness and make test stay stdlib only.
install-test-tools:
	@$(PYTHON) -m pip install "coverage==7.17.2"

# The same suite as make test, under coverage, with an XML report in .cover/cover.xml.
test-ci:
	@$(PYTHON) -m coverage run -m unittest discover -s harness/tests
	@$(PYTHON) -m coverage xml -o .cover/cover.xml
	@$(PYTHON) -m coverage report
