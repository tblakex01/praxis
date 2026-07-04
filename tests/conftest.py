"""Shared test helpers: locate and load the JSON trace fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Load ``tests/fixtures/<name>`` (a JSON file) as a dict."""
    with (FIXTURES_DIR / name).open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def fixture_trace() -> Callable[[str], list[dict[str, str]]]:
    """Return a loader mapping a fixture file name to its ``trace`` list."""

    def _load(name: str) -> list[dict[str, str]]:
        return load_fixture(name)["trace"]

    return _load
