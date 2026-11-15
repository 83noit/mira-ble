"""GATT identifiers of the valve's protocol service."""

from __future__ import annotations

from typing import Final

SERVICE_UUID: Final = "267f0001-eb15-43f5-94c3-67d2221188f7"
"""The protocol service of current valves; advertised, so usable as a discovery matcher."""
WRITE_CHARACTERISTIC_UUID: Final = "267f0002-eb15-43f5-94c3-67d2221188f7"
"""Commands are written here, without response."""
NOTIFY_CHARACTERISTIC_UUID: Final = "267f0003-eb15-43f5-94c3-67d2221188f7"
"""Replies arrive here as notifications."""

LEGACY_SERVICE_UUID: Final = "bccb0001-ca66-11e5-88a4-0002a5d5c51b"
"""The service of the older Mira Mode generation, which speaks an unrelated protocol."""
