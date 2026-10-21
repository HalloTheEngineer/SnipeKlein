"""Configuration loading: config.toml (non-secret) + .env (secrets)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

try:  # optional, only used to read .env
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is a hard dependency, but stay importable
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:  # type: ignore[misc]
        return False


from .paths import find_config, find_env_file, find_prices, resolve_relative

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


class ConfigError(RuntimeError):
    """Raised when the configuration is missing or invalid."""


@dataclass(frozen=True)
class QueryConfig:
    """One search query (spec 3.2)."""

    tag: str
    category: str
    category_id: int
    keywords: list[str]
    min_price: int = 0
    max_price: int = 0
    max_pages: int = 1

    @property
    def is_priced(self) -> bool:
        return self.max_price > 0


@dataclass(frozen=True)
class PanelConfigSettings:
    """Settings of the read-only web panel."""

    host: str = "127.0.0.1"
    port: int = 8137
    decision_retention_days: int = 14
    max_decision_rows: int = 100_000


@dataclass(frozen=True)
class WatchdogConfig:
    """Policy of the failure watchdog (spec: quiet by default)."""

    interval: int = 300
    grace: int = 600
    failures: int = 3
    cooldown: int = 21600


@dataclass(frozen=True)
class StorageConfig:
    observation_retention_days: int = 30
    listing_retention_days: int = 30


@dataclass(frozen=True)
class IngestionConfig:
    base_url: str = "https://www.kleinanzeigen.de"
    impersonate: str = "chrome131"
    user_agent: str = ""
    accept_language: str = "de-DE,de;q=0.9"
    request_timeout_s: int = 30
    max_retries: int = 3
    min_request_interval_s: float = 2.0
    poll_interval_min_s: int = 45
    poll_interval_max_s: int = 85
    proxy_url: str = ""


@dataclass(frozen=True)
class FilterConfig:
    required_ram_generation: str = "DDR5"
    triage_min_margin_pct: float = 15.0
    triage_min_components: int = 2
    # Let ads whose title hints at a current platform through to the extraction
    # even when the price table does not know the model yet.
    modern_platform_tier: bool = True


@dataclass(frozen=True)
class ValuationConfig:
    bundle_discount: float = 0.12
    min_coverage: float = 0.75
    actionable_margin_min_pct: float = 30.0
    actionable_margin_max_pct: float = 55.0
    extreme_margin_pct: float = 60.0
    # Listings between this margin and the actionable band are reported as a
    # "near miss" - clearly not a deal, but worth a look.
    near_miss_margin_min_pct: float = 15.0
    near_miss_alerts: bool = True
    asking_to_sold_factor: float = 0.92
    min_price_samples: int = 5


@dataclass(frozen=True)
class LlmConfig:
    provider: str = "gemini"
    model: str = "gemini-3.5-flash-lite"
    fallback_model: str = "gemini-3.1-flash-lite"
    base_url: str = ""
    api_key_env: str = "GEMINI_API_KEY"
    thinking_level: str = "minimal"
    max_rpm: int = 6
    max_attempts: int = 4
    timeout_s: int = 30
    enable_context_cache: bool = True

    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "") or os.environ.get("LLM_API_KEY", "")


@dataclass(frozen=True)
class NotifyConfig:
    webhook_url_env: str = "DISCORD_WEBHOOK_URL"
    username: str = "SnipeKlein"
    repeat_on_change: bool = False

    def webhook_url(self) -> str:
        return os.environ.get(self.webhook_url_env, "")


@dataclass(frozen=True)
class RiskConfig:
    account_new_days: int = 30
    account_very_new_days: int = 90
    max_active_ads: int = 10


@dataclass(frozen=True)
class Config:
    general: dict[str, Any] = field(default_factory=dict)
    ingestion: IngestionConfig = field(default_factory=IngestionConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    valuation: ValuationConfig = field(default_factory=ValuationConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)
    panel: PanelConfigSettings = field(default_factory=PanelConfigSettings)
    queries: list[QueryConfig] = field(default_factory=list)
    path: Path = DEFAULT_CONFIG_PATH

    @property
    def db_path(self) -> Path:
        raw = str(self.general.get("db_path", "data/snipeklein.db"))
        return resolve_relative(self.path, raw)

    @property
    def prices_path(self) -> Path:
        return find_prices(self.path)

    @property
    def log_level(self) -> str:
        return str(self.general.get("log_level", "INFO")).upper()

    @property
    def seen_ttl_days(self) -> int:
        return int(self.general.get("seen_ttl_days", 30))

    @property
    def observation_retention_days(self) -> int:
        return int(self.storage.observation_retention_days)

    @property
    def listing_retention_days(self) -> int:
        return int(self.storage.listing_retention_days)


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"config section [{name}] must be a table")
    return value


def _build(cls: type, raw: dict[str, Any], **extra: Any) -> Any:
    known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
    kwargs = {k: v for k, v in raw.items() if k in known}
    kwargs.update({k: v for k, v in extra.items() if k in known})
    return cls(**kwargs)


def load_config(
    path: str | Path | None = None,
    env_path: str | Path | None = None,
    load_env: bool = True,
) -> Config:
    """Load configuration from TOML plus environment variables."""
    cfg_path = find_config(path)
    if cfg_path is None:
        wanted = str(path) if path else "config.toml"
        raise ConfigError(f"config file not found: {wanted}")
    if load_env:
        env_file = Path(env_path) if env_path else find_env_file(cfg_path)
        if env_file is not None and env_file.exists():
            load_dotenv(dotenv_path=env_file)

    with cfg_path.open("rb") as fh:
        raw = tomllib.load(fh)

    queries: list[QueryConfig] = []
    for item in raw.get("queries", []):
        if not isinstance(item, dict):
            continue
        missing = [k for k in ("tag", "category", "category_id") if k not in item]
        if missing:
            raise ConfigError(f"query is missing required keys: {missing}")
        queries.append(
            QueryConfig(
                tag=str(item["tag"]),
                category=str(item["category"]),
                category_id=int(item["category_id"]),
                keywords=[str(k) for k in item.get("keywords", [])],
                min_price=int(item.get("min_price", 0)),
                max_price=int(item.get("max_price", 0)),
                max_pages=int(item.get("max_pages", 1)),
            )
        )

    return Config(
        general=_section(raw, "general"),
        ingestion=_ingestion(_section(raw, "ingestion")),
        filters=_build(FilterConfig, _section(raw, "filters")),
        valuation=_build(ValuationConfig, _section(raw, "valuation")),
        llm=_build(LlmConfig, _section(raw, "llm")),
        notify=_build(NotifyConfig, _section(raw, "notify")),
        risk=_build(RiskConfig, _section(raw, "risk")),
        storage=_build(StorageConfig, _section(raw, "storage")),
        watchdog=_build(WatchdogConfig, _section(raw, "watchdog")),
        panel=_build(PanelConfigSettings, _section(raw, "panel")),
        queries=queries,
        path=cfg_path,
    )


def _ingestion(raw: dict[str, Any]) -> IngestionConfig:
    """Build the ingestion config; the proxy may come from the environment."""
    config = _build(IngestionConfig, raw)
    if not config.proxy_url:
        proxy = os.environ.get("PROXY_URL", "").strip()
        if proxy:
            config = replace(config, proxy_url=proxy)
    return config
