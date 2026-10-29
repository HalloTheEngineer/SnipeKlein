"""Price harvester: refreshes the FMV table from the used market itself.

Only Kleinanzeigen is used as a source - the same session, the same parser.
One request per component, page 1 only, and the results are treated as asking
prices, so an ``asking_to_sold_factor`` is applied before they are stored.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Any

from ..config import QueryConfig, ValuationConfig
from ..filters.prefilter import is_bundle, is_system, is_wanted
from ..ingestion.client import KleinanzeigenClient
from ..ingestion.detail_parser import parse_listing
from ..ingestion.queries import build_search_url
from ..ingestion.search_parser import parse_cards
from ..logging_setup import get_logger
from ..models import SearchCard
from ..store.market import MarketRepo
from ..store.repo import PriceRepo
from .model import coverage_of
from .table import Component, PriceTable

log = get_logger("snipeklein.valuation.harvester")

CATEGORY_FOR_KIND = {
    "gpu": ("grafikkarten", 225),
    "ram": ("grafikkarten", 225),
    "storage": ("grafikkarten", 225),
    "board": ("grafikkarten", 225),
    "psu": ("grafikkarten", 225),
    # Standalone CPUs are sold in the "PC & Zubehör" category, not with the
    # complete systems.
    "cpu": ("grafikkarten", 225),
}

MIN_PRICE = 20.0
MAX_PRICE = 5000.0

ALL_KINDS = ("gpu", "cpu", "ram", "storage", "board", "psu")

# A component listing must not be a complete system - otherwise the harvest
# would measure whole PCs instead of the component.  Only the *title* is
# checked: the description of a CPU ad often mentions "Gaming PC".
SYSTEM_MARKERS = re.compile(
    r"\b(pc|rechner|computer|system|komplett|tower|workstation|desktop|server|"
    r"laptop|notebook|handy|tablet|monitor|all.?in.?one|konfiguration|konfigurator|"
    r"setup|bundle|paket)\b",
    re.IGNORECASE,
)

# Wanted ads and exchanges are not plain offers - their price is what someone
# is willing to pay, not what they ask.
WANTED_MARKERS = re.compile(
    r"(^|\b)(suche|gesucht|ich\s+suche|wtb|teile\s+mir|tausch|tausche|tauschen)\b",
    re.IGNORECASE,
)

# Accessories are not the component itself.
ACCESSORY_MARKERS = re.compile(
    r"\b(kühler|cooler|lüfter|ventilator|halterung|halter|adapter|kabel|cable|"
    r"pcie|riser|bracket|staubschutz|sticker|schutzfolie|heatsink|"
    r"waterblock|wasserkühler|rohling|dummy|platzhalter|"
    r"netzteil|psu|power\s?supply)\b",
    re.IGNORECASE,
)

# "CPU inkl. Kühler" is still a CPU - only accessories *for* the model or
# titles that start with the accessory are filtered out.
ACCESSORY_FOR = re.compile(
    r"(kühler|cooler|lüfter|halterung|adapter|kabel|heatsink)\s+(für|for|für\s+den)\b",
    re.IGNORECASE,
)
INCLUDED_MARKER = re.compile(r"(inkl|inklusive|mit|mit\s)[\s.,:]*$", re.IGNORECASE)

# Bundles: a CPU that is sold together with a board or a PSU does not have the
# price of the CPU alone.  Only structural bundle markers - the component
# words themselves are handled per kind by FOREIGN_MARKERS.
BUNDLE_MARKERS = re.compile(
    r"(\s\+\s|\bund\s+(netzteil|mainboard|motherboard|grafikkarte|ssd|ram|"
    r"arbeitsspeicher|gehäuse|kühler|cooler|ventilator|prozessor|cpu)\b|"
    r"\bmit\s+(netzteil|mainboard|motherboard|grafikkarte|gehäuse)\b)",
    re.IGNORECASE,
)

# Chipset names - a listing that mentions one is about a board as well.
CHIPSET_MARKERS = re.compile(r"\b([bxahz]\d{3}e?)\b", re.IGNORECASE)

# Defective units are not the reference for a working component.
DEFECT_MARKERS = re.compile(r"\b(defekt|kaputt|ersatzteil|defective|broken)\b", re.IGNORECASE)

# A listing that mentions a component of a *different* class is a bundle and
# does not reflect the price of the harvested component.
FOREIGN_MARKERS = {
    "cpu": re.compile(
        r"\b(rtx|gtx|radeon|geforce|grafikkarte|ssd|nvme|ram|arbeitsspeicher|"
        r"netzteil|mainboard|motherboard)\b",
        re.IGNORECASE,
    ),
    "gpu": re.compile(
        r"\b(ryzen|core\s?i|i[579][-\s]?\d{4,5}|ssd|nvme|ram|arbeitsspeicher|ddr[45]|"
        r"netzteil|mainboard|motherboard|prozessor|cpu)\b",
        re.IGNORECASE,
    ),
    "ram": re.compile(
        r"\b(ryzen|core\s?i|i[579][-\s]?\d{4,5}|rtx|gtx|radeon|geforce|grafikkarte|"
        r"netzteil|mainboard|motherboard)\b",
        re.IGNORECASE,
    ),
    "storage": re.compile(
        r"\b(ryzen|core\s?i|i[579][-\s]?\d{4,5}|rtx|gtx|radeon|geforce|grafikkarte|"
        r"netzteil|mainboard|motherboard|ram|arbeitsspeicher)\b",
        re.IGNORECASE,
    ),
    "board": re.compile(
        r"\b(ryzen|core\s?i|i[579][-\s]?\d{4,5}|rtx|gtx|radeon|geforce|grafikkarte|"
        r"ssd|nvme|netzteil)\b",
        re.IGNORECASE,
    ),
    "psu": re.compile(
        r"\b(ryzen|core\s?i|i[579][-\s]?\d{4,5}|rtx|gtx|radeon|geforce|grafikkarte|"
        r"ssd|nvme|ram|arbeitsspeicher)\b",
        re.IGNORECASE,
    ),
}

# New stock is not the second hand market we want to measure.
NEW_MARKERS = re.compile(
    r"(ovp|originalverpackt|originalverpack|unbenutzt|neuwertig|\bneu\b|new\b)",
    re.IGNORECASE,
)

# Guard against a broken harvest: never deviate more than this from the
# curated value (0 disables the guard).
MAX_DEVIATION = 0.5


@dataclass
class HarvestResult:
    component_id: str
    samples: int
    fmv_eur: float | None
    source: str = "kleinanzeigen"


@dataclass
class MarketResult:
    build_id: str
    samples: int
    median_ratio: float | None


class PriceHarvester:
    def __init__(
        self,
        client: KleinanzeigenClient,
        table: PriceTable,
        config: ValuationConfig,
        repo: PriceRepo,
        base_url: str = "https://www.kleinanzeigen.de",
        force: bool = False,
        curated: PriceTable | None = None,
        observation_days: int = 14,
        market: MarketRepo | None = None,
        extractor: Any = None,
        engine: Any = None,
    ) -> None:
        self.client = client
        self.table = table
        self.config = config
        self.repo = repo
        self.base_url = base_url
        self.force = force
        # The curated table (data/prices.json) is the reference the harvest is
        # compared against - the effective table may already contain stale
        # harvested values.
        self.curated = curated or table
        self.observation_days = observation_days
        self.market = market
        # the extractor and the engine let the market sample be measured the
        # same way the valuation measures a listing
        self.extractor = extractor
        self.engine = engine

    def harvest_market(
        self, kinds: tuple[str, ...] = ("gpu",), samples_per_build: int = 3
    ) -> list[MarketResult]:
        """Collect what complete systems with a given GPU really cost.

        The ratio between the asking price and the sum of the parts' market
        values is the *assembly factor* - it is measured here, not guessed.
        Wanted ads and bundles are left out: their price is not the price of
        the computer.

        The samples are extracted with the same extraction the valuation uses,
        so the ratio is comparable: a card that only names three of the seven
        parts would otherwise look far too expensive.
        """
        results: list[MarketResult] = []
        for component in self.table.components:
            if component.kind not in kinds or not component.aliases:
                continue
            try:
                samples = self._harvest_build(component, samples_per_build)
            except Exception as exc:  # pragma: no cover - network robustness
                log.warning("market harvest failed for %s: %s", component.id, exc)
                continue
            ratios = [sample.ratio for sample in samples]
            median = statistics.median(ratios) if ratios else None
            results.append(
                MarketResult(build_id=component.id, samples=len(samples), median_ratio=median)
            )
            if samples:
                log.info(
                    "market %s: %s systems, median ratio %.2f",
                    component.id,
                    len(samples),
                    median or 0.0,
                )
        return results

    def _harvest_build(self, gpu: Component, samples_per_build: int) -> list[Any]:
        keyword = f"{gpu.aliases[0]} gaming pc"
        query = QueryConfig(
            tag=f"market-{gpu.id}",
            category="pcs",
            category_id=228,
            keywords=[keyword],
            min_price=0,
            max_price=0,
            max_pages=1,
        )
        url = build_search_url(query, keyword, 1, self.base_url)
        cards = parse_cards(self.client.get(url).text, self.base_url, query.tag)

        samples: list[Any] = []
        for card in cards:
            if card.price is None:
                continue
            text = f"{card.title} {card.snippet}"
            if is_wanted(text) or is_bundle(text) or not is_system(text):
                continue
            parts = [
                component
                for component in self.table.components
                if component.kind not in ("peripheral", "accessory")
                and component.exact_match(text)
            ]
            if len(parts) < 2:
                continue
            # Only the ads the valuation would accept are a valid calibration
            # sample: a listing that names two of seven parts has an unknown
            # value, and its ratio would bias the factor.
            if coverage_of(self.table, parts) < self.config.min_coverage:
                continue

            sample = self._extract_sample(gpu, card, parts)
            if sample is not None:
                samples.append(sample)
            if len(samples) >= samples_per_build:
                break
        return samples

    def _extract_sample(self, gpu: Component, card: Any, parts: list[Component]) -> Any:
        """Read the listing, extract it like the valuation does and store the ratio."""
        if self.extractor is None or self.engine is None:
            return None
        try:
            listing = parse_listing(self.client.get(card.url).text, card.url, self.base_url)
        except Exception as exc:  # pragma: no cover - network robustness
            log.debug("could not read %s: %s", card.ad_id, exc)
            return None
        outcome = self.extractor.extract(listing.title, listing.description)
        if not outcome.ok:
            return None
        valuation = self.engine.value(outcome.extraction, listing)
        if valuation.coverage < self.config.min_coverage:
            return None
        if self.market is None:
            return None
        asking = listing.price or card.price
        return self.market.add(
            gpu.id,
            card.ad_id,
            card.title,
            asking,
            valuation.fmv_gross,
            estimate_eur=valuation.fmv_estimate,
            source="llm",
        )

    def harvest(self, kinds: tuple[str, ...] = ALL_KINDS) -> list[HarvestResult]:
        results: list[HarvestResult] = []
        for component in self.table.components:
            if component.kind not in kinds:
                continue
            if component.pinned:
                continue
            if not component.aliases:
                continue
            try:
                result = self._harvest_component(
                    component.id, component.aliases[0], component.kind, component
                )
            except Exception as exc:  # pragma: no cover - network robustness
                log.warning("harvest failed for %s: %s", component.id, exc)
                continue
            results.append(result)
        return results

    def _harvest_component(
        self, component_id: str, keyword: str, kind: str, component: Component
    ) -> HarvestResult:
        category, category_id = CATEGORY_FOR_KIND.get(kind, ("pcs", 228))
        query = QueryConfig(
            tag=f"harvest-{component_id}",
            category=category,
            category_id=category_id,
            keywords=[keyword],
            min_price=0,
            max_price=0,
            max_pages=1,
        )
        url = build_search_url(query, keyword, 1, self.base_url)
        response = self.client.get(url)
        cards = parse_cards(response.text, self.base_url, query_tag=query.tag)

        prices: list[float] = []
        for card in cards:
            if card.price is None:
                continue
            if not component.exact_match(f"{card.title} {card.snippet}"):
                continue
            if card.price < MIN_PRICE or card.price > MAX_PRICE:
                continue
            if self._is_complete_system(card, kind):
                continue
            if self._is_wanted_ad(card):
                continue
            if self._is_accessory(card):
                continue
            if self._is_new_stock(card):
                continue
            if self._is_pro_seller(card):
                continue
            if self._is_bundle(card, kind):
                continue
            prices.append(card.price)
            self.repo.add_observation(component_id, card.price, "kleinanzeigen", card.title[:80])

        # The stored observations give a larger (and more stable) sample than
        # a single search page.
        stored = self.repo.observations(component_id, days=self.observation_days)
        sample = stored or prices
        fmv, kept = self._robust_price(sample)
        if fmv is None or kept < self.config.min_price_samples:
            log.info("no usable samples for %s (%s raw)", component_id, len(prices))
            return HarvestResult(component_id=component_id, samples=len(prices), fmv_eur=None)
        curated = self.curated.get(component_id)
        if curated is not None and curated.fmv_eur > 0:
            deviation = abs(fmv - curated.fmv_eur) / curated.fmv_eur
            if deviation > MAX_DEVIATION and not self.force:
                log.warning(
                    "harvest for %s deviates %.0f%% from the curated value (%.0f vs %.0f) "
                    "- keeping the curated value",
                    component_id,
                    deviation * 100,
                    fmv,
                    curated.fmv_eur,
                )
                self.repo.set_price(
                    component_id,
                    curated.fmv_eur,
                    source="curated",
                    confidence=1.0,
                    sample_n=kept,
                )
                return HarvestResult(
                    component_id=component_id, samples=kept, fmv_eur=curated.fmv_eur
                )

        confidence = min(1.0, kept / max(1, self.config.min_price_samples * 2))
        self.repo.set_price(
            component_id,
            fmv,
            source="kleinanzeigen",
            confidence=round(confidence, 3),
            sample_n=kept,
        )
        log.info(
            "harvested %s: n=%s -> %.0f EUR (source=kleinanzeigen)",
            component_id,
            kept,
            fmv,
        )
        return HarvestResult(component_id=component_id, samples=kept, fmv_eur=fmv)

    def _is_complete_system(self, card: SearchCard, kind: str = "") -> bool:
        title = card.title
        if SYSTEM_MARKERS.search(title):
            return True
        if BUNDLE_MARKERS.search(title):
            return True
        if kind != "board" and CHIPSET_MARKERS.search(title):
            return True
        return bool(DEFECT_MARKERS.search(title))

    def _is_wanted_ad(self, card: SearchCard) -> bool:
        return bool(WANTED_MARKERS.search(card.title))

    def _is_accessory(self, card: SearchCard) -> bool:
        title = card.title
        if ACCESSORY_FOR.search(title):
            return True
        for match in ACCESSORY_MARKERS.finditer(title):
            prefix = title[max(0, match.start() - 12) : match.start()]
            if INCLUDED_MARKER.search(prefix):
                # "CPU inkl. Kühler" - the CPU is the subject
                continue
            return True
        return False

    def _is_new_stock(self, card: SearchCard) -> bool:
        return bool(NEW_MARKERS.search(card.title))

    def _is_pro_seller(self, card: SearchCard) -> bool:
        return bool(getattr(card, "is_pro", False))

    def _is_bundle(self, card: SearchCard, kind: str) -> bool:
        pattern = FOREIGN_MARKERS.get(kind)
        return bool(pattern and pattern.search(card.title))

    def _robust_price(self, prices: list[float]) -> tuple[float | None, int]:
        if len(prices) < self.config.min_price_samples:
            return None, len(prices)
        median = statistics.median(prices)
        mad = statistics.median([abs(p - median) for p in prices])
        kept = [p for p in prices if abs(p - median) <= 3 * mad] if mad > 0 else list(prices)
        if len(kept) < self.config.min_price_samples:
            return None, len(kept)
        return round(statistics.median(kept) * self.config.asking_to_sold_factor, 2), len(kept)


def import_prices_from_file(path: str, repo: PriceRepo, source: str = "import") -> int:
    """Import manual corrections from a CSV file (component_id, fmv_eur).

    Both ``,`` and ``;`` separated files are supported, prices may use the
    German decimal comma.
    """
    import csv

    with open(path, newline="", encoding="utf-8") as handle:
        first_line = handle.readline()
        handle.seek(0)
        delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
        count = 0
        for row in csv.DictReader(handle, delimiter=delimiter):
            component_id = (row.get("component_id") or row.get("id") or "").strip()
            raw_price = (row.get("fmv_eur") or row.get("price") or "").strip()
            if not component_id or not raw_price:
                continue
            price = _parse_decimal(raw_price)
            if price is None:
                continue
            repo.set_price(component_id, price, source=source, confidence=1.0)
            count += 1
    return count


def _parse_decimal(raw: str) -> float | None:
    cleaned = raw.replace("€", "").replace(" ", "").replace("EUR", "")
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned)
    except ValueError:
        return None
