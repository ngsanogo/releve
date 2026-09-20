# Contributing

Merci ! / Thanks! Issues and pull requests are welcome, in English or French.

## Getting started

```bash
make setup      # dependencies, the Home Assistant environment, the pre-commit hooks
make check      # everything CI checks, in CI's order
```

`make help` lists the rest. Every target mirrors one job of
`.github/workflows/ci.yml`, so green here and green there mean the same thing —
which is why these commands are a Makefile rather than a list to copy from a
page that drifts from the workflow.

| Target | The CI job it mirrors |
| --------- | ------------------------------------------------------------- |
| `lint` | `lint` — ruff's rules and formatting, checked, not applied |
| `typecheck` | `lint` — mypy, strict, over the sources and the tests |
| `test` | `test` — pytest with coverage, against a throwaway MQTT broker |
| `test-ha` | `home-assistant` — minus hassfest and HACS, which are actions |
| `audit` | `audit` — known vulnerabilities in the locked production set |
| `package` | `package` — the wheel, used from an environment that has nothing else |
| `image` | `image` — one architecture here, two on the runner |
| `secrets` | `secrets` — the same gitleaks version |

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
