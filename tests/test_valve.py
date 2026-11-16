"""MiraValve against the simulated valve."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest

from mira_ble import protocol
from mira_ble import valve as valve_module
from mira_ble.exceptions import (
    CommandRejected,
    ConnectionLost,
    MiraError,
    NotConfirmed,
    RequestTimeout,
    UnsafeValue,
)
from mira_ble.frame import Frame
from mira_ble.protocol import Opcode, Outlet, Phase, Preset, State
from mira_ble.testing import (
    NAME,
    OUTLET_1_CONFIG,
    PLATINUM_SHOWER,
    SERIAL_BLANK,
    STATE_RUNNING,
    STOP_FRAME,
    FakeValve,
)
from mira_ble.valve import NO_ANSWER, MiraValve


@pytest.fixture
async def fake() -> FakeValve:
    fake = FakeValve()
    await fake.connect()
    return fake


@pytest.fixture
def valve(fake: FakeValve) -> MiraValve:
    return MiraValve(fake)


def _opcodes(fake: FakeValve) -> list[int]:
    return [frame.opcode for frame in fake.sent]


def _commands(fake: FakeValve) -> list[bytes]:
    return [frame.encode() for frame in fake.sent if frame.opcode != Opcode.READ_STATE]


PAUSE_38C_8LPM = Frame.request(Opcode.SET_OUTLETS, bytes.fromhex("017c2040")).encode().hex(" ")


# --- starting ---------------------------------------------------------------------------------


async def test_start_is_confirmed_by_reading_the_state_back(
    fake: FakeValve, valve: MiraValve
) -> None:
    state = await valve.set_outlets(Outlet.FIRST, temperature=39.0, flow=10.0)
    assert state is not None
    assert state.outlets is Outlet.FIRST
    assert state.target_temperature == 39.0
    assert state.flow_setting == 10.0
    assert _opcodes(fake) == [Opcode.SET_OUTLETS, Opcode.READ_STATE]
    assert valve.transport is fake


async def test_the_fake_reads_the_command_with_its_own_arithmetic(
    fake: FakeValve, valve: MiraValve
) -> None:
    """A frame a real valve accepted must drive the fake to the state it describes."""
    await fake.request(Frame.decode(bytes.fromhex("aa5500ab040186640166")))
    assert (fake.outlets, fake.target_tenths, fake.flow_byte) == (1, 390, 100)


async def test_naming_one_outlet_switches_the_other_off(fake: FakeValve, valve: MiraValve) -> None:
    await valve.set_outlets(Outlet.FIRST | Outlet.SECOND, temperature=38.0, flow=8.0)
    state = await valve.set_outlets(Outlet.SECOND, temperature=38.0, flow=8.0)
    assert state is not None
    assert state.outlets is Outlet.SECOND


async def test_a_valve_that_takes_seconds_to_act_is_still_confirmed(
    fake: FakeValve, valve: MiraValve
) -> None:
    """Outlets have been reported to show several seconds after the write."""
    fake.lag = 6
    state = await valve.set_outlets(Outlet.FIRST, temperature=38.0, flow=8.0)
    assert state is not None
    assert state.outlets is Outlet.FIRST
    assert _opcodes(fake).count(Opcode.READ_STATE) == 7


async def test_no_target_passes_through_without_heating(fake: FakeValve, valve: MiraValve) -> None:
    state = await valve.set_outlets(Outlet.FIRST, temperature=None, flow=8.0)
    assert state is not None
    assert state.target_temperature is None
    assert fake.target_tenths == 0


async def test_unconfirmed_commands_do_not_read_back(fake: FakeValve, valve: MiraValve) -> None:
    assert await valve.set_outlets(Outlet.FIRST, temperature=38.0, flow=8.0, confirm=False) is None
    assert await valve.stop(confirm=False) is None
    assert await valve.run_preset(1, confirm=False) is None
    assert Opcode.READ_STATE not in _opcodes(fake)


async def test_a_start_the_valve_ignored_is_not_reported_as_done(
    fake: FakeValve, valve: MiraValve
) -> None:
    fake.ignore_commands = True
    with pytest.raises(NotConfirmed, match=r"outlets \(1,\) was acknowledged") as caught:
        await valve.set_outlets(Outlet.FIRST, temperature=38.0, flow=8.0)
    assert caught.value.state is not None  # what the valve reported, for the caller to act on
    assert not caught.value.state.active


# --- limits -----------------------------------------------------------------------------------


async def test_temperature_limits_are_enforced_before_anything_is_written(
    fake: FakeValve, valve: MiraValve
) -> None:
    with pytest.raises(UnsafeValue, match="above the limit of 45"):
        await valve.set_outlets(Outlet.FIRST, temperature=45.5, flow=8.0)
    with pytest.raises(UnsafeValue, match="above the limit of 41"):
        await valve.set_outlets(Outlet.FIRST, temperature=42.0, flow=8.0, max_temperature=41.0)
    hot = MiraValve(fake, max_temperature=60.0)
    assert hot.max_temperature == 48.0
    with pytest.raises(UnsafeValue):
        await hot.set_outlets(Outlet.FIRST, temperature=48.5, flow=8.0)
    # A per-call limit cannot raise the client's own.
    with pytest.raises(UnsafeValue, match="above the limit of 45"):
        await valve.set_outlets(Outlet.FIRST, temperature=46.0, flow=8.0, max_temperature=48.0)
    assert fake.sent == []


async def test_a_limit_that_is_not_a_number_does_not_switch_the_limit_off(
    fake: FakeValve, valve: MiraValve
) -> None:
    with pytest.raises(UnsafeValue, match="must be a number"):
        MiraValve(fake, max_temperature=float("nan"))
    with pytest.raises(UnsafeValue, match="above the limit"):
        await valve.set_outlets(
            Outlet.FIRST, temperature=44.0, flow=8.0, max_temperature=float("nan")
        )
    with pytest.raises(UnsafeValue):
        await valve.set_outlets(Outlet.FIRST, temperature=float("nan"), flow=8.0)
    assert fake.sent == []


# --- stopping ---------------------------------------------------------------------------------


async def test_stop_pauses_with_the_valves_own_settings_and_needs_two_dry_reads(
    fake: FakeValve, valve: MiraValve
) -> None:
    """Measured on a Platinum: the app's pause stops the water and keeps the settings."""
    await valve.set_outlets(Outlet.FIRST, temperature=38.0, flow=8.0)
    fake.volume = 3
    fake.sent.clear()
    state = await valve.stop()
    assert state is not None
    assert not state.running
    assert state.held
    assert (state.target_temperature, state.flow_setting, state.volume) == (38.0, 8.0, 3)
    assert fake.sent[1].encode().hex(" ") == PAUSE_38C_8LPM
    assert _opcodes(fake) == [
        Opcode.READ_STATE,
        Opcode.SET_OUTLETS,
        Opcode.READ_STATE,
        Opcode.READ_STATE,
    ]
    fake.rest()
    assert not (await valve.read_state()).active


