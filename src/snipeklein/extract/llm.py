"""LLM access layer.

Providers:

* ``GeminiProvider``      - native Gemini ``generateContent`` (default)
* ``OpenAICompatProvider`` - any OpenAI compatible endpoint (Groq, Ollama,
  vLLM, LM Studio, ...)
* ``FallbackProvider``     - deterministic regex extraction, used when no LLM
  is configured or the quota is exhausted, so the bot keeps running.

The clients are rate limited locally, honour the ``RetryInfo`` delay returned
by Gemini and fail over to a second model when the daily quota of the primary
model is used up.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ..config import LlmConfig
from ..logging_setup import get_logger
from .schema import EXTRACTION_SCHEMA

log = get_logger("snipeklein.extract.llm")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


class LlmError(RuntimeError):
    """Generic LLM failure."""


class QuotaExhausted(LlmError):
    """The daily quota of a model is used up - retrying will not help."""


class RateLimited(LlmError):
    """Per-minute limit hit - retry later."""


@dataclass
class LlmReply:
    text: str
    model: str
    in_tokens: int = 0
    out_tokens: int = 0
    degraded: bool = False


@dataclass
class TokenBucket:
    """Simple interval based rate limiter."""

    max_per_minute: int = 6
    _calls: list[float] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(self) -> None:
        if self.max_per_minute <= 0:
            return
        interval = 60.0 / float(self.max_per_minute)
        with self._lock:
            now = time.monotonic()
            self._calls = [t for t in self._calls if now - t < 60.0]
            if len(self._calls) >= self.max_per_minute:
                wait = 60.0 - (now - self._calls[0]) + 0.05
            else:
                wait = max(0.0, interval - (now - self._calls[-1])) if self._calls else 0.0
            self._calls.append(now + wait)
        if wait > 0:
            time.sleep(wait)


class LlmProvider(Protocol):
    name: str
    model: str

    def complete(
        self, system: str, user: str, schema: dict[str, Any] | None = None
    ) -> LlmReply: ...


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"```$", "", cleaned.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


class GeminiProvider:
    """Native Gemini API client (google generative language API)."""

    name = "gemini"

    def __init__(
        self,
        config: LlmConfig,
        model: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self.model = model or config.model
        self._client = httpx.Client(
            timeout=config.timeout_s,
            headers={"Content-Type": "application/json"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    # -- public API --------------------------------------------------------
    def complete(
        self, system: str, user: str, schema: dict[str, Any] | None = None
    ) -> LlmReply:
        api_key = self.config.api_key()
        if not api_key:
            raise LlmError("no API key configured for the Gemini provider")
        schema = schema or EXTRACTION_SCHEMA
        url = f"{GEMINI_BASE}/models/{self.model}:generateContent"
        body = self._build_body(system, user, schema, strict=True)
        try:
            data = self._post(url, api_key, body)
        except LlmError as exc:
            if "unsupported" not in str(exc).lower() and "invalid" not in str(exc).lower():
                raise
            log.warning("strict structured output rejected, falling back to prompt mode")
            body = self._build_body(system, user, schema, strict=False)
            data = self._post(url, api_key, body)
            return self._to_reply(data, degraded=True)
        return self._to_reply(data)

    # -- internals ---------------------------------------------------------
    def _build_body(
        self, system: str, user: str, schema: dict[str, Any], strict: bool
    ) -> dict[str, Any]:
        generation_config: dict[str, Any] = {"responseMimeType": "application/json"}
        if strict:
            generation_config["responseJsonSchema"] = schema
        else:
            generation_config["responseSchema"] = schema
        if self.config.thinking_level:
            generation_config["thinkingConfig"] = {"thinkingLevel": self.config.thinking_level}
        return {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": generation_config,
        }

    def _post(self, url: str, api_key: str, body: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                response = self._client.post(url, json=body, headers={"x-goog-api-key": api_key})
            except httpx.HTTPError as exc:
                last_error = LlmError(f"network error: {exc}")
                self._sleep_backoff(attempt)
                continue

            if response.status_code == 200:
                return response.json()

            detail = response.text[:800]
            if response.status_code == 429:
                retry_delay, daily = _parse_quota_error(response)
                if daily:
                    raise QuotaExhausted(
                        f"daily quota exhausted for {self.model} (attempt {attempt})"
                    )
                log.warning(
                    "429 rate limited for %s (attempt %s), waiting %ss",
                    self.model,
                    attempt,
                    retry_delay,
                )
                time.sleep(retry_delay)
                last_error = RateLimited(f"429 for {self.model}")
                continue

            if response.status_code in (400, 403, 404):
                raise LlmError(f"HTTP {response.status_code}: {detail}")

            last_error = LlmError(f"HTTP {response.status_code}: {detail}")
            log.warning("gemini http %s (attempt %s)", response.status_code, attempt)
            self._sleep_backoff(attempt)

        raise last_error or LlmError("gemini request failed")

    def _sleep_backoff(self, attempt: int) -> None:
        delay = min(30.0, (2 ** (attempt - 1)) * 2.0) + random.uniform(0, 1.0)
        time.sleep(delay)

    def _to_reply(self, data: dict[str, Any], degraded: bool = False) -> LlmReply:
        candidates = data.get("candidates") or []
        if not candidates:
            raise LlmError(f"empty response: {str(data)[:200]}")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        if not text.strip():
            raise LlmError("response contained no text")
        usage = data.get("usageMetadata") or {}
        return LlmReply(
            text=text,
            model=self.model,
            in_tokens=int(usage.get("promptTokenCount", 0) or 0),
            out_tokens=int(usage.get("candidatesTokenCount", 0) or 0)
            + int(usage.get("thoughtsTokenCount", 0) or 0),
            degraded=degraded,
        )


class OpenAICompatProvider:
    """Any OpenAI compatible ``/chat/completions`` endpoint."""

    name = "openai"

    def __init__(self, config: LlmConfig, model: str | None = None) -> None:
        self.config = config
        self.model = model or config.model
        self._client = httpx.Client(timeout=config.timeout_s)

    def close(self) -> None:
        self._client.close()

    def complete(
        self, system: str, user: str, schema: dict[str, Any] | None = None
    ) -> LlmReply:
        api_key = self.config.api_key()
        if not api_key:
            raise LlmError("no API key configured")
        schema = schema or EXTRACTION_SCHEMA
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "listing_extraction", "schema": schema, "strict": True},
            },
        }
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                response = self._client.post(
                    url, json=body, headers={"Authorization": f"Bearer {api_key}"}
                )
            except httpx.HTTPError as exc:
                last_error = LlmError(f"network error: {exc}")
                time.sleep(min(30.0, 2**attempt) + random.uniform(0, 1.0))
                continue
            if response.status_code == 200:
                data = response.json()
                choice = (data.get("choices") or [{}])[0]
                text = (choice.get("message") or {}).get("content") or ""
                usage = data.get("usage") or {}
                return LlmReply(
                    text=text,
                    model=self.model,
                    in_tokens=int(usage.get("prompt_tokens", 0) or 0),
                    out_tokens=int(usage.get("completion_tokens", 0) or 0),
                )
            if response.status_code == 429:
                retry_after = response.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 5.0
                time.sleep(delay)
                last_error = RateLimited("429")
                continue
            if response.status_code in (400, 401, 403, 404):
                raise LlmError(f"HTTP {response.status_code}: {response.text[:400]}")
            last_error = LlmError(f"HTTP {response.status_code}")
            time.sleep(min(30.0, 2**attempt))
        raise last_error or LlmError("request failed")


def _parse_quota_error(response: httpx.Response) -> tuple[float, bool]:
    """Return (retry delay seconds, is daily quota)."""
    delay = 5.0
    daily = False
    try:
        payload = response.json()
    except ValueError:
        return delay, daily
    details = (payload.get("error") or {}).get("details") or []
    for detail in details:
        kind = str(detail.get("@type", ""))
        if kind.endswith("RetryInfo"):
            raw = str(detail.get("retryDelay", ""))
            match = re.match(r"([\d.]+)s", raw)
            if match:
                delay = float(match.group(1))
        if kind.endswith("QuotaFailure"):
            for violation in detail.get("violations") or []:
                metric = str(violation.get("quotaMetric", ""))
                quota_id = str(violation.get("quotaId", ""))
                if "perday" in metric.lower().replace("_", "") or "perday" in quota_id.lower().replace(
                    "_", ""
                ):
                    daily = True
    if daily:
        delay = max(delay, 60.0)
    return delay, daily


class ProviderFactory:
    """Creates providers and tracks usage/quota state."""

    def __init__(self, config: LlmConfig) -> None:
        self.config = config
        self.bucket = TokenBucket(max_per_minute=config.max_rpm)
        self._providers: list[LlmProvider] = []
        self._index = 0
        self.quota_exhausted = False
        self.on_usage: Callable[[str, int, int, int, int], None] | None = None

    def build(self) -> LlmProvider | None:
        provider = self._create(self.config.model)
        if provider is None:
            return None
        self._providers = [provider]
        if self.config.fallback_model and self.config.fallback_model != self.config.model:
            fallback = self._create(self.config.fallback_model)
            if fallback is not None:
                self._providers.append(fallback)
        self._index = 0
        return self._providers[0] if self._providers else None

    def _create(self, model: str) -> LlmProvider | None:
        provider = (self.config.provider or "gemini").lower()
        if provider in ("gemini", "google"):
            if not self.config.api_key():
                log.warning("no API key (%s) - the LLM stage is disabled", self.config.api_key_env)
                return None
            return GeminiProvider(self.config, model)
        if provider in ("openai", "openai-compat", "ollama", "groq", "vllm"):
            if not self.config.api_key():
                log.warning("no API key - the LLM stage is disabled")
                return None
            return OpenAICompatProvider(self.config, model)
        log.warning("unknown provider %r", provider)
        return None

    def complete(self, system: str, user: str, schema: dict[str, Any] | None = None) -> LlmReply:
        if not self._providers:
            raise LlmError("no provider available")
        last_error: Exception | None = None
        rate_limited = 0
        while self._index < len(self._providers):
            provider = self._providers[self._index]
            self.bucket.acquire()
            try:
                reply = provider.complete(system, user, schema)
            except QuotaExhausted as exc:
                log.warning("quota exhausted on %s: %s", provider.model, exc)
                self._record(provider.model, 0, 0, 0, 1)
                if self._index + 1 < len(self._providers):
                    self._index += 1
                    log.info("failing over to model %s", self._providers[self._index].model)
                    continue
                self.quota_exhausted = True
                raise
            except RateLimited as exc:
                # a per-minute limit clears on its own - retry a few times, then
                # give up instead of looping forever
                rate_limited += 1
                log.warning(
                    "rate limited on %s (attempt %s): %s", provider.model, rate_limited, exc
                )
                last_error = exc
                if rate_limited >= self.config.max_attempts:
                    break
                continue
            except LlmError as exc:
                log.warning("llm error on %s: %s", provider.model, exc)
                last_error = exc
                if self._index + 1 < len(self._providers):
                    self._index += 1
                    continue
                raise
            self._record(provider.model, 1, reply.in_tokens, reply.out_tokens, 0)
            return reply
        raise last_error or LlmError("no provider answered")

    def _record(self, model: str, requests: int, in_tokens: int, out_tokens: int, errors: int) -> None:
        if self.on_usage is not None:
            try:
                self.on_usage(model, requests, in_tokens, out_tokens, errors)
            except Exception:  # pragma: no cover - never break the pipeline
                log.exception("could not record llm usage")


def parse_json_reply(reply: LlmReply) -> dict[str, Any]:
    """Parse the model output into a JSON object."""
    return _extract_json(reply.text)
