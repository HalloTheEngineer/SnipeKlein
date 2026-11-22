"""Aggregate statistics for the read-only decision panel.

Everything here is derived from the existing tables; the panel never writes.
The response is intentionally one payload so the browser can render the
whole tab with a single request.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..config import Config
from ..models import ad_age_days
from ..store.market import MarketRepo
from ..store.repo import PriceRepo, UsageRepo
from ..valuation.table import PriceTable

OUTCOMES = ("alerted", "accepted", "near_miss", "not_target", "rejected")
BANDS = ("extreme", "actionable", "near_miss", "not_a_deal")
RISK_LEVELS = ("green", "yellow", "red")
AGE_BUCKETS = (
    "0-1 d",
    "2-3 d",
    "4-7 d",
    "8-14 d",
    "15-30 d",
    "31+ d",
    "unknown",
)
SERIES_KEYS = ("ads", "valued", "alerts", "cycles", "errors")

# The JSON columns are written by json.dumps, but an old or manually edited
# row must never break the whole statistics tab.
def _json(alias: str, path: str) -> str:
    detail = (
        f"CASE WHEN json_valid({alias}.detail) THEN {alias}.detail ELSE '{{}}' END"
    )
    return f"json_extract({detail}, '{path}')"


def _cohort_where(days: int, query_tag: str) -> tuple[str, list[Any], str]:
    clauses: list[str] = []
    params: list[Any] = []
    since = ""
    if days > 0:
        since = (datetime.now(UTC) - timedelta(days=days)).replace(
            microsecond=0
        ).isoformat()
        clauses.append("d.first_seen >= ?")
        params.append(since)
    if query_tag:
        clauses.append("d.query_tag = ?")
        params.append(query_tag)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params, since


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _numbers(values: list[Any]) -> list[float]:
    return [number for value in values if (number := _number(value)) is not None]


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return round(ordered[low], 2)
    value = ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    return round(value, 2)


def _pct(count: int, total: int) -> float:
    return round(count / total * 100, 1) if total else 0.0


def _histogram(
    values: list[float],
    bins: int = 16,
    low: float | None = None,
    high: float | None = None,
) -> list[dict[str, Any]]:
    """Fixed-width histogram with a shared domain for threshold overlays."""
    if not values:
        return []
    low = min(values) if low is None else low
    high = max(values) if high is None else high
    if high <= low:
        high = low + 1.0
    width = (high - low) / bins
    counts = [0] * bins
    for value in values:
        index = min(bins - 1, max(0, int((value - low) / width)))
        counts[index] += 1
    return [
        {
            "from": round(low + index * width, 2),
            "to": round(low + (index + 1) * width, 2),
            "count": counts[index],
        }
        for index in range(bins)
    ]


def _counts(values: list[Any], total: int) -> list[dict[str, Any]]:
    counter = Counter(
        "unknown" if value is None or str(value).strip() == "" else str(value)
        for value in values
    )
    return [
        {"key": key, "count": count, "pct": _pct(count, total)}
        for key, count in counter.most_common()
    ]


def _top(
    rows: list[dict[str, Any]], key: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Group rows by a label and keep count + median margin/asking price."""
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = str(row.get(key) or "").strip()
        if not label:
            continue
        group = groups.setdefault(
            label, {"label": label, "count": 0, "margins": [], "prices": []}
        )
        group["count"] += 1
        margin = _number(row.get("margin_pct"))
        price = _number(row.get("price"))
        if margin is not None:
            group["margins"].append(margin)
        if price is not None:
            group["prices"].append(price)
    items = [
        {
            "label": group["label"],
            "count": group["count"],
            "median_margin_pct": _median(group["margins"]),
            "median_asking_eur": _median(group["prices"]),
        }
        for group in groups.values()
    ]
    items.sort(key=lambda item: (-item["count"], item["label"].lower()))
    return items[:limit]


