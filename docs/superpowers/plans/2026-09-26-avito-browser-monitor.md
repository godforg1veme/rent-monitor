# Avito Browser Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Avito's permanently-pausing HTTP collector with a persistent Playwright/Chromium collector that polls once per minute, preserves the existing listing pipeline, reports durable health transitions, and lets the owner complete CAPTCHA from an iPhone over Tailscale/noVNC.

**Architecture:** Keep one Python application as the owner of SQLite, Telegram, HTTP collection, and the Playwright browser. Run one independent async runner per source; Avito uses a persistent headed Chromium context while Yandex keeps the bounded HTTP client. A small systemd support unit provides Xvfb, x11vnc, and loopback-only websockify/noVNC for manual CAPTCHA control.

**Tech Stack:** Python 3.11+, asyncio, Playwright Python/Chromium, HTTPX, aiosqlite/SQLite, aiogram 3, systemd, Xvfb, x11vnc, noVNC/websockify, Tailscale Serve, unittest, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-26-avito-browser-monitor-design.md`

## Global Constraints

- Poll the active Avito search every 60 seconds with up to 5 seconds of jitter; never overlap Avito navigations.
- Use a dedicated persistent Chromium profile without login, proxy rotation, stealth plugins, fingerprint overrides, or CAPTCHA automation.
- Stop all Avito navigation in `manual_attention` until the owner manually completes the challenge.
- Keep Yandex operational and independent; leave Cian and Domclick code intact but disabled in the production configuration.
- Preserve existing SQLite listings, baseline rows, duplicate groups, Telegram binding, and pending listing notifications.
- Store no raw HTML, listing description, seller profile, phone number, contact data, browser trace, HAR, or video.
- Keep raw page content in memory only; logs contain closed diagnostic codes and counts.
- Do not put an LLM in the discovery or notification path.
- Deploy first to the current German VPS; Netherlands and Russian VPS hosts remain manual alternatives.
- Allocate `MemoryMax=4G`, `CPUQuota=200%`, and `TasksMax=512` for the main service.

---

## File map

New focused modules:

- `src/rent_monitor/core/source_state.py` — pure persisted runtime-state and backoff policy.
- `src/rent_monitor/browser/__init__.py` — browser package exports.
- `src/rent_monitor/browser/transport.py` — Playwright lifecycle and ephemeral page snapshots.
- `src/rent_monitor/browser/captcha.py` — single-use noVNC token/session creation and expiry.
- `tests/unit/test_config.py` — TOML validation and search-job configuration.
- `tests/unit/test_source_state.py` — deterministic state transition policy.
- `tests/unit/test_captcha.py` — token file and URL lifecycle.
- `tests/integration/test_browser_transport.py` — real Chromium against a local HTTP fixture server.
- `deploy/run-captcha-stack.sh` — supervised Xvfb/x11vnc/websockify stack.
- `deploy/systemd/rent-monitor-captcha.service` — support unit for the private display.

Existing modules with bounded changes:

- `src/rent_monitor/config.py` — nested source/search and CAPTCHA configuration.
- `config/search.toml` — one active Avito search, Yandex active, Cian/Domclick inactive.
- `src/rent_monitor/core/models.py` — runtime states, evidence, browser-neutral alert models.
- `src/rent_monitor/core/scheduler.py` — independent source runners and dual outbox delivery.
- `src/rent_monitor/storage/sqlite.py` — additive runtime state, evidence column, and source-alert queue migrations.
- `src/rent_monitor/parsers/avito.py` — verified search context and commission provenance.
- `src/rent_monitor/collectors/avito.py` — browser-backed multi-job collection without permanent in-memory pause.
- `src/rent_monitor/collectors/__init__.py` — construct enabled non-browser collectors only.
- `src/rent_monitor/telegram/bot.py` — source alerts, richer `/status`, CAPTCHA callbacks.
- `src/rent_monitor/main.py` — initialize transports, runners, CAPTCHA manager, and task lifecycle.
- `deploy/install.sh` and `deploy/systemd/rent-monitor.service` — browser dependencies, environment, limits, and ordering.
- `.github/workflows/ci.yml` — one Chromium integration job in addition to the Python matrix.
- `README.md` and `deploy/README.md` — operation, Tailscale, CAPTCHA, rollback, and smoke verification.

---

### Task 1: Configuration and dependency foundation

**Files:**
- Modify: `src/rent_monitor/config.py`
- Modify: `config/search.toml`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `AvitoSearchConfig(name: str, url: str, poll_interval_seconds: int)`.
- Produces: `SourceConfig(name: str, enabled: bool, poll_interval_seconds: int, searches: tuple[AvitoSearchConfig, ...])`.
- Produces: `CaptchaConfig(enabled: bool, public_base_url: str | None, token_directory: Path)`.
- Produces: `RuntimeConfig.captcha: CaptchaConfig` and source-specific schedules used by Tasks 3, 4, 6, and 8.

- [ ] **Step 1: Write failing configuration tests**

```python
class ConfigTest(unittest.TestCase):
    def test_loads_one_avito_search_and_disables_future_sources(self) -> None:
        config = load_text_config(
            """
            [search]
            city = "Москва"
            rooms = 2
            max_monthly_price_rub = 70000
            require_no_commission = true

            [limits]
            max_response_bytes = 8388608

            [sources.avito]
            enabled = true

            [[sources.avito.searches]]
            name = "moscow-rent"
            url = "https://www.avito.ru/moskva/kvartiry/sdam/na_dlitelnyy_srok/bez_komissii-ASgBAgICA0SSA8gQ8AeQUp74DgI?s=104"
            poll_interval_seconds = 60

            [sources.yandex]
            enabled = true
            poll_interval_seconds = 300

            [sources.cian]
            enabled = false
            poll_interval_seconds = 300

            [sources.domclick]
            enabled = false
            poll_interval_seconds = 300

            [captcha]
            enabled = true
            token_directory = "/run/rent-monitor-captcha/tokens"

            [database]
            path = "data/test.sqlite3"
            """
        )
        avito = next(source for source in config.sources if source.name == "avito")
        self.assertEqual(avito.searches[0].name, "moscow-rent")
        self.assertEqual(avito.searches[0].poll_interval_seconds, 60)
        self.assertFalse(next(s for s in config.sources if s.name == "cian").enabled)

    def test_rejects_duplicate_job_names_and_sub_minute_polling(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_text_config(duplicate_job_toml(interval=59))

    def test_rejects_non_https_or_credentialed_avito_url(self) -> None:
        for url in ("http://www.avito.ru/moskva/kvartiry", "https://u:p@avito.ru/x"):
            with self.subTest(url=url), self.assertRaises(ConfigurationError):
                load_text_config(avito_toml(url=url))
```

Implement a private `_load_raw(raw: dict[str, object], base_path: Path)` helper so tests can parse TOML text without writing files; `load_config(path)` remains the public file entry point.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run --locked python -m unittest tests.unit.test_config -v`

Expected: import or attribute failures for the new nested source types.

- [ ] **Step 3: Implement nested source and CAPTCHA configuration**

Use these dataclasses and validation bounds:

```python
@dataclass(frozen=True, slots=True)
class AvitoSearchConfig:
    name: str
    url: str
    poll_interval_seconds: int

@dataclass(frozen=True, slots=True)
class SourceConfig:
    name: str
    enabled: bool
    poll_interval_seconds: int
    searches: tuple[AvitoSearchConfig, ...] = ()

@dataclass(frozen=True, slots=True)
class CaptchaConfig:
    enabled: bool
    public_base_url: str | None
    token_directory: Path
```

Require unique search names matching `[a-z0-9][a-z0-9_-]{0,47}`, HTTPS, host `avito.ru` or a subdomain, no username/password, and intervals from 60 through 3600 seconds. Read `RENT_MONITOR_CAPTCHA_BASE_URL` before the optional TOML `public_base_url`, so deployment can inject the discovered Tailscale URL.

Replace the flat `[sources]` production section with the exact nested structure exercised by the test. Set Cian and Domclick `enabled = false`.

- [ ] **Step 4: Add Playwright without installing browsers during package resolution**

Add `playwright>=1.55,<2` to project dependencies and run:

```powershell
uv lock
uv sync --locked
```

Browser binary installation remains a deployment/CI command (`playwright install chromium`), not a package import side effect.

- [ ] **Step 5: Run configuration tests and the existing suite**

Run:

```powershell
uv run --locked python -m unittest tests.unit.test_config -v
uv run --locked python -m unittest discover -s tests/e2e -v
uv run --locked ruff check .
```

Expected: all tests pass; existing source-building tests use the new nested configuration.

- [ ] **Step 6: Commit**

```powershell
git add src/rent_monitor/config.py config/search.toml pyproject.toml uv.lock tests/unit/test_config.py
git commit -m "feat(config): add Avito search jobs"
```

---

### Task 2: Persisted source runtime state and backoff policy

**Files:**
- Create: `src/rent_monitor/core/source_state.py`
- Create: `tests/unit/test_source_state.py`
- Modify: `src/rent_monitor/core/models.py`
- Modify: `src/rent_monitor/storage/sqlite.py`
- Modify: `tests/e2e/test_monitor_pipeline.py`

**Interfaces:**
- Produces: `SourceRunHealth` enum with `starting`, `healthy`, `degraded`, `cooldown`, `blocked`, `manual_attention`.
- Produces: `SourceRunState` dataclass persisted by `SQLiteRepository.get_source_run_state()` and `save_source_run_state()`.
- Produces: `transition_source_state(previous, outcome, now, normal_interval_seconds) -> SourceTransition`.
- Consumes later: Task 3 source runner and Task 7 alert generation.

- [ ] **Step 1: Write table-driven failing state-policy tests**

```python
class SourceStatePolicyTest(unittest.TestCase):
    def test_first_forbidden_enters_fifteen_minute_cooldown(self) -> None:
        now = datetime(2026, 9, 26, 12, tzinfo=UTC)
        transition = transition_source_state(
            SourceRunState.initial("avito"),
            SourceOutcome.failure("access_restricted"),
            now,
            normal_interval_seconds=60,
        )
        self.assertEqual(transition.current.health, SourceRunHealth.COOLDOWN)
        self.assertEqual(transition.current.consecutive_failures, 1)
        self.assertEqual(transition.current.next_attempt_at, now + timedelta(minutes=15))

    def test_third_forbidden_blocks_for_six_hours(self) -> None:
        state = SourceRunState(
            source="avito",
            health=SourceRunHealth.COOLDOWN,
            consecutive_failures=2,
            failure_code="access_restricted",
            last_attempt_at=None,
            last_success_at=None,
            next_attempt_at=None,
            transitioned_at=None,
            outage_started_at=None,
            last_card_count=None,
            last_newest_id=None,
        )
        transition = transition_source_state(
            state,
            SourceOutcome.failure("access_restricted"),
            datetime(2026, 9, 26, 12, tzinfo=UTC),
            normal_interval_seconds=60,
        )
        self.assertEqual(transition.current.health, SourceRunHealth.BLOCKED)
        self.assertEqual(
            transition.current.next_attempt_at,
            datetime(2026, 9, 26, 18, tzinfo=UTC),
        )

    def test_captcha_has_no_automatic_next_attempt(self) -> None:
        transition = transition_source_state(
            SourceRunState.initial("avito"),
            SourceOutcome.failure("captcha"),
            datetime(2026, 9, 26, 12, tzinfo=UTC),
            normal_interval_seconds=60,
        )
        self.assertEqual(transition.current.health, SourceRunHealth.MANUAL_ATTENTION)
        self.assertIsNone(transition.current.next_attempt_at)
```

Cover success recovery, first transport retry at 30 seconds, repeated transport delays `2/5/15/30/60` minutes, `Retry-After`, unrecognized structure at five minutes and block on the third occurrence.

- [ ] **Step 2: Run the policy test and verify failure**

Run: `uv run --locked python -m unittest tests.unit.test_source_state -v`

Expected: module import failure.

- [ ] **Step 3: Implement pure state types and transition policy**

Keep existing `SourceHealth` unchanged for collector compatibility. Add separate runtime types:

```python
class SourceRunHealth(StrEnum):
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    COOLDOWN = "cooldown"
    BLOCKED = "blocked"
    MANUAL_ATTENTION = "manual_attention"

@dataclass(frozen=True, slots=True)
class SourceOutcome:
    ok: bool
    failure_code: str | None = None
    retry_after_seconds: float | None = None
    card_count: int | None = None
    newest_id: str | None = None

    @classmethod
    def failure(
        cls, failure_code: str, retry_after_seconds: float | None = None
    ) -> SourceOutcome:
        return cls(False, failure_code, retry_after_seconds)

@dataclass(frozen=True, slots=True)
class SourceRunState:
    source: str
    health: SourceRunHealth
    consecutive_failures: int = 0
    failure_code: str | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    next_attempt_at: datetime | None = None
    transitioned_at: datetime | None = None
    outage_started_at: datetime | None = None
    last_card_count: int | None = None
    last_newest_id: str | None = None

    @classmethod
    def initial(cls, source: str) -> SourceRunState:
        return cls(source=source, health=SourceRunHealth.STARTING)

    @classmethod
    def manual_attention(cls, source: str, failure_code: str) -> SourceRunState:
        return cls(
            source=source,
            health=SourceRunHealth.MANUAL_ATTENTION,
            consecutive_failures=1,
            failure_code=failure_code,
        )

@dataclass(frozen=True, slots=True)
class SourceTransition:
    previous: SourceRunState
    current: SourceRunState

    @property
    def changed(self) -> bool:
        return self.previous.health is not self.current.health
```

Use only UTC-aware datetimes. Clamp externally supplied retry delay to six hours.

- [ ] **Step 4: Add an additive `source_runtime` table and repository methods**

Add this table without changing the existing checked `source_status` table:

```sql
CREATE TABLE IF NOT EXISTS source_runtime (
    source TEXT PRIMARY KEY,
    health TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    failure_code TEXT,
    last_attempt_at TEXT,
    last_success_at TEXT,
    next_attempt_at TEXT,
    transitioned_at TEXT,
    outage_started_at TEXT,
    last_card_count INTEGER,
    last_newest_id TEXT
);
```

Implement:

```python
async def get_source_run_state(self, source: str) -> SourceRunState
async def save_source_run_state(self, state: SourceRunState) -> None
async def list_source_run_states(self) -> list[SourceRunState]
```

When no row exists, `get_source_run_state` returns `SourceRunState.initial(source)` without writing.

- [ ] **Step 5: Test persistence across repository restart**

Use a temporary on-disk SQLite file, save a cooldown state, close/reopen the repository, and assert every timestamp and counter survives.

Run: `uv run --locked python -m unittest tests.unit.test_source_state tests.e2e.test_monitor_pipeline -v`

Expected: pass.

- [ ] **Step 6: Commit**

```powershell
git add src/rent_monitor/core/source_state.py src/rent_monitor/core/models.py src/rent_monitor/storage/sqlite.py tests/unit/test_source_state.py tests/e2e/test_monitor_pipeline.py
git commit -m "feat(core): persist source runtime state"
```

---

### Task 3: Independent per-source runners

**Files:**
- Modify: `src/rent_monitor/core/scheduler.py`
- Modify: `src/rent_monitor/main.py`
- Modify: `src/rent_monitor/collectors/__init__.py`
- Create: `tests/unit/test_source_runner.py`
- Modify: `tests/e2e/test_monitor_pipeline.py`

**Interfaces:**
- Consumes: Task 2 state policy and repository methods.
- Produces: `run_source_runner(collector, criteria, client, repository, stop_event, *, interval_seconds, jitter_seconds, state_changed, on_transition)`.
- Produces: `run_collectors(runtimes, criteria, repository, stop_event, state_changed, on_transition)` that starts one task per source.
- Retains: `process_collection_result()` and `run_outbox_worker()` behavior.

- [ ] **Step 1: Write failing concurrency and persisted-wait tests**

```python
class SourceRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_slow_source_does_not_delay_fast_source(self) -> None:
        slow = RecordingCollector("yandex", delay=0.20)
        fast = RecordingCollector("avito", delay=0.0)
        await run_collectors_for_test(
            [(slow, 300), (fast, 60)],
            duration=0.12,
            interval_scale=0.001,
        )
        self.assertGreaterEqual(fast.calls, 2)

    async def test_manual_attention_makes_no_network_call(self) -> None:
        repository = await repository_with_state(
            SourceRunState.manual_attention("avito", failure_code="captcha")
        )
        collector = RecordingCollector("avito")
        await run_one_runner_tick(collector, repository)
        self.assertEqual(collector.calls, 0)
```

Inject `clock`, `sleep`, and `jitter` callables into the runner as keyword-only test seams rather than using real minute-long waits.

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run --locked python -m unittest tests.unit.test_source_runner -v`

Expected: missing runner interfaces.

- [ ] **Step 3: Refactor scheduling without changing collection processing**

Define:

```python
@dataclass(frozen=True, slots=True)
class CollectorRuntime:
    collector: Collector
    client: object
    interval_seconds: int
    jitter_seconds: int = 5
```

Each runner loads persisted runtime state before an attempt, waits until `next_attempt_at`, calls only its collector, applies the pure transition, saves it, then invokes `on_transition` only when health changes. Global `/pause` continues to stop collection while Telegram/outbox tasks remain active.

Do not use the old slot calculation. Keep `BoundedHttpClient` serialization for HTTP collectors; the browser transport has its own lock.

- [ ] **Step 4: Wire independent tasks in `main.py`**

Build Yandex as an HTTP runtime. Leave Avito temporarily on its existing collector/client until Task 6 swaps the client. Do not construct disabled Cian or Domclick collectors.

Use one `asyncio.TaskGroup` child for each source runner, one for Telegram polling, and one for outbox delivery. A collector exception is converted to a failure outcome inside its runner and must not escape the task group.

- [ ] **Step 5: Run focused and full tests**

Run:

```powershell
uv run --locked python -m unittest tests.unit.test_source_runner -v
uv run --locked python -m unittest discover -s tests/e2e -v
uv run --locked ruff check .
```

Expected: pass, including an assertion that Avito and Yandex call timestamps are independent.

- [ ] **Step 6: Commit**

```powershell
git add src/rent_monitor/core/scheduler.py src/rent_monitor/main.py src/rent_monitor/collectors/__init__.py tests/unit/test_source_runner.py tests/e2e/test_monitor_pipeline.py
git commit -m "refactor(scheduler): run sources independently"
```

---

### Task 4: Persistent Playwright browser transport

**Files:**
- Create: `src/rent_monitor/browser/__init__.py`
- Create: `src/rent_monitor/browser/transport.py`
- Create: `tests/integration/test_browser_transport.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: `BrowserPage(status_code, final_url, html, observed_at, screenshot_png)`.
- Produces: `PlaywrightBrowserTransport(profile_path, *, headless=False, navigation_timeout_ms=30_000)`.
- Produces async methods: `start()`, `fetch(url) -> BrowserPage`, `screenshot() -> bytes | None`, `restart()`, and `aclose()`.
- Consumes later: Task 6 Avito collector and Task 8 CAPTCHA workflow.

Define the production snapshot as:

```python
@dataclass(frozen=True, slots=True)
class BrowserPage:
    status_code: int | None
    final_url: str
    html: str
    observed_at: datetime
    screenshot_png: bytes | None = None
```

Test modules define `ok_page(html, url=SEARCH_URL)` and `forbidden_page(url=SEARCH_URL)` helpers that
construct `BrowserPage` with deterministic UTC observation times. Production code remains
source-neutral.

- [ ] **Step 1: Write a failing real-browser integration test**

Start an `asyncio` loopback HTTP server that serves `/search` and records the `Cookie` header. The first response sets `rm_test=present`; the second renders the received cookie in the body.

```python
class BrowserTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_context_reuses_cookie_and_profile(self) -> None:
        async with local_fixture_server() as url:
            with TemporaryDirectory() as directory:
                transport = PlaywrightBrowserTransport(Path(directory), headless=True)
                await transport.start()
                first = await transport.fetch(url)
                second = await transport.fetch(url)
                await transport.aclose()
                self.assertEqual(first.status_code, 200)
                self.assertIn("rm_test=present", second.html)
```

Also assert that two concurrent `fetch` calls are serialized and that HTML is returned but no file is created outside the profile directory.

- [ ] **Step 2: Install Chromium locally and verify the test fails**

Run:

```powershell
uv run --locked playwright install chromium
uv run --locked python -m unittest tests.integration.test_browser_transport -v
```

Expected: module import failure.

- [ ] **Step 3: Implement the browser lifecycle**

Use Playwright's async API and dedicated profile:

```python
self._context = await self._playwright.chromium.launch_persistent_context(
    user_data_dir=str(self.profile_path),
    headless=self.headless,
    accept_downloads=False,
    viewport={"width": 1440, "height": 1000},
)
self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
```

Do not pass a custom user agent or stealth arguments. Set navigation timeout, use `wait_until="domcontentloaded"`, then wait for the page's main DOM to settle for at most five seconds. Validate HTTPS Avito URLs in production; expose a constructor-only `allowed_test_origins` set for loopback integration tests. Protect `fetch`, `restart`, and `aclose` with one async lock.

Never enable trace, video, HAR, or downloads. Capture a PNG only when `screenshot()` is explicitly called.

- [ ] **Step 4: Add one CI browser job**

Keep the existing Python-version matrix. Add a separate Ubuntu/Python 3.12 job that runs:

```yaml
- run: uv sync --locked
- run: uv run --locked playwright install --with-deps chromium
- run: uv run --locked python -m unittest tests.integration.test_browser_transport -v
```

- [ ] **Step 5: Run browser, E2E, lint, and format checks**

Run:

```powershell
uv run --locked python -m unittest tests.integration.test_browser_transport -v
uv run --locked python -m unittest discover -s tests/e2e -v
uv run --locked ruff check .
uv run --locked ruff format --check .
```

Expected: pass.

- [ ] **Step 6: Commit**

```powershell
git add src/rent_monitor/browser .github/workflows/ci.yml tests/integration/test_browser_transport.py
git commit -m "feat(browser): add persistent Playwright transport"
```

---

### Task 5: Verified Avito search context and commission evidence

**Files:**
- Modify: `src/rent_monitor/core/models.py`
- Modify: `src/rent_monitor/parsers/avito.py`
- Modify: `src/rent_monitor/storage/sqlite.py`
- Modify: `tests/e2e/test_avito_pipeline.py`

**Interfaces:**
- Produces: `FieldEvidence` enum: `explicit_card`, `structured_page`, `verified_filter`, `detail_page`, `unknown`.
- Extends: `Candidate` and `Listing` with `price_evidence`, `rooms_evidence`, and `commission_evidence`, each defaulting to `unknown`.
- Produces: `AvitoSearchContext(recognized, city, long_term, no_commission, newest_first)`.
- Extends: `parse_search_page(html, base_url, *, expected_city="Москва")` to return verified context with candidates.

- [ ] **Step 1: Add failing parser cases for filter-derived commission**

```python
def test_verified_no_commission_filter_supplies_missing_card_value(self) -> None:
    parsed = parse_search_page(
        search_page(
            card("5555555555", details="Залог 60 000 ₽ · ЖКУ включены"),
            heading="Аренда квартир на длительный срок в Москве без комиссии",
            selected_filters=("Без комиссии", "Сначала новые"),
        ),
        SEARCH_URL,
    )
    candidate = parsed.candidates[0]
    self.assertEqual(candidate.commission_status, CommissionStatus.NONE)
    self.assertEqual(candidate.commission_evidence, FieldEvidence.VERIFIED_FILTER)

def test_url_without_visible_filter_does_not_supply_commission(self) -> None:
    parsed = parse_search_page(
        search_page(card("5555555555", details="Залог 60 000 ₽"), heading="Квартиры"),
        SEARCH_URL,
    )
    self.assertEqual(parsed.candidates[0].commission_status, CommissionStatus.UNKNOWN)

def test_explicit_positive_commission_overrides_filter(self) -> None:
    parsed = parse_search_page(
        verified_search_page(card("5555555555", details="Комиссия 50%")),
        SEARCH_URL,
    )
    self.assertEqual(parsed.candidates[0].commission_status, CommissionStatus.POSITIVE)

def test_machine_timestamp_and_relative_timestamp_are_normalized(self) -> None:
    observed_at = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    exact = parse_search_page(
        verified_search_page(card("6666666666", published="2026-09-26T11:58:00+00:00")),
        SEARCH_URL,
        observed_at=observed_at,
    )
    relative = parse_search_page(
        verified_search_page(card("7777777777", published="5 минут назад")),
        SEARCH_URL,
        observed_at=observed_at,
    )
    self.assertEqual(exact.candidates[0].published_at, datetime(2026, 9, 26, 11, 58, tzinfo=UTC))
    self.assertEqual(relative.candidates[0].published_at, observed_at - timedelta(minutes=5))
```

Fixtures must mark the primary result container and selected filters separately; recommendation cards outside the primary container are ignored.

- [ ] **Step 2: Run the Avito tests and verify failure**

Run: `uv run --locked python -m unittest tests.e2e.test_avito_pipeline -v`

Expected: missing evidence/context types or wrong unknown commission result.

- [ ] **Step 3: Implement evidence-aware parsing**

Require all of these signals before `no_commission=True`:

- the validated Avito URL path contains the no-commission route segment;
- the visible page heading contains `без комиссии`;
- a selected/active filter element in the rendered DOM contains `без комиссии`;
- the card is inside the primary `catalog-serp` container.

Use explicit card commission first. Apply `verified_filter` only when card commission is unknown. Explicit positive data always wins.

Set `price_evidence` and `rooms_evidence` to `structured_page` or `explicit_card` according to the
input used. Extend `SearchPageParse` with an optional `context` field so other parsers remain
source-compatible. Parse ISO-8601 publication timestamps first; accept Russian relative minute/hour
labels only when `observed_at` is supplied, and leave all other publication labels unknown.

- [ ] **Step 4: Add an additive evidence column migration**

During repository initialization, inspect `PRAGMA table_info(listings)`. Add every absent evidence
column independently:

```sql
ALTER TABLE listings ADD COLUMN price_evidence TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE listings ADD COLUMN rooms_evidence TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE listings ADD COLUMN commission_evidence TEXT NOT NULL DEFAULT 'unknown';
```

Read/write the enum through `_listing_values` and `_listing_from_row`. Existing production rows become `unknown` without changing identity, baseline, or outbox rows.

- [ ] **Step 5: Run parser, pipeline, and migration tests**

Run:

```powershell
uv run --locked python -m unittest tests.e2e.test_avito_pipeline -v
uv run --locked python -m unittest discover -s tests/e2e -v
uv run --locked ruff check .
```

Expected: the previously unknown-fee fixture is accepted only on a fully verified filtered search page.

- [ ] **Step 6: Commit**

```powershell
git add src/rent_monitor/core/models.py src/rent_monitor/parsers/avito.py src/rent_monitor/storage/sqlite.py tests/e2e/test_avito_pipeline.py
git commit -m "feat(avito): verify filter-derived commission"
```

---

### Task 6: Browser-backed Avito collection and multiple configured jobs

**Files:**
- Modify: `src/rent_monitor/collectors/avito.py`
- Modify: `src/rent_monitor/main.py`
- Modify: `src/rent_monitor/collectors/__init__.py`
- Modify: `tests/e2e/test_avito_pipeline.py`
- Create: `tests/unit/test_avito_collector.py`

**Interfaces:**
- Consumes: `AvitoSearchConfig`, `PlaywrightBrowserTransport.fetch()`, evidence-aware parser, and independent runner.
- Produces: `AvitoCollector(searches: tuple[AvitoSearchConfig, ...])` with `source = "avito"` and `interval_seconds = min(search.poll_interval_seconds)`.
- Produces: one union `CollectionResult` per due cycle; first call loads every configured job so baseline seeding is complete.

- [ ] **Step 1: Write failing browser-collector tests**

```python
class AvitoCollectorTest(unittest.IsolatedAsyncioTestCase):
    async def test_collects_all_jobs_sequentially_and_unions_ids(self) -> None:
        browser = FakeBrowserTransport(
            {
                SEARCH_ONE.url: ok_page(verified_search_page(card("1111111111")), SEARCH_ONE.url),
                SEARCH_TWO.url: ok_page(verified_search_page(card("2222222222")), SEARCH_TWO.url),
            }
        )
        collector = AvitoCollector((SEARCH_ONE, SEARCH_TWO))
        result = await collector.collect(CRITERIA, browser)
        self.assertEqual(result.status, SourceHealth.OK)
        self.assertEqual(result.seen_source_ids, ("1111111111", "2222222222"))
        self.assertEqual(browser.max_concurrent_fetches, 1)

    async def test_captcha_returns_paused_without_second_job_navigation(self) -> None:
        browser = FakeBrowserTransport({SEARCH_ONE.url: ok_page(CAPTCHA_HTML, SEARCH_ONE.url)})
        collector = AvitoCollector((SEARCH_ONE, SEARCH_TWO))
        result = await collector.collect(CRITERIA, browser)
        self.assertEqual(result.failure_code, "captcha")
        self.assertEqual(browser.requested_urls, [SEARCH_ONE.url])
```

Also cover 403, 429 with `Retry-After`, browser exception, unknown structure, and a successful second call after a temporary error. Assert there is no `_paused_reason` latch.

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run --locked python -m unittest tests.unit.test_avito_collector -v`

Expected: constructor or BrowserPage interface failure.

- [ ] **Step 3: Replace the constant URL and permanent pause**

Remove `SEARCH_URL` from production code and remove `_paused_reason`. The collector loops through configured jobs sequentially, awaits one browser fetch at a time, validates each final URL, parses it, and unions candidates by source ID while preserving configuration order.

On the first call, every job is due. On later calls, track each job's monotonic next-due time; a job is due when its configured interval has elapsed. If no job is due, return a recognized OK result with no listings and preserve the prior card-count metric rather than treating the source as empty.

CAPTCHA or access restriction aborts the remaining jobs immediately. A normal job error returns a source failure so Task 2 controls retries. Do not perform a direct detail-page request before notification.

- [ ] **Step 4: Wire BrowserTransport in application lifecycle**

Resolve the browser profile under the state directory:

```python
profile_path = config.database_path.parent / "avito-browser-profile"
browser = PlaywrightBrowserTransport(profile_path, headless=False)
await browser.start()
```

Create the Avito runtime with the browser client and minimum configured interval. Build Yandex with `BoundedHttpClient`. Close the browser in `finally` after runners stop.

- [ ] **Step 5: Preserve first-deployment baseline behavior**

Add an on-disk repository test that starts with the existing `avito` baseline and baseline candidates, runs the browser-backed collector, and confirms no old listing notification is created. Keep `CollectionResult.source == "avito"`, so existing baseline keys remain valid.

- [ ] **Step 6: Run tests and commit**

Run:

```powershell
uv run --locked python -m unittest tests.unit.test_avito_collector tests.e2e.test_avito_pipeline -v
uv run --locked python -m unittest discover -s tests/e2e -v
uv run --locked ruff check .
```

Then:

```powershell
git add src/rent_monitor/collectors/avito.py src/rent_monitor/collectors/__init__.py src/rent_monitor/main.py tests/unit/test_avito_collector.py tests/e2e/test_avito_pipeline.py
git commit -m "feat(avito): collect through persistent Chromium"
```

---

### Task 7: Durable source alerts and richer status

**Files:**
- Modify: `src/rent_monitor/core/models.py`
- Modify: `src/rent_monitor/storage/sqlite.py`
- Modify: `src/rent_monitor/core/scheduler.py`
- Modify: `src/rent_monitor/telegram/bot.py`
- Create: `tests/unit/test_source_alerts.py`
- Modify: `tests/e2e/test_monitor_pipeline.py`

**Interfaces:**
- Produces: `SourceAlert(alert_id, event_key, source, health, failure_code, occurred_at, last_success_at, next_attempt_at, outage_seconds, attempts)`.
- Produces repository methods: `enqueue_source_alert`, `claim_pending_source_alerts`, `mark_source_alert_delivered`, `retry_source_alert`.
- Extends notifier with `send_source_alert(chat_id, alert)`.
- Consumes: Task 2 `SourceTransition` from Task 3 runner.

- [ ] **Step 1: Write failing durable alert tests**

```python
class SourceAlertTest(unittest.IsolatedAsyncioTestCase):
    async def test_transition_is_enqueued_once_and_retried(self) -> None:
        transition = transition_fixture(
            previous=SourceRunHealth.HEALTHY,
            current=SourceRunHealth.MANUAL_ATTENTION,
            failure_code="captcha",
        )
        first = await self.repository.enqueue_source_transition(transition)
        second = await self.repository.enqueue_source_transition(transition)
        self.assertTrue(first)
        self.assertFalse(second)
        claimed = await self.repository.claim_pending_source_alerts(limit=10)
        await self.repository.retry_source_alert(claimed[0].alert_id, "telegram_send_failed")
        self.assertEqual(len(await self.repository.claim_pending_source_alerts(limit=10)), 0)
```

Advance the injected repository clock past retry delay and assert the alert is claimable again. Add a recovery alert test with exact outage duration.

- [ ] **Step 2: Add the source alert table**

```sql
CREATE TABLE IF NOT EXISTS source_alert_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    health TEXT NOT NULL,
    failure_code TEXT,
    occurred_at TEXT NOT NULL,
    last_success_at TEXT,
    next_attempt_at TEXT,
    outage_seconds INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    deliver_after TEXT NOT NULL,
    claimed_until TEXT,
    delivered_at TEXT,
    last_error_code TEXT
);
```

Build `event_key` from source plus transition timestamp plus target health. Use the same five-minute claim lease and bounded retry schedule as listing notifications.

- [ ] **Step 3: Generate alerts only for meaningful transitions**

The runner calls `enqueue_source_transition` for:

- any transition to `cooldown`, `blocked`, or `manual_attention`;
- `degraded` only when failure count reaches two;
- any transition from a non-healthy state to `healthy`.

Do not alert for `starting -> healthy` or the first transient transport failure.

- [ ] **Step 4: Deliver and format source alerts**

Extend `deliver_outbox_once` to drain listing notifications first, then source alerts, without allowing failure in one queue to skip the other. Add `TelegramNotifier.send_source_alert` with Russian messages and Moscow-local timestamps.

Update `/status` to prefer `source_runtime` and show last success, next attempt, interval,
consecutive failures, last card count, newest visible ID, and manual-action requirement. Keep a
compatibility fallback to old `source_status` rows during migration. When an outbox item is marked
delivered, log numeric `first_seen_to_delivered_seconds`; when reliable `published_at` exists, also
log `published_to_first_seen_seconds` without logging listing text or URL.

- [ ] **Step 5: Run alert and E2E tests**

Run:

```powershell
uv run --locked python -m unittest tests.unit.test_source_alerts -v
uv run --locked python -m unittest discover -s tests/e2e -v
uv run --locked ruff check .
```

Expected: one transition alert, durable retry, one recovery alert, and no duplicate after repository restart.

- [ ] **Step 6: Commit**

```powershell
git add src/rent_monitor/core/models.py src/rent_monitor/storage/sqlite.py src/rent_monitor/core/scheduler.py src/rent_monitor/telegram/bot.py tests/unit/test_source_alerts.py tests/e2e/test_monitor_pipeline.py
git commit -m "feat(alerts): notify source health transitions"
```

---

### Task 8: Single-use CAPTCHA sessions and Telegram controls

**Files:**
- Create: `src/rent_monitor/browser/captcha.py`
- Create: `tests/unit/test_captcha.py`
- Modify: `src/rent_monitor/telegram/bot.py`
- Modify: `src/rent_monitor/main.py`
- Modify: `src/rent_monitor/core/scheduler.py`

**Interfaces:**
- Produces: `CaptchaSessionManager(token_directory, public_base_url, target="127.0.0.1:5900", ttl_seconds=900)`.
- Produces async methods: `issue(source) -> CaptchaSession`, `expire(token)`, `expire_all()`, `active_session()`.
- Consumes: BrowserTransport screenshot and `source_runtime.manual_attention`.
- Produces: owner-only callback actions `captcha:open:avito` and `captcha:check:avito`.
- Produces: `check_manual_attention_source(source, browser, repository, criteria) -> bool`, which
  performs one inspection, persists success only for a recognized non-CAPTCHA page, and enqueues one
  recovery transition.

- [ ] **Step 1: Write failing token lifecycle tests**

```python
class CaptchaSessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_issue_creates_token_file_and_expiry_removes_it(self) -> None:
        manager = CaptchaSessionManager(
            self.directory,
            "https://rent-monitor.example.ts.net",
            ttl_seconds=900,
            clock=self.clock,
        )
        session = await manager.issue("avito")
        token_file = self.directory / session.token
        self.assertEqual(token_file.read_text(encoding="ascii"), "127.0.0.1:5900\n")
        self.assertIn(f"token={session.token}", session.url)
        self.clock.advance(timedelta(minutes=16))
        await manager.remove_expired()
        self.assertFalse(token_file.exists())

    async def test_only_one_session_can_be_active(self) -> None:
        first = await self.manager.issue("avito")
        second = await self.manager.issue("avito")
        self.assertEqual(first.token, second.token)
