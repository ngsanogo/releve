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

`make help` lists every target. Each one runs the commands of a CI job, so a
target green here is its job green there. `make check` covers `lint`, `test`,
`home-assistant` (minus hassfest and HACS) and `audit`; `package`, `image` and
`secrets` run only in CI unless you call them by name.

`make test` starts the MQTT broker itself and removes it afterwards:
`tests/test_exporter_mqtt.py` is the one test that proves the exporter really
speaks MQTT, and without a broker it *skips*, which reads as a pass.

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

## Releases

1. Set `version` in `pyproject.toml` and `custom_components/releve/manifest.json`,
   the pinned tag in `README.md` and `docker-compose.yaml`, run `uv lock`, and
   turn `[Unreleased]` in `CHANGELOG.md` into `[X.Y.Z] - YYYY-MM-DD`. A test
   holds all of them equal to the project version.
2. Tag `vX.Y.Z` (or `vX.Y.Z-rc.N`) on `main` and push the tag.
3. The release workflow reruns CI, checks the tag against the version, pushes
   the multi-arch image to GHCR and creates the GitHub release with the wheel
   and the source archive.
