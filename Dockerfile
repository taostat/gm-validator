# syntax=docker/dockerfile:1.7
# gm-validator — Python service that subprocess-spawns the gm-verifier
# Rust binary against each finalized epoch's S3 artifacts.
#
# Build context: repo root.
#   docker build -t gm-validator .

# ── Rust builder stage ────────────────────────────────────────────────────────
# Compile the gm-verifier release binary. Pinned by digest so the build is
# reproducible against the same toolchain.
FROM rust:1.83-slim-bookworm@sha256:540c902e99c384163b688bbd8b5b8520e94e7731b27f7bd0eaa56ae1960627ab AS rust-build

ENV SOURCE_DATE_EPOCH=1700000000 \
    RUSTFLAGS="-C codegen-units=1 -C debuginfo=0" \
    CARGO_INCREMENTAL=0

WORKDIR /build

# Copy workspace + verifier manifests first for layer caching.
COPY Cargo.toml Cargo.lock ./
COPY verifier/Cargo.toml ./verifier/

# Pre-build dependency layer with a dummy main; lets cargo cache the
# dep compilation across source-only changes. The verifier exposes
# both a lib and a bin, so we stub both with valid placeholders.
RUN mkdir -p verifier/src \
    && echo "fn main() {}" > verifier/src/main.rs \
    && echo "" > verifier/src/lib.rs \
    && cargo build --release --bin gm-verifier 2>/dev/null || true \
    && rm -rf verifier/src

# Real sources.
COPY verifier/src/ ./verifier/src/

# Touch to invalidate the dummy build cache, then build for real.
RUN touch verifier/src/main.rs verifier/src/lib.rs \
    && cargo build --release --bin gm-verifier \
    && strip target/release/gm-verifier

# ── Python builder stage ──────────────────────────────────────────────────────
FROM python:3.13-slim-bookworm@sha256:386df64585134ba00b1d5e307acb1e72f33e9e87dbbb00aad9b8f24dbb51db72 AS py-build

ENV SOURCE_DATE_EPOCH=1700000000 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=ghcr.io/astral-sh/uv:0.11.9 /uv /usr/local/bin/uv

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
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --no-create-home app

WORKDIR /app

# Python venv (carries the editable gm-validator install) + source.
COPY --from=py-build /app/.venv /app/.venv
COPY validator/src/ validator/src/

# gm-verifier binary on PATH. The validator subprocess-spawns it as
# `gm-verifier verify --epoch N --dir D --sample S` (see
# validator/src/gm_validator/verifier.py); env GM_VERIFIER_BIN can
# override the location.
COPY --from=rust-build /build/target/release/gm-verifier /usr/local/bin/gm-verifier

# Local S3 mirror — the validator copies finalized artifacts here
# before invoking the verifier. Created at runtime by gm_validator if
# absent; this just declares a sane mount point.
RUN mkdir -p /var/cache/gm-validator \
    && chown 1000:1000 /var/cache/gm-validator

WORKDIR /app/validator

USER 1000

# Prometheus metrics (prometheus_client.start_http_server).
EXPOSE 9092

ENTRYPOINT ["python", "-m", "gm_validator.main"]
