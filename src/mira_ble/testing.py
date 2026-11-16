"""Test helpers: a simulated valve, and replies recorded from real ones.

:class:`FakeValve` is a :class:`~mira_ble.transport.Transport` that answers frames, so a
client or an integration can be exercised without hardware. It is a model built from the
recordings and from what other projects report, not the firmware: code that passes against
it is consistent with what is known, never proven on a valve. It reads commands with its own
arithmetic, not the library's decoders, so an encoder fault is not mirrored back as success.

It can be made awkward in the ways real valves are: slow to act (``lag``), silent on some
requests (``ignore``), chatty (``unsolicited``), holding the last user's settings at rest,
or behaving like a Mira Platinum (``platinum``).

The captures come from mira-mode-control (Ryan Shaw, MIT) and ha-mira-mode (Hayden Griffin,
MIT), recorded from current-generation dual outlet valves; each constant says which.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from mira_ble.exceptions import CommandRejected, ConnectionLost, RequestTimeout
from mira_ble.frame import Frame
from mira_ble.transport import ReplyTest


def _hex(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", ""))


# --- Recorded replies (payloads) ----------------------------------------------------------------

STATE_IDLE = _hex("21 20 00 00 00 00 00 00 80 00 00 00 10 00 01 00 00 01")
"""Nothing running; flow rests at 4 L/min, water 25.6 C. (mira-mode-control)"""
STATE_RUNNING = _hex("21 20 00 00 00 12 00 00 00 00 01 a4 32 01 00 f6 12 01")
"""Outlet 1, four seconds after being commanded to 42.0 C at 12.5 L/min. (mira-mode-control)"""
STATE_RUNNING_WARMER = _hex("21 20 00 00 00 1b 00 00 00 00 01 a4 32 01 01 04 1b 01")
"""The same run six seconds later: water 24.6 C to 26.0 C. (mira-mode-control)"""
STATE_STOPPED = _hex("21 20 00 00 00 00 00 00 80 00 00 00 10 00 01 03 00 01")
"""Just after stopping. (mira-mode-control)"""
PLATINUM_REST = _hex("31 20 00 00 00 00 00 00 a0 00 01 90 20 00 00 a1 00 01")
"""A Mira Platinum at rest, Everyday (40.0 C, 8 L/min) selected on the controller. (own capture)"""
PLATINUM_WARM_UP = _hex("31 20 00 01 00 1c 10 00 20 00 01 90 20 01 01 0a 1c 01")
"""The same valve warming up a preset started at the wall: outlet 1, 7 L/min, 1 L so far."""
PLATINUM_READY = _hex("31 20 00 04 00 00 00 00 e0 00 01 90 20 40 01 9a 00 01")
"""Warm-up done: water off, held, waiting for the outlet button; rest follows after 2 min."""
PLATINUM_SHOWER = _hex("31 24 00 02 00 1d 00 00 61 dd 0d 90 20 01 01 92 1d 01")
"""The shower proper after the outlet button: preset 4, 477 s left, 7.25 L/min, 2 L so far."""
PLATINUM_STOPPING = _hex("31 20 00 02 00 1d 00 00 60 00 01 90 20 40 01 9d 1d 01")
"""Two seconds after a stop at the wall: held, no outlet bit, flow not yet zero."""
PLATINUM_PRESET_PICKED = _hex("31 23 00 00 00 00 00 00 a1 2c 01 86 30 00 01 59 00 01")
"""At rest, as preset 3 (5:00) is picked on the wall controller: its number and duration show."""
PLATINUM_PRESET_BY_BLE = _hex("31 26 00 02 00 18 00 00 61 64 0d 7c 18 01 01 80 18 01")
"""Preset 6 proper, started by a second preset command during the hold: 356 s of 360 left."""
PLATINUM_OUTLET_FROM_HOLD = _hex("31 20 00 02 00 1e 00 00 60 00 0d 90 20 01 01 9a 1e 01")
"""A plain run started by an outlet command during the hold: no preset number, no countdown."""
STATE_RAMPING = _hex("21 c0 00 00 00 1e 00 00 00 00 01 c2 30 01 00 fd 1e 01")
"""Outlet 1 just after a start: 12 L/min commanded, 7.5 reached, target 45.0 C. (ha-mira-mode)"""
STATE_TARGET_WITH_FLAGS = _hex("21 20 00 00 00 12 00 00 00 00 0d c2 32 01 00 f6 12 01")
"""A running state whose target bytes carry flag bits above those of 45.0 C.

