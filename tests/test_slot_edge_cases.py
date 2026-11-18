"""Slot generation edge cases."""
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from app.repo.slots import compute_window_slots, get_clinician_slots_for_date


def test_nonpositive_slot_duration_rejected():
    with pytest.raises(ValueError):
        compute_window_slots(date(2026, 11, 18), "09:00", "10:00", 0, ZoneInfo("Asia/Kolkata"))


def test_overlapping_windows_do_not_duplicate_slots():
    window = {"society_id": "s", "weekday": "WED", "start": "09:00", "end": "10:00"}
    slots = get_clinician_slots_for_date([window, window], 30, date(2026, 11, 18), "s", ZoneInfo("Asia/Kolkata"))
    assert len(slots) == 2
