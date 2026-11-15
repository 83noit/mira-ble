"""Bluetooth LE transport for Mira valves, built on bleak and bleak-retry-connector.

The transport takes a ``BLEDevice`` from the caller and never scans. On every connection it
pairs, waits for the encryption to settle, subscribes to the reply characteristic, and then
runs one request at a time: write the frame, wait for a reply of the shape the caller expects.

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
import contextlib
import logging
import re
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, Protocol, Self

from bleak import BleakClient
from bleak.exc import BleakCharacteristicNotFoundError, BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from mira_ble.bluez import bluez_device_path, bluez_pairing_agent
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
    from bleak.backends.characteristic import BleakGATTCharacteristic
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)

type ReplyTest = Callable[[bytes], bool]
"""Says whether a reply payload has the shape a request expects."""

DEFAULT_REQUEST_TIMEOUT: Final = 10.0
"""Seconds to wait for a whole reply, write included.

State replies on an idle link have been reported to take around 6 s through a proxy, with
occasional stalls beyond 10 s (Mira-Activate-HA). Not measured by this project.
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
DEFAULT_MTU_PAYLOAD: Final = 20
"""Bytes one write can carry when the negotiated MTU is unknown (ATT default 23, less 3)."""
ATT_OVERHEAD: Final = 3
CHUNK_SPACING: Final = 0.025
MAX_PIECES_REPORTED: Final = 8
"""Notification pieces of an unfinished reply quoted in a RequestTimeout."""
RETRY_DELAY: Final = 0.25

PAIRING_REFUSED_CODES: Final = frozenset({82})
"""ESP-IDF SMP reasons that mean "no bond and the valve is not in pairing mode"."""
PAIRING_UNSUPPORTED_CODES: Final = frozenset({129})
"""Reasons reported by proxies that cannot pair at all; the bond then has to exist already."""
INSUFFICIENT_AUTHENTICATION_CODE: Final = 5
"""The GATT status behind a subscribe refused for want of encryption."""