Constructed, not recorded: ha-mira-mode reports seeing ``0D C2`` and ``05 C2`` in those two
bytes at a 45.0 C target, and this puts the first into a recorded running state."""
STATE_TARGET_WITH_OTHER_FLAGS = (
    STATE_TARGET_WITH_FLAGS[:10] + b"\x05" + STATE_TARGET_WITH_FLAGS[11:]
)
"""The same with ``05 C2``, the other reported form. Constructed."""

OUTLET_1_CONFIG = _hex("00 01 01 01 c2 01 2c 01 7c 40 10 30 07 08 00 00")
"""Outlet 1: 45.0 C maximum, 30.0 minimum, 38.0 default, flow 4 to 16 L/min, default 12,
1800 s. (ha-mira-mode)"""
OUTLET_2_CONFIG = _hex("01 01 01 01 cc 01 2c 01 7c 58 10 38 03 84 00 00")
"""Outlet 2 after setting 46.0 C, 14 L/min and 15 minutes in the vendor app. (ha-mira-mode)"""

PRESET_SHOWER = _hex(
    "01 44 65 66 61 75 6c 74 00 00 00 00 00 00 00 00"
    "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    "00 07 08 00 00 09 7c 58" + " 00" * 21
)
"""Slot 1, a factory preset: "Default", 38.0 C, outlet 2, 30 minutes. (mira-mode-control)"""
PRESET_BATH = _hex(
    "02 44 65 66 61 75 6c 74 20 42 61 74 68 66 69 6c"
    "6c 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    "00 00 00 00 32 05 a4 58" + " 00" * 21
)
"""Slot 2: "Default Bathfill", 42.0 C, outlet 1, 50 litres. (mira-mode-control)"""
PRESET_EMPTY = _hex("03" + " 00" * 60)
"""Slot 3, never configured: the slot echoes back and all else is zero. (mira-mode-control)"""
PRESET_INVALID = _hex("ff 20 00 00 00 00 00 00 80 00 00 00 10 00 01 01 00 01" + " 00" * 43)
"""Slot 0, which is not a slot: the valve echoes 0xFF instead. (mira-mode-control)"""

VALVE_CONFIG = _hex("00 00 02")
"""Opcode 0x1A: two outlets. Built from the app's reading of the reply (the count is its third
byte); the other two bytes are not known. Not a recording."""

NAME = _hex("42 61 74 68 72 6f 6f 6d 00 00 00 00 00 00 00 00")
"""Opcode 0x44: "Bathroom". (mira-mode-control)"""
SERIAL = b"3140090125121900" + bytes(2)
"""Opcode 0x40: the layout of a real reply, with the last digits of the serial changed.

