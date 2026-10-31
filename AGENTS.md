# Agent guide — releve

Vendor-neutral instructions for coding agents. Human conventions:
[`CONTRIBUTING.md`](CONTRIBUTING.md). Deeper design notes (historical, not
binding): [`docs/architecture.md`](docs/architecture.md).

If a tool only reads a differently named file, symlink it here rather than
duplicating rules.

## Project

Self-hosted cache of French electricity meter readings from
[MyElectricalData](https://www.myelectricaldata.fr). One process, one SQLite
database, one YAML config. Syncs within a daily call budget, then exports to
Home Assistant, MQTT and InfluxDB/VictoriaMetrics, plus a small web / JSON /
Prometheus surface. HACS integration: `custom_components/releve/` (JSON API
client only; never imports the `releve` package).

**Stack:** Python from `.python-version`, [uv](https://docs.astral.sh/uv/),
Pydantic, Starlette, SQLite, Ruff, mypy, pytest. License: Apache-2.0.

Only OSI-permissive tooling belongs in the distributed product. `tests_ha/` may
pull Home Assistant for CI only — never vendor that into the package or image.

## Commands

One tool for the main package: **uv**. Prefer the Makefile; targets mirror CI
(`.github/workflows/ci.yml`).

| Command | Purpose |
| --- | --- |
| `make setup` | Once after clone: deps, HA venv, pre-commit hooks |
| `make check` | Local gate: lint + typecheck + test + test-ha + audit |
| `make test` | pytest (starts a throwaway MQTT broker) |
| `make fmt` | Apply Ruff fixes and formatting |
| `make help` | List all targets |

`make check` covers CI's `lint`, `test`, `home-assistant` (minus hassfest/HACS)
and `audit`. CI's `package`, `image` and `secrets` are separate targets.

The Home Assistant integration uses a second env (`.venv-ha`, same Python) from
`tests_ha/requirements.txt` — `make test-ha` manages it. Do not invent parallel
pip/venv workflows for the main package.

**Pins:** each version has one source (`.python-version`, `mise.toml`,
`pyproject.toml` with `==`). Change at the source, update every copy (Dockerfile,
workflows, pre-commit), then `uv lock` / `mise lock` as needed. Do not add `[tool.uv] required-version`.

## Layout

| Path | Role |
| --- | --- |
| `src/releve/` | Application |
| `tests/` | Unit and integration tests (90% coverage floor) |
| `tests_ha/` | HA-core environment for the custom component |
| `custom_components/releve/` | HACS integration |
| `docs/` | Architecture notes |

Domain types live in `domain.py`, `config.py` and `errors.py`.

## Hard rules

1. **Every gateway HTTP call goes through `QuotaGovernor.reserve`.**
2. **Errors are typed** in `errors.py`; no empty `except` blocks.
3. **Datetimes are timezone-aware** (Ruff `DTZ`). Instants as unix seconds UTC;
   Paris civil days as ISO dates.
4. **The SQLite cache is the source of truth.** Upserts never erase richer data
   with poorer answers.
5. **Tests use fake tokens and PDLs** (e.g. `01234567890123`).
6. **Behaviour change ⇒ failing test first**, then the fix.
7. **Conventional Commits**; user-visible changes in `CHANGELOG.md` under
   `[Unreleased]`.
8. **`cursor` / `export_cursor` means an exporter's resume position** in the
   cache's change feed.

## Do not commit

Runtime databases (`*.db*`), `config.yaml`, `.env`, secrets, coverage output,
local venvs, Hypothesis / tool caches. See `.gitignore`.
