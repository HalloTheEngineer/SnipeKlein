"""Pipeline orchestration (spec 2).

    ingestion -> dedupe -> prefilter -> triage -> extraction
              -> valuation -> risk -> dispatcher
"""

from __future__ import annotations

import random
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .extract.extractor import Extractor
from .filters.prefilter import evaluate as evaluate_prefilter
from .ingestion.client import KleinanzeigenClient
from .ingestion.dedupe import Deduplicator
from .ingestion.detail_parser import DetailParseError, parse_listing
from .ingestion.queries import build_search_url
from .ingestion.search_parser import parse_cards
from .logging_setup import get_logger
from .models import Band, Deal, Extraction, SearchCard
from .notify.discord import DiscordError, DiscordNotifier
from .store.decisions import (
    OUTCOME_ACCEPTED,
    OUTCOME_ALERTED,
    OUTCOME_NEAR_MISS,
    OUTCOME_NOT_TARGET,
    OUTCOME_REJECTED,
    Decision,
    DecisionRepo,
)
from .store.market import MarketRepo
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

# Human readable texts for the machine codes of the filter stages.
REASON_TEXTS = {
    "title_exclude": "the title marks it as a wanted ad, exchange or defective item",
    "spec_exclude": "an older platform marker (DDR3/4, AM4, LGA1200, GTX, ...)",
    "no_system_marker": "the title does not look like a complete system",
    "no_component_marker": (
        "the title and the first line do not mention a component - the "
        "specifications are probably in the description"
    ),
    "bundle": (
        "a bundle (computer plus monitor and other accessories) whose "
        "specifications are not in the title - the price includes the "
        "accessories, so it cannot be valued"
    ),
    "empty_title": "the listing has no title",
    "triage_components": "fewer than two components could be recognised",
    "triage_margin": "not far enough below the market price",
    "triage_no_price": "the card has no price",
    "triage_no_value": "no component value could be derived",
    "not_a_complete_system": "it is not a complete system",
    "ram_generation": "the RAM is not DDR5",
    "ram_unknown": "the RAM generation was not mentioned - treated as not DDR5",
    "triage_modern_tier": "the price table does not know this system yet",
    "band": "the margin is not in the deal band",
    "coverage": "too much of the system is unknown",
    "no_price": "the listing has no asking price",
    "detail_fetch_failed": "the listing page could not be loaded",
    "detail_parse_failed": "the listing page could not be parsed",
    "alert_failed": "the alert could not be delivered",
    "alerted": "an alert was sent to Discord",
    "alert_skipped": "the alert was skipped",
    "dry_run": "dry run - no alert was sent",
}


def _extraction_reason(rejected: str) -> tuple[str, str]:
    """Split the extraction rejection into a code and a readable text.

    "the ad says DDR4" and "the ad never mentioned the RAM" are both a
    rejection, but only the first one is a real disqualification - the
    second one is a gap in the ad and is worth seeing in the panel.
    """
    code = rejected.split(":")[0] if rejected else "extraction"
    tail = rejected.split(":", 1)[1].strip() if ":" in rejected else ""
    if code == "ram_generation" and tail.upper() == "UNKNOWN":
        return (
            "ram_unknown",
            "the RAM generation was not mentioned - treated as not DDR5",
        )
    return code, _reason_text(rejected)


