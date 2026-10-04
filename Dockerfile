# syntax=docker/dockerfile:1.27.1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# releve: a small, non-root image.
#
# Configuration at /home/app/config.yaml, data in the /home/app/data volume, uid 1000.
ARG PYTHON_IMAGE=python:3.14.8-slim-trixie@sha256:c3e521df8b2b498a7a682e7e18676771cb80c6b75b8699af886b2d554ce40151

FROM ghcr.io/astral-sh/uv:0.12.22@sha256:f513a91fc62fe7c17567eee97230dd198e43edb8a9fbecca843714a4358fe1bc AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first: this layer is reused until uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE NOTICE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="releve" \
      org.opencontainers.image.description="Quota-aware local cache of French electricity meter readings, with MQTT and time-series exports" \
      org.opencontainers.image.source="https://github.com/ngsanogo/releve" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN useradd --create-home --uid 1000 app \
 && install --directory --owner app --group app --mode 0700 /home/app/data

# The venv's scripts embed /app/.venv: same path as in the build stage.
COPY --from=build /app/.venv /app/.venv
COPY LICENSE NOTICE /licenses/
# TZ: releve states every instant in Paris civil time; so does its log.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Paris \
    RELEVE_CONFIG=/home/app/config.yaml \
    RELEVE_DEFAULT_DATABASE=/home/app/data/releve.db \
    RELEVE_WEB__HOST=0.0.0.0

USER app
WORKDIR /home/app
VOLUME ["/home/app/data"]
EXPOSE 8080

# /healthz answers 503 when the database is unusable, the scheduler stalled or the last pass crashed.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]

ENTRYPOINT ["releve"]
CMD ["serve"]
