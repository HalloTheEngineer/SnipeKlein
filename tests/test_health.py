"""The health verdict: ok / starting / attention."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from snipeklein.config import (
    Config,
    FilterConfig,
    IngestionConfig,
    LlmConfig,
    NotifyConfig,
    RiskConfig,
    StorageConfig,
    ValuationConfig,
)
from snipeklein.health import (
    STATUS_ATTENTION,
    STATUS_OK,
    STATUS_STARTING,
    collect_health,
)
from snipeklein.store.db import connect, migrate


def _config(tmp_path: Path) -> Config:
    return Config(
        general={"db_path": str(tmp_path / "test.db")},
        ingestion=IngestionConfig(poll_interval_max_s=85),
        filters=FilterConfig(),
        valuation=ValuationConfig(),
        llm=LlmConfig(api_key_env="MISSING_KEY_FOR_TESTS"),
        notify=NotifyConfig(webhook_url_env="MISSING_HOOK_FOR_TESTS"),
        risk=RiskConfig(),
        storage=StorageConfig(),
    )


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "test.db")
    migrate(connection)
    return connection


def _insert_run(conn: sqlite3.Connection, started_at: datetime, errors: int = 0) -> None:
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, queries, ads_seen, ads_new, "
        "ads_evaluated, alerts_sent, errors) VALUES (?,?,?,?,?,?,?,?)",
        (
            started_at.isoformat(),
            started_at.isoformat(),
            "[]",
            10,
            1,
            1,
            0,
            errors,
        ),
    )


def test_no_run_yet_is_starting_not_attention(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """A fresh deployment must not look broken."""
    report = collect_health(_config(tmp_path), conn)
    assert report["status"] == STATUS_STARTING
    assert report["never_ran"] is True
    assert report["stale"] is False


def test_a_fresh_run_is_ok(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MISSING_KEY_FOR_TESTS", "key")
    monkeypatch.setenv("MISSING_HOOK_FOR_TESTS", "https://example.invalid/hook")
    _insert_run(conn, datetime.now(UTC) - timedelta(seconds=30))
    report = collect_health(_config(tmp_path), conn)
    assert report["status"] == STATUS_OK
    assert report["last_run_age_s"] is not None


def test_a_stale_run_is_attention(tmp_path: Path, conn: sqlite3.Connection) -> None:
    _insert_run(conn, datetime.now(UTC) - timedelta(hours=2))
    report = collect_health(_config(tmp_path), conn)
    assert report["status"] == STATUS_ATTENTION
    assert report["stale"] is True


def test_errors_in_the_last_run_are_attention(tmp_path: Path, conn: sqlite3.Connection) -> None:
    _insert_run(conn, datetime.now(UTC) - timedelta(seconds=10), errors=3)
    report = collect_health(_config(tmp_path), conn)
    assert report["status"] == STATUS_ATTENTION


def test_missing_secrets_are_attention(tmp_path: Path, conn: sqlite3.Connection) -> None:
    _insert_run(conn, datetime.now(UTC) - timedelta(seconds=10))
    report = collect_health(_config(tmp_path), conn)
    assert report["status"] == STATUS_ATTENTION
    assert "llm key" in report["missing"]
    assert "discord webhook" in report["missing"]
