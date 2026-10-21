"""Tier-1 pre-filter."""

from __future__ import annotations

from snipeklein.filters.prefilter import evaluate


def test_accepts_a_ddr5_gaming_pc() -> None:
    result = evaluate("Gaming PC Ryzen 7 7800X3D RTX 4070 32GB DDR5 1TB NVMe")
    assert result.passed


def test_rejects_wanted_ads() -> None:
    assert not evaluate("Suche Gaming PC RTX 4070").passed


def test_rejects_exchange_offers() -> None:
    assert not evaluate("Gaming PC Tausch ist auch möglich").passed


def test_rejects_ddr4_builds() -> None:
    assert not evaluate("Gaming PC Ryzen 7 5700X RTX 3060 16GB DDR4").passed


def test_rejects_gtx_and_old_platforms() -> None:
    assert not evaluate("Gaming PC i5-7500 GTX 1070 16GB").passed


def test_rejects_lga1200() -> None:
    assert not evaluate("Gaming PC i7-11700 RTX 3060 Ti 32GB").passed


def test_rejects_single_components() -> None:
    assert not evaluate("RTX 4070 Grafikarte").passed


def test_reason_is_reported() -> None:
    result = evaluate("Suche RTX 4070")
    assert result.reason.startswith("title_exclude")


# -- bundles ---------------------------------------------------------------
def test_bundles_are_recognised() -> None:
    """PC plus monitor and accessories can never be a good deal."""
    for title in (
        "Gaming PC Komplettset inklusive 2 Monitoren u. Tastatur",
        "Komplett PC-Set mit Gaming Monitor 144 HZ, Tastatur, Maus, Stuhl",
        "PC Komplettset",
        "2 PC-Komplettsets mit Monitor – Windows 11 Pro + GRATIS Zubehör!",
    ):
        result = evaluate(title)
        assert not result.passed
        assert result.reason == "bundle", title


def test_a_single_pc_is_not_a_bundle() -> None:
    result = evaluate("Gaming PC / sehr gut erhalten")
    assert not result.passed
    assert result.reason == "no_component_marker"
