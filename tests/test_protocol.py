"""Payload layouts, against replies recorded from real valves."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from mira_ble import protocol
from mira_ble.exceptions import ProtocolError, UnsafeValue
from mira_ble.protocol import Opcode, Outlet, OutletKind, Preset
from mira_ble.testing import (
    APP_SET_38C_16LPM_BOTH,
    APP_SET_38C_16LPM_OUTLET_2,
    APP_SET_39C_8LPM_OUTLET_2,
    DEVICE_ID_EMPTY,
    LAST_USAGE,
    MANUFACTURED,
    NAME,
    OUTLET_1_CONFIG,
    OUTLET_2_CONFIG,
    PLATINUM_READY,
    PLATINUM_REST,
    PLATINUM_SHOWER,
    PLATINUM_STOPPING,
    PLATINUM_WARM_UP,
    PRESET_BATH,
    PRESET_EMPTY,
    PRESET_INVALID,
    PRESET_SHOWER,
    SERIAL,
    SERIAL_BLANK,
    SET_39C_25LPM_OUTLET_1,
    STATE_IDLE,
    STATE_RAMPING,
    STATE_RUNNING,
    STATE_RUNNING_WARMER,
    STATE_STOPPED,
    STATE_TARGET_WITH_FLAGS,
    STATE_TARGET_WITH_OTHER_FLAGS,
    STOP_FRAME,
    STOP_KEEPING_TEMPERATURE_FRAME,
    VALVE_CONFIG,
)

# --- commands ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outlets", "temperature", "flow", "recorded"),
    [
        (Outlet.SECOND, 38.0, 16.0, APP_SET_38C_16LPM_OUTLET_2),
        (Outlet.SECOND, 39.0, 8.0, APP_SET_39C_8LPM_OUTLET_2),
        (Outlet.FIRST | Outlet.SECOND, 38.0, 16.0, APP_SET_38C_16LPM_BOTH),
        (Outlet.FIRST, 39.0, 25.0, SET_39C_25LPM_OUTLET_1),
    ],
)
def test_set_outlets_matches_frames_real_valves_accepted(
    outlets: Outlet, temperature: float, flow: float, recorded: bytes
) -> None:
    assert protocol.set_outlets_request(outlets, temperature, flow).encode() == recorded


def test_stop_matches_the_frame_logged_against_a_valve() -> None:
    assert protocol.stop_request().encode() == STOP_FRAME


def test_the_other_measured_stop_form_is_the_same_command_with_a_temperature() -> None:
    frame = protocol.Frame.decode(STOP_KEEPING_TEMPERATURE_FRAME)
    assert frame.opcode == Opcode.SET_OUTLETS
    assert frame.payload[2:] == b"\x00\x00"  # no flow, no outlets


def test_the_preset_stop_is_run_preset_zero() -> None:
    assert protocol.preset_stop_request().encode().hex(" ") == "aa 55 00 b1 01 00 4f"


def test_every_outlet_bit_is_encoded() -> None:
    frame = protocol.set_outlets_request(Outlet.FIRST | Outlet.SECOND | Outlet.THIRD, 38.0, 8.0)
    assert frame.payload[3] == 0b111
    assert protocol.set_outlets_request(Outlet.THIRD, 38.0, 8.0).payload[3] == 0b100


@pytest.mark.parametrize(
    ("temperature", "expected"), [(39.0, "0186"), (42.0, "01a4"), (39.55, "018c"), (24.9, "00f9")]
)
def test_temperature_is_rounded_to_tenths(temperature: float, expected: str) -> None:
    assert (
        protocol.set_outlets_request(Outlet.FIRST, temperature, 8.0).payload[:2].hex() == expected
    )


def test_flow_is_rounded_to_quarter_litres() -> None:
    assert protocol.set_outlets_request(Outlet.FIRST, 38.0, 7.9).payload[2] == 32
    assert protocol.set_outlets_request(Outlet.FIRST, 38.0, 0.2).payload[2] == 1


def test_no_target_is_sent_as_wire_zero() -> None:
    """An outlet change on a valve running cold must pass that through, never heat it."""
    frame = protocol.set_outlets_request(Outlet.SECOND, None, 8.0)
    assert frame.payload == bytes((0, 0, 32, 2))


@pytest.mark.parametrize("temperature", [48.1, 10.0, 14.9, 60.0, float("nan"), float("inf")])
def test_unsafe_temperatures_are_refused(temperature: float) -> None:
    with pytest.raises(UnsafeValue, match="temperature"):
        protocol.set_outlets_request(Outlet.FIRST, temperature, 8.0)


@pytest.mark.parametrize("flow", [25.5, -1.0, float("nan")])
def test_flows_off_the_scale_are_refused(flow: float) -> None:
    with pytest.raises(UnsafeValue, match="flow out of range"):
        protocol.set_outlets_request(Outlet.FIRST, 38.0, flow)


def test_set_outlets_refuses_empty_commands() -> None:
    with pytest.raises(UnsafeValue, match="above zero"):
        protocol.set_outlets_request(Outlet.FIRST, 38.0, 0.0)
    with pytest.raises(UnsafeValue, match="above zero"):
        protocol.set_outlets_request(Outlet.FIRST, 38.0, 0.1)  # rounds to no flow
    with pytest.raises(ValueError, match="no outlet"):
        protocol.set_outlets_request(Outlet.NONE, 38.0, 8.0)


def test_read_requests() -> None:
    assert protocol.read_state_request().encode().hex(" ") == "aa 55 00 2b 01 02 d3"
    assert protocol.run_preset_request(2).encode().hex(" ") == "aa 55 00 b1 01 02 4d"
    assert protocol.read_valve_config_request().encode().hex(" ") == "aa 55 00 1a 00 e7"
    assert protocol.read_serial_request().encode().hex(" ") == "aa 55 00 40 01 01 bf"
    assert protocol.read_name_request().encode().hex(" ") == "aa 55 00 44 00 bd"
    assert protocol.read_manufactured_request().encode().hex(" ") == "aa 55 00 41 00 c0"
    assert protocol.read_last_usage_request().encode().hex(" ") == "aa 55 00 5c 02 02 00 a1"
    assert protocol.read_outlet_config_request(2).payload == b"\x01"
    assert protocol.read_preset_request(3).payload == b"\x03"


def test_requests_refuse_values_out_of_range() -> None:
    for slot in (0, -1, 10, 99):
        with pytest.raises(ValueError, match="slot"):
            protocol.run_preset_request(slot)
        with pytest.raises(ValueError, match="slot"):
            protocol.delete_preset_request(slot)
    with pytest.raises(ValueError, match="slot"):
        protocol.read_preset_request(10)
    with pytest.raises(ValueError, match="outlet"):
        protocol.read_outlet_config_request(4)


def test_outlet_numbers() -> None:
    assert Outlet.from_number(1) is Outlet.FIRST
    assert Outlet.from_number(3) is Outlet.THIRD
    assert (Outlet.FIRST | Outlet.THIRD).numbers == (1, 3)
    assert Outlet.NONE.numbers == ()
    with pytest.raises(ValueError, match="outlet"):
        Outlet.from_number(0)


# --- state ------------------------------------------------------------------------------------


def test_idle_state() -> None:
    state = protocol.decode_state(STATE_IDLE)
    assert not state.active
    assert state.outlets is Outlet.NONE
    assert not state.held
    assert state.volume == 0
    assert not state.running
    assert state.target_temperature is None
    assert state.water_temperature == 25.6
    assert state.flow_setting == 4.0
    assert state.flow == 0.0
    assert state.raw == STATE_IDLE
    assert state.error_code == 1
    assert not state.fault


def test_running_state_shows_what_was_commanded() -> None:
    state = protocol.decode_state(STATE_RUNNING)
    assert state.active
    assert state.outlet_running(1)
    assert not state.outlet_running(2)
    assert state.target_temperature == 42.0
    assert state.flow_setting == 12.5
    assert state.water_temperature == 24.6
    warmer = protocol.decode_state(STATE_RUNNING_WARMER)
    assert warmer.water_temperature == 26.0
    assert warmer.target_temperature == 42.0  # the target does not move as the water warms


def test_flow_reached_ramps_after_a_start() -> None:
    state = protocol.decode_state(STATE_RAMPING)
    assert state.flow_setting == 12.0
    assert state.flow == 7.5
    assert state.target_temperature == 45.0


@pytest.mark.parametrize("recorded", [STATE_TARGET_WITH_FLAGS, STATE_TARGET_WITH_OTHER_FLAGS])
def test_target_temperature_ignores_the_flag_bits_above_it(recorded: bytes) -> None:
    assert protocol.decode_state(recorded).target_temperature == 45.0


def test_stopped_state() -> None:
    state = protocol.decode_state(STATE_STOPPED)
    assert not state.active
    assert state.water_temperature == 25.9


def test_every_outlet_bit_is_decoded() -> None:
    running = bytearray(STATE_RUNNING)
    running[13] = 0b111
    state = protocol.decode_state(bytes(running))
    assert state.outlets == Outlet.FIRST | Outlet.SECOND | Outlet.THIRD
    assert state.outlet_running(3)


def test_measured_flow_means_running_whatever_the_outlet_bits_say() -> None:
    held = bytearray(STATE_RUNNING)
    held[13] = 0x40
    state = protocol.decode_state(bytes(held))
    assert state.outlets is Outlet.NONE
    assert state.held
    assert state.flow == 4.5
    assert state.running
    assert state.active


def test_the_held_bit_alone_is_active_but_not_running() -> None:
    held = bytearray(STATE_IDLE)
    held[13] = 0x40
    state = protocol.decode_state(bytes(held))
    assert state.active
    assert not state.running


def test_the_volume_byte_alone_is_neither_running_nor_active() -> None:
    counted = bytearray(STATE_IDLE)
    counted[3] = 0x04
    state = protocol.decode_state(bytes(counted))
    assert state.volume == 4
    assert not state.active


@pytest.mark.parametrize(
    ("payload", "outlets", "held", "running", "active", "volume", "flow"),
    [
        (PLATINUM_REST, Outlet.NONE, False, False, False, 0, 0.0),
        (PLATINUM_WARM_UP, Outlet.FIRST, False, True, True, 1, 7.0),
        (PLATINUM_READY, Outlet.NONE, True, False, True, 4, 0.0),
        (PLATINUM_SHOWER, Outlet.FIRST, False, True, True, 2, 7.25),
        (PLATINUM_STOPPING, Outlet.NONE, True, True, True, 2, 7.25),
    ],
)
def test_the_phases_recorded_on_a_platinum(
    payload: bytes,
    outlets: Outlet,
    held: bool,
    running: bool,
    active: bool,
    volume: int,
    flow: float,
) -> None:
    state = protocol.decode_state(payload)
    assert (state.outlets, state.held, state.running, state.active) == (
        outlets,
        held,
        running,
        active,
    )
    assert (state.volume, state.flow) == (volume, flow)
    assert state.target_temperature == 40.0
    assert state.flow_setting == 8.0
    assert not state.fault


def test_fault_flag_and_error_code() -> None:
    flagged = bytearray(STATE_RUNNING)
    flagged[13] = 0x81
    state = protocol.decode_state(bytes(flagged))
    assert state.fault
    assert state.outlets is Outlet.FIRST
    coded = bytearray(STATE_IDLE)
    coded[17] = 50  # the app's "no flow"
    state = protocol.decode_state(bytes(coded))
    assert state.fault
    assert state.error_code == 50


def test_an_implausible_water_temperature_is_no_reading() -> None:
    hot = bytearray(STATE_IDLE)
    hot[14:16] = (2698).to_bytes(2, "big")
    assert protocol.decode_state(bytes(hot)).water_temperature is None


@pytest.mark.parametrize(
    "other",
    [
        NAME,
        SERIAL,
        SERIAL_BLANK,
        OUTLET_1_CONFIG,
        PRESET_SHOWER,
        LAST_USAGE,
        STATE_RUNNING[:16],
        STATE_RUNNING + b"\x00",
        bytes(18),
    ],
    ids=[
        "name",
        "serial",
        "blank-serial",
        "outlet-config",
        "preset",
        "usage",
        "short",
        "long",
        "zeroes",
    ],
)
def test_other_replies_are_not_taken_for_a_state(other: bytes) -> None:
    assert not protocol.is_state_reply(other)
    with pytest.raises(ProtocolError, match="not a state reply"):
        protocol.decode_state(other)


def test_reply_shapes_tell_the_replies_apart() -> None:
    replies = {
        "state": STATE_RUNNING,
        "name": NAME,
        "serial": SERIAL,
        "manufactured": MANUFACTURED,
        "valve_config": VALVE_CONFIG,
        "usage": LAST_USAGE,
        "outlet_1": OUTLET_1_CONFIG,
        "outlet_2": OUTLET_2_CONFIG,
        "preset_1": PRESET_SHOWER,
        "ack": b"\x01",
    }
    tests = {
        "state": protocol.is_state_reply,
        "name": protocol.is_name_reply,
        "serial": protocol.is_serial_reply,
        "manufactured": protocol.is_manufactured_reply,
        "valve_config": protocol.is_valve_config_reply,
        "usage": protocol.is_last_usage_reply,
        "outlet_1": lambda p: protocol.is_outlet_config_reply(p, 1),
        "outlet_2": lambda p: protocol.is_outlet_config_reply(p, 2),
        "preset_1": lambda p: protocol.is_preset_reply(p, 1),
        "ack": protocol.is_command_reply,
    }
    for expected, test in tests.items():
        accepted = {name for name, reply in replies.items() if test(reply)}
        assert accepted == {expected}, expected


def test_preset_reply_shape() -> None:
    assert protocol.is_preset_reply(PRESET_EMPTY, 3)
    assert protocol.is_preset_reply(PRESET_INVALID, 4)  # "no such slot" answers any slot
    assert not protocol.is_preset_reply(PRESET_BATH, 1)  # another slot's reply
    assert not protocol.is_command_reply(b"\x80")
    assert protocol.is_command_reply(b"\x01\x00")
    assert not protocol.is_command_reply(b"\x01\x00\x00")
    assert not protocol.is_last_usage_reply(b"\x00\x01\x01\x00\x08\x00")


# --- outlet configuration ---------------------------------------------------------------------


def test_outlet_config() -> None:
    config = protocol.decode_outlet_config(OUTLET_1_CONFIG, 1)
    assert config.kind is OutletKind.HAND_SHOWER
    assert config.outlet_type == 1
    assert config.max_temperature == 45.0
    assert config.min_temperature == 30.0
    assert config.default_temperature == 38.0
    assert (config.min_flow, config.default_flow, config.max_flow) == (4.0, 12.0, 16.0)
    assert config.max_duration == 1800
    changed = protocol.decode_outlet_config(OUTLET_2_CONFIG, 2)
    assert changed.max_temperature == 46.0
    assert changed.default_flow == 14.0
    assert changed.max_duration == 900


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (1, OutletKind.HAND_SHOWER),
        (21, OutletKind.BATH_FILLER),
        (23, OutletKind.BATH_FILLER),
        (31, OutletKind.OVERHEAD),
        (0, OutletKind.UNKNOWN),
        (62, OutletKind.UNKNOWN),
    ],
)
def test_outlet_kinds(value: int, kind: OutletKind) -> None:
    assert OutletKind.from_type(value) is kind


def test_outlet_count() -> None:
    assert protocol.decode_outlet_count(VALVE_CONFIG) == 2
    with pytest.raises(ProtocolError, match="valve configuration"):
        protocol.decode_outlet_count(b"\x02")
    with pytest.raises(ProtocolError, match="reports 0 outlets"):
        protocol.decode_outlet_count(b"\x00\x00\x00")
    with pytest.raises(ProtocolError, match="reports 4 outlets"):
        protocol.decode_outlet_count(b"\x00\x00\x04")


def test_outlet_config_refuses_another_outlets_reply() -> None:
    with pytest.raises(ProtocolError, match="asked for outlet 2"):
        protocol.decode_outlet_config(OUTLET_1_CONFIG, 2)
    with pytest.raises(ProtocolError, match="outlet configuration"):
        protocol.decode_outlet_config(OUTLET_1_CONFIG[:10], 1)


# --- presets ----------------------------------------------------------------------------------


def test_timed_preset() -> None:
    preset = protocol.decode_preset(PRESET_SHOWER, 1)
    assert preset is not None
    assert preset.name == "Default"
    assert preset.outlets is Outlet.SECOND
    assert preset.temperature == 38.0
    assert preset.flow == 22.0
    assert preset.duration == 1800
    assert preset.volume is None


def test_volume_preset() -> None:
    preset = protocol.decode_preset(PRESET_BATH, 2)
    assert preset is not None
    assert preset.name == "Default Bathfill"
    assert preset.outlets is Outlet.FIRST
    assert preset.temperature == 42.0
    assert preset.volume == 50
    assert preset.duration is None


@pytest.mark.parametrize(("recorded", "slot"), [(PRESET_SHOWER, 1), (PRESET_BATH, 2)])
def test_presets_re_encode_to_the_bytes_the_valve_sent(recorded: bytes, slot: int) -> None:
    preset = protocol.decode_preset(recorded, slot)
    assert preset is not None
    assert protocol.encode_preset(preset) == recorded
    assert protocol.write_preset_request(preset).opcode == Opcode.WRITE_PRESET


def test_a_name_of_thirty_one_characters_survives() -> None:
    """The factory name is exactly 16 long, so only a longer one proves the field's width."""
    name = "Evening bath for the children 1"
    assert len(name) == 31
    preset = Preset(4, name, Outlet.FIRST | Outlet.SECOND, 40.5, 18.0, volume=65)
    encoded = protocol.encode_preset(preset)
    assert len(encoded) == 61
    decoded = protocol.decode_preset(encoded, 4)
    assert decoded == replace(preset, raw=encoded)
    assert encoded[32] == 0  # the thirty-second byte of the name field stays NUL


