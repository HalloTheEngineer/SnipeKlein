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
from typing import Any

from .config import Config
from .store.repo import ListingRepo, RunRepo, UsageRepo

STATUS_OK = "ok"
STATUS_STARTING = "starting"
STATUS_ATTENTION = "attention"

EXIT_CODES = {STATUS_OK: 0, STATUS_STARTING: 0, STATUS_ATTENTION: 1}


def collect_health(config: Config, conn: sqlite3.Connection) -> dict[str, Any]:
    """Build the health report of the running bot."""
    last_run = RunRepo(conn).last()
    seen = conn.execute("SELECT COUNT(*) AS c FROM seen_ads").fetchone()["c"]
    listings = ListingRepo(conn).count()
    alerts = conn.execute("SELECT COUNT(*) AS c FROM alerts").fetchone()["c"]
    llm_key = bool(config.llm.api_key())
    webhook = bool(config.notify.webhook_url())
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

    never_ran = not started_at
    stale_after = 3 * max(config.ingestion.poll_interval_max_s, 60)
    # A run that exists but cannot be dated is treated as stale.
    stale = not never_ran and (run_age_s is None or run_age_s > stale_after)
    errors = bool(last_run and last_run.get("errors"))

    missing: list[str] = []
    if not llm_key:
        missing.append("llm key")
    if not webhook:
        missing.append("discord webhook")

    if never_ran:
        status = STATUS_STARTING
    elif stale or errors or missing:
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
        "missing": missing,
        "llm_requests_today": today_requests,
        "llm_quota_errors_today": today_429,
        "llm_errors_today": today_errors,
        "queries": [q.tag for q in config.queries],
        "status": status,
    }


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
        f"llm key     : {'set' if report['llm_configured'] else 'MISSING'}",
        f"discord hook: {'set' if report['webhook_configured'] else 'MISSING'}",
        f"llm today   : {report['llm_requests_today']} requests, "
        f"{report['llm_errors_today']} errors, {report['llm_quota_errors_today']} quota errors",
        f"queries     : {', '.join(report['queries'])}",
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
    return "\n".join(lines)
