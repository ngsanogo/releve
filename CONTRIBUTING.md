# Contributing

Merci ! / Thanks! Issues and pull requests are welcome, in English or French.

## Getting started

```bash
uv sync                                    # dependencies, including the dev group
uvx pre-commit install                     # ruff and gitleaks before each commit
uv run pytest --cov                        # tests (CI expects 90% coverage)
uv run mypy                                # strict, sources and tests
uv run ruff check src tests && uv run ruff format --check src tests
```

The MQTT integration test needs a broker:

```bash
docker run --rm -d -p 127.0.0.1:1883:1883 eclipse-mosquitto:2 mosquitto -c /mosquitto-no-auth.conf
MQTT_TEST_BROKER=127.0.0.1:1883 uv run pytest tests/test_exporter_mqtt.py
```

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

1. Set `version` in `pyproject.toml`, run `uv lock`, and turn `[Unreleased]` in
   `CHANGELOG.md` into `[X.Y.Z] - YYYY-MM-DD`.
2. Tag `vX.Y.Z` (or `vX.Y.Z-rc.N`) on `main` and push the tag.
3. The release workflow reruns CI, checks the tag against the version, pushes
   the multi-arch image to GHCR and creates the GitHub release with the wheel
   and the source archive.
