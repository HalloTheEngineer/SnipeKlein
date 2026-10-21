"""Pipeline orchestration (spec 2).

    ingestion -> dedupe -> prefilter -> triage -> extraction
              -> valuation -> risk -> dispatcher
"""

from __future__ import annotations

import random
import sqlite3
import time
from dataclasses import dataclass, field

from .config import Config
from .extract.extractor import Extractor
from .filters.prefilter import evaluate as evaluate_prefilter
from .ingestion.client import KleinanzeigenClient
from .ingestion.dedupe import Deduplicator
from .ingestion.detail_parser import DetailParseError, parse_listing
from .ingestion.queries import build_search_url
from .ingestion.search_parser import parse_cards
from .logging_setup import get_logger
from .models import Band, Deal, SearchCard
from .notify.discord import DiscordError, DiscordNotifier
from .store.repo import (
    AlertRepo,
    ExtractionRepo,
    FilterStatsRepo,
    ListingRepo,
    PriceRepo,
    RunRepo,
    SeenStore,
    UsageRepo,
)
from .valuation.model import ValuationEngine
from .valuation.risk import RiskEngine
from .valuation.table import PriceTable
from .valuation.triage import Triage

log = get_logger("snipeklein.pipeline")

# Full database maintenance (listing/observation retention) every 6 hours.
MAINTENANCE_INTERVAL_S = 6 * 3600


@dataclass
class CycleResult:
    ads_seen: int = 0
    ads_new: int = 0
    ads_evaluated: int = 0
    alerts_sent: int = 0
    errors: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    deals: list[Deal] = field(default_factory=list)

    def reject(self, reason: str) -> None:
        key = reason.split(":")[0]
        self.rejected[key] = self.rejected.get(key, 0) + 1


