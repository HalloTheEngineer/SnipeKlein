"""Fraud and risk heuristics (spec 6)."""

from __future__ import annotations

from datetime import UTC, datetime

from ..config import RiskConfig
from ..models import (
    Extraction,
    Listing,
    RiskAssessment,
    RiskLevel,
    RiskSignal,
    ShippingMode,
    Valuation,
)

GREEN_MAX = 19
YELLOW_MAX = 49


class RiskEngine:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def assess(
        self, listing: Listing, extraction: Extraction, valuation: Valuation
    ) -> RiskAssessment:
        signals: list[RiskSignal] = []

        age_days = self._account_age_days(listing)
        if age_days is None:
            signals.append(RiskSignal(label="account age unknown", points=5, detail=""))
        elif age_days <= self.config.account_new_days:
            signals.append(
                RiskSignal(
                    label="very new account",
                    points=35,
                    detail=f"registered {age_days}d ago",
                )
            )
        elif age_days <= self.config.account_very_new_days:
            signals.append(
                RiskSignal(
                    label="new account",
                    points=15,
                    detail=f"registered {age_days}d ago",
                )
            )

        if not listing.seller_badges:
            signals.append(
                RiskSignal(label="no rating badges", points=20, detail="no seller history")
            )

        extreme = valuation.margin_pct > 60.0
        if listing.shipping_mode is ShippingMode.SHIPPING:
            if extreme:
                signals.append(
                    RiskSignal(label="shipping + extreme margin", points=40, detail="")
                )
            else:
                signals.append(RiskSignal(label="shipping only", points=10, detail=""))

        if extreme:
            signals.append(
                RiskSignal(
                    label="extreme outlier margin",
                    points=30,
                    detail=f"{valuation.margin_pct:.0f}%",
                )
            )

        if (
            valuation.fmv_net > 0
            and valuation.asking_price > 0
            and valuation.asking_price < valuation.fmv_net * 0.35
        ):
            signals.append(
                RiskSignal(
                    label="price far below FMV",
                    points=25,
                    detail=f"{valuation.asking_price:.0f} vs {valuation.fmv_net:.0f}",
                )
            )

        if listing.seller_active_ads >= self.config.max_active_ads:
            signals.append(
                RiskSignal(
                    label="many active ads",
                    points=10,
                    detail=f"{listing.seller_active_ads} ads",
                )
            )

        if listing.seller_type.lower().startswith("gewerb") and valuation.margin_pct > 40.0:
            signals.append(RiskSignal(label="commercial seller, high margin", points=15, detail=""))

        if extraction.defects_or_missing_parts:
            points = min(30, 10 * len(extraction.defects_or_missing_parts))
            signals.append(
                RiskSignal(
                    label="defects listed",
                    points=points,
                    detail=", ".join(extraction.defects_or_missing_parts[:3]),
                )
            )

        score = sum(signal.points for signal in signals)
        level = self._level(score)
        return RiskAssessment(score=score, level=level, signals=signals)

    def _level(self, score: int) -> RiskLevel:
        if score <= GREEN_MAX:
            return RiskLevel.GREEN
        if score <= YELLOW_MAX:
            return RiskLevel.YELLOW
        return RiskLevel.RED

    def _account_age_days(self, listing: Listing) -> int | None:
        since = listing.seller_since_date
        if since is None:
            return None
        now = datetime.now(UTC).replace(tzinfo=None)
        return max(0, (now - since).days)
