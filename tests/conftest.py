"""Pytest configuration: register the `needs_keys` marker.

Tests that hit keyed-tier APIs should be decorated with
`@pytest.mark.needs_keys` so CI can skip them with `-m "not needs_keys"` while
local runs (with `.env` loaded) can include them with `-m needs_keys`.
"""
from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "needs_keys: test requires one or more API keys to be set in the environment",
    )