```

Assert generated token filenames contain only URL-safe random characters, mode `0600`, and no source name or chat ID.

- [ ] **Step 2: Implement token files compatible with websockify `TokenFileName`**

Create the directory with mode `0700`. Write each token file atomically with contents `127.0.0.1:5900\n`, then chmod `0600`. Build the URL as:

```python
query = urlencode(
    {
        "autoconnect": "true",
        "resize": "scale",
        "reconnect": "false",
        "show_dot": "true",
        "token": token,
    }
)
url = f"{base_url.rstrip('/')}/vnc_lite.html?{query}"
```

Maintain expiry in memory and remove every stale token at startup. Use noVNC's lite client, which
does not expose the full application's clipboard panel or file controls. Tailscale is the network
access boundary; the random token is an additional single-session gate.

- [ ] **Step 3: Add owner-only Telegram callbacks**

Source-alert messages for `manual_attention/captcha` include callback buttons, not an already-expiring URL. On `captcha:open:avito`, verify the bound private chat and current state, issue a session, call `browser.screenshot()`, optionally send the PNG from memory, then send a URL button valid for 15 minutes.

On `captcha:check:avito`, trigger exactly one controlled browser inspection. If the expected search context is recognized, apply a successful state transition, expire the token, wake the source runner, and enqueue recovery. If CAPTCHA remains, keep `manual_attention` and answer without starting the schedule.

- [ ] **Step 4: Test unauthorized, expired, failed, and successful flows**

Use fake callback queries and a fake browser. Assert an unbound chat receives no link, expired tokens are removed, remaining CAPTCHA never resumes, and recognized search resumes exactly once.

Run:

```powershell
uv run --locked python -m unittest tests.unit.test_captcha -v
uv run --locked python -m unittest discover -s tests/e2e -v
uv run --locked ruff check .
```

- [ ] **Step 5: Commit**

```powershell
git add src/rent_monitor/browser/captcha.py src/rent_monitor/telegram/bot.py src/rent_monitor/main.py src/rent_monitor/core/scheduler.py tests/unit/test_captcha.py
git commit -m "feat(captcha): add private manual recovery sessions"
```

---

### Task 9: VPS display, noVNC, Tailscale, and resource deployment

**Files:**
- Create: `deploy/run-captcha-stack.sh`
- Create: `deploy/systemd/rent-monitor-captcha.service`
- Modify: `deploy/systemd/rent-monitor.service`
- Modify: `deploy/install.sh`
- Modify: `deploy/README.md`

**Interfaces:**
- Provides: display `:99` for headed Chromium.
- Provides: VNC on loopback `127.0.0.1:5900`.
- Provides: noVNC/websockify on loopback `127.0.0.1:6080` using `TokenFileName` and `/run/rent-monitor-captcha/tokens`.
- Provides: Tailscale Serve HTTPS proxy to `127.0.0.1:6080` within the tailnet.

- [ ] **Step 1: Write a shell smoke check before the service implementation**

Add `deploy/check-captcha-stack.sh` logic inside the install verification section rather than a permanent test daemon:

```sh
test -S /tmp/.X11-unix/X99
ss -ltn | grep -F '127.0.0.1:5900'
ss -ltn | grep -F '127.0.0.1:6080'
test -d /run/rent-monitor-captcha/tokens
test "$(stat -c %a /run/rent-monitor-captcha/tokens)" = "700"
```

Run the checks manually before creating the unit and record the expected initial failure in the deployment notes.

- [ ] **Step 2: Implement the supervised support stack**

`run-captcha-stack.sh` starts:

```sh
Xvfb :99 -screen 0 1440x1000x24 -nolisten tcp
x11vnc -display :99 -rfbport 5900 -listen 127.0.0.1 -forever -shared -nopw
websockify --web /usr/share/novnc --token-plugin TokenFileName \
  --token-source /run/rent-monitor-captcha/tokens 127.0.0.1:6080
