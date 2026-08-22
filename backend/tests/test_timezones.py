"""Business-timezone handling.

New Zealand is UTC+12 in winter and UTC+13 in summer, so every one of these
assertions would be wrong by half a day if the code compared naive UTC.
"""
from __future__ import annotations

from datetime import date, datetime, time

from app.core.timezones import (
    combine_local,
    in_send_window,
    local_date,
    next_send_slot,
    to_local,
    to_utc_naive,
)

WINDOW_START = time(9, 0)
WINDOW_END = time(19, 0)


def test_winter_offset_is_twelve_hours():
    """June is NZST, UTC+12."""
    assert to_local(datetime(2025, 6, 1, 2, 0)).hour == 14


def test_summer_offset_is_thirteen_hours():
    """January is NZDT, UTC+13."""
    assert to_local(datetime(2025, 1, 15, 2, 0)).hour == 15


def test_round_trip_local_to_utc():
    utc = to_utc_naive(datetime(2025, 6, 1, 14, 0))
    assert utc == datetime(2025, 6, 1, 2, 0)
    assert to_local(utc).hour == 14


def test_local_date_can_differ_from_utc_date():
    """9pm on the 1st in Auckland is still the 1st of June for the customer."""
    moment = datetime(2025, 6, 1, 9, 0)  # 21:00 local on the 1st
    assert moment.date() == date(2025, 6, 1)
    assert local_date(moment) == date(2025, 6, 1)

    # ...but 8am UTC on the 2nd is 8pm local on the same 2nd, whereas 14:00
    # UTC on the 1st has already rolled over to the 2nd locally.
    assert local_date(datetime(2025, 6, 1, 14, 0)) == date(2025, 6, 2)


def test_combine_local_produces_utc():
    assert combine_local(date(2025, 6, 10), time(10, 0)) == datetime(2025, 6, 9, 22, 0)


def test_combine_local_survives_spring_forward():
    """On 28 Sep 2025 NZ clocks jump 02:00 -> 03:00; 02:30 does not exist."""
    utc = combine_local(date(2025, 9, 28), time(2, 30))
    local = to_local(utc)
    assert local.date() == date(2025, 9, 28)
    assert local.hour >= 3  # nudged past the gap rather than raising


def test_send_window_uses_local_time():
    assert in_send_window(datetime(2025, 6, 1, 2, 0), WINDOW_START, WINDOW_END)  # 14:00
    assert not in_send_window(datetime(2025, 6, 1, 9, 0), WINDOW_START, WINDOW_END)  # 21:00
    assert not in_send_window(datetime(2025, 6, 1, 18, 0), WINDOW_START, WINDOW_END)  # 06:00


def test_window_boundaries_are_half_open():
    nine_local = combine_local(date(2025, 6, 2), time(9, 0))
    seven_pm_local = combine_local(date(2025, 6, 2), time(19, 0))
    assert in_send_window(nine_local, WINDOW_START, WINDOW_END)
    assert not in_send_window(seven_pm_local, WINDOW_START, WINDOW_END)


def test_next_slot_returns_the_moment_when_already_inside():
    inside = datetime(2025, 6, 1, 2, 0)
    assert next_send_slot(inside, WINDOW_START, WINDOW_END) == inside


def test_late_evening_send_defers_to_next_morning():
    late = datetime(2025, 6, 1, 9, 0)  # 21:00 local Sunday
    slot = next_send_slot(late, WINDOW_START, WINDOW_END)
    local = to_local(slot)
    assert (local.hour, local.minute) == (9, 0)
    assert local.date() == date(2025, 6, 2)  # Monday morning


def test_early_morning_send_defers_to_the_same_day():
    early = datetime(2025, 6, 1, 18, 0)  # 06:00 local on the 2nd
    local = to_local(next_send_slot(early, WINDOW_START, WINDOW_END))
    assert (local.hour, local.date()) == (9, date(2025, 6, 2))


def test_next_slot_is_always_inside_the_window():
    for hour in range(24):
        slot = next_send_slot(datetime(2025, 6, 1, hour, 0), WINDOW_START, WINDOW_END)
        assert in_send_window(slot, WINDOW_START, WINDOW_END)
        assert slot >= datetime(2025, 6, 1, hour, 0)
