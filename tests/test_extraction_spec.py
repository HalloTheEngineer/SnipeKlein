"""The deterministic extraction must read the spec list, not the marketing."""

from __future__ import annotations

import pytest

from snipeklein.config import ValuationConfig
from snipeklein.extract.extractor import (
    _spec_fields,
    apply_spec_override,
    regex_extract,
)
from snipeklein.models import Band, Extraction, Listing
from snipeklein.valuation.model import ValuationEngine
from snipeklein.valuation.table import PriceTable

AD_TEXT = """Ich verkaufe meinen nahezu neuwertigen Gaming-PC.
Anders als aeltere Systeme basiert dieser PC auf der modernen AMD AM5-Plattform
mit DDR5-RAM und einem ATX 3.1 750W Netzteil. Aktuell perfekt fuer Full-HD &
E-Sports - und jederzeit problemlos mit einer staerkeren Grafikkarte
(z. B. RTX 4070/5070) aufruestbar, ohne Board oder Netzteil tauschen zu muessen.
Technische Daten PC:
* Prozessor: AMD Ryzen 5 7600 (6 Kerne / 12 Threads)
* Mainboard: ASUS TUF Gaming B650-E WiFi (AM5, WiFi 6E)
* Grafikkarte: NVIDIA GeForce GTX 1060 (6 GB VRAM)
* Arbeitsspeicher: 16 GB (2x 8 GB) Kingston FURY Beast DDR5-5600
* Speicher: 1 TB Crucial P3 Plus PCIe 4.0 NVMe M.2 SSD
* Netzteil: Thermaltake Berlin 750W (ATX 3.1, 80 PLUS)
* Gehaeuse: MSI MAG Forge 112R
Nur Abholung."""


@pytest.fixture(scope="module")
def table() -> PriceTable:
    return PriceTable.load("data/prices.json")


def test_the_spec_line_beats_the_upgrade_sentence() -> None:
    """The installed GTX 1060 must win over the advertised upgrade path."""
    extraction = regex_extract(AD_TEXT)
    assert extraction.gpu_model == "GTX 1060"
    assert "4070" not in (extraction.gpu_model or "")
    assert extraction.cpu_model and "7600" in extraction.cpu_model
    assert extraction.ram_capacity_gb == 16
    assert extraction.ram_generation == "DDR5"
    assert extraction.storage_capacity_gb == 1024
    assert extraction.motherboard_chipset == "B650"
    assert extraction.psu_wattage == 750


def test_an_upgrade_sentence_alone_is_not_a_gpu() -> None:
    text = "Gaming PC mit Ryzen 5 7600, 16 GB DDR5. Aufruestbar mit RTX 4070/5070."
    extraction = regex_extract(text)
    assert extraction.gpu_model is None


def test_the_llm_result_is_corrected_by_the_spec_line() -> None:
    """A model that read the marketing sentence is overruled deterministically."""
    extraction = Extraction(
        is_complete_system=True,
        cpu_model="Ryzen 5 7600",
        gpu_model="RTX 4070",
        ram_generation="DDR5",
        ram_capacity_gb=16,
    )
    changed = apply_spec_override(extraction, AD_TEXT)
    assert "gpu_model" in changed
    assert extraction.gpu_model == "GTX 1060"


def test_spec_fields_read_the_ram_kit_and_storage() -> None:
    fields = _spec_fields(
        "Arbeitsspeicher: 32 GB (2x 16 GB) Corsair DDR5-6000\n"
        "Speicher: 2 TB Samsung 990 PRO NVMe"
    )
    assert fields["ram_capacity_gb"] == 32
    assert fields["ram_generation"] == "DDR5"
    assert fields["storage_capacity_gb"] == 2048


def test_the_old_gpus_are_valued_instead_of_missing(table: PriceTable) -> None:
    component = table.match("GTX 1060 6GB")
    assert component is not None
    assert component.id == "NVIDIA_GTX_1060_6GB"
    assert table.match("gtx 1080 ti").id == "NVIDIA_GTX_1080_TI"  # type: ignore[union-attr]
    assert table.match("gtx 1080").id == "NVIDIA_GTX_1080"  # type: ignore[union-attr]
    assert table.match("RTX 2080 Super").id == "NVIDIA_RTX_2080_SUPER"  # type: ignore[union-attr]
    assert table.match("rx 580").id == "AMD_RX_580"  # type: ignore[union-attr]


def test_the_old_gpu_variants_stay_apart_in_the_triage(table: PriceTable) -> None:
    super_card = table.get("NVIDIA_GTX_1660_SUPER")
    plain = table.get("NVIDIA_GTX_1660")
    assert super_card is not None and plain is not None
    assert super_card.exact_match("GTX 1660 Super")
    assert not plain.exact_match("GTX 1660 Super")


def test_the_corrected_ad_is_no_deal(table: PriceTable) -> None:
    """With the real GPU the false extreme alert disappears."""
    extraction = regex_extract(AD_TEXT)
    engine = ValuationEngine(table, ValuationConfig())
    listing = Listing(
        ad_id="1",
        url="https://example.invalid/s-anzeige/gaming-pc/1-228-1",
        title="Gaming PC Ryzen 5 7600 | 16 GB DDR5 (2x8) | 1 TB SSD",
        price=790.0,
    )
    valuation = engine.value(extraction, listing)
    ids = [component.component_id for component in valuation.components]
    assert "NVIDIA_GTX_1060_6GB" in ids
    assert "NVIDIA_RTX_4070_12GB" not in ids
    assert valuation.band not in (Band.ACTIONABLE, Band.EXTREME)
    assert valuation.margin_pct < 12
