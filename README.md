# 🛡️ Safe Scanarr

Monitors your media folders for new video files, generates contact sheets for review, and automatically detects inappropriate content, keeping your kids' media library safe.

## Features

- **Automatic NSFW detection:** runs locally on your server, no data sent anywhere
- **Zone-based handling:** configurable thresholds determine whether files are auto-approved, queued for review, quarantined, or rejected
- **Four-tab UI:** Review, Approved, Quarantine, Rejected
- **Quarantine:** suspicious files are moved rather than deleted, giving you a chance to review before permanently removing
- **Webhook notifications:** alerts when items are quarantined or rejected (Discord, Slack, Ntfy, Gotify, Home Assistant, etc.)
- **Sonarr/Radarr integration:** polls for new imports, blacklists rejected content
- **Scheduled scans:** configurable scan schedule as a safety net
- **Docker:** single container, runs alongside your existing stack

---

## Quick Start (Docker)

```bash
git clone https://github.com/YOURUSERNAME/safescanarr.git
cd safescanarr
# Edit docker-compose.yml; mount your media folders
docker compose up -d --build
```

Open **http://yourserver:8686** and configure everything from the **Config** page.

---

## Configuration

Only two environment variables are needed in `docker-compose.yml`:

| Variable | Default | Description |
|---|---|---|
| `BASE_DIR` | `/opt/safescanarr/data` | Where config, db, logs, and sheets are stored |
| `WEB_PORT` | `8686` | Web UI port |

All other settings (watch folders, detection zones, Sonarr/Radarr API keys, quarantine folder, webhooks, scan schedule) are managed from the **Config** page in the UI and saved to `./data/config.json`.

---

## Detection Zones

Three thresholds control what happens when NSFW content is detected:

```
Low confidence ──── Auto-approve ──── Review ──── Quarantine ──── Auto-reject ──── High confidence
```

| Zone | Default | Behavior |
|---|---|---|
| Auto-approve below | `0.1` | File approved automatically, no review needed |
| Quarantine above | `0.4` | File moved to quarantine folder for review |
| Auto-reject above | `0.85` | File deleted immediately |
| Between 0.1–0.4 | Review | Goes to the Review queue for manual decision |

Setting `quarantine_auto_reject_days = 0` skips quarantine entirely; files go straight to rejected.

---

## Tab Overview

- **Review:** new files awaiting manual approval, flagged items sorted to top
- **Approved:** confirmed clean files with their contact sheets
- **Quarantine:** files moved pending your decision; Approve restores the video, Reject deletes it
- **Rejected:** audit log of permanently deleted files

---

## docker-compose.yml volumes

```yaml
volumes:
  - ./data:/opt/safescanarr/data
  - /your/media/path:/mnt/media
```

Media folders must **not** be mounted `:ro` for quarantine and delete features to work.

---

## Updating

```bash
git pull
docker compose up -d --build
```

The database migrates automatically with no manual steps needed.

---

## Non-Docker install

```bash
sudo bash install.sh
```

---

## Credits

NSFW detection powered by [NudeNet](https://github.com/notAI-tech/NudeNet), an open source nudity detection library.

---

## License

Copyright (c) 2026 jckrbbt. All rights reserved.

This source code is made available for personal reference only. No use, copying, modification, distribution, or deployment of this code is permitted without explicit written permission from the author.
