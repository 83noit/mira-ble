# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

While the version is 0.x, minor versions (0.1 to 0.2) may break the public API and the protocol
layer; patch releases do not. Pin the exact version you have tested.

## [Unreleased]

> **Note:** nothing in this library has yet run on a real valve. Treat it as untested
> against hardware.

### Added

- Frame codec with reassembly, and a transport for local BlueZ adapters and ESPHome proxies:
  pairing on every connection, a settle before the subscribe, replies matched by shape, and
  any request that does not complete closes the link.
- Typed state (`State.active` covers an open outlet, the held bit and the run flag), outlet
  configuration, presets, identity and last-usage reads. Every read except the state is
  optional and tolerates a valve that refuses or ignores it.
- Confirmed control: run outlets, run a preset, and a stop that falls back to "run preset 0";
  confirmation waits up to 8 s for the valve to act. Preset write and delete.
- Temperature limits enforced before anything is written.
- `mira-ble` command line, with read-only `scan`, `info`, `status`, `watch`,
  `outlets-config` and `presets`.
- `mira_ble.testing`: a simulated valve and replies recorded from real valves.
