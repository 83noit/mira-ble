"""Opcodes, payload layouts and the typed values they carry.

Every layout here states where it comes from, because the sources differ in strength:

* **measured**: bytes recorded from a real valve, usually after commanding a known value
  (see ``tests/`` and :mod:`mira_ble.testing`);
* **app**: read from the vendor app's code, which names the fields but is not proof of what
  a given valve firmware does.

Units: temperatures are degrees Celsius, flow is litres per minute, durations are seconds and
volumes are litres. On the wire a temperature is tenths of a degree, and flow is a byte of
quarter litres per minute (so 48 is 12 L/min; other projects call it a percentage of
25 L/min, which is the same number, and the vendor app shows it on a 0 to 25 scale).

A reply carries no opcode of its own, so the only way to tell replies apart is their shape.
The ``is_*_reply`` functions are those shape tests; the transport uses them to refuse a
stray or late frame rather than hand it to the wrong decoder.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, IntFlag, StrEnum
from typing import Final

from mira_ble.exceptions import ProtocolError, UnsafeValue
from mira_ble.frame import STATUS_OK, Frame


class Opcode(IntEnum):
    """Request opcodes this library sends.

    The vendor app builds more (configuration writes, commissioning, credentials, reset).
    They are deliberately absent: see ``docs/protocol.md`` for the full table and why.
    """

    READ_VALVE_CONFIG = 0x1A
    READ_OUTLET_CONFIG = 0x1B
    READ_STATE = 0x2B
    READ_SERIAL_NUMBER = 0x40
    READ_MANUFACTURED = 0x41
    READ_NAME = 0x44
    READ_USAGE = 0x5C
    READ_PRESET = 0x5D
    SET_OUTLETS = 0xAB
    RUN_PRESET = 0xB1
    WRITE_PRESET = 0xDD


class Phase(StrEnum):
    """What the valve is doing, in one word (see :attr:`State.phase`)."""

    REST = "rest"
    """Nothing runs and nothing is held."""
    WARMING_UP = "warming_up"
    """A preset is running water up to temperature; the valve holds when it gets there."""
    PAUSED = "paused"
    """Held with the water off: ready after a warm-up, or stopped. Rest follows by itself."""
    PRESET = "preset"
    """A preset is running and the valve is counting its duration down."""
    RUNNING = "running"
    """Water runs with no preset timer: an outlet opened directly."""


class Outlet(IntFlag):
    """Outlets, as the bitfield the valve uses in commands and state.

    A command states the complete desired state: any outlet not named is switched off.
    Which fixture is "first" depends on how the unit is plumbed; the outlet configuration
    and the presets tell.
    """

    NONE = 0
    FIRST = 0x01
    SECOND = 0x02
    THIRD = 0x04

    @classmethod
    def from_number(cls, number: int) -> Outlet:
        """Return the bit for outlet ``number`` (1 to 3)."""
        if not 1 <= number <= MAX_OUTLETS:
            raise ValueError(f"outlet out of range: {number}")
        return cls(1 << (number - 1))

    @property
    def numbers(self) -> tuple[int, ...]:
        """The outlet numbers (1 to 3) set in this value."""
        return tuple(n for n in range(1, MAX_OUTLETS + 1) if self & (1 << (n - 1)))


class OutletKind(IntEnum):
    """What an outlet feeds, from the type byte of its configuration (app)."""

    UNKNOWN = 0
    HAND_SHOWER = 1
    BATH_FILLER = 21
    OVERHEAD = 31

    @classmethod
    def from_type(cls, value: int) -> OutletKind:
        """Map the valve's outlet type byte; 21 to 23 are all bath fillers."""
        if 21 <= value <= 23:
            return cls.BATH_FILLER
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


