"""Parser for the listing detail page.

Three sources are combined, in this order:

1. the Astro island props (the complete ad state: price, condition, seller,
   location, creation date) - authoritative and template independent;
2. the legacy flat ad-context JSON (``adid``, ``ExactPreis``, ``Versand``,
   ``Zustand`` ...);
3. the classic detail template (``#viewad-price``, ``#viewad-locality``,
   ``#viewad-description-text`` ...) as fallback.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup, Tag

from ..logging_setup import get_logger
from ..models import Listing, ShippingMode
from ..textutils import parse_german_date, parse_price_eur
from .embedded import extract_ad_payload, extract_props, find_all, find_key

log = get_logger("snipeklein.ingestion.detail")

_AD_CONTEXT_RE = re.compile(r"\{[^{}]*\"adid\"\s*:\s*\"(?P<adid>\d+)\"[^{}]*\}", re.DOTALL)
_ACTIVE_ADS_RE = re.compile(r"(\d+)\s+Anzeigen?\s+online")
_SINCE_RE = re.compile(r"Aktiv\s+seit\s*(\d{1,2}\.\d{1,2}\.\d{4})")
_SELLER_TYPE_RE = re.compile(r"(Privater|Gewerblicher)\s+Nutzer")
_BADGE_RE = re.compile(
    r"^(TOP|Sehr\s+freundlich|Freundlich|Zuverlässig|Sehr\s+zuverlässig|"
    r"Schnell\s+zuverlässig|Seriös|Vor Ort|Kommission)$",
    re.IGNORECASE,
)

_CONDITION_LABELS = {
    "like_new": "Sehr Gut",
    "new": "Neu",
    "ok": "Gut",
    "alright": "In Ordnung",
    "defect": "Defekt",
}

_CONDITION_CODES = {label: code for code, label in _CONDITION_LABELS.items()}


class DetailParseError(RuntimeError):
    """The page did not contain a usable listing."""


def parse_listing(html: str, url: str, base_url: str = "") -> Listing:
    """Parse a detail page into a :class:`Listing`."""
    context = extract_ad_context(html)
    props = extract_ad_payload(extract_props(html))
    soup = BeautifulSoup(html, "lxml")

    ad_id = str(
        find_key(props, "id") or context.get("adid") or _ad_id_from_dom(soup) or ""
    )
    if not ad_id:
        raise DetailParseError(f"no ad id found in {url}")

    title = _text(soup.select_one("#viewad-title")) or _text(soup.select_one("h1[itemprop=name]"))
    if not title:
        title = _text(soup.select_one("h1"))
    if not title:
        raise DetailParseError(f"no title found in {url}")

    price, negotiable, original = _parse_price(soup, context, props)
    shipping = _parse_shipping(soup, context, props)
    plz, city, region = _parse_location(soup, context, props)
    condition_label, condition_code = _parse_condition(soup, context, props)
    description = _parse_description(soup)
    posted_at = _parse_posted_at(soup, props)
    seller_name, seller_since, seller_type, badges, active_ads = _parse_seller(soup, props)

    category_id = find_key(props, "categoryId") or context.get("cat") or context.get("tcat")
    try:
        category_id = int(category_id) if category_id is not None else None
    except (TypeError, ValueError):
        category_id = None

    seller_since_date = parse_german_date(seller_since)

    return Listing(
        ad_id=ad_id,
        url=url,
        title=title.strip(),
        description=description,
        price=price,
        price_is_negotiable=negotiable,
        original_price=original,
        plz=plz,
        city=city,
        region=region,
        category_id=category_id,
        posted_at=posted_at,
        condition=condition_label,
        condition_code=condition_code,
        shipping_mode=shipping,
        seller_name=seller_name,
        seller_since=seller_since,
        seller_since_date=seller_since_date,
        seller_badges=badges,
        seller_type=seller_type,
        seller_active_ads=active_ads,
        ad_type=str(context.get("Angebotstyp") or ""),
        fetched_at=datetime.now(),
    )


def extract_ad_context(html: str) -> dict[str, Any]:
    """Pull the flat ad-context JSON out of the page (legacy template)."""
    match = _AD_CONTEXT_RE.search(html)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        log.debug("ad context found but not parseable")
        return {}
    return data if isinstance(data, dict) else {}


# -- helpers ---------------------------------------------------------------
def _text(node: Tag | None) -> str:
    if node is None:
        return ""
    return node.get_text(" ", strip=True)


def _ad_id_from_dom(soup: BeautifulSoup) -> str:
    box = soup.select_one("#viewad-ad-id-box")
    if box is None:
        return ""
    for li in box.find_all("li"):
        text = li.get_text(" ", strip=True)
        if text.isdigit():
            return text
    return ""


def _parse_price(
    soup: BeautifulSoup, context: dict[str, Any], props: dict[str, Any]
) -> tuple[float | None, bool, float | None]:
    price_payload = find_key(props, "price")
    asking: float | None = None
    negotiable = False
    if isinstance(price_payload, dict):
        amount = price_payload.get("amount")
        if isinstance(amount, (int, float)):
            asking = float(amount)
        kind = str(price_payload.get("type", ""))
        negotiable = "NEGOTIABLE" in kind.upper()
    if asking is None:
        exact = context.get("ExactPreis")
        if isinstance(exact, (int, float)) and exact > 0:
            asking = float(exact)

    node = soup.select_one("#viewad-price")
    dom_text = _text(node)
    if asking is None:
        asking = parse_price_eur(dom_text)
    if "VB" in dom_text:
        negotiable = True

    original: float | None = None
    old = soup.select_one(".aditem-main--middle--price-shipping--old-price")
    if old is not None:
        original = parse_price_eur(_text(old))
    if original is None:
        meta = soup.select_one('meta[itemprop="price"]')
        if meta is not None:
            try:
                meta_value = float(meta.get("content"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                meta_value = None
            if meta_value and asking and meta_value != asking:
                original = meta_value
    return asking, negotiable, original


def _parse_shipping(
    soup: BeautifulSoup, context: dict[str, Any], props: dict[str, Any]
) -> ShippingMode:
    for value in find_all(props, "localizedValue"):
        text = str(value)
        if "abholung" in text.lower():
            return ShippingMode.PICKUP
        if "versand" in text.lower():
            return ShippingMode.SHIPPING
    raw = str(context.get("Versand") or "").strip().lower()
    if raw in ("ja", "true", "yes"):
        return ShippingMode.SHIPPING
    if raw in ("nein", "false", "no"):
        return ShippingMode.PICKUP
    node = soup.select_one(".boxedarticle--details--shipping")
    text = _text(node).lower()
    if "versand" in text:
        return ShippingMode.SHIPPING
    if "abholung" in text:
        return ShippingMode.PICKUP
    for tag in soup.find_all("span"):
        text = _text(tag).lower()
        if "versand möglich" in text:
            return ShippingMode.SHIPPING
        if "nur abholung" in text:
            return ShippingMode.PICKUP
    return ShippingMode.UNKNOWN


def _parse_location(
    soup: BeautifulSoup, context: dict[str, Any], props: dict[str, Any]
) -> tuple[str, str, str]:
    plz = str(context.get("plz") or "")
    city_raw = str(context.get("city") or "").replace("_", " ")
    region = str(context.get("region") or "").replace("_", " ")

    embedded = find_key(props, "location")
    text = str(embedded) if embedded else ""
    if not text:
        node = soup.select_one("#viewad-locality")
        text = _text(node)
    if text:
        parts = text.split(None, 1)
        if parts and parts[0].isdigit():
            plz = plz or parts[0]
            rest = parts[1] if len(parts) > 1 else ""
        else:
            rest = text
        if " - " in rest:
            region, city_raw = rest.split(" - ", 1)
            city_raw = city_raw.strip()
        elif rest:
            city_raw = rest.strip()
    return plz, city_raw.strip(), region.strip()


def _parse_condition(
    soup: BeautifulSoup, context: dict[str, Any], props: dict[str, Any]
) -> tuple[str, str]:
    attributes = find_key(props, "localizedAttributes") or []
    if isinstance(attributes, dict):
        attributes = [attributes]
    label = ""
    for entry in attributes:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("localizedName", ""))
        if name.lower() == "zustand":
            label = str(entry.get("localizedValue", ""))
            break

    code = str(context.get("Zustand") or "").strip()
    if not label:
        label = _CONDITION_LABELS.get(code, "")
    if not label:
        box = soup.select_one("#viewad-details")
        if box is not None:
            for item in box.find_all(["li", "div"]):
                text = item.get_text(" ", strip=True)
                if text.lower().startswith("zustand"):
                    label = text.split(":", 1)[-1].strip() or text[len("Zustand") :].strip()
                    break
    if not code and label:
        code = _CONDITION_CODES.get(label.strip(), "")
    return label, code


def _parse_description(soup: BeautifulSoup) -> str:
    node = soup.select_one("#viewad-description-text")
    if node is None:
        node = soup.select_one("#viewad-description")
    if node is None:
        return ""
    return node.get_text("\n", strip=True)


def _parse_posted_at(soup: BeautifulSoup, props: dict[str, Any]) -> str:
    embedded = find_key(props, "formattedCreationDate")
    if embedded:
        match = re.search(r"\d{1,2}\.\d{1,2}\.\d{4}", str(embedded))
        if match:
            return match.group(0)
    box = soup.select_one("#viewad-extra-info")
    if box is not None:
        match = re.search(r"\d{1,2}\.\d{1,2}\.\d{4}", box.get_text(" ", strip=True))
        if match:
            return match.group(0)
    return ""


def _parse_seller(
    soup: BeautifulSoup, props: dict[str, Any]
) -> tuple[str, str, str, list[str], int]:
    name = ""
    since = ""
    seller_type = ""
    active_ads = 0

    details = find_key(props, "userDetails")
    if isinstance(details, dict):
        name = str(details.get("contactName") or "")
        since = str(details.get("userSince") or "")
        if details.get("commercial"):
            seller_type = "Gewerblicher Nutzer"
        elif details.get("userId"):
            seller_type = "Privater Nutzer"
        biz_type = details.get("bizUserType")
        if biz_type:
            seller_type = f"{seller_type} ({biz_type})".strip()

    page_text = soup.get_text(" ", strip=True)

    if not since:
        match = _SINCE_RE.search(page_text)
        if match:
            since = match.group(1)

    if not seller_type:
        match = _SELLER_TYPE_RE.search(page_text)
        if match:
            seller_type = match.group(0)

    if not name:
        node = soup.select_one(".userprofile-vip a")
        if node is not None:
            name = _text(node)
    if not name:
        link = soup.select_one('a[href*="userId="]')
        if link is not None:
            name = _text(link)

    link = soup.select_one("#poster-other-ads-link")
    if link is not None:
        match = _ACTIVE_ADS_RE.search(_text(link))
        if match:
            active_ads = int(match.group(1))
    if not active_ads:
        match = _ACTIVE_ADS_RE.search(page_text)
        if match:
            active_ads = int(match.group(1))

    badges = _parse_badges(soup)
    return name, since, seller_type, badges, active_ads


def _parse_badges(soup: BeautifulSoup) -> list[str]:
    badges: list[str] = []
    for node in soup.select(".userbadge-tag, .userbadges-profile-rating"):
        text = _text(node)
        if text:
            badges.append(text)
    for link in soup.select('a[href*="abzeichen"]'):
        text = _text(link)
        if text and _BADGE_RE.match(text):
            badges.append(text)
    unique: list[str] = []
    for badge in badges:
        if badge not in unique:
            unique.append(badge)
    return unique