```

Use `trap` to terminate all children and `wait -n` so failure of any child terminates the stack and lets systemd restart it. The script must reject a non-empty unexpected argument list.

The support unit runs as `rent-monitor`, creates `RuntimeDirectory=rent-monitor-captcha` with mode `0700`, restarts on failure, and uses the same hardening baseline as the main service. It does not receive Telegram credentials.

- [ ] **Step 3: Update the main service**

Add:

```ini
After=network-online.target rent-monitor-captcha.service tailscaled.service
Wants=network-online.target rent-monitor-captcha.service
Environment=DISPLAY=:99
Environment=RENT_MONITOR_CAPTCHA_TOKEN_DIRECTORY=/run/rent-monitor-captcha/tokens
CPUQuota=200%
MemoryMax=4G
TasksMax=512
```

Keep `ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp=true`, and `NoNewPrivileges=true`. Allow read/write access to the state directory and CAPTCHA token directory only.

- [ ] **Step 4: Update installation**

Install distribution packages `xvfb`, `x11vnc`, `novnc`, `websockify`, and Tailscale from its official repository. After `uv sync --locked`, install Playwright Chromium and OS dependencies:

```sh
/opt/rent-monitor/current/.venv/bin/playwright install chromium
/opt/rent-monitor/current/.venv/bin/playwright install-deps chromium
```

Install and enable both unit files. Do not enable Tailscale Funnel.

- [ ] **Step 5: Configure private Tailscale Serve**

After the operator enrolls the VPS and iPhone in the same tailnet, run:

```sh
sudo tailscale serve --bg 127.0.0.1:6080
sudo tailscale serve status --json
```

Read the resulting tailnet-only HTTPS URL and set it as `RENT_MONITOR_CAPTCHA_BASE_URL` in a root-owned environment file referenced by the main unit. Document that the iPhone must have Tailscale connected before opening the Telegram link.

- [ ] **Step 6: Verify unit hardening and ports**

Run:

```sh
systemd-analyze verify /etc/systemd/system/rent-monitor.service
systemd-analyze verify /etc/systemd/system/rent-monitor-captcha.service
systemctl restart rent-monitor-captcha rent-monitor
systemctl is-active rent-monitor-captcha rent-monitor
ss -ltn
```

Expected: ports 5900 and 6080 are bound only to `127.0.0.1`; there is no public VNC/noVNC listener; the main service is allowed two CPU equivalents, 4 GiB, and 512 tasks.

- [ ] **Step 7: Commit**

```powershell
git add deploy/run-captcha-stack.sh deploy/systemd/rent-monitor-captcha.service deploy/systemd/rent-monitor.service deploy/install.sh deploy/README.md
git commit -m "feat(deploy): add private browser control stack"
```

---

### Task 10: End-to-end verification, documentation, and rollout guardrails

**Files:**
- Modify: `README.md`
- Modify: `deploy/README.md`
- Modify: `tests/e2e/test_avito_pipeline.py`
- Modify: `tests/e2e/test_monitor_pipeline.py`
- Modify: `docs/superpowers/plans/2026-09-26-avito-browser-monitor.md` only to check completed boxes during execution

**Interfaces:**
- Validates every acceptance criterion from the design.
- Produces an operator checklist for German-VPS deployment and rollback.

- [ ] **Step 1: Add the final E2E scenarios**

Create these concrete scenarios using the `FakeBrowserTransport`, `FakeTelegramSender`,
`run_one_runner_tick`, and fixture builders already introduced by earlier tasks:

```python
async def test_browser_listing_reaches_telegram_in_same_cycle(self) -> None:
    await seed_avito_baseline(self.repository, "1111111111")
    browser = FakeBrowserTransport.single(
        ok_page(verified_search_page(card("2222222222")))
    )
    result = await AvitoCollector((SEARCH_ONE,)).collect(CRITERIA, browser)
    await process_collection_result(result, CRITERIA, self.repository)
    delivered = await deliver_outbox_once(self.repository, self.notifier)
    self.assertEqual(delivered, 1)
    self.assertEqual(self.notifier.notifications[0].listing.source_id, "2222222222")