MAX_OUTLETS: Final = 3
OUTLET_MASK: Final = 0x07
OUTLET_FLAG_HELD: Final = 0x40
STATE_PRESET_MASK: Final = 0x1F
STATE_FLAG_WARMING_UP: Final = 0x10
"""Bit 6 of the state's outlet byte: the valve holds a run with no outlet bit set.

The vendor app calls it "paused". On a Mira Platinum it was measured during a running preset,
with the water temperature rising. So it never means "stopped", whatever else it means.
"""
OUTLET_FLAG_FAULT: Final = 0x80
"""Set by the valve in the state's outlet byte when it reports a fault (app)."""

TEMPERATURE_MASK: Final = 0x1FF
"""Nine bits of tenths of a degree, as the vendor app reads a state or outlet temperature.

The bits above carry flags while the valve runs (``05 C2`` and ``0D C2`` were both reported
for a 45.0 C target), so they must be masked off. Presets pack ten bits instead.
"""
ERROR_CODE_OK: Final = 1
"""The state's error code when nothing is wrong (the app's "Product OK")."""
FLOW_STEPS_PER_LPM: Final = 4
USAGE_TEMPERATURE_NONE: Final = 0xFF
"""The last-usage temperature byte the app treats as "no reading"."""
MAX_PLAUSIBLE_WATER_TEMPERATURE: Final = 100.0

ABSOLUTE_MIN_TEMPERATURE: Final = 15.0
ABSOLUTE_MAX_TEMPERATURE: Final = 48.0
"""The hottest target this library will ever send, whatever the caller or the valve allows.

It is a backstop, not a recommendation: callers are expected to apply the valve's own
per-outlet maximum (45 to 46 C as shipped), which is lower.
"""
ABSOLUTE_MAX_FLOW: Final = 25.0
"""Litres per minute: the top of the wire scale (a byte value of 100)."""

STATE_LENGTH: Final = 18
VALVE_CONFIG_LENGTH: Final = 3
OUTLET_CONFIG_LENGTH: Final = 16
NAME_LENGTH: Final = 16
SERIAL_LENGTH: Final = 18
USAGE_LAST_LENGTH: Final = 6
PRESET_LENGTH: Final = 61
PRESET_NAME_LENGTH: Final = 31
PRESET_NAME_FIELD: Final = 32
MAX_PRESET_SLOT: Final = 9
"""The highest preset slot this library addresses. Not sourced: other code uses 15."""
PRESET_INVALID_SLOT: Final = 0xFF
"""What the valve echoes in place of the slot when asked for one it does not have."""
MANUFACTURED_LENGTH: Final = 4
MAX_COMMAND_REPLY: Final = 2
MAX_U16: Final = 0xFFFF

STATE_REQUEST: Final = b"\x02"
SERIAL_REQUEST: Final = b"\x01"
USAGE_LAST_REQUEST: Final = b"\x02\x00"


