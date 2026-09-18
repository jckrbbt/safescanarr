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
- Zone-based handling: auto-approve clean content, flag ambiguous content for review, quarantine or reject high-risk content (rejects are quarantined by default; permanent deletion is an separate opt-in)
- Four-tab UI: Review, Approved, Quarantine, Rejected
- Quarantine: suspicious files are moved rather than deleted immediately, giving you a chance to review before permanently removing
- Lightbox viewer with keyboard navigation and per-item actions, presented as a swipeable bottom sheet on mobile
- Mobile-friendly UI: bottom navigation with live badges, bottom-sheet modals, 44px touch targets, and keyboard shortcuts
- Webhook notifications for quarantine and reject events (Discord, Slack, Ntfy, Gotify, Home Assistant, etc.)
- Arr integration: polls for new imports and blocklists rejected releases by marking the original grab as failed (requires `delete_on_reject`)
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

Open **http://yourserver:8666** and complete the first-run setup to choose a password or token.

> **Upgrade note for 1.0.5:** the default web port was changed from 8686 to 8666. If you are running bare-metal without `WEB_PORT` set, update your bookmarks.

---

## Authentication

Safe Scanarr supports two authentication methods, chosen during first-run setup:

* **Password**: recommended for browser use. The password is hashed with `pbkdf2:sha256` and stored in `data/config.json`. It cannot be recovered, only reset.
* **Security token**: recommended for scripts, API clients, and Docker. During setup the token is generated in your browser (never sent from the server) and displayed once with a copy button; it is stored exactly as shown, so what you save is what works. Use it via `X-Auth-Token: <token>` or `Authorization: Bearer <token>` on API calls.

### Environment overrides

| Variable | Description |
|---|---|
| `SS_TOKEN` | Shared token; forces token mode and overrides any stored token |
| `SS_PASSWORD_HASH` | Pre-set password hash for declarative deployments |

### Recovery

If you lose access, run as the service user:

```bash
python3 -m web.authtool status           # show current auth method and state (no secrets)
python3 -m web.authtool reset            # return to first-run setup
python3 -m web.authtool set-password     # prompts for a new password
python3 -m web.authtool set-token --show # set a new token and print it
```

### Login throttling

Failed login and setup attempts are throttled per IP address: 10 failures trigger a lockout with exponential backoff (1 minute doubling, capped at 1 hour). The lockout is keyed on the connecting IP as the server sees it. If multiple users access Safe Scanarr behind the same reverse proxy or NAT gateway, they all share one IP, so one person's failed attempts can lock everyone else out. Run Safe Scanarr directly on your LAN, or give each site a dedicated proxy, if that is a concern.

**Note:** upgrading to 1.0.5 introduces an independent `session_secret`, which invalidates existing sessions once. You will be asked to sign in again.

---

## Configuration

Only two environment variables are required in `docker-compose.yml`:

| Variable | Default | Description |
|---|---|---|
| `BASE_DIR` | `/opt/safescanarr/data` | Where config, database, logs, and contact sheets are stored |
| `WEB_PORT` | `8666` | Web UI port |

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
| Above auto-reject | Video rejected (quarantined by default; permanently deleted only if `delete_on_reject` is enabled) |

Four built-in profiles are available in Config under NSFW Detection. Custom mode allows free-form threshold editing.

---

## Tab Overview

- **Review:** New files awaiting your decision, with higher-risk items sorted to the top
- **Approved:** Confirmed clean files with their contact sheets
- **Quarantine:** Files pending your decision; Restore returns the video to its original location, Delete rejects it (quarantine by default; permanent delete only if `delete_on_reject` is enabled)
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
      - "8666:8666"
    volumes:
      - ./data:/opt/safescanarr/data
      - /your/media/path:/mnt/media
    environment:
      - BASE_DIR=/opt/safescanarr/data
      - WEB_PORT=8666
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

## Limitations

- Arr blocklisting requires `delete_on_reject` to be enabled and only works for releases that were grabbed (not manually imported) and whose grabbed history still exists in the arr.
- Manually imported files and releases whose history has been trimmed cannot be blocklisted automatically.
- A different indexer's copy of the same content can still be returned by an arr search; Safe Scanarr only blocks the specific rejected release.

---

## Credits

NSFW detection powered by [NudeNet](https://github.com/notAI-tech/NudeNet), an open source nudity detection library.

---

## License

Safe Scanarr is licensed under the GNU General Public License v3.0 or later
(GPL-3.0). See [LICENSE](./LICENSE) for the full license text.

NSFW detection is powered by [NudeNet](https://github.com/notAI-tech/NudeNet),
which is also licensed under GPL-3.0.
