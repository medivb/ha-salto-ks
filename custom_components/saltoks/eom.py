"""Easy Office Mode schedule helpers for Salto KS.

The schedule is returned as the ``easy_office_mode_schedule`` property of each
lock (an empty list when no schedule is set) and written with
``PUT /sites/{site_id}/locks/{lock_id}/easy_office_mode_schedule``. The PUT
replaces the whole schedule, so every change sends all fields.
"""

from __future__ import annotations

from datetime import date, time
from typing import Any

from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

EOM_PROPERTY = "easy_office_mode_schedule"

AUTO_START = "eom_auto_start"
START_TIME = "start_time"
END_TIME = "end_time"
START_DATE = "start_date"
END_DATE = "end_date"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Dates are sent as local timestamps; the end date includes the whole last day.
START_OF_DAY = "T00:00:00.000"
END_OF_DAY = "T23:59:59.999"

DEFAULT_START_TIME = time(9, 0)
DEFAULT_END_TIME = time(17, 0)


def parse_time(value: Any) -> time | None:
    if isinstance(value, time):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        hours, minutes = value.split(":")[:2]
        return time(int(hours), int(minutes))
    except ValueError:
        return None


def parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def current_schedule(lock: dict[str, Any] | None) -> dict[str, Any] | None:
    """The configured schedule, or None when the lock has none."""
    value = (lock or {}).get(EOM_PROPERTY)
    return value if isinstance(value, dict) and value else None


def to_values(schedule: dict[str, Any] | None) -> dict[str, Any]:
    """Typed values for the controls. Without a schedule, everything is off
    and the times and dates are unknown."""
    schedule = schedule or {}
    values: dict[str, Any] = {AUTO_START: bool(schedule.get(AUTO_START, False))}
    for day in WEEKDAYS:
        values[day] = bool(schedule.get(day, False))
    values[START_TIME] = parse_time(schedule.get(START_TIME))
    values[END_TIME] = parse_time(schedule.get(END_TIME))
    values[START_DATE] = parse_date(schedule.get(START_DATE))
    values[END_DATE] = parse_date(schedule.get(END_DATE))
    return values


def build_payload(schedule: dict[str, Any] | None, changes: dict[str, Any]) -> dict[str, Any]:
    """Merge typed changes into the current schedule and return the PUT body.

    Fields without a value (no schedule yet) get safe defaults: weekdays and
    auto start off, 09:00-17:00, and today as start and end date. With all
    days off the schedule has no effect until a day is enabled.
    """
    values = {**to_values(schedule), **changes}
    today = dt_util.now().date()
    start_time = values[START_TIME] or DEFAULT_START_TIME
    end_time = values[END_TIME] or DEFAULT_END_TIME
    start_date = values[START_DATE] or today
    end_date = values[END_DATE] or max(start_date, today)

    if end_time <= start_time:
        raise ServiceValidationError(
            f"The end time ({end_time:%H:%M}) must be later than the start time "
            f"({start_time:%H:%M})."
        )
    if end_date < start_date:
        raise ServiceValidationError(
            f"The end date ({end_date.isoformat()}) must not be before the start date "
            f"({start_date.isoformat()})."
        )

    payload: dict[str, Any] = {AUTO_START: bool(values[AUTO_START])}
    for day in WEEKDAYS:
        payload[day] = bool(values[day])
    payload[START_TIME] = start_time.strftime("%H:%M")
    payload[END_TIME] = end_time.strftime("%H:%M")
    payload[START_DATE] = start_date.isoformat() + START_OF_DAY
    payload[END_DATE] = end_date.isoformat() + END_OF_DAY
    return payload
