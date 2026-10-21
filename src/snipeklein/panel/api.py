"""Read-only query layer for the panel."""

from __future__ import annotations

import json
import sqlite3
import statistics
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import Config
from ..health import collect_health
from ..logging_setup import get_logger
from ..store.decisions import DecisionRepo
from ..store.repo import AlertRepo, PriceRepo, RunRepo, UsageRepo
from ..valuation.table import PriceTable

log = get_logger("snipeklein.panel.api")


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open the database read-only - the panel must never write."""
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"no database at {path}")
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = 1")
        return conn
    except sqlite3.Error as exc:
        log.warning("read-only connection failed (%s), falling back to a normal one", exc)
        conn = sqlite3.connect(str(path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn


class DatabaseNotReady(RuntimeError):
    """The bot has not written the database yet."""


class PanelData:
    """All data the panel can show.

    The HTTP server is threaded, so every thread gets its own read-only
    connection (sqlite3 connections may not be shared between threads).
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._local = threading.local()

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if not Path(self.config.db_path).exists():
                raise DatabaseNotReady(
                    f"no database at {self.config.db_path} - the bot has to run first"
                )
            conn = open_readonly(self.config.db_path)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close the connection of the current thread."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- endpoints ---------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        decisions = DecisionRepo(self.conn).counts()
        runs = RunRepo(self.conn)
        last_run = runs.last()
        usage = UsageRepo(self.conn).daily(days=1)
        today_requests = sum(int(row["requests"]) for row in usage)
        today_tokens = sum(int(row["in_tokens"]) + int(row["out_tokens"]) for row in usage)
        alerts = self.conn.execute("SELECT COUNT(*) AS c FROM alerts").fetchone()["c"]
        listings = self.conn.execute("SELECT COUNT(*) AS c FROM listings").fetchone()["c"]
        prices = self.conn.execute("SELECT COUNT(*) AS c FROM price_table").fetchone()["c"]
        return {
            "decisions": decisions,
            "listings": listings,
            "alerts": alerts,
            "price_rows": prices,
            "llm_requests_today": today_requests,
            "llm_tokens_today": today_tokens,
            "last_run": last_run,
            "watchdog": self.watchdog(),
            "queries": [q.tag for q in self.config.queries],
        }

    def ads(
        self,
        limit: int = 50,
        offset: int = 0,
        outcome: str = "",
        stage: str = "",
        reason: str = "",
        query_tag: str = "",
        search: str = "",
        hours: int = 0,
        order: str = "updated",
    ) -> dict[str, Any]:
        since = ""
        if hours > 0:
            since = (
                datetime.now(UTC) - timedelta(hours=hours)
            ).replace(microsecond=0).isoformat()
        rows, total = DecisionRepo(self.conn).list_decisions(
            limit=limit,
            offset=offset,
            outcome=outcome,
            stage=stage,
            reason=reason,
            query_tag=query_tag,
            search=search,
            since=since,
            order=order,
        )
        return {"ads": rows, "total": total, "limit": limit, "offset": offset}

    def ad(self, ad_id: str) -> dict[str, Any] | None:
        decision = DecisionRepo(self.conn).get(ad_id)
        if decision is None:
            return None
        row = self.conn.execute(
            "SELECT * FROM listings WHERE ad_id = ?", (ad_id,)
        ).fetchone()
        if row is not None:
            decision["listing"] = dict(row)
        extraction = self.conn.execute(
            "SELECT * FROM extractions WHERE ad_id = ?", (ad_id,)
        ).fetchone()
        if extraction is not None:
            decision["extraction_row"] = dict(extraction)
        return decision

    def alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        return AlertRepo(self.conn).recent(limit=limit)

    def prices(self) -> list[dict[str, Any]]:
        """The effective component table - the values the bot actually uses."""
        table = PriceTable.load(self.config.prices_path, PriceRepo(self.conn).all_prices())
        rows = []
        for component in table.components:
            rows.append(
                {
                    "component_id": component.id,
                    "label": component.label,
                    "kind": component.kind,
                    "fmv_eur": round(component.fmv_eur, 2),
                    "curated_eur": round(component.curated_eur, 2),
                    "source": "market" if component.source == "kleinanzeigen" else "estimate",
                    "sample_n": component.sample_n,
                    "confidence": round(component.confidence, 3),
                    "pinned": component.pinned,
                    "aliases": component.aliases,
                    "tdp_w": component.tdp_w,
                    "updated_at": component.updated_at,
                }
            )
        rows.sort(key=lambda row: (str(row["kind"]), -float(str(row["fmv_eur"]))))
        return rows

    def price_detail(self, component_id: str) -> dict[str, Any] | None:
        """A component with its market samples, distribution and trend."""
        component = None
        for row in self.prices():
            if row["component_id"] == component_id:
                component = row
                break
        if component is None:
            return None

        samples = [
            {
                "price": float(row["price_eur"]),
                "title": row["condition"] or "",
                "at": row["observed_at"],
            }
            for row in self.conn.execute(
                "SELECT price_eur, condition, observed_at FROM price_observations "
                "WHERE component_id = ? ORDER BY observed_at",
                (component_id,),
            )
        ]
        prices = [sample["price"] for sample in samples]
        by_day: dict[str, list[float]] = {}
        for sample in samples:
            day = sample["at"][:10]
            by_day.setdefault(day, []).append(sample["price"])

        trend = [
            {
                "day": day,
                "median": round(statistics.median(values), 2),
                "n": len(values),
            }
            for day, values in sorted(by_day.items())[-14:]
        ]

        distribution: dict[str, Any] = {"count": len(prices)}
        if prices:
            distribution.update(
                {
                    "min": round(min(prices), 2),
                    "median": round(statistics.median(prices), 2),
                    "max": round(max(prices), 2),
                }
            )
        else:
            distribution.update({"min": None, "median": None, "max": None})

        return {
            "component": component,
            "distribution": distribution,
            "trend": trend,
            "samples": list(reversed(samples[-50:])),
        }

    def runs(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def health(self) -> dict[str, Any]:
        # The panel runs in its own container and cannot see the bot's
        # secrets, so it does not judge them.
        return collect_health(self.config, self.conn, check_secrets=False)

    def watchdog(self) -> dict[str, Any]:
        """The watchdog state file (read only)."""
        path = Path(self.config.db_path).parent / "watchdog_state.json"
        if not path.exists():
            return {"state": "unknown", "reason": "no watchdog state file"}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"state": "unknown", "reason": str(exc)}
