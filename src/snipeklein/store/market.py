"""The complete-system market: what a system with a given GPU really costs.

The component prices alone do not say what a ready-to-use machine is worth:
the market asks a certain multiple of the sum of its parts.  The observations
in this table are used to calibrate that multiple (the *assembly factor*) and
to place a listing inside the price distribution of its configuration.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..logging_setup import get_logger
from .db import utcnow

log = get_logger("snipeklein.store.market")


@dataclass
class MarketSample:
    build_id: str
    ad_id: str
    title: str
    asking_eur: float
    parts_sum_eur: float
    ratio: float
    observed_at: str


class MarketRepo:
    """Stores and queries the system market observations."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add(
        self,
        build_id: str,
        ad_id: str,
        title: str,
        asking_eur: float,
        parts_sum_eur: float,
        estimate_eur: float = 0.0,
        source: str = "estimate",
    ) -> MarketSample | None:
        if asking_eur <= 0 or parts_sum_eur <= 0:
            return None
        # the ratio is measured against the *estimate* - the value of a
        # complete system with these parts - so it does not depend on how many
        # parts an ad happens to name
        basis = estimate_eur or parts_sum_eur
        ratio = asking_eur / basis
        now = utcnow()
        self.conn.execute(
            "INSERT INTO market_systems (build_id, ad_id, title, asking_eur, parts_sum_eur, "
            "estimate_eur, ratio, source, observed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                build_id,
                ad_id,
                title[:120],
                asking_eur,
                parts_sum_eur,
                estimate_eur or parts_sum_eur,
                ratio,
                source,
                now,
            ),
        )
        return MarketSample(build_id, ad_id, title[:120], asking_eur, parts_sum_eur, ratio, now)

    def set_extracted(
        self, build_id: str, ad_id: str, parts_sum_eur: float, source: str = "llm"
    ) -> None:
        """Replace the estimated parts' sum with the extracted one."""
        if parts_sum_eur <= 0:
            return
        self.conn.execute(
            "UPDATE market_systems SET parts_sum_eur = ?, ratio = asking_eur / ?, source = ? "
            "WHERE build_id = ? AND ad_id = ?",
            (parts_sum_eur, parts_sum_eur, source, build_id, ad_id),
        )

    def ratios(self, build_id: str = "", days: int = 14, source: str = "llm") -> list[float]:
        """The observed ratios - only the extracted ones are comparable."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).replace(microsecond=0).isoformat()
        clause = "build_id = ? AND " if build_id else ""
        params: list[Any] = [source, cutoff]
        if build_id:
            params.insert(1, build_id)
        rows = self.conn.execute(
            f"SELECT ratio FROM market_systems WHERE {clause}source = ? AND observed_at >= ? "
            "AND ratio > 0",
            params,
        ).fetchall()
        return [float(row["ratio"]) for row in rows]

    def asking_prices(self, build_id: str = "", days: int = 14) -> list[float]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).replace(microsecond=0).isoformat()
        if build_id:
            rows = self.conn.execute(
                "SELECT asking_eur FROM market_systems WHERE build_id = ? AND observed_at >= ? "
                "AND asking_eur > 0",
                (build_id, cutoff),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT asking_eur FROM market_systems WHERE observed_at >= ? AND asking_eur > 0",
                (cutoff,),
            ).fetchall()
        return [float(row["asking_eur"]) for row in rows]

    def assembly_factor(
        self, build_id: str = "", min_samples: int = 8, days: int = 14, fallback: float = 1.0
    ) -> tuple[float, int]:
        """The calibrated multiple of the parts' sum that the market asks for.

        A per-build factor needs enough samples of its own; until then the
        factor measured across all builds is used.
        """
        ratios = self.ratios(build_id, days)
        if len(ratios) < min_samples:
            ratios = self.ratios("", days)
            if len(ratios) < min_samples:
                return fallback, len(ratios)
        trimmed = _trim_outliers(ratios)
        return round(statistics.median(trimmed), 4), len(ratios)

    def price_rank(
        self, asking_eur: float, build_id: str = "", days: int = 14, min_samples: int = 5
    ) -> float | None:
        """Where the asking price sits in the market distribution (0-100 %).

        The per build distribution is only meaningful with enough samples;
        until then the distribution of all builds is used.
        """
        prices = self.asking_prices(build_id, days)
        if len(prices) < min_samples and build_id:
            prices = self.asking_prices("", days)
        if not prices or asking_eur <= 0:
            return None
        below = sum(1 for price in prices if price > asking_eur)
        return round(below / len(prices) * 100, 1)

    def distribution(self, build_id: str = "", days: int = 14) -> dict[str, Any]:
        prices = sorted(self.asking_prices(build_id, days))
        if not prices:
            return {"count": 0, "min": None, "median": None, "max": None, "p25": None, "p75": None}
        return {
            "count": len(prices),
            "min": prices[0],
            "median": statistics.median(prices),
            "max": prices[-1],
            "p25": prices[max(0, int(len(prices) * 0.25) - 1)],
            "p75": prices[min(len(prices) - 1, int(len(prices) * 0.75))],
        }

    def prune(self, days: int = 30) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).replace(microsecond=0).isoformat()
        cur = self.conn.execute("DELETE FROM market_systems WHERE observed_at < ?", (cutoff,))
        return cur.rowcount or 0


def _trim_outliers(values: list[float], factor: float = 3.0) -> list[float]:
    """Median absolute deviation trim - the same estimator the harvester uses."""
    if len(values) < 3:
        return list(values)
    median = statistics.median(values)
    mad = statistics.median([abs(value - median) for value in values])
    if mad <= 0:
        return list(values)
    return [value for value in values if abs(value - median) <= factor * mad]
