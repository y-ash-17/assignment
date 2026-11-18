"""Pure functions for clinician slot grid calculation and free-slot discovery."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.enums import Weekday

WEEKDAY_MAP = {
    0: Weekday.MON,
    1: Weekday.TUE,
    2: Weekday.WED,
    3: Weekday.THU,
    4: Weekday.FRI,
    5: Weekday.SAT,
    6: Weekday.SUN,
}


def get_weekday(d: date) -> Weekday:
    """Return the 3-letter uppercase weekday for a given date."""
    return WEEKDAY_MAP[d.weekday()]


def parse_time(time_str: str) -> time:
    """Parse 'HH:MM' string to time object."""
    parts = time_str.strip().split(":")
    return time(hour=int(parts[0]), minute=int(parts[1]))


def compute_window_slots(
    d: date,
    start_time_str: str,
    end_time_str: str,
    slot_minutes: int,
    tz: ZoneInfo,
) -> list[datetime]:
    """Generate discrete slot start datetimes fitting entirely within [start, end]."""
    if slot_minutes <= 0:
        raise ValueError("slot_minutes must be positive")
    start_t = parse_time(start_time_str)
    end_t = parse_time(end_time_str)

    slot_start = datetime.combine(d, start_t, tzinfo=tz)
    window_end = datetime.combine(d, end_t, tzinfo=tz)
    duration = timedelta(minutes=slot_minutes)

    slots: list[datetime] = []
    current = slot_start
    while current + duration <= window_end:
        slots.append(current)
        current += duration

    return slots


def get_clinician_slots_for_date(
    availability: list[dict],
    slot_minutes: int,
    target_date: date,
    society_id: str,
    tz: ZoneInfo,
) -> list[datetime]:
    """Calculate all scheduled slots for a clinician at a specific society on a given date."""
    target_weekday = get_weekday(target_date)
    slots: list[datetime] = []

    for window in availability:
        if window.get("society_id") == society_id and window.get("weekday") == target_weekday:
            window_slots = compute_window_slots(
                target_date,
                window["start"],
                window["end"],
                slot_minutes,
                tz,
            )
            slots.extend(window_slots)

    return sorted(set(slots))


def is_on_slot_grid(
    scheduled_for: datetime,
    availability: list[dict],
    slot_minutes: int,
    society_id: str,
    tz: ZoneInfo,
) -> bool:
    """Check if scheduled_for matches an exact slot start on the clinician's availability grid."""
    scheduled_local = scheduled_for.astimezone(tz)
    slots = get_clinician_slots_for_date(
        availability=availability,
        slot_minutes=slot_minutes,
        target_date=scheduled_local.date(),
        society_id=society_id,
        tz=tz,
    )
    return any(scheduled_local == s for s in slots)


def compute_free_slots(
    all_slots: list[datetime],
    booked_slots: set[datetime],
    min_time: datetime | None = None,
) -> list[datetime]:
    """Filter out booked slots and past/too-soon slots."""
    free: list[datetime] = []
    for s in all_slots:
        if s in booked_slots:
            continue
        if min_time is not None and s < min_time:
            continue
        free.append(s)
    return free


def find_nearest_free_slots(
    target_time: datetime,
    candidate_slots: list[datetime],
    booked_slots: set[datetime],
    min_time: datetime | None = None,
    limit: int = 3,
) -> list[datetime]:
    """Find the nearest available slots to the requested target time."""
    free = compute_free_slots(candidate_slots, booked_slots, min_time=min_time)
    if not free:
        return []
    # Sort by absolute time difference from target
    free.sort(key=lambda s: abs((s - target_time).total_seconds()))
    # Then re-sort the top candidates chronologically for user display
    nearest = sorted(free[:limit])
    return nearest