async def test_existing_avito_baseline_suppresses_old_browser_results(self) -> None:
    await seed_avito_baseline(self.repository, "1111111111")
    browser = FakeBrowserTransport.single(
        ok_page(verified_search_page(card("1111111111")))
    )
    result = await AvitoCollector((SEARCH_ONE,)).collect(CRITERIA, browser)
    await process_collection_result(result, CRITERIA, self.repository)
    self.assertEqual(await deliver_outbox_once(self.repository, self.notifier), 0)

async def test_single_403_schedules_recovery_instead_of_permanent_pause(self) -> None:
    browser = FakeBrowserTransport.single(forbidden_page())
    collector = AvitoCollector((SEARCH_ONE,))
    await run_one_runner_tick(collector, browser, self.repository)
    state = await self.repository.get_source_run_state("avito")
    self.assertEqual(state.health, SourceRunHealth.COOLDOWN)
    self.assertIsNotNone(state.next_attempt_at)
    self.assertEqual(browser.fetch_count, 1)

async def test_captcha_stops_navigation_across_restart(self) -> None:
    database_path = self.temporary_directory / "restart.sqlite3"
    first = await open_repository(database_path)
    await first.save_source_run_state(SourceRunState.manual_attention("avito", "captcha"))
    await first.close()
    second = await open_repository(database_path)
    browser = FakeBrowserTransport.single(ok_page(CAPTCHA_HTML))
    await run_one_runner_tick(AvitoCollector((SEARCH_ONE,)), browser, second)
    self.assertEqual(browser.fetch_count, 0)
    await second.close()

