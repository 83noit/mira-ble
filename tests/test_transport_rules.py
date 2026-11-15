"""The transport's rules for settling, matching replies and closing links."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest
from bleak import BleakError
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
from mira_ble.testing import NAME, STATE_RUNNING
from mira_ble.transport import BleTransport

from .test_transport import (  # noqa: F401 - fixtures
    STATE_REPLY,
    STATE_REQUEST,
    FakeClient,
    _device,
    _establish_sequence,
    fake_establish,
    no_settle,
)

# --- connecting -------------------------------------------------------------------------------


async def test_the_link_settles_between_pairing_and_subscribing_and_again_after(
    fake_establish: FakeClient,
) -> None:
    """A subscribe landing before the encryption is up reads as a missing bond."""
    events: list[str] = []

    async def sleep(delay: float) -> None:
        events.append(f"sleep {delay}")

    async def start_notify(_char: str, callback: Callable[[Any, bytearray], None]) -> None:
        events.append("subscribe")
        fake_establish.notify_cb = callback

    fake_establish.start_notify = start_notify  # type: ignore[method-assign]
    with (
        patch.object(transport_module, "SETTLE_AFTER_PAIR", 0.5),
        patch.object(transport_module, "SETTLE_AFTER_SUBSCRIBE", 0.25),
        patch.object(transport_module.asyncio, "sleep", sleep),
    ):
        await BleTransport(_device()).connect()
    assert events == ["sleep 0.5", "subscribe", "sleep 0.25"]


async def test_connection_parameters_are_requested_where_the_backend_can(
    fake_establish: FakeClient,
) -> None:
    await BleTransport(_device()).connect()
    assert fake_establish.params == (24, 60, 0, 2000)


async def test_a_backend_that_cannot_set_parameters_is_fine(fake_establish: FakeClient) -> None:
    async def failing(*_params: int) -> None:
        raise BleakError("not supported")

    fake_establish.set_connection_params = failing  # type: ignore[method-assign]
    transport = BleTransport(_device())
    await transport.connect()
    assert transport.is_connected


async def test_the_adapter_actually_used_is_reported(fake_establish: FakeClient) -> None:
    """The bond lives on one adapter; a refusal means something only if it came through it."""
    transport = BleTransport(_device())
    await transport.connect()
    assert transport.source == "00:11:22:33:44:55"  # from the device handed in
    await transport.disconnect()
    fake_establish.is_connected = True
    fake_establish._connected_scanner = type("Scanner", (), {"source": "00:11:22:33:44:99"})()
    await transport.connect()
    assert transport.source == "00:11:22:33:44:99"  # Home Assistant picked another proxy
    local = BleTransport(_device({"path": "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"}))
    local._note_source(None)
    assert local.source == "/org/bluez/hci0"


async def test_notifications_are_stopped_before_the_link_is_dropped(
    fake_establish: FakeClient,
) -> None:
    transport = BleTransport(_device())
    await transport.connect()
    await transport.disconnect()
    assert fake_establish.events == ["start_notify", "stop_notify", "disconnect"]


async def test_async_with_connects_and_disconnects(fake_establish: FakeClient) -> None:
    async with BleTransport(_device()) as transport:
        assert transport.is_connected
        assert (await transport.request(STATE_REQUEST)).payload == STATE_RUNNING
    assert not transport.is_connected


@pytest.mark.parametrize("message", ["Insufficient authentication", "Insufficient Encryption"])
async def test_a_subscribe_refused_twice_is_weak_evidence_of_a_missing_bond(
    fake_establish: FakeClient, message: str
) -> None:
    attempts: list[int] = []

    async def failing_notify(*_args: Any) -> None:
        attempts.append(1)
        raise BleakError(f"Bluetooth GATT Error handle=17 description={message}")

    fake_establish.start_notify = failing_notify  # type: ignore[method-assign]
    transport = BleTransport(_device())
    with pytest.raises(PairingRequired) as caught:
        await transport.connect()
    assert len(attempts) == 2  # retried once: the encryption may just not have been up
    assert caught.value.error_code == 5
    assert not caught.value.refused  # not, by itself, a reason to pair again
    assert not transport.is_connected


async def test_a_subscribe_that_races_the_encryption_succeeds_on_the_retry(
    fake_establish: FakeClient,
) -> None:
    calls: list[int] = []

    async def racing_notify(_char: str, callback: Callable[[Any, bytearray], None]) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise BleakError("Insufficient authentication")
        fake_establish.notify_cb = callback

    fake_establish.start_notify = racing_notify  # type: ignore[method-assign]
    transport = BleTransport(_device())
    await transport.connect()
    assert (await transport.request(STATE_REQUEST)).payload == STATE_RUNNING


@pytest.mark.parametrize(
    "message", ["GATT error: status=5", "failed to subscribe: error=5", "error 5 in handler 55"]
)
async def test_a_bare_five_in_an_error_is_not_read_as_a_missing_bond(
    fake_establish: FakeClient, message: str
) -> None:
    async def failing_notify(*_args: Any) -> None:
        raise BleakError(message)

    fake_establish.start_notify = failing_notify  # type: ignore[method-assign]
    with pytest.raises(MiraConnectionError) as caught:
        await BleTransport(_device()).connect()
    assert not isinstance(caught.value, PairingRequired)


async def test_a_device_without_the_protocol_characteristic_says_so(
    fake_establish: FakeClient,
) -> None:
    async def missing(*_args: Any) -> None:
        raise BleakCharacteristicNotFoundError("267f0003")

    fake_establish.start_notify = missing  # type: ignore[method-assign]
    with pytest.raises(MiraConnectionError, match="does not offer the 267f service"):
        await BleTransport(_device()).connect()


async def test_the_reason_code_is_read_from_the_exact_message_not_the_first_digits() -> None:
    """An address or handle before the reason must not be mistaken for it."""
    establish, _ = _establish_sequence(
        BleakError("AA:BB:CC:DD:12:34: Pairing failed due to error: 82")
    )
    with (
        patch.object(transport_module, "establish_connection", establish),
        pytest.raises(PairingRequired) as caught,
    ):
        await BleTransport(_device()).connect()
    assert caught.value.error_code == 82


@pytest.mark.parametrize(
    "failure",
    [
        NotImplementedError("Pairing is not available"),
        BleakError("Pairing failed due to error: 129"),
    ],
)
async def test_an_adapter_that_cannot_pair_is_tried_once_without_pairing(
    failure: Exception,
) -> None:
    """If it already holds a bond the link may encrypt by itself."""
    client = FakeClient({STATE_REQUEST.encode(): STATE_REPLY})
    establish, calls = _establish_sequence(failure, client)
    with patch.object(transport_module, "establish_connection", establish):
        transport = BleTransport(_device(), max_attempts=1)
        await transport.connect()
    assert [call["pair"] for call in calls] == [True, False]
    assert (await transport.request(STATE_REQUEST)).payload == STATE_RUNNING


# --- reply matching ---------------------------------------------------------------------------


async def test_a_stray_frame_of_the_wrong_shape_does_not_answer_the_request(
    fake_establish: FakeClient,
) -> None:
    """Replies carry no id: one stray frame used to shift every later reply by one."""
    fake_establish.auto_reply = False
    transport = BleTransport(_device())
    await transport.connect()
    task = asyncio.ensure_future(transport.request(STATE_REQUEST, accept=protocol.is_state_reply))
    await asyncio.sleep(0)
    assert fake_establish.notify_cb is not None
    fake_establish.notify_cb(None, bytearray(Frame.reply(NAME).encode()))  # not a state
    await asyncio.sleep(0)
    assert not task.done()
    fake_establish.notify_cb(None, bytearray(STATE_REPLY))
    assert (await task).payload == STATE_RUNNING
    assert transport.unsolicited == 1


async def test_a_frame_that_is_not_a_valve_reply_does_not_answer_the_request(
    fake_establish: FakeClient,
) -> None:
    fake_establish.auto_reply = False
    transport = BleTransport(_device(), request_timeout=0.02)
    await transport.connect()
    task = asyncio.ensure_future(transport.request(STATE_REQUEST))
    await asyncio.sleep(0)
    assert fake_establish.notify_cb is not None
    echo = Frame(0, 0x2B, STATE_RUNNING).encode()  # a request-channel frame of the right size
    fake_establish.notify_cb(None, bytearray(echo))
    with pytest.raises(RequestTimeout):
        await task


async def test_a_rejection_answers_whatever_shape_was_expected(fake_establish: FakeClient) -> None:
    fake_establish.replies[STATE_REQUEST.encode()] = Frame.reply(b"\x80").encode()
    transport = BleTransport(_device())
    await transport.connect()
    with pytest.raises(CommandRejected):
        await transport.request(STATE_REQUEST, accept=protocol.is_state_reply)


# --- closing a link whose request did not complete --------------------------------------------


async def test_a_reply_arriving_after_its_timeout_never_answers_the_next_request(
    fake_establish: FakeClient,
) -> None:
    fake_establish.auto_reply = False
    transport = BleTransport(_device(), request_timeout=0.01)
    await transport.connect()
    with pytest.raises(RequestTimeout):
        await transport.request(STATE_REQUEST)
    assert not transport.is_connected  # the link is gone, and the late reply with it
    assert fake_establish.notify_cb is not None
    fake_establish.notify_cb(None, bytearray(STATE_REPLY))
    with pytest.raises(ConnectionLost):
        await transport.request(protocol.stop_request())


async def test_a_cancelled_request_closes_the_link(fake_establish: FakeClient) -> None:
    """Its reply would otherwise acknowledge the next command."""
    fake_establish.auto_reply = False
    transport = BleTransport(_device())
    await transport.connect()
    task = asyncio.ensure_future(transport.request(STATE_REQUEST))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not transport.is_connected


async def test_a_failed_write_closes_the_link(fake_establish: FakeClient) -> None:
    async def failing_write(*_args: Any, **_kwargs: Any) -> None:
        raise BleakError("write failed")

    fake_establish.write_gatt_char = failing_write  # type: ignore[method-assign]
    transport = BleTransport(_device())
    await transport.connect()
    with pytest.raises(MiraConnectionError, match="write failed"):
        await transport.request(STATE_REQUEST)
    assert not transport.is_connected


async def test_a_timeout_of_zero_is_zero_not_the_default(fake_establish: FakeClient) -> None:
    fake_establish.auto_reply = False
    transport = BleTransport(_device(), request_timeout=30)
    await transport.connect()
    with pytest.raises(RequestTimeout):
        await transport.request(STATE_REQUEST, timeout=0)


async def test_the_link_works_again_after_a_timeout(fake_establish: FakeClient) -> None:
    fake_establish.auto_reply = False
    transport = BleTransport(_device(), request_timeout=0.01)
    await transport.connect()
    with pytest.raises(RequestTimeout):
        await transport.request(STATE_REQUEST)
    fake_establish.is_connected = fake_establish.auto_reply = True
    fake_establish.writes.clear()
    await transport.connect()
    assert (await transport.request(STATE_REQUEST)).payload == STATE_RUNNING
