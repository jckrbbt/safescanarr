# Safe Scanarr Changelog

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
