# mira-ble

Async Python library for the **Mira Platinum** digital shower over Bluetooth LE. Mira Activate
and current-generation Mira Mode valves speak the same protocol and are expected to work, but
this library has not been run on one (see [Supported hardware](#supported-hardware)). It reads the valve's state, outlet limits,
presets and last run, and runs or stops outlets and presets, with every control command
confirmed by reading the valve back.

It is the library behind the Home Assistant integration
[ha-mira-ble](https://github.com/83noit/ha-mira-ble), and has no Home Assistant dependency.

> **Status: alpha, run on one real valve.** Pairing, every read, starting a preset, running an
> outlet and both stops were run against one Mira Platinum, a handful of times, attended:
> from macOS, and through an ESPHome proxy under Home Assistant. Nothing has run on a local
> Linux adapter or on another model; those paths are tested only against a simulation and
> against replies recorded by the projects credited below. Start with the read-only commands
> and watch it behave.

> **This drives plumbing that produces hot water.** Never run it unattended. The library
> cannot stop the water if the link drops or your program exits: the valve's own maximum run
> time is the only backstop then.

This project is not affiliated with Mira Showers or Kohler. Mira and Kohler are trademarks of
their owners.

## Supported hardware

Valves whose Bluetooth name looks like `Mira 0051#…` or `Mira 004C Bathroom` and that offer
the GATT service `267f0001-eb15-43f5-94c3-67d2221188f7`. Older Mira Mode valves offer
`bccb0001-ca66-11e5-88a4-0002a5d5c51b` and speak an unrelated protocol; they are not
supported.

| Model | Status |
| --- | --- |
| Mira Platinum | Run on one unit from macOS: pairing, every read, a preset start, an outlet command, the pause and the all-zero stop. A preset warms the water up and then holds until the outlet button or an outlet command starts the shower. The vendor app itself controls the Platinum through the cloud |
| Mira Activate, current Mira Mode | **Expected to work, untested.** The protocol was measured on real valves by the projects credited below, and this library is tested against their recordings, but it has not been run on one. For those valves the field-proven choices are [Mira-Activate-HA](https://github.com/jonwilliams84/Mira-Activate-HA) (Activate) and [ha-mira-mode](https://github.com/HaydenGriffin/ha-mira-mode) or [mira-mode-control](https://github.com/ryan-shaw/mira-mode-control) (Mode). Testers are welcome: please [open an issue](https://github.com/83noit/mira-ble/issues) |

## Install

```sh
pip install mira-ble      # or: uv add mira-ble
```

Python 3.13 or newer.

## Pairing

The valve only talks over a bonded link, and only accepts a new bond while it is in pairing
mode: on a controller with a button, hold it for five seconds until it flashes; on a dial
controller use Menu → Settings → Connect. Switching the valve's power off and on is reported
to leave it pairable for a short while as well. Pairing is "Just Works" (no code to enter).

The library pairs on every connection, which is a quick re-encrypt once a bond exists. The
bond lives on the adapter (or ESPHome proxy) that made it. The vendor app's own text says a
valve keeps up to ten paired devices and lists them under "Manage Paired Devices"; nobody
here has measured the limit.

If the valve is reset, or the device is removed from it in the app, the adapter keeps
offering keys the valve no longer accepts. Forget the valve on the adapter and pair again
(`remove_bluez_bond()` does that on Linux).

**An ESPHome proxy can lose the bond, in two different ways:**

- *A connection that drops while it is being encrypted erases the bond from the proxy's
  flash.* This is how ESP-IDF's Bluetooth stack is built, on every version; it happened once
  in about 150 connections on a marginal link (-76 dBm). The valve then has to be paired
  again. A strong signal makes it rare. From ESP-IDF 5.5.6 the proxy can be built with
  `CONFIG_BT_BLE_SMP_REMOVE_BOND_ON_PAIR_FAIL_AS_CENTRAL: "n"` to keep the bond (untested
  here).
- *ESP-IDF 5.5.6 (ESPHome 2026.10.x) forgets a bond after the first connection*, until the
  proxy restarts ([esphome#21880](https://github.com/esphome/esphome/issues/21880)). Pin the
  proxy to ESP-IDF 5.5.5, or use ESPHome 2026.11 or later, which works around it.

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

The output above is illustrative. `scan`, `info`, `status`, `watch`, `outlets-config` and
`presets` change nothing on the valve: **start with those**, with `watch --raw` running while
someone uses the shower at the wall, before sending anything.

- `run` reads the outlets' own maximum temperature first and applies the lower of that and
  `--max-temperature` (default 45 °C; give it before the command name).
- If a `run` or `start` cannot be confirmed, the tool sends a stop before exiting and says
  so; if it prints `CHECK THE SHOWER`, go and look.
- `start` sends the preset command once. On a Mira Platinum that warms the water up and then
  holds, waiting; `start --through` waits for the hold and sends the command again, which
  runs the preset with its own timer (water then runs for the preset's whole duration).
- `presets` exits with an error and a reason when it could read none; some valves only
  answer preset reads while running.
- `-v` logs every frame, from this package only. Frames carry the valve's name and serial
  number when those are read.
- Addresses are MAC addresses on Linux and Windows, and UUIDs on macOS. Flow is litres per
  minute; the vendor app shows the same number on its 0 to 25 scale.

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

Units are degrees Celsius, litres per minute, seconds and litres throughout.

### What the control commands guarantee

- **Temperature limits before anything is written.** A target above `max_temperature`
  (default 45 °C), above a per-call limit, or above the absolute ceiling of 48 °C raises
  `UnsafeValue`; so does a limit that is not a number.
- **Confirmation by the valve's state, within a few seconds.** `set_outlets`, `stop` and
  `run_preset` require the valve's acknowledgement and then read the state until it shows
  the command, for up to 8 s (valves take time to act). `NotConfirmed` carries the last
  state read: the valve may still be acting, so **if a start is not confirmed, call
  `stop()`**.
- **"Stopped" means no water.** `State.running` is true for an open outlet or measured
  flow; a stop is confirmed only when neither shows on two reads. A valve may stay *held*
  (`State.held`, a pause it ends by itself) for a while after; `State.active` covers both. `stop()`
  first sends the vendor app's pause (the valve's own temperature and flow with the hold
  bit), which leaves the valve's settings alone; if water still runs it sends the all-zero
  stop, then "run preset 0".
- **One word for what the valve is doing.** `State.phase` is `rest`, `warming_up`, `paused`,
  `preset` or `running`; `State.preset` and `State.time_remaining` give the running preset
  and the seconds it has left by the valve's own count, and `State.volume` the litres used.
  `run_preset()` sends the preset command once; `run_preset_through()` carries a preset past
  the warm-up hold measured on a Mira Platinum.
- **Complete state.** A command names every outlet that should run; any other is switched
  off. Which fixture is outlet 1 depends on the plumbing: `outlets-config` and `presets` tell.
- **Replies are matched by shape.** A reply carries no identifier, so each read accepts only
  a reply of the right shape, and a request that does not complete (timeout, cancellation)
  closes the link.
- **Prefer short connections through a proxy.** Measured through an ESPHome proxy on a Mira
  Platinum, requests on a new link take 0.1 to 0.3 s; once the link has been up for a few
  seconds they take 7 to 10 s and often time out. `BleTransport.link_age` says how old the
  link is. From macOS a link held for minutes stayed quick.
- **Optional reads.** `read_identity()` and `probe()` record what a valve refused or ignored
  in `errors` and carry on; only the state read must succeed.
- **No configuration writes.** The library does not implement the opcodes that change outlet
  limits, controller configuration, Wi-Fi credentials, commissioning state, the serial number
  or that reset the valve. Preset write and delete are the only persistent writes; a write
  replaces the whole slot for good, so note down what is there first.

### Errors

Everything derives from `MiraError`. Connection problems are `MiraConnectionError`
(`PairingRequired`, `ConnectionLost`, `RequestTimeout`); the valve refusing a command is
`CommandRejected`; a malformed reply is `ProtocolError`.

`PairingRequired.refused` tells an outright refusal (the valve is not in pairing mode and the
adapter holds no bond it accepts) from a link that merely could not be encrypted, which can
also be a proxy that needs a restart. Only the first is a reason to pair again.

### Testing your own code

`mira_ble.testing.FakeValve` is a transport standing in for a valve: its state changes when
commanded, and it can be made slow to act, silent on chosen opcodes, chatty, or to stay held
after a stop as a Platinum was measured to. It is a model built from recordings and reports, not the
firmware. The module also ships the recorded replies. It is test support, and may change
between minor versions.

## Versions

Pin an exact version. While the version is 0.x a minor release may change the API or the
protocol layer; read the
[changelog](https://github.com/83noit/mira-ble/blob/main/CHANGELOG.md) before upgrading. The
supported API is what `mira_ble` exports.

## Protocol

[docs/protocol.md](https://github.com/83noit/mira-ble/blob/main/docs/protocol.md) describes
the frame format, every opcode the vendor app builds, the layouts this library decodes, and
where each fact comes from.

## Acknowledgements

- [Ryan Shaw, mira-mode-control](https://github.com/ryan-shaw/mira-mode-control) (MIT):
  the state and preset layouts, verified by commanding known values on a real valve, and the
  recorded replies used as test vectors here.
- [Hayden Griffin, ha-mira-mode](https://github.com/HaydenGriffin/ha-mira-mode) (MIT): the
  outlet configuration and last-usage layouts, with recordings.
- [Jon Williams, Mira-Activate-HA](https://github.com/jonwilliams84/Mira-Activate-HA) (MIT):
  frames captured from the vendor app, and the hard-won lessons about ESPHome proxies:
  pairing on every connection, settling before the subscribe, and not re-pairing on suspicion.
- [Nigel Hannam, shower-controller-documentation](https://github.com/nhannam/shower-controller-documentation):
  the older generation's protocol.
- [ble-bond](https://github.com/83noit/ble-bond) (MIT, same author): the bonded Bluetooth
  link this library runs on, shared with grundfos-ble. Its BlueZ pairing agent is adapted
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
