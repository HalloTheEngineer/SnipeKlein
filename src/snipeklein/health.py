"""Health report shared by the ``health`` command and the watchdog.

Three states are possible:

``ok``         the bot is running fine
``starting``   no run has been recorded yet (fresh deployment, first cycle
               pending) - this is *not* an error
``attention``  something is really wrong: a stale run, errors in the last
               run, or a missing secret
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from .config import Config
from .store.market import MarketRepo
from .store.repo import ListingRepo, RunRepo, UsageRepo

STATUS_OK = "ok"
STATUS_STARTING = "starting"
STATUS_ATTENTION = "attention"

EXIT_CODES = {STATUS_OK: 0, STATUS_STARTING: 0, STATUS_ATTENTION: 1}

# The price refresher runs once a day; two missed days is a real problem.
REFRESH_STALE_HOURS = 48


def _package_version() -> str:
    try:
        return version("snipeklein")
    except PackageNotFoundError:  # pragma: no cover - installed in production
        return "unknown"


def _age_hours(timestamp: Any) -> float | None:
    text = str(timestamp or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - moment).total_seconds() / 3600.0)


def collect_health(
    config: Config, conn: sqlite3.Connection, check_secrets: bool = True
) -> dict[str, Any]:
    """Build the health report of the running bot.

    ``check_secrets`` is False for the panel: it runs in its own container
    and cannot see the bot's secrets, so it must not report them as missing.
    The watchdog (which has them) stays the authority on that.
    """
    last_run = RunRepo(conn).last()
    seen = conn.execute("SELECT COUNT(*) AS c FROM seen_ads").fetchone()["c"]
    listings = ListingRepo(conn).count()
    alerts = conn.execute("SELECT COUNT(*) AS c FROM alerts").fetchone()["c"]
    llm_key = bool(config.llm.api_key()) if check_secrets else None
    webhook = bool(config.notify.webhook_url()) if check_secrets else None
    usage = UsageRepo(conn).daily(days=1)
    today_requests = sum(int(row["requests"]) for row in usage)
    today_429 = sum(int(row["quota_429"]) for row in usage)
    today_errors = sum(int(row["errors"]) for row in usage)

    run_age_s: float | None = None
    started_at = str(last_run.get("started_at")) if last_run else ""
    if started_at:
        try:
            started = datetime.fromisoformat(started_at)
            run_age_s = (datetime.now(UTC) - started).total_seconds()
        except ValueError:
            run_age_s = None

    # What the valuation is calibrated on.  A silently empty or stale market
    # was invisible for a week before, so it is part of the verdict now.
    calibration = MarketRepo(conn).calibration(
        days=config.valuation.assembly_window_days,
        sold_factor=config.valuation.asking_to_sold_factor,
    )
    price_row = conn.execute(
        "SELECT COUNT(*) AS c, MAX(updated_at) AS last_update FROM price_table"
    ).fetchone()
    measured_row = conn.execute(
        "SELECT COUNT(*) AS c FROM price_table WHERE source = 'kleinanzeigen'"
    ).fetchone()
    market_age_h = _age_hours(calibration.get("newest_sample_at"))
    price_age_h = _age_hours(price_row["last_update"])
    refresh_stale = (market_age_h is not None and market_age_h > REFRESH_STALE_HOURS) or (
        price_age_h is not None and price_age_h > REFRESH_STALE_HOURS
    )

    never_ran = not started_at
    stale_after = 3 * max(config.ingestion.poll_interval_max_s, 60)
    # A run that exists but cannot be dated is treated as stale.
    stale = not never_ran and (run_age_s is None or run_age_s > stale_after)
    errors = bool(last_run and last_run.get("errors"))

    missing: list[str] = []
    if check_secrets:
        if not llm_key:
            missing.append("llm key")
        if not webhook:
            missing.append("discord webhook")

    if never_ran:
        status = STATUS_STARTING
    elif stale or errors or missing or refresh_stale:
        status = STATUS_ATTENTION
    else:
        status = STATUS_OK

    return {
        "config": str(config.path),
        "db": str(config.db_path),
        "last_run": last_run,
        "last_run_age_s": round(run_age_s, 1) if run_age_s is not None else None,
        "never_ran": never_ran,
        "stale": stale,
        "stale_after_s": stale_after,
        "seen_ads": seen,
        "listings": listings,
        "alerts": alerts,
        "llm_configured": llm_key,
        "webhook_configured": webhook,
        "secrets": "checked" if check_secrets else "unknown",
        "missing": missing,
        "llm_requests_today": today_requests,
        "llm_quota_errors_today": today_429,
        "llm_errors_today": today_errors,
        "queries": [q.tag for q in config.queries],
        "version": _package_version(),
        "market": calibration,
        "prices": {
            "rows": int(price_row["c"]),
            "measured": int(measured_row["c"]),
            "last_update": price_row["last_update"],
        },
        "refresh": {
            "market_sample_age_h": round(market_age_h, 1) if market_age_h is not None else None,
            "price_age_h": round(price_age_h, 1) if price_age_h is not None else None,
            "stale": refresh_stale,
        },
        "status": status,
    }


def _secret_state(value: bool | None) -> str:
    if value is None:
        return "not visible here"
    return "set" if value else "MISSING"


def format_health(report: dict[str, Any]) -> str:
    """Render the health report as a human readable block."""
    lines = [
        f"config      : {report['config']}",
        f"database    : {report['db']}",
    ]
    if report["last_run"] and report.get("last_run_age_s") is not None:
        run = report["last_run"]
        lines.append(
            f"last run    : {run['started_at']} ({report['last_run_age_s']}s ago) "
            f"seen={run['ads_seen']} new={run['ads_new']} alerts={run['alerts_sent']} "
            f"errors={run['errors']}"
        )
    else:
        lines.append("last run    : never")
    lines += [
        f"seen ads    : {report['seen_ads']}",
        f"listings    : {report['listings']}",
        f"alerts      : {report['alerts']}",
        f"llm key     : {_secret_state(report['llm_configured'])}",
        f"discord hook: {_secret_state(report['webhook_configured'])}",
        f"llm today   : {report['llm_requests_today']} requests, "
        f"{report['llm_errors_today']} errors, {report['llm_quota_errors_today']} quota errors",
        f"queries     : {', '.join(report['queries'])}",
        f"version     : {report.get('version', 'unknown')}",
        (
            "calibration : "
            + (
                f"{report['market']['samples']} systems, factor {report['market']['factor']}"
                if report.get("market", {}).get("factor") is not None
                else "no measured systems yet"
            )
        ),
        (
            "refresh     : "
            + (
                f"market {report['refresh']['market_sample_age_h']}h ago, "
                f"prices {report['refresh']['price_age_h']}h ago"
                if report.get("refresh")
                else "unknown"
            )
        ),
        f"status      : {report['status'].upper()}",
    ]
    if report["status"] == STATUS_STARTING:
        lines.append("             -> no run recorded yet, the bot is still starting up")
    if report["stale"]:
        lines.append(
            f"             -> the last run is older than {report['stale_after_s']}s, "
            "the bot is probably stuck"
        )
    if report["last_run"] and report["last_run"].get("errors"):
        lines.append("             -> the last run had errors, check the logs")
    if report["missing"]:
        lines.append("             -> missing: " + ", ".join(report["missing"]))
    if (report.get("refresh") or {}).get("stale"):
        lines.append(
            "             -> the price/market refresh is stale "
            f"(limit {REFRESH_STALE_HOURS}h), check the price-refresher"
        )
    return "\n".join(lines)
