"""The transport as an adapter over ble-bond: policy, reply matching, rejections, exceptions.

The link itself (retries, refusals, which proxy was asked, closing on an unfinished request,
the BlueZ agent) is tested in ble-bond.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import ble_bond
import pytest
from ble_bond import LinkPolicy, SubscribeRetry
from ble_bond import link as link_module
from ble_bond.testing import FakeClient, patched_establish
from bleak import BleakError
from bleak.backends.device import BLEDevice
from bleak.exc import BleakCharacteristicNotFoundError

from mira_ble import bluez, protocol
from mira_ble import transport as transport_module
from mira_ble.const import NOTIFY_CHARACTERISTIC_UUID, WRITE_CHARACTERISTIC_UUID
from mira_ble.exceptions import (
    CommandRejected,
    ConnectionLost,
    MiraConnectionError,
    PairingRequired,
    RequestTimeout,
)
from mira_ble.frame import Frame
from mira_ble.protocol import Opcode
from mira_ble.testing import NAME, PRESET_SHOWER, STATE_RUNNING, STOP_FRAME
from mira_ble.transport import POLICY, BleTransport

STATE_REQUEST = protocol.read_state_request()
STATE_REPLY = Frame.reply(STATE_RUNNING).encode()
ACK = Frame.reply(b"\x01").encode()
REJECTED = Frame.reply(b"\x80").encode()
SOURCE = "00:11:22:33:44:55"


class Client(FakeClient):
    """ble-bond's fake client, also noting which characteristics were used."""

    def __init__(self, replies: dict[bytes, bytes] | None = None, *, mtu_size: int = 23) -> None:
        super().__init__(replies, mtu_size=mtu_size)
        self.subscribed_to: str | None = None
        self.written_to: set[str] = set()
        self.cache_cleared = False

    async def start_notify(self, char: str, callback: Any) -> None:
        self.subscribed_to = char
        await super().start_notify(char, callback)

    async def write_gatt_char(self, char: str, data: bytes, response: bool | None = None) -> None:
        self.written_to.add(char)
        await super().write_gatt_char(char, data, response)

    async def clear_cache(self) -> None:
        self.cache_cleared = True


def _device() -> BLEDevice:
    return BLEDevice("AA:BB:CC:DD:EE:FF", "Mira 0051 Test", {"source": SOURCE})


@pytest.fixture(autouse=True)
def no_pauses() -> Iterator[None]:
    quick = dataclasses.replace(
        POLICY, settle_after_pair=0, settle_after_subscribe=0, subscribe_retry_delay=0
    )
    with (
        patch.object(transport_module, "POLICY", quick),
        patch.object(link_module, "RETRY_DELAY", 0),
    ):
        yield


@pytest.fixture
def client() -> Iterator[Client]:
    fake = Client({STATE_REQUEST.encode(): STATE_REPLY, STOP_FRAME: ACK})
    with patched_establish(fake):
        yield fake


def _failing_establish(*errors: BaseException) -> Any:
    """Connection attempts that fail in turn with ``errors``; the last one repeats."""
    queue = list(errors)
    calls: list[dict[str, Any]] = []

    async def establish(_cls: Any, _device: Any, _name: str, **kwargs: Any) -> Any:
        calls.append(kwargs)
        error = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(error, BaseException):
            raise error
        return error

    return patch.object(link_module, "establish_connection", establish), calls


# --- policy -----------------------------------------------------------------------------------


def test_the_valves_link_policy() -> None:
    """Each value is a measurement or a choice recorded in the transport module."""
    assert (
        LinkPolicy(
            settle_after_pair=0.5,
            settle_after_subscribe=0.25,
            subscribe_retry=SubscribeRetry.INSUFFICIENT_AUTHENTICATION,
            subscribe_retry_delay=1.0,
            insufficient_authentication_is_evidence=True,
            retry_unpaired=True,
            write_chunk=None,
            connection_params=(24, 60, 0, 2000),
            connect_timeout=15.0,
            request_timeout=10.0,
        )
        == POLICY
    )


async def test_the_link_uses_the_valves_two_characteristics_and_parameters(client: Client) -> None:
    async with BleTransport(_device()) as transport:
        await transport.request(STATE_REQUEST, accept=protocol.is_state_reply)
    assert client.subscribed_to == NOTIFY_CHARACTERISTIC_UUID
    assert client.written_to == {WRITE_CHARACTERISTIC_UUID}
    assert client.connection_params == (24, 60, 0, 2000)
    assert client.unsubscribed


