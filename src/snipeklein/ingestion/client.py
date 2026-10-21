"""HTTP client for Kleinanzeigen with TLS fingerprint emulation and polite pacing.

Spec 3.1 requires browser signature emulation and session persistence; the
session object is kept for the whole process lifetime so TCP connections and
cookies are reused across polling cycles.
"""

from __future__ import annotations

import contextlib
import random
import time
from dataclasses import dataclass

from curl_cffi import requests as cffi_requests

from ..config import IngestionConfig
from ..logging_setup import get_logger

log = get_logger("snipeklein.ingestion.client")


class FetchError(RuntimeError):
    """Raised when a page could not be retrieved."""

    def __init__(self, message: str, status: int = 0, challenge: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.challenge = challenge


class ChallengeError(FetchError):
    """The site answered with an anti-bot challenge (403/429)."""


@dataclass
class Response:
    url: str
    status: int
    text: str
    elapsed_s: float


class KleinanzeigenClient:
    """Session-persistent HTTP client with rate limiting and retries."""

    def __init__(self, config: IngestionConfig) -> None:
        self.config = config
        self._last_request = 0.0
        self._session: cffi_requests.Session = self._build_session()

    def _build_session(self) -> cffi_requests.Session:
        kwargs: dict[str, object] = {
            "impersonate": self.config.impersonate or None,
            "timeout": self.config.request_timeout_s,
        }
        if self.config.proxy_url:
            kwargs["proxy"] = self.config.proxy_url
        session: cffi_requests.Session = cffi_requests.Session(**kwargs)  # type: ignore[arg-type]
        session.headers.update(
            {
                "User-Agent": self.config.user_agent,
                "Accept-Language": self.config.accept_language,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        return session

    def close(self) -> None:
        with contextlib.suppress(Exception):  # pragma: no cover - best effort
            self._session.close()

    def __enter__(self) -> KleinanzeigenClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------
    def _wait_turn(self) -> None:
        interval = self.config.min_request_interval_s
        if interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_request = time.monotonic()

    def _is_challenge(self, status: int, body: str) -> bool:
        if status in (403, 429, 503):
            return True
        markers = ("captcha", "Zugriff verweigert", "blocked", "challenge", "schnellzugriff")
        lowered = body[:4000].lower()
        return any(marker.lower() in lowered for marker in markers)

    def get(self, url: str, params: dict[str, str] | None = None) -> Response:
        """Fetch a page, honouring the politeness delay and retry policy."""
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            self._wait_turn()
            started = time.monotonic()
            try:
                resp = self._session.get(url, params=params, allow_redirects=True)
            except Exception as exc:  # network-level failure
                last_error = exc
                log.warning("request failed (%s/%s): %s", attempt, self.config.max_retries, exc)
                self._backoff(attempt)
                continue

            body = resp.text or ""
            if resp.status_code == 200 and not self._is_challenge(resp.status_code, body):
                return Response(
                    url=str(resp.url),
                    status=resp.status_code,
                    text=body,
                    elapsed_s=time.monotonic() - started,
                )

            if self._is_challenge(resp.status_code, body):
                last_error = ChallengeError(
                    f"anti-bot challenge for {url} (status={resp.status_code})",
                    status=resp.status_code,
                    challenge=True,
                )
                log.warning(
                    "challenge detected for %s (status=%s), attempt %s/%s",
                    url,
                    resp.status_code,
                    attempt,
                    self.config.max_retries,
                )
                if resp.status_code == 429:
                    # Long cool-down: hammering makes it worse.
                    time.sleep(min(600, 30 * attempt))
                else:
                    self._backoff(attempt)
                continue

            last_error = FetchError(f"HTTP {resp.status_code} for {url}", status=resp.status_code)
            log.warning("http %s for %s (attempt %s)", resp.status_code, url, attempt)
            self._backoff(attempt)

        raise last_error or FetchError(f"could not fetch {url}")

    def _backoff(self, attempt: int) -> None:
        delay = min(30.0, (2 ** (attempt - 1)) * 2.0) + random.uniform(0, 1.5)
        time.sleep(delay)