class Pipeline:
    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        client: KleinanzeigenClient | None = None,
    ) -> None:
        self.config = config
        self.conn = conn
        self.seen = SeenStore(conn)
        self.dedupe = Deduplicator(self.seen, config.seen_ttl_days)
        self.listings = ListingRepo(conn)
        self.extractions = ExtractionRepo(conn)
        self.alerts = AlertRepo(conn)
        self.runs = RunRepo(conn)
        self.prices = PriceRepo(conn)
        self.usage = UsageRepo(conn)
        self.filter_stats = FilterStatsRepo(conn)

        self.table = PriceTable.load(config.prices_path, self.prices.all_prices())
        self.engine = ValuationEngine(table=self.table, config=config.valuation)
        self.risk_engine = RiskEngine(config.risk)
        self.triage = Triage(self.table, config.filters, self.engine)
        self.extractor = Extractor(
            config.llm,
            required_ram_generation=config.filters.required_ram_generation,
            require_complete_system=True,
        )
        self.extractor.factory.on_usage = self._record_usage
        self.client = client or KleinanzeigenClient(config.ingestion)
        self.notifier = DiscordNotifier(config.notify, self.alerts)

    # -- helpers -----------------------------------------------------------
    def _record_usage(
        self, model: str, requests: int, in_tokens: int, out_tokens: int, errors: int
    ) -> None:
        self.usage.record(model, requests, in_tokens, out_tokens, errors)

    def reload_prices(self) -> bool:
        """Reload the component table (the harvester may have updated it).

        Returns True when the effective table changed.
        """
        fresh = PriceTable.load(self.config.prices_path, self.prices.all_prices())
        before = {c.id: c.fmv_eur for c in self.table.components}
        after = {c.id: c.fmv_eur for c in fresh.components}
        if before == after:
            return False
        self.table = fresh
        self.engine = ValuationEngine(table=fresh, config=self.config.valuation)
        self.triage = Triage(fresh, self.config.filters, self.engine)
        changed = sum(1 for key, value in after.items() if before.get(key) != value)
        log.info("reloaded the component table (%s prices changed)", changed)
        return True

    def close(self) -> None:
        self.client.close()
        self.notifier.close()

    def maintenance(self) -> dict[str, int]:
        """Prune old rows so the database does not grow without bounds."""
        from .store.repo import MaintenanceRepo

        removed = MaintenanceRepo(self.conn).prune(
            seen_ttl_days=self.config.seen_ttl_days,
            observation_days=self.config.observation_retention_days,
            listing_days=self.config.listing_retention_days,
        )
        total = sum(removed.values())
        if total:
            log.info("maintenance: %s", ", ".join(f"{k}={v}" for k, v in removed.items() if v))
        return removed

    # -- main --------------------------------------------------------------
    def run_cycle(self, query_tags: list[str] | None = None, dry_run: bool = False) -> CycleResult:
        result = CycleResult()
        self.reload_prices()
        run_id = self.runs.start([q.tag for q in self._queries(query_tags)])

        for query in self._queries(query_tags):
            for keyword in query.keywords or [""]:
                for page in range(1, max(1, query.max_pages) + 1):
                    url = build_search_url(
                        query, keyword, page, self.config.ingestion.base_url
                    )
                    try:
                        response = self.client.get(url)
                    except Exception as exc:
                        log.error("search failed (%s): %s", url, exc)
                        result.errors += 1
                        continue
                    cards = parse_cards(
                        response.text, self.config.ingestion.base_url, query.tag
                    )
                    log.info("%s/%s -> %s cards", query.tag, keyword, len(cards))
                    for card in cards:
                        result.ads_seen += 1
                        self._process_card(card, result, dry_run)

        self.runs.finish(
            run_id,
            ads_seen=result.ads_seen,
            ads_new=result.ads_new,
            ads_evaluated=result.ads_evaluated,
            alerts_sent=result.alerts_sent,
            errors=result.errors,
        )
        for reason, count in result.rejected.items():
            self.filter_stats.record(reason, count)
        return result

    def _queries(self, tags: list[str] | None) -> list:
        queries = self.config.queries
        if not tags:
            return queries
        wanted = set(tags)
        selected = [q for q in queries if q.tag in wanted]
        if not selected:
            raise ValueError(f"unknown query tag(s): {', '.join(sorted(wanted))}")
        return selected

    def _process_card(self, card: SearchCard, result: CycleResult, dry_run: bool) -> None:
        pre = evaluate_prefilter(f"{card.title} {card.snippet}")
        if not pre.passed:
            result.reject(pre.reason)
            return

        if self.dedupe.seen(card.ad_id):
            return

        triage = self.triage.evaluate(card)
        if not triage.passed:
            result.reject(triage.reason)
            return

        try:
            html = self.client.get(card.url).text
        except Exception as exc:
            log.warning("detail fetch failed for %s: %s", card.ad_id, exc)
            result.errors += 1
            return

        try:
            listing = parse_listing(html, card.url, self.config.ingestion.base_url)
        except DetailParseError as exc:
            log.warning("detail parse failed for %s: %s", card.ad_id, exc)
            result.errors += 1
            return

        self.dedupe.mark(card.ad_id, card.query_tag)
        result.ads_new += 1
        self.listings.upsert(listing)

        outcome = self.extractor.extract(listing.title, listing.description)
        if not outcome.ok:
            result.reject(outcome.rejected or "extraction_rejected")
            return

        valuation = self.engine.value(outcome.extraction, listing)
        result.ads_evaluated += 1
        risk = self.risk_engine.assess(listing, outcome.extraction, valuation)

        self.extractions.upsert(
            listing.ad_id,
            outcome.extraction,
            valuation,
            risk.score,
            risk.level.value,
            outcome.model,
            "2026-10-20.v1",
        )

        if valuation.band not in (Band.ACTIONABLE, Band.EXTREME):
            result.reject(f"band:{valuation.band.value}")
            return
        if valuation.coverage < self.config.valuation.min_coverage:
            result.reject("coverage")
            return
        if valuation.asking_price <= 0:
            result.reject("no_price")
            return

        deal = Deal(listing=listing, extraction=outcome.extraction, valuation=valuation, risk=risk)
        result.deals.append(deal)

        if dry_run:
            log.info(
                "[dry-run] %s | margin %.1f%% | %s",
                listing.title[:60],
                valuation.margin_pct,
                risk.level.value,
            )
            return

        try:
            sent = self.notifier.send(deal)
        except DiscordError as exc:
            log.error("could not send alert for %s: %s", listing.ad_id, exc)
            result.errors += 1
            return
        if sent:
            result.alerts_sent += 1
            log.info(
                "ALERT %s | asking %.0f EUR | FMV %.0f EUR | margin %.1f%% | risk %s",
                listing.title[:60],
                valuation.asking_price,
                valuation.fmv_net,
                valuation.margin_pct,
                risk.level.value,
            )

    # -- long running ------------------------------------------------------
    def poll_forever(self, query_tags: list[str] | None = None, dry_run: bool = False) -> None:
        log.info("poll loop started")
        self.dedupe.purge()
        last_maintenance = time.monotonic()
        while True:
            started = time.monotonic()
            try:
                result = self.run_cycle(query_tags, dry_run=dry_run)
                log.info(
                    "cycle done: seen=%s new=%s evaluated=%s alerts=%s errors=%s",
                    result.ads_seen,
                    result.ads_new,
                    result.ads_evaluated,
                    result.alerts_sent,
                    result.errors,
                )
            except Exception:
                log.exception("cycle failed")
            self.dedupe.purge()
            if time.monotonic() - last_maintenance > MAINTENANCE_INTERVAL_S:
                last_maintenance = time.monotonic()
                self.maintenance()
            elapsed = time.monotonic() - started
            low = max(0.0, self.config.ingestion.poll_interval_min_s - elapsed)
            high = max(low, self.config.ingestion.poll_interval_max_s - elapsed)
            delay = random.uniform(low, high)
            log.info("next cycle in %.0f s", delay)
            time.sleep(delay)
