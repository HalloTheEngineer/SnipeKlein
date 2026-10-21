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