_PAIRING_FAILED = re.compile(r"Pairing failed due to error: (-?\d+)")
"""bleak-esphome's message for a failed pair call, with the proxy's reason code."""
_INSUFFICIENT_AUTHENTICATION = re.compile(r"insufficient (authentication|encryption)", re.I)
BLUEZ_AUTHENTICATION_FAILED: Final = "org.bluez.Error.AuthenticationFailed"
"""BlueZ's report of a refused pairing."""
_LINK_ERRORS: Final = (BleakError, EOFError, BrokenPipeError, AttributeError, NotImplementedError)
"""What bleak backends raise for link trouble; bleak-retry-connector retries most of these."""


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
        self._device = device
        self._device_callback = device_callback
        self._name = name or device.name or device.address
        self._max_attempts = max(1, max_attempts)
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout
        self._disconnected_callback = disconnected_callback
        self._expect_new_bond = expect_new_bond
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._pending: asyncio.Future[bytes] | None = None
        self._accept: ReplyTest | None = None
        self._assembler = FrameAssembler()
        self._pieces: list[bytes] = []
        self._source: str | None = None
        self.unsolicited = 0
        """Frames received that no request was waiting for, or that fitted none."""
        self.last_frames: deque[tuple[str, bytes]] = deque(maxlen=48)
        """Recent traffic as ("tx" | "rx", frame) for diagnostics and fixture capture."""

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.disconnect()

    @property
    def device(self) -> BLEDevice:
        """The device this transport talks to."""
        return self._device

    @property
    def is_connected(self) -> bool:
        """True while the BLE link is up."""
        return self._client is not None and self._client.is_connected

    @property
    def source(self) -> str | None:
        """The adapter or proxy the last connection attempt went through, when known.

        The bond lives on one adapter, so a refusal only says something about the bond when
        it came through that adapter.
        """
        return self._source

    async def connect(self) -> None:
        """Connect through the device's path, pair (encrypt) and subscribe to replies.

        Retries are run here, one connection attempt at a time, so that a refusal on any
        attempt is seen; bleak-retry-connector reports only the last attempt's error.
        """
        if self.is_connected:
            return
        self._assembler.reset()
        self._pieces.clear()
        client = await self._connect_with_retries()
        self._client = client
        try:
            await self._set_connection_params(client)
            await asyncio.sleep(SETTLE_AFTER_PAIR)
            await self._subscribe(client)
        except BleakCharacteristicNotFoundError as err:
            # The service cache is stale, or this is not a valve that speaks the protocol.
            if isinstance(client, BleakClientWithServiceCache):
                with contextlib.suppress(Exception):
                    await client.clear_cache()
            await self.disconnect()
            raise MiraConnectionError(
                f"{self._name}: the protocol characteristic was not found; the device does "
                f"not offer the 267f service, or a cached service list is stale ({err})"
            ) from err
        except (*_LINK_ERRORS, TimeoutError) as err:
            await self.disconnect()
            raise self._translate(err, subscribing=True) from err
        except BaseException:
            await self.disconnect()
            raise
        _LOGGER.debug("%s: connected and subscribed via %s", self._name, self._source)
        await asyncio.sleep(SETTLE_AFTER_SUBSCRIBE)

    async def _subscribe(self, client: BleakClient) -> None:
        """Subscribe to replies, retrying once if the encryption was not up yet."""
        try:
            await client.start_notify(NOTIFY_CHARACTERISTIC_UUID, self._on_notify)
        except BleakCharacteristicNotFoundError:
            raise
        except _LINK_ERRORS as err:
            if not _INSUFFICIENT_AUTHENTICATION.search(str(err)):
                raise
            _LOGGER.debug("%s: subscribe refused (%s); retrying once", self._name, err)
            await asyncio.sleep(SUBSCRIBE_RETRY_DELAY)
            await client.start_notify(NOTIFY_CHARACTERISTIC_UUID, self._on_notify)

    async def _set_connection_params(self, client: BleakClient) -> None:
        """Ask for a quicker, more tolerant link where the backend can; never fail on it."""
        setter = getattr(client, "set_connection_params", None)
        if setter is None:
            return
        try:
            await setter(*CONNECTION_PARAMS)
        except Exception as err:  # best effort: every backend differs
            _LOGGER.debug("%s: connection parameters not set: %s", self._name, err)

    def _note_source(self, client: object | None) -> None:
        """Record which adapter or proxy served the attempt."""
        scanner = getattr(client, "_connected_scanner", None)  # Home Assistant's wrapper
        source = getattr(scanner, "source", None)
        if not isinstance(source, str):
            details = self._device.details
            source = details.get("source") if isinstance(details, dict) else None
            if not isinstance(source, str):
                path = bluez_device_path(self._device)
                source = path.rsplit("/", 1)[0] if path else None
        self._source = source

    async def _establish(self, *, pair: bool) -> BleakClient:
        return await establish_connection(
            BleakClientWithServiceCache,
            self._device,
            self._name,
            disconnected_callback=self._on_disconnect,
            max_attempts=1,
            pair=pair,
            # With bleak's own cache a local connect returns before bluetoothd has finished
            # setting up GATT; without it bleak waits for ServicesResolved, which BlueZ's
            # cache for a bonded device keeps quick. A proxy keeps its cache.
            use_services_cache=bluez_device_path(self._device) is None,
        )

    async def _connect_with_retries(self) -> BleakClient:
        last: MiraConnectionError | None = None
        for attempt in range(self._max_attempts):
            if attempt:
                await asyncio.sleep(RETRY_DELAY)
            if self._device_callback is not None:
                self._device = self._device_callback() or self._device
            self._note_source(None)
            try:
                async with asyncio.timeout(self._connect_timeout), self._agent():
                    try:
                        client = await self._establish(pair=True)
                    except _LINK_ERRORS as err:
                        if not self._pairing_unsupported(err):
                            raise
                        # This adapter cannot pair at all. If it already holds a bond the link
                        # may still encrypt by itself, so try once without pairing.
                        _LOGGER.debug("%s: adapter cannot pair (%s)", self._name, err)
                        client = await self._establish(pair=False)
                self._note_source(client)
            except _LINK_ERRORS as err:
                last = self._translate(err)
                last.__cause__ = err
                if isinstance(last, PairingRequired):
                    raise last from err
            except TimeoutError as err:
                last = MiraConnectionError(f"timed out connecting to {self._name}")
                last.__cause__ = err
            else:
                return client
        assert last is not None
        raise last

    def _agent(self) -> contextlib.AbstractAsyncContextManager[None]:
        if self._expect_new_bond:
            return bluez_pairing_agent(self._device)
        return contextlib.nullcontext()

    @staticmethod
    def _pairing_unsupported(err: BaseException) -> bool:
        if isinstance(err, NotImplementedError):
            return True
        match = _PAIRING_FAILED.search(str(err))
        return match is not None and int(match.group(1)) in PAIRING_UNSUPPORTED_CODES

    async def disconnect(self) -> None:
        """Drop the link; safe to call when not connected."""
        client, self._client = self._client, None
        if client is None:
            return
        if client.is_connected:
            # Leaving notifications running can let a native callback fire into a torn-down
            # client (mira-mode-control saw it crash a process on exit).
            with contextlib.suppress(Exception):
                await client.stop_notify(NOTIFY_CHARACTERISTIC_UUID)
        with contextlib.suppress(Exception):
            await client.disconnect()

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
        async with self._lock:
            client = self._require_client()
            loop = asyncio.get_running_loop()
            future: asyncio.Future[bytes] = loop.create_future()
            self._assembler.reset()
            self._pieces.clear()
            self._pending, self._accept = future, accept
            data = frame.encode()
            completed = False
            try:
                async with asyncio.timeout(self._request_timeout if timeout is None else timeout):
                    await self._write(client, data)
                    reply = await future
                completed = True
            except TimeoutError as err:
                raise RequestTimeout(
                    f"{self._name}: no reply to {data.hex(' ')}; {self._received()}"
                ) from err
            except _LINK_ERRORS as err:
                raise self._translate(err) from err
            finally:
                self._pending = self._accept = None
                if future.done() and not future.cancelled():
                    future.exception()  # mark as retrieved
                else:
                    future.cancel()
                if not completed:
                    await self.disconnect()
            decoded = Frame.decode(reply)
            if decoded.is_rejection:
                raise CommandRejected(frame.opcode, reply)
            return decoded

    def _require_client(self) -> BleakClient:
        if self._client is None or not self._client.is_connected:
            raise ConnectionLost(f"{self._name}: not connected")
        return self._client

    async def _write(self, client: BleakClient, data: bytes) -> None:
        self.last_frames.append(("tx", data))
        _LOGGER.debug("%s: tx %s", self._name, data.hex(" "))
        size = self._write_size(client)
        for start in range(0, len(data), size):
            if start:
                await asyncio.sleep(CHUNK_SPACING)
            await client.write_gatt_char(
                WRITE_CHARACTERISTIC_UUID, data[start : start + size], response=False
            )

    @staticmethod
    def _write_size(client: BleakClient) -> int:
        """Bytes one write may carry on this link.

        Only a preset write (67 bytes) is ever longer than the default. Whether a valve joins
        a command split over several writes has not been measured: the vendor app and the
        other projects always write a frame whole, on a link with a larger MTU.
        """
        try:
            mtu = int(client.mtu_size)
        except Exception:  # some backends only know the MTU after an exchange
            return DEFAULT_MTU_PAYLOAD
        return max(DEFAULT_MTU_PAYLOAD, mtu - ATT_OVERHEAD)

    def _received(self) -> str:
        """Describe what came for a request that got no whole reply: silence, or pieces."""
        if not self._pieces:
            return "nothing received"
        quoted = " | ".join(piece.hex(" ") for piece in self._pieces[:MAX_PIECES_REPORTED])
        more = " | ..." if len(self._pieces) > MAX_PIECES_REPORTED else ""
        return f"{len(self._pieces)} piece(s) received: {quoted}{more}"

    def _on_notify(self, _characteristic: BleakGATTCharacteristic, data: bytearray) -> None:
        self._pieces.append(bytes(data))
        for raw in self._assembler.feed(data):
            self.last_frames.append(("rx", raw))
            _LOGGER.debug("%s: rx %s", self._name, raw.hex(" "))
            frame = Frame.decode(raw)
            pending = self._pending
            if (
                pending is not None
                and not pending.done()
                and frame.is_valid_reply
                and (frame.is_rejection or self._accept is None or self._accept(frame.payload))
            ):
                pending.set_result(raw)
            else:
                self.unsolicited += 1
                _LOGGER.debug("%s: unsolicited frame %s", self._name, raw.hex(" "))

    def _on_disconnect(self, client: BleakClient) -> None:
        if self._client is not None and client is not self._client:
            return  # a late callback from an earlier link
        self._client = None
        pending = self._pending
        if pending is not None and not pending.done():
            pending.set_exception(ConnectionLost(f"{self._name}: disconnected"))
        if self._disconnected_callback is not None:
            self._disconnected_callback()

    def _translate(self, err: BaseException, *, subscribing: bool = False) -> MiraConnectionError:
        text = str(err)
        if match := _PAIRING_FAILED.search(text):
            code = int(match.group(1))
            if code in PAIRING_REFUSED_CODES:
                return PairingRequired(f"{self._name}: {text}", code)
            return MiraConnectionError(f"{self._name}: {text}", code)
        if BLUEZ_AUTHENTICATION_FAILED in text:
            return PairingRequired(f"{self._name}: {text}")
        if subscribing and _INSUFFICIENT_AUTHENTICATION.search(text):
            # The link came up but was not encrypted, twice. Weak evidence about the bond.
            return PairingRequired(
                f"{self._name}: {text}", INSUFFICIENT_AUTHENTICATION_CODE, refused=False
            )
        return MiraConnectionError(f"{self._name}: {type(err).__name__}: {text}")
