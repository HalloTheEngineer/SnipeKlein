"""SQLite storage: connection handling and schema migrations."""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 7

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_ads (
    ad_id        TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    query_tag     TEXT DEFAULT '',
    status        TEXT DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS idx_seen_ads_last_seen ON seen_ads(last_seen_at);

CREATE TABLE IF NOT EXISTS ad_decisions (
    ad_id        TEXT PRIMARY KEY,
    first_seen   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    query_tag    TEXT DEFAULT '',
    title        TEXT DEFAULT '',
    url          TEXT DEFAULT '',
    price        REAL,
    card_price   REAL,
    location     TEXT DEFAULT '',
    snippet      TEXT DEFAULT '',
    is_pro       INTEGER DEFAULT 0,
    outcome      TEXT NOT NULL,
    stage        TEXT NOT NULL,
    reason       TEXT DEFAULT '',
    reason_code  TEXT DEFAULT '',
    steps        TEXT DEFAULT '[]',
    detail       TEXT DEFAULT '{}',
    fingerprint  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_decisions_updated ON ad_decisions(updated_at);
CREATE INDEX IF NOT EXISTS idx_decisions_outcome ON ad_decisions(outcome);
CREATE INDEX IF NOT EXISTS idx_decisions_stage ON ad_decisions(stage);

CREATE TABLE IF NOT EXISTS listings (
    ad_id            TEXT PRIMARY KEY,
    url              TEXT NOT NULL,
    title            TEXT NOT NULL,
    description      TEXT DEFAULT '',
    price            REAL,
    is_negotiable    INTEGER DEFAULT 0,
    original_price   REAL,
    plz              TEXT DEFAULT '',
    city             TEXT DEFAULT '',
    region           TEXT DEFAULT '',
    category_id      INTEGER,
    posted_at        TEXT DEFAULT '',
    fetched_at       TEXT NOT NULL,
    condition        TEXT DEFAULT '',
    condition_code   TEXT DEFAULT '',
    shipping_mode    TEXT DEFAULT 'unknown',
    seller_name      TEXT DEFAULT '',
    seller_since     TEXT DEFAULT '',
    seller_badges    TEXT DEFAULT '',
    seller_type      TEXT DEFAULT '',
    seller_active_ads INTEGER DEFAULT 0,
    ad_type          TEXT DEFAULT '',
    raw_json         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_listings_fetched ON listings(fetched_at);

CREATE TABLE IF NOT EXISTS extractions (
    ad_id          TEXT PRIMARY KEY,
    fmv_gross      REAL,
    fmv_net        REAL,
    margin_pct     REAL,
    band           TEXT,
    risk_score     INTEGER,
    risk_level     TEXT,
    coverage       REAL,
    components     TEXT DEFAULT '[]',
    defects        TEXT DEFAULT '[]',
    model          TEXT DEFAULT '',
    prompt_version TEXT DEFAULT '',
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_systems (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id     TEXT NOT NULL,
    ad_id        TEXT DEFAULT '',
    title        TEXT DEFAULT '',
    asking_eur   REAL,
    parts_sum_eur REAL,
    estimate_eur REAL,
    ratio        REAL,
    source       TEXT DEFAULT 'estimate',
    observed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_systems_build ON market_systems(build_id, observed_at);

CREATE TABLE IF NOT EXISTS price_observations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id TEXT NOT NULL,
    price_eur    REAL NOT NULL,
    source       TEXT NOT NULL,
    condition    TEXT DEFAULT '',
    observed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_obs_component ON price_observations(component_id, observed_at);

CREATE TABLE IF NOT EXISTS price_table (
    component_id TEXT PRIMARY KEY,
    fmv_eur      REAL NOT NULL,
    source       TEXT NOT NULL,
    confidence   REAL DEFAULT 1.0,
    sample_n     INTEGER DEFAULT 0,
    pinned       INTEGER DEFAULT 0,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    ad_id        TEXT PRIMARY KEY,
    band         TEXT,
    margin_pct   REAL,
    risk_level   TEXT,
    payload_hash TEXT,
    webhook_key  TEXT DEFAULT '',
    status       TEXT DEFAULT 'sent',
    sent_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    queries       TEXT DEFAULT '[]',
    ads_seen      INTEGER DEFAULT 0,
    ads_new       INTEGER DEFAULT 0,
    ads_evaluated INTEGER DEFAULT 0,
    alerts_sent   INTEGER DEFAULT 0,
    errors        INTEGER DEFAULT 0,
    error_detail  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS llm_usage (
    day        TEXT NOT NULL,
    model      TEXT NOT NULL,
    requests   INTEGER DEFAULT 0,
    in_tokens  INTEGER DEFAULT 0,
    out_tokens INTEGER DEFAULT 0,
    errors     INTEGER DEFAULT 0,
    quota_429  INTEGER DEFAULT 0,
    PRIMARY KEY (day, model)
);

CREATE TABLE IF NOT EXISTS filter_stats (
    day      TEXT NOT NULL,
    reason   TEXT NOT NULL,
    count    INTEGER DEFAULT 0,
    PRIMARY KEY (day, reason)
);
"""


def utcnow() -> str:
    """ISO-8601 timestamp in UTC (second resolution)."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def today() -> str:
    return datetime.now(UTC).date().isoformat()


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (and create) the SQLite database in WAL mode."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# Statements that upgrade an existing database to a newer schema version.
MIGRATIONS: list[tuple[int, list[str]]] = [
    (3, ["ALTER TABLE ad_decisions ADD COLUMN snippet TEXT DEFAULT ''"]),
    (
        7,
        ["ALTER TABLE market_systems ADD COLUMN estimate_eur REAL"],
    ),
    (
        6,
        ["ALTER TABLE market_systems ADD COLUMN source TEXT DEFAULT 'estimate'"],
    ),
    (
        5,
        [
            """CREATE TABLE IF NOT EXISTS market_systems (
                   id            INTEGER PRIMARY KEY AUTOINCREMENT,
                   build_id      TEXT NOT NULL,
                   ad_id         TEXT DEFAULT '',
                   title         TEXT DEFAULT '',
                   asking_eur    REAL,
                   parts_sum_eur REAL,
                   ratio         REAL,
                   observed_at   TEXT NOT NULL
               )""",
            "CREATE INDEX IF NOT EXISTS idx_market_systems_build "
            "ON market_systems(build_id, observed_at)",
        ],
    ),
    (
        4,
        [
            "ALTER TABLE ad_decisions ADD COLUMN card_price REAL",
            # v0.4.1: the stored price was the (possibly stale) search index
            # price, while the decision used the seller's current price from
            # the detail page.  The old price becomes the card price and the
            # real asking price comes from the listings table.
            (
                "UPDATE ad_decisions SET card_price = price, "
                "price = COALESCE((SELECT l.price FROM listings l "
                "WHERE l.ad_id = ad_decisions.ad_id), price) "
                "WHERE EXISTS (SELECT 1 FROM listings l WHERE l.ad_id = ad_decisions.ad_id)"
            ),
        ],
    ),
]


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


def migrate(conn: sqlite3.Connection) -> None:
    """Create the schema if needed and upgrade it to the current version."""
    conn.executescript(_SCHEMA)
    version = schema_version(conn)
    for target, statements in sorted(MIGRATIONS, key=lambda item: item[0]):
        if version >= target:
            continue
        for statement in statements:
            with contextlib.suppress(sqlite3.OperationalError):
                # the column may already exist - nothing to do then
                conn.execute(statement)
        version = target
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
