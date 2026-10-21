"""Search result page parser."""

from __future__ import annotations

from snipeklein.ingestion.search_parser import parse_cards

BASE = "https://www.kleinanzeigen.de"


def test_parses_cards(search_page: str) -> None:
    cards = parse_cards(search_page, BASE, "test")
    assert len(cards) > 10
    card = cards[0]
    assert card.ad_id.isdigit()
    assert card.url.startswith(BASE)
    assert card.title
    assert card.price is not None and card.price > 0


def test_prices_and_location(search_page: str) -> None:
    cards = parse_cards(search_page, BASE)
    with_price = [c for c in cards if c.price]
    assert with_price
    for card in with_price:
        assert card.price > 0
    located = [c for c in cards if c.location_text]
    assert located
    assert any(c.location_text[0].isdigit() for c in located)


def test_struck_through_price_is_not_the_asking_price() -> None:
    """The original price is rendered with 'line-through' and must be ignored."""
    from tests.conftest import read_fixture

    html = read_fixture("search_pcs_gaming.html")
    cards = parse_cards(html, BASE)
    for card in cards:
        if card.original_price is not None:
            assert card.original_price != card.price
            break


def test_gpu_search_page(gpu_search_page: str) -> None:
    cards = parse_cards(gpu_search_page, BASE)
    assert len(cards) > 5
    assert any("4070" in c.title for c in cards)