def test_empty_and_invalid_slots() -> None:
    assert protocol.decode_preset(PRESET_EMPTY, 3) is None
    assert protocol.decode_preset(PRESET_INVALID, 0) is None
    with pytest.raises(ProtocolError, match="preset reply"):
        protocol.decode_preset(PRESET_EMPTY[:20], 3)


def test_a_reply_about_another_slot_is_not_this_slots_preset() -> None:
    assert protocol.decode_preset(PRESET_BATH, 4) is None


def test_delete_preset_writes_the_slot_and_zeroes() -> None:
    assert protocol.delete_preset_request(3).payload == PRESET_EMPTY


def test_encode_preset_validates() -> None:
    good = Preset(3, "Evening", Outlet.FIRST, 40.5, 18.0, duration=600)
    assert protocol.decode_preset(protocol.encode_preset(good), 3) == replace(
        good, raw=protocol.encode_preset(good)
    )
    for slot in (0, -1, 10, 99):
        with pytest.raises(ValueError, match="slot"):
            protocol.encode_preset(replace(good, slot=slot))
    with pytest.raises(ValueError, match="outlet"):
        protocol.encode_preset(replace(good, outlets=Outlet.NONE))
    with pytest.raises(ValueError, match="ASCII"):
        protocol.encode_preset(replace(good, name="Café"))
    for name in ("x" * 32, "", "tab\there"):
        with pytest.raises(ValueError, match="characters"):
            protocol.encode_preset(replace(good, name=name))
    with pytest.raises(UnsafeValue):
        protocol.encode_preset(replace(good, temperature=60.0))
    with pytest.raises(UnsafeValue, match="flow"):
        protocol.encode_preset(replace(good, flow=26.0))
    with pytest.raises(UnsafeValue, match="above zero"):
        protocol.encode_preset(replace(good, flow=0.0))


