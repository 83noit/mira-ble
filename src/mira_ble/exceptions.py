"""Exceptions raised by mira-ble."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mira_ble.protocol import State


class MiraError(Exception):
    """Base class for every error raised by this library."""


class ProtocolError(MiraError):
    """A frame or payload did not look like the valve's protocol, or not like what was asked for.

    ``raw`` holds the offending bytes when they are available, so callers can log or store
    them for later analysis.
    """

    def __init__(self, message: str, raw: bytes | None = None) -> None:
        super().__init__(message)
        self.raw = raw


class ChecksumError(ProtocolError):
    """The frame's checksum did not match its contents."""


class CommandRejected(ProtocolError):
    """The valve answered a command with its failure status (``0x80``)."""

    def __init__(self, opcode: int, raw: bytes | None = None) -> None:
        super().__init__(f"the valve rejected opcode 0x{opcode:02X}", raw)
        self.opcode = opcode


class NotConfirmed(MiraError):
    """The valve acknowledged a command but its state did not change to what was asked.

    Raised by the read-back after a control command, so that a caller never reports water as
    stopped (or running) on the strength of an acknowledgement alone. ``state`` is the last
    state read, when there was one: it is what the valve reports, and the valve may still be
    acting on the command.
    """

    def __init__(self, message: str, state: State | None = None) -> None:
        super().__init__(message)
        self.state = state


class UnsafeValue(MiraError, ValueError):
    """A temperature or flow outside the limits this library will send to a valve."""


class NotSupported(MiraError):
    """The valve does not offer this, or this release of the library does not support it."""


class MiraConnectionError(MiraError):
    """Connecting to or talking to the valve failed at the Bluetooth level.

    ``error_code`` holds the adapter's numeric reason when it reported one.
    """

    def __init__(self, message: str, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class PairingRequired(MiraConnectionError):
    """The link to the valve could not be encrypted.

    Two cases, of very different strength:

    * ``refused`` is True: the valve turned pairing down outright (ESP-IDF SMP reason 82
      through an ESPHome proxy, BlueZ ``AuthenticationFailed`` on a local adapter). The
      adapter holds no bond the valve accepts and the valve is not in pairing mode.
    * ``refused`` is False: the link came up but the valve answered the subscribe with
      "insufficient authentication". That can be a lost bond, but also a proxy whose bond
      store is stuck, or a connection routed through an adapter that never held the bond.
      It is not, by itself, a reason to pair again.
    """

    def __init__(
        self, message: str, error_code: int | None = None, *, refused: bool = True
    ) -> None:
        super().__init__(message, error_code)
        self.refused = refused


class ConnectionLost(MiraConnectionError):
    """The valve dropped the connection while a request was in flight."""


class RequestTimeout(MiraConnectionError, TimeoutError):
    """No reply arrived within the request timeout."""
