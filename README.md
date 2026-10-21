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

---

## Deployment (Docker, German VPS)

```bash
cp .env.example .env && $EDITOR .env && chmod 600 .env
docker compose up -d bot                          # the poller
docker compose --profile harvest up -d            # daily price refresh + backup
docker compose --profile watchdog up -d           # notifies Discord if the bot is unhealthy
docker compose logs -f bot
```

Three services:

| Service | Profile | What it does |
|---|---|---|
| `bot` | – | polls every 45–85 s, values candidates, sends Discord alerts |
| `price-refresher` | `harvest` | refreshes the FMV table once a day and writes a daily backup to `data/backups/` |
| `watchdog` | `watchdog` | checks `snipeklein health` every 5 minutes and posts to Discord if the bot is unhealthy |

* All services run as the host user (`SNIPEKLEIN_UID`/`SNIPEKLEIN_GID`, default
  1000) so the bind mounted `./data` stays writable.
* `docker compose run --rm bot health` prints the bot state; the same command
  is the container healthcheck and the watchdog's check.
* Set `TZ=Europe/Berlin` (already done in the compose file) and deploy on a
  German/EU host - the site answers foreign data centre IPs with more
  challenges. If that is not possible, set `PROXY_URL` in `.env` to a German
  residential proxy.

---

## CLI

| Command | Purpose |
|---|---|
| `snipeklein poll [--once] [--query TAG] [--dry-run] [--reset-seen]` | poll for new listings (continuous by default) |
| `snipeklein value <ad_id\|url> [--json] [--embed]` | value a single listing; `--embed` prints the Discord embed |
| `snipeklein prices refresh [--force] [--reset-observations]` | refresh the FMV table from the used market |
| `snipeklein prices show` | show the effective component table with provenance |
| `snipeklein prices import file.csv` | import manual corrections (`component_id;fmv_eur`) |
| `snipeklein listings [--hours N]` | ingested listings with margins |
| `snipeklein alerts [--band actionable]` | sent alerts |
| `snipeklein filter-report` | why listings were rejected (tuning aid) |
| `snipeklein usage [--days N]` | LLM requests / tokens / 429s per day |
| `snipeklein health` | configuration, database and quota state |
| `snipeklein triage <ad_id\|url>` | explain the cheap triage decision |
| `snipeklein extract` | smoke test the extraction against the live Gemini API |
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
case- and space-insensitively.

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
```

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
