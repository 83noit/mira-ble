"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from mira_ble import valve as valve_module


@pytest.fixture(autouse=True)
def quick_confirmation() -> Iterator[None]:
    """Keep the confirmation loop's shape but not its real-time waits."""
    with (
        patch.object(valve_module, "CONFIRM_INTERVAL", 0.001),
        patch.object(valve_module, "CONFIRM_TIMEOUT", 0.05),
    ):
        yield
