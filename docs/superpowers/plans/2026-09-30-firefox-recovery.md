> Исторический документ. С 6 октября 2026 актуален домашний маршрут: [текущее состояние](../../home-route-status.md).

# Firefox recovery implementation plan

**Goal:** Restore actual Avito discovery and delivery through the existing bot.

**Architecture:** Retain the one-process SQLite/Telegram pipeline on Germany with the latest
Netherlands database. Firefox uses direct access and a VPS-local profile, without a PC.
Chromium remains a rollback option. No CAPTCHA automation or parallel pollers.

**Spec:** ../specs/2026-09-26-avito-browser-monitor-design.md; user-approved continuation:
isolated Firefox profile, existing proxy, existing bot, manual CAPTCHA from phone.

## Tasks (sequential, as requested)

- [x] Reproduce live DOM extraction failure without persisting raw page HTML or descriptions.
- [x] Add current `/moskva/kvartiry/<slug>_<id>` route and visible H1 search-scope tests in
  `tests/e2e/test_avito_pipeline.py`. Run failing tests, correct parser, re-run Avito tests.
- [x] Add `BrowserTransport` protocol and optional Selenium Firefox transport. Serialize all
  driver commands off the async event loop, preserve HTTP navigation status from BiDi,
  answer only proxy 407 authentication (never origin 401), bound page size, keep CAPTCHA manual.
  Test status tracking, proxy-auth isolation, command serialization, safe URL validation.
- [x] Configure opt-in backend through environment; load proxy secret using systemd credential.
  Install official Mozilla Firefox and locked Selenium on the existing Netherlands service.
  Copy only Avito cookies, not Firefox saved passwords, extensions, or unrelated browsing data.
  The release and drop-in are staged; the active backend is deliberately unchanged after HTTP 403.
- [x] Perform three VPS smoke polls before enabling continuous collection. Preserve the existing
  Telegram owner binding and baseline, verify two successful scheduled polls and restart
  persistence, send a clearly labelled real matching-listing delivery test if available.
- [x] Run Ruff and unit, integration and e2e unittest discovery (63 tests), document live result
  and rollback in `docs/firefox-recovery-status.md`.

## Rollback

Keep both original Chromium state and Windows Firefox profile intact. Switching
`RENT_MONITOR_BROWSER_BACKEND` back to `chromium` restores the previous transport. Do not run
the German bot simultaneously. Failed Firefox smoke checks leave the existing bot enabled
and Avito in paused/backoff state, without increasing request frequency.
