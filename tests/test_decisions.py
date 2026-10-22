"""The decision trail: write-on-change, filters, pruning."""

from __future__ import annotations

from pathlib import Path

import pytest

from snipeklein.store.db import connect, migrate
from snipeklein.store.decisions import (
    OUTCOME_ALERTED,
    OUTCOME_REJECTED,
    Decision,
    DecisionRepo,
)


@pytest.fixture()
def repo(tmp_path: Path) -> DecisionRepo:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    return DecisionRepo(conn)


def _decision(ad_id: str = "1", **kwargs) -> Decision:
    data = {
        "ad_id": ad_id,
        "outcome": OUTCOME_REJECTED,
        "stage": "prefilter",
        "reason": "older platform",
        "reason_code": "spec_exclude",
        "query_tag": "pc-gaming-ddr5",
        "title": "Gaming PC DDR4",
        "url": "https://example.invalid/1",
        "price": 500.0,
        "location": "12345 Teststadt",
        "steps": [{"stage": "prefilter", "outcome": "rejected", "reason": "spec_exclude"}],
    }
    data.update(kwargs)
    return Decision(**data)


def test_record_and_read(repo: DecisionRepo) -> None:
    assert repo.record(_decision()) is True
    stored = repo.get("1")
    assert stored is not None
    assert stored["outcome"] == OUTCOME_REJECTED
    assert stored["stage"] == "prefilter"
    assert stored["steps"][0]["stage"] == "prefilter"
    assert stored["detail"] == {}


def test_unchanged_decision_is_not_written_again(repo: DecisionRepo) -> None:
    """The row is only written when the decision actually changes."""
    assert repo.record(_decision()) is True
    assert repo.record(_decision()) is False
    assert repo.record(_decision(stage="triage", reason_code="triage_margin")) is True
    assert repo.get("1")["stage"] == "triage"


def test_the_snippet_is_stored(repo: DecisionRepo) -> None:
    """The panel shows what the triage actually saw."""
    repo.record(_decision("1", snippet="AMD Ryzen 7 7800X3D, RTX 4070, 32 GB DDR5"))
    stored = repo.get("1")
    assert stored is not None
    assert stored["snippet"] == "AMD Ryzen 7 7800X3D, RTX 4070, 32 GB DDR5"


def test_changed_price_updates_the_row(repo: DecisionRepo) -> None:
    repo.record(_decision())
    repo.record(_decision(price=450.0))
    assert repo.get("1")["price"] == 450.0


def test_filters(repo: DecisionRepo) -> None:
    repo.record(_decision("1", outcome=OUTCOME_REJECTED, stage="prefilter"))
    repo.record(
        _decision(
            "2",
            outcome=OUTCOME_ALERTED,
            stage="alert",
            reason_code="alerted",
            title="RTX 4070",
            query_tag="gpu-rtx40",
        )
    )
    rows, total = repo.list_decisions()
    assert total == 2
    rows, total = repo.list_decisions(outcome=OUTCOME_ALERTED)
    assert total == 1 and rows[0]["ad_id"] == "2"
    rows, total = repo.list_decisions(stage="prefilter")
    assert total == 1
    rows, total = repo.list_decisions(query_tag="gpu-rtx40")
    assert total == 1
    rows, total = repo.list_decisions(search="RTX 4070")
    assert total == 1
    rows, total = repo.list_decisions(reason="alert")
    assert total == 1


def test_counts(repo: DecisionRepo) -> None:
    repo.record(_decision("1"))
    repo.record(_decision("2", stage="triage", reason_code="triage_margin"))
    counts = repo.counts()
    assert counts["total"] == 2
    assert counts["by_outcome"][OUTCOME_REJECTED] == 2
    assert counts["by_stage"]["prefilter"] == 1
    assert counts["by_reason"][0]["count"] == 1


def test_prune_removes_old_rows(repo: DecisionRepo) -> None:
    repo.record(_decision("1"))
    repo.conn.execute("UPDATE ad_decisions SET updated_at = '2000-01-01T00:00:00+00:00'")
    removed = repo.prune(retention_days=14)
    assert removed["expired"] == 1
    assert repo.counts()["total"] == 0


def test_the_effective_price_and_the_card_price_are_kept_apart(repo: DecisionRepo) -> None:
    """The price the decision used must not be confused with the search index."""
    repo.record(_decision("1", price=1649.0, card_price=7.0))
    stored = repo.get("1")
    assert stored is not None
    assert stored["price"] == 1649.0
    assert stored["card_price"] == 7.0


def test_the_v4_migration_backfills_the_price(tmp_path: Path) -> None:
    """The rows decided before v0.4.1 get the price the decision used."""
    import sqlite3

    from snipeklein.store.db import migrate, schema_version

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE ad_decisions (
          ad_id TEXT PRIMARY KEY, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL,
          query_tag TEXT DEFAULT '', title TEXT DEFAULT '', url TEXT DEFAULT '', price REAL,
          location TEXT DEFAULT '', snippet TEXT DEFAULT '', is_pro INTEGER DEFAULT 0,
          outcome TEXT NOT NULL, stage TEXT NOT NULL, reason TEXT DEFAULT '',
          reason_code TEXT DEFAULT '', steps TEXT DEFAULT '[]', detail TEXT DEFAULT '{}',
          fingerprint TEXT DEFAULT '');
        CREATE TABLE listings (
          ad_id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL, price REAL,
          fetched_at TEXT NOT NULL);
        INSERT INTO schema_meta VALUES('schema_version','3');
        INSERT INTO ad_decisions (ad_id, first_seen, updated_at, outcome, stage, price)
          VALUES ('1','x','x','rejected','valuation',7.0);
        INSERT INTO listings (ad_id, url, title, price, fetched_at) VALUES ('1','u','t',1649.0,'x');
        """
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db_path)
    try:
        migrate(conn)
        assert schema_version(conn) >= 4
        row = conn.execute(
            "SELECT price, card_price FROM ad_decisions WHERE ad_id = '1'"
        ).fetchone()
        assert row == (1649.0, 7.0)
    finally:
        conn.close()
