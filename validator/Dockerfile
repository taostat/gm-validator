# syntax=docker/dockerfile:1.7
# gm-validator — Python worker + gm-verifier Rust binary (subprocess).
#
# Build context: repo root
#   docker build -f validator/Dockerfile .
#
# Stage 1: compile the gm-verifier Rust binary
FROM rust:1.92-slim-bookworm@sha256:f1f73538ebe623fd3673a35aff3df358ae1084c64c55646516e5b17b321b6c9b AS rust-builder

ENV SOURCE_DATE_EPOCH=1700000000 \
    RUSTFLAGS="-C codegen-units=1 -C debuginfo=0" \
    CARGO_INCREMENTAL=0

WORKDIR /build

# Dependency layer: cache cargo fetch separately from source compilation.
COPY Cargo.toml Cargo.lock ./
COPY verifier/Cargo.toml verifier/
# Stub out the binary source so `cargo fetch` + the dep compile layer works.
RUN mkdir -p verifier/src \
    && echo 'fn main() {}' > verifier/src/main.rs \
    && cargo build --release --bin gm-verifier \
    && rm verifier/src/main.rs

# Real source compile
COPY verifier/src/ verifier/src/
RUN touch verifier/src/main.rs \
    && cargo build --release --bin gm-verifier

# ── Stage 2: install Python deps ─────────────────────────────────────────────
FROM python:3.13-slim-bookworm@sha256:386df64585134ba00b1d5e307acb1e72f33e9e87dbbb00aad9b8f24dbb51db72 AS python-build

ENV SOURCE_DATE_EPOCH=1700000000 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=ghcr.io/astral-sh/uv:0.11.9 /uv /usr/local/bin/uv

WORKDIR /app

COPY validator/pyproject.toml validator/uv.lock validator/

RUN uv sync --frozen --no-dev --project validator

# ── Stage 3: runtime image ────────────────────────────────────────────────────
FROM python:3.13-slim-bookworm@sha256:386df64585134ba00b1d5e307acb1e72f33e9e87dbbb00aad9b8f24dbb51db72

ENV SOURCE_DATE_EPOCH=1700000000 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --no-create-home app

WORKDIR /app

# Python venv
COPY --from=python-build /app/.venv /app/.venv
# Python source
COPY validator/src/ src/
# Rust binary — place on PATH
COPY --from=rust-builder /build/target/release/gm-verifier /usr/local/bin/gm-verifier

USER 1000

EXPOSE 9092

ENTRYPOINT ["python", "-m", "gm_validator.main"]
