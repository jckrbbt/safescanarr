# Safe Scanarr Changelog

## 1.0.6 (UI refinement)

### Added
- Modern surface system with layered backgrounds, glass panels, depth shadows, and gradient accents
- Mobile bottom navigation bar with live state badges
- Skeleton loading grid with shimmer animation
- Mobile modal bottom sheets, lightbox swipe gestures
- View-transition-aware page navigation
- Search-context chip showing origin tab
- Hero metric and distribution bars on stats dashboard
- Empty-state rendering for logs and all tabs

### Changed
- Unified responsive breakpoint at 900px; removed unused CSS breakpoint custom properties
- Risk-low tier now uses the accent colour instead of green
- Mobile touch targets raised to 44px; inputs use 16px font to prevent iOS zoom
- Card titles truncate, selected cards get visible focus ring, flagged cards softened
- Search field uses CSS icon instead of emoji placeholder
- Login/setup pages polished with safe-area support and larger tap targets
- Config form widened; input rows flex correctly on all widths

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
