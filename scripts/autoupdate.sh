#!/usr/bin/env bash
#
# Autoupdate loop for the gm-validator.
#
# What it does:
#   1. Periodically fetches origin/main.
#   2. When new commits exist, hard-resets to them and restarts the
#      validator with the README's docker compose setup.
#   3. Designed to survive machine reboots when wired into a system
#      service (systemd, cron @reboot, etc — see scripts/README.md).
#
# Notes:
#   - This script does NOT install itself as a service. Run it via
#     systemd, supervisord, tmux/screen, or `nohup ./autoupdate.sh &`.
#     Examples are in scripts/README.md.
#   - It uses `docker compose up -d --build` (NOT `docker compose pull`)
#     because docker-compose.yml builds from the bundled Dockerfile rather
#     than a pre-built image.
#   - Safe to run alongside the validator: docker compose handles the
#     graceful restart, and the on-disk S3 mirror persists in its named
#     volume across rebuilds.
#
set -euo pipefail

# Resolve the repo root from the script location (script lives in scripts/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# How often to check for updates (seconds). 5 minutes is plenty — main
# doesn't move fast and we don't want to thrash the git remote / registry.
CHECK_INTERVAL="${AUTOUPDATE_INTERVAL:-300}"

# Branch to track. Override with AUTOUPDATE_BRANCH if you maintain a fork.
BRANCH="${AUTOUPDATE_BRANCH:-main}"

log() {
    printf '%s [autoupdate] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        log "ERROR: required command not found: $1"
        exit 1
    }
}

require_cmd git
require_cmd docker

if ! docker compose version >/dev/null 2>&1; then
    log "ERROR: 'docker compose' (v2 plugin) is required"
    exit 1
fi

cd "$REPO_DIR"

if [ ! -d .git ]; then
    log "ERROR: $REPO_DIR is not a git repository"
    exit 1
fi

if [ ! -f docker-compose.yml ]; then
    log "ERROR: docker-compose.yml not found in $REPO_DIR"
    exit 1
fi

if [ ! -f .env ]; then
    log "ERROR: .env not found — copy .env.mainnet to .env and set BITTENSOR_HOTKEY_SEED"
    exit 1
fi

restart_validator() {
    # `up -d --build` is idempotent: rebuilds the image with the new code
    # and restarts the container. The S3 mirror volume persists.
    log "Rebuilding and restarting validator..."
    if docker compose up -d --build; then
        log "Validator restarted successfully"
    else
        log "ERROR: docker compose up failed (will retry on next cycle)"
        return 1
    fi
}

log "Starting autoupdate loop (branch=$BRANCH, interval=${CHECK_INTERVAL}s, repo=$REPO_DIR)"

# Pull the latest code BEFORE the first start, so a reboot or downtime never
# launches stale code (the loop then keeps it current). Best-effort: if the
# remote is unreachable, start the current checkout rather than refuse to run.
log "Syncing to origin/$BRANCH before initial start..."
if git fetch origin "$BRANCH" --quiet 2>&1 && git reset --hard "origin/$BRANCH" --quiet; then
    log "Synced to $(git rev-parse --short HEAD): $(git log -1 --pretty=format:'%s')"
else
    log "Initial sync failed (offline?) — starting with the current checkout"
fi

log "Initial validator start..."
restart_validator || log "Initial start had errors — continuing anyway"

while true; do
    sleep "$CHECK_INTERVAL"

    # Fetch latest refs without touching the working tree.
    if ! git fetch origin "$BRANCH" --quiet 2>&1; then
        log "git fetch failed — will retry next cycle"
        continue
    fi

    LOCAL=$(git rev-parse HEAD)
    REMOTE=$(git rev-parse "origin/$BRANCH")

    if [ "$LOCAL" = "$REMOTE" ]; then
        continue
    fi

    log "New commits on origin/$BRANCH ($LOCAL -> $REMOTE), pulling..."

    # Hard checkout to remote — discards any local changes. Validators
    # should never be hand-editing files here; if they are, they shouldn't
    # use this script. (.env is gitignored, so the operator's seed is safe.)
    if ! git reset --hard "origin/$BRANCH" --quiet; then
        log "ERROR: git reset failed — manual intervention needed"
        continue
    fi

    log "Pulled $(git rev-parse --short HEAD): $(git log -1 --pretty=format:'%s')"

    restart_validator || true
done