# --- Values -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class State:
    """What the valve is doing now (opcode 0x2B).

    Offsets 10 to 15 are measured: confirmed on real valves by commanding known values and
    reading them back. Byte 16 is measured as the flow actually reached, ramping after a start.
    """

    outlets: Outlet
    """Outlets the valve reports open."""
    held: bool
    """Bit 6 of the outlet byte; see :data:`OUTLET_FLAG_HELD`."""
    volume: int
    """State bytes 2-3: the litres delivered since the valve last left rest. The vendor app
    reads them as the volume a preset has used; measured on a Mira Platinum for every kind
    of run. Holds while the valve is held and clears at rest."""
    fault: bool
    """True when the valve flags a fault: outlet byte bit 7, or an error code other than OK."""
    error_code: int
    """The valve's error code (app: 1 is "Product OK")."""
    water_temperature: float | None
    """Measured outlet water temperature; None when the bytes are not a plausible reading."""
    target_temperature: float | None
    """The temperature the valve aims for.

    None is wire zero: no target at rest on some models, and "cold" while water runs.
    """
    flow_setting: float
    """Commanded flow in L/min. At rest the valve reports a default or the last value used."""
    flow: float
    """Flow reached in L/min, as the valve reports it (byte 16), whatever the outlet bits say."""
    raw: bytes = field(repr=False, default=b"")
    preset: int = 0
    """The preset slot the valve says is running, or 0 (byte 1, low five bits; the vendor app
    reads it so). Measured on a Mira Platinum: set only while a preset proper runs, not
    during its warm-up, and not for a run started by an outlet command."""
    time_remaining: int | None = None
    """Seconds the running preset has left (bytes 8-9, low twelve bits); None with no preset."""
    warming_up: bool = False
    """Bit 4 of byte 6: measured on a Mira Platinum while a preset warms the water up."""

    @property
    def phase(self) -> Phase:
        """Sum the state up as one :class:`Phase`."""
        if not self.running:
            return Phase.PAUSED if self.held else Phase.REST
        if self.warming_up:
            return Phase.WARMING_UP
        if self.preset:
            return Phase.PRESET
        return Phase.RUNNING

    @property
    def running(self) -> bool:
        """True while water runs: an outlet is reported open, or flow is measured.

        This is the property to test before saying water has stopped. The measured flow
        counts whatever the outlet bits say.
        """
        return bool(self.outlets) or self.flow > 0

    @property
    def active(self) -> bool:
        """True unless the valve reports itself fully at rest.

        Water running, or the held bit. Measured on a Mira Platinum, the held bit with no
        flow is a pause: the valve sets it when a preset has warmed the water and waits for
        the outlet button, and again after a stop, and drops to rest two minutes later.
        """
        return self.running or self.held

    def outlet_running(self, number: int) -> bool:
        """Return True when outlet ``number`` (1 to 3) is reported open."""
        return bool(self.outlets & Outlet.from_number(number))


@dataclass(frozen=True, slots=True)
class OutletConfig:
    """Limits and defaults the valve applies to one outlet (opcode 0x1B).

    Measured: maximum temperature, default flow and maximum duration were each changed in the
    vendor app and read back, and one recording carries the minimum and default temperatures
    and the flow limits at the offsets the app reads. App: the type byte.
    """

    number: int
    """Outlet number, 1 to 3."""
    kind: OutletKind
    """What the outlet feeds (app), for naming it."""
    outlet_type: int
    """The raw type byte behind ``kind``."""
    max_temperature: float
    min_temperature: float
    default_temperature: float
    max_flow: float
    min_flow: float
    default_flow: float
    max_duration: int
    """Seconds after which the valve stops the outlet by itself: the real safety backstop."""
    raw: bytes = field(repr=False, default=b"")


@dataclass(frozen=True, slots=True)
class Preset:
    """A stored preset (opcode 0x5D to read, 0xDD to write, 0xB1 to run).

    Measured: presets read from a valve re-encode to the bytes they arrived as.
    A preset runs either for a time or until a volume is delivered, never both.
    """

    slot: int
    name: str
    outlets: Outlet
    temperature: float
    flow: float
    duration: int | None = None
    """Seconds, or None for a volume preset."""
    volume: int | None = None
    """Litres, or None for a timed preset."""
    raw: bytes = field(repr=False, default=b"")


@dataclass(frozen=True, slots=True)
class LastUsage:
    """The valve's record of its last finished run (opcode 0x5C, request ``02 00``).

    Measured against the vendor app's "last usage" screen.
    """

    duration: int
    volume: int
    temperature: float | None
    """Blended water temperature, to half a degree; None when the valve stored none."""


@dataclass(frozen=True, slots=True)
class Identity:
    """Who the valve says it is."""

    name: str | None = None
    """The name set at commissioning, such as the room."""
    serial_number: str | None = None
    """Digits only; None when the valve has none (a blank serial is all zeroes)."""
    manufactured: datetime | None = None
    """Naive local time of manufacture, as stored in the valve."""


# --- Scaling ------------------------------------------------------------------------------------


def flow_to_wire(litres_per_minute: float) -> int:
    """Convert a flow in L/min to its wire byte, refusing values off the scale."""
    if not (math.isfinite(litres_per_minute) and 0 <= litres_per_minute <= ABSOLUTE_MAX_FLOW):
        raise UnsafeValue(f"flow out of range: {litres_per_minute} L/min")
    return round(litres_per_minute * FLOW_STEPS_PER_LPM)


