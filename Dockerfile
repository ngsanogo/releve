# syntax=docker/dockerfile:1
# releve: a small, non-root image.
#
# Configuration at /home/app/config.yaml, data in the /home/app/data volume, uid 1000.
ARG PYTHON_IMAGE=python:3.14-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

FROM ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc AS uv

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
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="releve" \
      org.opencontainers.image.description="Quota-aware local cache of Enedis Linky data, exported to Home Assistant, MQTT and InfluxDB" \
      org.opencontainers.image.source="https://github.com/ngsanogo/releve" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN useradd --create-home --uid 1000 app \
 && install --directory --owner app --group app --mode 0700 /home/app/data

# The venv's scripts embed /app/.venv: same path as in the build stage.
COPY --from=build /app/.venv /app/.venv
# TZ: releve states every instant in Paris time (Enedis counts Paris days); so does its log.
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
