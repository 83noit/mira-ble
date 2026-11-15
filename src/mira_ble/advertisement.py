"""What a valve's advertisement tells us before connecting.

Valves advertise the protocol service UUID and a local name in one of two forms:

* ``Mira 0051#0123456789012345``: a four-digit hexadecimal product code, then the serial
  number (the factory form);
* ``Mira 004C Bathroom``: the product code, then the name given at commissioning.

The same valve can use either form at different times, and its Bluetooth address can change
across a power cut, so neither the name nor the address identifies it for good. The serial
number does, and when the name carries it a valve can be recognised at a new address without
connecting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NAME = re.compile(
    r"^mira\s+(?P<code>[0-9a-f]{4})(?:#(?P<serial>\d+)|\s+(?P<label>\S.*))?$", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class Advertisement:
    """The parts of a valve's advertised name."""

    product_code: str
    """Four hexadecimal digits, upper case; the same for every unit of a product."""
    serial_number: str | None = None
    label: str | None = None
    """The commissioning name, when the valve advertises that form."""


def parse_local_name(name: str | None) -> Advertisement | None:
    """Parse an advertised local name; None when it is not a Mira valve's."""
    if not name:
        return None
    match = _NAME.match(name.strip())
    if match is None:
        return None
    label = match["label"]
    return Advertisement(
        product_code=match["code"].upper(),
        serial_number=match["serial"],
        label=label.strip() if label else None,
    )