def _reason_text(code: str) -> str:
    """Turn a machine reason code into a readable sentence."""
    if not code:
        return ""
    head, _, tail = code.partition(":")
    text = REASON_TEXTS.get(head, head.replace("_", " "))
    if tail and head in ("triage_margin", "triage_components", "ram_generation"):
        text = f"{text} ({tail})"
    return text

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
        self.decisions = DecisionRepo(conn)

        self.table = PriceTable.load(config.prices_path, self.prices.all_prices())
        self.market = MarketRepo(conn)
        self.engine = ValuationEngine(
            table=self.table, config=config.valuation, market=self.market
        )
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
        self.engine = ValuationEngine(
            table=fresh, config=self.config.valuation, market=self.market
        )
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
        removed.update(
            self.decisions.prune(
                retention_days=self.config.panel.decision_retention_days,
                max_rows=self.config.panel.max_decision_rows,
            )
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
        steps: list[dict[str, Any]] = []
        detail: dict[str, Any] = {}

        pre = evaluate_prefilter(f"{card.title} {card.snippet}")
        steps.append(
            {"stage": "prefilter", "outcome": "passed" if pre.passed else "rejected"}
        )
        if not pre.passed:
            result.reject(pre.reason)
            self._record_decision(
                card,
                steps + [{"stage": "prefilter", "outcome": "rejected", "reason": pre.reason}],
                outcome=OUTCOME_REJECTED,
                stage="prefilter",
                reason=_reason_text(pre.reason),
                reason_code=pre.reason.split(":")[0],
            )
            return

        if self.dedupe.seen(card.ad_id):
            # already processed - the stored decision is still valid
            return

        triage = self.triage.evaluate(card)
        steps.append(
            {
                "stage": "triage",
                "outcome": "passed" if triage.passed else "rejected",
                "reason": triage.reason if not triage.passed else "",
                "note": triage.note,
                "coarse_fmv": round(triage.coarse_fmv, 2),
                "coarse_margin_pct": round(triage.coarse_margin_pct, 1),
            }
        )
        if not triage.passed:
            result.reject(triage.reason)
            self._record_decision(
                card,
                steps,
                outcome=OUTCOME_REJECTED,
                stage="triage",
                reason=_reason_text(triage.reason),
                reason_code=triage.reason.split(":")[0],
            )
            return

        try:
            html = self.client.get(card.url).text
        except Exception as exc:
            log.warning("detail fetch failed for %s: %s", card.ad_id, exc)
            result.errors += 1
            steps.append({"stage": "detail", "outcome": "error", "reason": str(exc)[:200]})
            self._record_decision(
                card,
                steps,
                outcome=OUTCOME_REJECTED,
                stage="detail",
                reason=f"could not load the listing: {exc}"[:200],
                reason_code="detail_fetch_failed",
            )
            return

        try:
            listing = parse_listing(html, card.url, self.config.ingestion.base_url)
        except DetailParseError as exc:
            log.warning("detail parse failed for %s: %s", card.ad_id, exc)
            result.errors += 1
            steps.append({"stage": "detail", "outcome": "error", "reason": str(exc)[:200]})
            self._record_decision(
                card,
                steps,
                outcome=OUTCOME_REJECTED,
                stage="detail",
                reason=f"could not parse the listing: {exc}"[:200],
                reason_code="detail_parse_failed",
            )
            return

        self.dedupe.mark(card.ad_id, card.query_tag)
        result.ads_new += 1
        self.listings.upsert(listing)
        steps.append({"stage": "detail", "outcome": "passed"})
        detail["listing"] = {
            "price": listing.price,
            "location": f"{listing.plz} {listing.city}".strip(),
            "shipping": listing.shipping_mode.value,
            "condition": listing.condition,
            "seller": listing.seller_name,
            "seller_since": listing.seller_since,
            "seller_type": listing.seller_type,
            "seller_badges": listing.seller_badges,
            "description": listing.description[:2000],
        }

        outcome = self.extractor.extract(listing.title, listing.description)
        detail["extraction"] = outcome.extraction.model_dump()
        detail["extraction_source"] = outcome.source
        if not outcome.ok:
            steps.append(
                {
                    "stage": "extraction",
                    "outcome": "rejected",
                    "reason": outcome.rejected,
                }
            )
            result.reject(outcome.rejected or "extraction_rejected")
            code, reason = _extraction_reason(outcome.rejected)
            self._record_decision(
                card,
                steps,
                outcome=OUTCOME_REJECTED,
                stage="extraction",
                reason=reason,
                reason_code=code,
                detail=detail,
                price=listing.price,
            )
            return
        steps.append({"stage": "extraction", "outcome": "passed"})

        valuation = self.engine.value(outcome.extraction, listing)
        result.ads_evaluated += 1
        risk = self.risk_engine.assess(listing, outcome.extraction, valuation)
        detail["valuation"] = valuation.model_dump()
        detail["risk"] = risk.model_dump()
        steps.append(
            {
                "stage": "valuation",
                "outcome": "passed",
                "band": valuation.band.value,
                "margin_pct": round(valuation.margin_pct, 1),
                "fmv_net": round(valuation.fmv_net, 2),
                "coverage": round(valuation.coverage, 3),
                "risk_score": risk.score,
                "risk_level": risk.level.value,
            }
        )

        self.extractions.upsert(
            listing.ad_id,
            outcome.extraction,
            valuation,
            risk.score,
            risk.level.value,
            outcome.model,
            "2026-10-20.v1",
        )

        is_near_miss = valuation.band is Band.NEAR_MISS
        # The specification targets DDR5 builds.  A listing that is not DDR5
        # is still valued and visible, it is just never alerted.
        if not self._is_target_spec(outcome.extraction):
            result.reject("not_ddr5")
            steps.append(
                {
                    "stage": "valuation",
                    "outcome": "not_target",
                    "reason": "not a DDR5 build - valued, but never alerted",
                }
            )
            self._record_decision(
                card,
                steps,
                outcome=OUTCOME_NOT_TARGET,
                stage="valuation",
                reason="not a DDR5 build - valued, but never alerted",
                reason_code="not_ddr5",
                detail=detail,
                price=listing.price,
            )
            return

        if valuation.band not in (Band.ACTIONABLE, Band.EXTREME, Band.NEAR_MISS):
            result.reject(f"band:{valuation.band.value}")
            steps.append(
                {
                    "stage": "valuation",
                    "outcome": "rejected",
                    "reason": f"margin {valuation.margin_pct:.1f}% is no deal",
                }
            )
            self._record_decision(
                card,
                steps,
                outcome=OUTCOME_REJECTED,
                stage="valuation",
                reason=self._band_reason(valuation.margin_pct),
                reason_code="band",
                detail=detail,
                price=listing.price,
            )
            return
        if valuation.coverage < self.config.valuation.min_coverage:
            result.reject("coverage")
            steps.append(
                {
                    "stage": "valuation",
                    "outcome": "rejected",
                    "reason": f"coverage {valuation.coverage:.0%} too low",
                }
            )
            self._record_decision(
                card,
                steps,
                outcome=OUTCOME_REJECTED,
                stage="valuation",
                reason=(
                    f"only {valuation.coverage:.0%} of the system is known - "
                    "too much is unknown"
                ),
                reason_code="coverage",
                detail=detail,
                price=listing.price,
            )
            return
        if valuation.asking_price <= 0:
            result.reject("no_price")
            self._record_decision(
                card,
                steps,
                outcome=OUTCOME_REJECTED,
                stage="valuation",
                reason="the listing has no asking price",
                reason_code="no_price",
                detail=detail,
                price=listing.price,
            )
            return

        deal = Deal(listing=listing, extraction=outcome.extraction, valuation=valuation, risk=risk)
        result.deals.append(deal)

        if is_near_miss:
            steps.append(
                {
                    "stage": "valuation",
                    "outcome": "near_miss",
                    "reason": (
                        f"{valuation.margin_pct:.1f}% below market - not a deal, but close"
                    ),
                }
            )

        if dry_run:
            log.info(
                "[dry-run] %s | margin %.1f%% | %s",
                listing.title[:60],
                valuation.margin_pct,
                risk.level.value,
            )
            steps.append(
                {"stage": "alert", "outcome": "skipped", "reason": "dry run"}
            )
            self._record_decision(
                card,
                steps,
                outcome=OUTCOME_NEAR_MISS if is_near_miss else OUTCOME_ACCEPTED,
                stage="valuation" if is_near_miss else "alert",
                reason=(
                    f"near miss: {valuation.margin_pct:.1f}% below market, not a deal"
                    if is_near_miss
                    else f"deal found: {valuation.margin_pct:.1f}% margin, risk {risk.level.value}"
                ),
                reason_code="near_miss" if is_near_miss else "dry_run",
                detail=detail,
                price=listing.price,
            )
            return

        try:
            sent = self.notifier.send(deal)
        except DiscordError as exc:
            log.error("could not send alert for %s: %s", listing.ad_id, exc)
            result.errors += 1
            steps.append({"stage": "alert", "outcome": "error", "reason": str(exc)[:200]})
            self._record_decision(
                card,
                steps,
                outcome=OUTCOME_ACCEPTED,
                stage="alert",
                reason=f"deal found, but the alert failed: {exc}"[:200],
                reason_code="alert_failed",
                detail=detail,
                price=listing.price,
            )
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
        steps.append(
            {"stage": "alert", "outcome": "sent" if sent else "skipped"}
        )
        if is_near_miss:
            decision_outcome = OUTCOME_NEAR_MISS
            decision_stage = "valuation"
            decision_code = "near_miss"
            decision_reason = (
                f"near miss: {valuation.margin_pct:.1f}% below market, not a deal"
            )
        else:
            decision_outcome = OUTCOME_ALERTED if sent else OUTCOME_ACCEPTED
            decision_stage = "alert"
            decision_code = "alerted" if sent else "alert_skipped"
            decision_reason = (
                f"alert sent: {valuation.margin_pct:.1f}% margin, risk {risk.level.value}"
            )
        self._record_decision(
            card,
            steps,
            outcome=decision_outcome,
            stage=decision_stage,
            reason=decision_reason,
            reason_code=decision_code,
            detail=detail,
            price=listing.price,
        )

    def _band_reason(self, margin_pct: float) -> str:
        """Describe the margin with the bands that are actually configured."""
        valuation = self.config.valuation
        return (
            f"margin {margin_pct:.1f}% - a deal is "
            f"{valuation.actionable_margin_min_pct:.0f}-{valuation.actionable_margin_max_pct:.0f} %, "
            f"a near miss is {valuation.near_miss_margin_min_pct:.0f}-"
            f"{valuation.actionable_margin_min_pct:.0f} %, above "
            f"{valuation.extreme_margin_pct:.0f} % is an outlier"
        )

    def _is_target_spec(self, extraction: Extraction) -> bool:
        """Is this a listing of the kind we are looking for (DDR5)?"""
        required = self.config.filters.required_ram_generation.upper()
        if required == "DDR5":
            return (extraction.ram_generation or "").upper() == "DDR5"
        return True

    def _record_decision(
        self,
        card: SearchCard,
        steps: list[dict[str, Any]],
        outcome: str,
        stage: str,
        reason: str = "",
        reason_code: str = "",
        detail: dict[str, Any] | None = None,
        price: float | None = None,
    ) -> None:
        """Persist what the bot decided about this ad (write-on-change).

        ``price`` is the asking price the decision was based on.  Once the
        detail page was read, that is the seller's current price - the search
        card can be stale (a cached index).  The card price is kept
        separately.
        """
        try:
            self.decisions.record(
                Decision(
                    ad_id=card.ad_id,
                    outcome=outcome,
                    stage=stage,
                    reason=reason,
                    reason_code=reason_code,
                    query_tag=card.query_tag,
                    title=card.title,
                    url=card.url,
                    price=card.price if price is None else price,
                    card_price=card.price,
                    location=card.location_text,
                    snippet=card.snippet[:400],
                    is_pro=card.is_pro,
                    steps=steps,
                    detail=detail or {},
                )
            )
        except sqlite3.Error:  # pragma: no cover - never break the pipeline
            log.exception("could not record the decision for %s", card.ad_id)

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
