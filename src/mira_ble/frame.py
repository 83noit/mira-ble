"""The ``AA 55`` frame envelope shared by every command and reply.

::

    AA 55 <channel> <opcode> <length> <payload ...> <checksum>

``channel`` is 0 on requests and 1 on replies. ``length`` counts the payload only. The
checksum is the two's complement of the sum of every preceding byte, so a whole valid frame
sums to zero modulo 256.

A reply does not echo the opcode it answers: its own opcode byte is always ``0x01``, whether
it carries a one-byte acknowledgement or a long read. The transport therefore pairs replies
with requests by order alone, and drops the link when one goes unanswered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from mira_ble.exceptions import ChecksumError, ProtocolError

PREAMBLE: Final = b"\xaa\x55"
CHANNEL_REQUEST: Final = 0x00
CHANNEL_REPLY: Final = 0x01
REPLY_OPCODE: Final = 0x01
"""The opcode byte of every reply, whatever it answers."""
STATUS_OK: Final = 0x01
STATUS_REJECTED: Final = 0x80
"""A one-byte reply payload of this value means the valve refused the command."""
HEADER_LENGTH: Final = 5
MAX_PAYLOAD: Final = 255
"""The length byte's range."""


def checksum(data: bytes) -> int:
    """Return the checksum byte for the frame bytes that precede it."""
    return -sum(data) & 0xFF


@dataclass(frozen=True, slots=True)
class Frame:
    """One decoded frame."""

    channel: int
    opcode: int
    payload: bytes

    @property
    def is_reply(self) -> bool:
        """True for a frame sent by the valve."""
        return self.channel == CHANNEL_REPLY

    @property
    def is_rejection(self) -> bool:
        """True for a one-byte reply with the failure bit set (``0x80`` is what valves send)."""
        return self.is_reply and len(self.payload) == 1 and bool(self.payload[0] & STATUS_REJECTED)

    @property
    def is_ack(self) -> bool:
        """True for a reply whose whole payload is the success status."""
        return self.is_reply and self.payload == bytes((STATUS_OK,))

    @property
    def is_valid_reply(self) -> bool:
        """True for a frame with the channel and opcode every valve reply carries."""
        return self.channel == CHANNEL_REPLY and self.opcode == REPLY_OPCODE

    def encode(self) -> bytes:
        """Return the frame as bytes, checksum included."""
        if len(self.payload) > MAX_PAYLOAD:
            raise ProtocolError(f"payload of {len(self.payload)} bytes does not fit a frame")
        body = PREAMBLE + bytes((self.channel, self.opcode, len(self.payload))) + self.payload
        return body + bytes((checksum(body),))

    @classmethod
    def request(cls, opcode: int, payload: bytes = b"") -> Frame:
        """Build a request frame."""
        return cls(CHANNEL_REQUEST, opcode, bytes(payload))

    @classmethod
    def reply(cls, payload: bytes) -> Frame:
        """Build a reply frame, as the valve sends it (used by tests and fakes)."""
        return cls(CHANNEL_REPLY, REPLY_OPCODE, bytes(payload))

    @classmethod
    def decode(cls, data: bytes) -> Frame:
        """Decode exactly one frame, checking its length and checksum."""
        if len(data) < HEADER_LENGTH + 1:
            raise ProtocolError("frame is too short", data)
        if data[:2] != PREAMBLE:
            raise ProtocolError("frame does not start with AA 55", data)
        length = data[4]
        if len(data) != HEADER_LENGTH + length + 1:
            raise ProtocolError(
                f"frame declares {length} payload bytes but is {len(data)} bytes long", data
            )
        if sum(data) & 0xFF:
            raise ChecksumError("frame checksum does not match", data)
        return cls(data[2], data[3], bytes(data[HEADER_LENGTH:-1]))


class FrameAssembler:
    """Reassemble frames from notification pieces.

    A reply can arrive split over notifications at arbitrary points. The assembler
    resynchronises on the preamble, yields every complete frame with a good checksum, and
    skips past one whose checksum fails.

    A payload can contain ``AA 55`` by chance. If the assembler locks on to such a false
    start whose length byte promises more than is coming, the genuine frame behind it would
    wait for bytes that never arrive; so while a candidate is incomplete, a complete valid
    frame found later in the buffer wins and the bytes before it are dropped.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.discarded = 0
        """Bytes dropped while resynchronising, for diagnostics."""

    def reset(self) -> None:
        """Forget any partial frame."""
        self._buffer.clear()

    @property
    def pending(self) -> bytes:
        """The bytes of an unfinished frame."""
        return bytes(self._buffer)

    def feed(self, data: bytes | bytearray) -> list[bytes]:
        """Add a notification piece and return the frames it completed."""
        self._buffer.extend(data)
        frames: list[bytes] = []
        while True:
            start = self._buffer.find(PREAMBLE)
            if start < 0:
                # Keep a trailing AA: the 55 may be in the next piece.
                keep = 1 if self._buffer.endswith(PREAMBLE[:1]) else 0
                self.discarded += len(self._buffer) - keep
                del self._buffer[: len(self._buffer) - keep]
                return frames
            if start:
                self.discarded += start
                del self._buffer[:start]
            if len(self._buffer) < HEADER_LENGTH:
                return frames
            total = HEADER_LENGTH + self._buffer[4] + 1
            if len(self._buffer) < total:
                later = self._later_frame()
                if later is None:
                    return frames
                self.discarded += later
                del self._buffer[:later]
                continue
            candidate = bytes(self._buffer[:total])
            if sum(candidate) & 0xFF:
                # Not a frame after all: step past this preamble and look again.
                self.discarded += len(PREAMBLE)
                del self._buffer[: len(PREAMBLE)]
                continue
            del self._buffer[:total]
            frames.append(candidate)

    def _later_frame(self) -> int | None:
        """Return the offset of a complete valid frame behind an incomplete candidate."""
        start = self._buffer.find(PREAMBLE, len(PREAMBLE))
        while start >= 0:
            if len(self._buffer) >= start + HEADER_LENGTH:
                total = HEADER_LENGTH + self._buffer[start + 4] + 1
                candidate = self._buffer[start : start + total]
                if len(candidate) == total and not sum(candidate) & 0xFF:
                    return start
            start = self._buffer.find(PREAMBLE, start + 1)
        return None
