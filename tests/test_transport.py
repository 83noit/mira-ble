"""BleTransport against a fake bleak client."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import patch

import pytest
from bleak import BleakError
from bleak.backends.device import BLEDevice
from bleak.exc import BleakCharacteristicNotFoundError

from mira_ble import protocol
from mira_ble import transport as transport_module
from mira_ble.exceptions import (
    CommandRejected,
    ConnectionLost,
    MiraConnectionError,
    PairingRequired,
    RequestTimeout,
)
from mira_ble.frame import Frame
from mira_ble.testing import ACK_FRAME, PRESET_SHOWER, STATE_RUNNING, STOP_FRAME
from mira_ble.transport import BleTransport

STATE_REQUEST = protocol.read_state_request()
STATE_REPLY = Frame.reply(STATE_RUNNING).encode()


class FakeClient:
    """Enough of BleakClient for the transport: notifications, writes, disconnects."""

    def __init__(self, replies: dict[bytes, bytes] | None = None) -> None:
        self.replies = replies or {}
        self.writes: list[bytes] = []
        self.is_connected = True
        self.mtu_size = 23
        self.notify_cb: Callable[[Any, bytearray], None] | None = None
        self.disconnected_callback: Callable[[Any], None] | None = None
        self.auto_reply = True
        self.cache_cleared = False
        self.events: list[str] = []
        self.params: tuple[int, ...] | None = None
        self._connected_scanner: Any = None

    async def stop_notify(self, _char: str) -> None:
        self.events.append("stop_notify")

    async def set_connection_params(self, *params: int) -> None:
        self.params = params

    async def start_notify(self, _char: str, callback: Callable[[Any, bytearray], None]) -> None:
        self.events.append("start_notify")
        self.notify_cb = callback

    async def write_gatt_char(self, _char: str, data: bytes, response: bool | None = None) -> None:
        assert response is False
        self.writes.append(bytes(data))
        if not self.auto_reply:
            return
        frame = b"".join(self.writes)
        if frame in self.replies and self.notify_cb is not None:
            reply = self.replies[frame]
            self.writes.clear()
            for start in range(0, len(reply), 20):
                self.notify_cb(None, bytearray(reply[start : start + 20]))

    async def disconnect(self) -> None:
        self.events.append("disconnect")
        self.is_connected = False

    async def clear_cache(self) -> None:
        self.cache_cleared = True

    def drop(self) -> None:
        self.is_connected = False
        assert self.disconnected_callback is not None
        self.disconnected_callback(self)


def _device(details: dict[str, Any] | None = None) -> BLEDevice:
    return BLEDevice(
        "AA:BB:CC:DD:EE:FF", "Mira 0051 Test", details or {"source": "00:11:22:33:44:55"}
    )


@contextlib.asynccontextmanager
async def _no_agent(_device: BLEDevice) -> AsyncIterator[None]:
    yield


@pytest.fixture(autouse=True)
def no_settle():
    with (
        patch.object(transport_module, "SETTLE_AFTER_SUBSCRIBE", 0),
        patch.object(transport_module, "SETTLE_AFTER_PAIR", 0),
        patch.object(transport_module, "SUBSCRIBE_RETRY_DELAY", 0),
        patch.object(transport_module, "RETRY_DELAY", 0),
        patch.object(transport_module, "CHUNK_SPACING", 0),
    ):
        yield


@pytest.fixture
def fake_establish():
    client = FakeClient({STATE_REQUEST.encode(): STATE_REPLY, STOP_FRAME: ACK_FRAME})

    async def establish(_cls: Any, _device: BLEDevice, _name: str, **kwargs: Any) -> FakeClient:
        assert kwargs["pair"] is True
        client.disconnected_callback = kwargs["disconnected_callback"]
        return client

    with patch.object(transport_module, "establish_connection", establish):
        yield client


def _establish_sequence(*outcomes: Any):
    remaining = list(outcomes)
    calls: list[dict[str, Any]] = []

    async def establish(_cls: Any, _device: BLEDevice, _name: str, **kwargs: Any) -> Any:
        calls.append(kwargs)
        outcome = remaining.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        outcome.disconnected_callback = kwargs["disconnected_callback"]
        return outcome

    return establish, calls


async def test_request_roundtrip(fake_establish: FakeClient) -> None:
    transport = BleTransport(_device(), name="test")
    assert not transport.is_connected
    await transport.connect()
    await transport.connect()  # idempotent
    assert transport.is_connected
    reply = await transport.request(STATE_REQUEST)
    assert reply.payload == STATE_RUNNING
    assert list(transport.last_frames) == [("tx", STATE_REQUEST.encode()), ("rx", STATE_REPLY)]
    assert transport.device.address == "AA:BB:CC:DD:EE:FF"
    await transport.disconnect()
    assert not transport.is_connected
    await transport.disconnect()  # idempotent


async def test_a_reply_split_over_notifications_is_reassembled(fake_establish: FakeClient) -> None:
    assert len(STATE_REPLY) > 20  # so the fake delivers it in two pieces
    transport = BleTransport(_device())
    await transport.connect()
    assert (await transport.request(STATE_REQUEST)).payload == STATE_RUNNING


async def test_short_frames_are_written_whole_and_long_ones_split_at_the_mtu(
    fake_establish: FakeClient,
) -> None:
    fake_establish.auto_reply = False
    transport = BleTransport(_device(), request_timeout=0.01)
    await transport.connect()
    long_frame = Frame.request(0xDD, PRESET_SHOWER)
    with pytest.raises(RequestTimeout):
        await transport.request(long_frame)
    data = long_frame.encode()
    assert fake_establish.writes == [data[i : i + 20] for i in range(0, len(data), 20)]


async def test_a_larger_mtu_carries_a_long_frame_in_one_write(fake_establish: FakeClient) -> None:
    fake_establish.auto_reply = False
    fake_establish.mtu_size = 247
    transport = BleTransport(_device(), request_timeout=0.01)
    await transport.connect()
    long_frame = Frame.request(0xDD, PRESET_SHOWER)
    with pytest.raises(RequestTimeout):
        await transport.request(long_frame)
    assert fake_establish.writes == [long_frame.encode()]


async def test_an_unknown_mtu_falls_back_to_the_default() -> None:
    class NoMtu:
        @property
        def mtu_size(self) -> int:
            raise RuntimeError("not exchanged")

    assert BleTransport._write_size(NoMtu()) == 20  # type: ignore[arg-type]


async def test_rejection_raises_with_the_request_opcode(fake_establish: FakeClient) -> None:
    fake_establish.replies[STOP_FRAME] = Frame.reply(b"\x80").encode()
    transport = BleTransport(_device())
    await transport.connect()
    with pytest.raises(CommandRejected) as caught:
        await transport.request(protocol.stop_request())
    assert caught.value.opcode == 0xAB
    assert transport.is_connected  # a refusal is an answer; the link stays


async def test_timeout_closes_the_link_and_says_nothing_came(fake_establish: FakeClient) -> None:
    fake_establish.auto_reply = False
    transport = BleTransport(_device(), request_timeout=0.01)
    await transport.connect()
    with pytest.raises(RequestTimeout, match=r"no reply to .*; nothing received$"):
        await transport.request(STATE_REQUEST)
    assert not transport.is_connected


async def test_timeout_quotes_the_pieces_of_an_unfinished_reply(fake_establish: FakeClient) -> None:
    fake_establish.auto_reply = False
    transport = BleTransport(_device(), request_timeout=0.01)
    await transport.connect()
    task = asyncio.ensure_future(transport.request(STATE_REQUEST))
    await asyncio.sleep(0)
    assert fake_establish.notify_cb is not None
    fake_establish.notify_cb(None, bytearray(STATE_REPLY[:10]))
    with pytest.raises(RequestTimeout) as caught:
        await task
    assert str(caught.value).endswith(f"; 1 piece(s) received: {STATE_REPLY[:10].hex(' ')}")


async def test_timeout_quotes_a_bounded_number_of_pieces(fake_establish: FakeClient) -> None:
    fake_establish.auto_reply = False
    transport = BleTransport(_device(), request_timeout=0.01)
    await transport.connect()
    task = asyncio.ensure_future(transport.request(STATE_REQUEST))
    await asyncio.sleep(0)
    assert fake_establish.notify_cb is not None
    for _ in range(transport_module.MAX_PIECES_REPORTED + 1):
        fake_establish.notify_cb(None, bytearray(b"\x00"))
    with pytest.raises(RequestTimeout, match=r"9 piece\(s\) received: (00 \| ){8}\.\.\.$"):
        await task


@pytest.mark.parametrize(
    ("details", "cache"),
    [
        ({"source": "00:11:22:33:44:55"}, True),
        ({"path": "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"}, False),
    ],
    ids=["proxy", "local-bluez"],
)
async def test_a_local_adapter_waits_for_services_and_a_proxy_uses_its_cache(
    details: dict[str, Any], cache: bool
) -> None:
    establish, calls = _establish_sequence(FakeClient())
    with (
        patch.object(transport_module, "establish_connection", establish),
        patch.object(transport_module, "bluez_pairing_agent", _no_agent),
    ):
        await BleTransport(_device(details)).connect()
    assert calls[0]["use_services_cache"] is cache
    assert calls[0]["pair"] is True
    assert calls[0]["max_attempts"] == 1


async def test_not_connected() -> None:
    with pytest.raises(ConnectionLost):
        await BleTransport(_device()).request(STATE_REQUEST)


async def test_a_drop_during_a_request_is_connection_lost(fake_establish: FakeClient) -> None:
    fake_establish.auto_reply = False
    dropped: list[bool] = []
    transport = BleTransport(_device(), disconnected_callback=lambda: dropped.append(True))
    await transport.connect()
    task = asyncio.create_task(transport.request(STATE_REQUEST))
    await asyncio.sleep(0)
    fake_establish.drop()
    with pytest.raises(ConnectionLost):
        await task
    assert dropped == [True]
    assert not transport.is_connected


async def test_a_late_callback_from_an_earlier_link_is_ignored(fake_establish: FakeClient) -> None:
    transport = BleTransport(_device())
    await transport.connect()
    transport._on_disconnect(FakeClient())  # type: ignore[arg-type]
    assert transport.is_connected


async def test_an_unsolicited_frame_is_recorded_and_ignored(fake_establish: FakeClient) -> None:
    transport = BleTransport(_device())
    await transport.connect()
    assert fake_establish.notify_cb is not None
    fake_establish.notify_cb(None, bytearray(ACK_FRAME))
    assert transport.last_frames[-1] == ("rx", ACK_FRAME)


async def test_a_refused_pairing_is_pairing_required() -> None:
    establish, _ = _establish_sequence(BleakError("Pairing failed due to error: 82"))
    with (
        patch.object(transport_module, "establish_connection", establish),
        pytest.raises(PairingRequired) as caught,
    ):
        await BleTransport(_device()).connect()
    assert caught.value.error_code == 82
    assert caught.value.refused


async def test_bluez_authentication_failed_is_pairing_required() -> None:
    establish, _ = _establish_sequence(
        BleakError("[org.bluez.Error.AuthenticationFailed] Authentication Failed")
    )
    with (
        patch.object(transport_module, "establish_connection", establish),
        pytest.raises(PairingRequired),
    ):
        await BleTransport(_device()).connect()


@pytest.mark.parametrize("code", [99, 102, 96, 92, -1])
async def test_transient_smp_reasons_are_not_pairing_required(code: int) -> None:
    establish, _ = _establish_sequence(BleakError(f"Pairing failed due to error: {code}"))
    with (
        patch.object(transport_module, "establish_connection", establish),
        pytest.raises(MiraConnectionError) as caught,
    ):
        await BleTransport(_device(), max_attempts=1).connect()
    assert not isinstance(caught.value, PairingRequired)
    assert caught.value.error_code == code


async def test_a_refusal_on_the_first_attempt_is_not_lost_to_a_retry() -> None:
    establish, calls = _establish_sequence(
        BleakError("Pairing failed due to error: 82"), FakeClient()
    )
    with (
        patch.object(transport_module, "establish_connection", establish),
        pytest.raises(PairingRequired),
    ):
        await BleTransport(_device(), max_attempts=2).connect()
    assert len(calls) == 1


async def test_a_transient_failure_is_retried_through_the_freshest_device() -> None:
    client = FakeClient()
    establish, calls = _establish_sequence(BleakError("no connection slot"), client)
    asked: list[bool] = []
    other = _device({"source": "00:11:22:33:44:66"})

    def device_callback() -> BLEDevice:
        asked.append(True)
        return other

    with patch.object(transport_module, "establish_connection", establish):
        transport = BleTransport(_device(), max_attempts=2, device_callback=device_callback)
        await transport.connect()
    assert len(calls) == 2
    assert asked == [True, True]
    assert transport.device is other


async def test_connect_errors_and_timeouts() -> None:
    establish, _ = _establish_sequence(BleakError("no slot"), BleakError("no slot"))
    with (
        patch.object(transport_module, "establish_connection", establish),
        pytest.raises(MiraConnectionError, match="no slot"),
    ):
        await BleTransport(_device()).connect()
    establish, _ = _establish_sequence(TimeoutError(), TimeoutError())
    with (
        patch.object(transport_module, "establish_connection", establish),
        pytest.raises(MiraConnectionError, match="timed out"),
    ):
        await BleTransport(_device()).connect()


async def test_other_subscribe_failures_disconnect(fake_establish: FakeClient) -> None:
    async def failing_notify(*_args: Any) -> None:
        raise BleakError("GATT error 133")

    fake_establish.start_notify = failing_notify  # type: ignore[method-assign]
    transport = BleTransport(_device())
    with pytest.raises(MiraConnectionError, match="133") as caught:
        await transport.connect()
    assert not isinstance(caught.value, PairingRequired)
    assert not transport.is_connected


async def test_a_cancelled_subscribe_disconnects(fake_establish: FakeClient) -> None:
    async def cancelled(*_args: Any) -> None:
        raise asyncio.CancelledError

    fake_establish.start_notify = cancelled  # type: ignore[method-assign]
    transport = BleTransport(_device())
    with pytest.raises(asyncio.CancelledError):
        await transport.connect()
    assert not fake_establish.is_connected


async def test_a_stale_service_cache_is_cleared() -> None:
    class CachedClient(FakeClient):
        async def start_notify(self, *_args: Any) -> None:
            raise BleakCharacteristicNotFoundError("267f0003")

    client = CachedClient()
    establish, _ = _establish_sequence(client)
    with (
        patch.object(transport_module, "establish_connection", establish),
        patch.object(transport_module, "BleakClientWithServiceCache", CachedClient),
        pytest.raises(MiraConnectionError),
    ):
        await BleTransport(_device()).connect()
    assert client.cache_cleared


async def test_a_write_error_is_translated(fake_establish: FakeClient) -> None:
    async def failing_write(*_args: Any, **_kwargs: Any) -> None:
        raise BleakError("write failed")

    fake_establish.write_gatt_char = failing_write  # type: ignore[method-assign]
    transport = BleTransport(_device())
    await transport.connect()
    with pytest.raises(MiraConnectionError, match="write failed"):
        await transport.request(STATE_REQUEST)
