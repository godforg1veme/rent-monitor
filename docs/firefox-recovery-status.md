# Firefox recovery — 2026-09-30

The existing bot runs autonomously on Germany (`jarvis-vps`), using installed Mozilla
Firefox 157, a persistent VPS-local profile and direct access. No PC process, Windows cookies
or proxy is required. Netherlands polling is stopped and disabled; never start both bots.

The latest Netherlands SQLite database and owner binding were transferred consistently.
Private pre-handoff backups and the previous German database are preserved. Production release:
`/opt/rent-monitor/releases/20260930-firefox-recovery`. The installed systemd browser drop-in
uses `deploy/firefox-direct-backend.conf`.

Verified: three minute-spaced smoke polls returned HTTP 200 with 51–52 cards, followed by
successful scheduled production polls with 52 cards and successful collection after restart.
The subsequent poll returned `access_restricted`: current Avito state is cooldown with one
failure and a 15-minute backoff. Stable continuous collection is NOT established. Initial
baseline completed without treating old listings as new. A clearly labelled existing matching
listing was delivered to Telegram without modifying the production outbox. The phone endpoint
`http://100.97.66.10:10001` returned HTTP 200 and a token-authenticated VNC handshake.
`/avito` generates a temporary browser link; `/status` reports source health.

Current listing routes now parse correctly. City, apartment category, HTTPS origin, listing ID
and visible search scope are validated. Street/house and metro parse separately. Explicit or
unknown commission fails closed. No descriptions or contact information are stored.

Pending HTTP 439 security checks are no longer mistaken for a finished blocked page. Firefox
may finish the site's ordinary JavaScript check within the navigation timeout. An unresolved
check pauses collection for manual attention. Actual CAPTCHA is never solved automatically;
real IP restriction retains backoff. Driver work is serialized off the Telegram event loop.
BiDi tracks navigation status. Optional proxy auth answers only HTTP 407, not origin HTTP 401,
and is not enabled in production. Secrets remain outside source and diagnostics.

Avito interval is 60 seconds plus jitter; Yandex remains about 300 seconds. Cian and Domclick
stay disabled. New matching listings use the persistent deduplicated outbox. Short successful
checks do not guarantee permanent access. A new live phone CAPTCHA was not encountered.

For rollback, stop Germany before starting any alternative bot. Preserve the current database,
original Chromium profiles and backups. Revert the Firefox drop-in/release only with systemd
reload and restart; do not blindly replace the database with an older backup that loses state.

References: [Mozilla Linux installation](https://support.mozilla.org/en-US/kb/install-firefox-linux),
[Selenium BiDi network](https://www.selenium.dev/documentation/webdriver/bidi/network/).