async def test_a_new_bond_is_expected_only_when_asked() -> None:
    assert BleTransport(_device())._link._expect_new_bond is False
    assert BleTransport(_device(), expect_new_bond=True)._link._expect_new_bond is True


async def test_writes_are_sized_from_the_mtu() -> None:
    long_frame = Frame.request(Opcode.WRITE_PRESET, PRESET_SHOWER)  # 61 bytes of payload
    small = Client(mtu_size=23)
    small.auto_reply = False
    with patched_establish(small):
        transport = BleTransport(_device(), request_timeout=0.3)  # three 25 ms gaps fit
        await transport.connect()
        with pytest.raises(RequestTimeout):
            await transport.request(long_frame)
    assert [len(piece) for piece in small.writes] == [20, 20, 20, 7]
    large = Client(mtu_size=100)
    large.auto_reply = False
    with patched_establish(large):
        transport = BleTransport(_device(), request_timeout=0.01)
        await transport.connect()
        with pytest.raises(RequestTimeout):
            await transport.request(long_frame)
    assert [len(piece) for piece in large.writes] == [67]


# --- replies ----------------------------------------------------------------------------------


async def test_request_roundtrip(client: Client) -> None:
    transport = BleTransport(_device())
    await transport.connect()
    reply = await transport.request(STATE_REQUEST, accept=protocol.is_state_reply)
    assert reply.payload == STATE_RUNNING
    assert list(transport.last_frames) == [("tx", STATE_REQUEST.encode()), ("rx", STATE_REPLY)]
    assert transport.last_frames is transport._link.last_frames
    assert transport.unsolicited == 0


async def test_without_a_shape_the_first_valid_reply_answers(client: Client) -> None:
    transport = BleTransport(_device())
    await transport.connect()
    assert (await transport.request(STATE_REQUEST)).payload == STATE_RUNNING


async def test_a_stray_frame_of_the_wrong_shape_does_not_answer_the_request(
    client: Client,
) -> None:
    """One stray frame must not shift every later reply by one."""
    client.auto_reply = False
    transport = BleTransport(_device())
    await transport.connect()
    original = client.write_gatt_char

    async def write(char: str, data: bytes, response: bool | None = None) -> None:
        await original(char, data, response)
        client.notify(Frame.reply(NAME).encode())  # a late reply to something else
        client.notify(STATE_REPLY)

    client.write_gatt_char = write  # type: ignore[method-assign]
    reply = await transport.request(STATE_REQUEST, accept=protocol.is_state_reply)
    assert reply.payload == STATE_RUNNING
    assert transport.unsolicited == 1


async def test_a_frame_that_is_not_a_valve_reply_does_not_answer_the_request(
    client: Client,
) -> None:
    """A frame on the request channel, such as an echo, is not a reply."""
    client.auto_reply = False
    transport = BleTransport(_device(), request_timeout=0.02)
    await transport.connect()
    original = client.write_gatt_char

    async def write(char: str, data: bytes, response: bool | None = None) -> None:
        await original(char, data, response)
        client.notify(STATE_REQUEST.encode())

    client.write_gatt_char = write  # type: ignore[method-assign]
    with pytest.raises(RequestTimeout, match="did not answer the request"):
        await transport.request(STATE_REQUEST)
    assert transport.unsolicited == 1
    assert not transport.is_connected  # an unfinished request closes the link


async def test_a_rejection_raises_with_the_request_opcode(client: Client) -> None:
    client.replies[STOP_FRAME] = REJECTED
    transport = BleTransport(_device())
    await transport.connect()
    with pytest.raises(CommandRejected) as caught:
        await transport.request(protocol.stop_request(), accept=protocol.is_command_reply)
    assert caught.value.opcode == Opcode.SET_OUTLETS
    assert transport.is_connected  # answered: the link stays usable


async def test_a_rejection_answers_whatever_shape_was_expected(client: Client) -> None:
    client.replies[STATE_REQUEST.encode()] = REJECTED
    transport = BleTransport(_device())
    await transport.connect()
    with pytest.raises(CommandRejected):
        await transport.request(STATE_REQUEST, accept=protocol.is_state_reply)


# --- what the transport reports ---------------------------------------------------------------


async def test_the_source_and_the_age_of_the_link_are_reported(client: Client) -> None:
    transport = BleTransport(_device())
    assert transport.source is None
    assert transport.link_age is None
    await transport.connect()
    assert transport.source == SOURCE  # where the device was heard: plain bleak says no more
    age = transport.link_age
    assert age is not None
    assert 0 <= age < 5
    await transport.connect()  # already connected: the link is not made again
    assert transport.link_age is not None
    assert transport.link_age >= age
    await transport.disconnect()
    assert transport.link_age is None
    assert not transport.is_connected


