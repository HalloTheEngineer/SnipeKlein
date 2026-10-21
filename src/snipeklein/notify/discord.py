"""Discord webhook dispatcher (spec 7)."""

from __future__ import annotations

import hashlib
import json
import random
import time

import httpx

from ..config import NotifyConfig
from ..logging_setup import get_logger
from ..models import Deal
from ..store.repo import AlertRepo
from .templates import build_payload

log = get_logger("snipeklein.notify.discord")


class DiscordError(RuntimeError):
    pass


class DiscordNotifier:
    def __init__(
        self,
        config: NotifyConfig,
        repo: AlertRepo | None = None,
        timeout_s: int = 20,
    ) -> None:
        self.config = config
        self.repo = repo
        self._client = httpx.Client(timeout=timeout_s)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DiscordNotifier:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def send(self, deal: Deal) -> bool:
        """Send an alert. Returns True when a webhook call was made."""
        webhook_url = self.config.webhook_url()
        if not webhook_url:
            raise DiscordError(
                f"webhook url missing - set {self.config.webhook_url_env} in .env"
            )

        payload = build_payload(deal, self.config.username)
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

        if self.repo is not None:
            existing = self.repo.get(deal.listing.ad_id)
            existing_match = existing and existing.get("payload_hash") == payload_hash
            if existing_match and not self.config.repeat_on_change:
                log.debug("alert for %s already sent, skipping", deal.listing.ad_id)
                return False

        self._post(webhook_url, payload, deal.listing.ad_id)

        if self.repo is not None:
            self.repo.record(
                ad_id=deal.listing.ad_id,
                band=deal.valuation.band.value,
                margin_pct=deal.valuation.margin_pct,
                risk_level=deal.risk.level.value,
                payload_hash=payload_hash,
                webhook_key=_webhook_key(webhook_url),
                status="sent",
            )
        return True

    def _post(self, url: str, payload: dict, label: str = "") -> None:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = self._client.post(url, json=payload)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("discord request failed (%s/3): %s", attempt, exc)
                time.sleep(2**attempt + random.uniform(0, 1))
                continue
            if response.status_code in (200, 204):
                log.info("alert sent for listing %s", label)
                return
            if response.status_code == 429:
                retry_after = response.headers.get("retry-after", "5")
                delay = float(retry_after) if retry_after.isdigit() else 5.0
                log.warning("discord rate limited, waiting %ss", delay)
                time.sleep(delay)
                last_error = DiscordError("429 rate limited")
                continue
            last_error = DiscordError(f"HTTP {response.status_code}: {response.text[:300]}")
            log.warning("discord http %s", response.status_code)
            time.sleep(2**attempt)
        raise last_error or DiscordError("could not deliver alert")


def _webhook_key(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return hashlib.sha256(tail.encode("utf-8")).hexdigest()[:12]
