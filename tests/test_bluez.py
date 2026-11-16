"""The BlueZ pairing agent against a fake bleak BlueZ manager and D-Bus connection."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from bleak import BleakError
from bleak.backends.device import BLEDevice
from dbus_fast import Message, MessageType
from dbus_fast.errors import DBusError

from mira_ble import bluez
from mira_ble import transport as transport_module
from mira_ble.exceptions import PairingRequired
from mira_ble.transport import BleTransport

PATH = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"


class FakeBus:
    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.exported: dict[str, Any] = {}
        self.calls: list[str] = []
        self.messages: list[Message] = []

    def export(self, path: str, interface: Any) -> None:
        self.exported[path] = interface

    def unexport(self, path: str) -> None:
        del self.exported[path]

    async def call(self, message: Message) -> Any:
        self.calls.append(message.member)
        self.messages.append(message)
        if message.member == self.fail:
            return SimpleNamespace(
                message_type=MessageType.ERROR, error_name="org.bluez.Error.Failed", body=[]
            )
        return SimpleNamespace(message_type=MessageType.METHOD_RETURN)


class FakeManager:
    def __init__(self, bus: FakeBus | None, paired: bool = False) -> None:
        self._bus = bus
        self.paired = paired

    def is_paired(self, _path: str) -> bool:
        return self.paired


def _bluez_device() -> BLEDevice:
    return BLEDevice("AA:BB:CC:DD:EE:FF", "Test valve", {"path": PATH, "props": {}})


def _manager(manager: FakeManager):
    async def get() -> FakeManager:
        return manager

    return patch("bleak.backends.bluezdbus.manager.get_global_bluez_manager", get)


async def test_agent_is_registered_while_pairing() -> None:
    bus = FakeBus()
    with _manager(FakeManager(bus)):
        async with bluez.bluez_pairing_agent(_bluez_device()):
            assert bus.calls == ["RegisterAgent", "RequestDefaultAgent"]
            ((agent_path, agent),) = bus.exported.items()
            assert agent_path == f"{bluez.AGENT_PATH_PREFIX}/{PATH.rsplit('/', 1)[-1]}"
        assert bus.calls[-1] == "UnregisterAgent"
        assert not bus.exported

    other = "/org/bluez/hci0/dev_11_22_33_44_55_66"
    agent.RequestConfirmation(PATH, 0)
    agent.RequestAuthorization(PATH)
    agent.AuthorizeService(PATH, "0000fe5d-0000-1000-8000-00805f9b34fb")
    with pytest.raises(DBusError):
        agent.RequestConfirmation(other, 0)
    with pytest.raises(DBusError):
        agent.RequestAuthorization(other)
    with pytest.raises(DBusError):
        agent.AuthorizeService(other, "0000fe5d-0000-1000-8000-00805f9b34fb")
    agent.Release()
    agent.Cancel()


@pytest.mark.parametrize(
    ("device", "manager"),
    [
        (BLEDevice("AA:BB:CC:DD:EE:FF", "proxy", {"source": "esphome"}), FakeManager(FakeBus())),
        (_bluez_device(), FakeManager(FakeBus(), paired=True)),
        (_bluez_device(), FakeManager(None)),
    ],
    ids=["proxy", "already-bonded", "no-bus"],
)
async def test_agent_is_skipped(device: BLEDevice, manager: FakeManager) -> None:
    with _manager(manager):
        async with bluez.bluez_pairing_agent(device):
            pass
    assert manager._bus is None or not manager._bus.calls


async def test_agents_are_held_one_at_a_time() -> None:
    """BlueZ has one default agent for the whole system."""
    bus = FakeBus()
    other = BLEDevice(
        "11:22:33:44:55:66", "Other valve", {"path": "/org/bluez/hci0/dev_11_22_33_44_55_66"}
    )
    order: list[str] = []

    async def hold(device: BLEDevice, name: str) -> None:
        async with bluez.bluez_pairing_agent(device):
            order.append(f"{name} in")
            await asyncio.sleep(0)
            order.append(f"{name} out")

    with _manager(FakeManager(bus)):
        await asyncio.gather(hold(_bluez_device(), "a"), hold(other, "b"))
    assert order == ["a in", "a out", "b in", "b out"]
    assert not bus.exported


def test_the_agent_lock_is_per_event_loop() -> None:
    """An asyncio.Lock breaks once it has been contended under a second loop."""

    async def lock() -> asyncio.Lock:
        return bluez._agent_lock()

    first, second = asyncio.new_event_loop(), asyncio.new_event_loop()
    try:
        one = first.run_until_complete(lock())
        assert first.run_until_complete(lock()) is one
        assert second.run_until_complete(lock()) is not one
    finally:
        first.close()
        second.close()


async def test_no_bluez_still_pairs() -> None:
    async def unavailable() -> None:
        raise BleakError("no system bus")

    with patch("bleak.backends.bluezdbus.manager.get_global_bluez_manager", unavailable):
        async with bluez.bluez_pairing_agent(_bluez_device()):
            pass


async def test_refused_registration_still_pairs_and_cleans_up() -> None:
    bus = FakeBus(fail="RequestDefaultAgent")
    with _manager(FakeManager(bus)):
        async with bluez.bluez_pairing_agent(_bluez_device()):
            pass
    assert bus.calls == ["RegisterAgent", "RequestDefaultAgent", "UnregisterAgent"]
    assert not bus.exported


async def test_transport_holds_the_agent_and_reports_a_refusal() -> None:
    bus = FakeBus()
    seen: list[str] = []

    async def establish(*_args: Any, **_kwargs: Any) -> Any:
        seen.extend(bus.calls)
        raise BleakError("[org.bluez.Error.AuthenticationFailed] Authentication Failed")

    with (
        _manager(FakeManager(bus)),
        patch.object(transport_module, "establish_connection", establish),
        pytest.raises(PairingRequired),
    ):
        await BleTransport(_bluez_device(), max_attempts=1, expect_new_bond=True).connect()
    assert seen == ["RegisterAgent", "RequestDefaultAgent"]
    assert bus.calls[-1] == "UnregisterAgent"


async def test_an_ordinary_connection_never_takes_over_pairing_for_the_system() -> None:
    """Only a pairing flow registers the agent; a poll must not become the default agent."""
    bus = FakeBus()

    async def establish(*_args: Any, **_kwargs: Any) -> Any:
        raise BleakError("[org.bluez.Error.AuthenticationFailed] Authentication Failed")

    with (
        _manager(FakeManager(bus)),
        patch.object(transport_module, "establish_connection", establish),
        pytest.raises(PairingRequired) as caught,
    ):
        await BleTransport(_bluez_device(), max_attempts=1).connect()
    assert bus.calls == []
    assert caught.value.refused


async def test_bond_is_removed_from_the_adapter() -> None:
    bus = FakeBus()
    with _manager(FakeManager(bus, paired=True)):
        assert await bluez.remove_bluez_bond(_bluez_device())
    (message,) = bus.messages
    assert (message.path, message.interface, message.member, message.body) == (
        "/org/bluez/hci0",
        bluez.ADAPTER_INTERFACE,
        "RemoveDevice",
        [PATH],
    )


@pytest.mark.parametrize(
    ("device", "manager"),
    [
        (BLEDevice("AA:BB:CC:DD:EE:FF", "proxy", {"source": "esphome"}), FakeManager(FakeBus())),
        (_bluez_device(), FakeManager(FakeBus(), paired=False)),
        (_bluez_device(), FakeManager(FakeBus(fail="RemoveDevice"), paired=True)),
        (_bluez_device(), FakeManager(None, paired=True)),
    ],
    ids=["proxy", "no-bond", "refused", "no-bus"],
)
async def test_bond_removal_skipped(device: BLEDevice, manager: FakeManager) -> None:
    with _manager(manager):
        assert not await bluez.remove_bluez_bond(device)
