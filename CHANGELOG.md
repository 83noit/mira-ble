# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

While the version is 0.x, minor versions (0.1 to 0.2) may break the public API and the protocol
layer; patch releases do not. Pin the exact version you have tested.

## [Unreleased]

> **Note:** run by hand on one Mira Platinum from macOS (pairing, reads, preset start, outlet
> command, both stops). Not yet run through a proxy, on Linux, or on another model.

### Added

- Frame codec with reassembly, and a transport for local BlueZ adapters and ESPHome proxies:
  pairing on every connection, a settle before the subscribe, replies matched by shape, and
  any request that does not complete closes the link.
- Typed state (`State.running` is an open outlet or measured flow; `State.active` adds the
  held bit; `State.volume` is the litres a Platinum counts while it runs), outlet
  configuration, presets, identity and last-usage reads. Every read except the state is
  optional and tolerates a valve that refuses or ignores it.
- Confirmed control: run outlets, run a preset, and a stop that sends the vendor app's pause
  first (keeping the valve's settings), then the all-zero stop, then "run preset 0";
  confirmation waits up to 8 s for the valve to act. Preset write and delete.
- `State.phase`, `State.preset`, `State.time_remaining` and `State.warming_up`, and
  `run_preset_through()` / `mira-ble start --through` for valves that warm a preset up and
  then hold (measured on a Mira Platinum: the same preset command during the hold starts the
  preset proper).
- The proxy or adapter Home Assistant chose is recorded for every connection attempt,
  refused ones included, so a refusal is not pinned on the proxy that merely last heard the
  valve. One BlueZ pairing agent at a time. Both ported from grundfos-ble.
- `BleTransport.link_age`: through an ESPHome proxy a link held for more than a few seconds
  was measured to answer slowly, so callers can choose to reconnect.
- The Bluetooth link is [ble-bond](https://github.com/83noit/ble-bond), the package shared
  with grundfos-ble; `BleTransport` is a thin adapter over it and keeps its interface. Frames
  are logged on `ble_bond.frames` (`mira-ble -v` turns it on).
- Temperature limits enforced before anything is written.
- `mira-ble` command line, with read-only `scan`, `info`, `status`, `watch`,
  `outlets-config` and `presets`.
- `mira_ble.testing`: a simulated valve and replies recorded from real valves.
