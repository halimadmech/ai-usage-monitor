# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project aims to follow [Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-07-01

### Added
- **Live Codex usage limits.** The Codex Session/Weekly bars are now read live from
  the official `codex app-server` (`account/rateLimits/read`) — the same account
  read the Codex desktop app uses. It is an account-status call, **not** a model
  request, so it uses no quota and updates without you having to run Codex. If the
  Codex CLI isn't installed, it falls back to the most recent snapshot in the local
  logs, labelled with its age.

### Fixed
- Codex bars could sit **frozen on a weeks-old value**. Codex changed its session-log
  format so `rate_limits` no longer sat where the parser looked; the app silently
  kept the last snapshot it could still read. The parser now finds `rate_limits`
  across log-schema versions (and the live read above sidesteps the problem entirely).
- Codex reset countdowns read a field that doesn't exist in the logs
  (`resets_in_seconds`); they now use Codex's real absolute `resets_at`, and hide the
  countdown instead of showing a misleading "0m" when a snapshot is stale.

## [1.0.0] - 2026-06-29

### Added
- Initial public release. A compact Windows widget showing **Claude** and
  **Codex (ChatGPT)** usage as percentage bars plus tokens used today / yesterday /
  last 30 days. **Usage only — no cost figures.**
- One-click **Connect Claude** sign-in (drives the official `claude auth login`, no
  terminal needed) and a **Sign out** button.
- Fixed, non-resizable **widget** sizing (~⅔ of screen height) that scales to fit and
  never scrolls.
- **Codex plan** badge (e.g. "ChatGPT Plus"), read from the local Codex login file.
- Beginner-friendly README with before/after screenshots and a downloadable `.exe`.

[1.1.0]: https://github.com/halimadmech/ai-usage-monitor/releases/tag/v1.1.0
[1.0.0]: https://github.com/halimadmech/ai-usage-monitor/releases/tag/v1.0.0
