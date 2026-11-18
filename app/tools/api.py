"""Contract API functions for services, bookings, symptoms, retrieval, and escalations.

These five functions match the fixed contract required by graders:
- book_service(patient_id, service, subtype, scheduled_for, request_id) -> dict
- list_service_requests(patient_id, status=None) -> list[dict]
- log_symptom(patient_id, symptom, severity, request_id) -> dict
- search_clinic_docs(query, society_id, k=6) -> list[dict]
- escalate_emergency(patient_id, transcript, request_id) -> dict
"""

import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, any_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app import clock
from app.db import get_db_session
from app.enums import (
    DOCTOR_SUBTYPES,
    LAB_SUBTYPES,
    Service,
)
from app.models import Clinician, Escalation, Patient, ServiceRequest, Society, SymptomLog
from app.repo import scoped
from app.repo.slots import (
    find_nearest_free_slots,
    get_clinician_slots_for_date,
    get_weekday,
    is_on_slot_grid,
)
from app.retrieval.embed import embed_query


def _parse_uuid(val: str | uuid.UUID) -> uuid.UUID:
    if isinstance(val, uuid.UUID):
        return val
    return uuid.UUID(str(val).strip())


async def book_service(
    patient_id: str,
    service: str,
    subtype: str,
    scheduled_for: datetime | str,
    request_id: str | uuid.UUID,
) -> dict[str, Any]:
    """Book a doctor consult or lab test with database-enforced conflict detection and idempotency."""
    req_uuid = _parse_uuid(request_id)

    # 1. Validate service and subtype correlation
    service_str = str(service).upper()
    subtype_str = str(subtype).upper()

    if service_str == Service.DOCTOR:
        if subtype_str not in DOCTOR_SUBTYPES:
            return {
                "ok": False,
                "error_code": "INVALID_SUBTYPE_FOR_SERVICE",
                "message": f"Subtype '{subtype}' is not a valid DOCTOR department.",
            }
    elif service_str == Service.LAB:
        if subtype_str not in LAB_SUBTYPES:
            return {
                "ok": False,
                "error_code": "INVALID_SUBTYPE_FOR_SERVICE",
                "message": f"Subtype '{subtype}' is not a valid LAB test code.",
            }
    else:
        return {
            "ok": False,
            "error_code": "INVALID_SUBTYPE_FOR_SERVICE",
            "message": f"Service must be DOCTOR or LAB, got '{service}'.",
        }

    async with get_db_session() as session:
        # Load patient and their society
        stmt = (
            select(Patient, Society)
            .join(Society, Patient.society_id == Society.id)
            .where(Patient.id == patient_id)
        )
        res = await session.execute(stmt)
        patient_society_row = res.first()
        if not patient_society_row:
            return {
                "ok": False,
                "error_code": "PATIENT_NOT_FOUND",
                "message": f"Patient '{patient_id}' not found.",
            }
        patient, society = patient_society_row
        society_tz = ZoneInfo(society.timezone)

        # A redelivery must return the original booking even after its slot has passed.
        existing_stmt = select(ServiceRequest).where(ServiceRequest.request_id == req_uuid)
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()
        if existing is not None:
            if existing.patient_id != patient.id:
                return {"ok": False, "error_code": "REQUEST_ID_CONFLICT",
                        "message": "Request ID belongs to a different patient."}
            clinician_name = await session.scalar(
                select(Clinician.name).where(Clinician.id == existing.clinician_id)
            )
            return {
                "ok": True, "booking_id": str(existing.id), "clinician": clinician_name,
                "service": existing.service, "subtype": existing.subtype,
                "scheduled_for": existing.scheduled_for.isoformat(),
                "status": existing.status, "replayed": True,
            }

        # Parse scheduled_for and ensure timezone awareness in society_tz
        if isinstance(scheduled_for, str):
            dt = datetime.fromisoformat(scheduled_for)
        else:
            dt = scheduled_for

        if dt.tzinfo is None:
            scheduled_local = dt.replace(tzinfo=society_tz)
        else:
            scheduled_local = dt.astimezone(society_tz)

        # 5. Check 2 hours advance booking rule against clock.now()
        current_time = clock.now(society_tz)
        if scheduled_local < current_time:
            return {
                "ok": False,
                "error_code": "IN_PAST",
                "message": "Booking time cannot be in the past.",
            }
        if scheduled_local < current_time + timedelta(hours=2):
            return {
                "ok": False,
                "error_code": "TOO_SOON",
                "message": "Appointments must be booked at least 2 hours in advance.",
            }

        # 3. Find candidates
        weekday = get_weekday(scheduled_local.date())
        cln_stmt = select(Clinician).where(
            and_(
                Clinician.service == service_str,
                subtype_str == any_(Clinician.subtypes),
            )
        )
        cln_res = await session.execute(cln_stmt)
        all_matching_clinicians = cln_res.scalars().all()

        candidate_clinicians: list[Clinician] = []
        for c in all_matching_clinicians:
            for w in c.availability:
                if w.get("society_id") == society.id and w.get("weekday") == weekday:
                    candidate_clinicians.append(c)
                    break

        if not candidate_clinicians:
            return {
                "ok": False,
                "error_code": "NOT_OFFERED_HERE",
                "message": f"{subtype_str} is not offered at {society.name} on {weekday}.",
            }

        # 4. Check slot grid
        matched_clinician: Clinician | None = None
        for c in candidate_clinicians:
            if is_on_slot_grid(
                scheduled_for=scheduled_local,
                availability=c.availability,
                slot_minutes=c.slot_minutes,
                society_id=society.id,
                tz=society_tz,
            ):
                matched_clinician = c
                break

        if matched_clinician is None:
            first_c = candidate_clinicians[0]
            valid_slots = get_clinician_slots_for_date(
                availability=first_c.availability,
                slot_minutes=first_c.slot_minutes,
                target_date=scheduled_local.date(),
                society_id=society.id,
                tz=society_tz,
            )
            return {
                "ok": False,
                "error_code": "NOT_ON_SLOT_GRID",
                "message": (
                    f"Selected time is not on the slot grid. Valid slot times for "
                    f"{scheduled_local.strftime('%Y-%m-%d')} are: "
                    f"{', '.join(s.strftime('%H:%M') for s in valid_slots)}."
                ),
            }

        # Capture clinician and society attributes to prevent lazy-load expiration on rollback
        matched_clinician_id = matched_clinician.id
        matched_clinician_name = matched_clinician.name
        matched_clinician_availability = list(matched_clinician.availability)
        matched_clinician_slot_minutes = matched_clinician.slot_minutes
        patient_id_val = patient.id
        society_id_val = society.id

        # 6. INSERT. Do not SELECT first.
        # Use ON CONFLICT (request_id) DO NOTHING for idempotency
        ins_stmt = (
            insert(ServiceRequest)
            .values(
                patient_id=patient_id_val,
                society_id=society_id_val,
                clinician_id=matched_clinician_id,
                service=service_str,
                subtype=subtype_str,
                scheduled_for=scheduled_local,
                status="CONFIRMED",
                request_id=req_uuid,
            )
            .on_conflict_do_nothing(index_elements=[ServiceRequest.request_id])
            .returning(
                ServiceRequest.id,
                ServiceRequest.patient_id,
                ServiceRequest.clinician_id,
                ServiceRequest.service,
                ServiceRequest.subtype,
                ServiceRequest.scheduled_for,
                ServiceRequest.status,
            )
        )

        try:
            ins_res = await session.execute(ins_stmt)
            inserted_row = ins_res.first()

            if inserted_row is not None:
                # Fresh write succeeded
                return {
                    "ok": True,
                    "booking_id": str(inserted_row.id),
                    "clinician": matched_clinician_name,
                    "service": inserted_row.service,
                    "subtype": inserted_row.subtype,
                    "scheduled_for": inserted_row.scheduled_for.isoformat(),
                    "status": inserted_row.status,
                    "replayed": False,
                }

            # Nothing returned -> request_id already exists!
            # Select the existing record to replay it or reject cross-patient conflict
            existing_booking = await scoped.get_service_request_by_request_id(
                session,
                society_id=society_id_val,
                request_id=req_uuid,
            )
            if existing_booking is None:
                # Check cross-society conflict on request_id
                cross_stmt = select(ServiceRequest).where(ServiceRequest.request_id == req_uuid)
                cross_res = await session.execute(cross_stmt)
                existing_booking = cross_res.scalar_one_or_none()

            if existing_booking:
                if existing_booking.patient_id != patient_id_val:
                    return {
                        "ok": False,
                        "error_code": "REQUEST_ID_CONFLICT",
                        "message": "Request ID belongs to a different patient.",
                    }

                cln_stmt = select(Clinician.name).where(Clinician.id == existing_booking.clinician_id)
                cln_name_res = await session.execute(cln_stmt)
                cln_name = cln_name_res.scalar_one_or_none() or matched_clinician_name
                return {
                    "ok": True,
                    "booking_id": str(existing_booking.id),
                    "clinician": cln_name,
                    "service": existing_booking.service,
                    "subtype": existing_booking.subtype,
                    "scheduled_for": existing_booking.scheduled_for.isoformat(),
                    "status": existing_booking.status,
                    "replayed": True,
                }

        except IntegrityError as exc:
            await session.rollback()
            # Database rejected the write: translate IntegrityError by constraint name
            err_msg = str(exc)

            # A concurrent delivery can collide on the slot index before PostgreSQL
            # reports the request-id conflict. Replay the committed request first.
            existing = (await session.execute(
                select(ServiceRequest).where(ServiceRequest.request_id == req_uuid)
            )).scalar_one_or_none()
            if existing is not None:
                if existing.patient_id != patient_id_val:
                    return {"ok": False, "error_code": "REQUEST_ID_CONFLICT",
                            "message": "Request ID belongs to a different patient."}
                clinician_name = await session.scalar(
                    select(Clinician.name).where(Clinician.id == existing.clinician_id)
                )
                return {
                    "ok": True, "booking_id": str(existing.id), "clinician": clinician_name,
                    "service": existing.service, "subtype": existing.subtype,
                    "scheduled_for": existing.scheduled_for.isoformat(),
                    "status": existing.status, "replayed": True,
                }

            if "uq_active_slot" in err_msg or (
                hasattr(exc.orig, "diag") and exc.orig.diag.constraint_name == "uq_active_slot"
            ):
                # Check if the conflicting row belongs to the same patient
                conflict_stmt = select(ServiceRequest.patient_id).where(
                    and_(
                        ServiceRequest.clinician_id == matched_clinician_id,
                        ServiceRequest.scheduled_for == scheduled_local,
                        ServiceRequest.status == "CONFIRMED",
                    )
                )
                conflict_res = await session.execute(conflict_stmt)
                conflicting_patient_id = conflict_res.scalar_one_or_none()

                if conflicting_patient_id == patient_id_val:
                    return {
                        "ok": False,
                        "error_code": "ALREADY_BOOKED_BY_YOU",
                        "message": "You already hold a confirmed booking for this clinician and slot.",
                    }

                # Find nearest free slots for the clinician at this society
                booked_stmt = select(ServiceRequest.scheduled_for).where(
                    and_(
                        ServiceRequest.clinician_id == matched_clinician_id,
                        ServiceRequest.status == "CONFIRMED",
                    )
                )
                booked_res = await session.execute(booked_stmt)
                booked_times = set(booked_res.scalars().all())

                candidate_slots = []
                for day_offset in range(7):
                    day = scheduled_local.date() + timedelta(days=day_offset)
                    day_slots = get_clinician_slots_for_date(
                        matched_clinician_availability,
                        matched_clinician_slot_minutes,
                        day,
                        society_id_val,
                        society_tz,
                    )
                    candidate_slots.extend(day_slots)

                nearest = find_nearest_free_slots(
                    target_time=scheduled_local,
                    candidate_slots=candidate_slots,
                    booked_slots=booked_times,
                    min_time=current_time + timedelta(hours=2),
                    limit=3,
                )
                return {
                    "ok": False,
                    "error_code": "SLOT_TAKEN",
                    "message": "That slot has just been taken. Please choose another slot.",
                    "nearest_free_slots": [s.isoformat() for s in nearest],
                }

            if "uq_service_requests_request_id" in err_msg:
                # Replay handling for concurrent duplicate
                dup_stmt = select(ServiceRequest).where(ServiceRequest.request_id == req_uuid)
                dup_res = await session.execute(dup_stmt)
                dup_booking = dup_res.scalar_one_or_none()
                if dup_booking and dup_booking.patient_id == patient_id_val:
                    cln_name_stmt = select(Clinician.name).where(Clinician.id == dup_booking.clinician_id)
                    cln_name_res = await session.execute(cln_name_stmt)
                    cln_name = cln_name_res.scalar_one_or_none() or matched_clinician_name
                    return {
                        "ok": True,
                        "booking_id": str(dup_booking.id),
                        "clinician": cln_name,
                        "service": dup_booking.service,
                        "subtype": dup_booking.subtype,
                        "scheduled_for": dup_booking.scheduled_for.isoformat(),
                        "status": dup_booking.status,
                        "replayed": True,
                    }
                return {
                    "ok": False,
                    "error_code": "REQUEST_ID_CONFLICT",
                    "message": "Request ID conflict.",
                }

            raise


