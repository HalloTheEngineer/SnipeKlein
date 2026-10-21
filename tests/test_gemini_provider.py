"""Gemini provider: request shape, parsing and quota handling."""

from __future__ import annotations

import json

import httpx
import pytest

from snipeklein.config import LlmConfig
from snipeklein.extract.llm import (
    GeminiProvider,
    LlmError,
    ProviderFactory,
    QuotaExhausted,
)
from snipeklein.extract.schema import EXTRACTION_SCHEMA

PAYLOAD = {
    "is_complete_system": True,
    "cpu_model": "AMD Ryzen 7 7800X3D",
    "gpu_model": "NVIDIA GeForce RTX 4070",
    "ram_generation": "DDR5",
    "ram_capacity_gb": 32,
    "storage_capacity_gb": 1024,
    "motherboard_chipset": "B650",
    "psu_wattage": 750,
    "operating_system_included": True,
    "defects_or_missing_parts": [],
    "pickup_only": True,
}


def _response(text: str) -> dict:
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 120},
    }


def _config() -> LlmConfig:
    return LlmConfig(
        provider="gemini",
        model="gemini-3.5-flash-lite",
        api_key_env="TEST_GEMINI_KEY",
        thinking_level="minimal",
        max_attempts=2,
    )


def test_request_shape_and_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_GEMINI_KEY", "secret")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_response(json.dumps(PAYLOAD)))

    provider = GeminiProvider(_config(), transport=httpx.MockTransport(handler))
    reply = provider.complete("system", "user")
    assert reply.model == "gemini-3.5-flash-lite"
    assert reply.in_tokens == 900
    assert reply.out_tokens == 120
    assert json.loads(reply.text)["ram_generation"] == "DDR5"

    assert captured["url"].endswith("/models/gemini-3.5-flash-lite:generateContent")
    assert captured["headers"]["x-goog-api-key"] == "secret"
    generation = captured["body"]["generationConfig"]
    assert generation["responseMimeType"] == "application/json"
    assert generation["responseJsonSchema"] == EXTRACTION_SCHEMA
    assert generation["thinkingConfig"]["thinkingLevel"] == "minimal"
    assert captured["body"]["systemInstruction"]["parts"][0]["text"] == "system"


def test_strict_schema_rejection_falls_back_to_prompt_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_GEMINI_KEY", "secret")
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "responseJsonSchema" in body["generationConfig"]:
            return httpx.Response(
                400, json={"error": {"message": "Unsupported field: responseJsonSchema"}}
            )
        return httpx.Response(200, json=_response(json.dumps(PAYLOAD)))

    provider = GeminiProvider(_config(), transport=httpx.MockTransport(handler))
    reply = provider.complete("system", "user")
    assert reply.degraded is True
    assert len(bodies) == 2
    assert "responseSchema" in bodies[1]["generationConfig"]


def test_daily_quota_raises_quota_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_GEMINI_KEY", "secret")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429,
            json={
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                            "violations": [
                                {
                                    "quotaMetric": (
                                        "generativelanguage.googleapis.com/"
                                        "generate_content_free_tier_requests"
                                    ),
                                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                    "quotaValue": "250",
                                }
                            ],
                        },
                        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3s"},
                    ],
                }
            },
        )

    provider = GeminiProvider(_config(), transport=httpx.MockTransport(handler))
    with pytest.raises(QuotaExhausted):
        provider.complete("system", "user")
    assert calls["n"] == 1  # not retried


def test_per_minute_limit_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_GEMINI_KEY", "secret")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                json={
                    "error": {
                        "details": [
                            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "0s"}
                        ]
                    }
                },
            )
        return httpx.Response(200, json=_response(json.dumps(PAYLOAD)))

    provider = GeminiProvider(_config(), transport=httpx.MockTransport(handler))
    reply = provider.complete("system", "user")
    assert calls["n"] == 2
    assert reply.out_tokens == 120


def test_client_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_GEMINI_KEY", "secret")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    provider = GeminiProvider(_config(), transport=httpx.MockTransport(handler))
    with pytest.raises(LlmError):
        provider.complete("system", "user")
    assert calls["n"] == 1


def test_factory_fails_over_to_the_second_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_GEMINI_KEY", "secret")
    used: list[str] = []

    class _Failing:
        name = "gemini"
        model = "primary"

        def complete(self, system: str, user: str, schema=None):  # type: ignore[no-untyped-def]
            used.append("primary")
            raise QuotaExhausted("daily quota")

    class _Working:
        name = "gemini"
        model = "secondary"

        def complete(self, system: str, user: str, schema=None):  # type: ignore[no-untyped-def]
            used.append("secondary")
            from snipeklein.extract.llm import LlmReply

            return LlmReply(text="{}", model="secondary")

    factory = ProviderFactory(_config())
    factory._providers = [_Failing(), _Working()]  # noqa: SLF001
    reply = factory.complete("s", "u")
    assert reply.model == "secondary"
    assert used == ["primary", "secondary"]
