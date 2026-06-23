# Validator scripts

## `autoupdate.sh`

Background loop that pulls `origin/main` periodically and rebuilds the
validator container when new commits land, using the `docker compose` setup
in the repo root. Keeps an operator's validator on the latest code with no
manual intervention.

### What it does

- On startup: syncs to `origin/main`, then `docker compose up -d --build`
- Polls `origin/main` every 5 minutes (configurable via `AUTOUPDATE_INTERVAL`)
- On new commits: `git reset --hard origin/main` then `docker compose up -d --build`
- Logs to stdout — pipe to a file or rely on systemd/journald

### Prerequisites

- `git`, `docker`, and the `docker compose` v2 plugin
- A `.env` in the repo root (`cp .env.mainnet .env`, then set `BITTENSOR_HOTKEY_SEED`)

### Run it manually (foreground, useful for testing)

```bash
./scripts/autoupdate.sh
```

### Run it as a persistent service

You need to keep this running across reboots. Pick whichever fits your setup.

#### systemd (recommended on Linux)

Create `/etc/systemd/system/gm-validator-autoupdate.service`:

```ini
[Unit]
Description=gm-validator autoupdate
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/gm-validator
ExecStart=/path/to/gm-validator/scripts/autoupdate.sh
Restart=on-failure
RestartSec=30
User=YOUR_USER
# Optional overrides:
# Environment=AUTOUPDATE_INTERVAL=300
# Environment=AUTOUPDATE_BRANCH=main

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now gm-validator-autoupdate
sudo journalctl -u gm-validator-autoupdate -f
```

#### cron (simpler, less robust)

```bash
crontab -e
# Add:
@reboot cd /path/to/gm-validator && nohup ./scripts/autoupdate.sh >> /var/log/gm-validator-autoupdate.log 2>&1 &
```

Start it the first time without a reboot:

```bash
nohup ./scripts/autoupdate.sh >> /var/log/gm-validator-autoupdate.log 2>&1 &
```

#### tmux / screen (interactive, simplest)

```bash
tmux new -d -s autoupdate './scripts/autoupdate.sh'
# Detach: Ctrl-b d. Reattach: tmux attach -t autoupdate
```

This won't survive reboots — combine with `@reboot` cron if needed.

### Environment variables

| Variable | Default | What it does |
|---|---|---|
| `AUTOUPDATE_INTERVAL` | 300 | Seconds between update checks |
| `AUTOUPDATE_BRANCH` | main | Branch to track (override for forks) |

### Important notes

- The script does `git reset --hard origin/main`. **Don't hand-edit tracked
  files in the repo while the autoupdater is running** — they'll be discarded
  on the next pull. Your `.env` is gitignored, so your config and hotkey seed
  are never touched.
- The validator is stateless; the only on-disk state is the S3 mirror cache,
  preserved across rebuilds in the `gm_validator_mirror` Docker volume.
- On startup the script first syncs to `origin/$BRANCH`, then runs
  `docker compose up -d --build` — so a reboot or downtime never launches stale
  code: commits that landed while offline are pulled before the first start.
  (If the remote is unreachable at startup it falls back to the current
  checkout rather than refusing to run.)
- If a build fails, the previous container keeps running and the script
  retries on the next cycle.
