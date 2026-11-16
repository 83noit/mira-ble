# Changelog

Notable changes, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/).

While the version is 0.x, a minor release (0.1 to 0.2) may break the API or the protocol
code; patch releases won't. Pin the exact version you've tested.

## [Unreleased]

### Changed

- The README and the package description now say what's actually been tried: the library is
  for the Mira Platinum first. Activate and Mode valves should work but haven't been tried,
  and the README points to the projects built on them.
- The docs say a Mira Platinum takes two connections at once (measured), and explain the
  proxy settings around losing and keeping a bond.

## [0.1.1] - 2026-11-16

### Fixed

- `State.phase` now says `paused` as soon as the valve holds with no outlet open. Before, the
  second or two of water still draining after a warm-up or a stop showed as `running` (seen
  in Home Assistant during a real shower). `State.running` hasn't changed.

## [0.1.0] - 2026-11-16

First release. It has only run on one Mira Platinum: by hand from a Mac (pairing, reads,
starting a preset, the outlet command, both ways of stopping) and through an ESPHome proxy
under Home Assistant. Not tried yet on a local Linux adapter or on another model.

### Added

- Building and decoding messages, including replies that arrive in pieces, and a connection
  that works with local BlueZ adapters and ESPHome proxies: it pairs on every connection,
  waits a moment before subscribing, matches replies by their shape, and closes the
  connection if a request doesn't finish.
- Typed readings: the state (`State.running` is "water is coming out", `State.active` also
  counts the paused state, `State.volume` is the litres a Platinum counts during a shower),
  outlet settings, presets, identity and the last shower. Every read except the state is
  optional and copes with a valve that refuses or ignores it.
- Commands that check they worked: running outlets, running a preset, and a stop that first
  sends the Mira app's own pause (which keeps the valve's settings), then the all-zero stop,
  then "run preset 0". Each waits up to 8 s for the valve to act. Writing and deleting
  presets.
- `State.phase`, `State.preset`, `State.time_remaining` and `State.warming_up`, plus
  `run_preset_through()` / `mira-ble start --through` for valves that warm a preset up and then
  wait (seen on a Mira Platinum: sending the same preset again during that wait starts it for
  real).
- For every connection attempt, the adapter or proxy Home Assistant picked is recorded,
  failed attempts included, so a refusal isn't blamed on whichever proxy last happened to hear
  the valve. Only one BlueZ pairing agent at a time. Both come from grundfos-ble.
- `BleTransport.link_age`: through an ESPHome proxy, a connection kept open for more than a
  few seconds was measured to answer slowly, so callers can choose to reconnect.
- The Bluetooth connection code is [ble-bond](https://github.com/83noit/ble-bond), shared
  with grundfos-ble; `BleTransport` sits on top of it and keeps its interface. Messages are
  logged on `ble_bond.frames` (`mira-ble -v` turns that on).
- Temperature limits are checked before anything is sent.
- The `mira-ble` command line, with read-only `scan`, `info`, `status`, `watch`,
  `outlets-config` and `presets`.
- `mira_ble.testing`: a simulated valve, and replies recorded from real valves.
