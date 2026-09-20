# releve — every command this repository has, in one place.
#
# `make check` is what CI runs, in the order CI runs it (.github/workflows/ci.yml).
# Green here and green there mean the same thing, which is the only reason this
# file exists: the commands used to live as prose in CONTRIBUTING.md, where a
# flag drifts from the workflow without anything noticing.
#
# One tool: uv. It owns the Python versions, the lockfile and every dev
# dependency; nothing here needs a Python on the machine.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# The integration is tested against a REAL Home Assistant core, which pins its
# own versions of libraries this project also locks. Hence a second environment,
# built from tests_ha/requirements.txt and never from uv.lock, on the Python
# version CI uses for that job.
HA_VENV    := .venv-ha
HA_PYTHON  := 3.14

# The MQTT exporter's integration test needs a broker, and skips itself without
# one — a skip that looks like a pass. CI starts mosquitto for it; so does
# `make test`, on the loopback and on a port nothing else on this machine holds.
MQTT_PORT      := 1883
MQTT_CONTAINER := releve-test-mqtt

# Paths ruff is pointed at. tests_ha and custom_components are first-party too
# (pyproject's `tool.ruff.src`), so leaving them out would lint less than CI.
SOURCES := src tests custom_components tests_ha

.PHONY: help setup check lint typecheck test test-ha audit package image secrets fmt clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*## "}; {printf "  make %-10s %s\n", $$1, $$2}'

setup: ## Once after a clone: dependencies, the Home Assistant environment, the hooks
	uv sync --frozen
	uv venv --python $(HA_PYTHON) --allow-existing $(HA_VENV)
	uv pip install --quiet --python $(HA_VENV) -r tests_ha/requirements.txt
	uvx pre-commit install
	@echo "✔ ready — 'make check' is what CI runs."

check: lint typecheck test test-ha audit ## Everything CI checks, in CI's order

# --- What CI's `lint` job runs ---------------------------------------------
lint: ## ruff: rules and formatting, checked not applied
	uv run ruff check $(SOURCES)
	uv run ruff format --check $(SOURCES)

typecheck: ## mypy, strict, over the sources and the tests
	uv run mypy

# --- What CI's `test` job runs ----------------------------------------------
# The broker is started and removed here rather than left to the reader: the one
# test that needs it is the one that proves the exporter actually speaks MQTT,
# and it is silent about being skipped.
test: ## pytest with coverage (90% floor), against a throwaway MQTT broker
	@docker rm --force $(MQTT_CONTAINER) >/dev/null 2>&1 || true
	@docker run --detach --rm --name $(MQTT_CONTAINER) \
	  --publish 127.0.0.1:$(MQTT_PORT):1883 \
	  eclipse-mosquitto:2 mosquitto -c /mosquitto-no-auth.conf >/dev/null
	@trap 'docker rm --force $(MQTT_CONTAINER) >/dev/null 2>&1 || true' EXIT; \
	  MQTT_TEST_BROKER=127.0.0.1:$(MQTT_PORT) \
	  uv run pytest --cov --cov-report=term-missing --cov-fail-under=90

# --- What CI's `home-assistant` job runs ------------------------------------
# hassfest and the HACS validation are GitHub actions with no local equivalent;
# they are NOT run here, and that is the whole difference between this target
# and that job.
test-ha: ## The integration against the pinned Home Assistant core: types, then tests
	@uv venv --quiet --python $(HA_PYTHON) --allow-existing $(HA_VENV)
	@uv pip install --quiet --python $(HA_VENV) -r tests_ha/requirements.txt
	$(CURDIR)/$(HA_VENV)/bin/python -m mypy --config-file tests_ha/mypy.ini custom_components/releve
	cd tests_ha && $(CURDIR)/$(HA_VENV)/bin/python -m pytest -q

# --- What CI's `audit` job runs ---------------------------------------------
audit: ## Known vulnerabilities in the locked PRODUCTION dependencies
	@tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	  uv export --frozen --no-dev --no-emit-project --output-file "$$tmp/requirements.txt" >/dev/null; \
	  uv run pip-audit --requirement "$$tmp/requirements.txt" --disable-pip --progress-spinner off

# --- The two jobs that build an artefact ------------------------------------
package: ## Build the wheel and use it from an environment that has nothing else
	uv build
	@tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	  uv venv --quiet "$$tmp/venv"; \
	  uv pip install --quiet --python "$$tmp/venv" dist/releve-*.whl; \
	  XDG_CONFIG_HOME="$$tmp/config" XDG_STATE_HOME="$$tmp/state" \
	  RELEVE_GATEWAY__TOKEN=not-a-real-token PATH="$$tmp/venv/bin:$$PATH" \
	    bash -c 'releve version && python -m releve version && releve init && releve check && releve status' ; \
	  test -f "$$tmp/state/releve/releve.db" && echo "✔ the wheel alone runs and writes its database"

image: ## Build the container for this machine's architecture and ask it for /healthz
	docker build --tag releve:local .
	@tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"; docker rm --force releve-smoke >/dev/null 2>&1 || true' EXIT; \
	  printf '%s\n' 'gateway:' '  token: not-a-real-token' '  base_url: https://127.0.0.1:9' \
	    'usage_points:' '  - id: "01234567890123"' 'sync:' '  rte_signals: false' > "$$tmp/config.yaml"; \
	  chmod 644 "$$tmp/config.yaml"; \
	  docker run --detach --name releve-smoke --publish 127.0.0.1:8080:8080 \
	    --volume "$$tmp/config.yaml:/home/app/config.yaml:ro" releve:local >/dev/null; \
	  for _ in $$(seq 30); do \
	    if curl --silent --fail http://127.0.0.1:8080/healthz >/dev/null; then echo "✔ healthy"; exit 0; fi; \
	    sleep 2; \
	  done; \
	  docker logs releve-smoke; exit 1

secrets: ## Scan the working tree and the whole history (same gitleaks as CI)
	gitleaks git --verbose --redact .

# --- Everyday ---------------------------------------------------------------
fmt: ## Apply ruff's fixes and formatting
	uv run ruff check --fix $(SOURCES)
	uv run ruff format $(SOURCES)

clean: ## Remove the build output and the caches
	rm -rf dist build .mypy_cache .ruff_cache .pytest_cache .coverage htmlcov
