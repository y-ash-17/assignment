"""Test 2: Idempotent duplicate requests write once and return identical booking details."""

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app import clock
from app.db import get_db_session
from app.models import ServiceRequest
from app.tools.api import book_service, log_symptom

KOLKATA_TZ = ZoneInfo("Asia/Kolkata")


@pytest.mark.asyncio
async def test_duplicate_booking_request_idempotency():
    """Sending the same booking request twice with the same request_id returns the initial booking."""
    now = clock.now(KOLKATA_TZ)
    days_ahead = (2 - now.weekday() + 7) % 7  # Next Wednesday
    if days_ahead < 2:
        days_ahead += 7
    target_date = now.date() + timedelta(days=days_ahead)
    slot_time = datetime(target_date.year, target_date.month, target_date.day, 10, 0, tzinfo=KOLKATA_TZ)

    patient_id = "pat_001"
    request_id = uuid.uuid4()

    # First call
    res1 = await book_service(
        patient_id=patient_id,
        service="DOCTOR",
        subtype="GENERAL_PHYSICIAN",
        scheduled_for=slot_time,
        request_id=request_id,
    )
    assert res1["ok"] is True
    assert res1["replayed"] is False
    booking_id = res1["booking_id"]

    # Second call with the same request_id
    res2 = await book_service(
        patient_id=patient_id,
        service="DOCTOR",
        subtype="GENERAL_PHYSICIAN",
        scheduled_for=slot_time,
        request_id=request_id,
    )
    assert res2["ok"] is True
    assert res2["replayed"] is True
    assert res2["booking_id"] == booking_id

    # Verify only 1 row in the database
    async with get_db_session() as session:
        stmt = select(ServiceRequest).where(ServiceRequest.request_id == request_id)
        rows = (await session.execute(stmt)).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_duplicate_request_id_different_patient_conflict():
    """A reused request_id from a different patient triggers REQUEST_ID_CONFLICT."""
    now = clock.now(KOLKATA_TZ)
    days_ahead = (4 - now.weekday() + 7) % 7  # Next Friday
    if days_ahead < 2:
        days_ahead += 7
    target_date = now.date() + timedelta(days=days_ahead)
    slot_time = datetime(target_date.year, target_date.month, target_date.day, 9, 0, tzinfo=KOLKATA_TZ)

    request_id = uuid.uuid4()

    # First patient books
    res1 = await book_service(
        patient_id="pat_001",
        service="DOCTOR",
        subtype="GENERAL_PHYSICIAN",
        scheduled_for=slot_time,
        request_id=request_id,
    )
    assert res1["ok"] is True

    # Second patient attempts using the exact same request_id
    res2 = await book_service(
        patient_id="pat_002",
        service="DOCTOR",
        subtype="GENERAL_PHYSICIAN",
        scheduled_for=slot_time,
        request_id=request_id,
    )
    assert res2["ok"] is False
    assert res2["error_code"] == "REQUEST_ID_CONFLICT"


@pytest.mark.asyncio
async def test_duplicate_symptom_log_idempotency():
    """Logging the same symptom twice with the same request_id writes once and replays."""
    req_id = uuid.uuid4()
    patient_id = "pat_001"

    res1 = await log_symptom(patient_id=patient_id, symptom="Mild headache", severity=2, request_id=req_id)
    assert res1["ok"] is True
    assert res1["replayed"] is False

    res2 = await log_symptom(patient_id=patient_id, symptom="Mild headache", severity=2, request_id=req_id)
    assert res2["ok"] is True
    assert res2["replayed"] is True
    assert res2["log_id"] == res1["log_id"]


@pytest.mark.asyncio
async def test_booking_replay_after_slot_passes(monkeypatch):
    """Redelivery returns the original booking even after the booking time."""
    now = clock.now(KOLKATA_TZ)
    days_ahead = (2 - now.weekday() + 7) % 7 + 7
    target = now.date() + timedelta(days=days_ahead)
    slot = datetime(target.year, target.month, target.day, 10, 0, tzinfo=KOLKATA_TZ)
    request_id = uuid.uuid4()
    first = await book_service("pat_001", "DOCTOR", "GENERAL_PHYSICIAN", slot, request_id)
    assert first["ok"]
    monkeypatch.setattr(clock, "now", lambda tz=None: slot + timedelta(days=1))
    replay = await book_service("pat_001", "DOCTOR", "GENERAL_PHYSICIAN", slot, request_id)
    assert replay["ok"] and replay["replayed"]
    assert replay["booking_id"] == first["booking_id"]
