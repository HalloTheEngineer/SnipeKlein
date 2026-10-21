"""Locating the configuration and data files.

The package is used in three ways: from a source checkout, from a virtualenv
install and from the Docker image - in all of them the configuration lives
next to the working directory, not inside the package.
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_ENV = "SNIPEKLEIN_CONFIG"
PRICES_ENV = "SNIPEKLEIN_PRICES"

_PACKAGE_ROOT = Path(__file__).resolve().parent
_PROJECT_ROOT = _PACKAGE_ROOT.parent.parent


def _candidates(name: str) -> list[Path]:
    cwd = Path.cwd()
    paths: list[Path] = []
    for parent in (cwd, *cwd.parents):
        paths.append(parent / name)
        if len(paths) > 6:
            break
    paths.append(_PROJECT_ROOT / name)
    return paths


def find_config(explicit: str | Path | None = None) -> Path | None:
    """Locate ``config.toml``."""
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.exists() else None
    env = os.environ.get(CONFIG_ENV)
    if env:
        path = Path(env).expanduser()
        if path.exists():
            return path
    for candidate in _candidates("config.toml"):
        if candidate.is_file():
            return candidate
    return None


def find_prices(config_path: Path | None = None) -> Path:
    """Locate the curated component table.

    Preference order:

    1. ``$SNIPEKLEIN_PRICES``
    2. ``<config dir>/prices.json`` - the packaged table, updated with every
       deploy (this is the one the Docker image ships)
    3. ``<config dir>/data/prices.json`` - the development checkout
    """
    env = os.environ.get(PRICES_ENV)
    if env:
        path = Path(env).expanduser()
        if path.exists():
            return path
    if config_path is not None:
        packaged = config_path.parent / "prices.json"
        if packaged.exists():
            return packaged
        candidate = config_path.parent / "data" / "prices.json"
        if candidate.exists():
            return candidate
    for candidate in _candidates("prices.json"):
        if candidate.exists():
            return candidate
    for candidate in _candidates("data/prices.json"):
        if candidate.exists():
            return candidate
    base = config_path.parent if config_path is not None else _PROJECT_ROOT
    return base / "data" / "prices.json"


def resolve_relative(config_path: Path | None, raw: str) -> Path:
    """Resolve a relative path against the directory of the config file."""
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    base = config_path.parent if config_path is not None else Path.cwd()
    return base / path


def find_env_file(config_path: Path | None = None) -> Path | None:
    """Locate the ``.env`` file."""
    if config_path is not None:
        candidate = config_path.parent / ".env"
        if candidate.exists():
            return candidate
    for candidate in _candidates(".env"):
        if candidate.exists():
            return candidate
    return None