def test_the_pause_copies_the_recorded_state() -> None:
    frame = protocol.pause_request(protocol.decode_state(PLATINUM_SHOWER))
    assert frame.encode().hex(" ") == "aa 55 00 ab 04 01 90 20 40 61"  # as sent to the valve
    with pytest.raises(ValueError, match="decoded"):
        protocol.pause_request(replace(protocol.decode_state(PLATINUM_SHOWER), raw=b""))


async def test_a_refused_pause_falls_back_to_the_all_zero_stop(
    fake: FakeValve, valve: MiraValve
) -> None:
    await valve.set_outlets(Outlet.FIRST, temperature=38.0, flow=8.0)
    fake.pause_rejected = True
    fake.sent.clear()
    state = await valve.stop()
    assert state is not None
    assert not state.active
    assert _commands(fake)[-1] == STOP_FRAME


async def test_an_ignored_pause_falls_back_to_the_all_zero_stop(
    fake: FakeValve, valve: MiraValve
) -> None:
    await valve.set_outlets(Outlet.FIRST, temperature=38.0, flow=8.0)
    fake.pause_ignored = True
    fake.sent.clear()
    state = await valve.stop()
    assert state is not None
    assert not state.active
    assert [frame.hex(" ") for frame in _commands(fake)] == [PAUSE_38C_8LPM, STOP_FRAME.hex(" ")]


