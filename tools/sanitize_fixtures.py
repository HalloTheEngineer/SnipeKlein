"""Redact personal data from the captured test fixtures.

The fixtures are real kleinanzeigen.de pages.  They are needed to test the
parsers offline, but they must not publish the identity of the people who
wrote those ads.

The script works in two steps:

1. *collect* - find every value that identifies a person (display name, user
   id, registration date, postal code, city, region, e-mail address, shop
   slug).  The site repeats those values in many places: in the ad context
   json, in the navigation state, in value arrays and in aria labels.
2. *replace* - replace every occurrence of those values in the whole document
   with a neutral placeholder.

Everything else stays byte identical: listing ids, titles, prices,
descriptions, the page structure and the layout.

Usage:
    python tools/sanitize_fixtures.py [--check]
"""

from __future__ import annotations

import gzip
import html
import re
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "data" / "fixtures"

SELLER_NAME = "Test Seller"
SELLER_SINCE = "01.01.2015"
SELLER_LOCATION = "12345 Teststadt"
SELLER_CITY = "Teststadt"
SELLER_REGION = "Testregion"
SELLER_PLZ = "12345"
SELLER_EMAIL = "kontakt@example.invalid"
SHOP_SLUG = "testshop"
SHOP_NAME = "TestShop"

# Values that are too generic to be replaced safely.
IGNORED = {
    "",
    "0",
    "1",
    "2",
    "de",
    "de_DE",
    "null",
    "true",
    "false",
    "ja",
    "nein",
    "none",
    "None",
    "NONE",
    "auto",
    "inherit",
    "initial",
    "unset",
    "currentColor",
    "anonymous",
    "undefined",
    "pcs",
    "PCs",
    "elektro",
    "Elektronik",
    "unbekannt",
}

# (pattern, kind) - the kind decides the placeholder
SOURCES: list[tuple[str, str]] = [
    (r'"contactName":\[0,"([^"]*)"\]', "name"),
    (r'"sellerName":\[0,"([^"]*)"\]', "name"),
    (r'aria-label="Profil von ([^"]{2,60})"', "name"),
    (r'<div class="pb-xsmall text-bodyRegularStrong">([^<]{2,60})</div>', "name"),
    (r'class="userprofile-vip"[^>]*>\s*<a[^>]*>\s*([^<]{2,60})', "name"),
    (r'"userSince":\[0,"([^"]*)"\]', "since"),
    (r'"userId":\[0,(\d+)\]', "id"),
    (r'"posterid":\[0,(\d+)\]', "id"),
    (r'"posterid":"(\d+)"', "id"),
    (r"userId=(\d+)", "id"),
    (r'"plz":"?(\d+)"?', "plz"),
    (r'"postal_code":"?(\d+)"?', "plz"),
    (r'"l4_location_name":"?(\d+)"?', "plz"),
    (r'"selected_location_name":"?(\d+)"?', "plz"),
    (r'"city":"([^"]*)"', "city"),
    (r'"l2_location_name":"([^"]*)"', "city"),
    (r'"l3_location_name":"([^"]*)"', "city"),
    (r'"location_name":"([^"]*)"', "city"),
    (r'"locationName":\[0,"([^"]*)"\]', "city"),
    (r'"parentLocationName":\[0,"([^"]*)"\]', "city"),
    (r'title="Kleinanzeigen ([^"]{2,40})"', "city"),
    (r'"region":"([^"]*)"', "region"),
    (r'"l1_location_name":"([^"]*)"', "region"),
    (r'"location":\[0,"([^"]*)"\]', "location"),
    (r'[\w.+-]+@[\w-]+\.[\w.-]{2,}', "email"),
    (r"/pro/([A-Za-z0-9\-_]+)", "shop"),
    (r'"title":\[0,"([Oo]ri[Tt]ech)"\]', "shopname"),
    (r'aria-label="Zum shop ([^"]{2,60})', "shopname"),
    (r'>\s*([Tt]echnikhafen)\s*<', "shopname"),
]

PLACEHOLDER = {
    "name": SELLER_NAME,
    "since": SELLER_SINCE,
    "id": "1",
    "plz": SELLER_PLZ,
    "city": SELLER_CITY,
    "region": SELLER_REGION,
    "location": SELLER_LOCATION,
    "email": SELLER_EMAIL,
    "shop": SHOP_SLUG,
    "shopname": SHOP_NAME,
}

MIN_LENGTH = 3


def collect(text: str) -> dict[str, str]:
    """Find every identifying value in the document."""
    found: dict[str, str] = {}
    for pattern, kind in SOURCES:
        for match in re.findall(pattern, text):
            value = match.strip()
            if len(value) < MIN_LENGTH or value in IGNORED:
                continue
            if value.isdigit():
                # a numeric value is always a postal code
                kind = "plz"
            found.setdefault(value, PLACEHOLDER[kind])
    return found


def sanitize(raw: str) -> tuple[str, int]:
    """Replace every identifying value, returns the new text and the count.

    The values are collected from the unescaped document (the site stores
    them in escaped json as well) and then replaced in the raw document.
    """
    found = collect(html.unescape(raw))
    changes = 0
    out = raw
    # longest values first, so a full 'City - District' value is replaced
    # before the bare city name
    for value in sorted(found, key=len, reverse=True):
        replacement = found[value]
        if value == replacement:
            continue
        flags = re.IGNORECASE if found[value] in (SELLER_CITY, SELLER_REGION) else 0
        out, count = re.subn(re.escape(value), replacement, out, flags=flags)
        changes += count
        escaped = html.escape(value, quote=False)
        if escaped != value:
            out, count = re.subn(re.escape(escaped), replacement, out, flags=flags)
            changes += count
    return out, changes


def main() -> int:
    check_only = "--check" in sys.argv
    files = sorted(FIXTURE_DIR.glob("*.gz"))
    if not files:
        print(f"no fixtures in {FIXTURE_DIR}")
        return 1

    dirty = []
    for path in files:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            original = fh.read()
        cleaned, changes = sanitize(original)
        if changes:
            dirty.append((path.name, changes))
        if check_only:
            continue
        if cleaned != original:
            with gzip.open(path, "wt", encoding="utf-8") as fh:
                fh.write(cleaned)
        print(f"  {path.name:<40} {changes:>5} replacements")

    if check_only and dirty:
        print(f"\n{len(dirty)} fixture(s) still contain personal data")
        return 1
    if not check_only and dirty:
        print(f"\nsanitized {len(dirty)} fixture(s)")
    else:
        print("\nnothing to sanitize (already clean)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
