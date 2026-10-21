# SnipeKlein

Kleinanzeigen Deal Sniper - monitors German classifieds for undervalued custom
gaming PCs, values every candidate deterministically and pushes the good ones
to Discord.

The full specification lives in [`project_plan.md`](project_plan.md); this
README describes the implementation.

---

## How it works

```text
poll loop (45-85 s, jittered)
   -> search pages (category + price filter + keyword, page 1)
   -> parse cards (id, title, price, location, shipping)
   -> pre-filter      (title rules: no "Suche"/"Defekt", no DDR4/AM4/GTX, ...)
   -> triage          (regex component match + FMV estimate, no network, no LLM)
   -> detail page     (Astro props JSON + legacy template fallback)
   -> extraction      (Gemini, strict JSON schema, regex fallback)
   -> valuation       (component FMV table + bundle discount + defect costs)
   -> risk            (account age, badges, shipping, margin outliers)
   -> Discord webhook (green / yellow / red border)
```

Two design decisions keep the bot cheap enough to run 24/7:

* the **triage** stage rejects most listings with a regex and the price table -
  no detail page, no LLM call;
* the **extractor** only runs for survivors and its result is cached in SQLite.

---

## Requirements

* Python 3.11+ (Docker image uses 3.13)
* A Google AI Studio API key for the extraction model
* A Discord webhook URL
* Ideally a German/EU VPS (see [Deployment](#deployment))

---

## Quick start (local)

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env      # fill in GEMINI_API_KEY and DISCORD_WEBHOOK_URL
.venv/bin/snipeklein config validate
.venv/bin/snipeklein poll --once --dry-run     # one cycle, no alerts
.venv/bin/snipeklein poll                      # continuous
```

`.env`:

```ini
GEMINI_API_KEY=...
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
# optional: German residential proxy so the crawler egresses from DACH
# PROXY_URL=http://user:pass@host:port
```

These are read from the environment (the compose file and the CLI both use
`os.environ`), never from the config file.

---

## Deployment (Docker)

The compose file takes **all secrets from the environment** - there is no
`env_file` and no committed `.env`:

```yaml
environment:
  TZ: Europe/Berlin
  GEMINI_API_KEY: "${GEMINI_API_KEY:-}"
  DISCORD_WEBHOOK_URL: "${DISCORD_WEBHOOK_URL:-}"
  PROXY_URL: "${PROXY_URL:-}"
```

Locally `docker compose` loads `./.env` automatically for the substitution; on
a server the platform injects the variables. A missing variable becomes an
empty string, so the bot starts but reports `llm key MISSING` and `health`
returns `attention` - the watchdog then posts to Discord. Fail-soft, not a
crash loop.

Three services, all started by default:

| Service | What it does |
|---|---|
| `bot` | polls every 45–85 s, values candidates, sends Discord alerts (container healthcheck = `snipeklein health`) |
| `price-refresher` | refreshes the FMV table every day at 03:00 (server local time) and writes a consistent backup afterwards |
| `watchdog` | checks the bot every 5 minutes and posts to Discord if it is unhealthy |
| `panel` | read-only web panel with the full decision trail (see below) |

State lives in the named volume `snipeklein-data` (SQLite database + harvested
prices). The curated component table is **not** in the volume: it is shipped
with the image from `data/prices.json` and therefore updated with every
deploy, while the harvested values live in the volume's database and are
merged on top.

### The modern platform tier

The price table only knows the components it was given, and a new chipset
appears every few months.  Instead of dropping such an ad, the triage lets it
through when the **title hints at a current generation platform** (RTX 40/50
series, AM5 board, Ryzen 7000/9000, 12th gen+ Intel, or the literal
"DDR5"):

| components recognised | title hints modern | outcome |
|---|---|---|
| 2 or more | either | margin check as usual |
| 0-1 | yes | the ad goes to the extraction - **the description is where the specification usually is** |
| 0-1 | no | dropped as `triage_components` (older DDR4 hardware, no LLM waste) |

Switch it off with `filters.modern_platform_tier = false` in `config.toml`.

Cost: the extraction is now on the critical path of a cycle.  A *cold* cycle
(after a fresh database) takes a few minutes because every new ad costs a
detail page and an LLM call; the steady state is a few seconds per cycle
because known ads are skipped.  The LLM rate is capped locally at
`llm.max_rpm`.

### The decision panel

Every ad the bot looks at is recorded with the decision it made and why - the
listing was rejected by the title filter, it did not survive the triage, the
RAM is not DDR5, the margin is not in the deal band, an alert was sent. The
panel shows that trail.

```bash
snipeklein panel --port 8137          # http://127.0.0.1:8137
```

* **Read-only by construction**: the database is opened with the sqlite
  `mode=ro` URI and `PRAGMA query_only`, the server only implements GET, and
  every write endpoint answers `405`. Listing data is inserted into the page
  with `textContent`, never as HTML.
* **Authentication**: HTTP basic auth with `PANEL_USER` / `PANEL_PASSWORD`
  from the environment. Binding to anything but `127.0.0.1` without them is
  refused at startup.
* **Data**: one row per ad in `ad_decisions` - the outcome, the stage that
  decided it, the reason, the full step trail and the structured extraction /
  valuation / risk data. The row is only rewritten when the decision or the
  asking price changes, so the write volume stays tiny.
* **Retention**: `decision_retention_days` (14) and a cap of
  `max_decision_rows` (100 000), pruned by the bot's maintenance run and by
  `snipeklein prune`.

Endpoints: `/` (the UI), `/api/summary`, `/api/ads`, `/api/ads/<ad_id>`,
`/api/alerts`, `/api/prices`, `/api/runs`, `/api/health`.

On the VPS the panel is a compose service; give it a domain in Dokploy
(service `panel`, port 8137) and set `PANEL_USER` / `PANEL_PASSWORD` in the
environment. It shows seller names, locations and account ages, so keep it
behind the credentials.

### Health and the watchdog

`snipeklein health` reports one of three states:

| state | meaning | exit code |
|---|---|---|
| `ok` | the last run is recent, error free, secrets present | 0 |
| `starting` | no run recorded yet - a fresh deployment is finishing its first cycle | 0 |
| `attention` | the last run is stale, had errors, or a secret is missing | 1 |

`starting` exists so that a fresh deployment is never reported as broken -
the first poll cycle takes ~45 s and the watchdog used to fire a false alarm
during exactly that window.

The watchdog is deliberately quiet (all values in `config.toml` under
`[watchdog]`):

| setting | default | meaning |
|---|---|---|
| `interval` | 300 s | how often the bot is checked |
| `grace` | 600 s | no alarm during this period after the watchdog starts |
| `failures` | 3 | consecutive failed checks before Discord is told |
| `cooldown` | 21600 s | at most one alarm per 6 h while the bot stays broken |

One "back to normal" message is sent when the bot recovers. The state
(consecutive failures, last alarm) lives in `data/watchdog_state.json` in the
volume, so restarting the watchdog does not re-alarm.

### Backups

`snipeklein backup` writes a **consistent** snapshot of the database using
SQLite's online backup API (copying a WAL-mode file with `cp` would miss the
transactions that still live in the `-wal` file), copies the price table next
to it and prunes to the last 14 daily backups. The price refresher runs it
after every refresh.

### Local

```bash
cp .env.example .env && $EDITOR .env && chmod 600 .env
docker compose up -d --build          # bot + price-refresher + watchdog
docker compose logs -f bot
docker compose exec bot snipeklein health
```

Only the bot: `docker compose up -d bot`.

The CLI in the virtualenv uses the *host* directory `data/` (not the volume),
so `snipeklein listings` from your shell shows the development state while the
container keeps its own. `snipeklein health` prints the resolved path.

### VPS with Dokploy

1. Push this repository to a private GitHub (or Gitea/GitLab) repository.
2. Dokploy → **Create → Application → Compose** (not *Stack* - Swarm mode has
   no `build:`), connect the Git provider, select the repository and the
   `main` branch, compose path `docker-compose.yml`.
3. **Environment** tab → add `GEMINI_API_KEY` and `DISCORD_WEBHOOK_URL`.
   Dokploy writes them into a `.env` next to the compose file and passes it
   with `--env-file`. Leave "create .env file" enabled (it is the default).
   Optionally add `PROXY_URL` for a German residential proxy.
4. **General** → **Deploy**. No domain and no ports are needed: this is a
   worker that only talks to kleinanzeigen.de, the Gemini API and the Discord
   webhook.
5. **Advanced → Volume Backups** → enable the backup for the named volume
   `snipeklein-data` (Dokploy can only snapshot named volumes) so the daily
   backup also leaves the machine.
6. **General → Enable Auto Deploy** with the GitHub webhook URL.
7. Check the deployment: the log must show `cycle done: … errors=0` and
   `snipeklein health` must print `status: OK`.

Dokploy runs `docker compose -p <app> --env-file <dir>/.env -f <compose> up -d
--build --remove-orphans` - no `--profile`, which is why the compose file has
no profiles.

### EU residency

The site answers foreign data centre IPs with more anti-bot challenges
(spec 3.1). Deploy on a German/EU host, or set `PROXY_URL` in the environment
to a German residential proxy.

---

## CLI

| Command | Purpose |
|---|---|
| `snipeklein poll [--once] [--query TAG] [--dry-run] [--reset-seen]` | poll for new listings (continuous by default) |
| `snipeklein value <ad_id\|url> [--json] [--embed]` | value a single listing; `--embed` prints the Discord embed |
| `snipeklein prices refresh [--force] [--reset-observations]` | refresh the FMV table from the used market |
| `snipeklein prices refresh --daily-at 03:00 [--backup]` | refresh once a day at a fixed time (the price refresher container) |
| `snipeklein prices show` | show the effective component table with provenance |
| `snipeklein prices import file.csv` | import manual corrections (`component_id;fmv_eur`) |
| `snipeklein listings [--hours N]` | ingested listings with margins |
| `snipeklein alerts [--band actionable]` | sent alerts |
| `snipeklein filter-report` | why listings were rejected (tuning aid) |
| `snipeklein usage [--days N]` | LLM requests / tokens / 429s per day |
| `snipeklein health` | configuration, database and quota state |
| `snipeklein triage <ad_id\|url>` | explain the cheap triage decision |
| `snipeklein extract` | smoke test the extraction against the live Gemini API |
| `snipeklein panel [--port 8137]` | serve the read-only decision panel |
| `snipeklein panel --check` | verify that the panel can read the database |
| `snipeklein panel --check` | verify that the panel can read the database |
| `snipeklein watchdog` | watch the bot and report problems to Discord (used by the watchdog container) |
| `snipeklein backup` | write a consistent snapshot of the database and the price table |
| `snipeklein test-alert [--dry-run]` | send a synthetic deal to the Discord webhook |
| `snipeklein notify "text"` | post a plain text message to the Discord webhook |
| `snipeklein prune` | delete expired data (seen-ids, old listings, old observations) |

`--json` is available on every command for machine readable output.

---

## Configuration

`config.toml` holds everything that is not a secret:

```toml
[[queries]]
tag = "pc-gaming-ddr5"
category = "pcs"          # category slug
category_id = 228         # category id used in the URL
keywords = ["gaming pc", "rtx 4070"]
min_price = 300
max_price = 1800
max_pages = 1             # page 1 only (newest first)
```

Important keys:

| Section | Key | Meaning |
|---|---|---|
| `general` | `seen_ttl_days` | how long a listing id is remembered (30) |
| `ingestion` | `poll_interval_min_s` / `max_s` | randomised poll cadence |
| `ingestion` | `min_request_interval_s` | global politeness delay (2 s) |
| `ingestion` | `proxy_url` | optional DE/EU proxy |
| `filters` | `required_ram_generation` | `DDR5` |
| `filters` | `triage_min_margin_pct` | margin a listing needs to reach before it costs an LLM call |
| `valuation` | `bundle_discount` | 0.12 (spec: 0.10-0.15) |
| `valuation` | `min_coverage` | share of a complete system that must be known (0.75) |
| `valuation` | `actionable_margin_min_pct` / `max_pct` | 30 / 55 |
| `valuation` | `extreme_margin_pct` | 60 |
| `llm` | `model` / `fallback_model` | `gemini-3.5-flash-lite` / `gemini-3.1-flash-lite` |
| `llm` | `max_rpm` | local rate limit (6 requests per minute) |
| `risk` | `account_new_days` | accounts younger than this are flagged (30) |

### Search URLs

The site encodes filters into the path:

```
https://www.kleinanzeigen.de/s-{category}/preis:{min}:{max}[/seite:N]/{keyword}/k{location}c{category_id}
```

The keyword **must** come after the price segment - a wrong order silently
returns unfiltered results. `src/snipeklein/ingestion/queries.py` builds these
URLs and `tests/test_queries.py` locks the layout down.

---

## Valuation model

```text
FMV_gross = sum(Price(component))
FMV_net   = (FMV_gross + adjustments) * (1 - bundle_discount) - sum(Cost(defects))
Margin %  = (FMV_net - asking_price) / FMV_net * 100
```

* Only components that are actually named in the ad are valued - the buyer
  cannot count on parts the seller did not mention.
* `coverage` compares the recognised value with the value of a complete system
  (CPU + GPU + RAM + storage + board + PSU + case/cooler). Below
  `min_coverage` the listing is not alerted, because too much is unknown.
* `adjustments`: AM5/PCIe4 platform bonus, PSU too small, RAM below 16 GB,
  storage below 512 GB.
* `defect_costs`: missing storage/cooler/OS/case, damaged GPU (80 % of its
  value), unknown defects (30 EUR).
* Condition multipliers: `like_new` 1.0, `ok` 0.93, `alright` 0.85,
  `defect` 0.6, unknown 0.9.
* Bands: **actionable** 30-55 %, **extreme** > 60 %, everything else is no deal.

### Price table

`data/prices.json` is the source of truth: ~90 components (CPUs, GPUs, RAM,
storage, boards, PSUs, case/cooler bundle) with aliases that are matched
case- and space-insensitively. It is shipped with the image (`/app/prices.json`)
and the harvested values are layered on top from the database in the volume.

Every component keeps the value from the specification as `spec_baseline`
(e.g. `AMD_RYZEN_7_7800X3D` 310 EUR, `NVIDIA_RTX_4070_12GB` 440 EUR) next to
the current `fmv_eur`, plus `curated_fmv_eur`, `market_source`,
`market_samples` and `market_observed` so every change stays traceable.

The current values are the **measured 2026 used market**, not the estimates
from the specification - the second-hand prices in this market are far above
the 2024/2025 reference values (a used RTX 4070 asks ~600 EUR, 32 GB DDR5
~290 EUR), and a table built on the old numbers would mark every listing as a
bargain.

Manual corrections win over automatic ones - set `"pinned": true` on a
component or import a CSV (`snipeklein prices import`).

### Price harvester (Kleinanzeigen only)

`snipeklein prices refresh` measures the used market itself: for every
component it runs one targeted search, keeps the cards that really offer the
component and computes a robust FMV (median ± 3·MAD) from the accumulated
observations, multiplied by `asking_to_sold_factor` (0.92). At least 5 samples
are required, otherwise the curated value is kept.

The filters matter more than the estimator - a card is only a valid sample if
it is

* not a complete system (`Gaming PC ...`, `Workstation ...`, `Laptop ...`),
* not a wanted ad (`Suche ...`, `Tausch ...`),
* not a bundle (`CPU + Mainboard`, `Mainboard mit CPU`),
* not an accessory (`Kühler für ...`, `Lüfter für ...`),
* not new stock (`OVP`, `Neu`, `unbenutzt`),
* not a professional seller (`/pro/...` shop links),
* and the *exact* model - `rtx 4070` never matches an `rtx 4070 ti` card and
  `ryzen 7 7800x` never matches a `ryzen 7 7800x3d` card.

The job is slow on purpose (one request per component, page 1 only) and runs
in its own container. Use `--force` to apply values that deviate strongly
from the curated ones and `--reset-observations` to start from scratch.

To fold a harvest into the table:

```bash
snipeklein prices refresh --reset-observations --force --json > harvest.json
python tools/update_prices_from_harvest.py harvest.json
```

---

## LLM (Gemini)

* Default: `gemini-3.5-flash-lite` (free tier, `thinking_level=minimal`).
* Failover: `gemini-3.1-flash-lite` has its own daily quota and takes over
  when the primary model is exhausted.
* The extraction schema is hand written because Gemini only supports a subset
  of JSON Schema (`anyOf`/`oneOf` are rejected, nullables are type arrays).
* `429` handling: `RetryInfo.retryDelay` is honoured, `quota_exceeded` is
  *not* retried - the bot switches model and, if that fails too, keeps running
  on the deterministic regex extractor until the quota resets.
* Usage is tracked in SQLite: `snipeklein usage`.

Cost at the observed volume (a handful of extractions per day): far below
0.10 EUR/month on the paid tier, free on the free tier.

Run `snipeklein extract` once after deploying to verify that the live API
accepts the schema.

---

## Tests

```bash
.venv/bin/python -m pytest -q      # unit + pipeline tests, no network
.venv/bin/ruff check .
.venv/bin/mypy
```

The suite runs against real pages captured in `data/fixtures/` (gzipped). To
refresh them:

```bash
.venv/bin/python tools/capture_fixtures.py
.venv/bin/python tools/sanitize_fixtures.py     # redact the personal data
```

The fixtures are real kleinanzeigen.de pages, so `tools/sanitize_fixtures.py`
replaces every identifying value (seller name, user id, registration date,
postal code, city, e-mail, shop slug) with a neutral placeholder and keeps
ids, titles, prices and the page structure intact. Run it after every
capture - `--check` only reports whether anything is left.

---

## Compliance notes

* Only robots-permitted paths are fetched (`/s-suchergebnisse/*`,
  `/s-anzeige/*`); the RSS feed, message pages and account pages are never
  touched.
* The crawler is slow on purpose: at most one request every two seconds, a
  randomised 45-85 s poll cycle, one request per component for the price
  refresh.
* No login, no interaction with sellers, no personal data beyond the public
  seller fields (name, "Aktiv seit", badges) that are stored to judge fraud
  risk. Listing data is kept locally in SQLite.
* Please check the current terms of use of kleinanzeigen.de yourself and use
  this for your own deal hunting.
