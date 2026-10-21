"""The read-only panel: auth, API and the read-only database connection."""

from __future__ import annotations

import base64
import sqlite3
import threading
import urllib.error
import urllib.request
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


def test_panel_serves_the_ui(config: Config) -> None:
    server, thread = _start_server(config)
    try:
        status, body = _request(server, "/", auth=True)
        assert status == 200
        assert b"SnipeKlein" in body
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
