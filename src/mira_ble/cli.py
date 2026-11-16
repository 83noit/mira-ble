"""Command line: find a valve, read it, and run or stop it.

The reading commands (``scan``, ``info``, ``status``, ``watch``, ``outlets-config``,
``presets``) change nothing on the valve and are the right first test on new hardware. The
valve must already be bonded with this computer's Bluetooth adapter, or be in pairing mode
when a command connects.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import partial

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from mira_ble.advertisement import parse_local_name
from mira_ble.const import LEGACY_SERVICE_UUID, SERVICE_UUID
from mira_ble.exceptions import MiraError, NotConfirmed
from mira_ble.protocol import Outlet, Preset, State
from mira_ble.transport import BleTransport
from mira_ble.valve import DEFAULT_MAX_TEMPERATURE, MiraValve

DESCRIPTION = "Find a Mira valve, read it, and run or stop it."
SCAN_SECONDS = 8.0
WATCH_SECONDS = 2.0
EXIT_INTERRUPTED = 130


def _state_line(state: State) -> str:
    if state.outlets:
        what = "outlet " + "+".join(str(n) for n in state.outlets.numbers)
    elif state.running:
        what = "running"
    elif state.held:
        what = "held (paused)"
    else:
        what = "idle"
    water = "-" if state.water_temperature is None else f"{state.water_temperature:.1f} C"
    parts = [f"{what:<14}", f"water {water}"]
    if state.target_temperature is not None:
        parts.append(f"target {state.target_temperature:.1f} C")
    elif state.running:
        parts.append("target cold")
    parts.append(f"flow {state.flow:g} of {state.flow_setting:g} L/min")
    if state.volume:
        parts.append(f"{state.volume} L used")
    if state.fault:
        parts.append(f"FAULT (code {state.error_code})")
    return "  ".join(parts)


def _span(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _preset_line(preset: Preset) -> str:
    outlets = "+".join(str(n) for n in preset.outlets.numbers)
    span = f"{preset.volume} litres" if preset.volume else _span(preset.duration or 0)
    return (
        f"{preset.slot:>2}  {preset.name:<31}  {preset.temperature:.1f} C  "
        f"outlet {outlets}  {span}  {preset.flow:g} L/min"
    )


async def _scan(_args: argparse.Namespace) -> int:
    found = await BleakScanner.discover(timeout=SCAN_SECONDS, return_adv=True)
    seen = 0
    for device, advertisement in found.values():
        name = advertisement.local_name or device.name
        if LEGACY_SERVICE_UUID in advertisement.service_uuids:
            print(
                f"{device.address}  {name}  {advertisement.rssi} dBm  (older protocol, unsupported)"
            )
            seen += 1
        elif parse_local_name(name) is not None:
            service = (
                "" if SERVICE_UUID in advertisement.service_uuids else "  (service not advertised)"
            )
            print(f"{device.address}  {name}  {advertisement.rssi} dBm{service}")
            seen += 1
    if not seen:
        print("No Mira valve found.", file=sys.stderr)
        return 1
    return 0


async def _find(address: str) -> BLEDevice:
    device = await BleakScanner.find_device_by_address(address, timeout=SCAN_SECONDS)
    if device is None:
        raise MiraError(
            f"{address} was not seen; is it powered, in range, and not held by the Mira app?"
        )
    return device


async def _with_valve(
    args: argparse.Namespace, action: Callable[[MiraValve], Awaitable[None]]
) -> int:
    transport = BleTransport(await _find(args.address), expect_new_bond=True)
    valve = MiraValve(transport, max_temperature=args.max_temperature)
    try:
        await transport.connect()
        await action(valve)
    finally:
        await transport.disconnect()
    return 0


async def _info(args: argparse.Namespace) -> int:
    async def action(valve: MiraValve) -> None:
        errors: dict[str, str] = {}
        identity = await valve.read_identity(errors)
        print("Protocol:      supported (connected, paired and subscribed)")
        print(f"Name:          {identity.name or '-'}")
        print(f"Serial number: {identity.serial_number or '-'}")
        print(f"Manufactured:  {identity.manufactured or '-'}")
        for key, reason in errors.items():
            print(f"{key}: {reason}", file=sys.stderr)

    return await _with_valve(args, action)


async def _status(args: argparse.Namespace) -> int:
    async def action(valve: MiraValve) -> None:
        state = await valve.read_state()
        print(_state_line(state))
        if args.raw:
            print(state.raw.hex(" "))

    return await _with_valve(args, action)


async def _watch(args: argparse.Namespace) -> int:
    async def action(valve: MiraValve) -> None:
        previous: str | None = None
        loop = asyncio.get_running_loop()
        end = None if args.duration is None else loop.time() + args.duration
        while end is None or loop.time() < end:
            state = await valve.read_state()
            line = _state_line(state)
            if args.raw:
                line += "  | " + state.raw.hex(" ")
            if line != previous:
                print(f"{datetime.now().astimezone():%H:%M:%S}  {line}", flush=True)
                previous = line
            await asyncio.sleep(args.interval)

    return await _with_valve(args, action)


async def _outlets_config(args: argparse.Namespace) -> int:
    async def action(valve: MiraValve) -> None:
        capabilities = await valve.probe()
        for outlet in capabilities.outlets:
            print(
                f"Outlet {outlet.number} ({outlet.kind.name.lower()}, type {outlet.outlet_type}): "
                f"{outlet.min_temperature:g}-{outlet.max_temperature:g} C "
                f"(default {outlet.default_temperature:g}), "
                f"{outlet.min_flow:g}-{outlet.max_flow:g} L/min (default {outlet.default_flow:g}), "
                f"stops after {_span(outlet.max_duration)}"
            )
        for key, reason in capabilities.errors.items():
            print(f"{key}: {reason}", file=sys.stderr)
        if not capabilities.outlets:
            raise MiraError("the valve reported no outlet configuration")

    return await _with_valve(args, action)


async def _presets(args: argparse.Namespace) -> int:
    async def action(valve: MiraValve) -> None:
        errors: dict[str, str] = {}
        presets = await valve.read_presets(errors=errors)
        for preset in presets:
            print(_preset_line(preset))
        for key, reason in errors.items():
            print(f"{key}: {reason}", file=sys.stderr)
        if not presets:
            raise MiraError(
                "no preset could be read"
                + (" (the valve did not answer; some only answer while running)" if errors else "")
            )

    return await _with_valve(args, action)


async def _stop_after_failure(valve: MiraValve, err: Exception) -> None:
    """Try to stop a valve whose start could not be confirmed; water may be running."""
    print(f"{type(err).__name__}: {err}", file=sys.stderr)
    print("The start was not confirmed; stopping the valve as a precaution.", file=sys.stderr)
    with contextlib.suppress(MiraError, BleakError):
        await valve.transport.connect()
        state = await valve.stop()
        if state is not None:
            print(_state_line(state))
            return
    print("Could not confirm the stop. CHECK THE SHOWER and use the wall control.", file=sys.stderr)


async def _start(args: argparse.Namespace) -> int:
    async def action(valve: MiraValve) -> None:
        before = await valve.read_state()
        try:
            state = await valve.run_preset(args.slot, before=before)
        except (NotConfirmed, MiraError, BleakError) as err:
            await _stop_after_failure(valve, err)
            raise MiraError("preset start failed") from err
        assert state is not None
        print(_state_line(state))

    return await _with_valve(args, action)


async def _run(args: argparse.Namespace) -> int:
    outlets = Outlet.NONE
    for number in args.outlet:
        outlets |= Outlet.from_number(number)

    async def action(valve: MiraValve) -> None:
        # Apply each outlet's own maximum when the valve will tell it.
        limit: float | None = None
        errors: dict[str, str] = {}
        for number in outlets.numbers:
            config = await valve.optional(
                f"outlet_{number}", partial(valve.read_outlet_config, number), errors
            )
            if config is not None and config.max_temperature > 0:
                limit = (
                    config.max_temperature if limit is None else min(limit, config.max_temperature)
                )
        if limit is None:
            print(
                "Outlet limits could not be read; only --max-temperature applies.", file=sys.stderr
            )
        valve.check_temperature(args.temperature, limit)
        try:
            state = await valve.set_outlets(
                outlets, temperature=args.temperature, flow=args.flow, max_temperature=limit
            )
        except (NotConfirmed, MiraError, BleakError) as err:
            await _stop_after_failure(valve, err)
            raise MiraError("start failed") from err
        assert state is not None
        print(_state_line(state))

    return await _with_valve(args, action)


async def _stop(args: argparse.Namespace) -> int:
    async def action(valve: MiraValve) -> None:
        state = await valve.stop()
        assert state is not None
        print(_state_line(state))

    return await _with_valve(args, action)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(prog="mira-ble", description=DESCRIPTION)
    parser.add_argument("-v", "--verbose", action="store_true", help="log every frame")
    parser.add_argument(
        "--max-temperature",
        type=float,
        default=DEFAULT_MAX_TEMPERATURE,
        help="hottest target this run may send (default %(default)s C); give it before the command",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def add(
        name: str, handler: Callable[[argparse.Namespace], Awaitable[int]], text: str
    ) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=text)
        sub.set_defaults(handler=handler)
        if handler is not _scan:
            sub.add_argument("address", help="Bluetooth address (a UUID on macOS)")
        return sub

    add("scan", _scan, "find nearby valves")
    add("info", _info, "read the name, serial number and date of manufacture")
    add("status", _status, "read what the valve is doing").add_argument(
        "--raw", action="store_true", help="also print the undecoded reply"
    )
    watch = add("watch", _watch, "follow the valve's state until interrupted (read-only)")
    watch.add_argument(
        "--interval", type=float, default=WATCH_SECONDS, help="seconds between reads"
    )
    watch.add_argument("--raw", action="store_true", help="also print the undecoded reply")
    watch.add_argument("--duration", type=float, default=None, help="stop after this many seconds")
    add("outlets-config", _outlets_config, "read each outlet's limits and defaults")
    add("presets", _presets, "list the stored presets")
    add("start", _start, "run a stored preset").add_argument("slot", type=int)
    run = add("run", _run, "run outlets at a temperature and flow")
    run.add_argument("--outlet", type=int, action="append", required=True, choices=(1, 2, 3))
    run.add_argument("--temperature", type=float, required=True, help="degrees Celsius")
    run.add_argument("--flow", type=float, required=True, help="litres per minute")
    add("stop", _stop, "close every outlet")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command line and return its exit status."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    if args.verbose:
        # Only this package: bleak's own debug output buries the frames.
        logging.getLogger("mira_ble").setLevel(logging.DEBUG)
    try:
        return int(asyncio.run(args.handler(args)))
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except (MiraError, BleakError) as err:
        print(f"{type(err).__name__}: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
