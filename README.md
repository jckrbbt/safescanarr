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
git clone https://github.com/YOURUSERNAME/safescanarr.git
cd safescanarr

# Edit docker-compose.yml — update volume paths and API keys
docker compose up -d --build
```

Open **http://yourserver:8686**

---

## Configuration

Edit `docker-compose.yml` environment variables:

| Variable | Default | Description |
|---|---|---|
| `WATCH_FOLDERS` | see compose | Comma-separated folders to monitor |
| `OUTPUT_DIR` | `./data/vcs` | Where contact sheets are saved |
| `WEB_PORT` | `8686` | Web UI port |

Settings can also be changed live from the **Config** page in the UI.

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

See `install.sh` for details.

---

## License

Copyright (c) 2026 jckrbbt. All rights reserved.

This source code is made available for personal reference only. No use, copying,
modification, distribution, or deployment of this code is permitted without
explicit written permission from the author.