async def test_a_state_that_cannot_be_read_does_not_hold_up_the_stop(
    fake: FakeValve, valve: MiraValve
) -> None:
    await valve.set_outlets(Outlet.FIRST, temperature=38.0, flow=8.0)
    fake.reject.add(Opcode.READ_STATE)
    fake.sent.clear()
    with pytest.raises(MiraError):
        await valve.stop()
    assert STOP_FRAME in _commands(fake)  # the all-zero stop still went out


async def test_a_state_read_that_goes_unanswered_does_not_hold_up_the_stop(
    fake: FakeValve, valve: MiraValve
) -> None:
    """Silence closes the link; the stop reconnects and sends the all-zero command."""
    await valve.set_outlets(Outlet.FIRST, temperature=38.0, flow=8.0)
    fake.ignore.add(Opcode.READ_STATE)
    fake.sent.clear()
    with pytest.raises(RequestTimeout):
        await valve.stop()
    assert STOP_FRAME in _commands(fake)
    assert fake.outlets == 0


async def test_stop_without_confirmation_sends_only_the_all_zero_stop(
    fake: FakeValve, valve: MiraValve
) -> None:
    await valve.set_outlets(Outlet.FIRST, temperature=38.0, flow=8.0)
    fake.sent.clear()
    assert await valve.stop(confirm=False) is None
    assert [frame.encode() for frame in fake.sent] == [STOP_FRAME]


async def test_a_stop_the_valve_acknowledged_but_ignored_is_never_confirmed(
    fake: FakeValve, valve: MiraValve
) -> None:
    await valve.set_outlets(Outlet.FIRST, temperature=38.0, flow=8.0)
    fake.ignore_commands = True
    with pytest.raises(NotConfirmed, match="stop was acknowledged") as caught:
        await valve.stop()
    assert caught.value.state is not None
    assert caught.value.state.active
    assert fake.outlets == 1


async def test_a_preset_warms_up_holds_and_runs_on_the_second_command(
    fake: FakeValve, valve: MiraValve
) -> None:
    """Measured on a Mira Platinum."""
    fake.warm_up = 3
    state = await valve.run_preset(1)
    assert state is not None
    assert state.phase is Phase.WARMING_UP
    assert state.outlets is Outlet.SECOND  # the recorded preset's outlet
    for _ in range(3):
        state = await valve.read_state()
    assert state.phase is Phase.PAUSED
    assert not state.running
    state = await valve.run_preset(1, before=state)
    assert state is not None
    assert state.phase is Phase.PRESET
    assert (state.preset, state.time_remaining) == (1, 1800)


async def test_an_outlet_command_during_the_hold_starts_a_plain_run(
    fake: FakeValve, valve: MiraValve
) -> None:
    fake.warm_up = 1
    await valve.run_preset(1)
    assert (await valve.read_state()).phase is Phase.PAUSED
    state = await valve.set_outlets(Outlet.FIRST, temperature=38.0, flow=8.0)
    assert state is not None
    assert state.phase is Phase.RUNNING
    assert (state.preset, state.time_remaining) == (0, None)


async def test_run_preset_through_sends_the_command_again_at_the_hold(
    fake: FakeValve, valve: MiraValve
) -> None:
    fake.warm_up = 4
    state = await valve.run_preset_through(1)
    assert state.phase is Phase.PRESET
    assert state.preset == 1
    assert _commands(fake) == [protocol.run_preset_request(1).encode()] * 2


async def test_run_preset_through_waits_out_the_water_draining_after_the_warm_up(
    fake: FakeValve, valve: MiraValve
) -> None:
    """Found on a Mira Platinum: held, no outlet bit, flow still read for two seconds. That
    was taken for a valve running the preset with no warm-up, and the second command never
    went out."""
    fake.warm_up, fake.drain = 3, 6
    state = await valve.run_preset_through(1)
    assert state.phase is Phase.PRESET
    assert _commands(fake) == [protocol.run_preset_request(1).encode()] * 2


async def test_run_preset_through_from_the_hold_needs_one_command(
    fake: FakeValve, valve: MiraValve
) -> None:
    fake.warm_up = 1
    await valve.run_preset(1)
    before = await valve.read_state()
    fake.sent.clear()
    state = await valve.run_preset_through(1, before=before)
    assert state.phase is Phase.PRESET
    assert _commands(fake) == [protocol.run_preset_request(1).encode()]


