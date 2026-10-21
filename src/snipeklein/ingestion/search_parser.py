"""Parser for the search result page (spec 3.2).

The live markup exposes every card as

    <article data-adid="..." data-href="/s-anzeige/...">
        <h3><a href="...">title</a></h3>
        <p>snippet</p>
        <p class="... font-strong ...">1.200 € VB</p>   <- asking price
        <p class="... line-through">2.000 €</p>          <- original price
        <span>35745 Herborn</span>                        <- location
    </article>
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from ..logging_setup import get_logger
from ..models import SearchCard
from ..textutils import parse_price_eur

log = get_logger("snipeklein.ingestion.search")

_LOCATION_RE = re.compile(r"^\s*(\d{5})\s+([A-Za-zÄÖÜäöüß][\wÄÖÜäöüß\-\s]{1,40})\s*$")
_POSTED_RE = re.compile(
    r"^\s*(Heute|Gestern|Vorgestern|\d{1,2}\.\d{1,2}\.|\d{1,2}\.\d{1,2}\.\d{4})", re.IGNORECASE
)
_IMAGE_COUNT_RE = re.compile(r"^\s*(\d{1,3})\s*$")


def parse_cards(html: str, base_url: str, query_tag: str = "") -> list[SearchCard]:
    """Extract all listing cards from a search result page."""
    soup = BeautifulSoup(html, "lxml")
    cards: list[SearchCard] = []
    for article in soup.find_all("article"):
        ad_id = article.get("data-adid")
        href = article.get("data-href")
        if not ad_id or not href:
            continue
        card = _parse_card(article, str(ad_id), str(href), base_url, query_tag)
        if card is not None:
            cards.append(card)
    if not cards:
        log.debug("no cards found (selector drift?)")
    return cards


def _parse_card(
    article: Tag, ad_id: str, href: str, base_url: str, query_tag: str
) -> SearchCard | None:
    title = ""
    heading = article.find("h3")
    if heading is not None:
        link = heading.find("a")
        title = (link.get_text(" ", strip=True) if link else heading.get_text(" ", strip=True))
    if not title:
        title = _title_from_jsonld(article) or _title_from_alt(article)
    if not title:
        return None

    url = href if href.startswith("http") else base_url.rstrip("/") + href

    snippet = ""
    for para in article.find_all("p"):
        text = para.get_text(" ", strip=True)
        if text and text != title and "€" not in text:
            snippet = text
            break

    price, original, negotiable = _parse_prices(article)
    location_text, posted_hint, image_count, shipping_hint = _parse_extras(article)
    is_pro = _is_pro_seller(article)

    return SearchCard(
        ad_id=ad_id,
        url=url,
        title=title,
        price=price,
        price_is_negotiable=negotiable,
        original_price=original,
        location_text=location_text,
        snippet=snippet,
        posted_hint=posted_hint,
        image_count=image_count,
        shipping_hint=shipping_hint,
        is_pro=is_pro,
        query_tag=query_tag,
    )


def _is_pro_seller(article: Tag) -> bool:
    """Professional sellers link to their shop page (/pro/<shop>)."""
    for link in article.find_all("a"):
        href = str(link.get("href") or "")
        if href.startswith("/pro/"):
            return True
    return False


def _title_from_jsonld(article: Tag) -> str:
    for script in article.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if '"title"' in raw:
            match = re.search(r'"title"\s*:\s*"(.*?)"\s*,\s*"description"', raw, re.DOTALL)
            if match:
                return match.group(1).replace('\\"', '"').replace("\\n", " ").strip()
    return ""


def _title_from_alt(article: Tag) -> str:
    img = article.find("img")
    if img is None:
        return ""
    alt = str(img.get("alt") or "").strip()
    if not alt:
        return ""
    # 'Title Bochum - Bochum-Ost Vorschau'
    alt = re.sub(r"\s+[A-Za-zÄÖÜäöüß.\- ]+\s+-\s+.*?Vorschau$", "", alt)
    return alt.strip()


def _parse_prices(article: Tag) -> tuple[float | None, float | None, bool]:
    asking: float | None = None
    original: float | None = None
    negotiable = False
    for para in article.find_all("p"):
        text = para.get_text(" ", strip=True)
        if "€" not in text:
            continue
        classes = " ".join(para.get("class") or [])
        value = parse_price_eur(text)
        if value is None:
            continue
        if "line-through" in classes:
            original = value
            continue
        if asking is None:
            asking = value
        if "VB" in text:
            negotiable = True
    if asking is None and original is not None:
        asking = original
    return asking, original, negotiable


def _parse_extras(article: Tag) -> tuple[str, str, int, str]:
    location_text = ""
    posted_hint = ""
    shipping_hint = ""
    image_count = 0

    for span in article.find_all("span"):
        text = span.get_text(" ", strip=True)
        if not text:
            continue
        loc = _LOCATION_RE.match(text)
        if loc and not location_text:
            location_text = text
            continue
        if _POSTED_RE.match(text) and not posted_hint:
            posted_hint = text
            continue
        if "Versand" in text and not shipping_hint:
            shipping_hint = text
            continue
        if "Abholung" in text and not shipping_hint:
            shipping_hint = text

    for div in article.find_all("div"):
        classes = " ".join(div.get("class") or [])
        if "absolute" not in classes:
            continue
        text = div.get_text(" ", strip=True)
        count = _IMAGE_COUNT_RE.match(text)
        if count and "bottom" in classes:
            image_count = int(count.group(1))
            break

    return location_text, posted_hint, image_count, shipping_hint
