"""Business-timezone handling.

The database stores naive UTC throughout. Customers, however, experience send
times in New Zealand local time, which is UTC+12 (NZST) or UTC+13 (NZDT)
depending on the date. A quiet-hours check performed against naive UTC is
therefore wrong by twelve or thirteen hours — it would happily fire a 3am
text.

Everything that needs to reason about "what time is it for the customer"
goes through here.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.config import settings

UTC = timezone.utc


def business_tz() -> ZoneInfo:
    """The configured business timezone (Pacific/Auckland by default)."""
    return ZoneInfo(settings.BUSINESS_TIMEZONE)


def to_local(moment: datetime) -> datetime:
    """Convert a naive-UTC (or aware) datetime to business local time."""
    aware = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment
    return aware.astimezone(business_tz())


def to_utc_naive(moment: datetime) -> datetime:
    """Convert a local or aware datetime to the naive UTC the database stores."""
    if moment.tzinfo is None:
        # Interpret a naive value as business local time — that is what a
        # scheduling input from the UI means.
        moment = moment.replace(tzinfo=business_tz())
    return moment.astimezone(UTC).replace(tzinfo=None)


def local_now() -> datetime:
    """Current business local time."""
    return datetime.now(business_tz())


def local_date(moment: datetime) -> date:
    """The customer's local calendar date for a stored UTC timestamp.

    Used for per-day frequency capping: "one message per day" has to mean the
    customer's day, not a UTC day that straddles their evening.
    """
    return to_local(moment).date()


def combine_local(day: date, at: time) -> datetime:
    """Build a naive-UTC timestamp for a local date and time-of-day.

    Handles DST transitions: on the spring-forward day the requested wall
    time may not exist, in which case the next valid instant is used.
    """
    local = datetime.combine(day, at).replace(tzinfo=business_tz())
    utc = local.astimezone(UTC).replace(tzinfo=None)
    # Round-trip to detect a skipped wall time and nudge past the gap.
    if to_local(utc).time() != at:
        local = datetime.combine(day, at) + timedelta(hours=1)
        local = local.replace(tzinfo=business_tz())
        utc = local.astimezone(UTC).replace(tzinfo=None)
    return utc


def in_send_window(moment: datetime, start: time, end: time) -> bool:
    """True when the customer's local time falls inside the allowed window.

    ``start``/``end`` are local wall-clock times. A window that does not wrap
    (09:00-19:00) is the normal case for business hours.
    """
    local_time = to_local(moment).time()
    if start <= end:
        return start <= local_time < end
    # A wrapping window (e.g. 20:00-06:00) is inclusive of midnight.
    return local_time >= start or local_time < end


def next_send_slot(moment: datetime, start: time, end: time) -> datetime:
    """The earliest instant at or after ``moment`` inside the send window.

    Returns naive UTC. Used to defer a send that would otherwise land outside
    business hours rather than dropping it.
    """
    if in_send_window(moment, start, end):
        return moment.replace(tzinfo=None) if moment.tzinfo else moment

    local = to_local(moment)
    candidate_day = local.date()
    # If we are past the window today, the next slot is tomorrow's open.
    if local.time() >= end:
        candidate_day = candidate_day + timedelta(days=1)
    return combine_local(candidate_day, start)
