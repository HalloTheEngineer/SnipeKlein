"""The card level matcher: one part per kind and the real notations."""

from __future__ import annotations

import pytest

from snipeklein.config import FilterConfig, ValuationConfig
from snipeklein.models import SearchCard
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


def _parts(triage: Triage, title: str) -> list[str]:
    return [component.id for component in triage._match_parts(title)]  # noqa: SLF001


def test_one_storage_per_listing(triage: Triage) -> None:
    """"1TB NVMe" must not count as a Gen4 *and* a Gen3 drive."""
    parts = _parts(triage, "Gaming PC RTX 4070 Ti 1TB NVMe 32GB DDR5")
    assert parts.count("NVME_GEN4_1TB") == 1
    assert "NVME_GEN3_1TB" not in parts


def test_one_ram_kit_per_listing(triage: Triage) -> None:
    parts = _parts(triage, "Gaming PC RTX 4070 Ti 32GB DDR5")
    assert parts.count("DDR5_32GB_STANDARD") == 1


def test_the_memory_kit_in_the_german_notation(triage: Triage) -> None:
    """"32GB (2x 16GB)" is a 32 GB kit, not a 16 GB stick."""
    parts = _parts(triage, "Gaming PC Ryzen 7 7700X RTX 4070 32GB (2x 16GB) DDR5-6000")
    assert "DDR5_32GB_STANDARD" in parts


def test_the_memory_in_megabytes(triage: Triage) -> None:
    parts = _parts(triage, "Gaming PC Ryzen 7 3700X 32GB (2x 16384MB) Corsair DDR4-3200")
    assert "DDR4_32GB_STANDARD" in parts


def test_the_storage_in_the_german_notation(triage: Triage) -> None:
    parts = _parts(triage, "Gaming PC RTX 4070 Ti 1,5TB Samsung SSD 980 PRO 32GB DDR5")
    assert "SATA_SSD_1TB" in parts


def test_the_storage_in_gigabytes(triage: Triage) -> None:
    parts = _parts(triage, "Gaming PC RTX 4070 Ti 1000 GB NVMe 16GB DDR5")
    assert "NVME_GEN4_1TB" in parts


def test_the_notation_does_not_change_the_estimate(triage: Triage) -> None:
    """The same system, written plainly and in the German notation."""
    plain = SearchCard(
        ad_id="1",
        url="u",
        title="Gaming PC Ryzen 7 7700X RTX 4070 32GB DDR5 1TB NVMe",
        price=1200.0,
    )
    fancy = SearchCard(
        ad_id="2",
        url="u",
        title="Gaming PC Ryzen 7 7700X RTX 4070 32GB (2x 16GB) DDR5-6000 1TB NVMe",
        price=1200.0,
    )
    plain_result = triage.evaluate(plain)
    fancy_result = triage.evaluate(fancy)
    assert plain_result.coarse_margin_pct == pytest.approx(
        fancy_result.coarse_margin_pct, abs=0.1
    )


def test_a_ddr4_listing_is_not_promoted(triage: Triage) -> None:
    """The old platform must not slip through the modern tier."""
    card = SearchCard(
        ad_id="1",
        url="u",
        title="Gaming PC Ryzen 7 3700X 32GB (2x 16384MB) Corsair DDR4-3200 RTX 3060 Ti",
        price=700.0,
    )
    result = triage.evaluate(card)
    assert not result.passed
    assert result.reason.startswith("triage_components")


def test_the_prompt_keeps_a_long_description() -> None:
    from snipeklein.extract.schema import build_user_prompt

    long_description = "x" * 20000 + " 1TB NVMe"
    prompt = build_user_prompt("Gaming PC", long_description)
    assert "1TB NVMe" in prompt
    assert len(prompt) > 12000
