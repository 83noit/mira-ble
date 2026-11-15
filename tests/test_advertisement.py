"""Advertised local names."""

from __future__ import annotations

import pytest

from mira_ble.advertisement import Advertisement, parse_local_name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "Mira 0051#0123456789012345",
            Advertisement("0051", serial_number="0123456789012345"),
        ),
        ("Mira 004C Bathroom", Advertisement("004C", label="Bathroom")),
        ("MIRA 003F ENSUITE", Advertisement("003F", label="ENSUITE")),
        ("Mira 003f en suite ", Advertisement("003F", label="en suite")),
        ("Mira 0051", Advertisement("0051")),
    ],
)
def test_names_of_real_valves(name: str, expected: Advertisement) -> None:
    assert parse_local_name(name) == expected


@pytest.mark.parametrize(
    "name", [None, "", "Mira", "Mirage 0051", "Mira 51", "Mira 00ZZ x", "Oral-B"]
)
def test_other_names(name: str | None) -> None:
    assert parse_local_name(name) is None
