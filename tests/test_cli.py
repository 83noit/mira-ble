"""The command line, against the simulated valve."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from bleak import BleakError
from bleak.backends.device import BLEDevice

from mira_ble import cli, protocol
from mira_ble.const import LEGACY_SERVICE_UUID, SERVICE_UUID
from mira_ble.protocol import Opcode
from mira_ble.testing import STOP_FRAME, FakeValve

ADDRESS = "AA:BB:CC:DD:EE:FF"


@pytest.fixture
def fake():
    fake = FakeValve()
    device = BLEDevice(ADDRESS, "Mira 0051 Test", {})
    with (
        patch.object(cli, "BleTransport", lambda _device, **_kwargs: fake),
        patch.object(cli.BleakScanner, "find_device_by_address", AsyncMock(return_value=device)),
    ):
        yield fake


def test_status(fake: FakeValve, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["status", ADDRESS, "--raw"]) == 0
    out = capsys.readouterr().out
    assert "idle" in out
    assert "water 25.6 C" in out
    assert "21 20 00" in out
    assert not fake.is_connected


def test_info(fake: FakeValve, capsys: pytest.CaptureFixture[str]) -> None:
    fake.reject.add(Opcode.READ_NAME)
    assert cli.main(["info", ADDRESS]) == 0
    captured = capsys.readouterr()
    assert "Protocol:      supported" in captured.out
    assert "3140090125121900" in captured.out
    assert "2025-01-09" in captured.out
    assert "name: CommandRejected" in captured.err


def test_outlets_config(fake: FakeValve, capsys: pytest.CaptureFixture[str]) -> None:
    fake.reject.add(Opcode.READ_VALVE_CONFIG)
    assert cli.main(["outlets-config", ADDRESS]) == 0
    captured = capsys.readouterr()
    assert (
        "Outlet 1 (hand_shower, type 1): 30-45 C (default 38), 4-16 L/min (default 12)"
        in captured.out
    )
    assert "stops after 30:00" in captured.out
    assert "outlet_count" in captured.err
    fake.reject.add(Opcode.READ_OUTLET_CONFIG)
    assert cli.main(["outlets-config", ADDRESS]) == 1
    assert "no outlet configuration" in capsys.readouterr().err


def test_presets(fake: FakeValve, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["presets", ADDRESS]) == 0
    out = capsys.readouterr().out
    assert "Default Bathfill" in out
    assert "50 litres" in out
    assert "30:00" in out


def test_presets_says_so_when_none_can_be_read(
    fake: FakeValve, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silence must not look like "this valve has no presets"."""
    fake.ignore.add(Opcode.READ_PRESET)
    assert cli.main(["presets", ADDRESS]) == 1
    err = capsys.readouterr().err
    assert "preset_1: no answer" in err
    assert "some only answer while running" in err
    fake.ignore.clear()
    fake.presets.clear()
    assert cli.main(["presets", ADDRESS]) == 1
    assert "no preset could be read" in capsys.readouterr().err


def test_durations_are_shown_to_the_second() -> None:
    assert cli._span(45) == "0:45"
    assert cli._span(90) == "1:30"


