"""The modern platform tier and the health verdict without secrets."""

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
from snipeklein.filters.prefilter import is_modern_platform
from snipeklein.health import STATUS_ATTENTION, STATUS_OK, collect_health
from snipeklein.models import SearchCard
from snipeklein.store.db import connect, migrate
from snipeklein.valuation.model import ValuationEngine
from snipeklein.valuation.table import PriceTable
from snipeklein.valuation.triage import Triage


@pytest.fixture(scope="module")
def table() -> PriceTable:
    return PriceTable.load("data/prices.json")


@pytest.fixture()
def triage(table: PriceTable) -> Triage:
    engine = ValuationEngine(table=table, config=ValuationConfig())
    return Triage(table, FilterConfig(), engine)


def _card(title: str, price: float = 1000.0, snippet: str = "") -> SearchCard:
    return SearchCard(
        ad_id="1",
        url="https://example.invalid/1",
        title=title,
        price=price,
        snippet=snippet,
    )


# -- the modern platform hint --------------------------------------------
@pytest.mark.parametrize(
    "title",
    [
        "Gaming PC RTX 4070 Ryzen 7 7800X3D 32GB DDR5",
        "Gaming PC MSI RTX 5070, Ryzen 7 7700x, 32GB RAM",
        "Gaming-PC | Intel i9-12900K | RTX 4060 | 32 GB RAM",
        "AM5 Spiele PC B650 Ryzen 5 9600X",
        "Gaming PC 32GB DDR5 RTX 5060 Ryzen 5 7500f",
    ],
)
def test_modern_platform_is_detected(title: str) -> None:
    assert is_modern_platform(title)


@pytest.mark.parametrize(
    "title",
    [
        "Gaming PC AMD 5600X RTX 3060 Ti 16GB RAM",
        "Gaming PC i7-11700F RTX 3060 Ti 32GB DDR4",
        "Gaming PC RTX 3050 AMD Ryzen 5 512GB SSD",
        "Gaming PC AMD Ryzen 7 2700X Radeon RX 6600",
    ],
)
def test_old_platforms_are_not_modern(title: str) -> None:
    assert not is_modern_platform(title)


# -- the triage tier -------------------------------------------------------
def test_unknown_but_modern_ad_reaches_the_extraction(triage: Triage) -> None:
    """The price table does not know the CPU yet, the title hints at AM5."""
    card = _card("Gaming PC Ryzen 7 9850X3D RTX 5070", price=1200.0)
    result = triage.evaluate(card)
    assert result.passed
    assert result.reason == "triage_modern_tier"
    assert result.note


def test_unknown_and_old_ad_is_rejected(triage: Triage) -> None:
    card = _card("Gaming PC AMD 5600X RTX 3060 Ti 16GB RAM", price=400.0)
    result = triage.evaluate(card)
    assert not result.passed
    assert result.reason.startswith("triage_components")


def test_known_system_still_needs_the_margin(triage: Triage) -> None:
    """A modern title does not bypass the margin check when the parts are known."""
    card = _card(
        "Gaming PC Ryzen 7 7800X3D RTX 4070 32GB DDR5 1TB NVMe", price=1800.0
    )
    result = triage.evaluate(card)
    assert not result.passed
    assert result.reason.startswith("triage_margin")


def test_tier_can_be_switched_off(table: PriceTable) -> None:
    engine = ValuationEngine(table=table, config=ValuationConfig())
    triage = Triage(table, FilterConfig(modern_platform_tier=False), engine)
    card = _card("Gaming PC Ryzen 7 9850X3D RTX 5070", price=1200.0)
    result = triage.evaluate(card)
    assert not result.passed
    assert result.reason.startswith("triage_components")


# -- the health verdict without secrets ------------------------------------
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


def _insert_run(conn: sqlite3.Connection, started_at: datetime) -> None:
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, queries, ads_seen, ads_new, "
        "ads_evaluated, alerts_sent, errors) VALUES (?,?,?,?,?,?,?,?)",
        (started_at.isoformat(), started_at.isoformat(), "[]", 1, 0, 0, 0, 0),
    )


def test_health_without_secrets_ignores_them(tmp_path: Path) -> None:
    """The panel cannot see the bot's secrets and must not report them missing."""
    config = _config(tmp_path)
    conn = connect(config.db_path)
    migrate(conn)
    _insert_run(conn, datetime.now(UTC) - timedelta(seconds=20))
    try:
        with_secrets = collect_health(config, conn, check_secrets=True)
        without = collect_health(config, conn, check_secrets=False)
    finally:
        conn.close()
    assert with_secrets["status"] == STATUS_ATTENTION
    assert with_secrets["missing"]
    assert without["status"] == STATUS_OK
    assert without["missing"] == []
    assert without["secrets"] == "unknown"
    assert without["llm_configured"] is None


# -- the extraction rejection split ----------------------------------------
def test_ddr4_and_unknown_ram_are_different_reasons() -> None:
    """A missing RAM generation is a gap in the ad, not a disqualification."""
    from snipeklein.pipeline import _extraction_reason

    code, text = _extraction_reason("ram_generation:DDR4")
    assert code == "ram_generation"
    assert "DDR4" in text

    code, text = _extraction_reason("ram_generation:UNKNOWN")
    assert code == "ram_unknown"
    assert "not mentioned" in text

    code, text = _extraction_reason("not_a_complete_system")
    assert code == "not_a_complete_system"


# -- variants are counted once ---------------------------------------------
def test_a_variant_is_not_counted_twice(triage: Triage) -> None:
    """"RTX 4070 Ti" must not count as a 4070 *and* a 4070 Ti."""
    card = _card("Gaming PC RTX 4070 Ti 32GB DDR5", price=1000.0)
    result = triage.evaluate(card)
    labels = [c.label for c in result.components]
    assert "NVIDIA GeForce RTX 4070 Ti" in labels
    assert "NVIDIA GeForce RTX 4070 12GB" not in labels


def test_the_base_model_is_counted(triage: Triage) -> None:
    card = _card("Gaming PC RTX 4070 32GB DDR5", price=1000.0)
    result = triage.evaluate(card)
    labels = [c.label for c in result.components]
    assert "NVIDIA GeForce RTX 4070 12GB" in labels
    assert "NVIDIA GeForce RTX 4070 Ti" not in labels