async def test_run_preset_through_leaves_a_valve_with_no_warm_up_alone(
    fake: FakeValve, valve: MiraValve
) -> None:
    state = await valve.run_preset_through(2)
    assert state.phase is Phase.RUNNING
    assert _commands(fake) == [protocol.run_preset_request(2).encode()]


async def test_run_preset_through_gives_up_on_a_warm_up_that_never_ends(
    fake: FakeValve, valve: MiraValve
) -> None:
    fake.warm_up = 10**6
    with pytest.raises(NotConfirmed, match="still warming up") as caught:
        await valve.run_preset_through(1, timeout=0.01)
    assert caught.value.state is not None
    assert caught.value.state.running  # the caller must stop it


async def test_run_preset_through_reports_a_preset_that_ended_in_its_warm_up(
    fake: FakeValve, valve: MiraValve
) -> None:
    """Someone stopped it at the wall, or the hold timed out between two reads."""
    fake.warm_up = 5
    real = valve.run_preset

    async def start_then_rest(slot: int, **kwargs: object) -> State | None:
        state = await real(slot, **kwargs)  # type: ignore[arg-type]
        fake.rest()
        return await valve.read_state() if state else None

    with (
        patch.object(valve, "run_preset", start_then_rest),
        pytest.raises(NotConfirmed, match="ended before"),
    ):
        await valve.run_preset_through(1)


async def test_a_preset_with_no_outlet_bit_is_not_taken_for_stopped(
    fake: FakeValve, valve: MiraValve
) -> None:
    """Water measured flowing with the held bit and no outlet bit is running, not stopped."""
    fake.hidden_outlet = True
    state = await valve.run_preset(1)
    assert state is not None
    assert state.running
    assert state.outlets is Outlet.NONE
    assert state.held


async def test_when_the_zero_stop_does_nothing_the_preset_stop_is_tried(
    fake: FakeValve, valve: MiraValve
) -> None:
    """Where the all-zero stop does nothing, "run preset 0" is the other stop."""
    fake.hidden_outlet = fake.zero_stop_ignored = fake.pause_ignored = True
    await valve.run_preset(1)
    fake.sent.clear()
    state = await valve.stop()
    assert state is not None
    assert not state.active
    assert not fake.active
    assert _commands(fake)[1:] == [STOP_FRAME, protocol.preset_stop_request().encode()]


async def test_a_valve_without_the_preset_stop_reports_the_unconfirmed_stop(
    fake: FakeValve, valve: MiraValve
) -> None:
    await valve.set_outlets(Outlet.FIRST, temperature=38.0, flow=8.0)
    fake.zero_stop_ignored = fake.pause_ignored = True  # and it rejects "run preset 0"
    with pytest.raises(NotConfirmed, match="stop was acknowledged") as caught:
        await valve.stop()
    assert isinstance(caught.value.__cause__, CommandRejected)


async def test_a_stop_that_takes_a_moment_is_confirmed(fake: FakeValve, valve: MiraValve) -> None:
    await valve.set_outlets(Outlet.FIRST, temperature=38.0, flow=8.0)
    fake.lag = 3
    state = await valve.stop()
    assert state is not None
    assert not state.running


# --- presets ----------------------------------------------------------------------------------


async def test_run_preset(fake: FakeValve, valve: MiraValve) -> None:
    state = await valve.run_preset(2)
    assert state is not None
    assert state.outlets is Outlet.FIRST
    assert state.target_temperature == 42.0


async def test_a_held_bit_that_was_already_set_does_not_confirm_a_preset(
    fake: FakeValve, valve: MiraValve
) -> None:
    """The vendor app's own stop can leave the held bit set; an ignored preset must fail."""
    fake.held = True
    fake.ignore_commands = True
    before = await valve.read_state()
    with pytest.raises(NotConfirmed, match="preset 1"):
        await valve.run_preset(1, before=before)


async def test_a_preset_the_valve_ignored_is_not_confirmed(
    fake: FakeValve, valve: MiraValve
) -> None:
    fake.ignore_commands = True
    with pytest.raises(NotConfirmed, match="preset 1"):
        await valve.run_preset(1)


