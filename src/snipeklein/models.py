"""Domain models shared across the pipeline."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


def ad_age_days(posted_at: str, today: date | None = None) -> int | None:
    """Age of a listing in days from the date the detail page shows.

    Kleinanzeigen renders the posting date as ``29.10.2026``.  Returns
    ``None`` when the string is missing or in an unknown format.
    """
    text = (posted_at or "").strip()
    if not text:
        return None
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            posted = datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
        reference = today or datetime.now(UTC).date()
        return max(0, (reference - posted).days)
    return None


class Band(StrEnum):
    """Deal classification (spec 5.3)."""

    NOT_A_DEAL = "not_a_deal"
    ACTIONABLE = "actionable"
    EXTREME = "extreme"
    NEAR_MISS = "near_miss"


class RiskLevel(StrEnum):
    """Fraud risk badge (spec 6/7)."""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class ShippingMode(StrEnum):
    PICKUP = "pickup"
    SHIPPING = "shipping"
    UNKNOWN = "unknown"


class SearchCard(BaseModel):
    """A single search result card (spec 3.2)."""

    model_config = ConfigDict(extra="ignore")

    ad_id: str
    url: str
    title: str
    price: float | None = None
    price_is_negotiable: bool = False
    original_price: float | None = None
    location_text: str = ""
    snippet: str = ""
    posted_hint: str = ""
    image_count: int = 0
    shipping_hint: str = ""
    is_pro: bool = False
    query_tag: str = ""


class Listing(BaseModel):
    """Facts scraped from a listing detail page."""

    model_config = ConfigDict(extra="ignore")

    ad_id: str
    url: str
    title: str
    description: str = ""
    price: float | None = None
    price_is_negotiable: bool = False
    original_price: float | None = None
    plz: str = ""
    city: str = ""
    region: str = ""
    category_id: int | None = None
    posted_at: str = ""
    condition: str = ""
    condition_code: str = ""
    shipping_mode: ShippingMode = ShippingMode.UNKNOWN
    seller_name: str = ""
    seller_since: str = ""
    seller_since_date: datetime | None = None
    seller_badges: list[str] = Field(default_factory=list)
    seller_type: str = ""
    seller_active_ads: int = 0
    ad_type: str = ""
    fetched_at: datetime = Field(default_factory=datetime.now)


class Extraction(BaseModel):
    """Strict extraction schema (spec 4.2)."""

    model_config = ConfigDict(extra="ignore")

    is_complete_system: bool = False
    cpu_model: str | None = None
    gpu_model: str | None = None
    ram_generation: str = "UNKNOWN"
    ram_capacity_gb: int | None = None
    storage_capacity_gb: int | None = None
    motherboard_chipset: str | None = None
    psu_wattage: int | None = None
    operating_system_included: bool = False
    defects_or_missing_parts: list[str] = Field(default_factory=list)
    pickup_only: bool = False
    # optional: the accessories a bundle includes ("24 Zoll Monitor", ...)
    accessories: list[str] = Field(default_factory=list)


class ComponentValue(BaseModel):
    """A single component that was matched against the FMV table."""

    component_id: str
    label: str
    kind: str
    fmv_eur: float
    source: str = "curated"


class DefectCost(BaseModel):
    label: str
    cost_eur: float


class Valuation(BaseModel):
    """Result of the valuation engine (spec 5)."""

    fmv_gross: float = 0.0
    fmv_net: float = 0.0
    # the value of a complete system with the named parts (the unnamed parts
    # are assumed to be present and worth their median)
    fmv_estimate: float = 0.0
    # the calibrated multiple of the parts' sum that the market asks for
    assembly_factor: float = 1.0
    assembly_samples: int = 0
    # the market value of the accessories a bundle includes
    accessories_eur: float = 0.0
    # where the asking price sits in the market distribution (0-100 %)
    price_rank: float | None = None
    asking_price: float = 0.0
    margin_eur: float = 0.0
    margin_pct: float = 0.0
    band: Band = Band.NOT_A_DEAL
    coverage: float = 0.0
    components: list[ComponentValue] = Field(default_factory=list)
    defect_costs: list[DefectCost] = Field(default_factory=list)
    adjustments: list[str] = Field(default_factory=list)
    missing_components: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class RiskSignal(BaseModel):
    label: str
    points: int
    detail: str = ""


class RiskAssessment(BaseModel):
    """Fraud/risk heuristic result (spec 6)."""

    score: int = 0
    level: RiskLevel = RiskLevel.GREEN
    signals: list[RiskSignal] = Field(default_factory=list)

    @property
    def reasons(self) -> str:
        if not self.signals:
            return ""
        return ", ".join(f"{s.label} ({s.detail})" if s.detail else s.label for s in self.signals)


class Deal(BaseModel):
    """A fully evaluated deal, ready to be dispatched."""

    listing: Listing
    extraction: Extraction
    valuation: Valuation
    risk: RiskAssessment
    alert_key: str = ""
