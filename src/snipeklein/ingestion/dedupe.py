"""Deduplication of already processed listings (spec 3.3)."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..logging_setup import get_logger
from ..store.repo import SeenStore

log = get_logger("snipeklein.ingestion.dedupe")


@dataclass
class DedupeStats:
    total: int = 0
    new: int = 0
    duplicates: int = 0
    reasons: dict[str, int] = field(default_factory=dict)


class Deduplicator:
    """Wraps the seen-id store and keeps counters for the filter report.

    A listing is marked as *seen* only after it was processed successfully, so
    a temporary failure does not hide a listing forever.  Listings that are
    rejected by the cheap triage are not marked either - re-checking them
    costs nothing and lets them become interesting after a price refresh.
    """

    def __init__(self, store: SeenStore, ttl_days: int = 30) -> None:
        self.store = store
        self.ttl_days = ttl_days
        self.stats = DedupeStats()

    def seen(self, ad_id: str) -> bool:
        self.stats.total += 1
        if self.store.was_seen(ad_id):
            self.stats.duplicates += 1
            return True
        return False

    def mark(self, ad_id: str, query_tag: str = "") -> None:
        self.store.claim(ad_id, query_tag)
        self.stats.new += 1

    def purge(self) -> int:
        removed = self.store.purge(self.ttl_days)
        if removed:
            log.info("purged %s expired seen-ids", removed)
        return removed

    def release(self, ad_id: str) -> None:
        """Undo a claim so the listing is retried in the next cycle."""
        self.store.release(ad_id)