@pytest.mark.parametrize(
    ("duration", "volume"),
    [
        (None, None),
        (600, 20),
        (0, None),
        (None, 0),
        (0, 0),
        (70000, None),
        (None, 70000),
        (-1, None),
        (90.5, None),
        (True, None),
    ],
)
def test_a_preset_needs_exactly_one_real_bound(duration: object, volume: object) -> None:
    """A preset with no time and no volume would run without limit."""
    preset = Preset(3, "Evening", Outlet.FIRST, 40.5, 18.0, duration=duration, volume=volume)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"either|out of range"):
        protocol.encode_preset(preset)


# --- identity and usage -----------------------------------------------------------------------


def test_text_fields() -> None:
    assert protocol.decode_text(NAME) == "Bathroom"
    assert protocol.decode_text(b"  Bathroom \x00\x00") == "Bathroom"
    assert protocol.decode_text(bytes(16)) is None
    assert protocol.decode_text(b"\xff\x01abc") is None


def test_serial_number() -> None:
    assert protocol.decode_serial(SERIAL) == "3140090125121900"
    assert protocol.decode_serial(SERIAL_BLANK) is None  # never an identity
    assert protocol.decode_serial(bytes(18)) is None
    assert protocol.decode_serial(NAME + bytes(2)) is None  # a name is not a serial


