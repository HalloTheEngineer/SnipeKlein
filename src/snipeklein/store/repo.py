"""Repositories: typed access to the SQLite tables."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from ..models import Extraction, Listing, Valuation
from .db import today, utcnow


class SeenStore:
    """Deduplication state store (spec 3.3)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def claim(self, ad_id: str, query_tag: str = "") -> bool:
        """Return True if the id was seen for the first time."""
        now = utcnow()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO seen_ads(ad_id, first_seen_at, last_seen_at, query_tag, status) "
            "VALUES (?, ?, ?, ?, 'new')",
            (ad_id, now, now, query_tag),
        )
        if cur.rowcount == 0:
            self.conn.execute(
                "UPDATE seen_ads SET last_seen_at = ? WHERE ad_id = ?", (now, ad_id)
            )
            return False
        return True

    def was_seen(self, ad_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM seen_ads WHERE ad_id = ?", (ad_id,)).fetchone()
        return row is not None

    def release(self, ad_id: str) -> None:
        """Forget an id again (used when processing failed and should be retried)."""
        self.conn.execute("DELETE FROM seen_ads WHERE ad_id = ?", (ad_id,))

    def purge(self, ttl_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=ttl_days)).replace(
            microsecond=0
        ).isoformat()
        cur = self.conn.execute("DELETE FROM seen_ads WHERE last_seen_at < ?", (cutoff,))
        return cur.rowcount or 0

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM seen_ads").fetchone()
        return int(row["c"]) if row else 0


class ListingRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, listing: Listing, raw: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO listings (
                ad_id, url, title, description, price, is_negotiable, original_price,
                plz, city, region, category_id, posted_at, fetched_at, condition,
                condition_code, shipping_mode, seller_name, seller_since, seller_badges,
                seller_type, seller_active_ads, ad_type, raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ad_id) DO UPDATE SET
                url=excluded.url, title=excluded.title, description=excluded.description,
                price=excluded.price, is_negotiable=excluded.is_negotiable,
                original_price=excluded.original_price, plz=excluded.plz, city=excluded.city,
                region=excluded.region, category_id=excluded.category_id,
                posted_at=excluded.posted_at, fetched_at=excluded.fetched_at,
                condition=excluded.condition, condition_code=excluded.condition_code,
                shipping_mode=excluded.shipping_mode, seller_name=excluded.seller_name,
                seller_since=excluded.seller_since, seller_badges=excluded.seller_badges,
                seller_type=excluded.seller_type, seller_active_ads=excluded.seller_active_ads,
                ad_type=excluded.ad_type, raw_json=excluded.raw_json
            """,
            (
                listing.ad_id,
                listing.url,
                listing.title,
                listing.description,
                listing.price,
                int(listing.price_is_negotiable),
                listing.original_price,
                listing.plz,
                listing.city,
                listing.region,
                listing.category_id,
                listing.posted_at,
                listing.fetched_at.isoformat(),
                listing.condition,
                listing.condition_code,
                listing.shipping_mode.value,
                listing.seller_name,
                listing.seller_since,
                json.dumps(listing.seller_badges, ensure_ascii=False),
                listing.seller_type,
                listing.seller_active_ads,
                listing.ad_type,
                json.dumps(raw or {}, ensure_ascii=False),
            ),
        )

    def get(self, ad_id: str) -> Listing | None:
        row = self.conn.execute("SELECT * FROM listings WHERE ad_id = ?", (ad_id,)).fetchone()
        if row is None:
            return None
        return _row_to_listing(row)

    def recent(self, hours: int = 24, limit: int = 100) -> list[Listing]:
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM listings WHERE fetched_at >= ? ORDER BY fetched_at DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return [_row_to_listing(r) for r in rows]

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM listings").fetchone()
        return int(row["c"]) if row else 0


def _row_to_listing(row: sqlite3.Row) -> Listing:
    from ..models import ShippingMode

    try:
        badges = json.loads(row["seller_badges"] or "[]")
    except (TypeError, ValueError):
        badges = []
    mode = row["shipping_mode"] or "unknown"
    try:
        shipping = ShippingMode(mode)
    except ValueError:
        shipping = ShippingMode.UNKNOWN
    return Listing(
        ad_id=row["ad_id"],
        url=row["url"],
        title=row["title"],
        description=row["description"] or "",
        price=row["price"],
        price_is_negotiable=bool(row["is_negotiable"]),
        original_price=row["original_price"],
        plz=row["plz"] or "",
        city=row["city"] or "",
        region=row["region"] or "",
        category_id=row["category_id"],
        posted_at=row["posted_at"] or "",
        condition=row["condition"] or "",
        condition_code=row["condition_code"] or "",
        shipping_mode=shipping,
        seller_name=row["seller_name"] or "",
        seller_since=row["seller_since"] or "",
        seller_badges=[str(b) for b in badges],
        seller_type=row["seller_type"] or "",
        seller_active_ads=int(row["seller_active_ads"] or 0),
        ad_type=row["ad_type"] or "",
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
    )


class ExtractionRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(
        self,
        ad_id: str,
        extraction: Extraction,
        valuation: Valuation,
        risk_score: int,
        risk_level: str,
        model: str,
        prompt_version: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO extractions (
                ad_id, fmv_gross, fmv_net, margin_pct, band, risk_score, risk_level,
                coverage, components, defects, model, prompt_version, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ad_id) DO UPDATE SET
                fmv_gross=excluded.fmv_gross, fmv_net=excluded.fmv_net,
                margin_pct=excluded.margin_pct, band=excluded.band,
                risk_score=excluded.risk_score, risk_level=excluded.risk_level,
                coverage=excluded.coverage, components=excluded.components,
                defects=excluded.defects, model=excluded.model,
                prompt_version=excluded.prompt_version, created_at=excluded.created_at
            """,
            (
                ad_id,
                valuation.fmv_gross,
                valuation.fmv_net,
                valuation.margin_pct,
                valuation.band.value,
                risk_score,
                risk_level,
                valuation.coverage,
                json.dumps([c.model_dump() for c in valuation.components], ensure_ascii=False),
                json.dumps(extraction.defects_or_missing_parts, ensure_ascii=False),
                model,
                prompt_version,
                utcnow(),
            ),
        )

    def get(self, ad_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM extractions WHERE ad_id = ?", (ad_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        for key in ("components", "defects"):
            try:
                data[key] = json.loads(data[key] or "[]")
            except (TypeError, ValueError):
                data[key] = []
        return data


class AlertRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, ad_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM alerts WHERE ad_id = ?", (ad_id,)).fetchone()
        return dict(row) if row else None

    def record(
        self,
        ad_id: str,
        band: str,
        margin_pct: float,
        risk_level: str,
        payload_hash: str,
        webhook_key: str = "",
        status: str = "sent",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO alerts (ad_id, band, margin_pct, risk_level, payload_hash, webhook_key,
                                status, sent_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(ad_id) DO UPDATE SET
                band=excluded.band, margin_pct=excluded.margin_pct,
                risk_level=excluded.risk_level, payload_hash=excluded.payload_hash,
                webhook_key=excluded.webhook_key, status=excluded.status,
                sent_at=excluded.sent_at
            """,
            (ad_id, band, margin_pct, risk_level, payload_hash, webhook_key, status, utcnow()),
        )

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM alerts ORDER BY sent_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


class RunRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def start(self, queries: Iterable[str]) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, queries) VALUES (?, ?)",
            (utcnow(), json.dumps(list(queries))),
        )
        return int(cur.lastrowid or 0)

    def finish(self, run_id: int, **counters: Any) -> None:
        allowed = {
            "ads_seen",
            "ads_new",
            "ads_evaluated",
            "alerts_sent",
            "errors",
            "error_detail",
        }
        fields = {k: v for k, v in counters.items() if k in allowed}
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [utcnow(), run_id]
        self.conn.execute(
            f"UPDATE runs SET {assignments}, finished_at = ? WHERE id = ?", values
        )

    def last(self) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


class PriceRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_observation(
        self,
        component_id: str,
        price_eur: float,
        source: str,
        condition: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT INTO price_observations (component_id, price_eur, source, condition, observed_at) "
            "VALUES (?,?,?,?,?)",
            (component_id, price_eur, source, condition, utcnow()),
        )

    def observations(self, component_id: str, days: int = 30) -> list[float]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT price_eur FROM price_observations WHERE component_id = ? AND observed_at >= ? "
            "ORDER BY observed_at",
            (component_id, cutoff),
        ).fetchall()
        return [float(r["price_eur"]) for r in rows]

    def set_price(
        self,
        component_id: str,
        fmv_eur: float,
        source: str,
        confidence: float = 1.0,
        sample_n: int = 0,
        pinned: bool = False,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO price_table (component_id, fmv_eur, source, confidence, sample_n, pinned, updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(component_id) DO UPDATE SET
                fmv_eur=excluded.fmv_eur, source=excluded.source, confidence=excluded.confidence,
                sample_n=excluded.sample_n, pinned=excluded.pinned, updated_at=excluded.updated_at
            """,
            (component_id, fmv_eur, source, confidence, sample_n, int(pinned), utcnow()),
        )

    def all_prices(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM price_table").fetchall()
        return {r["component_id"]: dict(r) for r in rows}


class UsageRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record(
        self,
        model: str,
        requests: int = 0,
        in_tokens: int = 0,
        out_tokens: int = 0,
        errors: int = 0,
        quota_429: int = 0,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO llm_usage (day, model, requests, in_tokens, out_tokens, errors, quota_429)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(day, model) DO UPDATE SET
                requests = requests + excluded.requests,
                in_tokens = in_tokens + excluded.in_tokens,
                out_tokens = out_tokens + excluded.out_tokens,
                errors = errors + excluded.errors,
                quota_429 = quota_429 + excluded.quota_429
            """,
            (today(), model, requests, in_tokens, out_tokens, errors, quota_429),
        )

    def daily(self, days: int = 7) -> list[dict[str, Any]]:
        """Usage of the last ``days`` days, one row per day *and* model."""
        days = max(1, int(days))
        cutoff = (datetime.now(UTC) - timedelta(days=days - 1)).date().isoformat()
        rows = self.conn.execute(
            "SELECT * FROM llm_usage WHERE day >= ? ORDER BY day DESC, model",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]


class MaintenanceRepo:
    """Housekeeping: keep the database from growing without bounds."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def prune(
        self,
        seen_ttl_days: int = 30,
        observation_days: int = 30,
        listing_days: int = 30,
        run_days: int = 14,
    ) -> dict[str, int]:
        removed: dict[str, int] = {}

        cutoff = (datetime.now(UTC) - timedelta(days=observation_days)).isoformat()
        cur = self.conn.execute("DELETE FROM price_observations WHERE observed_at < ?", (cutoff,))
        removed["price_observations"] = cur.rowcount or 0

        cutoff = (datetime.now(UTC) - timedelta(days=listing_days)).isoformat()
        cur = self.conn.execute("DELETE FROM extractions WHERE created_at < ?", (cutoff,))
        removed["extractions"] = cur.rowcount or 0
        cur = self.conn.execute("DELETE FROM listings WHERE fetched_at < ?", (cutoff,))
        removed["listings"] = cur.rowcount or 0

        cutoff = (datetime.now(UTC) - timedelta(days=run_days)).isoformat()
        cur = self.conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
        removed["runs"] = cur.rowcount or 0

        cur = self.conn.execute("DELETE FROM filter_stats WHERE day < date('now', ?)", ("-30 days",))
        removed["filter_stats"] = cur.rowcount or 0

        cur = self.conn.execute("DELETE FROM llm_usage WHERE day < date('now', ?)", ("-90 days",))
        removed["llm_usage"] = cur.rowcount or 0

        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return removed


class FilterStatsRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record(self, reason: str, count: int = 1) -> None:
        self.conn.execute(
            """
            INSERT INTO filter_stats (day, reason, count) VALUES (?,?,?)
            ON CONFLICT(day, reason) DO UPDATE SET count = count + excluded.count
            """,
            (today(), reason, count),
        )

    def report(self, days: int = 7) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT reason, SUM(count) AS count FROM filter_stats "
            "WHERE day >= date('now', ?) GROUP BY reason ORDER BY count DESC",
            (f"-{days} days",),
        ).fetchall()
        return [dict(r) for r in rows]
