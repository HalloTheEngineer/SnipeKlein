"""Discord embed construction (spec 7)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from ..logging_setup import get_logger
from ..models import (
    Band,
    Deal,
    RiskLevel,
    ShippingMode,
    ad_age_days,
)

log = get_logger("snipeklein.notify.templates")

# A Discord user/role mention from the environment: either a raw ID or an
# already formatted mention.  Discord mentions require the numeric ID.
_TARGET_RE = re.compile(
    r"^(?:<@(?P<role>&)?!?(?P<wrapped>\d{15,25})>|@?(?P<plain>\d{15,25}))$"
)

# Broad mentions are explicit opt-ins, never the default.
_BROADCAST_TARGETS = {
    "everyone": "@everyone",
    "@everyone": "@everyone",
    "here": "@here",
    "@here": "@here",
}

# Discord rejects a message content longer than 2000 characters.  Leave a
# little room for the separators and any future prefix.
MAX_PING_CONTENT = 1900

COLOR_GREEN = 0x2ECC71
COLOR_YELLOW = 0xF1C40F
COLOR_RED = 0xE74C3C
COLOR_NEAR_MISS = 0x58A6FF

MAX_FIELD_VALUE = 1024
MAX_DESCRIPTION = 4096
MAX_TOTAL = 6000

BAND_LABEL = {
    Band.ACTIONABLE: "Actionable Deal",
    Band.EXTREME: "EXTREME OUTLIER",
    Band.NEAR_MISS: "Near Miss - kein Deal",
    Band.NOT_A_DEAL: "kein Deal",
}

RISK_LABEL = {
    RiskLevel.GREEN: "🟢 Geringes Risiko",
    RiskLevel.YELLOW: "🟡 Erhöhtes Risiko",
    RiskLevel.RED: "🔴 Hohes Risiko",
}


def border_color(deal: Deal) -> int:
    """Colour coded fraud risk border (spec 7)."""
    listing = deal.listing
    valuation = deal.valuation

    if valuation.band is Band.NEAR_MISS:
        # its own colour - a near miss must never look like a deal
        return COLOR_NEAR_MISS

    if valuation.band is Band.EXTREME or deal.risk.level is RiskLevel.RED:
        return COLOR_RED

    green_candidate = (
        listing.shipping_mode is ShippingMode.PICKUP
        and 30.0 <= valuation.margin_pct <= 45.0
        and bool(listing.seller_badges)
    )
    if green_candidate and deal.risk.level is RiskLevel.GREEN:
        return COLOR_GREEN

    return COLOR_YELLOW


def _fmt_eur(value: float) -> str:
    return f"{value:,.0f} €".replace(",", ".")


def _components_text(deal: Deal) -> str:
    extraction = deal.extraction
    lines = [
        f"**CPU:** {extraction.cpu_model or '—'}",
        f"**GPU:** {extraction.gpu_model or '—'}",
        f"**RAM:** {extraction.ram_capacity_gb or '?'} GB {extraction.ram_generation}",
        f"**Storage:** {extraction.storage_capacity_gb or '?'} GB",
        f"**Mainboard:** {extraction.motherboard_chipset or '—'}",
        f"**PSU:** {extraction.psu_wattage or '?'} W",
        f"**OS dabei:** {'ja' if extraction.operating_system_included else 'nein'}",
    ]
    if extraction.accessories:
        lines.append(f"**Dazu:** {', '.join(extraction.accessories)}")
    return "\n".join(lines)


def _seller_text(deal: Deal) -> str:
    listing = deal.listing
    badges = ", ".join(listing.seller_badges) if listing.seller_badges else "keine"
    location = " / ".join(part for part in (listing.plz, listing.city) if part)
    shipping = {
        ShippingMode.PICKUP: "Nur Abholung",
        ShippingMode.SHIPPING: "Versand möglich",
        ShippingMode.UNKNOWN: "unbekannt",
    }[listing.shipping_mode]
    lines = [
        f"**Aktiv seit:** {listing.seller_since or 'unbekannt'}",
        f"**Badges:** {badges}",
        f"**Ort:** {location or 'unbekannt'}",
        f"**Übergabe:** {shipping}",
        f"**Aktive Anzeigen:** {listing.seller_active_ads}",
    ]
    if listing.seller_type:
        lines.insert(0, f"**Typ:** {listing.seller_type}")
    return "\n".join(lines)


def _ad_age_text(listing: object) -> str:
    posted = str(getattr(listing, "posted_at", "") or "")
    if not posted:
        return "unbekannt"
    age = ad_age_days(posted)
    if age is None:
        return posted
    return f"{posted} ({age} Tage online)"


def _risk_text(deal: Deal) -> str:
    risk = deal.risk
    lines = [RISK_LABEL[risk.level], f"**Score:** {risk.score}"]
    if risk.signals:
        lines.append("**Signale:** " + ", ".join(signal.label for signal in risk.signals))
    return "\n".join(lines)


def build_embed(deal: Deal) -> dict:
    """Build the Discord embed payload for a deal."""
    listing = deal.listing
    valuation = deal.valuation
    color = border_color(deal)

    description = (
        f"**{_fmt_eur(valuation.asking_price)}** asking vs. "
        f"**{_fmt_eur(valuation.fmv_net)}** FMV → "
        f"**{valuation.margin_pct:+.0f} %** ({_fmt_eur(valuation.margin_eur)})"
    )
    if valuation.band is Band.EXTREME:
        description += "\n⚠️ **Extremes Preis-Missverhältnis – hohes Risiko für Betrug oder Defekte.**"
    elif valuation.band is Band.NEAR_MISS:
        description += (
            "\n🔵 **Near Miss – kein Deal.** Der Preis liegt nur leicht unter dem "
            "Marktniveau, das ist kein echter Rabatt. Zum Weiterverhandeln "
            "interessant, aber kein Fund."
        )
    if deal.extraction_source == "fallback":
        description += (
            "\n⚠️ Extraktion: Regex-Fallback (LLM nicht erreichbar) – "
            "die Angaben bitte prüfen."
        )
    if valuation.price_rank is not None:
        description += f"\n📊 Preisrang p{round(valuation.price_rank)} in dieser Konfiguration"
    if valuation.notes:
        description += "\n" + " / ".join(valuation.notes)

    fields = [
        {"name": "💶 Preis", "value": _fmt_eur(valuation.asking_price), "inline": True},
        {"name": "📈 FMV (net)", "value": _fmt_eur(valuation.fmv_net), "inline": True},
        {"name": "📊 Marge", "value": f"{valuation.margin_pct:+.1f} %", "inline": True},
        {"name": "🏷️ Einstufung", "value": BAND_LABEL[valuation.band], "inline": True},
        {"name": "🗓️ Anzeige", "value": _ad_age_text(listing), "inline": True},
        {"name": "🧩 Hardware", "value": _components_text(deal), "inline": False},
        {"name": "👤 Verkäufer", "value": _seller_text(deal), "inline": False},
        {"name": "🚨 Risiko", "value": _risk_text(deal), "inline": False},
    ]

    embed = {
        "title": listing.title[:256],
        "url": listing.url,
        "description": description[:MAX_DESCRIPTION],
        "color": color,
        "fields": fields,
        "footer": {
            "text": (
                f"SnipeKlein · FMV-Deckung {valuation.coverage:.0%} · "
                f"Anzeige {listing.ad_id}"
            )[:2048]
        },
        "timestamp": datetime.now(UTC).isoformat(),
    }
    return embed


def _target_mention(target: str) -> tuple[str, str, str] | None:
    """Parse one ping target into ``(mention, kind, key)``.

    ``kind`` is ``users``, ``roles`` or ``broad``; ``key`` is the numeric ID or
    the normalized broad mention.  An invalid target is ignored instead of
    failing the alert: a typo in the environment must never suppress a deal
    notification.
    """
    raw = target.strip()
    if not raw:
        return None
    broadcast = _BROADCAST_TARGETS.get(raw.lower())
    if broadcast is not None:
        return broadcast, "broad", broadcast
    match = _TARGET_RE.match(raw)
    if match is None:
        log.warning("ignoring invalid Discord ping target %r", target)
        return None
    target_id = match.group("wrapped") or match.group("plain") or ""
    if match.group("role"):
        return f"<@&{target_id}>", "roles", target_id
    return f"<@{target_id}>", "users", target_id


def _target_mentions(value: str) -> tuple[str, dict[str, list[str]]] | None:
    """Turn a comma-separated list of ping targets into content + mentions.

    Empty entries and duplicates are ignored.  A valid entry is still used
    when another entry in the list is malformed.  ``everyone`` / ``here`` are
    explicit broad-mention opt-ins.
    """
    mentions: list[str] = []
    seen: set[tuple[str, str]] = set()
    users: list[str] = []
    roles: list[str] = []
    broad = False
    content_length = 0

    for entry in value.split(","):
        parsed = _target_mention(entry)
        if parsed is None:
            continue
        mention, kind, key = parsed
        identity = (kind, key)
        if identity in seen:
            continue
        seen.add(identity)
        separator = 1 if mentions else 0
        if content_length + separator + len(mention) > MAX_PING_CONTENT:
            log.warning(
                "ping target list is too long - ignoring the remaining targets"
            )
            break
        mentions.append(mention)
        content_length += separator + len(mention)
        if kind == "users":
            users.append(key)
        elif kind == "roles":
            roles.append(key)
        else:
            broad = True

    if not mentions:
        return None
    allowed_mentions: dict[str, list[str]] = {"parse": ["everyone"] if broad else []}
    if users:
        allowed_mentions["users"] = users
    if roles:
        allowed_mentions["roles"] = roles
    return " ".join(mentions), allowed_mentions


def build_payload(
    deal: Deal, username: str = "SnipeKlein", ping_target: str = ""
) -> dict:
    """Build the webhook payload, optionally mentioning Discord users/roles."""
    payload: dict = {"username": username, "embeds": [build_embed(deal)]}
    mention = _target_mentions(ping_target)
    if mention is not None:
        content, allowed_mentions = mention
        payload["content"] = content
        payload["allowed_mentions"] = allowed_mentions
    return payload


def payload_size(payload: dict) -> int:
    return sum(len(str(value)) for value in payload.get("embeds", []))