async def test_successful_manual_check_resumes_and_alerts_once(self) -> None:
    await self.repository.save_source_run_state(
        SourceRunState.manual_attention("avito", "captcha")
    )
    browser = FakeBrowserTransport.single(
        ok_page(verified_search_page(card("1111111111")))
    )
    resumed = await check_manual_attention_source(
        "avito", browser, self.repository, CRITERIA
    )
    self.assertTrue(resumed)
    self.assertEqual(
        (await self.repository.get_source_run_state("avito")).health,
        SourceRunHealth.HEALTHY,
    )
    self.assertEqual(len(await self.repository.claim_pending_source_alerts(10)), 1)

async def test_browser_crash_does_not_cancel_yandex_or_outbox(self) -> None:
    avito = RaisingCollector("avito", BrowserClosedError("closed"))
    yandex = RecordingCollector("yandex", result=CollectionResult(source="yandex"))
    await run_collectors_for_test(
        [(avito, FailingBrowserClient(), 60), (yandex, object(), 300)],
        self.repository,
        ticks=1,
    )
    self.assertEqual(yandex.calls, 1)
    self.assertEqual(await deliver_outbox_once(self.repository, self.notifier), 0)
```

Define the named helpers locally in the test modules with no sleeps longer than a test tick. Restart
cases use a temporary on-disk SQLite database; all other cases use SQLite in memory. No test accesses
Avito.

- [ ] **Step 2: Run the full local verification suite**

Run:

```powershell
uv sync --locked
uv run --locked python -m unittest discover -s tests/unit -v
uv run --locked python -m unittest discover -s tests/integration -v
uv run --locked python -m unittest discover -s tests/e2e -v
uv run --locked ruff check .
uv run --locked ruff format --check .
git diff --check
```

Expected: all tests and static checks pass.

- [ ] **Step 3: Document operator behavior**

README must state:

- Avito is checked approximately once per minute through a persistent Chromium profile.
- Cian and Domclick are inactive pending later work.
- A CAPTCHA pauses Avito and produces an owner-only Telegram control flow.
- The iPhone must be connected to Tailscale; no VNC endpoint is public.
- LLM enrichment is not installed and is unnecessary for current filters.
- `/status` fields and source transitions have precise meanings.

Deployment documentation must include database backup, previous-release symlink, service installation, Tailscale enrollment, private Serve configuration, smoke check, journal commands, and rollback commands.

- [ ] **Step 4: Deploy with an atomic rollback point**

On the German VPS:

```sh
sudo systemctl stop rent-monitor
sudo cp --reflink=auto /var/lib/rent-monitor/rent-monitor.sqlite3 \
  /var/lib/rent-monitor/rent-monitor.sqlite3.pre-browser