def _component_top(
    rows: list[dict[str, Any]], kind: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Group the matched components stored in ``extractions.components``."""
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("kind") or "") != kind:
            continue
        label = str(row.get("label") or "").strip()
        if not label:
            continue
        group = groups.setdefault(
            label, {"label": label, "count": 0, "margins": [], "prices": []}
        )
        group["count"] += 1
        margin = _number(row.get("margin_pct"))
        price = _number(row.get("price"))
        if margin is not None:
            group["margins"].append(margin)
        if price is not None:
            group["prices"].append(price)
    items = [
        {
            "label": group["label"],
            "count": group["count"],
            "median_margin_pct": _median(group["margins"]),
            "median_asking_eur": _median(group["prices"]),
        }
        for group in groups.values()
    ]
    items.sort(key=lambda item: (-item["count"], item["label"].lower()))
    return items[:limit]


def _ram_bucket(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "unknown"
    if number >= 64:
        return "64+ GB"
    if number >= 32:
        return "32 GB"
    if number >= 16:
        return "16 GB"
    if number >= 8:
        return "8 GB"
    return f"{int(number)} GB"


def _age_at(posted_at: str, first_seen: str) -> int | None:
    try:
        seen = datetime.fromisoformat(first_seen).date()
    except (TypeError, ValueError):
        return None
    return ad_age_days(posted_at or "", today=seen)


def _age_bucket(age: int | None) -> str:
    if age is None:
        return "unknown"
    if age <= 1:
        return "0-1 d"
    if age <= 3:
        return "2-3 d"
    if age <= 7:
        return "4-7 d"
    if age <= 14:
        return "8-14 d"
    if age <= 30:
        return "15-30 d"
    return "31+ d"


def _series(
    days: int, maps: dict[str, dict[str, int]]
) -> list[dict[str, Any]]:
    keys = set().union(*(set(values) for values in maps.values()))
    if days > 0:
        start = (datetime.now(UTC) - timedelta(days=max(0, days - 1))).date()
    elif keys:
        start = date.fromisoformat(min(keys))
    else:
        start = (datetime.now(UTC) - timedelta(days=6)).date()
    today = datetime.now(UTC).date()
    if start > today:
        start = today
    if (today - start).days > 180:
        start = today - timedelta(days=180)
    rows: list[dict[str, Any]] = []
    current = start
    while current <= today:
        day = current.isoformat()
        row: dict[str, Any] = {"day": day}
        for key in SERIES_KEYS:
            row[key] = int(maps.get(key, {}).get(day, 0))
        rows.append(row)
        current += timedelta(days=1)
    return rows


def _query_stats(
    core: list[dict[str, Any]],
    valued: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in core:
        tag = str(row.get("query_tag") or "(none)")
        group = groups.setdefault(
            tag, {"tag": tag, "ads": 0, "valued": 0, "alerts": 0, "margins": []}
        )
        group["ads"] += 1
    for row in valued:
        tag = str(row.get("query_tag") or "(none)")
        group = groups.setdefault(
            tag, {"tag": tag, "ads": 0, "valued": 0, "alerts": 0, "margins": []}
        )
        group["valued"] += 1
        margin = _number(row.get("margin_pct"))
        if margin is not None:
            group["margins"].append(margin)
    for row in alerts:
        tag = str(row.get("query_tag") or "(none)")
        group = groups.setdefault(
            tag, {"tag": tag, "ads": 0, "valued": 0, "alerts": 0, "margins": []}
        )
        group["alerts"] += 1
    items = []
    for group in groups.values():
        valued_count = group["valued"]
        items.append(
            {
                "tag": group["tag"],
                "ads": group["ads"],
                "valued": valued_count,
                "alerts": group["alerts"],
                "alert_rate_pct": _pct(group["alerts"], valued_count),
                "median_margin_pct": _median(group["margins"]),
            }
        )
    items.sort(key=lambda item: (-item["ads"], item["tag"]))
    return items[:12]


def build_statistics(
    conn: sqlite3.Connection, config: Config, days: int = 7, query_tag: str = ""
) -> dict[str, Any]:
    days = max(0, int(days))
    where, params, since = _cohort_where(days, query_tag)

    core = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT d.ad_id, d.outcome, d.stage, d.reason_code, d.query_tag,
                   d.price, d.card_price, d.first_seen, d.is_pro
            FROM ad_decisions d{where}
            """,
            params,
        )
    ]
    valued = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT e.ad_id, e.margin_pct, e.band, e.risk_score, e.risk_level,
                   e.coverage, e.fmv_net, e.created_at,
                   d.price, d.query_tag,
                   {_json("d", "$.extraction_source")} AS extraction_source,
                   {_json("d", "$.extraction.cpu_model")} AS cpu,
                   {_json("d", "$.extraction.gpu_model")} AS gpu,
                   {_json("d", "$.extraction.ram_capacity_gb")} AS ram
            FROM ad_decisions d
            JOIN extractions e ON e.ad_id = d.ad_id{where}
            """,
            params,
        )
    ]
    alerts = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT a.ad_id, a.band, a.margin_pct, a.risk_level, a.sent_at,
                   d.first_seen, d.price, d.query_tag, e.fmv_net
            FROM alerts a
            JOIN ad_decisions d ON d.ad_id = a.ad_id
            LEFT JOIN extractions e ON e.ad_id = a.ad_id{where}
            """,
            params,
        )
    ]
    listings = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT d.ad_id, d.outcome, d.price, d.card_price, d.first_seen,
                   d.is_pro, d.query_tag, e.margin_pct,
                   l.shipping_mode, l.condition, l.seller_type, l.seller_badges,
                   l.seller_name, l.seller_active_ads, l.posted_at, l.plz,
                   l.city, l.region, l.is_negotiable, l.ad_type
            FROM ad_decisions d
            JOIN listings l ON l.ad_id = d.ad_id
            LEFT JOIN extractions e ON e.ad_id = d.ad_id{where}
            """,
            params,
        )
    ]
    component_rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT d.price, e.margin_pct,
                   json_extract(j.value, '$.kind') AS kind,
                   json_extract(j.value, '$.label') AS label
            FROM ad_decisions d
            JOIN extractions e ON e.ad_id = d.ad_id,
                 json_each(
                   CASE WHEN json_valid(e.components) THEN e.components ELSE '[]' END
                 ) j{where}
            """,
            params,
        )
    ]

    total = len(core)
    valued_count = len(valued)
    alert_count = len(alerts)
    outcome_counts = Counter(row["outcome"] for row in core)
    stage_counts = Counter(row["stage"] for row in core)

    prefilter_fail = stage_counts.get("prefilter", 0)
    triage_fail = stage_counts.get("triage", 0)
    detail_fail = stage_counts.get("detail", 0)
    extraction_fail = stage_counts.get("extraction", 0)
    valuation_stage = stage_counts.get("valuation", 0) + stage_counts.get("alert", 0)
    not_target = outcome_counts.get("not_target", 0)
    deal_band = outcome_counts.get("accepted", 0) + outcome_counts.get("alerted", 0)
    alerted = outcome_counts.get("alerted", 0)

    funnel_counts = [
        ("ads seen", total),
        ("prefilter passed", total - prefilter_fail),
        ("triage passed", total - prefilter_fail - triage_fail),
        (
            "detail parsed",
            total - prefilter_fail - triage_fail - detail_fail,
        ),
        (
            "extraction passed",
            total - prefilter_fail - triage_fail - detail_fail - extraction_fail,
        ),
        ("valuation completed", valuation_stage),
        ("DDR5 target", valuation_stage - not_target),
        ("deal band", deal_band),
        ("alerted", alerted),
    ]
    funnel = [
        {
            "key": label.replace(" ", "_"),
            "label": label,
            "count": max(0, count),
            "pct": _pct(max(0, count), total),
        }
        for label, count in funnel_counts
    ]

    margin_values = _numbers([row["margin_pct"] for row in valued])
    coverage_values = _numbers([row["coverage"] for row in valued])
    fallback_count = sum(
        1 for row in valued if row.get("extraction_source") == "fallback"
    )
    potential_margin = sum(
        max(0.0, (_number(row.get("fmv_net")) or 0.0) - (_number(row.get("price")) or 0.0))
        for row in alerts
    )
    cards = {
        "ads": total,
        "valued": valued_count,
        "alerts": alert_count,
        "alert_rate_pct": _pct(alert_count, valued_count),
        "actionable_rate_pct": _pct(deal_band, valued_count),
        "median_margin_pct": _median(margin_values),
        "potential_margin_eur": round(potential_margin, 2),
        "median_coverage_pct": (
            round((_median(coverage_values) or 0.0) * 100, 1)
            if coverage_values
            else None
        ),
        "fallback_share_pct": _pct(fallback_count, valued_count),
    }

    outcomes = [
        {
            "key": key,
            "count": outcome_counts.get(key, 0),
            "pct": _pct(outcome_counts.get(key, 0), total),
        }
        for key in OUTCOMES
    ]
    band_counts = Counter(row["band"] for row in valued if row.get("band"))
    bands = [
        {
            "key": key,
            "count": band_counts.get(key, 0),
            "pct": _pct(band_counts.get(key, 0), valued_count),
        }
        for key in BANDS
    ]

    rejection_counts = Counter(
        (row["reason_code"] or "(none)", row["stage"])
        for row in core
        if row["outcome"] == "rejected"
    )
    rejected_total = outcome_counts.get("rejected", 0)
    rejections = [
        {
            "code": code,
            "stage": stage,
            "count": count,
            "pct": _pct(count, rejected_total),
        }
        for (code, stage), count in rejection_counts.most_common(15)
    ]

    margin = {
        "count": len(margin_values),
        "min": round(min(margin_values), 2) if margin_values else None,
        "max": round(max(margin_values), 2) if margin_values else None,
        "p25": _percentile(margin_values, 0.25),
        "median": _median(margin_values),
        "p75": _percentile(margin_values, 0.75),
        "bins": _histogram(margin_values, bins=16),
    }

    risk_level_counts = Counter(
        row["risk_level"] for row in valued if row.get("risk_level")
    )
    risk_levels = [
        {
            "key": key,
            "count": risk_level_counts.get(key, 0),
            "pct": _pct(risk_level_counts.get(key, 0), valued_count),
        }
        for key in RISK_LEVELS
    ]
    score_values = _numbers([row["risk_score"] for row in valued])
    signals = [
        {"label": row["label"], "count": int(row["count"])}
        for row in conn.execute(
            f"""
            SELECT json_extract(j.value, '$.label') AS label,
                   COUNT(*) AS count
            FROM ad_decisions d,
                 json_each(
                   CASE WHEN json_valid(d.detail)
                        THEN COALESCE({_json("d", "$.risk.signals")}, '[]')
                        ELSE '[]' END
                 ) j{where}
            GROUP BY label
            ORDER BY count DESC, label
            LIMIT 12
            """,
            params,
        )
        if row["label"]
    ]
    risk = {
        "levels": risk_levels,
        "signals": signals,
        "score_bins": _histogram(score_values, bins=10, low=0, high=100),
    }

    # The matched component labels are cleaner than the free-text extraction
    # ("NVIDIA GeForce RTX 4070" vs. a vendor-specific title).  Fall back to
    # the extraction when an old row has no components JSON.
    hardware = {
        "gpus": _component_top(component_rows, "gpu") or _top(valued, "gpu"),
        "cpus": _component_top(component_rows, "cpu") or _top(valued, "cpu"),
        "ram": _component_top(component_rows, "ram")
        or _counts([_ram_bucket(row.get("ram")) for row in valued], valued_count),
    }

    condition_values = [row.get("condition") for row in listings]
    shipping_values = [row.get("shipping_mode") for row in listings]
    seller_type_values = [row.get("seller_type") for row in listings]
    badges_present = 0
    for row in listings:
        try:
            badges = json.loads(row.get("seller_badges") or "[]")
        except (TypeError, ValueError):
            badges = []
        if badges:
            badges_present += 1
    listing_mix = {
        "count": len(listings),
        "shipping": _counts(shipping_values, len(listings)),
        "condition": _counts(condition_values, len(listings)),
        "seller_type": _counts(seller_type_values, len(listings)),
        "badges": [
            {
                "key": "with_badges",
                "count": badges_present,
                "pct": _pct(badges_present, len(listings)),
            },
            {
                "key": "without_badges",
                "count": len(listings) - badges_present,
                "pct": _pct(len(listings) - badges_present, len(listings)),
            },
        ],
        "negotiable": _counts(
            ["yes" if row.get("is_negotiable") else "no" for row in listings],
            len(listings),
        ),
        "pro": _counts(
            ["pro" if row.get("is_pro") else "private" for row in listings],
            len(listings),
        ),
    }

    alert_band_counts = Counter(row["band"] for row in alerts if row.get("band"))
    alert_risk_counts = Counter(
        row["risk_level"] for row in alerts if row.get("risk_level")
    )
    latencies: list[float] = []
    for row in alerts:
        try:
            sent = datetime.fromisoformat(row["sent_at"])
            seen = datetime.fromisoformat(row["first_seen"])
        except (TypeError, ValueError):
            continue
        latencies.append(max(0.0, (sent - seen).total_seconds()))
    alert_margins = _numbers([row["margin_pct"] for row in alerts])
    alert_stats = {
        "count": alert_count,
        "by_band": [
            {
                "key": key,
                "count": alert_band_counts.get(key, 0),
                "pct": _pct(alert_band_counts.get(key, 0), alert_count),
            }
            for key in BANDS
        ],
        "by_risk": [
            {
                "key": key,
                "count": alert_risk_counts.get(key, 0),
                "pct": _pct(alert_risk_counts.get(key, 0), alert_count),
            }
            for key in RISK_LEVELS
        ],
        "median_margin_pct": _median(alert_margins),
        "median_latency_s": _median(latencies),
    }

    price_pairs = [
        (_number(row.get("price")), _number(row.get("card_price")))
        for row in core
        if row.get("price") is not None and row.get("card_price") is not None
    ]
    changed_deltas = [
        abs(price - card)
        for price, card in price_pairs
        if price is not None and card is not None and abs(price - card) > 0.5
    ]
    price_freshness = {
        "with_both_prices": len(price_pairs),
        "changed": len(changed_deltas),
        "changed_share_pct": _pct(len(changed_deltas), len(price_pairs)),
        "median_delta_eur": _median(changed_deltas),
    }

    valued_ids = {row["ad_id"] for row in valued}
    missing_extraction = sum(
        1
        for row in core
        if row["stage"] in ("valuation", "alert") and row["ad_id"] not in valued_ids
    )
    quality = {
        "with_valuation": valued_count,
        "missing_listing": max(0, total - len(listings)),
        "missing_extraction": missing_extraction,
        "fallback_share_pct": _pct(fallback_count, valued_count),
        "coverage_bins": _histogram(coverage_values, bins=10, low=0, high=1),
        "price_freshness": price_freshness,
    }

    ages: list[tuple[int | None, str]] = []
    for row in listings:
        age = _age_at(str(row.get("posted_at") or ""), str(row.get("first_seen") or ""))
        ages.append((age, str(row.get("outcome") or "")))
    age_counts = Counter(_age_bucket(age) for age, _ in ages)
    known_ages = [age for age, _ in ages if age is not None]
    alert_ages = [
        age
        for age, outcome in ages
        if age is not None and outcome == "alerted"
    ]
    stale_share = _pct(
        sum(
            1
            for age in known_ages
            if config.filters.max_alert_age_days > 0
            and age > config.filters.max_alert_age_days
        ),
        len(known_ages),
    )
    age_stats = {
        "bins": [
            {"key": key, "count": age_counts.get(key, 0)}
            for key in AGE_BUCKETS
        ],
        "median_alert_age_days": _median([float(age) for age in alert_ages]),
        "stale_share_pct": stale_share,
        "known": len(known_ages),
    }

    seller_counts = Counter(
        str(row["seller_name"]).strip()
        for row in listings
        if str(row.get("seller_name") or "").strip()
    )
    sellers = {
        "distinct": len(seller_counts),
        "multi": sum(1 for count in seller_counts.values() if count > 1),
        "top": [
            {"seller": seller, "ads": count}
            for seller, count in seller_counts.most_common(10)
        ],
    }

    cities = _top(listings, "city", limit=10)
    regions = _top(listings, "region", limit=10)
    geography = {
        "cities": [
            {
                "city": row["label"],
                "count": row["count"],
                "median_margin_pct": row["median_margin_pct"],
            }
            for row in cities
        ],
        "regions": [
            {
                "region": row["label"],
                "count": row["count"],
                "median_margin_pct": row["median_margin_pct"],
            }
            for row in regions
        ],
    }

    table = PriceTable.load(config.prices_path, PriceRepo(conn).all_prices())
    measured = sum(1 for component in table.components if component.source == "kleinanzeigen")
    stale_cutoff = (datetime.now(UTC) - timedelta(days=7)).replace(microsecond=0).isoformat()
    component_updates = [
        str(component.updated_at) for component in table.components if component.updated_at
    ]
    stale_components = sum(
        1 for updated in component_updates if updated < stale_cutoff
    )
    system_row = conn.execute(
        "SELECT COUNT(*) AS c, MAX(observed_at) AS newest FROM market_systems"
    ).fetchone()
    observation_row = conn.execute(
        "SELECT COUNT(*) AS c, MAX(observed_at) AS newest FROM price_observations"
    ).fetchone()
    calibration = MarketRepo(conn).calibration(
        days=config.valuation.assembly_window_days,
        sold_factor=config.valuation.asking_to_sold_factor,
    )
    market = {
        "components": len(table.components),
        "measured": measured,
        "estimated": len(table.components) - measured,
        "newest_component_at": max(component_updates) if component_updates else None,
        "stale_components": stale_components,
        "stale_after_days": 7,
        "systems": int(system_row["c"] or 0) if system_row else 0,
        "newest_system_at": system_row["newest"] if system_row else None,
        "observations": int(observation_row["c"] or 0) if observation_row else 0,
        "newest_observation_at": observation_row["newest"] if observation_row else None,
        "calibration": calibration,
    }

    run_where = " WHERE started_at >= ?" if days > 0 else ""
    run_params = [since] if days > 0 else []
    runs = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT started_at, finished_at, ads_seen, ads_new, ads_evaluated,
                   alerts_sent, errors
            FROM runs{run_where}
            ORDER BY started_at
            """,
            run_params,
        )
    ]
    durations: list[float] = []
    for row in runs:
        try:
            started = datetime.fromisoformat(row["started_at"])
            finished = datetime.fromisoformat(row["finished_at"])
        except (TypeError, ValueError):
            continue
        durations.append(max(0.0, (finished - started).total_seconds()))
    llm_rows = UsageRepo(conn).daily(days=days if days > 0 else 90)
    llm_by_day: dict[str, dict[str, int]] = {}
    for row in llm_rows:
        day = str(row["day"])
        group = llm_by_day.setdefault(
            day, {"requests": 0, "tokens": 0, "errors": 0, "quota_429": 0}
        )
        group["requests"] += int(row["requests"] or 0)
        group["tokens"] += int(row["in_tokens"] or 0) + int(row["out_tokens"] or 0)
        group["errors"] += int(row["errors"] or 0)
        group["quota_429"] += int(row["quota_429"] or 0)
    llm: list[dict[str, Any]] = [
        {"day": day, **values} for day, values in sorted(llm_by_day.items())
    ]
    ops = {
        "cycles": len(runs),
        "error_cycles": sum(1 for row in runs if row.get("errors")),
        "errors": sum(int(row["errors"] or 0) for row in runs),
        "ads_seen": sum(int(row["ads_seen"] or 0) for row in runs),
        "ads_new": sum(int(row["ads_new"] or 0) for row in runs),
        "ads_evaluated": sum(int(row["ads_evaluated"] or 0) for row in runs),
        "alerts_sent": sum(int(row["alerts_sent"] or 0) for row in runs),
        "cycle_median_s": _median(durations),
        "llm": llm,
        "llm_requests": sum(row["requests"] for row in llm),
        "llm_tokens": sum(row["tokens"] for row in llm),
        "llm_errors": sum(row["errors"] for row in llm),
        "llm_quota_429": sum(row["quota_429"] for row in llm),
    }

    ads_by_day = Counter(str(row["first_seen"])[:10] for row in core)
    valued_by_day = Counter(
        str(row["created_at"])[:10] for row in valued if row.get("created_at")
    )
    alerts_by_day = Counter(
        str(row["sent_at"])[:10] for row in alerts if row.get("sent_at")
    )
    cycles_by_day = Counter(str(row["started_at"])[:10] for row in runs)
    errors_by_day: dict[str, int] = defaultdict(int)
    for row in runs:
        day = str(row["started_at"])[:10]
        errors_by_day[day] += int(row["errors"] or 0)
    series = _series(
        days,
        {
            "ads": ads_by_day,
            "valued": valued_by_day,
            "alerts": alerts_by_day,
            "cycles": cycles_by_day,
            "errors": dict(errors_by_day),
        },
    )

    return {
        "range": {
            "days": days,
            "since": since or None,
            "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "cohort": total,
        },
        "cards": cards,
        "funnel": funnel,
        "series": series,
        "outcomes": outcomes,
        "bands": bands,
        "rejections": rejections,
        "margin": margin,
        "risk": risk,
        "hardware": hardware,
        "listing_mix": listing_mix,
        "alerts": alert_stats,
        "quality": quality,
        "age": age_stats,
        "sellers": sellers,
        "geography": geography,
        "queries": _query_stats(core, valued, alerts),
        "market": market,
        "ops": ops,
    }