async def test_write_and_delete_preset(fake: FakeValve, valve: MiraValve) -> None:
    preset = Preset(3, "Evening Bath", Outlet.FIRST, 40.5, 18.0, volume=65)
    stored = await valve.write_preset(preset)
    assert stored.name == "Evening Bath"
    assert stored.volume == 65
    assert 3 in fake.presets
    await valve.delete_preset(3)
    assert 3 not in fake.presets


async def test_a_written_preset_is_compared_by_its_fields(
    fake: FakeValve, valve: MiraValve
) -> None:
    """Bytes nobody decodes may differ; a write that stored the fields has worked."""
    preset = Preset(3, "Evening Bath", Outlet.FIRST, 40.5, 18.0, volume=65)
    original = fake._answer

    def padded(frame: Frame) -> bytes:
        reply = original(frame)
        if frame.opcode == Opcode.READ_PRESET and frame.payload == b"\x03" and any(reply[1:]):
            return reply[:60] + b"\x07"
        return reply

    with patch.object(FakeValve, "_answer", lambda _self, frame: padded(frame)):
        assert (await valve.write_preset(preset)).name == "Evening Bath"


async def test_preset_writes_are_checked(fake: FakeValve, valve: MiraValve) -> None:
    preset = Preset(3, "Evening Bath", Outlet.FIRST, 40.5, 18.0, volume=65)
    with pytest.raises(UnsafeValue):
        await valve.write_preset(Preset(3, "Hot", Outlet.FIRST, 47.0, 18.0, volume=65))
    with pytest.raises(ValueError, match="out of range"):
        await valve.write_preset(Preset(3, "Endless", Outlet.FIRST, 40.0, 16.0, duration=0))
    fake.ignore_commands = True
    with pytest.raises(NotConfirmed, match="did not read back"):
        await valve.write_preset(preset)
    with pytest.raises(NotConfirmed, match="still stored"):
        await valve.delete_preset(1)


# --- reads ------------------------------------------------------------------------------------


async def test_reads(fake: FakeValve, valve: MiraValve) -> None:
    assert (await valve.read_state()).water_temperature == 25.6
    assert await valve.read_outlet_count() == 2
    assert (await valve.read_outlet_config(2)).max_temperature == 46.0
    assert (await valve.read_last_usage()).volume == 1
    identity = await valve.read_identity()
    assert identity.name == "Bathroom"
    assert identity.serial_number == "3140090125121900"
    assert identity.manufactured is not None
    assert [preset.slot for preset in await valve.read_presets()] == [1, 2]
    assert await valve.read_preset(3) is None


async def test_a_blank_serial_is_no_identity(fake: FakeValve, valve: MiraValve) -> None:
    fake.serial = SERIAL_BLANK
    assert (await valve.read_identity()).serial_number is None


async def test_identity_survives_a_valve_that_refuses_parts_of_it(
    fake: FakeValve, valve: MiraValve
) -> None:
    fake.reject.update({Opcode.READ_NAME, Opcode.READ_MANUFACTURED})
    errors: dict[str, str] = {}
    identity = await valve.read_identity(errors)
    assert (identity.name, identity.manufactured) == (None, None)
    assert identity.serial_number == "3140090125121900"
    assert set(errors) == {"name", "manufactured"}


async def test_identity_survives_a_valve_that_stays_silent_on_parts_of_it(
    fake: FakeValve, valve: MiraValve
) -> None:
    """Some valves ignore a request instead of refusing it; the link is reopened."""
    fake.ignore.add(Opcode.READ_NAME)
    errors: dict[str, str] = {}
    identity = await valve.read_identity(errors)
    assert identity.name is None
    assert identity.serial_number == "3140090125121900"
    assert errors == {"name": NO_ANSWER}
    assert fake.connects == 2


async def test_a_stray_frame_does_not_shift_the_identity_reads(
    fake: FakeValve, valve: MiraValve
) -> None:
    """One unsolicited state frame once turned the room name into the serial number."""
    fake.unsolicited.append(STATE_RUNNING)
    identity = await valve.read_identity()
    assert identity.name == "Bathroom"
    assert identity.serial_number == "3140090125121900"


