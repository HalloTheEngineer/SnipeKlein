"""Extraction: schema, LLM plumbing and the deterministic fallback."""

from __future__ import annotations

import json

import pytest

from snipeklein.config import LlmConfig
from snipeklein.extract.extractor import Extractor, regex_extract
from snipeklein.extract.llm import LlmError, ProviderFactory, QuotaExhausted, parse_json_reply
from snipeklein.extract.schema import EXTRACTION_SCHEMA


def test_schema_is_gemini_compatible() -> None:
    """Gemini rejects anyOf/oneOf - only type arrays are allowed."""
    serialized = json.dumps(EXTRACTION_SCHEMA)
    assert "anyOf" not in serialized
    assert "oneOf" not in serialized
    assert '"type": ["integer", "null"]' in serialized or '"type":["integer","null"]' in serialized
    for prop in EXTRACTION_SCHEMA["properties"].values():
        assert prop["type"] in ("string", "boolean", "integer", "array", "object") or isinstance(
            prop["type"], list
        )


def test_regex_fallback_extracts_components() -> None:
    text = (
        "Gaming PC mit AMD Ryzen 7 7800X3D, NVIDIA GeForce RTX 4070, "
        "32 GB DDR5 RAM, 1 TB NVMe SSD, B650 Mainboard, 750W Netzteil, Windows 11, "
        "Nur Abholung"
    )
    extraction = regex_extract(text)
    assert extraction.is_complete_system
    assert extraction.cpu_model and "7800X3D" in extraction.cpu_model
    assert extraction.gpu_model and "4070" in extraction.gpu_model
    assert extraction.ram_generation == "DDR5"
    assert extraction.ram_capacity_gb == 32
    assert extraction.storage_capacity_gb == 1024
    assert extraction.psu_wattage == 750
    assert extraction.operating_system_included
    assert extraction.pickup_only


def test_regex_fallback_detects_ddr4() -> None:
    extraction = regex_extract("Gaming PC Ryzen 7 5700X RTX 3060 16 GB DDR4 256 GB SSD")
    assert extraction.ram_generation == "DDR4"


def test_extractor_falls_back_without_api_key() -> None:
    config = LlmConfig(provider="gemini", api_key_env="MISSING_KEY_FOR_TESTS")
    extractor = Extractor(config)
    outcome = extractor.extract(
        "Gaming PC Ryzen 7 7800X3D RTX 4070 32GB DDR5 1TB NVMe",
        "Nur Abholung, Windows 11 dabei",
    )
    assert outcome.source == "fallback"
    assert outcome.extraction.ram_generation == "DDR5"
    assert not outcome.rejected


def test_extractor_uses_the_llm_when_a_key_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the provider factory must be built, not only created."""
    monkeypatch.setenv("TEST_GEMINI_KEY", "secret")
    config = LlmConfig(provider="gemini", model="gemini-3.5-flash-lite",
                       api_key_env="TEST_GEMINI_KEY")
    extractor = Extractor(config)
    assert extractor.factory._providers  # noqa: SLF001
    assert extractor.factory._providers[0].model == "gemini-3.5-flash-lite"  # noqa: SLF001


def test_extractor_rejects_non_ddr5() -> None:
    config = LlmConfig(provider="gemini", api_key_env="MISSING_KEY_FOR_TESTS")
    extractor = Extractor(config)
    outcome = extractor.extract("Gaming PC Ryzen 7 5700X RTX 3060 16 GB DDR4")
    assert outcome.rejected.startswith("ram_generation")


def test_parse_json_reply_handles_code_fences() -> None:
    from snipeklein.extract.llm import LlmReply

    reply = LlmReply(text='```json\n{"a": 1}\n```', model="m")
    assert parse_json_reply(reply) == {"a": 1}


def test_provider_factory_without_key_returns_none() -> None:
    factory = ProviderFactory(LlmConfig(api_key_env="MISSING_KEY_FOR_TESTS"))
    assert factory.build() is None


class _FakeProvider:
    name = "fake"
    model = "fake-model"

    def complete(self, system: str, user: str, schema=None):  # type: ignore[no-untyped-def]
        raise QuotaExhausted("daily quota exhausted")


def test_quota_exhausted_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = ProviderFactory(LlmConfig())
    provider = _FakeProvider()
    factory._providers = [provider]  # noqa: SLF001
    with pytest.raises(QuotaExhausted):
        factory.complete("s", "u")
    assert factory.quota_exhausted


def test_llm_error_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        name = "boom"
        model = "boom"

        def complete(self, system: str, user: str, schema=None):  # type: ignore[no-untyped-def]
            raise LlmError("boom")

    factory = ProviderFactory(LlmConfig())
    factory._providers = [_Boom()]  # noqa: SLF001
    with pytest.raises(LlmError):
        factory.complete("s", "u")