def flow_from_wire(value: int) -> float:
    """Convert a wire flow byte to L/min."""
    return value / FLOW_STEPS_PER_LPM


def temperature_to_wire(celsius: float) -> int:
    """Convert a target temperature to tenths of a degree, refusing unsafe values."""
    if not (
        math.isfinite(celsius) and ABSOLUTE_MIN_TEMPERATURE <= celsius <= ABSOLUTE_MAX_TEMPERATURE
    ):
        raise UnsafeValue(
            f"temperature {celsius} is outside {ABSOLUTE_MIN_TEMPERATURE}-"
            f"{ABSOLUTE_MAX_TEMPERATURE} C"
        )
    return round(celsius * 10)


def _temperature(data: bytes) -> float:
    """Decode two bytes of masked tenths of a degree."""
    return (int.from_bytes(data, "big") & TEMPERATURE_MASK) / 10


def _is_text(data: bytes) -> bool:
    """Return True when every byte is printable ASCII or NUL padding."""
    return all(byte == 0 or 0x20 <= byte < 0x7F for byte in data)


def decode_text(data: bytes) -> str | None:
    """Decode a NUL-padded ASCII field; None when empty or not text."""
    text = data.split(b"\x00", 1)[0]
    if not text or any(not 0x20 <= byte < 0x7F for byte in text):
        return None
    return text.decode("ascii").strip() or None


def decode_serial(data: bytes) -> str | None:
    """Decode a serial number; None unless it is digits that are not all zero.

    A valve that was never given a serial reports zeroes, which the vendor app also treats
    as "none"; it must not be used as an identity.
    """
    text = decode_text(data)
    if text is None or not text.isdigit() or not text.strip("0"):
        return None
    return text


# --- Reply shapes -------------------------------------------------------------------------------


def is_command_reply(payload: bytes) -> bool:
    """Return True for an acknowledgement: a short reply that starts with the OK status."""
    return 1 <= len(payload) <= MAX_COMMAND_REPLY and payload[0] == STATUS_OK


def is_state_reply(payload: bytes) -> bool:
    """Return True for a reply shaped like a state: 18 bytes that are not text."""
    return len(payload) == STATE_LENGTH and any(payload) and not _is_text(payload)


def is_name_reply(payload: bytes) -> bool:
    """Return True for a reply shaped like a name: 16 bytes of text."""
    return len(payload) == NAME_LENGTH and _is_text(payload)


def is_serial_reply(payload: bytes) -> bool:
    """Return True for a reply shaped like a serial number: 18 bytes of text."""
    return len(payload) == SERIAL_LENGTH and _is_text(payload)


def is_manufactured_reply(payload: bytes) -> bool:
    """Return True for a reply the length of a manufacture date."""
    return len(payload) == MANUFACTURED_LENGTH


def is_valve_config_reply(payload: bytes) -> bool:
    """Return True for a reply the length of a valve configuration."""
    return len(payload) == VALVE_CONFIG_LENGTH


def is_last_usage_reply(payload: bytes) -> bool:
    """Return True for a last-usage record: 6 bytes echoing the request's first byte."""
    return len(payload) == USAGE_LAST_LENGTH and payload[0] == USAGE_LAST_REQUEST[0]


def is_outlet_config_reply(payload: bytes, number: int) -> bool:
    """Return True for outlet ``number``'s configuration: 16 bytes echoing its index."""
    return len(payload) == OUTLET_CONFIG_LENGTH and payload[0] == number - 1


def is_preset_reply(payload: bytes, slot: int) -> bool:
    """Return True for a preset reply about ``slot`` (or the valve's "no such slot")."""
    return len(payload) == PRESET_LENGTH and payload[0] in (slot, PRESET_INVALID_SLOT)


# --- Requests -----------------------------------------------------------------------------------


