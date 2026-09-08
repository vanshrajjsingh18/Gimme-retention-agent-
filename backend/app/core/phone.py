"""New Zealand phone number normalisation.

TNZ is handed one string per recipient, and it is not the place to be guessing.
A number that reaches the provider in the wrong shape either fails silently or,
worse, routes somewhere else — so numbers are canonicalised to E.164 on the way
in and checked again before dispatch.

This module is deliberately pure: no ORM, no settings, no I/O. It refuses
anything it cannot resolve with confidence rather than emitting a plausible
guess, because a wrong number is a text to a stranger.
"""
from __future__ import annotations

import re

#: New Zealand. The only country whose national ("0…") format we expand.
NZ_COUNTRY_CODE = "64"

#: NZ mobile prefixes, after the national trunk "0" is removed: 20-29.
#: Landlines (3, 4, 6, 7, 9) are valid numbers but cannot receive SMS.
NZ_MOBILE_PREFIXES = ("20", "21", "22", "23", "24", "25", "26", "27", "28", "29")

#: Subscriber digits after the country code for a valid NZ mobile. 021 numbers
#: are famously variable in length, which is why this is a range and not a rule.
NZ_MOBILE_MIN_DIGITS = 8
NZ_MOBILE_MAX_DIGITS = 10

_NON_DIGITS = re.compile(r"[^\d+]")


def normalize_nz_phone(value: str | None) -> str | None:
    """Canonicalise a phone number to E.164, or return None if it cannot be.

    Accepts the shapes real New Zealand data actually arrives in::

        021 123 4567   0211234567   +64 21 123 4567
        0064211234567  64211234567  (021) 123-4567

    Returns None — never a guess — for anything else, including landlines,
    short codes and numbers whose length is not plausible. A caller that gets
    None should treat the customer as having no usable phone number.

    Numbers already in international form for another country are passed
    through unchanged: an Australian customer's +61 number is not ours to
    reinterpret.
    """
    if not value:
        return None

    cleaned = _NON_DIGITS.sub("", str(value).strip())
    if not cleaned:
        return None

    # A "+" is only meaningful at the front.
    plus = cleaned.startswith("+")
    digits = cleaned.lstrip("+")
    if not digits.isdigit():
        return None

    if plus and not digits.startswith(NZ_COUNTRY_CODE):
        # Already international, and not ours to second-guess.
        return f"+{digits}" if 8 <= len(digits) <= 15 else None

    if digits.startswith("00" + NZ_COUNTRY_CODE):
        national = digits[len("00" + NZ_COUNTRY_CODE) :]
    elif digits.startswith(NZ_COUNTRY_CODE) and not digits.startswith("0"):
        national = digits[len(NZ_COUNTRY_CODE) :]
    elif digits.startswith("0"):
        national = digits[1:]
    else:
        # No country code and no trunk prefix. Treating a bare "211234567" as
        # a New Zealand mobile is the one guess worth making, since that is
        # what a spreadsheet does to a number stored without its leading zero.
        national = digits

    # A national number may still carry the trunk zero after a country code
    # ("+64 021 …"), which is a common copy-paste artefact.
    national = national.lstrip("0") if national.startswith("0") else national

    if not national.startswith(NZ_MOBILE_PREFIXES):
        return None
    if not (NZ_MOBILE_MIN_DIGITS <= len(national) <= NZ_MOBILE_MAX_DIGITS):
        return None

    return f"+{NZ_COUNTRY_CODE}{national}"


def is_sendable(value: str | None) -> bool:
    """Whether an SMS to this number could actually be delivered."""
    return normalize_nz_phone(value) is not None
