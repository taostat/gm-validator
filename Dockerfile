# syntax=docker/dockerfile:1.7
# gm-validator — Python service that polls S3 for finalized epoch
# artifacts and submits Bittensor weights.
#
# Build context: repo root.
#   docker build -t gm-validator .

# ── Python builder stage ──────────────────────────────────────────────────────
FROM python:3.13-slim-bookworm@sha256:386df64585134ba00b1d5e307acb1e72f33e9e87dbbb00aad9b8f24dbb51db72 AS py-build

ENV SOURCE_DATE_EPOCH=1700000000 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b /uv /usr/local/bin/uv

WORKDIR /app

COPY validator/pyproject.toml validator/uv.lock validator/
# uv sync installs the project editable, so the package directory must
# exist before sync runs.
COPY validator/src/ validator/src/

# Pin the venv to /app/.venv so the runtime stage copies from a stable
# path regardless of which workdir uv sync runs in.
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app/validator
RUN uv sync --frozen --no-dev

# ── Runtime image ─────────────────────────────────────────────────────────────
FROM python:3.13-slim-bookworm@sha256:386df64585134ba00b1d5e307acb1e72f33e9e87dbbb00aad9b8f24dbb51db72

ENV SOURCE_DATE_EPOCH=1700000000 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    HOME=/home/app

# `import bittensor` creates ~/.bittensor at import time, so the app user
# needs a writable HOME. Without this the container crashes on startup with
# PermissionError on /home/app (the k8s deployment used to paper over it with
# a HOME env + emptyDir mount; baking it into the image fixes every run path —
# docker compose, bare docker, and k8s alike).
# The base image ships ca-certificates 20230311+deb12u1; this upgrades it to
# the current bookworm-security release, and the check after the venv copy
# fails the build if a removed root is still in the OS bundle or in the venv's
# certifi (scripts/check_ca_bundle.py). Unpinned on purpose: operators rebuild
# this image themselves from main (scripts/autoupdate.sh), and the security
# mirror drops superseded versions, so a pin would eventually fail their
# rebuilds where nobody sees it. taostat/gm pins the version because it builds
# in CI, where a failed build is visible.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --create-home app \
    && mkdir -p /home/app/.bittensor \
    && chown -R 1000:1000 /home/app

WORKDIR /app

# Python venv (carries the editable gm-validator install) + source.
COPY --from=py-build /app/.venv /app/.venv
RUN --mount=type=bind,source=scripts/check_ca_bundle.py,target=/tmp/check_ca_bundle.py \
    python -I /tmp/check_ca_bundle.py /etc/ssl/certs/ca-certificates.crt "$(python -m certifi)"
COPY validator/src/ validator/src/

# Local S3 mirror — the validator copies finalized artifacts here as
# an on-disk audit cache. Created at runtime by gm_validator if absent;
# this just declares a sane mount point.
RUN mkdir -p /var/cache/gm-validator \
    && chown 1000:1000 /var/cache/gm-validator

WORKDIR /app/validator

USER 1000

ENTRYPOINT ["python", "-m", "gm_validator.main"]
