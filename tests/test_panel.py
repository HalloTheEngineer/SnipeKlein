"""The read-only panel: auth, API and the read-only database connection."""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from snipeklein.config import (
    Config,
    FilterConfig,
    IngestionConfig,
    LlmConfig,
    NotifyConfig,
    RiskConfig,
    StorageConfig,
    ValuationConfig,
)
from snipeklein.panel.api import PanelData, open_readonly
from snipeklein.panel.server import PanelConfig, check
from snipeklein.store.db import connect, migrate
from snipeklein.store.decisions import OUTCOME_REJECTED, Decision, DecisionRepo

USER = "panel-user"
PASSWORD = "s3cret"


def _config(tmp_path: Path) -> Config:
    return Config(
        general={"db_path": str(tmp_path / "test.db")},
        ingestion=IngestionConfig(),
        filters=FilterConfig(),
        valuation=ValuationConfig(),
        llm=LlmConfig(api_key_env="MISSING_KEY_FOR_TESTS"),
        notify=NotifyConfig(webhook_url_env="MISSING_HOOK_FOR_TESTS"),
        risk=RiskConfig(),
        storage=StorageConfig(),
    )


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    config = _config(tmp_path)
    conn = connect(config.db_path)
    migrate(conn)
    repo = DecisionRepo(conn)
    repo.record(
        Decision(
            ad_id="1",
            outcome=OUTCOME_REJECTED,
            stage="prefilter",
            reason="older platform",
            reason_code="spec_exclude",
            title="Gaming PC DDR4",
            url="https://example.invalid/1",
            price=500.0,
            steps=[{"stage": "prefilter", "outcome": "rejected", "reason": "spec_exclude"}],
        )
    )
    conn.close()
    return config


