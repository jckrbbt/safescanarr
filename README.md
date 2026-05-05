# 🛡️ Safe Scanarr

Monitors your media folders for new or changed video files, generates
**video contact sheets** for review, and lets you approve or delete media
from a clean web UI.

## Features

- **Web UI** — review contact sheets, manage config, browse DB, tail logs
- **API poller** — checks Sonarr/Radarr every 10 minutes for new imports
- **Nightly scan** — full folder scan at midnight as a safety net
- **Delete & blacklist** — remove bad media and tell Sonarr/Radarr to retry
- **Docker** — single container, runs alongside your existing stack

---

## Quick Start (Docker)

```bash
git clone https://github.com/jckrbbt/safescanarr.git
cd safescanarr

# Edit docker-compose.yml — mount your media folders
docker compose up -d --build
```

Open **http://yourserver:8686** and configure everything from the **Config** page.

---

## Configuration

Only two environment variables are needed in `docker-compose.yml`:

| Variable | Default | Description |
|---|---|---|
| `BASE_DIR` | `/opt/safescanarr/data` | Where config, db, and logs are stored |
| `WEB_PORT` | `8686` | Web UI port |

Everything else — Sonarr/Radarr URLs, API keys, watch folders, poll interval,
vcsi settings — is configured from the **Config** page in the UI and saved to
`./data/config.json`.

---

## docker-compose.yml volumes

Mount your media folders so the container can read (and optionally delete) them:

```yaml
volumes:
  - ./data:/opt/safescanarr/data
  - /your/media/path:/mnt/media
```

Remove `:ro` if you want the Delete Media feature to work.

---

## Updating

```bash
git pull
docker compose up -d --build
```

---

## Non-Docker install

```bash
sudo bash install.sh
```

---

## License

Copyright (c) 2026 jckrbbt. All rights reserved.

This source code is made available for personal reference only. No use, copying,
modification, distribution, or deployment of this code is permitted without
explicit written permission from the author.
