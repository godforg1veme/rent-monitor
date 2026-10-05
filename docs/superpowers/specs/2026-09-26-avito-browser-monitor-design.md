> Исторический документ. С 6 октября 2026 актуален домашний маршрут: [текущее состояние](../../home-route-status.md).

# Avito browser monitor design

## Objective

Make Avito the primary fast source for Rent Monitor while preserving the reliable SQLite,
deduplication, Telegram outbox, and baseline behavior already present in the project. The active
Avito search should normally be checked once per minute, recover automatically from temporary
failures, stop safely for CAPTCHA, and let the owner complete a CAPTCHA from an iPhone.

The first release targets the existing German VPS. The Netherlands VPS and a possible Russian VPS
are manual fallback deployment targets, not concurrent pollers or automatic IP rotation. Yandex
continues to run with its existing HTTP collector. Cian and Domclick remain in the repository for
later work but are inactive in the first release.

## Non-goals

- No CAPTCHA solving, stealth plugin, fingerprint spoofing, proxy rotation, or account login.
- No local LLM in the notification-critical path.
- No Cian or Domclick implementation work.
- No separate browser microservice or inter-process protocol in the first release.
- No storage of raw Avito HTML, listing descriptions, seller profiles, phone numbers, or contacts.
- No simultaneous polling of the same Avito search from multiple VPS hosts.

## Chosen approach

Run one Rent Monitor systemd service on the existing German VPS. The service owns SQLite, Telegram,
the existing HTTP transport, and a Playwright-managed Chromium process. Avito uses a dedicated,
persistent, unauthenticated Chromium profile. Yandex continues to use HTTP. Each source has an
independent runner, so browser failure or Avito backoff does not delay Yandex, Telegram, or outbox
delivery.

Chromium runs headed in a private virtual display. The display is normally inaccessible. It is
made temporarily available through noVNC over Tailscale only when the owner needs to complete a
CAPTCHA. The browser profile, cookies, local storage, source state, and queues survive service
restart.

This approach is preferred over an HTTP header imitation because the current direct HTTP client was
blocked from the VPS while an ordinary browser on that VPS has been usable. It is preferred over a
separate browser service because one browser source does not yet justify an IPC protocol, a second
deployment lifecycle, and another failure boundary.

## Components and responsibilities

### Source runner

One `SourceRunner` instance owns the schedule and health state for one source. It:

- schedules that source independently;
- prevents overlapping attempts for the same source;
- applies jitter, cooldown, and backoff;
- persists state before waiting;
- emits source transition events;
- passes successful `CollectionResult` values to the existing processing pipeline.

The initial Avito interval is 60 seconds with small jitter so requests do not always occur at an
exact wall-clock boundary. The next attempt is scheduled after the previous attempt completes.
Yandex keeps its existing five-minute interval.

### Browser transport

`BrowserTransport` owns Playwright, one Chromium process, and one persistent context dedicated to
Avito. It returns an ephemeral page result containing the final URL, navigation result, rendered
HTML or a minimal page snapshot, observation time, and a closed diagnostic category. Page content
is not written to SQLite or logs.

The browser uses ordinary Chromium defaults. The implementation does not override the user agent,
install stealth scripts, alter browser fingerprints, authenticate to Avito, or automatically
interact with a challenge. Browser crashes are caught as transport failures. The transport may
restart Chromium after backoff while retaining the persistent profile.

### Avito collector and extractor

The collector validates the configured search, asks the browser transport to load it, detects
access restrictions or CAPTCHA, and converts the recognized result to the existing normalized
listing model.

Extraction prefers structured page data and stable DOM markers, then cross-checks visible cards in
the primary result list. When both representations exist, listing identifiers and important fields
must agree. A material mismatch is an unrecognized structure rather than an empty search.

The extractor must distinguish primary search results from recommendations, promoted adjacent
content, and nearby-region results. It extracts only the fields required for notification:

- Avito listing ID and canonical URL;
- price;
- room count;
- title, area, address, and metro when available;
- commission evidence;
- publication time when Avito exposes a reliable value.

### Search jobs

Avito searches are configuration entries rather than Python constants. The first release activates
one job, but the configuration and scheduling model support additional jobs later:

```toml
[sources.avito]
enabled = true

[[sources.avito.searches]]
name = "moscow-rent"
url = "https://www.avito.ru/moskva/kvartiry/sdam/na_dlitelnyy_srok/bez_komissii-ASgBAgICA0SSA8gQ8AeQUp74DgI?s=104"
poll_interval_seconds = 60
```