def set_outlets_request(outlets: Outlet, temperature: float | None, flow: float) -> Frame:
    """Build the command that runs exactly ``outlets`` at ``temperature`` and ``flow``.

    Payload: temperature (tenths, big endian), flow byte, outlet bitfield. Measured on several
    valves and the form the vendor app sends.

    ``temperature=None`` sends wire zero, which the app uses for "cold" and other code reads
    as "leave the setting alone". It exists so that an outlet change on a valve running with
    no target can pass that state through untouched; it never raises a temperature.
    """
    bits = int(outlets) & OUTLET_MASK
    if not bits:
        raise ValueError("no outlet named; use stop_request() to stop")
    wire_flow = flow_to_wire(flow)
    if not wire_flow:
        raise UnsafeValue("flow must be above zero to run an outlet")
    tenths = 0 if temperature is None else temperature_to_wire(temperature)
    return Frame.request(Opcode.SET_OUTLETS, tenths.to_bytes(2, "big") + bytes((wire_flow, bits)))


def pause_request(state: State) -> Frame:
    """Build the stop the vendor app sends: the valve's own target and flow, with the hold bit.

    The temperature and flow bytes are copied from ``state`` (a state the valve just
    reported), so nothing is raised and the valve keeps its settings. Measured on a Mira
    Platinum: the water stops and the valve holds, as after a stop at the wall, then drops
    to rest two minutes later with its target and flow unchanged.
    """
    raw = state.raw
    if len(raw) != STATE_LENGTH:
        raise ValueError("pause_request needs a state decoded from a reply")
    tenths = int.from_bytes(raw[10:12], "big") & TEMPERATURE_MASK
    return Frame.request(
        Opcode.SET_OUTLETS, tenths.to_bytes(2, "big") + bytes((raw[12], OUTLET_FLAG_HELD))
    )


def stop_request() -> Frame:
    """Build the command that closes every outlet: four zero bytes.

    Measured to stop Mode valves and a Mira Platinum. On the Platinum it also overwrites the
    valve's resting target and flow with "cold" and the minimum, which the wall controller
    then shows; :func:`pause_request` does not.
    """
    return Frame.request(Opcode.SET_OUTLETS, bytes(4))


def preset_stop_request() -> Frame:
    """Build the other stop: "run preset 0". Measured to stop an earlier Mira Platinum."""
    return Frame.request(Opcode.RUN_PRESET, b"\x00")


def run_preset_request(slot: int) -> Frame:
    """Build the command that starts the preset in ``slot`` (1-based)."""
    if not 1 <= slot <= MAX_PRESET_SLOT:
        raise ValueError(f"preset slot out of range: {slot}")
    return Frame.request(Opcode.RUN_PRESET, bytes((slot,)))


def read_state_request() -> Frame:
    """Build the state read."""
    return Frame.request(Opcode.READ_STATE, STATE_REQUEST)


def read_valve_config_request() -> Frame:
    """Build the read of the valve's configuration (its number of outlets)."""
    return Frame.request(Opcode.READ_VALVE_CONFIG)


def read_outlet_config_request(number: int) -> Frame:
    """Build the read of outlet ``number``'s (1 to 3) limits and defaults."""
    Outlet.from_number(number)
    return Frame.request(Opcode.READ_OUTLET_CONFIG, bytes((number - 1,)))


def read_preset_request(slot: int) -> Frame:
    """Build the read of the preset in ``slot``."""
    if not 0 <= slot <= MAX_PRESET_SLOT:
        raise ValueError(f"preset slot out of range: {slot}")
    return Frame.request(Opcode.READ_PRESET, bytes((slot,)))


def read_last_usage_request() -> Frame:
    """Build the read of the last finished run."""
    return Frame.request(Opcode.READ_USAGE, USAGE_LAST_REQUEST)


def read_name_request() -> Frame:
    """Build the read of the valve's name."""
    return Frame.request(Opcode.READ_NAME)


def read_serial_request() -> Frame:
    """Build the read of the valve's serial number."""
    return Frame.request(Opcode.READ_SERIAL_NUMBER, SERIAL_REQUEST)


