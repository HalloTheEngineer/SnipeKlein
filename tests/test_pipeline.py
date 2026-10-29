"""End to end pipeline test with a fake client (no network access)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from snipeklein.config import (
    Config,
    FilterConfig,
    IngestionConfig,
    LlmConfig,
    NotifyConfig,
    QueryConfig,
    RiskConfig,
    ValuationConfig,
)
from snipeklein.ingestion.client import Response
from snipeklein.pipeline import Pipeline
from snipeklein.store.db import connect, migrate
from snipeklein.store.repo import ExtractionRepo, ListingRepo

AD_ID = "3999999999"
AD_URL = "https://www.kleinanzeigen.de/s-anzeige/gaming-pc/3999999999-228-1"

SEARCH_HTML = f"""
<html><body>
<article class="flex" data-adid="{AD_ID}" data-href="/s-anzeige/gaming-pc/3999999999-228-1">
  <h3><a href="/s-anzeige/gaming-pc/3999999999-228-1">
    Gaming PC Ryzen 7 7800X3D RTX 4070 32GB DDR5 1TB NVMe</a></h3>
  <p class="mb-xsmall text-bodyRegular text-onSurfaceSubdued">
    Kompletter Gaming-PC mit Ryzen 7 7800X3D, RTX 4070, 32 GB DDR5 und 1 TB NVMe.</p>
  <div class="flex">
    <p class="my-xsmall text-title3 font-strong text-secondary">700 € VB</p>
  </div>
  <span>12345 Berlin</span>
</article>
</body></html>
"""

PROPS = {
    "data": {
        "id": AD_ID,
        "categoryId": 228,
        "price": {"amount": 700, "type": "NEGOTIABLE"},
        "location": "12345 Berlin - Mitte",
        "formattedCreationDate": datetime.now().strftime("%d.%m.%Y"),
        "localizedAttributes": [
            {"localizedName": "Zustand", "localizedValue": "Sehr Gut"},
            {"localizedName": "Versand", "localizedValue": "Nur Abholung"},
        ],
        "userDetails": {
            "userId": 42,
            "contactName": "Test Seller",
            "userSince": "01.01.2015",
            "commercial": False,
        },
    }
}

DETAIL_HTML = f"""
<html><body>
<h1 id="viewad-title" class="boxedarticle--title" itemprop="name">
  Gaming PC Ryzen 7 7800X3D RTX 4070 32GB DDR5 1TB NVMe</h1>
<h2 class="boxedarticle--price" id="viewad-price">700 € VB</h2>
<span id="viewad-locality" itemprop="addressLocality">12345 Berlin - Mitte</span>
<div id="viewad-details"><ul><li class="addetailslist--detail">Zustand<span>Sehr Gut</span></li></ul></div>
<div id="viewad-description-text">
  AMD Ryzen 7 7800X3D, NVIDIA GeForce RTX 4070, 32 GB DDR5 RAM,
  1 TB NVMe SSD, B650 Mainboard, 750W Netzteil, Windows 11. Nur Abholung.</div>
