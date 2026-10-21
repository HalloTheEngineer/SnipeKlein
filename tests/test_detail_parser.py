"""Listing detail page parser."""

from __future__ import annotations

import pytest

from snipeklein.ingestion.detail_parser import (
    DetailParseError,
    extract_ad_context,
    parse_listing,
)
from snipeklein.models import ShippingMode

URL = "https://www.kleinanzeigen.de/s-anzeige/test/3064361110-228-1"


def test_ad_context_is_extracted(detail_page: str) -> None:
    context = extract_ad_context(detail_page)
    assert context.get("adid") == "3064361110"
    assert "Versand" in context
    assert "Zustand" in context
    assert "Verkaeufer" in context


def test_parse_listing(detail_page: str) -> None:
    listing = parse_listing(detail_page, URL)
    assert listing.ad_id == "3064361110"
    assert listing.title
    assert listing.price is not None and listing.price > 0
    assert listing.description
    assert listing.shipping_mode in (ShippingMode.PICKUP, ShippingMode.SHIPPING)
    assert listing.seller_since
    assert listing.seller_since_date is not None


def test_parse_gpu_listing(detail_page_gpu: str) -> None:
    listing = parse_listing(detail_page_gpu, URL)
    assert listing.ad_id == "3547079834"
    assert listing.price and listing.price > 0
    assert "4070" in listing.title


def test_parse_old_listing(detail_page_old: str) -> None:
    listing = parse_listing(detail_page_old, URL)
    assert listing.ad_id == "3533437624"
    assert listing.price


def test_invalid_page_raises() -> None:
    with pytest.raises(DetailParseError):
        parse_listing("<html><body>nothing here</body></html>", URL)
