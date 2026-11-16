"""The README's library example runs as written, against the simulated valve."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bleak.backends.device import BLEDevice

import mira_ble
from mira_ble.protocol import Opcode
from mira_ble.testing import FakeValve

README = Path(__file__).parent.parent / "README.md"


def _example() -> str:
    blocks = re.findall(r"```python\n(.*?)```", README.read_text(encoding="utf-8"), re.DOTALL)
    assert len(blocks) == 1
    return blocks[0]


def test_the_library_example_runs_and_stops_the_valve(capsys) -> None:
    fake = FakeValve()
    device = BLEDevice("AA:BB:CC:DD:EE:FF", "Mira 0051 Test", {})
    with (
        patch.object(mira_ble, "BleTransport", lambda _device, **_kwargs: fake),
        patch("bleak.BleakScanner.find_device_by_address", AsyncMock(return_value=device)),
        patch("asyncio.sleep", AsyncMock()),
        patch("mira_ble.valve.asyncio.sleep", AsyncMock()),
    ):
        exec(compile(_example(), str(README), "exec"), {"__name__": "__main__"})
    commands = [f.encode() for f in fake.sent if f.opcode == Opcode.SET_OUTLETS]
    assert commands[0].hex(" ") == "aa 55 00 ab 04 01 7c 28 01 ac"  # 38.0 C, 10 L/min, outlet 1
    assert commands[-1].hex(" ") == "aa 55 00 ab 04 01 7c 28 40 6d"  # the same, paused
    assert not fake.running
    assert not fake.is_connected
    assert capsys.readouterr().out.strip() == "25.6 10.0"


def test_every_name_the_readme_imports_is_exported() -> None:
    for names in re.findall(r"from mira_ble import (.+)", README.read_text(encoding="utf-8")):
        for name in names.split(","):
            assert name.strip() in mira_ble.__all__
