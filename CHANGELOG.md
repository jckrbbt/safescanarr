# Safe Scanarr Changelog

## 1.0.11

### Added
- "Remember me" checkbox on the login page: a 30-day, absolute-expiry session backed by a server-side revocable token (only its SHA-256 hash is stored). Controlled by `remember_me_days` in `config.json` (config-file only, requires a restart; `0` disables the feature).
- "Sign out other devices" button on the Config page (`POST /api/auth/revoke-sessions`): signs out every other browser and device, including remembered ones, while keeping the current device signed in.
- `authtool revoke-sessions` CLI command to sign out all web sessions from the shell.
- `authtool fix-lock` CLI command to repair a `config.json.lock` that fails validation (wrong owner/mode/symlink): it removes and recreates the lock file with mode 0600, revalidating the inode identity immediately before the unlink (twice: by path, then again through a directory fd) so a replacement detected BEFORE the last comparison is refused instead of removed; an actively held invalid lock is refused outright. This is an explicit maintenance operation: stop the web service and all other config writers before running it, and do not run two repairs concurrently — with writers stopped the inode revalidation makes a lone repair safe, but a fully uncoordinated replacement landing between the last stat and the unlink is not detected (no atomic unlink-if-inode promise is made), and concurrent uncoordinated repairs are not supported. Lock validation itself is never silently weakened.
- `SS_COOKIE_SECURE=1|true|yes` environment variable to mark the session cookie `Secure` when serving over HTTPS.

### Fixed
- Sessions now end on every device when the password or token changes, and after an auth reset + re-setup, including when the new credential text equals the old one (credential transitions advance a monotonic `session_epoch` generation and revoke remembered rows).
- Config saves, credential changes, and session revocation are serialized by a cross-process config lock (`config.json.lock`): the config API, startup session-secret generation, first-run default-key self-heal on load, first-run bootstrap, and `config.save()` calls all take the lock before writing, and locked mutations apply their changes to the freshly-read state under the lock — automatic default-key detection uses setdefault semantics on that fresh state, so a concurrent writer that supplied the key first keeps its value and a completed update can no longer be lost. The low-level `config.save()` function itself remains a whole-snapshot replacement (with a monotonic `session_epoch` clamp) for its documented callers; merged writes are what `config_lock.mutate()` provides. First-run bootstrap re-checks config.json's absence under the lock and, on losing the race, returns the winner's persisted config, so it can never overwrite a config another writer has created; if the winner exists but cannot be read back (I/O error, malformed JSON) the read error propagates — auth then treats the config as unavailable and fails closed — instead of silently returning never-persisted first-run defaults. The lock file is opened with `O_NOFOLLOW`, validated for owner/mode, and its inode re-verified after `flock`; a substituted or tampered lock aborts the write with an explicit error (API: JSON 500; `authtool`: clean message and exit 1) rather than a traceback. `BASE_DIR` must not be writable by untrusted local users — the lock is only as trustworthy as that directory. A read-only `BASE_DIR` with an older config is served with new default keys merged in memory (auth keeps working); persistent writes resume once the directory is writable again.
- Login verification is coupled to the auth generation it verified against: a credential rotated away mid-login can no longer mint a session stamped with the new credential's fingerprint.
- Restoring a database backup sanitizes the restore *before* the file is published; if sanitization fails the restore returns 500 and the live database is left untouched. The uploaded file's schema is treated as hostile and discarded entirely: no uploaded executable schema is published — supported application data is imported into a fresh database built from the application's own DDL, so uploaded triggers and views and all uploaded `web_sessions` content are dropped wholesale (a crafted `AFTER DELETE` trigger — including one with a quote-injected name — can no longer resurrect sanitized rows). The upload must be a supported application backup: all core tables (`files`, `errors`, `state`, `lifetime_stats`) must be present as ordinary tables — missing core tables, views, and virtual tables in their place are rejected instead of publishing an empty replacement, and uploaded views are never evaluated. Every imported value is validated before publication (types, NULLability, keys) so e.g. a BLOB stored in a TEXT column is rejected instead of later breaking the API, and the import streams in bounded batches under explicit row/value budgets. Recognized ephemeral process-state keys (`scan_pid`) are discarded at import instead of published — they describe the uploader's machine — and their consumers treat an unparseable stored value as not-running rather than erroring. Recognized persistent numeric cursors (`sonarr_last_id`, `radarr_last_id`) with an unparseable value are deliberately reset to absent at import (documented resynchronization semantics: the next poll re-examines the current history window — a bounded re-trigger of up to 50 recent imports per arr — and then stores a fresh cursor), and the poller parses the stored cursor defensively so a foreign value reaching the live state by another path degrades to a rescan instead of crashing the poll loop. The uploaded file must pass `PRAGMA integrity_check`, the rebuilt database is re-validated for integrity and required schema after the import, and the `web_sessions` row count is verified before publication. Each restore stages into a request-private file in the database directory, so concurrent restores cannot touch each other's staging or cleanup; leftover stage files from a crashed process are removed only at startup, and only when the owning pid is provably dead (never by age alone while the owner lives; unknown liveness preserves the file) and the file is a service-user-owned member of the stage family (`.tmp`, `.tmp.upload`, WAL/SHM/journal sidecars).
- In token mode, an unreadable or unparseable config fails closed (no session fingerprint is issued) instead of falling back to epoch 0.
- Sign-out reports failure (HTTP 500) and keeps the browser session when the remembered session row could not be deleted server-side, so a captured cookie cannot silently remain replayable.
- Startup no longer crashes after the ephemeral-secret fallback when the config is unreadable: `remember_me_days` read failures disable remember-me for the run instead of aborting import.
- The emergency fallback signing key for read-only installs is no longer a public constant; it is now an ephemeral random key (with "Remember me" disabled) instead of a value anyone could use to forge cookies.
- The session cookie was renamed from Flask's default `session` to `safescanarr_session`, fixing collisions with other Flask apps served on the same host.
- Restoring a database backup no longer lets remembered devices from the old database back in; the restoring operator stays signed in — and if the operator signs out (or is revoked) while the restore is in flight, that session is not resurrected: logout, remembered-row deletion (including re-login old-row removal, credential-transition revocation, and expiration cleanup), and the restore's final liveness recheck plus publication all coordinate on the same lock, so there is no gap between the check and the file going live in which a signed-out row can be revived. A failure at any step of publication — a checkpoint that errors or reports busy, leased writers that do not drain, or a failed final `os.replace` — aborts the restore and leaves the live database untouched and usable with its committed WAL data intact; the shared connection is tracked per generation, publication ownership is serialized so only one restore at a time runs the drain→checkpoint→replace sequence, new database handles are blocked for the whole publication window, and the file is replaced only after every lease on every still-active generation retired against the live file (including one left over by a previously timed-out attempt) has exited, and every production route now balances its database lease.
- The UI reports sign-out failures: when `/logout` returns an error while the session is still authenticated, the browser stays on the page, shows the server's message, and lets the user retry instead of silently redirecting to the login screen. (An already-expired or revoked session still redirects to `/login` as before — that is the expected expired-session behavior, not a swallowed sign-out failure.)

