"""Consistent backups of the bot state.

Copying a WAL-mode SQLite file while the bot is writing can miss the
transactions that still live in the ``-wal`` file.  The online backup API
takes a consistent snapshot instead.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger("snipeklein.store.backup")


def create_backup(
    db_path: str | Path,
    backup_dir: str | Path,
    prices_path: str | Path | None = None,
    keep: int = 14,
) -> list[Path]:
    """Write a consistent snapshot of the database (and the price table).

    Returns the list of files that were created.
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    created: list[Path] = []

    if db_path.exists():
        target = backup_dir / f"snipeklein-{today}.db"
        source = sqlite3.connect(str(db_path), timeout=30)
        try:
            destination = sqlite3.connect(str(target))
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        created.append(target)
        log.info("wrote database backup %s", target)
    else:
        log.warning("no database at %s, skipping the database backup", db_path)

    if prices_path and Path(prices_path).exists():
        target = backup_dir / f"prices-{today}.json"
        target.write_bytes(Path(prices_path).read_bytes())
        created.append(target)
        log.info("wrote price table backup %s", target)

    prune_backups(backup_dir, keep)
    return created


def prune_backups(backup_dir: str | Path, keep: int = 14) -> int:
    """Delete old backups, keep the newest ``keep`` of each kind."""
    backup_dir = Path(backup_dir)
    if not backup_dir.exists() or keep <= 0:
        return 0
    removed = 0
    for pattern in ("snipeklein-*.db", "prices-*.json"):
        files = sorted(backup_dir.glob(pattern))
        for path in files[:-keep] if len(files) > keep else []:
            try:
                path.unlink()
                removed += 1
            except OSError:  # pragma: no cover
                log.exception("could not remove the backup %s", path)
    if removed:
        log.info("removed %s old backups", removed)
    return removed