<astro-island props="{json.dumps(PROPS).replace('"', '&quot;')}"></astro-island>
</body></html>
"""


class FakeClient:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def get(self, url: str, params: dict[str, str] | None = None) -> Response:
        self.requested.append(url)
        if "/s-anzeige/" in url:
            return Response(url=url, status=200, text=DETAIL_HTML, elapsed_s=0.1)
        return Response(url=url, status=200, text=SEARCH_HTML, elapsed_s=0.1)

    def close(self) -> None:
        pass


def _config(tmp_path: Path) -> Config:
    return Config(
        general={"db_path": str(tmp_path / "test.db"), "log_level": "WARNING"},
        ingestion=IngestionConfig(base_url="https://www.kleinanzeigen.de"),
        filters=FilterConfig(required_ram_generation="DDR5", triage_min_margin_pct=15.0),
        valuation=ValuationConfig(),
        llm=LlmConfig(api_key_env="MISSING_KEY_FOR_TESTS"),
        notify=NotifyConfig(),
        risk=RiskConfig(),
        queries=[
            QueryConfig(
                tag="test",
                category="pcs",
                category_id=228,
                keywords=["gaming pc"],
                min_price=300,
                max_price=1800,
            )
        ],
    )


@pytest.fixture()
def pipeline(tmp_path: Path):
    config = _config(tmp_path)
    conn = connect(config.db_path)
    migrate(conn)
    pipe = Pipeline(config, conn, client=FakeClient())
    yield pipe
    pipe.close()
    conn.close()


def test_pipeline_evaluates_a_deal(pipeline: Pipeline) -> None:
    result = pipeline.run_cycle(dry_run=True)
    assert result.ads_seen == 1
    assert result.ads_new == 1
    assert result.errors == 0
    assert len(result.deals) == 1

    deal = result.deals[0]
    assert deal.listing.ad_id == AD_ID
    assert deal.listing.price == 700
    assert deal.listing.shipping_mode.value == "pickup"
    assert deal.extraction.ram_generation == "DDR5"
    assert deal.extraction.cpu_model and "7800X3D" in deal.extraction.cpu_model
    assert deal.valuation.band.value in ("actionable", "extreme")
    assert deal.valuation.margin_pct > 30


def test_pipeline_persists_listing_and_extraction(pipeline: Pipeline) -> None:
    pipeline.run_cycle(dry_run=True)
    listing = ListingRepo(pipeline.conn).get(AD_ID)
    assert listing is not None
    assert listing.title.startswith("Gaming PC")
    assert listing.seller_name == "Test Seller"
    extraction = ExtractionRepo(pipeline.conn).get(AD_ID)
    assert extraction is not None
    assert extraction["band"] in ("actionable", "extreme")


def test_pipeline_does_not_send_alerts_in_dry_run(pipeline: Pipeline) -> None:
    result = pipeline.run_cycle(dry_run=True)
    assert result.alerts_sent == 0


def test_the_decision_uses_the_detail_price_not_the_card_price(pipeline: Pipeline) -> None:
    """A stale search card price must not end up next to the valuation."""
    from snipeklein.models import SearchCard
    from snipeklein.pipeline import CycleResult
    from snipeklein.store.decisions import DecisionRepo

    card = SearchCard(
        ad_id=AD_ID,
        url=f"https://example.invalid/s-anzeige/gaming-pc/{AD_ID}-228-1",
        title="Gaming PC Ryzen 7 7800X3D RTX 4070 32GB DDR5 1TB NVMe",
        price=7.0,
        location_text="12345 Teststadt",
        query_tag="test",
    )
    result = CycleResult()
    pipeline._process_card(card, result, dry_run=True)  # noqa: SLF001

    decision = DecisionRepo(pipeline.conn).get(AD_ID)
    assert decision is not None
    # the detail page said 700 EUR - that is the price the decision used
    assert decision["price"] == 700.0
    # the search index said 7 EUR - kept separately
    assert decision["card_price"] == 7.0


def test_price_reload_keeps_the_market_calibration(pipeline: Pipeline) -> None:
    """The nightly price refresh must not silently drop the assembly factor.

    The bot reloads the component table whenever the harvester changed a
    price.  Rebuilding the engine without the complete-system market would
    make every following valuation fall back to factor 1.0 and lose the
    price rank - invisibly, until the next restart.
    """
    for index in range(10):
        pipeline.market.add(
            "NVIDIA_RTX_4070_12GB",
            f"market-{index}",
            "Gaming PC RTX 4070",
            1200.0,
            1000.0,
            estimate_eur=1000.0,
            source="llm",
        )
    pipeline.prices.set_price("AMD_RYZEN_7_7800X3D", 999.0, source="market", sample_n=5)
    assert pipeline.reload_prices() is True
    assert pipeline.engine.market is pipeline.market

    result = pipeline.run_cycle(dry_run=True)
    deal = result.deals[0]
    assert deal.valuation.assembly_samples == 10
    # the measured asking ratio (1.2) converted to a sold price
    assert deal.valuation.assembly_factor == pytest.approx(1.104)
    assert deal.valuation.price_rank is not None


def test_an_old_ad_is_valued_but_never_alerted(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A months-old listing is probably sold - record it, never notify."""
    import snipeklein.pipeline as pipeline_module
    from snipeklein.store.decisions import DecisionRepo

    original = pipeline_module.parse_listing
    old_date = (datetime.now(UTC) - timedelta(days=40)).strftime("%d.%m.%Y")

    def parse_old(html: str, url: str, base_url: str):
        return original(html, url, base_url).model_copy(update={"posted_at": old_date})

    monkeypatch.setattr(pipeline_module, "parse_listing", parse_old)
    result = pipeline.run_cycle(dry_run=True)
    assert result.deals == []
    decision = DecisionRepo(pipeline.conn).get(AD_ID)
    assert decision is not None
    assert decision["reason_code"] == "stale_ad"


def test_the_embed_shows_how_old_the_ad_is(pipeline: Pipeline) -> None:
    from snipeklein.notify.templates import build_embed

    result = pipeline.run_cycle(dry_run=True)
    embed = build_embed(result.deals[0])
    field = next(field for field in embed["fields"] if "🗓️" in field["name"])
    assert "Tage online" in field["value"]