def test_manufactured_agrees_with_the_digits_in_the_serial() -> None:
    made = protocol.decode_manufactured(MANUFACTURED)
    assert made == datetime(2025, 1, 9, 12, 19)  # noqa: DTZ001
    assert made is not None
    assert protocol.decode_serial(SERIAL)[4:14] == made.strftime("%d%m%y%H%M")  # type: ignore[index]


@pytest.mark.parametrize(
    "payload",
    [
        b"\x19\x09",
        b"\x19\x09\x02\xe3",
        b"\x19\x09\xd2\xe3",
        b"\x19\x00\x12\xe3",
        b"\x19\x63\x12\xe3",
        b"\x19\x09\x15\xa0",
        b"\x19\x1f\x22\xe3",
    ],
    ids=[
        "short",
        "month-zero",
        "month-thirteen",
        "day-zero",
        "day-99",
        "minutes-past-a-day",
        "31-february",
    ],
)
def test_implausible_manufacture_dates_are_refused(payload: bytes) -> None:
    assert protocol.decode_manufactured(payload) is None


def test_last_usage() -> None:
    usage = protocol.decode_last_usage(LAST_USAGE)
    assert (usage.duration, usage.volume, usage.temperature) == (10, 1, 44.0)
    with pytest.raises(ProtocolError):
        protocol.decode_last_usage(b"\x00\x01\x01\x00\x08\x00\x57\x55\x83\xff")


def test_a_last_usage_without_a_temperature() -> None:
    assert protocol.decode_last_usage(LAST_USAGE[:5] + b"\xff").temperature is None


def test_the_recorded_device_id_reply_is_kept_for_reference() -> None:
    assert len(DEVICE_ID_EMPTY) == 15
