"""Async client for Mira digital showers (Platinum, Activate, Mode) over Bluetooth LE."""

from __future__ import annotations

from mira_ble.advertisement import Advertisement, parse_local_name
from mira_ble.bluez import remove_bluez_bond
from mira_ble.const import (
    LEGACY_SERVICE_UUID,
    NOTIFY_CHARACTERISTIC_UUID,
    SERVICE_UUID,
    WRITE_CHARACTERISTIC_UUID,
)
from mira_ble.exceptions import (
    ChecksumError,
    CommandRejected,
    ConnectionLost,
    MiraConnectionError,
    MiraError,
    NotConfirmed,
    NotSupported,
    PairingRequired,
    ProtocolError,
    RequestTimeout,
    UnsafeValue,
)
from mira_ble.frame import Frame, FrameAssembler
from mira_ble.protocol import (
    ABSOLUTE_MAX_FLOW,
    ABSOLUTE_MAX_TEMPERATURE,
    ABSOLUTE_MIN_TEMPERATURE,
    Identity,
    LastUsage,
    Opcode,
    Outlet,
    OutletConfig,
    OutletKind,
    Phase,
    Preset,
    State,
)
from mira_ble.transport import BleTransport, ReplyTest, Transport
from mira_ble.valve import DEFAULT_MAX_TEMPERATURE, NO_ANSWER, Capabilities, MiraValve

__version__ = "0.1.0"

__all__ = [
    "ABSOLUTE_MAX_FLOW",
    "ABSOLUTE_MAX_TEMPERATURE",
    "ABSOLUTE_MIN_TEMPERATURE",
    "DEFAULT_MAX_TEMPERATURE",
    "LEGACY_SERVICE_UUID",
    "NOTIFY_CHARACTERISTIC_UUID",
    "NO_ANSWER",
    "SERVICE_UUID",
    "WRITE_CHARACTERISTIC_UUID",
    "Advertisement",
    "BleTransport",
    "Capabilities",
    "ChecksumError",
    "CommandRejected",
    "ConnectionLost",
    "Frame",
    "FrameAssembler",
    "Identity",
    "LastUsage",
    "MiraConnectionError",
    "MiraError",
    "MiraValve",
    "NotConfirmed",
    "NotSupported",
    "Opcode",
    "Outlet",
    "OutletConfig",
    "OutletKind",
    "PairingRequired",
    "Phase",
    "Preset",
    "ProtocolError",
    "ReplyTest",
    "RequestTimeout",
    "State",
    "Transport",
    "UnsafeValue",
    "__version__",
    "parse_local_name",
    "remove_bluez_bond",
]
