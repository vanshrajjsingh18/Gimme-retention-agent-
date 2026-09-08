"""Phone normalisation: what TNZ is actually handed."""
from __future__ import annotations

import pytest

from app.core.phone import is_sendable, normalize_nz_phone

CANONICAL = "+64211234567"


@pytest.mark.parametrize(
    "value",
    [
        "+64211234567",
        "0211234567",
        "021 123 4567",
        "021-123-4567",
        "(021) 123 4567",
        "0064211234567",
        "64211234567",
        "+64 21 123 4567",
        "  0211234567  ",
        "211234567",  # a spreadsheet ate the leading zero
        "+640211234567",  # trunk zero left in after the country code
    ],
)
def test_every_shape_real_data_arrives_in_lands_on_one_number(value):
    assert normalize_nz_phone(value) == CANONICAL


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "not a phone number",
        "0",
        "021",  # too short to be anybody
        "02112345678901",  # too long
        "093661234",  # Auckland landline: a real number that cannot receive SMS
        "0800838383",  # freephone
        "1234",  # short code
    ],
)
def test_anything_it_cannot_resolve_is_refused_rather_than_guessed(value):
    # A wrong number is a text to a stranger, so there is no useful
    # "best effort" here — None means the customer has no usable phone.
    assert normalize_nz_phone(value) is None
    assert is_sendable(value) is False


def test_another_countrys_number_is_left_alone():
    # An Australian customer's number is already international. Reinterpreting
    # it as a New Zealand one would send the message somewhere else entirely.
    assert normalize_nz_phone("+61412345678") == "+61412345678"


def test_a_landline_is_refused_even_though_it_is_a_valid_number():
    assert normalize_nz_phone("+6493661234") is None


@pytest.mark.parametrize("prefix", ["20", "21", "22", "23", "24", "25", "26", "27", "28", "29"])
def test_every_mobile_prefix_is_recognised(prefix):
    assert normalize_nz_phone(f"0{prefix}1234567") == f"+64{prefix}1234567"


def test_normalising_twice_changes_nothing():
    once = normalize_nz_phone("021 123 4567")
    assert normalize_nz_phone(once) == once
