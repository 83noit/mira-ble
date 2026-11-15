"""High-level valve client: typed reads, and control commands that are checked.

Control follows these rules, because this drives hot water:

* a target temperature is refused above the caller's limit and above the library's own
  absolute ceiling, before anything is written;
* a command counts as done only when the valve acknowledged it *and* its state shows it,
  within a few seconds, since valves take time to act;
* "stopped" means the valve reports itself fully at rest, twice in a row: no outlet bit, no
  held bit, no run flag. If the usual stop does not get there, the other stop is tried;
* every read except the state is optional: valves differ in what they answer, and some
  ignore a request instead of refusing it;
* nothing here writes configuration, credentials or commissioning state. Preset writes exist
  because presets were measured to round-trip, and are the only persistent write.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Final

from mira_ble import protocol
from mira_ble.exceptions import (
    CommandRejected,
    NotConfirmed,
    ProtocolError,
    RequestTimeout,
    UnsafeValue,
)
from mira_ble.frame import Frame
from mira_ble.protocol import (
    ABSOLUTE_MAX_TEMPERATURE,
    MAX_OUTLETS,
    MAX_PRESET_SLOT,
    Identity,
    LastUsage,
    Outlet,
    OutletConfig,
    Preset,
    State,
)
from mira_ble.transport import ReplyTest, Transport

_LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_TEMPERATURE: Final = 45.0
"""The limit applied when the caller gives none; the valves' own factory maximum."""
CONFIRM_TIMEOUT: Final = 8.0
"""Seconds a command is given to show in the valve's state.

Outlets have been reported to show 3 to 5 s after the write, most of it the valve acting.
"""
CONFIRM_INTERVAL: Final = 0.5
"""Seconds between the state reads that confirm a command."""
STOPPED_READS: Final = 2
"""Consecutive reads that must show the valve at rest before a stop counts as confirmed."""
PRESET_FIELDS_END: Final = 40
"""Bytes of a preset that carry decoded fields; the rest is padding nobody reads."""
NO_ANSWER: Final = "no answer"
"""How an unanswered optional read is recorded in ``Capabilities.errors``."""


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a valve offers, found by reading it once at setup."""

    outlets: tuple[OutletConfig, ...] = ()
    """Configuration of each outlet the valve has."""
    presets: tuple[Preset, ...] = ()
    errors: dict[str, str] = field(default_factory=dict)
    """Why each read that failed was judged unavailable, keyed ``outlet_count``,
    ``outlet_<n>`` or ``preset_<n>``. :data:`NO_ANSWER` means the valve stayed silent."""

    @property
    def outlet_numbers(self) -> tuple[int, ...]:
        """Numbers of the outlets the valve has."""
        return tuple(outlet.number for outlet in self.outlets)

    def outlet(self, number: int) -> OutletConfig | None:
        """Return the configuration of outlet ``number``, if the valve has it."""
        return next((o for o in self.outlets if o.number == number), None)

    def max_temperature(self, outlets: Outlet) -> float | None:
        """Return the lowest maximum temperature among ``outlets``, if all are known."""
        wanted = [number for number in outlets.numbers if self.outlet(number) is None]
        if wanted or not outlets:
            return None
        return min(
            o.max_temperature for o in self.outlets if Outlet.from_number(o.number) & outlets
        )

    def failed(self, prefix: str) -> bool:
        """Return True when any read whose key starts with ``prefix`` failed."""
        return any(key.startswith(prefix) for key in self.errors)


class MiraValve:
    """Typed access to one valve over a :class:`~mira_ble.transport.Transport`.

    ``max_temperature`` is the hottest target this client will send. It is capped at
    :data:`~mira_ble.protocol.ABSOLUTE_MAX_TEMPERATURE` whatever is passed.
    """

    def __init__(
        self, transport: Transport, *, max_temperature: float = DEFAULT_MAX_TEMPERATURE
    ) -> None:
        if not math.isfinite(max_temperature):
            raise UnsafeValue(f"the temperature limit must be a number, not {max_temperature}")
        self._transport = transport
        self.max_temperature = min(max_temperature, ABSOLUTE_MAX_TEMPERATURE)

    @property
    def transport(self) -> Transport:
        """The transport in use."""
        return self._transport

    # -- low level ---------------------------------------------------------------------------

    async def _read(self, frame: Frame, accept: ReplyTest) -> bytes:
        return (await self._transport.request(frame, accept=accept)).payload

    async def _command(self, frame: Frame) -> None:
        """Send a command and require the valve's acknowledgement."""
        await self._transport.request(frame, accept=protocol.is_command_reply)

    async def optional[T](
        self, label: str, read: Callable[[], Awaitable[T]], errors: dict[str, str]
    ) -> T | None:
        """Run a read the valve may not support; record why it failed in ``errors`` and carry on.

        A refusal or an odd reply leaves the link usable. Silence does not (the transport
        closes a link whose request went unanswered), so the link is opened again.
        """
        try:
            return await read()
        except RequestTimeout as err:
            _LOGGER.debug("%s went unanswered: %s", label, err)
            errors[label] = NO_ANSWER
            await self._transport.connect()
        except (CommandRejected, ProtocolError) as err:
            _LOGGER.debug("%s is not available: %s", label, err)
            errors[label] = f"{type(err).__name__}: {err}"
        return None

    # -- reads -------------------------------------------------------------------------------

    async def read_state(self) -> State:
        """Read what the valve is doing now."""
        payload = await self._read(protocol.read_state_request(), protocol.is_state_reply)
        return protocol.decode_state(payload)

    async def read_outlet_count(self) -> int:
        """Read how many outlets the valve has."""
        payload = await self._read(
            protocol.read_valve_config_request(), protocol.is_valve_config_reply
        )
        return protocol.decode_outlet_count(payload)

    async def read_outlet_config(self, number: int) -> OutletConfig:
        """Read outlet ``number``'s (1 to 3) limits and defaults."""
        payload = await self._read(
            protocol.read_outlet_config_request(number),
            partial(protocol.is_outlet_config_reply, number=number),
        )
        return protocol.decode_outlet_config(payload, number)

    async def read_preset(self, slot: int) -> Preset | None:
        """Read the preset in ``slot``; None when the slot is empty."""
        payload = await self._read(
            protocol.read_preset_request(slot), partial(protocol.is_preset_reply, slot=slot)
        )
        return protocol.decode_preset(payload, slot)

    async def read_presets(
        self, last_slot: int = MAX_PRESET_SLOT, errors: dict[str, str] | None = None
    ) -> tuple[Preset, ...]:
        """Read the configured presets in slots 1 to ``last_slot``.

        A slot the valve refuses or does not answer ends the listing, since every later slot
        would cost the same wait; the reason goes into ``errors``.
        """
        errors = {} if errors is None else errors
        presets: list[Preset] = []
        for slot in range(1, last_slot + 1):
            label = f"preset_{slot}"
            preset = await self.optional(label, partial(self.read_preset, slot), errors)
            if preset is not None:
                presets.append(preset)
            elif label in errors:
                break
        return tuple(presets)

    async def read_last_usage(self) -> LastUsage:
        """Read the valve's record of its last finished run."""
        payload = await self._read(protocol.read_last_usage_request(), protocol.is_last_usage_reply)
        return protocol.decode_last_usage(payload)

    async def read_identity(self, errors: dict[str, str] | None = None) -> Identity:
        """Read the valve's name, serial number and date of manufacture.

        Each is read on its own, so a valve that lacks one still reports the others.
        """
        errors = {} if errors is None else errors

        async def name() -> str | None:
            payload = await self._read(protocol.read_name_request(), protocol.is_name_reply)
            return protocol.decode_text(payload)

        async def serial() -> str | None:
            payload = await self._read(protocol.read_serial_request(), protocol.is_serial_reply)
            return protocol.decode_serial(payload)

        async def manufactured() -> datetime | None:
            payload = await self._read(
                protocol.read_manufactured_request(), protocol.is_manufactured_reply
            )
            return protocol.decode_manufactured(payload)

        return Identity(
            name=await self.optional("name", name, errors),
            serial_number=await self.optional("serial_number", serial, errors),
            manufactured=await self.optional("manufactured", manufactured, errors),
        )

    async def probe(self) -> Capabilities:
        """Read which outlets the valve has and which presets are stored.

        A lost connection propagates; a read the valve refuses, answers oddly or ignores is
        recorded in ``errors`` and skipped. When the valve does not say how many outlets it
        has, an outlet is taken to exist only if its configuration carries both a type and a
        maximum temperature: naming an outlet a valve lacks makes it ignore a whole command.
        """
        outlets: list[OutletConfig] = []
        errors: dict[str, str] = {}
        count = await self.optional("outlet_count", self.read_outlet_count, errors)
        for number in range(1, (count or MAX_OUTLETS) + 1):
            config = await self.optional(
                f"outlet_{number}", partial(self.read_outlet_config, number), errors
            )
            if config is None:
                continue
            if count is not None or (config.outlet_type and config.max_temperature > 0):
                outlets.append(config)
        presets = await self.read_presets(errors=errors)
        return Capabilities(outlets=tuple(outlets), presets=presets, errors=errors)

    # -- control -----------------------------------------------------------------------------

    def check_temperature(self, temperature: float, limit: float | None = None) -> None:
        """Raise :class:`~mira_ble.exceptions.UnsafeValue` if ``temperature`` may not be sent."""
        ceiling = self.max_temperature if limit is None else min(limit, self.max_temperature)
        if not (math.isfinite(ceiling) and temperature <= ceiling):
            raise UnsafeValue(f"temperature {temperature} C is above the limit of {ceiling} C")

    async def set_outlets(
        self,
        outlets: Outlet,
        *,
        temperature: float | None,
        flow: float,
        max_temperature: float | None = None,
        confirm: bool = True,
    ) -> State | None:
        """Run exactly ``outlets`` at ``temperature`` (C) and ``flow`` (L/min).

        Any outlet not named is switched off. ``max_temperature`` lowers the limit for this
        call, for instance to the outlet's own maximum. ``temperature=None`` passes "no
        target" through (see :func:`~mira_ble.protocol.set_outlets_request`).

        With ``confirm`` the state is read until it shows those outlets, and returned;
        :class:`~mira_ble.exceptions.NotConfirmed` is raised if it does not within
        :data:`CONFIRM_TIMEOUT`. The valve may still be acting on the command then: the
        exception carries the last state read, and the caller decides whether to stop.
        """
        if temperature is not None:
            self.check_temperature(temperature, max_temperature)
        wanted = Outlet(int(outlets) & protocol.OUTLET_MASK)
        await self._command(protocol.set_outlets_request(wanted, temperature, flow))
        if not confirm:
            return None
        return await self._confirm(
            lambda state: state.outlets == wanted, f"outlets {wanted.numbers}"
        )

    async def run_preset(
        self, slot: int, *, confirm: bool = True, before: State | None = None
    ) -> State | None:
        """Start the preset stored in ``slot`` (1-based).

        A running preset shows as an open outlet on some valves and as the held bit or the
        run flag on others. Confirmation accepts an open outlet, or a held bit or run flag
        that was not already set in ``before`` (the state read just before, when given).
        """
        await self._command(protocol.run_preset_request(slot))
        if not confirm:
            return None

        def started(state: State) -> bool:
            if state.outlets:
                return True
            was_held = before.held if before is not None else False
            was_flagged = before.run_flag if before is not None else False
            return (state.held and not was_held) or (state.run_flag and not was_flagged)

        return await self._confirm(started, f"preset {slot}")

    async def stop(self, *, confirm: bool = True) -> State | None:
        """Close every outlet, including a running preset.

        Sends the all-zero outlet command. If the valve does not come to rest, sends the
        other stop ("run preset 0", the one measured on a Platinum) and checks again.
        """
        await self._command(protocol.stop_request())
        if not confirm:
            return None
        try:
            return await self._confirm_stopped()
        except NotConfirmed as err:
            _LOGGER.debug("stop not confirmed (%s); trying the preset stop", err)
            unconfirmed = err
        try:
            await self._command(protocol.preset_stop_request())
        except (CommandRejected, ProtocolError) as err:
            _LOGGER.debug("the preset stop was not accepted: %s", err)
            raise unconfirmed from err
        return await self._confirm_stopped()

    async def _confirm_stopped(self) -> State:
        """Read until the valve reports itself at rest on consecutive reads."""
        at_rest = 0

        def stopped(state: State) -> bool:
            nonlocal at_rest
            at_rest = 0 if state.active else at_rest + 1
            return at_rest >= STOPPED_READS

        return await self._confirm(stopped, "stop")

    async def _confirm(self, done: Callable[[State], bool], what: str) -> State:
        """Read the state every :data:`CONFIRM_INTERVAL` until ``done`` or the deadline."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONFIRM_TIMEOUT
        state: State | None = None
        while True:
            state = await self.read_state()
            if done(state):
                return state
            if loop.time() + CONFIRM_INTERVAL > deadline:
                break
            await asyncio.sleep(CONFIRM_INTERVAL)
        raise NotConfirmed(
            f"{what} was acknowledged but the valve still reports outlets "
            f"{state.outlets.numbers or 'none'}"
            f"{', held' if state.held else ''}{', run flag' if state.run_flag else ''} "
            f"after {CONFIRM_TIMEOUT:g} s",
            state,
        )

    # -- presets -----------------------------------------------------------------------------

    async def write_preset(self, preset: Preset) -> Preset:
        """Store ``preset`` in its slot, replacing what is there, and return it as read back.

        A write replaces the whole slot. The previous contents are not recoverable: note
        them down first.
        """
        self.check_temperature(preset.temperature)
        await self._command(protocol.write_preset_request(preset))
        stored = await self.read_preset(preset.slot)
        # Compare the decoded fields, not the trailing bytes nobody decodes.
        fields = slice(0, PRESET_FIELDS_END)
        if stored is None or (
            protocol.encode_preset(stored)[fields] != protocol.encode_preset(preset)[fields]
        ):
            raise NotConfirmed(f"preset {preset.slot} did not read back as written")
        return stored

    async def delete_preset(self, slot: int) -> None:
        """Clear the preset in ``slot``."""
        await self._command(protocol.delete_preset_request(slot))
        if await self.read_preset(slot) is not None:
            raise NotConfirmed(f"preset {slot} is still stored after deleting it")