def test_open_readonly_cannot_write(config: Config) -> None:
    conn = open_readonly(config.db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM ad_decisions")
    finally:
        conn.close()


def test_open_readonly_missing_database(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        open_readonly(tmp_path / "nope.db")


def test_check_reports_the_state(config: Config) -> None:
    assert check(config) == 0


def test_check_fails_without_a_database(tmp_path: Path) -> None:
    assert check(_config(tmp_path)) == 1


def test_summary_and_ads(config: Config) -> None:
    data = PanelData(config)
    try:
        summary = data.summary()
        assert summary["decisions"]["total"] == 1
        payload = data.ads(limit=10)
        assert payload["total"] == 1
        assert payload["ads"][0]["ad_id"] == "1"
        assert payload["ads"][0]["steps"][0]["stage"] == "prefilter"
        ad = data.ad("1")
        assert ad is not None and ad["title"] == "Gaming PC DDR4"
        assert data.ad("unknown") is None
    finally:
        data.close()


def test_panel_requires_authentication(config: Config) -> None:
    server, thread = _start_server(config)
    try:
        status, _ = _request(server, "/api/summary", auth=False)
        assert status == 401
        status, _ = _request(server, "/api/summary", auth=True)
        assert status == 200
        status, _ = _request(server, "/api/summary", auth=True, password="wrong")
        assert status == 401
    finally:
        _stop_server(server, thread)


def test_panel_only_serves_get(config: Config) -> None:
    server, thread = _start_server(config)
    try:
        status, _ = _request(server, "/api/ads", auth=True, method="POST")
        assert status == 405
    finally:
        _stop_server(server, thread)


def test_panel_filters_by_near_miss(config: Config) -> None:
    from snipeklein.store.decisions import OUTCOME_NEAR_MISS, Decision, DecisionRepo

    conn = connect(config.db_path)
    DecisionRepo(conn).record(
        Decision(
            ad_id="2",
            outcome=OUTCOME_NEAR_MISS,
            stage="valuation",
            reason="near miss: 22.0% below market, not a deal",
            reason_code="near_miss",
            title="Gaming PC near miss",
            url="https://example.invalid/2",
            price=900.0,
        )
    )
    conn.close()

    data = PanelData(config)
    try:
        payload = data.ads(outcome=OUTCOME_NEAR_MISS)
        assert payload["total"] == 1
        assert payload["ads"][0]["ad_id"] == "2"
        assert data.summary()["decisions"]["by_outcome"][OUTCOME_NEAR_MISS] == 1
    finally:
        data.close()


def test_panel_serves_the_ui(config: Config) -> None:
    server, thread = _start_server(config)
    try:
        status, body = _request(server, "/", auth=True)
        assert status == 200
        assert b"SnipeKlein" in body
        assert b'data-view="statistics"' in body
        assert b'id="stats-grid"' in body
    finally:
        _stop_server(server, thread)


# -- helpers --------------------------------------------------------------
def _start_server(config: Config):
    from http.server import ThreadingHTTPServer

    from snipeklein.panel.server import PanelHandler

    data = PanelData(config)
    panel_config = PanelConfig(host="127.0.0.1", port=0, user=USER, password=PASSWORD)
    handler = type(
        "BoundPanelHandler", (PanelHandler,), {"data": data, "panel_config": panel_config}
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop_server(server, thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _request(
    server,
    path: str,
    auth: bool = True,
    password: str = PASSWORD,
    method: str = "GET",
) -> tuple[int, bytes]:
    host, port = server.server_address[0], server.server_address[1]
    request = urllib.request.Request(f"http://{host}:{port}{path}", method=method)
    if auth:
        token = base64.b64encode(f"{USER}:{password}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_prices_endpoint_returns_the_effective_table(config: Config) -> None:
    """The panel shows the table the bot values against, not only the DB rows."""
    data = PanelData(config)
    try:
        rows = data.prices()
        assert len(rows) > 50
        row = rows[0]
        for key in (
            "component_id",
            "label",
            "kind",
            "fmv_eur",
            "curated_eur",
            "source",
            "sample_n",
            "aliases",
        ):
            assert key in row
        assert {r["source"] for r in rows} <= {"market", "estimate"}
        assert any(r["source"] == "market" for r in rows)
        assert any(r["kind"] == "gpu" for r in rows)
    finally:
        data.close()


def test_price_detail_has_distribution_trend_and_samples(config: Config) -> None:
    from datetime import UTC, datetime, timedelta

    conn = connect(config.db_path)
    now = datetime.now(UTC)
    for day in range(3):
        for index in range(2):
            at = (now - timedelta(days=day)).replace(microsecond=0).isoformat()
            conn.execute(
                "INSERT INTO price_observations (component_id, price_eur, source, condition, "
                "observed_at) VALUES (?,?,?,?,?)",
                ("NVIDIA_RTX_4070_12GB", 500 + day * 10 + index, "kleinanzeigen", "a card", at),
            )
    conn.commit()
    conn.close()

    data = PanelData(config)
    try:
        detail = data.price_detail("NVIDIA_RTX_4070_12GB")
        assert detail is not None
        assert detail["distribution"]["count"] == 6
        assert detail["distribution"]["min"] == 500
        assert detail["distribution"]["max"] == 521
        assert detail["distribution"]["median"] is not None
        assert len(detail["trend"]) == 3
        assert detail["samples"][0]["title"] == "a card"
        assert data.price_detail("DOES_NOT_EXIST") is None
    finally:
        data.close()


def test_prices_endpoint_has_the_market_distribution(config: Config) -> None:
    """The price view exposes the market distribution of a configuration."""
    data = PanelData(config)
    try:
        rows = data.prices()
        assert rows
        for key in ("fmv_eur", "curated_eur", "source", "sample_n", "aliases"):
            assert key in rows[0]
    finally:
        data.close()


def _seed_statistics(config: Config) -> None:
    """Add one alerted deal, one near miss and the related tables."""
    today = datetime.now(UTC)
    posted_today = today.strftime("%d.%m.%Y")
    posted_old = (today - timedelta(days=20)).strftime("%d.%m.%Y")
    conn = connect(config.db_path)
    repo = DecisionRepo(conn)
    repo.record(
        Decision(
            ad_id="stats-deal",
            outcome="alerted",
            stage="alert",
            reason="alert sent: 32.0% margin, risk green",
            reason_code="alerted",
            query_tag="tag-a",
            title="Gaming PC RTX 4070",
            url="https://example.invalid/stats-deal",
            price=800.0,
            card_price=750.0,
            location="12345 Berlin",
            detail={
                "extraction_source": "llm",
                "extraction": {
                    "gpu_model": "NVIDIA GeForce RTX 4070",
                    "cpu_model": "AMD Ryzen 7 7800X3D",
                    "ram_capacity_gb": 32,
                },
                "valuation": {"margin_pct": 32.0, "band": "actionable"},
                "risk": {
                    "level": "green",
                    "score": 10,
                    "signals": [{"label": "shipping only", "points": 10}],
                },
            },
        )
    )
    repo.record(
        Decision(
            ad_id="stats-near",
            outcome="near_miss",
            stage="valuation",
            reason="near miss: 8.0% below market, not a deal",
            reason_code="near_miss",
            query_tag="tag-a",
            title="Gaming PC RTX 3070",
            url="https://example.invalid/stats-near",
            price=900.0,
            card_price=900.0,
            location="20095 Hamburg",
            detail={
                "extraction_source": "fallback",
                "extraction": {
                    "gpu_model": "NVIDIA GeForce RTX 3070",
                    "cpu_model": "AMD Ryzen 5 5600X",
                    "ram_capacity_gb": 16,
                },
                "valuation": {"margin_pct": 8.0, "band": "near_miss"},
                "risk": {
                    "level": "yellow",
                    "score": 20,
                    "signals": [{"label": "no rating badges", "points": 20}],
                },
            },
        )
    )
    conn.execute(
        """
        INSERT INTO extractions (
            ad_id, fmv_gross, fmv_net, margin_pct, band, risk_score, risk_level,
            coverage, components, defects, model, prompt_version, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "stats-deal",
            1100.0,
            1056.0,
            32.0,
            "actionable",
            10,
            "green",
            0.95,
            "[]",
            "[]",
            "test-model",
            "test",
            today.isoformat(),
        ),
    )
    conn.execute(
        """
        INSERT INTO extractions (
            ad_id, fmv_gross, fmv_net, margin_pct, band, risk_score, risk_level,
            coverage, components, defects, model, prompt_version, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "stats-near",
            1000.0,
            972.0,
            8.0,
            "near_miss",
            20,
            "yellow",
            0.8,
            "[]",
            "[]",
            "test-model",
            "test",
            today.isoformat(),
        ),
    )
    for ad_id, title, price, city, plz, shipping, seller, posted, badges in (
        (
            "stats-deal",
            "Gaming PC RTX 4070",
            800.0,
            "Berlin",
            "12345",
            "pickup",
            "Seller A",
            posted_today,
            '["TOP"]',
        ),
        (
            "stats-near",
            "Gaming PC RTX 3070",
            900.0,
            "Hamburg",
            "20095",
            "shipping",
            "Seller B",
            posted_old,
            "[]",
        ),
    ):
        conn.execute(
            """
            INSERT INTO listings (
                ad_id, url, title, description, price, is_negotiable, original_price,
                plz, city, region, category_id, posted_at, fetched_at, condition,
                condition_code, shipping_mode, seller_name, seller_since, seller_badges,
                seller_type, seller_active_ads, ad_type, raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ad_id,
                f"https://example.invalid/{ad_id}",
                title,
                "",
                price,
                0,
                None,
                plz,
                city,
                "Testland",
                228,
                posted,
                today.isoformat(),
                "Sehr Gut",
                "like_new",
                shipping,
                seller,
                "01.01.2020",
                badges,
                "Privater Nutzer",
                2,
                "",
                "{}",
            ),
        )
    conn.execute(
        """
        INSERT INTO alerts (
            ad_id, band, margin_pct, risk_level, payload_hash, webhook_key, status, sent_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        ("stats-deal", "actionable", 32.0, "green", "hash", "key", "sent", today.isoformat()),
    )
    conn.execute(
        """
        INSERT INTO runs (
            started_at, finished_at, queries, ads_seen, ads_new, ads_evaluated,
            alerts_sent, errors
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            today.isoformat(),
            (today + timedelta(seconds=30)).isoformat(),
            '["tag-a"]',
            4,
            2,
            1,
            1,
            1,
        ),
    )
    conn.execute(
        """
        INSERT INTO llm_usage (
            day, model, requests, in_tokens, out_tokens, errors, quota_429
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (today.date().isoformat(), "test-model", 3, 100, 20, 1, 0),
    )
    conn.close()


def test_statistics_contains_core_and_deep_dive_data(config: Config) -> None:
    _seed_statistics(config)
    data = PanelData(config)
    try:
        stats = data.statistics(days=0, use_cache=False)
    finally:
        data.close()

    assert stats["range"]["cohort"] == 3
    assert stats["cards"]["ads"] == 3
    assert stats["cards"]["valued"] == 2
    assert stats["cards"]["alerts"] == 1
    assert stats["cards"]["alert_rate_pct"] == 50.0
    assert stats["cards"]["fallback_share_pct"] == 50.0
    funnel = stats["funnel"]
    assert funnel[0]["count"] == 3
    assert all(funnel[i]["count"] >= funnel[i + 1]["count"] for i in range(len(funnel) - 1))
    assert stats["margin"]["count"] == 2
    assert sum(bin["count"] for bin in stats["margin"]["bins"]) == 2
    assert {level["key"] for level in stats["risk"]["levels"]} == {
        "green",
        "yellow",
        "red",
    }
    assert {signal["label"] for signal in stats["risk"]["signals"]} >= {
        "shipping only",
        "no rating badges",
    }
    assert stats["hardware"]["gpus"][0]["count"] >= 1
    assert stats["listing_mix"]["count"] == 2
    assert stats["alerts"]["count"] == 1
    assert stats["alerts"]["median_latency_s"] is not None
    assert stats["quality"]["with_valuation"] == 2
    assert stats["quality"]["coverage_bins"]
    assert stats["quality"]["price_freshness"]["changed"] == 1
    assert stats["age"]["bins"]
    assert stats["sellers"]["distinct"] == 2
    assert {row["city"] for row in stats["geography"]["cities"]} == {"Berlin", "Hamburg"}
    assert stats["queries"][0]["tag"] == "tag-a"
    assert stats["market"]["components"] > 0
    assert stats["ops"]["cycles"] == 1
    assert stats["ops"]["llm_requests"] == 3


def test_statistics_filters_by_query_tag(config: Config) -> None:
    _seed_statistics(config)
    data = PanelData(config)
    try:
        stats = data.statistics(days=7, query_tag="tag-a", use_cache=False)
    finally:
        data.close()

    assert stats["cards"]["ads"] == 2
    assert stats["cards"]["valued"] == 2
    assert stats["cards"]["alerts"] == 1
    assert stats["range"]["since"] is not None


def test_statistics_survives_malformed_detail_json(config: Config) -> None:
    _seed_statistics(config)
    conn = connect(config.db_path)
    conn.execute(
        "UPDATE ad_decisions SET detail = 'not-json' WHERE ad_id = 'stats-near'"
    )
    conn.close()

    data = PanelData(config)
    try:
        stats = data.statistics(days=0, use_cache=False)
    finally:
        data.close()

    assert stats["cards"]["ads"] == 3
    assert stats["quality"]["fallback_share_pct"] == 0.0
    assert stats["hardware"]["gpus"]


def test_statistics_http_endpoint(config: Config) -> None:
    _seed_statistics(config)
    server, thread = _start_server(config)
    try:
        status, body = _request(server, "/api/statistics?days=7&query_tag=tag-a", auth=True)
    finally:
        _stop_server(server, thread)

    assert status == 200
    payload = json.loads(body)
    assert payload["cards"]["ads"] == 2
    assert payload["queries"][0]["tag"] == "tag-a"