async def test_a_late_name_reply_is_not_read_as_the_serial(
    fake: FakeValve, valve: MiraValve
) -> None:
    fake.ignore.add(Opcode.READ_NAME)
    fake.unsolicited.clear()
    errors: dict[str, str] = {}
    await valve.read_identity(errors)
    fake.unsolicited.append(NAME)  # the name turns up while the serial is being asked for
    payload = await valve._read(protocol.read_serial_request(), protocol.is_serial_reply)
    assert protocol.decode_serial(payload) == "3140090125121900"


async def test_probe_finds_outlets_and_presets(fake: FakeValve, valve: MiraValve) -> None:
    capabilities = await valve.probe()
    assert capabilities.outlet_numbers == (1, 2)
    assert [preset.name for preset in capabilities.presets] == ["Default", "Default Bathfill"]
    assert capabilities.errors == {}
    assert capabilities.outlet(2) is not None
    assert capabilities.outlet(3) is None
    assert capabilities.max_temperature(Outlet.FIRST) == 45.0
    assert capabilities.max_temperature(Outlet.FIRST | Outlet.SECOND) == 45.0
    assert capabilities.max_temperature(Outlet.THIRD) is None
    assert capabilities.max_temperature(Outlet.FIRST | Outlet.THIRD) is None  # one is unknown
    assert capabilities.max_temperature(Outlet.NONE) is None
    assert not capabilities.failed("outlet")


async def test_probe_records_what_the_valve_refuses(fake: FakeValve, valve: MiraValve) -> None:
    fake.reject.update({Opcode.READ_OUTLET_CONFIG, Opcode.READ_PRESET})
    capabilities = await valve.probe()
    assert capabilities.outlets == ()
    assert capabilities.presets == ()
    assert set(capabilities.errors) == {"outlet_1", "outlet_2", "preset_1"}
    assert capabilities.failed("outlet")
    assert capabilities.failed("preset")


async def test_probe_survives_a_valve_silent_on_presets(fake: FakeValve, valve: MiraValve) -> None:
    """A Platinum was seen to answer preset reads only while running."""
    fake.ignore.add(Opcode.READ_PRESET)
    capabilities = await valve.probe()
    assert capabilities.outlet_numbers == (1, 2)
    assert capabilities.presets == ()
    assert capabilities.errors == {"preset_1": NO_ANSWER}  # one wait, not one per slot
    assert fake.is_connected


async def test_probe_without_an_outlet_count_keeps_only_outlets_with_a_type(
    fake: FakeValve, valve: MiraValve
) -> None:
    """Naming an outlet a valve lacks makes it ignore a whole command."""
    fake.reject.add(Opcode.READ_VALVE_CONFIG)
    untyped = bytearray(OUTLET_1_CONFIG)
    untyped[0], untyped[1] = 2, 0  # a third outlet with limits but no type
    fake.outlet_configs[3] = bytes(untyped)
    capabilities = await valve.probe()
    assert capabilities.outlet_numbers == (1, 2)
    assert set(capabilities.errors) == {"outlet_count"}


async def test_a_lost_connection_is_not_swallowed_by_the_optional_reads(
    fake: FakeValve, valve: MiraValve
) -> None:
    await fake.disconnect()
    with pytest.raises(ConnectionLost):
        await valve.probe()


async def test_an_unanswered_state_read_is_an_error(fake: FakeValve, valve: MiraValve) -> None:
    fake.silent = True
    with pytest.raises(RequestTimeout):
        await valve.read_state()


async def test_a_rejected_command_raises(fake: FakeValve, valve: MiraValve) -> None:
    fake.reject.add(Opcode.SET_OUTLETS)
    with pytest.raises(CommandRejected) as caught:
        await valve.stop()
    assert caught.value.opcode == Opcode.SET_OUTLETS


async def test_the_simulated_valve_can_refuse_to_connect() -> None:
    fake = FakeValve(connect_error=RequestTimeout("no answer"))
    with pytest.raises(RequestTimeout):
        await fake.connect()
    assert not fake.is_connected
    assert fake.connects == 1


async def test_the_simulated_valve_reports_what_it_was_left_at(fake: FakeValve) -> None:
    fake.idle_target_tenths, fake.idle_flow_byte = 400, 0x22
    state = protocol.decode_state(fake.state_payload())
    assert (state.target_temperature, state.flow_setting, state.active) == (40.0, 8.5, False)
    assert valve_module.NO_ANSWER == "no answer"