The loader accepts only HTTPS Avito URLs without embedded credentials. At runtime the collector
also verifies that the final page is an Avito rental search for the intended region and that the
expected filters are active. All jobs for the Avito domain share one browser context and run
sequentially. Adding jobs therefore increases the domain request budget predictably rather than
creating concurrent browser sessions.

### Existing core pipeline

The current path remains authoritative:

```text
CollectionResult -> filters -> baseline/deduplication -> SQLite -> Telegram outbox
```

The existing listing outbox is retained. Source health alerts use a separate additive durable table
so the production listing schema does not require a destructive migration. The same delivery worker
retries both kinds of notification.

## Filter evidence

Each extracted required value has evidence classified as one of:

- `explicit_card`: displayed on the result card;
- `structured_page`: present in page data associated with that card;
- `verified_filter`: guaranteed by a filter that the collector verified as active on the loaded
  search page;
- `detail_page`: obtained from a public listing detail page;
- `unknown`: not established reliably.

Commission is evaluated as follows:

1. Explicit positive commission rejects the listing.
2. Explicit zero commission accepts it.
3. Missing card-level commission may be treated as zero only when the collector verified the active
   no-commission filter and confirmed that the card belongs to the primary result list.
4. Missing verification leaves commission unknown and the existing fail-closed filter rejects it.
5. Any contradiction between an active filter and explicit card data rejects the listing and
   increments a diagnostic counter.

This removes the likely false negative caused by requiring every filtered card to repeat the words
"без комиссии", without trusting a substring in the configured URL.

Publication time is used only when it is reliable. Machine-readable timestamps are preferred.
Relative visible values may be resolved against `observed_at` and marked approximate. Otherwise
`published_at` remains null. SQLite already records authoritative `first_seen_at` and
`delivered_at`; these are exposed as operational latency metrics rather than replaced.

## Source health state machine

Persisted states are:

- `starting`;
- `healthy`;
- `degraded`;
- `cooldown`;
- `blocked`;
- `manual_attention`.

Persisted state includes consecutive failure count, last failure code, last attempt, last success,
next attempt, last transition time, and outage start time. Restart does not discard cooldown or cause
an immediate burst of retries.

Behavior by result:

| Result | Action |
| --- | --- |
| Successful recognized search | Reset failure counters and return to the 60-second schedule. |
| Single timeout, DNS error, or Chromium crash | Mark degraded, restart the browser if required, retry after 30 seconds. |
| Repeated transport errors | Back off through 2, 5, 15, 30, then at most 60 minutes. |
| HTTP 429 | Honor `Retry-After`; otherwise use 5, 15, 30, then 60 minutes. |
| First 401/403 | Enter cooldown for 15 minutes. |
| Second consecutive 401/403 | Cool down for 60 minutes. |
| Third consecutive 401/403 | Mark blocked and make one control attempt every six hours. |
| Unrecognized structure | Retry after five minutes; block after three consecutive failures. |
| Visible CAPTCHA or human verification | Enter `manual_attention` and stop automatic Avito navigation. |

A successful control attempt restores the normal schedule. A CAPTCHA never triggers an automatic
retry; only manual completion in the same browser session can resume the source.

## CAPTCHA workflow

Chromium runs headed inside a virtual display so the existing page can be controlled without moving
cookies or challenge state to another browser.

When CAPTCHA is detected:

1. Stop all scheduled Avito requests and persist `manual_attention`.
2. Enqueue one durable Telegram alert with the last success time and an optional current screenshot.
3. Offer `Пройти CAPTCHA` and `Проверить состояние` buttons to the bound owner chat.
4. `Пройти CAPTCHA` creates a single-use, 15-minute access token and returns a Tailscale-only HTTPS
   URL for noVNC.
5. The owner opens the link on an iPhone connected to the same tailnet and completes the challenge
   manually in the existing Chromium window.
6. The service verifies that the challenge disappeared, the expected search loaded, and its result
   structure is recognized.
7. On success, close the control session, return Avito to healthy operation, and enqueue one recovery
   alert containing outage duration.

noVNC and its WebSocket proxy listen only on loopback. Tailscale Serve makes the endpoint available
only inside the tailnet. There is no public VNC port. Only one control session is allowed. Clipboard
and file transfer are disabled. An expired or unsuccessful session leaves Avito in
`manual_attention` and may issue a new link without resuming automated requests.

## Alerts and status

Alerts are transition-based to avoid noise. They are created for:

- a sustained transition from healthy to degraded or cooldown;
- any transition to blocked or manual attention;
- recovery from an unhealthy state.

