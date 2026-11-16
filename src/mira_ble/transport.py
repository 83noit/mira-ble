"""Bluetooth LE transport for Mira valves, on top of ble-bond.

ble-bond's :class:`~ble_bond.BondedLink` does the Bluetooth part: pairing on every connection
(on an ESPHome proxy, a re-encrypt with the stored bond), recording which adapter or proxy each
attempt used, the BlueZ pairing agent, one request at a time, and closing the link whenever a
request does not complete. This module adds the valve's protocol: frames are reassembled by
:class:`~mira_ble.frame.FrameAssembler`, a reply is taken only if it has the shape the request
expects, a rejection raises :class:`~mira_ble.exceptions.CommandRejected`, and ble-bond's
exceptions become this library's.

Pairing on every connection is what makes the same code work on a local adapter and on an
ESPHome Bluetooth proxy. A local BlueZ adapter applies a stored bond by itself. A proxy does
not: each new connection starts unencrypted, the valve answers the subscribe with GATT
"insufficient authentication", and only an explicit pair call re-arms encryption from the
stored key. With a bond present that call is a quick re-encrypt; without one it is real SMP
pairing, which the valve accepts only while it is in pairing mode.

Replies carry no request id and no opcode of their own. Three rules follow:

* one request is in flight at a time;
* a frame that is not a reply, or not of the shape the pending request expects, is logged as
  unsolicited and the wait goes on, so a stray frame cannot shift every later reply by one;
* a request that does not complete (timeout, cancellation, write error) closes the link, so
  its late reply cannot be taken for the next request's answer.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, Protocol, Self

import ble_bond
from ble_bond import BondedLink, LinkPolicy, SubscribeRetry
from bleak.exc import BleakCharacteristicNotFoundError

from mira_ble.const import NOTIFY_CHARACTERISTIC_UUID, WRITE_CHARACTERISTIC_UUID
from mira_ble.exceptions import (
    CommandRejected,
    ConnectionLost,
    MiraConnectionError,
    PairingRequired,
    RequestTimeout,
)
from mira_ble.frame import Frame, FrameAssembler

if TYPE_CHECKING:
    from types import TracebackType

    from bleak.backends.device import BLEDevice

type ReplyTest = Callable[[bytes], bool]
"""Says whether a reply payload has the shape a request expects."""

DEFAULT_REQUEST_TIMEOUT: Final = 10.0
"""Seconds to wait for a whole reply, write included.

Measured through an ESPHome proxy on a Mira Platinum: 0.1 to 0.3 s on a new link, 7 to 10 s
with frequent losses once the link has been up for a few seconds (see
:attr:`BleTransport.link_age`). Mira-Activate-HA reports around 6 s on a held link.
"""
DEFAULT_CONNECT_TIMEOUT: Final = 15.0
"""Seconds allowed for one connect-and-pair attempt."""
SETTLE_AFTER_PAIR: Final = 0.5
"""Seconds between pairing and subscribing.

A pair call can return before the link-layer encryption is up; a subscribe landing in that
window is answered with "insufficient authentication", which is indistinguishable from a
missing bond. The vendor app waits 500 ms at the same point.
"""
SUBSCRIBE_RETRY_DELAY: Final = 1.0
"""Seconds before the one retry of a subscribe refused for want of encryption."""
SETTLE_AFTER_SUBSCRIBE: Final = 0.25
"""Seconds between subscribing and the first write.