### Note
- Everyone is signed out once on upgrade to 1.0.11 (intended; same precedent as 1.0.5).

## 1.0.10

### Fixed
- Arr blocklisting never worked: the old flow called `GET /api/v3/episodefile` and `GET /api/v3/moviefile` without the required `seriesId`/`movieId` parameters, which current Sonarr/Radarr v4 reject with HTTP 400, and it tried to remove blocklist entries via `DELETE /api/v3/blacklist/{historyId}`, a route that does not exist in API v3. Replaced the entire flow with the correct `POST /api/v3/history/failed/{grabbedHistoryId}` endpoint using the grabbed record (eventType=1), followed by a mandatory read-back verification against the blocklist endpoint.
- Arr reject failures are no longer silent: the result of each arr service call is persisted in the database (`arr_result`), included in webhook payloads, and returned by the API reject endpoint with `warnings` so the UI can surface them.
- Added `arr_blocklist_on_reject` and `arr_search_after_reject` config toggles (default true) and wired them through `config.py`, the web config API, and the UI.

### Changed
- README arr feature line now describes the new behavior and includes a Limitations note about manually imported files, trimmed history, and duplicate content from other indexers.

## 1.0.9

### Fixed
- Scan no longer crashes when quarantining/rejecting a file on a cross-device, non-writable media mount. Undeletable sources are now copied into quarantine, marked as "source retained" in the database and UI, and the scan continues.
- Quarantine copies are now atomic (written to `.sspart` beside the destination and then `os.replace()`-d) and size-verified before the source is unlinked, preventing interrupted copies from becoming the only copy.
- Added a UI chip "Copied - source still in place" for retained items in the Quarantine tab and lightbox, plus a tab hint explaining that the source file is still in the library.
- Blacklist / re-search is now gated: it only fires when the arr library file has actually been removed (MOVED outcome or verified deletion). When the source is retained, the action is skipped and logged clearly.
- Per-file error isolation in `run_scan`: an unexpected failure while processing a single file records an error, inserts a DB row with `status='error'`, `review_state='pending'`, and `flagged=1`, and the scan continues. The next scan retries error rows.
- `run_scan` now cleans up `scan_pid` in a `try/finally` block and prints final counts including errors and retained copies.
- Manual API actions (approve/reject/quarantine) now route through `fileops.relocate` and surface clear 409 JSON errors instead of crashing with 500 when the underlying move fails.

