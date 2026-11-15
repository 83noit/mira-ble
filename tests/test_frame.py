"""Frame envelope: checksum, encode, decode, reassembly."""

from __future__ import annotations

import pytest

from mira_ble.exceptions import ChecksumError, ProtocolError
from mira_ble.frame import Frame, FrameAssembler, checksum
from mira_ble.testing import (
    ACK_FRAME,
    APP_SET_38C_16LPM_BOTH,
    APP_SET_38C_16LPM_OUTLET_2,
    APP_SET_39C_8LPM_OUTLET_2,
    STATE_RUNNING,
    STOP_FRAME,
)


@pytest.mark.parametrize(
    "frame",
    [
        ACK_FRAME,
        STOP_FRAME,
        APP_SET_38C_16LPM_OUTLET_2,
        APP_SET_39C_8LPM_OUTLET_2,
        APP_SET_38C_16LPM_BOTH,
    ],
)
def test_recorded_frames_sum_to_zero_and_round_trip(frame: bytes) -> None:
    assert sum(frame) & 0xFF == 0
    assert checksum(frame[:-1]) == frame[-1]
    assert Frame.decode(frame).encode() == frame


def test_ack_and_rejection() -> None:
    ack = Frame.decode(ACK_FRAME)
    assert ack.is_reply
    assert ack.is_ack
    assert not ack.is_rejection
    rejection = Frame.reply(b"\x80")
    assert rejection.is_rejection
    assert not rejection.is_ack
    assert not Frame.request(0x2B, b"\x80").is_rejection  # only replies reject


def test_a_long_reply_is_neither_ack_nor_rejection() -> None:
    reply = Frame.reply(STATE_RUNNING)
    assert not reply.is_ack
    assert not reply.is_rejection


def test_decode_refuses_malformed_frames() -> None:
    with pytest.raises(ProtocolError, match="too short"):
        Frame.decode(b"\xaa\x55\x00")
    with pytest.raises(ProtocolError, match="AA 55"):
        Frame.decode(b"\x00" + ACK_FRAME[1:])
    with pytest.raises(ProtocolError, match="declares"):
        Frame.decode(ACK_FRAME + b"\x00")
    broken = bytearray(ACK_FRAME)
    broken[-1] ^= 0xFF
    with pytest.raises(ChecksumError):
        Frame.decode(bytes(broken))


def test_encode_refuses_an_oversized_payload() -> None:
    with pytest.raises(ProtocolError, match="does not fit"):
        Frame.request(0xDD, bytes(256)).encode()
    assert len(Frame.request(0xDD, bytes(255)).encode()) == 261


def test_assembler_joins_pieces_split_anywhere() -> None:
    whole = Frame.reply(STATE_RUNNING).encode()
    for cut in range(1, len(whole)):
        assembler = FrameAssembler()
        assert assembler.feed(whole[:cut]) == []
        assert assembler.pending == whole[:cut]
        assert assembler.feed(whole[cut:]) == [whole]
        assert assembler.pending == b""


def test_assembler_yields_several_frames_from_one_piece() -> None:
    assembler = FrameAssembler()
    assert assembler.feed(ACK_FRAME + ACK_FRAME) == [ACK_FRAME, ACK_FRAME]


def test_assembler_resynchronises_after_noise_and_a_bad_checksum() -> None:
    assembler = FrameAssembler()
    broken = bytearray(ACK_FRAME)
    broken[-1] ^= 0xFF
    assert assembler.feed(b"\x01\x02" + bytes(broken) + ACK_FRAME) == [ACK_FRAME]
    assert assembler.discarded > 0


def test_assembler_keeps_a_split_preamble() -> None:
    assembler = FrameAssembler()
    assert assembler.feed(b"\x00\xaa") == []
    assert assembler.pending == b"\xaa"
    assert assembler.feed(ACK_FRAME[1:]) == [ACK_FRAME]


def test_assembler_reset_forgets_a_partial_frame() -> None:
    assembler = FrameAssembler()
    assembler.feed(ACK_FRAME[:4])
    assembler.reset()
    assert assembler.feed(ACK_FRAME) == [ACK_FRAME]


def test_only_channel_one_opcode_one_is_a_valve_reply() -> None:
    assert Frame.decode(ACK_FRAME).is_valid_reply
    assert not Frame.request(0x2B, b"\x02").is_valid_reply
    assert not Frame(1, 0x2B, b"\x01").is_valid_reply
    assert not Frame(2, 0x01, b"\x01").is_valid_reply


@pytest.mark.parametrize("status", [0x80, 0x81, 0xFF])
def test_a_one_byte_reply_with_the_failure_bit_is_a_rejection(status: int) -> None:
    assert Frame.reply(bytes((status,))).is_rejection


def test_a_long_reply_starting_with_the_failure_bit_is_not_a_rejection() -> None:
    """An invalid preset slot answers 61 bytes starting FF; that is data, not a refusal."""
    assert not Frame.reply(b"\xff" + bytes(60)).is_rejection


def test_a_false_start_with_a_long_length_does_not_swallow_the_next_frame() -> None:
    """AA 55 can occur inside a payload; the genuine frame behind it must still come out."""
    assembler = FrameAssembler()
    false_start = b"\xaa\x55\x01\x01\xf0\x11\x22"  # promises 240 bytes that never come
    assert assembler.feed(false_start + ACK_FRAME) == [ACK_FRAME]
    assert assembler.pending == b""
    assert assembler.feed(ACK_FRAME) == [ACK_FRAME]


def test_a_corrupted_length_byte_costs_only_that_frame() -> None:
    assembler = FrameAssembler()
    whole = Frame.reply(STATE_RUNNING).encode()
    damaged = bytearray(whole)
    damaged[4] = 0x05  # too short: the checksum then fails where the frame "ends"
    assert assembler.feed(bytes(damaged) + ACK_FRAME) == [ACK_FRAME]


def test_the_buffer_does_not_grow_on_junk() -> None:
    assembler = FrameAssembler()
    for _ in range(100):
        assert assembler.feed(b"\x01\x02\x03\x04") == []
    assert len(assembler.pending) <= 1
