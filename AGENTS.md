# Agent guide — releve

Vendor-neutral instructions for any coding agent working in this repository.
Human contributor conventions live in [`CONTRIBUTING.md`](CONTRIBUTING.md);
architecture in [`docs/architecture.md`](docs/architecture.md) and
[`docs/adr/`](docs/adr/).

If a tool only reads a differently named file, point that file at this one
(e.g. a symlink) rather than duplicating rules.

## Project

Self-hosted, quota-aware local cache of French electricity meter readings from
[MyElectricalData](https://www.myelectricaldata.fr). One process, one SQLite
database, one YAML config. Syncs history within a daily call budget, then
exports to Home Assistant, MQTT and InfluxDB/VictoriaMetrics, plus a small web /
JSON / Prometheus surface. A HACS Home Assistant integration lives in
`custom_components/releve/` (JSON API client only; it never imports the
`releve` package).

**Stack:** Python 3.14 (one version, named by `.python-version`: the image runs
it and CI tests on it), [uv](https://docs.astral.sh/uv/), Pydantic, Starlette,
SQLite, Ruff, mypy, pytest. License: Apache-2.0 (see `LICENSE` and `NOTICE`).

Only OSI-permissive, commercially reusable tooling belongs in the distributed
product. Do not add proprietary SDKs, non-commercial-only licenses, GPL/AGPL
runtime dependencies, or vendor-specific agent rule files. `tests_ha/` may pull
Home Assistant (and its copyleft transitives) for CI only — never vendor that
environment into the package or image.

## Command surface

Prefer the Makefile. Targets mirror CI (`.github/workflows/ci.yml`): green
locally means the same checks as the pipeline.

| Command | Purpose |
| --- | --- |
| `make setup` | Once after clone: deps, HA venv, pre-commit hooks |
| `make check` | Full gate: lint + typecheck + test + test-ha + audit |
| `make fmt` | Apply Ruff fixes and formatting |
| `make help` | List all targets |

`uv` owns the Python version, the lockfile and every dependency. Do not invent
parallel pip/venv workflows for the main package. The Home Assistant integration
uses a separate env (`.venv-ha`, same Python) built from
`tests_ha/requirements.txt` — `make test-ha` manages it.

**Versions are exact, each with one source.** `mise.toml` names uv and gitleaks
(`mise install`; `mise.lock` holds their checksums), `.python-version` names
Python, `pyproject.toml` pins every dependency with `==`. The other places that
repeat a number (Dockerfile, CI, pre-commit) are held to those by
`tests/test_toolchain_pins.py`. Move a version at its source, then run
`uv lock` / `mise lock` as the case may be.

## Layout

| Path | Role |
| --- | --- |
| `src/releve/` | Application (cli, sync, store, gateway, exporters, web) |
| `tests/` | Unit and integration tests (pytest, 90% coverage floor) |
| `tests_ha/` | HA-core environment for the custom component |
| `custom_components/releve/` | HACS integration |
| `docs/` | Architecture + ADRs |
| `contrib/grafana/` | Optional dashboard JSON |

Domain types and contracts live in `domain.py`, `config.py` (Pydantic) and
`errors.py`. Prefer those over prose when changing behaviour.

## Hard rules

1. **Every gateway HTTP call goes through `QuotaGovernor.reserve`** — no other
   path to the gateway.
2. **Errors are typed** in `errors.py`; no empty `except` blocks.
3. **Datetimes are timezone-aware** (Ruff `DTZ`). Instants as unix seconds UTC;
   Paris civil days as ISO dates.
4. **The SQLite cache is the source of truth.** Upserts never erase richer data
   with poorer answers.
5. **Tests use fake tokens and PDLs** (e.g. `01234567890123`).
6. **Behaviour change ⇒ failing test first**, then the fix.
7. **Conventional Commits**; user-visible changes go in `CHANGELOG.md` under
   `[Unreleased]`.
8. **`cursor` / `export_cursor` means an exporter's resume position** in the
   cache's change feed.

## Do not commit

Runtime databases (`*.db*`), `config.yaml`, `.env`, secrets, coverage output,
local venvs, Hypothesis / tool caches. See `.gitignore` and `.ignore`.