On a local BlueZ link Home Assistant re-tunes the connection interval right after the
connection completes; a first request written into that update went unanswered on another
device (ha-grundfos-ble#1).
"""
CONNECTION_PARAMS: Final = (24, 60, 0, 2000)
"""Connection interval 30-75 ms, no latency, 20 s supervision timeout.

Requested best-effort where the backend can (Home Assistant's client can, through a proxy).
Mira-Activate-HA found the valve slow and prone to drop on default parameters, and a 15 ms
interval too heavy for a shared proxy radio.
"""

POLICY: Final = LinkPolicy(
    settle_after_pair=SETTLE_AFTER_PAIR,
    settle_after_subscribe=SETTLE_AFTER_SUBSCRIBE,
    # Only a subscribe refused for want of encryption is worth a second try on the same link.
    subscribe_retry=SubscribeRetry.INSUFFICIENT_AUTHENTICATION,
    subscribe_retry_delay=SUBSCRIBE_RETRY_DELAY,
    # Refused twice: the link is up but not encrypted. Weak evidence about the bond, reported
    # as PairingRequired(refused=False); it is also what a proxy with a stuck bond store does.
    insufficient_authentication_is_evidence=True,
    # An adapter or proxy that cannot pair at all may still hold a bond that encrypts the link
    # by itself (CoreBluetooth does), so it gets one more try without pairing.
    retry_unpaired=True,
    # Writes are sized from the negotiated MTU. Only a preset write (67 bytes) is longer than
    # the default; whether a valve joins a command split over several writes is not measured,
    # and the vendor app and the other projects always write a frame whole.
    write_chunk=None,
    connection_params=CONNECTION_PARAMS,
    connect_timeout=DEFAULT_CONNECT_TIMEOUT,
    request_timeout=DEFAULT_REQUEST_TIMEOUT,
)
"""How a Mira valve's link is handled (see the constants above for where each value comes
from)."""


class Transport(Protocol):
    """What :class:`~mira_ble.valve.MiraValve` needs from a transport."""

    last_frames: deque[tuple[str, bytes]]
    """Recent traffic as ("tx" | "rx", frame), for diagnostics and fixture capture."""

    @property
    def is_connected(self) -> bool:
        """True while a link is up."""
        ...

    async def connect(self) -> None:
        """Connect, encrypt and subscribe."""
        ...

    async def disconnect(self) -> None:
        """Drop the link."""
        ...

    async def request(
        self, frame: Frame, *, accept: ReplyTest | None = None, timeout: float | None = None
    ) -> Frame:
        """Send a request and return the first reply that ``accept`` recognises."""
        ...


class BleTransport:
    """The valve's protocol over one BLE connection.

    ``device_callback``, when given, is asked for the device again before every connection
    attempt, so a retry goes through whichever adapter or proxy now hears the valve best. It
    may return None to keep the last device.

    ``expect_new_bond`` says a new bond may be made on this connection (a pairing flow, the
    command line). Only then is a BlueZ pairing agent registered on a local adapter; ordinary
    polls never take over pairing for the system.

    ``disconnected_callback`` is called when the valve or the adapter drops the live link, not
    when this transport disconnects on purpose.

    ``async with BleTransport(device) as transport:`` connects on entry and disconnects on exit.
    """

    def __init__(
        self,
        device: BLEDevice,
        *,
        name: str | None = None,
        max_attempts: int = 2,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        disconnected_callback: Callable[[], None] | None = None,
        device_callback: Callable[[], BLEDevice | None] | None = None,
        expect_new_bond: bool = False,
    ) -> None:
        self._link = BondedLink(
            device,
            notify_uuid=NOTIFY_CHARACTERISTIC_UUID,
            write_uuid=WRITE_CHARACTERISTIC_UUID,
            assembler=FrameAssembler(),
            policy=POLICY,
            name=name,
            max_attempts=max_attempts,
            connect_timeout=connect_timeout,
            request_timeout=request_timeout,
            disconnected_callback=disconnected_callback,
            device_callback=device_callback,
            expect_new_bond=expect_new_bond,
        )
        self._name = name or device.name or device.address
        self._connected_at = 0.0
        self.last_frames = self._link.last_frames
        """Recent traffic as ("tx" | "rx", frame) for diagnostics and fixture capture."""

    async def __aenter__(self) -> Self:
        """Connect, as :meth:`connect` does, and return the transport."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Disconnect, whatever happened in the block."""
        await self.disconnect()

    @property
    def is_connected(self) -> bool:
        """True while the BLE link is up."""
        return self._link.is_connected

    @property
    def unsolicited(self) -> int:
        """Frames received that no request was waiting for, or that fitted none."""
        return self._link.unsolicited

    @property
    def link_age(self) -> float | None:
        """Seconds since the present link came up, or None when there is none.

        Measured through an ESPHome proxy on a Mira Platinum: requests on a new link take
        0.1 to 0.3 s, and once the link has been up for a few seconds they take 7 to 10 s
        and often time out (the valve appears to relax the connection once it is idle, as
        another project saw on an Activate). A caller that needs a prompt answer on a proxy
        should use a new connection rather than one that has been held.
        """
        if not self.is_connected:
            return None
        return asyncio.get_running_loop().time() - self._connected_at

    @property
    def source(self) -> str | None:
        """The adapter or proxy the last connection attempt went through, when known.

        The bond lives on one adapter, so a refusal only says something about the bond when
        it came through that adapter. Under Home Assistant this is the scanner chosen for
        the attempt, refused attempts included; elsewhere it is where the device was last
        heard when the attempt started, and None when that is not known either.
        """
        return self._link.last_source

    async def connect(self) -> None:
        """Connect through the device's path, pair (encrypt) and subscribe to replies.

        Retries are run one connection attempt at a time, so that a refusal on any attempt
        is seen; bleak-retry-connector reports only the last attempt's error.
        """
        if self.is_connected:
            return
        with _translated(self._name):
            await self._link.connect()
        self._connected_at = asyncio.get_running_loop().time()

    async def disconnect(self) -> None:
        """Unsubscribe and drop the link; safe to call when not connected."""
        await self._link.disconnect()

    async def request(
        self, frame: Frame, *, accept: ReplyTest | None = None, timeout: float | None = None
    ) -> Frame:
        """Write ``frame`` and return the valve's reply, one request at a time.

        ``accept`` says what the reply's payload must look like; frames that do not fit are
        treated as unsolicited and the wait goes on. A rejection (one byte with the failure
        bit) always answers the request and raises
        :class:`~mira_ble.exceptions.CommandRejected`. A request that does not complete, for
        whatever reason, closes the link.
        """

        def answers(raw: bytes) -> bool:
            reply = Frame.decode(raw)
            if not reply.is_valid_reply:
                return False
            return reply.is_rejection or accept is None or accept(reply.payload)

        with _translated(self._name):
            raw = await self._link.request(frame.encode(), accept=answers, timeout=timeout)
        reply = Frame.decode(raw)
        if reply.is_rejection:
            raise CommandRejected(frame.opcode, raw)
        return reply


class _translated:  # noqa: N801 - used as a context manager, reads like one
    """Turn ble-bond's exceptions into this library's, keeping the cause."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if not isinstance(exc, ble_bond.LinkError):
            return
        mapped: MiraConnectionError
        if isinstance(exc, ble_bond.PairingRequired):
            mapped = PairingRequired(str(exc), exc.error_code, refused=exc.refused)
        elif isinstance(exc, ble_bond.RequestTimeout):
            mapped = RequestTimeout(str(exc), exc.error_code)
        elif isinstance(exc, ble_bond.ConnectionLost):
            mapped = ConnectionLost(str(exc), exc.error_code)
        elif isinstance(exc.__cause__, BleakCharacteristicNotFoundError):
            # The service cache is stale (ble-bond has cleared it), or this is not a valve
            # that speaks the protocol.
            mapped = MiraConnectionError(
                f"{self._name}: the protocol characteristic was not found; the device does "
                f"not offer the 267f service, or a cached service list is stale "
                f"({exc.__cause__})"
            )
        else:
            # Includes a proxy that cannot pair and could not connect unpaired either.
            mapped = MiraConnectionError(str(exc), exc.error_code)
        raise mapped from exc