## 1.0.8

### Fixed
- Discord webhooks failed with HTTP 403 because urllib's default `Python-urllib/3.x` User-Agent is blocked by Cloudflare in front of `discord.com`. All webhook POSTs now send an explicit `SafeScanarr/<version>` User-Agent.
- The UI "Test Webhook" button never received the v1.0.7 provider-specific payload fix and still sent the generic JSON body; it now routes through the same `build_payload`/`send` path used by scanner webhooks and returns the provider's response status/body in the error detail.
- Webhook failures now log the response body (truncated to ~300 characters) instead of a generic exception so operators can diagnose rejected deliveries without exposing the URL/token.

## 1.0.7

### Fixed
- Discord and Slack webhooks returned HTTP 400 because the payload was a generic JSON event body those services reject. Discord URLs now receive `content`/`embeds`, Slack URLs receive `text`; ntfy and Gotify keep the full event body.

## 1.0.6 (UI refinement)

### Added
- Modern surface system: layered surfaces with depth, glass topbar and bottom navigation, and restrained gradient accents
- Mobile bottom navigation bar with live per-tab badges and thumb-reachable tab switching
- Mobile bottom sheets for modals, folder browser, and the lightbox, with swipe navigation and swipe-to-close in the lightbox
- Typography scale with tabular numerals for counts and percentages, plus tighter mobile content density
- Stats dashboard: hero metric, state distribution bars, and detail panels (auto vs manual, review quality)
- Motion system: pop-in modals, sheet slide-up panels, view-transition tab changes, with `prefers-reduced-motion` respected
- Search-context chip showing the origin tab during searches
- Skeleton loading grid of card-shaped shimmers replacing the full-width loading slab
- Empty-state rendering for logs and all tabs

### Fixed
- Lightbox panel was off-screen on desktop and unusable on mobile; it is now a two-pane layout on desktop and a bottom sheet with reachable actions on phones
- Mobile topbar overflowed horizontally at small widths; controls now fit at 320 to 900px
- Config page checkbox and toggle labels were stuck in all caps; they now render sentence case while section eyebrow labels stay uppercase
- Selected cards were invisible on flagged items; selection now shows a visible accent outline
- Card titles hid up to half of long filenames; titles now truncate with an ellipsis
- Inputs used 13 to 14px fonts, triggering iOS Safari zoom on focus; mobile inputs are 16px
- Viewport correctness: safe-area insets, dvh heights, overscroll behavior, and tap-highlight cleanup

### Changed
- Mobile touch targets raised to a 44px minimum
- Unified responsive breakpoints at 900px and 600px; removed invalid breakpoint custom properties
- Risk-low tier now uses the accent colour instead of approval green
- Help shortcut `?` now opens the shortcut sheet (Shift+/ previously never triggered it)
- Login/setup pages polished with safe-area support and larger tap targets
- Config form widened; input rows flex correctly on all widths
- Search field icon drawn in CSS instead of an emoji placeholder

## 1.0.5

### Added
- First-run setup screen (`/setup`) with password or security token method
- Method-aware auth module supporting password and token modes
- Per-IP login throttling (10 failures → lockout with exponential backoff)
- `session_secret` independent of auth token for Flask session integrity
- `web/authtool.py` CLI: `status`, `reset`, `set-password`, `set-token --show`
- UI loading, empty, and error state system
- Global keyboard shortcuts (`/`, `1-4`, `Esc`, `?`)
- Risk badge severity color scale (low / medium / high)
- Focus-visible rings and `prefers-reduced-motion` support
- Lightbox in-place updates, focus trap, close button, and neighbour preload
- Sticky bulk-action bar and unified toolbar ordering
- Sticky config save bar with dirty-state tracking

### Changed
- Session secret is independent of auth token, so existing sessions are invalidated once on upgrade
- Default web port changed from 8686 to 8666
- Config API no longer reads or writes `auth_token`, `auth_password_hash`, or `session_secret`
- README expanded with Authentication and recovery sections

### Security
- First-run setup is race-safe: the configuration check and write are atomic, so concurrent `/setup` requests cannot each claim the credential (only the first wins; the rest get 409)
- A pre-verify rate limit (~1 request/second per IP) blunts CPU exhaustion via repeated password hashing before the expensive verify runs
- Token-mode setup stores exactly the token generated in the browser and shown to the user
- Config API strips auth/session secrets
- Login throttling defends against brute-force attempts
- Cross-origin and CSRF guards remain enforced for all state-changing routes
