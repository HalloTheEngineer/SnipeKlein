"""Discord embed construction (spec 7)."""

from __future__ import annotations

from datetime import UTC, datetime

from ..models import (
    Band,
    Deal,
    RiskLevel,
    ShippingMode,
)

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
    if valuation.notes:
        description += "\n" + " / ".join(valuation.notes)

    fields = [
        {"name": "💶 Preis", "value": _fmt_eur(valuation.asking_price), "inline": True},
        {"name": "📈 FMV (net)", "value": _fmt_eur(valuation.fmv_net), "inline": True},
        {"name": "📊 Marge", "value": f"{valuation.margin_pct:+.1f} %", "inline": True},
        {"name": "🏷️ Einstufung", "value": BAND_LABEL[valuation.band], "inline": True},
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


def build_payload(deal: Deal, username: str = "SnipeKlein") -> dict:
    return {"username": username, "embeds": [build_embed(deal)]}


def payload_size(payload: dict) -> int:
    return sum(len(str(value)) for value in payload.get("embeds", []))
