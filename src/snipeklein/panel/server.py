"""A small read-only web panel for the decision trail.

* stdlib only (no new dependency)
* the database is opened read-only, the panel cannot write
* HTTP basic auth, credentials from the environment
* GET only - there is no endpoint that changes anything
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..config import Config
from ..logging_setup import get_logger
from .api import PanelData

log = get_logger("snipeklein.panel")

STATIC_DIR = Path(__file__).resolve().parent / "static"


class PanelConfig:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8137,
        user: str = "",
        password: str = "",
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password

    @property
    def auth_required(self) -> bool:
        return bool(self.user and self.password)


class PanelHandler(BaseHTTPRequestHandler):
    server_version = "SnipeKleinPanel/1.0"
    data: PanelData
    panel_config: PanelConfig

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _authenticated(self) -> bool:
        if not self.panel_config.auth_required:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        user, _, password = raw.partition(":")
        return hmac.compare_digest(user, self.panel_config.user) and hmac.compare_digest(
            password, self.panel_config.password
        )

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _unauthorized(self) -> None:
        body = b"authentication required"
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="SnipeKlein"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if not self._authenticated():
            self._unauthorized()
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            self._route(path, query)
        except Exception:
            log.exception("panel request failed: %s", self.path)
            self._json({"error": "internal error"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        self._json({"error": "the panel is read only"}, HTTPStatus.METHOD_NOT_ALLOWED)

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST

    def _route(self, path: str, query: dict[str, str]) -> None:
        if path == "/":
            self._send(
                HTTPStatus.OK,
                (STATIC_DIR / "index.html").read_bytes(),
                "text/html; charset=utf-8",
            )
            return
        if path == "/app.js":
            self._send(
                HTTPStatus.OK,
                (STATIC_DIR / "app.js").read_bytes(),
                "application/javascript; charset=utf-8",
            )
            return
        if path == "/api/summary":
            self._json(self.data.summary())
            return
        if path == "/api/ads":
            self._json(
                self.data.ads(
                    limit=_int(query.get("limit"), 50, 1, 500),
                    offset=_int(query.get("offset"), 0, 0, 100_000),
                    outcome=query.get("outcome", ""),
                    stage=query.get("stage", ""),
                    reason=query.get("reason", ""),
                    query_tag=query.get("query_tag", ""),
                    search=query.get("q", ""),
                    hours=_int(query.get("hours"), 0, 0, 24 * 365),
                    order=query.get("order", "updated"),
                )
            )
            return
        if path.startswith("/api/ads/"):
            ad_id = path[len("/api/ads/") :]
            payload = self.data.ad(ad_id)
            if payload is None:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            self._json(payload)
            return
        if path == "/api/alerts":
            self._json({"alerts": self.data.alerts(limit=_int(query.get("limit"), 50, 1, 500))})
            return
        if path == "/api/prices":
            self._json({"prices": self.data.prices()})
            return
        if path == "/api/runs":
            self._json({"runs": self.data.runs(limit=_int(query.get("limit"), 25, 1, 200))})
            return
        if path == "/api/health":
            self._json(self.data.health())
            return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def _int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def check(config: Config) -> int:
    """Verify that the panel can read the database (used as healthcheck)."""
    try:
        data = PanelData(config)
    except FileNotFoundError as exc:
        print(f"panel check failed: {exc}")
        return 1
    try:
        summary = data.summary()
    except sqlite3.Error as exc:
        print(f"panel check failed: {exc}")
        return 1
    finally:
        data.close()
    print(
        f"panel ok: {summary['decisions']['total']} decisions, "
        f"{summary['listings']} listings, {summary['alerts']} alerts"
    )
    return 0


def serve(config: Config, panel_config: PanelConfig) -> None:
    """Run the panel until interrupted."""
    if panel_config.host != "127.0.0.1" and not panel_config.auth_required:
        raise SystemExit(
            "refusing to bind to a public interface without PANEL_USER/PANEL_PASSWORD"
        )
    try:
        data = PanelData(config)
    except FileNotFoundError as exc:
        raise SystemExit(f"cannot open the database: {exc}") from exc
    data = PanelData(config)
    handler = type(
        "BoundPanelHandler",
        (PanelHandler,),
        {"data": data, "panel_config": panel_config},
    )
    server = ThreadingHTTPServer((panel_config.host, panel_config.port), handler)
    server.daemon_threads = True
    log.info(
        "panel listening on http://%s:%s (auth=%s)",
        panel_config.host,
        panel_config.port,
        "on" if panel_config.auth_required else "off",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        log.info("panel stopped")
    finally:
        server.server_close()
        data.close()


def panel_config_from_env(
    config: Config, host: str = "", port: int = 0
) -> PanelConfig:
    """Build the panel configuration from the environment and config.toml."""
    user = os.environ.get("PANEL_USER", "")
    password = os.environ.get("PANEL_PASSWORD", "")
    return PanelConfig(
        host=host or os.environ.get("PANEL_HOST", "") or config.panel.host,
        port=port or int(os.environ.get("PANEL_PORT", "") or 0) or config.panel.port,
        user=user,
        password=password,
    )


logging.getLogger("snipeklein.panel").addHandler(logging.NullHandler())