A single transient timeout does not notify the owner. An alert is warranted after a repeated error
or after two expected checks have been missed. Each persisted transition has a unique event key so a
restart cannot duplicate it.

`/status` shows current state, last successful poll, next scheduled attempt, current interval,
consecutive failures, last recognized card count, and whether manual CAPTCHA action is required.

## LLM policy and future searches

A local LLM is intentionally omitted from the first release because it cannot improve poll speed,
CAPTCHA handling, listing identity, or the reliability of standard Avito filters. It would add
latency and a new failure mode to the notification path.

A later optional enrichment stage may use a local model for free-text conditions such as pets,
furniture, renovation quality, utilities, or deposit terms. Enrichment runs after immediate listing
discovery, consumes only the necessary sanitized text, is marked `llm_derived`, and cannot alter
source ID, URL, price, room count, or deterministic commission evidence. Failure of the model never
delays the base notification.

The reusable technology is the combination of persistent per-domain browser sessions,
configuration-defined search jobs, deterministic extraction, evidence-aware filtering, independent
source runners, and durable delivery. It is not tied to the current apartment URL.

## Deployment and resources

The first deployment remains on the existing German VPS. The service receives enough headroom for
Chromium, virtual display, and manual control:

- `MemoryMax=4G`;
- `CPUQuota=200%`;
- `TasksMax=512`.

The exact limits may be lowered after measurements, but initial rollout favors stable browser
operation over premature resource restriction. The Chromium profile is stored below the protected
Rent Monitor state directory. The systemd sandbox remains enabled and grants write access only to
that state directory.

Playwright/Chromium, the virtual display, noVNC, and Tailscale are explicit deployment dependencies.
The previous release remains available for atomic rollback. The Netherlands and Russian VPS options
are documented manual fallback targets. Moving to one of them is an operator decision; automatic IP
rotation and concurrent duplicate polling are outside the design.

## Testing

Automated coverage includes:

- configuration validation for one and multiple Avito search jobs;
- state transitions, persisted cooldowns, restart behavior, and backoff caps;
- explicit, positive, unknown, contradictory, and verified-filter commission cases;
- recognition of primary results versus recommendations and nearby locations;
- local Playwright fixtures for a normal page, empty page, CAPTCHA, access restriction, and unknown
  markup;
- the full browser snapshot to filter to SQLite to fake-Telegram path;
- baseline behavior and duplicate suppression;
- durable source alert retry after Telegram failure;
- Chromium crash recovery without stopping Yandex or Telegram;
- no automatic navigation while in `manual_attention`.

Live verification runs from the German VPS after fixture tests pass. It confirms successful browser
navigation, recognized search context, card count, newest visible ID, profile persistence across
restart, and an end-to-end manual CAPTCHA session from the owner's iPhone. Live checks are deployment
verification, not ordinary CI, and do not add a second high-frequency request loop.

## Rollout

1. Add the independent source runner, additive state migration, and transition alerts while retaining
   the existing HTTP behavior.
2. Add Playwright transport, persistent profile, and local fixture coverage.
3. Move the Avito URL to a search-job configuration and switch Avito to BrowserTransport.
4. Add the virtual display and Tailscale-only CAPTCHA control path.
5. Deploy to the German VPS, preserve the existing database and Telegram binding, and create a fresh
   Avito browser profile.
6. Perform a live smoke check, verify the active search filters, and seed or preserve the existing
   Avito baseline without sending old listings.
7. Enable the 60-second schedule and observe navigation success, card counts, detection latency,
   browser memory, and source transitions.
8. Keep the previous release ready for rollback until the browser collector has completed at least
   72 hours of stable operation.

## Acceptance criteria

- Avito is normally checked approximately once per minute and is independent of the Yandex schedule.
- A new matching listing reaches the existing durable Telegram outbox during the same poll cycle.
- Existing source IDs, cross-source duplicate groups, baseline state, and pending notifications remain
  valid after migration.
- A single 403 cannot permanently disable the source.
- Repeated access restrictions back off without generating a request storm.
- CAPTCHA stops all Avito polling and can be completed manually from the owner's iPhone through a
  Tailscale-only session.
- Successful manual completion resumes polling automatically and produces one recovery alert.
- Chromium failure does not stop Yandex, Telegram polling, or outbox delivery.
- Restart preserves source cooldown, browser profile, baseline, and pending alerts.
- Adding another Avito search requires configuration rather than a new collector implementation.
- No LLM, CAPTCHA automation, stealth tooling, public remote-desktop port, or automatic IP rotation is
  introduced.