sudo /opt/rent-monitor/current/deploy/install.sh
sudo systemctl start rent-monitor-captcha rent-monitor
```

Confirm the existing Telegram binding and pending outbox are intact. Confirm the first recognized Avito result uses the existing baseline and sends no historical listing burst.

- [ ] **Step 5: Perform live acceptance checks**

Observe at least five normal minute cycles in the journal and verify:

- final URL remains an Avito search URL;
- active filters are recognized;
- card count is non-negative and plausible;
- no raw HTML or cookies appear in logs;
- `/status` shows healthy Avito and a next attempt near one minute;
- Yandex continues on its independent schedule;
- Chromium memory stays within the 4 GiB service limit.

If a CAPTCHA is naturally present, complete the iPhone flow. If not, use the local fixture/manual-attention test path to verify Telegram → Tailscale → noVNC without generating a real challenge.

- [ ] **Step 6: Keep rollback available for 72 hours**

Monitor transition alerts, successful polls, card counts, detection-to-outbox timing, and Chromium restarts. Roll back if the browser repeatedly enters blocked/manual-attention, baseline behavior is wrong, or the service breaches its memory/task limits. Do not activate the Netherlands or Russian VPS concurrently.

- [ ] **Step 7: Commit final documentation and test coverage**

```powershell
git add README.md deploy/README.md tests/e2e/test_avito_pipeline.py tests/e2e/test_monitor_pipeline.py docs/superpowers/plans/2026-09-26-avito-browser-monitor.md
git commit -m "docs: add Avito browser operations guide"
```