# --- exceptions -------------------------------------------------------------------------------


async def test_a_refused_pairing_is_pairing_required_and_keeps_code_and_cause() -> None:
    establish, _ = _failing_establish(BleakError("Pairing failed due to error: 82"))
    with establish, pytest.raises(PairingRequired) as caught:
        await BleTransport(_device()).connect()
    assert caught.value.refused is True
    assert caught.value.error_code == 82
    assert isinstance(caught.value.__cause__, ble_bond.PairingRequired)


async def test_a_subscribe_refused_twice_is_weak_evidence_of_a_missing_bond() -> None:
    class Unencrypted(Client):
        async def start_notify(self, _char: str, _callback: Any) -> None:
            raise BleakError("GATT error: insufficient authentication")

    with patched_establish(Unencrypted()), pytest.raises(PairingRequired) as caught:
        await BleTransport(_device()).connect()
    assert caught.value.refused is False
    assert caught.value.error_code == 5


async def test_other_connection_failures_are_connection_errors_with_their_code() -> None:
    establish, calls = _failing_establish(BleakError("Pairing failed due to error: 13"))
    with establish, pytest.raises(MiraConnectionError) as caught:
        await BleTransport(_device()).connect()
    assert not isinstance(caught.value, PairingRequired)
    assert caught.value.error_code == 13
    assert len(calls) == 2  # transient: tried again


async def test_an_adapter_that_cannot_pair_is_tried_once_without_pairing() -> None:
    client = Client({STATE_REQUEST.encode(): STATE_REPLY})
    establish, calls = _failing_establish(NotImplementedError("pairing"), client)
    with establish:
        transport = BleTransport(_device())
        await transport.connect()
    assert [call["pair"] for call in calls] == [True, False]
    assert transport.is_connected


async def test_an_adapter_that_cannot_pair_or_connect_unpaired_is_a_connection_error() -> None:
    establish, _ = _failing_establish(BleakError("Pairing failed due to error: 129"))
    with establish, pytest.raises(MiraConnectionError) as caught:
        await BleTransport(_device(), max_attempts=1).connect()
    assert not isinstance(caught.value, PairingRequired)
    assert caught.value.error_code == 129


async def test_a_timeout_is_this_librarys_and_a_builtin_timeout(client: Client) -> None:
    client.auto_reply = False
    transport = BleTransport(_device(), request_timeout=0.01)
    await transport.connect()
    with pytest.raises(RequestTimeout, match="nothing received") as caught:
        await transport.request(STATE_REQUEST)
    assert isinstance(caught.value, TimeoutError)
    assert isinstance(caught.value.__cause__, ble_bond.RequestTimeout)
    assert not transport.is_connected


async def test_a_request_without_a_link_is_connection_lost() -> None:
    with pytest.raises(ConnectionLost):
        await BleTransport(_device()).request(STATE_REQUEST)


async def test_a_drop_during_a_request_is_connection_lost(client: Client) -> None:
    client.auto_reply = False
    transport = BleTransport(_device())
    await transport.connect()
    original = client.write_gatt_char

    async def write(char: str, data: bytes, response: bool | None = None) -> None:
        await original(char, data, response)
        client.drop()

    client.write_gatt_char = write  # type: ignore[method-assign]
    with pytest.raises(ConnectionLost):
        await transport.request(STATE_REQUEST)


async def test_a_failed_write_is_a_connection_error(client: Client) -> None:
    async def failing(*_args: Any, **_kwargs: Any) -> None:
        raise BleakError("write failed")

    transport = BleTransport(_device())
    await transport.connect()
    client.write_gatt_char = failing  # type: ignore[method-assign]
    with pytest.raises(MiraConnectionError, match="write failed"):
        await transport.request(STATE_REQUEST)
    assert not transport.is_connected


async def test_a_device_without_the_protocol_characteristic_says_so() -> None:
    class NotAValve(Client):
        async def start_notify(self, _char: str, _callback: Any) -> None:
            raise BleakCharacteristicNotFoundError("267f0003")

    fake = NotAValve()
    with patched_establish(fake), pytest.raises(MiraConnectionError, match="267f service"):
        await BleTransport(_device()).connect()
    assert fake.cache_cleared


# --- BlueZ ------------------------------------------------------------------------------------


def test_the_bluez_helpers_are_ble_bonds() -> None:
    assert bluez.bluez_pairing_agent is ble_bond.bluez_pairing_agent
    assert bluez.remove_bluez_bond is ble_bond.remove_bluez_bond
    assert bluez.bluez_device_path is ble_bond.bluez_device_path
