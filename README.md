# Safe Scanarr

Safe Scanarr is a self-hosted media monitoring tool for parents who run automated media servers and want to keep their kids' libraries free from inappropriate content.

When new media is added (or when existing media changes), Safe Scanarr automatically scans it for explicit material using local on-device detection (nothing leaves your server). Each file gets a contact sheet for visual review and is sorted into one of four states: approved, pending review, quarantined, or rejected. Confidence thresholds are configurable, allowing low-risk content to be approved automatically, high-risk content to be quarantined or removed, and anything in between to be flagged for manual review.

It is designed to assist regular review, not replace it.

---

## Disclaimer

Safe Scanarr is intended to help monitor automated media libraries for potentially inappropriate content. It is not a substitute for regular manual review by a responsible adult.

Detection may produce both false positives and false negatives. No automated detection system should be relied upon as a complete safeguard. The user is solely responsible for all content on their system and for ensuring their media library is appropriate for their intended audience.

---

## Features

- Automatic NSFW detection runs locally on your server; no data is sent anywhere
- Configurable detection profiles (Conservative, Balanced, Aggressive, Custom)
- Zone-based handling: auto-approve clean content, flag ambiguous content for review, quarantine or remove high-risk content
- Four-tab UI: Review, Approved, Quarantine, Rejected
- Quarantine: suspicious files are moved rather than deleted immediately, giving you a chance to review before permanently removing
- Lightbox viewer with keyboard navigation and per-item actions
- Webhook notifications for quarantine and reject events (Discord, Slack, Ntfy, Gotify, Home Assistant, etc.)
- Arr integration: polls for new imports and blacklists rejected content
- Scheduled scans as a safety net for anything missed
- Docker: single container, runs alongside your existing stack

---

## Quick Start

```bash
git clone https://github.com/jckrbbt/safescanarr.git
cd safescanarr
```

Edit `docker-compose.yml` to mount your media folders, then:

```bash
docker compose up -d --build
```

Open **http://yourserver:8686** and configure everything from the Config page.

---

## Configuration

Only two environment variables are required in `docker-compose.yml`:

| Variable | Default | Description |
|---|---|---|
| `BASE_DIR` | `/opt/safescanarr/data` | Where config, database, logs, and contact sheets are stored |
| `WEB_PORT` | `8686` | Web UI port |

All other settings are managed from the Config page in the UI and saved to `./data/config.json`.

---

## Detection Zones

Three thresholds control what happens when content is detected:

```
Low risk ---- Auto-approve ---- Review ---- Quarantine ---- Auto-reject ---- High risk
```

| Zone | Behavior |
|---|---|
| Below auto-approve | Approved automatically, no review needed |
| Between auto-approve and quarantine | Goes to the Review queue for manual decision |
| Between quarantine and auto-reject | Video moved to quarantine folder |
| Above auto-reject | Video deleted immediately |

Four built-in profiles are available in Config under NSFW Detection. Custom mode allows free-form threshold editing.

---

## Tab Overview

- **Review:** New files awaiting your decision, with higher-risk items sorted to the top
- **Approved:** Confirmed clean files with their contact sheets
- **Quarantine:** Files pending your decision; Restore returns the video to its original location, Delete removes it permanently
- **Rejected:** Audit log of permanently removed files, with contact sheets available for review

---

## docker-compose.yml

```yaml
services:
  safescanarr:
    build: .
    container_name: safescanarr
    restart: unless-stopped
    ports:
      - "8686:8686"
    volumes:
      - ./data:/opt/safescanarr/data
      - /your/media/path:/mnt/media
    environment:
      - BASE_DIR=/opt/safescanarr/data
      - WEB_PORT=8686
```

Media folders must not be mounted read-only for quarantine and delete features to work.

---

## Updating

```bash
git pull
docker compose up -d --build
```

The database migrates automatically on startup. No manual steps required.

---

## Credits

NSFW detection powered by [NudeNet](https://github.com/notAI-tech/NudeNet), an open source nudity detection library.

---

## License

Copyright (c) 2026 jckrbbt. All rights reserved.

This source code is made available for personal reference only. No use, copying, modification, distribution, or deployment of this code is permitted without explicit written permission from the author.
