"""Command line interface."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from typing import Any

from .config import Config, ConfigError, load_config
from .extract.extractor import Extractor
from .health import EXIT_CODES, collect_health, format_health
from .ingestion.client import KleinanzeigenClient
from .ingestion.detail_parser import parse_listing
from .logging_setup import get_logger, setup_logging
from .models import Band
from .notify.discord import DiscordError, DiscordNotifier, post_message
from .pipeline import Pipeline
from .schedule import next_daily_run, parse_hhmm
from .store.backup import create_backup
from .store.db import connect, migrate
from .store.repo import (
    AlertRepo,
    FilterStatsRepo,
    ListingRepo,
    PriceRepo,
    UsageRepo,
)
from .watchdog import Watchdog

log = get_logger("snipeklein.cli")


def _config(args: argparse.Namespace) -> Config:
    return load_config(getattr(args, "config", None))


def _db(config: Config) -> sqlite3.Connection:
    conn = connect(config.db_path)
    migrate(conn)
    return conn


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


# -- commands -------------------------------------------------------------
def cmd_poll(args: argparse.Namespace) -> int:
    config = _config(args)
    conn = _db(config)
    pipeline = Pipeline(config, conn)
    try:
        if args.reset_seen:
            conn.execute("DELETE FROM seen_ads")
            print("cleared the seen-id store")
        if args.once:
            result = pipeline.run_cycle(args.query or None, dry_run=args.dry_run)
            print(
                f"cycle finished: seen={result.ads_seen} new={result.ads_new} "
                f"evaluated={result.ads_evaluated} alerts={result.alerts_sent} "
                f"errors={result.errors}"
            )
            if result.rejected:
                print("rejected: " + ", ".join(f"{k}={v}" for k, v in result.rejected.items()))
            return 1 if result.errors else 0
        pipeline.poll_forever(args.query or None, dry_run=args.dry_run)
        return 0
    except KeyboardInterrupt:
        log.info("interrupted")
        return 0
    finally:
        pipeline.close()
        conn.close()


def cmd_value(args: argparse.Namespace) -> int:
    config = _config(args)
    conn = _db(config)
    pipeline = Pipeline(config, conn)
    try:
        target = args.target
        if target.startswith("http"):
            url = target
        else:
            row = conn.execute(
                "SELECT url FROM listings WHERE ad_id = ?", (target,)
            ).fetchone()
            if row is None:
                print(f"unknown listing id: {target}", file=sys.stderr)
                return 2
            url = row["url"]

        html = pipeline.client.get(url).text
        listing = parse_listing(html, url, config.ingestion.base_url)
        outcome = pipeline.extractor.extract(listing.title, listing.description)
        valuation = pipeline.engine.value(outcome.extraction, listing)
        risk = pipeline.risk_engine.assess(listing, outcome.extraction, valuation)

        if args.embed:
            from .models import Deal
            from .notify.templates import build_payload

            deal = Deal(
                listing=listing,
                extraction=outcome.extraction,
                valuation=valuation,
                risk=risk,
            )
            _print_json(build_payload(deal, config.notify.username))
            return 0

        payload = {
            "listing": listing.model_dump(mode="json"),
            "extraction": outcome.extraction.model_dump(mode="json"),
            "extraction_source": outcome.source,
            "rejected": outcome.rejected,
            "valuation": valuation.model_dump(mode="json"),
            "risk": risk.model_dump(mode="json"),
        }
        if args.json:
            _print_json(payload)
        else:
            print(f"title      : {listing.title}")
            print(f"price      : {listing.price} EUR (negotiable={listing.price_is_negotiable})")
            print(f"shipping   : {listing.shipping_mode.value}")
            print(f"seller     : {listing.seller_name} (seit {listing.seller_since})")
            print(f"components : {outcome.extraction.model_dump()}")
            print(f"fmv gross  : {valuation.fmv_gross:.0f} EUR")
            print(f"fmv net    : {valuation.fmv_net:.0f} EUR")
            print(f"margin     : {valuation.margin_pct:.1f} % ({valuation.band.value})")
            print(f"coverage   : {valuation.coverage:.0%}")
            print(f"risk       : {risk.level.value} ({risk.score}) {risk.reasons}")
        return 0
    finally:
        pipeline.close()
        conn.close()


def _daily_refresh_loop(args: argparse.Namespace, config: Config) -> int:
    """Refresh the price table once a day at a fixed local time."""
    from .valuation.harvester import PriceHarvester
    from .valuation.table import PriceTable

    hour, minute = parse_hhmm(args.daily_at)
    conn = _db(config)
    try:
        repo = PriceRepo(conn)
        while True:
            now = datetime.now()
            target = next_daily_run(now, hour, minute)
            wait = max(0.0, (target - now).total_seconds())
            print(
                f"[price-refresher] next refresh at {target.isoformat(timespec='seconds')} "
                f"(in {wait / 3600:.1f} h)",
                flush=True,
            )
            time.sleep(wait)
            table = PriceTable.load(config.prices_path, repo.all_prices())
            client = KleinanzeigenClient(config.ingestion)
            try:
                harvester = PriceHarvester(
                    client,
                    table,
                    config.valuation,
                    repo,
                    config.ingestion.base_url,
                    force=args.force,
                    curated=PriceTable.load(config.prices_path),
                )
                results = harvester.harvest()
            except Exception as exc:
                print(f"[price-refresher] refresh failed: {exc}", file=sys.stderr, flush=True)
            else:
                refreshed = [r for r in results if r.fmv_eur is not None]
                print(
                    f"[price-refresher] refreshed {len(refreshed)}/{len(results)} components",
                    flush=True,
                )
            finally:
                client.close()
            if args.backup:
                created = create_backup(
                    config.db_path,
                    config.db_path.parent / "backups",
                    config.prices_path,
                    keep=args.keep,
                )
                for path in created:
                    print(f"[price-refresher] backup {path}", flush=True)
    except KeyboardInterrupt:
        return 0
    finally:
        conn.close()


def cmd_prices(args: argparse.Namespace) -> int:
    config = _config(args)
    conn = _db(config)
    try:
        if args.action == "show":
            from .valuation.table import PriceTable

            repo = PriceRepo(conn)
            table = PriceTable.load(config.prices_path, repo.all_prices())
            rows = [
                {
                    "id": c.id,
                    "kind": c.kind,
                    "label": c.label,
                    "fmv_eur": c.fmv_eur,
                    "source": c.source,
                    "samples": c.sample_n,
                }
                for c in table.components
            ]
            if args.json:
                _print_json(rows)
            else:
                for row in rows:
                    print(
                        f"{row['id']:<34} {row['fmv_eur']:>8.0f} EUR  "
                        f"[{row['source']} n={row['samples']}]"
                    )
                print(f"\n{len(rows)} components (table version {table.version})")
            return 0

        if args.action == "import":
            from .valuation.harvester import import_prices_from_file

            count = import_prices_from_file(args.file, PriceRepo(conn))
            print(f"imported {count} price rows")
            return 0

        # refresh
        from .valuation.harvester import PriceHarvester
        from .valuation.table import PriceTable

        if args.daily_at:
            return _daily_refresh_loop(args, config)

        repo = PriceRepo(conn)
        if args.reset_observations:
            conn.execute("DELETE FROM price_observations")
            conn.execute("DELETE FROM price_table")
            print("cleared the price observations and the harvested table", file=sys.stderr)
        curated = PriceTable.load(config.prices_path)
        table = PriceTable.load(config.prices_path, repo.all_prices())
        client = KleinanzeigenClient(config.ingestion)
        try:
            harvester = PriceHarvester(
                client,
                table,
                config.valuation,
                repo,
                config.ingestion.base_url,
                force=args.force,
                curated=curated,
            )
            results = harvester.harvest()
        finally:
            client.close()
        updated = [r for r in results if r.fmv_eur is not None]
        if args.json:
            _print_json([r.__dict__ for r in results])
        else:
            for result in results:
                value = f"{result.fmv_eur:.0f} EUR" if result.fmv_eur else "no data"
                print(f"{result.component_id:<34} n={result.samples:<3} {value}")
            print(f"\n{len(updated)}/{len(results)} components refreshed")
        return 0
    finally:
        conn.close()


def cmd_listings(args: argparse.Namespace) -> int:
    config = _config(args)
    conn = _db(config)
    try:
        repo = ListingRepo(conn)
        items = repo.recent(hours=args.hours, limit=args.limit)
        rows = []
        for listing in items:
            extraction = conn.execute(
                "SELECT fmv_net, margin_pct, band, risk_level FROM extractions WHERE ad_id = ?",
                (listing.ad_id,),
            ).fetchone()
            rows.append(
                {
                    "ad_id": listing.ad_id,
                    "title": listing.title,
                    "price": listing.price,
                    "fmv_net": extraction["fmv_net"] if extraction else None,
                    "margin_pct": extraction["margin_pct"] if extraction else None,
                    "band": extraction["band"] if extraction else None,
                    "risk": extraction["risk_level"] if extraction else None,
                    "url": listing.url,
                }
            )
        if args.json:
            _print_json(rows)
        else:
            for row in rows:
                margin = f"{row['margin_pct']:+.1f}%" if row["margin_pct"] is not None else "-"
                band = str(row["band"] or "-")
                print(
                    f"{row['ad_id']:<12} {str(row['price'] or '-'):>8} EUR  {margin:>8}  "
                    f"{band:<12} {str(row['title'])[:60]}"
                )
            print(f"\n{len(rows)} listings")
        return 0
    finally:
        conn.close()


def cmd_alerts(args: argparse.Namespace) -> int:
    config = _config(args)
    conn = _db(config)
    try:
        rows = AlertRepo(conn).recent(limit=args.limit)
        if args.band and args.band != "all":
            rows = [r for r in rows if r["band"] == args.band]
        if args.json:
            _print_json(rows)
        else:
            for row in rows:
                print(
                    f"{row['sent_at']}  {row['ad_id']:<12} {row['band']:<12} "
                    f"{row['margin_pct'] or 0:+.1f}%  risk={row['risk_level']}"
                )
            print(f"\n{len(rows)} alerts")
        return 0
    finally:
        conn.close()


def cmd_filter_report(args: argparse.Namespace) -> int:
    config = _config(args)
    conn = _db(config)
    try:
        rows = FilterStatsRepo(conn).report(days=args.days)
        if args.json:
            _print_json(rows)
        else:
            for row in rows:
                print(f"{row['reason']:<28} {row['count']}")
            print(f"\n{len(rows)} reasons")
        return 0
    finally:
        conn.close()


def cmd_usage(args: argparse.Namespace) -> int:
    config = _config(args)
    conn = _db(config)
    try:
        rows = UsageRepo(conn).daily(days=args.days)
        if args.json:
            _print_json(rows)
        else:
            for row in rows:
                print(
                    f"{row['day']}  {row['model']:<26} requests={row['requests']:<6} "
                    f"in={row['in_tokens']:<9} out={row['out_tokens']:<9} "
                    f"errors={row['errors']} 429={row['quota_429']}"
                )
            total = sum(r["requests"] for r in rows)
            print(f"\n{total} requests in the last {args.days} day(s)")
        return 0
    finally:
        conn.close()


def cmd_health(args: argparse.Namespace) -> int:
    config = _config(args)
    conn = _db(config)
    try:
        report = collect_health(config, conn)
        if args.json:
            _print_json(report)
        else:
            print(format_health(report))
        return EXIT_CODES.get(report["status"], 1)
    finally:
        conn.close()


def cmd_watchdog(args: argparse.Namespace) -> int:
    """Watch the bot and report problems to Discord."""
    config = _config(args)
    webhook = config.notify.webhook_url()
    if not webhook:
        print(
            f"webhook url missing - set {config.notify.webhook_url_env} in the environment",
            file=sys.stderr,
        )
        return 2

    def report() -> dict:
        conn = _db(config)
        try:
            return collect_health(config, conn)
        finally:
            conn.close()

    def notify(message: str) -> bool:
        try:
            post_message(webhook, message, config.notify.username)
            return True
        except DiscordError as exc:
            print(f"could not deliver the watchdog message: {exc}", file=sys.stderr)
            return False

    state_path = config.db_path.parent / "watchdog_state.json"
    watchdog = Watchdog(
        config=config,
        report=report,
        notify=notify,
        interval=args.interval if args.interval is not None else config.watchdog.interval,
        grace=args.grace if args.grace is not None else config.watchdog.grace,
        failures=args.failures if args.failures is not None else config.watchdog.failures,
        cooldown=args.cooldown if args.cooldown is not None else config.watchdog.cooldown,
        state_path=state_path,
    )
    try:
        watchdog.run()
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_panel(args: argparse.Namespace) -> int:
    """Serve the read-only decision panel."""
    from .panel.server import check, panel_config_from_env, serve

    config = _config(args)
    if args.check:
        return check(config)
    panel_config = panel_config_from_env(config, host=args.host, port=args.port)
    try:
        serve(config, panel_config)
    except KeyboardInterrupt:
        return 0
    except SystemExit:
        raise
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Write a consistent snapshot of the database and the price table."""
    config = _config(args)
    backup_dir = config.db_path.parent / "backups"
    created = create_backup(
        config.db_path,
        backup_dir,
        config.prices_path,
        keep=args.keep,
    )
    for path in created:
        print(f"wrote {path}")
    if not created:
        print("nothing to back up")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    try:
        config = _config(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    print(f"config ok: {config.path}")
    print(f"  queries   : {len(config.queries)}")
    print(f"  db        : {config.db_path}")
    print(f"  prices    : {config.prices_path}")
    print(f"  llm       : {config.llm.provider} / {config.llm.model}")
    print(f"  webhook   : {'set' if config.notify.webhook_url() else 'MISSING'}")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    """Smoke test the extraction stage against the configured LLM."""
    config = _config(args)
    extractor = Extractor(config.llm, required_ram_generation=config.filters.required_ram_generation)
    title = args.title or "Gaming PC Ryzen 7 7800X3D RTX 4070 32GB DDR5 1TB NVMe"
    description = args.description or (
        "Ich verkaufe meinen Gaming-PC. AMD Ryzen 7 7800X3D, NVIDIA RTX 4070 12GB, "
        "32 GB DDR5 RAM, 1 TB NVMe SSD, B650 Mainboard, 750W Netzteil, Windows 11. "
        "Nur Abholung."
    )
    outcome = extractor.extract(title, description)
    payload = {
        "source": outcome.source,
        "model": outcome.model,
        "degraded": outcome.degraded,
        "rejected": outcome.rejected,
        "extraction": outcome.extraction.model_dump(mode="json"),
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"source   : {outcome.source} ({outcome.model})")
        print(f"degraded : {outcome.degraded}")
        print(f"rejected : {outcome.rejected or '-'}")
        for key, value in outcome.extraction.model_dump().items():
            print(f"  {key:<26} {value}")
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    """Show the cheap triage result for a listing (debugging aid)."""
    config = _config(args)
    conn = _db(config)
    pipeline = Pipeline(config, conn)
    try:
        target = args.target
        if target.startswith("http"):
            url = target
        else:
            row = conn.execute(
                "SELECT url FROM listings WHERE ad_id = ?", (target,)
            ).fetchone()
            if row is None:
                print(f"unknown listing id: {target}", file=sys.stderr)
                return 2
            url = row["url"]
        html = pipeline.client.get(url).text
        listing = parse_listing(html, url, config.ingestion.base_url)
        from .models import SearchCard

        card = SearchCard(
            ad_id=listing.ad_id,
            url=listing.url,
            title=listing.title,
            price=listing.price,
            snippet=listing.description[:300],
        )
        result = pipeline.triage.evaluate(card)
        print(f"title   : {listing.title}")
        print(f"price   : {listing.price} EUR")
        print(f"passed  : {result.passed} ({result.reason})")
        print(f"coarse  : FMV {result.coarse_fmv:.0f} EUR, margin {result.coarse_margin_pct:.1f}%")
        for component in result.components:
            print(f"  - {component.label:<32} {component.fmv_eur:>7.0f} EUR")
        return 0
    finally:
        pipeline.close()
        conn.close()


def cmd_test_alert(args: argparse.Namespace) -> int:
    """Send a synthetic deal to the Discord webhook (webhook smoke test)."""
    from datetime import datetime, timedelta

    from .models import (
        Deal,
        Extraction,
        Listing,
        RiskAssessment,
        RiskLevel,
        ShippingMode,
        Valuation,
    )
    from .notify.templates import build_payload
    from .store.repo import AlertRepo

    config = _config(args)
    conn = _db(config)
    try:
        listing = Listing(
            ad_id="TEST-ALERT-0001",
            url="https://www.kleinanzeigen.de/s-suchergebnisse/pcs/k0c228",
            title="Gaming PC Ryzen 7 7800X3D RTX 4070 32GB DDR5 1TB NVMe (Test)",
            description="Test alert - kein echtes Angebot.",
            price=750.0,
            price_is_negotiable=True,
            plz="12345",
            city="Berlin",
            region="Berlin",
            category_id=228,
            posted_at="20.10.2026",
            condition="Sehr Gut",
            condition_code="like_new",
            shipping_mode=ShippingMode.PICKUP,
            seller_name="SnipeKlein Test",
            seller_since="01.01.2018",
            seller_since_date=datetime.now() - timedelta(days=2500),
            seller_badges=["TOP", "Sehr freundlich"],
            seller_type="Privater Nutzer",
            seller_active_ads=3,
        )
        extraction = Extraction(
            is_complete_system=True,
            cpu_model="AMD Ryzen 7 7800X3D",
            gpu_model="NVIDIA GeForce RTX 4070",
            ram_generation="DDR5",
            ram_capacity_gb=32,
            storage_capacity_gb=1024,
            motherboard_chipset="B650",
            psu_wattage=750,
            operating_system_included=True,
            pickup_only=True,
        )
        valuation = Valuation(
            fmv_gross=1150.0,
            fmv_net=1042.0,
            asking_price=750.0,
            margin_eur=292.0,
            margin_pct=28.0,
            band=Band.ACTIONABLE,
            coverage=1.0,
        )
        risk = RiskAssessment(score=0, level=RiskLevel.GREEN)
        deal = Deal(listing=listing, extraction=extraction, valuation=valuation, risk=risk)

        payload = build_payload(deal, config.notify.username)
        if args.dry_run:
            _print_json(payload)
            return 0

        notifier = DiscordNotifier(config.notify, AlertRepo(conn))
        try:
            sent = notifier.send(deal)
        finally:
            notifier.close()
        print("alert sent" if sent else "alert skipped (already sent)")
        return 0 if sent else 1
    finally:
        conn.close()


def cmd_notify(args: argparse.Namespace) -> int:
    """Post a plain text message to the Discord webhook."""
    import httpx

    config = _config(args)
    url = config.notify.webhook_url()
    if not url:
        print(f"webhook url missing - set {config.notify.webhook_url_env} in .env", file=sys.stderr)
        return 2
    payload = {"username": config.notify.username, "content": args.message}
    try:
        response = httpx.post(url, json=payload, timeout=20)
    except httpx.HTTPError as exc:
        print(f"could not deliver the message: {exc}", file=sys.stderr)
        return 1
    if response.status_code in (200, 204):
        print("message sent")
        return 0
    print(f"discord returned HTTP {response.status_code}", file=sys.stderr)
    return 1


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete expired data (seen-ids, old listings, old price observations)."""
    config = _config(args)
    conn = _db(config)
    try:
        from .store.decisions import DecisionRepo
        from .store.repo import MaintenanceRepo, SeenStore

        removed = SeenStore(conn).purge(config.seen_ttl_days)
        print(f"removed {removed} expired seen-ids")
        stats = MaintenanceRepo(conn).prune(
            seen_ttl_days=config.seen_ttl_days,
            observation_days=config.observation_retention_days,
            listing_days=config.listing_retention_days,
        )
        for table, count in stats.items():
            if count:
                print(f"removed {count} rows from {table}")
        decisions = DecisionRepo(conn).prune(
            retention_days=config.panel.decision_retention_days,
            max_rows=config.panel.max_decision_rows,
        )
        for table, count in decisions.items():
            if count:
                print(f"removed {count} decision rows ({table})")
        return 0
    finally:
        conn.close()


# -- argument parsing -----------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snipeklein",
        description="Kleinanzeigen deal sniper: monitor, value and alert on underpriced PCs.",
    )
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--json", action="store_true", help="machine readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_json(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="machine readable output",
        )
        return p

    poll = sub.add_parser("poll", help="poll for new listings")
    poll.add_argument("--once", action="store_true", help="run a single cycle and exit")
    poll.add_argument("--query", action="append", help="limit to a query tag (repeatable)")
    poll.add_argument("--dry-run", action="store_true", help="do not send discord alerts")
    poll.add_argument(
        "--reset-seen",
        action="store_true",
        help="re-process listings that were already seen (after a filter/price change)",
    )
    poll.set_defaults(func=cmd_poll)

    value = sub.add_parser("value", help="value a single listing")
    value.add_argument("target", help="ad id or url")
    value.add_argument("--embed", action="store_true", help="print the discord embed json")
    value.set_defaults(func=cmd_value)

    prices = sub.add_parser("prices", help="manage the component FMV table")
    prices.add_argument("action", choices=["refresh", "show", "import"])
    prices.add_argument("file", nargs="?", help="csv file for 'import'")
    prices.add_argument(
        "--force",
        action="store_true",
        help="apply harvested prices even if they deviate strongly from the curated values",
    )
    prices.add_argument(
        "--reset-observations",
        action="store_true",
        help="delete the stored price observations before harvesting",
    )
    prices.add_argument(
        "--daily-at",
        metavar="HH:MM",
        help="run the refresh once a day at the given local time instead of once",
    )
    prices.add_argument(
        "--backup",
        action="store_true",
        help="write a consistent backup after every refresh (only with --daily-at)",
    )
    prices.add_argument("--keep", type=int, default=14, help="number of backups to keep")
    prices.set_defaults(func=cmd_prices)

    listings = sub.add_parser("listings", help="show ingested listings")
    listings.add_argument("--hours", type=int, default=24)
    listings.add_argument("--limit", type=int, default=50)
    listings.set_defaults(func=cmd_listings)

    alerts = sub.add_parser("alerts", help="show sent alerts")
    alerts.add_argument("--limit", type=int, default=50)
    alerts.add_argument("--band", default="all", choices=["all", Band.ACTIONABLE.value, Band.EXTREME.value])
    alerts.set_defaults(func=cmd_alerts)

    report = sub.add_parser("filter-report", help="pre-filter rejection statistics")
    report.add_argument("--days", type=int, default=7)
    report.set_defaults(func=cmd_filter_report)

    usage = sub.add_parser("usage", help="LLM usage statistics")
    usage.add_argument("--days", type=int, default=7)
    usage.set_defaults(func=cmd_usage)

    health = sub.add_parser("health", help="show the state of the bot")
    health.set_defaults(func=cmd_health)

    conf = sub.add_parser("config", help="validate the configuration")
    conf.add_argument("action", nargs="?", default="validate", choices=["validate"])
    conf.set_defaults(func=cmd_config)

    extract = sub.add_parser("extract", help="smoke test the LLM extraction")
    extract.add_argument("--title")
    extract.add_argument("--description")
    extract.set_defaults(func=cmd_extract)

    triage = sub.add_parser("triage", help="explain the cheap triage decision")
    triage.add_argument("target", help="ad id or url")
    triage.set_defaults(func=cmd_triage)

    prune = sub.add_parser("prune", help="delete expired data")
    prune.set_defaults(func=cmd_prune)

    notify = sub.add_parser("notify", help="post a plain text message to the discord webhook")
    notify.add_argument("message", help="the text to send")
    notify.set_defaults(func=cmd_notify)

    watchdog = sub.add_parser("watchdog", help="watch the bot and report problems to discord")
    watchdog.add_argument(
        "--interval", type=int, default=None, help="seconds between checks (config: 300)"
    )
    watchdog.add_argument(
        "--grace", type=int, default=None, help="seconds without alarming after the start (config: 600)"
    )
    watchdog.add_argument(
        "--failures", type=int, default=None, help="consecutive failed checks before an alarm (config: 3)"
    )
    watchdog.add_argument(
        "--cooldown", type=int, default=None, help="seconds between repeated alarms (config: 21600)"
    )
    watchdog.set_defaults(func=cmd_watchdog)

    panel = sub.add_parser("panel", help="serve the read-only decision panel")
    panel.add_argument("--host", default="", help="interface to bind (default 127.0.0.1)")
    panel.add_argument("--port", type=int, default=0, help="port to listen on (default 8137)")
    panel.add_argument(
        "--check", action="store_true", help="only verify the database and exit"
    )
    panel.set_defaults(func=cmd_panel)

    backup = sub.add_parser("backup", help="write a consistent snapshot of the state")
    backup.add_argument("--keep", type=int, default=14, help="number of backups to keep")
    backup.set_defaults(func=cmd_backup)

    test_alert = sub.add_parser("test-alert", help="send a synthetic deal to the discord webhook")
    test_alert.add_argument("--dry-run", action="store_true", help="only print the embed")
    test_alert.set_defaults(func=cmd_test_alert)

    for action in sub.choices.values():
        add_json(action)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(getattr(args, "config", None))
        setup_logging(config.log_level)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log.exception("command failed: %s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
