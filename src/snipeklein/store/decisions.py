"""Persistence of the decision trail (one row per ad, write-on-change)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..logging_setup import get_logger
from .db import utcnow

log = get_logger("snipeklein.store.decisions")

OUTCOME_REJECTED = "rejected"
OUTCOME_ACCEPTED = "accepted"
OUTCOME_ALERTED = "alerted"


@dataclass
class Decision:
    """One ad and everything the bot decided about it."""

    ad_id: str
    outcome: str
    stage: str
    reason: str = ""
    reason_code: str = ""
    query_tag: str = ""
    title: str = ""
    url: str = ""
    price: float | None = None
    location: str = ""
    snippet: str = ""
    is_pro: bool = False
    steps: list[dict[str, Any]] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Hash of everything that makes this decision interesting.

        The asking price is part of it: when a seller lowers the price the row
        is rewritten, otherwise the panel would show a stale price.
        """
        payload = json.dumps(
            {
                "outcome": self.outcome,
                "stage": self.stage,
                "reason": self.reason,
                "reason_code": self.reason_code,
                "title": self.title,
                "price": self.price,
                "location": self.location,
                "snippet": self.snippet,
                "steps": self.steps,
                "detail": self.detail,
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DecisionRepo:
    """Stores and queries the decision trail."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record(self, decision: Decision) -> bool:
        """Write the decision if it changed. Returns True when a row was written."""
        fingerprint = decision.fingerprint()
        now = utcnow()
        cur = self.conn.execute(
            """
            INSERT INTO ad_decisions (
                ad_id, first_seen, updated_at, query_tag, title, url, price, location,
                snippet, is_pro, outcome, stage, reason, reason_code, steps, detail,
                fingerprint
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ad_id) DO UPDATE SET
                updated_at  = excluded.updated_at,
                query_tag   = excluded.query_tag,
                title       = excluded.title,
                url         = excluded.url,
                price       = excluded.price,
                location    = excluded.location,
                is_pro      = excluded.is_pro,
                outcome     = excluded.outcome,
                stage       = excluded.stage,
                reason      = excluded.reason,
                reason_code = excluded.reason_code,
                steps       = excluded.steps,
                detail      = excluded.detail,
                fingerprint = excluded.fingerprint
            WHERE ad_decisions.fingerprint != excluded.fingerprint
            """,
            (
                decision.ad_id,
                now,
                now,
                decision.query_tag,
                decision.title,
                decision.url,
                decision.price,
                decision.location,
                decision.snippet,
                int(decision.is_pro),
                decision.outcome,
                decision.stage,
                decision.reason,
                decision.reason_code,
                json.dumps(decision.steps, ensure_ascii=False, default=str),
                json.dumps(decision.detail, ensure_ascii=False, default=str),
                fingerprint,
            ),
        )
        return bool(cur.rowcount)

    # -- queries -----------------------------------------------------------
    def list_decisions(
        self,
        limit: int = 50,
        offset: int = 0,
        outcome: str = "",
        stage: str = "",
        reason: str = "",
        query_tag: str = "",
        search: str = "",
        since: str = "",
        order: str = "updated",
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if outcome:
            where.append("outcome = ?")
            params.append(outcome)
        if stage:
            where.append("stage = ?")
            params.append(stage)
        if reason:
            where.append("reason_code LIKE ?")
            params.append(f"%{reason}%")
        if query_tag:
            where.append("query_tag = ?")
            params.append(query_tag)
        if search:
            where.append("(title LIKE ? OR ad_id LIKE ? OR location LIKE ?)")
            params.extend([f"%{search}%"] * 3)
        if since:
            where.append("updated_at >= ?")
            params.append(since)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = self.conn.execute(
            f"SELECT COUNT(*) AS c FROM ad_decisions{clause}", params
        ).fetchone()["c"]
        column = "first_seen" if order == "first" else "updated_at"
        rows = self.conn.execute(
            f"SELECT * FROM ad_decisions{clause} ORDER BY {column} DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_row_to_dict(row) for row in rows], int(total)

    def get(self, ad_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM ad_decisions WHERE ad_id = ?", (ad_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def counts(self) -> dict[str, Any]:
        by_outcome = {
            row["outcome"]: row["c"]
            for row in self.conn.execute(
                "SELECT outcome, COUNT(*) AS c FROM ad_decisions GROUP BY outcome"
            )
        }
        by_stage = {
            row["stage"]: row["c"]
            for row in self.conn.execute(
                "SELECT stage, COUNT(*) AS c FROM ad_decisions GROUP BY stage"
            )
        }
        by_reason = [
            {"reason": row["reason_code"] or "(none)", "count": row["c"]}
            for row in self.conn.execute(
                "SELECT reason_code, COUNT(*) AS c FROM ad_decisions "
                "GROUP BY reason_code ORDER BY c DESC LIMIT 25"
            )
        ]
        total = self.conn.execute("SELECT COUNT(*) AS c FROM ad_decisions").fetchone()["c"]
        return {
            "total": total,
            "by_outcome": by_outcome,
            "by_stage": by_stage,
            "by_reason": by_reason,
        }

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM ad_decisions ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def prune(self, retention_days: int = 14, max_rows: int = 100_000) -> dict[str, int]:
        """Delete old decisions and cap the table size."""
        removed: dict[str, int] = {}
        if retention_days > 0:
            cutoff = (
                datetime.now(UTC) - timedelta(days=retention_days)
            ).replace(microsecond=0).isoformat()
            cur = self.conn.execute("DELETE FROM ad_decisions WHERE updated_at < ?", (cutoff,))
            removed["expired"] = cur.rowcount or 0
        if max_rows > 0:
            total = self.conn.execute("SELECT COUNT(*) AS c FROM ad_decisions").fetchone()["c"]
            if total > max_rows:
                cur = self.conn.execute(
                    "DELETE FROM ad_decisions WHERE ad_id IN ("
                    "  SELECT ad_id FROM ad_decisions ORDER BY updated_at ASC LIMIT ?)",
                    (total - max_rows,),
                )
                removed["overflow"] = cur.rowcount or 0
        if removed:
            log.info("pruned decisions: %s", removed)
        return removed


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("steps", "detail"):
        try:
            data[key] = json.loads(data.get(key) or ("[]" if key == "steps" else "{}"))
        except (TypeError, ValueError):
            data[key] = [] if key == "steps" else {}
    data["is_pro"] = bool(data.get("is_pro"))
    return data