def test_run_then_stop(fake: FakeValve, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        cli.main(
            [
                "run",
                ADDRESS,
                "--outlet",
                "1",
                "--outlet",
                "2",
                "--temperature",
                "39",
                "--flow",
                "10",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "outlet 1+2" in out
    assert "target 39.0 C" in out
    assert "flow 10 of 10 L/min" in out
    assert cli.main(["stop", ADDRESS]) == 0
    assert "idle" in capsys.readouterr().out
    assert fake.outlets == 0


def test_run_applies_the_outlets_own_maximum(
    fake: FakeValve, capsys: pytest.CaptureFixture[str]
) -> None:
    """Outlet 1 allows 45 C; a higher --max-temperature must not get past that."""
    assert (
        cli.main(
            [
                "--max-temperature",
                "47",
                "run",
                ADDRESS,
                "--outlet",
                "1",
                "--temperature",
                "46",
                "--flow",
                "10",
            ]
        )
        == 1
    )
    assert "above the limit of 45" in capsys.readouterr().err
    assert Opcode.SET_OUTLETS not in [frame.opcode for frame in fake.sent]
    assert (
        cli.main(
            [
                "--max-temperature",
                "47",
                "run",
                ADDRESS,
                "--outlet",
                "2",
                "--temperature",
                "46",
                "--flow",
                "10",
            ]
        )
        == 0
    )  # outlet 2 allows 46
    assert fake.target_tenths == 460


def test_run_without_readable_limits_falls_back_to_the_flag(
    fake: FakeValve, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.ignore.add(Opcode.READ_OUTLET_CONFIG)
    assert cli.main(["run", ADDRESS, "--outlet", "1", "--temperature", "46", "--flow", "10"]) == 1
    captured = capsys.readouterr()
    assert "Outlet limits could not be read" in captured.err
    assert "UnsafeValue" in captured.err
    assert cli.main(["run", ADDRESS, "--outlet", "1", "--temperature", "44", "--flow", "10"]) == 0
    assert fake.target_tenths == 440


def test_a_start_that_cannot_be_confirmed_is_followed_by_a_stop(
    fake: FakeValve, capsys: pytest.CaptureFixture[str]
) -> None:
    """The valve may be running; the tool must not just exit."""
    fake.lag = 1000  # the start never shows within the window
    assert cli.main(["run", ADDRESS, "--outlet", "1", "--temperature", "38", "--flow", "10"]) == 1
    err = capsys.readouterr().err
    assert "NotConfirmed" in err
    assert "stopping the valve as a precaution" in err
    assert STOP_FRAME in [frame.encode() for frame in fake.sent]


def test_a_failed_precautionary_stop_tells_the_user_to_go_and_look(
    fake: FakeValve, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.ignore_commands = True
    fake.outlets = 1  # as if the start had worked but nothing responds any more
    assert cli.main(["start", ADDRESS, "9"]) == 1  # no such preset: rejected
    assert "CHECK THE SHOWER" in capsys.readouterr().err


def test_start_a_preset(fake: FakeValve, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["start", ADDRESS, "2"]) == 0
    assert "outlet 1" in capsys.readouterr().out


def test_a_platinum_preset_is_shown_as_running(
    fake: FakeValve, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.platinum = True
    assert cli.main(["start", ADDRESS, "1"]) == 0
    assert "running (held)" in capsys.readouterr().out


def test_cold_and_fault_states_are_shown() -> None:
    fake = FakeValve(outlets=1, target_tenths=0, flow_byte=32)
    line = cli._state_line(protocol.decode_state(fake.state_payload()))
    assert "outlet 1" in line
    assert "target cold" in line
    payload = bytearray(fake.state_payload())
    payload[17] = 50
    payload[14:16] = (9000).to_bytes(2, "big")
    line = cli._state_line(protocol.decode_state(bytes(payload)))
    assert "FAULT (code 50)" in line
    assert "water -" in line


def test_watch_prints_changes_until_interrupted(
    fake: FakeValve, capsys: pytest.CaptureFixture[str]
) -> None:
    reads = 0
    real_sleep = asyncio.sleep

    async def sleep(_delay: float) -> None:
        nonlocal reads
        reads += 1
        if reads == 2:
            fake.outlets, fake.target_tenths = 1, 380
        if reads == 4:
            raise KeyboardInterrupt
        await real_sleep(0)

    with patch.object(cli.asyncio, "sleep", sleep):
        assert cli.main(["watch", ADDRESS, "--interval", "0", "--raw"]) == cli.EXIT_INTERRUPTED
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2  # idle, then running: unchanged readings are not repeated
    assert "idle" in lines[0]
    assert "outlet 1" in lines[1]
    assert "| 21 20" in lines[1]
    assert Opcode.SET_OUTLETS not in [frame.opcode for frame in fake.sent]  # read-only


def test_verbose_logs_only_this_package(fake: FakeValve) -> None:
    """bleak's debug output buries the frames."""
    logging.getLogger("mira_ble").setLevel(logging.NOTSET)
    assert cli.main(["-v", "status", ADDRESS]) == 0
    assert logging.getLogger("mira_ble").level == logging.DEBUG
    assert logging.getLogger("bleak").getEffectiveLevel() > logging.DEBUG
    logging.getLogger("mira_ble").setLevel(logging.NOTSET)


def test_a_bluetooth_error_is_a_message_not_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch.object(
        cli.BleakScanner, "find_device_by_address", AsyncMock(side_effect=BleakError("adapter off"))
    ):
        assert cli.main(["status", ADDRESS]) == 1
    assert "BleakError: adapter off" in capsys.readouterr().err


def test_a_valve_out_of_range(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(cli.BleakScanner, "find_device_by_address", AsyncMock(return_value=None)):
        assert cli.main(["status", ADDRESS]) == 1
    assert "was not seen" in capsys.readouterr().err


def test_a_silent_valve_and_a_rejected_command(
    fake: FakeValve, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.silent = True
    assert cli.main(["status", ADDRESS]) == 1
    assert "RequestTimeout" in capsys.readouterr().err
    fake.silent = False
    fake.reject.add(Opcode.SET_OUTLETS)
    assert cli.main(["stop", ADDRESS]) == 1
    assert "CommandRejected" in capsys.readouterr().err


def test_a_command_and_an_address_are_required() -> None:
    with pytest.raises(SystemExit):
        cli.main([])
    with pytest.raises(SystemExit):
        cli.main(["status"])


def test_scan(capsys: pytest.CaptureFixture[str]) -> None:
    def found(name: str | None, uuids: list[str]) -> tuple[Any, Any]:
        return (
            BLEDevice(ADDRESS, name, {}),
            SimpleNamespace(local_name=name, service_uuids=uuids, rssi=-60),
        )

    devices = {
        "a": found("Mira 0051#0123456789012345", [SERVICE_UUID]),
        "b": found("Mira 004C Bathroom", []),
        "c": found(None, [SERVICE_UUID]),  # shares the service UUID but is not a Mira valve
        "d": found("Toothbrush", []),
        "e": found("Mira Mode", [LEGACY_SERVICE_UUID]),
    }
    with patch.object(cli.BleakScanner, "discover", AsyncMock(return_value=devices)):
        assert cli.main(["scan"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert lines[0].endswith("-60 dBm")
    assert "service not advertised" in lines[1]
    assert "older protocol, unsupported" in lines[2]
    with patch.object(cli.BleakScanner, "discover", AsyncMock(return_value={})):
        assert cli.main(["scan"]) == 1
    assert "No Mira valve" in capsys.readouterr().err
