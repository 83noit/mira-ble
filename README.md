# mira-ble

[![CI](https://github.com/83noit/mira-ble/actions/workflows/ci.yml/badge.svg)](https://github.com/83noit/mira-ble/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mira-ble)](https://pypi.org/project/mira-ble/)
[![Python](https://img.shields.io/pypi/pyversions/mira-ble)](https://pypi.org/project/mira-ble/)
[![License: MIT](https://img.shields.io/github/license/83noit/mira-ble)](https://github.com/83noit/mira-ble/blob/main/LICENSE)

An async Python library for talking to a **Mira Platinum** digital shower over Bluetooth LE.
It reads what the valve is doing, its outlet limits, its presets and its last shower, and it
can start and stop outlets and presets. Every command is checked by reading the valve back.

Mira Activate and current Mira Mode valves speak the same language and should work too, but
this library hasn't been run on one yet (see [Supported hardware](#supported-hardware)).

It's the library behind the Home Assistant integration
[ha-mira-ble](https://github.com/83noit/ha-mira-ble), but it doesn't need Home Assistant.

> **Status: early days.** Pairing, every read, starting a preset, running an outlet and both
> ways of stopping have been run on one Mira Platinum, a handful of times, with someone
> watching: from a Mac, and through an ESPHome proxy under Home Assistant. Nothing has run on
> a local Linux adapter or on another model yet; those paths are only tested against a
> simulated valve and against replies recorded by the projects credited below. Start with the
> read-only commands and watch how it behaves.

> **This turns real hot water on and off.** Never leave it running unattended. If the
> connection drops or your program exits, the library can't stop the water: at that point the
> valve's own maximum run time is the only thing that will.

This project has nothing to do with Mira Showers or Kohler. Mira and Kohler are their
trademarks.

## Supported hardware

Valves whose Bluetooth name looks like `Mira 0051#…` or `Mira 004C Bathroom`, and which offer
the GATT service `267f0001-eb15-43f5-94c3-67d2221188f7`. Older Mira Mode valves offer
`bccb0001-ca66-11e5-88a4-0002a5d5c51b` instead and speak something else entirely; they aren't
supported.

| Model | Status |
| --- | --- |
| Mira Platinum | Run on one valve from a Mac and through an ESPHome proxy: pairing, every read, starting a preset, the outlet command, and both ways of stopping. A preset warms the water up, then waits until the outlet button or an outlet command starts the shower. (The Mira app itself controls the Platinum through the cloud, not Bluetooth.) |
| Mira Activate, current Mira Mode | **Should work, not tried yet.** Other projects worked out the protocol on real valves, and this library is tested against their recordings, but nobody has run it on one, so you'd be the first. If you'd like to help, please [open an issue](https://github.com/83noit/mira-ble/issues) before you start, and begin with the read-only commands. If you'd rather have something already in daily use on your valve, [Mira-Activate-HA](https://github.com/jonwilliams84/Mira-Activate-HA) (Activate) and [ha-mira-mode](https://github.com/HaydenGriffin/ha-mira-mode) / [mira-mode-control](https://github.com/ryan-shaw/mira-mode-control) (Mode) are built specifically for them |

## Install

```sh
pip install mira-ble      # or: uv add mira-ble
```

Python 3.13 or newer.

## Pairing

The valve only talks over a bonded (paired and encrypted) connection, and only accepts a new
bond while it's in pairing mode. On a controller with a button, hold it for five seconds
until it flashes; on a dial controller, go Menu → Settings → Connect. Turning the valve's
power off and on is said to leave it pairable for a little while too. Pairing is "Just Works"
(no code to type).

The library pairs on every connection; once a bond exists, that's just a quick re-encrypt.
The bond lives on whichever adapter (or ESPHome proxy) made it. A Mira Platinum was seen
keeping at least two bonds at once; the Mira app says a valve keeps up to ten.

If the valve is reset, or the device is removed in the app, the adapter keeps offering a key
the valve no longer accepts. Make the adapter forget the valve and pair again
(`remove_bluez_bond()` does that on Linux).

**An ESPHome proxy can lose the bond, in two ways:**

- *A connection that drops while it's being encrypted wipes the bond from the proxy.* That's
  how ESP-IDF's Bluetooth code works, on every version. It happened once in about 150
  connections on a weak signal (-76 dBm), and the valve then had to be paired again. A strong
  signal makes it rare. From ESP-IDF 5.5.6 the proxy can be built with
  `CONFIG_BT_BLE_SMP_REMOVE_BOND_ON_PAIR_FAIL_AS_CENTRAL: "n"` to keep the bond. It's running
  on this project's proxy, but no connection has dropped on it yet. With that setting the
  proxy never drops a key by itself, so after a valve reset pairing keeps failing until it
  does; the integration's [Known issues](https://github.com/83noit/ha-mira-ble#known-issues)
  lay out the options, including `CONFIG_BT_BLE_SMP_UNBOND_ON_KEY_MISSING` and its security
  catch.
- *ESPHome 2026.10 (ESP-IDF 5.5.6) forgets a bond after the first connection*, until the
  proxy restarts ([esphome#21880](https://github.com/esphome/esphome/issues/21880)). ESPHome
  2026.11 fixes it.

## Command line

```console
$ mira-ble scan
12345678-1234-5678-1234-56789ABCDEF0  Mira 0051#0123456789012345  -58 dBm

$ mira-ble status <address>
idle            water 21.4 C  flow 0 of 4 L/min

$ mira-ble watch <address>
19:18:26  idle            water 25.5 C  flow 0 of 4 L/min
19:18:29  outlet 1        water 25.5 C  target 40.0 C  flow 3 of 15 L/min

$ mira-ble outlets-config <address>
Outlet 1 (hand_shower, type 1): 30-45 C (default 38), 4-16 L/min (default 12), stops after 30:00

$ mira-ble presets <address>
 1  Everyday                         38.0 C  outlet 1  30:00  12 L/min

$ mira-ble run <address> --outlet 1 --temperature 38 --flow 10
outlet 1        water 22.0 C  target 38.0 C  flow 3 of 10 L/min

$ mira-ble stop <address>
idle            water 31.0 C  flow 0 of 4 L/min
```

The output above is made up. `scan`, `info`, `status`, `watch`, `outlets-config` and
`presets` don't change anything on the valve: **start with those**. Running `watch --raw`
while someone uses the shower at the wall is a good first step before sending anything.

- `run` first reads the outlets' own maximum temperature and uses the lower of that and
  `--max-temperature` (default 45 °C; put it before the command name).
- If a `run` or `start` can't be confirmed, the tool sends a stop before it exits and tells
  you. If it prints `CHECK THE SHOWER`, go and look.
- `start` sends the preset command once. On a Mira Platinum that warms the water up and then
  waits. `start --through` waits for that pause and sends the command again, which starts the
  preset for real, with its own timer (the water then runs for the whole preset).
- `presets` exits with an error, and says why, if it couldn't read any; some valves only
  answer preset reads while running.
- `-v` logs every message sent and received (this package only). They include the valve's
  name and serial number once those have been read.
- Addresses are MAC addresses on Linux and Windows, and UUIDs on macOS. Flow is in litres per
  minute; the Mira app shows the same number on its 0 to 25 scale.

## Library

```python
import asyncio

from bleak import BleakScanner

from mira_ble import BleTransport, MiraValve, Outlet


async def main() -> None:
    device = await BleakScanner.find_device_by_address("AA:BB:CC:DD:EE:FF")
    async with BleTransport(device, expect_new_bond=True) as transport:
        valve = MiraValve(transport, max_temperature=42.0)
        capabilities = await valve.probe()  # outlets and presets; tolerant of a quiet valve
        limit = capabilities.max_temperature(Outlet.FIRST)  # the outlet's own maximum
        state = await valve.set_outlets(
            Outlet.FIRST, temperature=38.0, flow=10.0, max_temperature=limit
        )
        print(state.water_temperature, state.flow)
        try:
            await asyncio.sleep(60)
        finally:
            await valve.stop()


asyncio.run(main())
```

Units are degrees Celsius, litres per minute, seconds and litres everywhere.

### What you can count on

- **Temperature limits are checked before anything is sent.** A target above
  `max_temperature` (default 45 °C), above a per-call limit, or above the hard ceiling of
  48 °C raises `UnsafeValue`, and so does a limit that isn't a number.
- **Commands are checked against the valve's state.** `set_outlets`, `stop` and `run_preset`
  wait for the valve to say yes, then keep reading its state until the change shows up, for up
  to 8 s (valves take a moment). `NotConfirmed` carries the last state read. The valve may
  still be getting there, so **if a start isn't confirmed, call `stop()`**.
- **"Stopped" means no water.** `State.running` is true when an outlet is open or the valve
  measures flow, and a stop only counts once neither is true on two reads in a row. The valve
  may stay *held* afterwards (`State.held`, a pause it ends by itself); `State.active` covers
  both. `stop()` first sends the same pause the Mira app uses (the valve's own temperature and
  flow, plus the hold bit), which leaves the valve's settings alone. If water is still running
  after that, it sends the all-zero stop, then "run preset 0".
- **One word for what the valve is doing.** `State.phase` is `rest`, `warming_up`, `paused`,
  `preset` or `running`. `State.preset` and `State.time_remaining` give the running preset and
  how many seconds it has left by the valve's count, and `State.volume` the litres used so far.
  `run_preset()` sends the preset command once; `run_preset_through()` also takes a preset
  past the warm-up pause seen on a Mira Platinum.
- **Commands describe the whole picture.** A command lists every outlet that should be on;
  anything not listed gets turned off. Which tap is outlet 1 depends on the plumbing:
  `outlets-config` and `presets` will tell you.
- **Replies are matched by their shape.** Replies don't say which question they answer, so
  each read only accepts a reply that looks right for it, and a request that doesn't finish
  (timeout, cancellation) closes the connection.
- **Keep connections short through a proxy.** Through an ESPHome proxy, requests on a fresh
  connection took 0.1 to 0.3 s, and once the connection had been open a few seconds they took
  7 to 10 s and often timed out. `BleTransport.link_age` tells you how old the connection is.
  From a Mac, a connection kept open for minutes stayed quick.
- **Some reads are optional.** `read_identity()` and `probe()` note in `errors` anything the
  valve refused or ignored, and carry on; only the state read has to work.
- **No settings changes.** The library doesn't include the commands that change outlet
  limits, controller settings, Wi-Fi details, installer settings or the serial number, or that
  reset the valve. Writing and deleting presets are the only lasting changes it can make; a
  write replaces the whole slot for good, so note down what's there first.

### Errors

Everything comes from `MiraError`. Connection problems are `MiraConnectionError`
(`PairingRequired`, `ConnectionLost`, `RequestTimeout`); the valve saying no to a command is
`CommandRejected`; a garbled reply is `ProtocolError`.

`PairingRequired.refused` separates a flat refusal (the valve isn't in pairing mode and the
adapter has no bond it accepts) from a connection that just couldn't be encrypted, which can
also be a proxy that needs a restart. Only a flat refusal is a reason to pair again.

### Testing your own code

`mira_ble.testing.FakeValve` stands in for a valve: its state changes when you send it
commands, and you can make it slow, silent on some requests, chatty, or behave the way a
Platinum was seen to (pausing after a stop, warming presets up first). It's a model built
from recordings and reports, not the real firmware. The module also includes the recorded
replies. It's there to help with tests, and may change between minor versions.

## Versions

Pin an exact version. While the version is 0.x, a minor release may change the API or the
protocol code, so read the
[changelog](https://github.com/83noit/mira-ble/blob/main/CHANGELOG.md) before upgrading. The
supported API is whatever `mira_ble` exports.

## Protocol

[docs/protocol.md](https://github.com/83noit/mira-ble/blob/main/docs/protocol.md) describes
the message format, every command the Mira app knows how to build, the layouts this library
decodes, and where each fact comes from.

## Acknowledgements

- [Ryan Shaw, mira-mode-control](https://github.com/ryan-shaw/mira-mode-control) (MIT): the
  state and preset layouts, worked out by sending known values to a real valve, and the
  recorded replies used in the tests here.
- [Hayden Griffin, ha-mira-mode](https://github.com/HaydenGriffin/ha-mira-mode) (MIT): the
  outlet settings and last-shower layouts, with recordings.
- [Jon Williams, Mira-Activate-HA](https://github.com/jonwilliams84/Mira-Activate-HA) (MIT):
  messages captured from the Mira app, and hard-won lessons about ESPHome proxies: pair on
  every connection, give the link a moment before subscribing, and don't re-pair just because
  something looks off.
- [Nigel Hannam, shower-controller-documentation](https://github.com/nhannam/shower-controller-documentation):
  the older generation's protocol.
- [ble-bond](https://github.com/83noit/ble-bond) (MIT, same author): the Bluetooth connection
  code this library runs on, shared with grundfos-ble. Its BlueZ pairing agent is adapted
  from [alpha-hwr](https://github.com/eman/alpha-hwr), credited there.

Copyright notices: [THIRD_PARTY_NOTICES.md](https://github.com/83noit/mira-ble/blob/main/THIRD_PARTY_NOTICES.md).

## Development

```sh
nix develop          # or: uv sync --group dev
uv run pytest --cov
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## Licence

[MIT](https://github.com/83noit/mira-ble/blob/main/LICENSE).
