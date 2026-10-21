"""Shared pytest fixtures."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "data" / "fixtures"


def read_fixture(name: str) -> str:
    """Read a gzipped fixture (or a plain file)."""
    gz = FIXTURE_DIR / f"{name}.gz"
    if gz.exists():
        with gzip.open(gz, "rt", encoding="utf-8") as fh:
            return fh.read()
    plain = FIXTURE_DIR / name
    if plain.exists():
        return plain.read_text(encoding="utf-8")
    raise FileNotFoundError(f"fixture not found: {name}")


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURE_DIR


@pytest.fixture()
def search_page() -> str:
    return read_fixture("search_pcs_gaming.html")


@pytest.fixture()
def gpu_search_page() -> str:
    return read_fixture("search_grafikkarten.html")


@pytest.fixture()
def detail_page() -> str:
    return read_fixture("detail_1_3064361110.html")


@pytest.fixture()
def detail_page_ddr4() -> str:
    return read_fixture("detail_2_3547114554.html")


@pytest.fixture()
def detail_page_gpu() -> str:
    return read_fixture("detail_0_3547079834.html")


@pytest.fixture()
def detail_page_old() -> str:
    return read_fixture("detail_0_3533437624.html")