def read_manufactured_request() -> Frame:
    """Build the read of the valve's date of manufacture."""
    return Frame.request(Opcode.READ_MANUFACTURED)


def write_preset_request(preset: Preset) -> Frame:
    """Build the command that stores ``preset`` in its slot, replacing what is there."""
    return Frame.request(Opcode.WRITE_PRESET, encode_preset(preset))


def delete_preset_request(slot: int) -> Frame:
    """Build the command that clears the preset in ``slot``."""
    if not 1 <= slot <= MAX_PRESET_SLOT:
        raise ValueError(f"preset slot out of range: {slot}")
    return Frame.request(Opcode.WRITE_PRESET, bytes((slot,)).ljust(PRESET_LENGTH, b"\x00"))


# --- Replies ------------------------------------------------------------------------------------


def decode_state(payload: bytes) -> State:
    """Decode a state reply."""
    if not is_state_reply(payload):
        raise ProtocolError(f"not a state reply ({len(payload)} bytes)", payload)
    outlet_byte = payload[13]
    error_code = payload[17]
    water = int.from_bytes(payload[14:16], "big") / 10
    preset = payload[1] & STATE_PRESET_MASK
    return State(
        outlets=Outlet(outlet_byte & OUTLET_MASK),
        held=bool(outlet_byte & OUTLET_FLAG_HELD),
        volume=int.from_bytes(payload[2:4], "big"),
        fault=bool(outlet_byte & OUTLET_FLAG_FAULT) or error_code not in (0, ERROR_CODE_OK),
        error_code=error_code,
        water_temperature=water if water <= MAX_PLAUSIBLE_WATER_TEMPERATURE else None,
        target_temperature=_temperature(payload[10:12]) or None,
        flow_setting=flow_from_wire(payload[12]),
        flow=flow_from_wire(payload[16]),
        raw=bytes(payload),
        preset=preset,
        time_remaining=(((payload[8] & 0x0F) << 8) | payload[9]) if preset else None,
        warming_up=bool(payload[6] & STATE_FLAG_WARMING_UP),
    )


def decode_outlet_count(payload: bytes) -> int:
    """Decode how many outlets the valve has, from its configuration reply (app)."""
    if not is_valve_config_reply(payload):
        raise ProtocolError(f"valve configuration of {len(payload)} bytes", payload)
    count = payload[2]
    if not 1 <= count <= MAX_OUTLETS:
        raise ProtocolError(f"valve reports {count} outlets", payload)
    return count


def decode_outlet_config(payload: bytes, number: int) -> OutletConfig:
    """Decode outlet ``number``'s configuration reply.

    The reply echoes the outlet index at byte 0, which is how it is told apart from a late
    reply to something else.
    """
    if len(payload) != OUTLET_CONFIG_LENGTH:
        raise ProtocolError(f"outlet configuration of {len(payload)} bytes", payload)
    if payload[0] != number - 1:
        raise ProtocolError(f"asked for outlet {number}, got index {payload[0]}", payload)
    return OutletConfig(
        number=number,
        kind=OutletKind.from_type(payload[1]),
        outlet_type=payload[1],
        max_temperature=_temperature(payload[3:5]),
        min_temperature=_temperature(payload[5:7]),
        default_temperature=_temperature(payload[7:9]),
        max_flow=flow_from_wire(payload[9]),
        min_flow=flow_from_wire(payload[10]),
        default_flow=flow_from_wire(payload[11]),
        max_duration=int.from_bytes(payload[12:14], "big"),
        raw=bytes(payload),
    )