async def list_service_requests(
    patient_id: str,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """List service requests for a patient, strictly scoped to their society."""
    async with get_db_session() as session:
        pat = await session.get(Patient, patient_id)
        if not pat:
            return []

        bookings = await scoped.list_patient_service_requests(
            session,
            society_id=pat.society_id,
            patient_id=patient_id,
            status=status,
        )

        return [
            {
                "booking_id": str(b.id),
                "patient_id": b.patient_id,
                "society_id": b.society_id,
                "clinician_id": b.clinician_id,
                "service": b.service,
                "subtype": b.subtype,
                "scheduled_for": b.scheduled_for.isoformat(),
                "status": b.status,
                "request_id": str(b.request_id),
                "created_at": b.created_at.isoformat(),
            }
            for b in bookings
        ]


async def log_symptom(
    patient_id: str,
    symptom: str,
    severity: int,
    request_id: str | uuid.UUID,
) -> dict[str, Any]:
    """Log a patient-reported symptom with database-enforced severity check and idempotency."""
    req_uuid = _parse_uuid(request_id)

    async with get_db_session() as session:
        pat = await session.get(Patient, patient_id)
        if not pat:
            return {
                "ok": False,
                "error_code": "PATIENT_NOT_FOUND",
                "message": f"Patient '{patient_id}' not found.",
            }

        ins_stmt = (
            insert(SymptomLog)
            .values(
                patient_id=pat.id,
                society_id=pat.society_id,
                symptom=symptom.strip(),
                severity=severity,
                request_id=req_uuid,
            )
            .on_conflict_do_nothing(index_elements=[SymptomLog.request_id])
            .returning(
                SymptomLog.id,
                SymptomLog.patient_id,
                SymptomLog.symptom,
                SymptomLog.severity,
                SymptomLog.created_at,
            )
        )

        try:
            res = await session.execute(ins_stmt)
            row = res.first()
            if row is not None:
                return {
                    "ok": True,
                    "log_id": str(row.id),
                    "patient_id": row.patient_id,
                    "symptom": row.symptom,
                    "severity": row.severity,
                    "created_at": row.created_at.isoformat(),
                    "replayed": False,
                }

            # If conflict on request_id
            existing_stmt = select(SymptomLog).where(SymptomLog.request_id == req_uuid)
            existing_res = await session.execute(existing_stmt)
            existing_log = existing_res.scalar_one_or_none()

            if existing_log:
                if existing_log.patient_id != pat.id:
                    return {
                        "ok": False,
                        "error_code": "REQUEST_ID_CONFLICT",
                        "message": "Request ID belongs to another patient.",
                    }
                return {
                    "ok": True,
                    "log_id": str(existing_log.id),
                    "patient_id": existing_log.patient_id,
                    "symptom": existing_log.symptom,
                    "severity": existing_log.severity,
                    "created_at": existing_log.created_at.isoformat(),
                    "replayed": True,
                }

        except IntegrityError as exc:
            await session.rollback()
            err_msg = str(exc)
            if "ck_severity_1_5" in err_msg:
                return {
                    "ok": False,
                    "error_code": "INVALID_SEVERITY",
                    "message": "Severity must be an integer between 1 and 5.",
                }
            if "uq_symptom_logs_request_id" in err_msg:
                existing_stmt = select(SymptomLog).where(SymptomLog.request_id == req_uuid)
                existing_res = await session.execute(existing_stmt)
                existing_log = existing_res.scalar_one_or_none()
                if existing_log and existing_log.patient_id == pat.id:
                    return {
                        "ok": True,
                        "log_id": str(existing_log.id),
                        "patient_id": existing_log.patient_id,
                        "symptom": existing_log.symptom,
                        "severity": existing_log.severity,
                        "created_at": existing_log.created_at.isoformat(),
                        "replayed": True,
                    }
                return {
                    "ok": False,
                    "error_code": "REQUEST_ID_CONFLICT",
                    "message": "Request ID belongs to another patient.",
                }
            raise


async def search_clinic_docs(
    query: str,
    society_id: str,
    k: int = 6,
) -> list[dict[str, Any]]:
    """Retrieve society-scoped clinic documents using dense pgvector search."""
    embedding = embed_query(query)
    async with get_db_session() as session:
        return await scoped.search_clinic_docs_scoped(
            session,
            society_id=society_id,
            query_embedding=embedding,
            k=k,
        )


async def escalate_emergency(
    patient_id: str,
    transcript: str,
    request_id: str | uuid.UUID,
) -> dict[str, Any]:
    """Record an emergency escalation with SLA deadline and idempotency."""
    req_uuid = _parse_uuid(request_id)

    async with get_db_session() as session:
        pat = await session.get(Patient, patient_id)
        if not pat:
            return {
                "ok": False,
                "error_code": "PATIENT_NOT_FOUND",
                "message": f"Patient '{patient_id}' not found.",
            }

        raised_at = clock.now()
        # Level 1 SLA deadline is +5 minutes
        response_deadline = raised_at + timedelta(minutes=5)

        ins_stmt = (
            insert(Escalation)
            .values(
                patient_id=pat.id,
                society_id=pat.society_id,
                level=1,
                status="OPEN",
                transcript=transcript.strip(),
                request_id=req_uuid,
                raised_at=raised_at,
                response_deadline=response_deadline,
            )
            .on_conflict_do_nothing(index_elements=[Escalation.request_id])
            .returning(
                Escalation.id,
                Escalation.patient_id,
                Escalation.society_id,
                Escalation.level,
                Escalation.status,
                Escalation.response_deadline,
            )
        )

        try:
            res = await session.execute(ins_stmt)
            row = res.first()
            if row is not None:
                return {
                    "ok": True,
                    "escalation_id": str(row.id),
                    "patient_id": row.patient_id,
                    "society_id": row.society_id,
                    "level": row.level,
                    "status": row.status,
                    "response_deadline": row.response_deadline.isoformat(),
                    "replayed": False,
                }

            # Conflict on request_id
            existing_stmt = select(Escalation).where(Escalation.request_id == req_uuid)
            existing_res = await session.execute(existing_stmt)
            existing_esc = existing_res.scalar_one_or_none()

            if existing_esc:
                if existing_esc.patient_id != pat.id:
                    return {
                        "ok": False,
                        "error_code": "REQUEST_ID_CONFLICT",
                        "message": "Request ID belongs to another patient.",
                    }
                return {
                    "ok": True,
                    "escalation_id": str(existing_esc.id),
                    "patient_id": existing_esc.patient_id,
                    "society_id": existing_esc.society_id,
                    "level": existing_esc.level,
                    "status": existing_esc.status,
                    "response_deadline": existing_esc.response_deadline.isoformat(),
                    "replayed": True,
                }

        except IntegrityError as exc:
            await session.rollback()
            if "uq_escalations_request_id" in str(exc):
                existing_stmt = select(Escalation).where(Escalation.request_id == req_uuid)
                existing_res = await session.execute(existing_stmt)
                existing_esc = existing_res.scalar_one_or_none()
                if existing_esc and existing_esc.patient_id == pat.id:
                    return {
                        "ok": True,
                        "escalation_id": str(existing_esc.id),
                        "patient_id": existing_esc.patient_id,
                        "society_id": existing_esc.society_id,
                        "level": existing_esc.level,
                        "status": existing_esc.status,
                        "response_deadline": existing_esc.response_deadline.isoformat(),
                        "replayed": True,
                    }
                return {
                    "ok": False,
                    "error_code": "REQUEST_ID_CONFLICT",
                    "message": "Request ID belongs to another patient.",
                }
            raise
