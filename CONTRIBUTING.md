# Contributing

Merci ! / Thanks! Issues and pull requests are welcome, in English or French.

Coding-agent instructions (tool-agnostic) live in [`AGENTS.md`](AGENTS.md).
Prefer that single file over any vendor-specific rules file.

## Getting started

```bash
mise install    # uv and gitleaks, at the versions mise.toml names
make setup      # dependencies, the Home Assistant environment, the pre-commit hooks
make check      # CI's lint, test, home-assistant and audit jobs, in CI's order
```

uv installs the one Python `.python-version` names. Without
[mise](https://mise.jdx.dev), install by hand the uv version `mise.toml` names:
it is the one CI and the image use.

`make help` lists the rest. Every target mirrors one job of
`.github/workflows/ci.yml` and runs that job's commands, so a target green here
is its job green there — which is why these commands are a Makefile rather than
a list to copy from a page that drifts from the workflow.

`make check` does not run all of them. It covers CI's `lint`, `test`,
`home-assistant` and `audit` jobs; CI's `package`, `image` and `secrets` jobs
run only in CI, unless you call their targets by name. A green `make check`
says nothing about those three.

| Target | The CI job it mirrors | In `make check` |
| --------- | ------------------------------------------------------------- | --- |
| `lint` | `lint` — ruff's rules and formatting, checked, not applied | yes |
| `typecheck` | `lint` — mypy, strict, over the sources and the tests | yes |
| `test` | `test` — pytest with coverage, against a throwaway MQTT broker | yes |
| `test-ha` | `home-assistant` — minus hassfest and HACS, which are actions | yes |
| `audit` | `audit` — known vulnerabilities in the locked production set | yes |
| `package` | `package` — the wheel, used from an environment that has nothing else | no |
| `image` | `image` — one architecture here, two on the runner | no |
| `secrets` | `secrets` — the same gitleaks version | no |

`make test` starts the MQTT broker itself and removes it afterwards. That
matters: `tests/test_exporter_mqtt.py` is the one test that proves the exporter
really speaks MQTT, and without a broker it *skips*, which reads as a pass.

## Conventions

- **Every gateway call goes through `QuotaGovernor.reserve`** — the budget is
  the one thing that must never be wrong.
- **Errors are typed** (`errors.py`) and end up in the exit status, the journal
  or the web page.
- **Datetimes are timezone-aware** (ruff's `DTZ` rules check it).
- **Tests use made-up data**: fake tokens and PDLs such as `01234567890123`.
- A behavior change comes with a test that fails without it.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org)
  (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
- User-visible changes get a line in `CHANGELOG.md` under `[Unreleased]`.
- Design decisions worth remembering get an ADR in `docs/adr/`.

## Releases

1. Set `version` in `pyproject.toml` and `custom_components/releve/manifest.json`,
   the pinned tag in `README.md` and `docker-compose.yaml`, run `uv lock`, and
   turn `[Unreleased]` in `CHANGELOG.md` into `[X.Y.Z] - YYYY-MM-DD`. A test
   holds all of them equal to the project version.
2. Tag `vX.Y.Z` (or `vX.Y.Z-rc.N`) on `main` and push the tag.
3. The release workflow reruns CI, checks the tag against the version, pushes
   the multi-arch image to GHCR and creates the GitHub release with the wheel
   and the source archive.