def decode_preset(payload: bytes, slot: int) -> Preset | None:
    """Decode a preset reply; None for an empty or invalid slot.

    An empty slot answers with zeroes where the name belongs, and an invalid one echoes a
    different slot number. Only the 61-byte layout is decoded; the vendor app also knows a
    shorter one that no recorded valve has produced.
    """
    if len(payload) != PRESET_LENGTH:
        raise ProtocolError(f"preset reply of {len(payload)} bytes", payload)
    if payload[0] != slot:
        return None
    name = decode_text(payload[1 : 1 + PRESET_NAME_LENGTH])
    if name is None:
        return None
    flags = payload[37]
    return Preset(
        slot=slot,
        name=name,
        # The outlet bits sit two places higher here than in a command's bitfield; the low
        # two bits of the same byte are the top of the temperature.
        outlets=Outlet((flags >> 2) & OUTLET_MASK),
        temperature=(((flags & 0b11) << 8) | payload[38]) / 10,
        flow=flow_from_wire(payload[39]),
        duration=int.from_bytes(payload[33:35], "big") or None,
        volume=int.from_bytes(payload[35:37], "big") or None,
        raw=bytes(payload),
    )


def _preset_span(preset: Preset) -> tuple[int, int]:
    """Return the preset's (duration, volume) as wire values; exactly one is non-zero."""
    given = [value for value in (preset.duration, preset.volume) if value is not None]
    if len(given) != 1:
        raise ValueError("a preset needs either a duration or a volume, not both")
    (value,) = given
    if type(value) is not int or not 1 <= value <= MAX_U16:  # a float or bool is refused
        label = "duration" if preset.duration is not None else "volume"
        raise ValueError(f"{label} out of range: {value!r}")
    return (value, 0) if preset.duration is not None else (0, value)


def encode_preset(preset: Preset) -> bytes:
    """Encode ``preset`` in the layout a read returns.

    A preset with neither a time nor a volume bound, or with no flow, is refused: it would
    run without limit, and the vendor app shows such a slot as empty.
    """
    if not 1 <= preset.slot <= MAX_PRESET_SLOT:
        raise ValueError(f"preset slot out of range: {preset.slot}")
    if not int(preset.outlets) & OUTLET_MASK:
        raise ValueError("a preset needs at least one outlet")
    duration, volume = _preset_span(preset)
    try:
        name = preset.name.encode("ascii")
    except UnicodeEncodeError as err:
        raise ValueError("preset names are ASCII only") from err
    if not name or len(name) > PRESET_NAME_LENGTH or not _is_text(name) or b"\x00" in name:
        raise ValueError(f"preset names are 1 to {PRESET_NAME_LENGTH} printable characters")
    tenths = temperature_to_wire(preset.temperature)
    flow = flow_to_wire(preset.flow)
    if not flow:
        raise UnsafeValue("a preset needs a flow above zero")
    settings = (
        duration.to_bytes(2, "big")
        + volume.to_bytes(2, "big")
        + bytes((((int(preset.outlets) & OUTLET_MASK) << 2) | (tenths >> 8), tenths & 0xFF, flow))
    )
    payload = bytes((preset.slot,)) + name.ljust(PRESET_NAME_FIELD, b"\x00") + settings
    return payload.ljust(PRESET_LENGTH, b"\x00")


def decode_last_usage(payload: bytes) -> LastUsage:
    """Decode the last-run record."""
    if not is_last_usage_reply(payload):
        raise ProtocolError("not a last-usage reply", payload)
    temperature = payload[5]
    return LastUsage(
        duration=int.from_bytes(payload[1:3], "big"),
        volume=int.from_bytes(payload[3:5], "big"),
        temperature=None if temperature == USAGE_TEMPERATURE_NONE else temperature / 2,
    )


def decode_manufactured(payload: bytes) -> datetime | None:
    """Decode the packed manufacture timestamp; None when it is not a plausible date.

    Four bytes: years since 2000, the day, then a nibble of month and twelve bits of minutes
    past midnight. Measured: the serial number of the recorded valve embeds the same digits
    (day, month, year, hour, minute from its fifth character).
    """
    if len(payload) < MANUFACTURED_LENGTH:
        return None
    month = payload[2] >> 4
    minutes = ((payload[2] & 0x0F) << 8) | payload[3]
    if minutes >= 24 * 60:
        return None
    try:
        return datetime(2000 + payload[0], month, payload[1], minutes // 60, minutes % 60)  # noqa: DTZ001 - the valve stores no zone
    except ValueError:
        return None