Characters 5 to 14 are day, month, year, hour and minute of manufacture, the same as
:data:`MANUFACTURED` decodes to; that agreement is what confirms the date layout.
(mira-mode-control)"""
SERIAL_BLANK = b"0" * 18
"""What a valve that was never given a serial reports (app); it is no identity."""
DEVICE_ID_EMPTY = _hex("02" + " 00" * 14)
"""Opcode 0x42 on a valve with no device ID set; the only recorded reply to it. Not decoded
by this library. (mira-mode-control)"""
MANUFACTURED = _hex("19 09 12 e3")
"""Opcode 0x41: 9 January 2025 at 12:19. (mira-mode-control)"""
LAST_USAGE = _hex("02 00 0a 00 01 58")
"""Opcode 0x5C ``02 00``: 10 s, 1 litre, 44.0 C. (ha-mira-mode)"""

ACK_FRAME = _hex("aa 55 01 01 01 01 fd")
"""The whole frame a valve sends to acknowledge a command. (mira-mode-control)"""
STOP_FRAME = _hex("aa 55 00 ab 04 00 00 00 00 52")
"""The whole frame that stops every outlet, as logged against a real valve. (mira-mode-control)"""
STOP_KEEPING_TEMPERATURE_FRAME = _hex("aa 55 00 ab 04 01 86 00 00 cb")
"""Another frame confirmed to stop a real valve: no outlets, the temperature left in place.
(mira-mode-control)"""
SET_39C_25LPM_OUTLET_1 = _hex("aa 55 00 ab 04 01 86 64 01 66")
"""A frame that ran outlet 1 of a real valve at 39.0 C and full flow. (mira-mode-control)"""
APP_SET_38C_16LPM_OUTLET_2 = _hex("aa 55 00 ab 04 01 7c 40 02 93")
"""A frame the vendor app sent: 38.0 C, 16 L/min, outlet 2. (Mira-Activate-HA, HCI capture)"""
APP_SET_39C_8LPM_OUTLET_2 = _hex("aa 55 00 ab 04 01 86 20 02 a9")
"""A frame the vendor app sent: 39.0 C, 8 L/min, outlet 2. (Mira-Activate-HA, HCI capture)"""
APP_SET_38C_16LPM_BOTH = _hex("aa 55 00 ab 04 01 7c 40 03 92")
"""A frame the vendor app sent: 38.0 C, 16 L/min, both outlets. (Mira-Activate-HA)"""


# --- Simulated valve ----------------------------------------------------------------------------

_OP_VALVE_CONFIG, _OP_OUTLET_CONFIG, _OP_STATE = 0x1A, 0x1B, 0x2B
_OP_SERIAL, _OP_MANUFACTURED, _OP_NAME = 0x40, 0x41, 0x44
_OP_USAGE, _OP_PRESET, _OP_SET_OUTLETS = 0x5C, 0x5D, 0xAB
_OP_RUN_PRESET, _OP_WRITE_PRESET = 0xB1, 0xDD
_OK, _REJECTED = b"\x01", b"\x80"
_PRESET_SIZE, _OUTLET_CONFIG_SIZE = 61, 16


@dataclass(slots=True)
class FakeValve:
    """A :class:`~mira_ble.transport.Transport` standing in for a valve.

    Its state changes when it is commanded, so a command followed by a read sees the result.

    Knobs, all off by default:

    * ``lag``: state reads that still show the old state after a command, as valves that
      take seconds to act do;
    * ``ignore``: opcodes it stays silent on (the link is then dropped, as the real
      transport does); ``silent`` ignores everything;
    * ``reject``: opcodes it refuses with the failure status;
    * ``ignore_commands``: acknowledge commands without acting on them;
    * ``zero_stop_ignored``: acknowledge the all-zero stop without acting on it;
    * ``unsolicited``: payloads it pushes ahead of its next reply;
    * ``idle_target_tenths`` / ``idle_flow_byte``: what it reports at rest (some valves keep
      the last user's values there);
    * ``hidden_outlet``: a running preset shows the held bit and measured flow with no outlet
      bit, and "run preset 0" stops it (reported for one valve, not reproduced since);
    * ``pause_ignored`` / ``pause_rejected``: what it does with the app's pause (an outlet
      command with only the hold bit). By default the water stops and the valve stays held,
      as a Mira Platinum was measured to for two minutes; :meth:`rest` ends the hold;
    * ``volume``: state bytes 2-3, the litres a Platinum counts while it runs;
    * ``warm_up``: how many state reads a preset's warm-up lasts. Above zero the valve runs
      presets as a Mira Platinum was measured to: water runs for the warm-up, then it holds;
      the same preset command during the hold starts the preset proper, with its number and
      its countdown in the state; an outlet command during the hold starts a plain run;
    * ``drain``: how many state reads still show flow after a warm-up ends in the hold (a
      Platinum showed two seconds of it, held and with no outlet bit);
    * ``connect_error``: raised by :meth:`connect`.
    """

    outlet_configs: dict[int, bytes] = field(
        default_factory=lambda: {1: OUTLET_1_CONFIG, 2: OUTLET_2_CONFIG}
    )
    presets: dict[int, bytes] = field(default_factory=lambda: {1: PRESET_SHOWER, 2: PRESET_BATH})
    valve_config: bytes = VALVE_CONFIG
    name: bytes = NAME
    serial: bytes = SERIAL
    manufactured: bytes = MANUFACTURED
    last_usage: bytes = LAST_USAGE
    water_temperature: float = 25.6
    error_code: int = 1
    outlets: int = 0
    held: bool = False
    flowing: bool = False
    """Water runs although no outlet bit is set (see ``hidden_outlet``)."""
    volume: int = 0
    warm_up: int = 0
    drain: int = 0
    draining: int = 0
    """State reads left before the flow of a finished warm-up reads zero."""
    warming: int = 0
    """State reads left in the present warm-up."""
    preset_running: int = 0
    remaining: int = 0
    target_tenths: int = 0
    flow_byte: int = 0x10
    idle_target_tenths: int = 0
    idle_flow_byte: int = 0x10
    lag: int = 0
    hidden_outlet: bool = False
    pause_ignored: bool = False
    pause_rejected: bool = False
    connected: bool = False
    link_age: float | None = None
    """Seconds the simulated link has been up; a test sets it to make the link an old one."""
    silent: bool = False
    ignore_commands: bool = False
    zero_stop_ignored: bool = False
    ignore: set[int] = field(default_factory=set)
    reject: set[int] = field(default_factory=set)
    unsolicited: list[bytes] = field(default_factory=list)
    connect_error: Exception | None = None
    source: str | None = None
    """The adapter the simulated connection is said to have used (see ``BleTransport.source``)."""
    sent: list[Frame] = field(default_factory=list)
    last_frames: deque[tuple[str, bytes]] = field(default_factory=lambda: deque(maxlen=48))
    connects: int = 0
    _queued: list[tuple[int, dict[str, int | bool]]] = field(default_factory=list)

    @property
    def is_connected(self) -> bool:
        """True after :meth:`connect`."""
        return self.connected

    @property
    def active(self) -> bool:
        """True while the simulated valve is running or holding a run."""
        return self.running or self.held

    @property
    def running(self) -> bool:
        """True while the simulated valve lets water through."""
        return bool(self.outlets) or self.flowing

    def rest(self) -> None:
        """End a hold, as the valve does by itself after two minutes."""
        self.outlets, self.held, self.flowing, self.volume = 0, False, False, 0
        self.warming, self.preset_running, self.remaining, self.draining = 0, 0, 0, 0

    async def connect(self) -> None:
        """Pretend to connect, or fail as ``connect_error`` says."""
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True
        self.link_age = 0.0

    async def disconnect(self) -> None:
        """Pretend to disconnect."""
        self.link_age = None
        self.connected = False

    async def __aenter__(self) -> FakeValve:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.disconnect()

    def state_payload(self) -> bytes:
        """Return the state reply for the valve's present condition."""
        payload = bytearray(STATE_IDLE)
        active = self.active
        payload[1] = (payload[1] & 0xE0) | self.preset_running
        payload[2:4] = self.volume.to_bytes(2, "big")
        payload[6] = 0x10 if self.warming else 0x00
        payload[8] = 0x00 if active else 0x80
        if self.preset_running:
            payload[8:10] = (0x6000 | self.remaining).to_bytes(2, "big")
        target = self.target_tenths if active else self.idle_target_tenths
        payload[10:12] = target.to_bytes(2, "big")
        payload[12] = self.flow_byte if active else self.idle_flow_byte
        payload[13] = self.outlets | (0x40 if self.held else 0)
        payload[14:16] = round(self.water_temperature * 10).to_bytes(2, "big")
        payload[16] = self.flow_byte if self.running else 0
        payload[17] = self.error_code
        return bytes(payload)

    async def request(
        self, frame: Frame, *, accept: ReplyTest | None = None, timeout: float | None = None
    ) -> Frame:
        """Answer ``frame`` as the valve would, through the same rules as the transport."""
        del timeout  # a simulation answers at once
        if not self.connected:
            raise ConnectionLost("fake valve: not connected")
        self.sent.append(frame)
        self.last_frames.append(("tx", frame.encode()))
        while self.unsolicited:
            stray = self.unsolicited.pop(0)
            self.last_frames.append(("rx", Frame.reply(stray).encode()))
            if accept is None or accept(stray):
                return Frame.reply(stray)  # a stray frame of the expected shape is taken
        if self.silent or frame.opcode in self.ignore:
            return await self._no_answer(frame)
        payload = self._answer(frame)
        reply = Frame.reply(payload)
        self.last_frames.append(("rx", reply.encode()))
        if reply.is_rejection:
            raise CommandRejected(frame.opcode, reply.encode())
        if accept is not None and not accept(payload):
            return await self._no_answer(frame)  # the transport would wait in vain
        return reply

    async def _no_answer(self, frame: Frame) -> Frame:
        self.connected = False
        raise RequestTimeout(f"no reply to {frame.encode().hex(' ')}; nothing received")

    def _apply(self, **changes: int | bool) -> None:
        """Make a command take effect now, or after ``lag`` more state reads."""
        if self.lag:
            self._queued.append((self.lag, changes))
        else:
            self._set(changes)

    def _set(self, changes: dict[str, int | bool]) -> None:
        for name, value in changes.items():
            setattr(self, name, value)

    def _tick(self) -> None:
        """Count a state read against every queued change and against a warm-up."""
        if self.draining:
            self.draining -= 1
            if not self.draining:
                self.flowing = False
        if self.warming:
            self.warming -= 1
            if not self.warming:
                self.outlets, self.held = 0, True
                self.draining, self.flowing = self.drain, bool(self.drain)
        waiting: list[tuple[int, dict[str, int | bool]]] = []
        for reads_left, changes in self._queued:
            if reads_left <= 1:
                self._set(changes)
            else:
                waiting.append((reads_left - 1, changes))
        self._queued = waiting

    def _answer(self, frame: Frame) -> bytes:
        opcode, payload = frame.opcode, frame.payload
        if opcode in self.reject:
            return _REJECTED
        if opcode == _OP_STATE:
            reply = self.state_payload()
            self._tick()
            return reply
        if opcode == _OP_VALVE_CONFIG:
            return self.valve_config
        if opcode == _OP_OUTLET_CONFIG:
            config = self.outlet_configs.get(payload[0] + 1)
            # An outlet the valve does not have: the index echoes back, all else zero.
            return config or bytes((payload[0],)).ljust(_OUTLET_CONFIG_SIZE, b"\x00")
        if opcode == _OP_PRESET:
            slot = payload[0]
            if slot == 0:
                return PRESET_INVALID
            return self.presets.get(slot, bytes((slot,)).ljust(_PRESET_SIZE, b"\x00"))
        if opcode == _OP_NAME:
            return self.name
        if opcode == _OP_SERIAL:
            return self.serial
        if opcode == _OP_MANUFACTURED:
            return self.manufactured
        if opcode == _OP_USAGE:
            return self.last_usage
        if opcode == _OP_SET_OUTLETS:
            return self._set_outlets(payload)
        if opcode == _OP_RUN_PRESET:
            return self._run_preset(payload)
        if opcode == _OP_WRITE_PRESET:
            if len(payload) != _PRESET_SIZE:
                return _REJECTED
            if not self.ignore_commands:
                if any(payload[1:]):
                    self.presets[payload[0]] = bytes(payload)
                else:
                    self.presets.pop(payload[0], None)
            return _OK
        return _REJECTED

    def _set_outlets(self, payload: bytes) -> bytes:
        if len(payload) != 4:
            return _REJECTED
        tenths = ((payload[0] & 0x01) << 8) | payload[1]
        outlets = payload[3] & 0x07
        pause = not outlets and bool(payload[3] & 0x40)
        if pause and self.pause_rejected:
            return _REJECTED
        if self.ignore_commands or (pause and self.pause_ignored):
            return _OK
        if not outlets and not pause and self.zero_stop_ignored:
            return _OK
        if pause:
            self._apply(
                outlets=0,
                held=True,
                flowing=False,
                target_tenths=tenths,
                flow_byte=payload[2],
                warming=0,
                preset_running=0,
            )
            return _OK
        if outlets:
            self._apply(
                outlets=outlets,
                held=False,
                flowing=False,
                target_tenths=tenths,  # zero is "cold": the valve does not keep the old target
                flow_byte=payload[2],
                warming=0,
                preset_running=0,
            )
        else:
            self._apply(outlets=0, held=False, flowing=False, warming=0, preset_running=0)
        return _OK

    def _run_preset(self, payload: bytes) -> bytes:
        slot = payload[0]
        if slot == 0:
            if not self.hidden_outlet:
                return _REJECTED
            if not self.ignore_commands:
                self._apply(outlets=0, held=False, flowing=False)
            return _OK
        stored = self.presets.get(slot)
        if stored is None:
            return _REJECTED
        if self.ignore_commands:
            return _OK
        flags = stored[37]
        changes: dict[str, int | bool] = {
            "target_tenths": ((flags & 0x03) << 8) | stored[38],
            "flow_byte": stored[39],
        }
        if self.hidden_outlet:
            changes |= {"outlets": 0, "held": True, "flowing": True}
        else:
            changes |= {"outlets": (flags >> 2) & 0x07, "held": False, "flowing": False}
            if self.warm_up and self.held and not self.running:
                changes |= {
                    "preset_running": slot,
                    "remaining": int.from_bytes(stored[33:35], "big"),
                    "warming": 0,
                }
            elif self.warm_up:
                changes |= {"warming": self.warm_up, "preset_running": 0}
        self._apply(**changes)
        return _OK
