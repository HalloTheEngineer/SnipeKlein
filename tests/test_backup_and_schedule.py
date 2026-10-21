"""Consistent backups and the daily schedule."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from snipeklein.schedule import next_daily_run, parse_hhmm
from snipeklein.store.backup import create_backup, prune_backups
from snipeklein.store.db import connect, migrate


def test_backup_is_a_consistent_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "snipeklein.db"
    conn = connect(db_path)
    migrate(conn)
    conn.execute("INSERT INTO seen_ads (ad_id, first_seen_at, last_seen_at) VALUES ('1','x','y')")
    conn.commit()

    prices = tmp_path / "prices.json"
    prices.write_text('{"version": "test"}', encoding="utf-8")

    created = create_backup(db_path, tmp_path / "backups", prices, keep=14)
    assert len(created) == 2

    backups = list((tmp_path / "backups").glob("snipeklein-*.db"))
    assert len(backups) == 1
    backup = backups[0]
    # the snapshot can be opened and contains the data
    restored = sqlite3.connect(str(backup))
    try:
        rows = restored.execute("SELECT COUNT(*) FROM seen_ads").fetchone()[0]
        tables = {
            r[0] for r in restored.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        restored.close()
    assert rows == 1
    assert "listings" in tables
    assert list((tmp_path / "backups").glob("prices-*.json"))


def test_old_backups_are_pruned(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    for index in range(5):
        (backups / f"snipeklein-2024010{index}.db").write_text("x", encoding="utf-8")
    removed = prune_backups(backups, keep=2)
    assert removed == 3
    assert len(list(backups.glob("snipeklein-*.db"))) == 2


def test_parse_hhmm() -> None:
    assert parse_hhmm("03:00") == (3, 0)
    assert parse_hhmm("23:59") == (23, 59)
    with pytest.raises(ValueError):
        parse_hhmm("25:00")
    with pytest.raises(ValueError):
        parse_hhmm("nonsense")


def test_next_daily_run() -> None:
    # before the window -> today
    assert next_daily_run(datetime(2026, 10, 21, 1, 30), 3, 0) == datetime(2026, 10, 21, 3, 0)
    # after the window -> tomorrow
    assert next_daily_run(datetime(2026, 10, 21, 3, 30), 3, 0) == datetime(2026, 10, 22, 3, 0)
    # exactly on the window -> tomorrow
    assert next_daily_run(datetime(2026, 10, 21, 3, 0), 3, 0) == datetime(2026, 10, 22, 3, 0)
