"""Running practices after go-live: patient data requests (OP8), blocked numbers and cost
caps (OP10), offboarding (OP7), and operator alerts (OP9)."""

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import data_requests, events, notifications, receptionist
from app.database import get_db
from app.models import AdminLink, Alert, Appointment, Call, CallStatus, DataRequest, Practice
from app.routers.practices import _get
from app.schemas import AlertOut, AppointmentOut, CostCapIn, DataRequestOut, PhoneIn, UsageOut

router = APIRouter(prefix="/practices", tags=["ops"])
alerts_router = APIRouter(prefix="/alerts", tags=["ops"])

# Cancels every call forwarding on Greek mobile networks (Cosmote, Vodafone, Nova).
FORWARDING_OFF = "##002#"


# --- patient data requests (OP8) ---


@router.post("/{practice_id}/data/export")
async def export_caller(practice_id: str, payload: PhoneIn, db: AsyncSession = Depends(get_db)):
    """Everything this practice holds about one caller, as JSON (the phone travels in the body,
    not the URL, so it stays out of access logs)."""
    practice = await _get(db, practice_id)
    out = await data_requests.export(db, practice, payload.phone)
    await db.commit()
    return out


@router.post("/{practice_id}/data/erase")
async def erase_caller(practice_id: str, payload: PhoneIn, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    try:
        counts = await data_requests.erase(db, practice, payload.phone, datetime.now(timezone.utc))
    except data_requests.HasUpcoming as e:
        raise HTTPException(status_code=409, detail={
            "error": "has_upcoming",
            "appointments": [AppointmentOut.model_validate(a).model_dump(mode="json") for a in e.appointments],
        })
    await db.commit()
    events.publish(f"practice:{practice.id}")
    return counts


@router.get("/{practice_id}/data/requests", response_model=list[DataRequestOut])
async def list_data_requests(practice_id: str, db: AsyncSession = Depends(get_db)):
    await _get(db, practice_id)
    return (await db.execute(select(DataRequest).where(DataRequest.practice_id == practice_id)
                             .order_by(DataRequest.created_at.desc()).limit(200))).scalars().all()


# --- blocked numbers and cost cap (OP10) ---


@router.get("/{practice_id}/usage", response_model=UsageOut)
async def usage(practice_id: str, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    return UsageOut(month_cost_eur=round(await receptionist.month_cost(db, practice), 2),
                    monthly_cost_cap_eur=practice.monthly_cost_cap_eur,
                    blocked_numbers=practice.blocked_numbers or [], offboarded_at=practice.offboarded_at)


@router.put("/{practice_id}/cost-cap", response_model=UsageOut)
async def set_cost_cap(practice_id: str, payload: CostCapIn, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    practice.monthly_cost_cap_eur = payload.monthly_cost_cap_eur
    await db.commit()
    return await usage(practice_id, db)


@router.post("/{practice_id}/blocked", response_model=UsageOut)
async def block(practice_id: str, payload: PhoneIn, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    if payload.phone not in (practice.blocked_numbers or []):
        practice.blocked_numbers = [*(practice.blocked_numbers or []), payload.phone]
    await db.commit()
    return await usage(practice_id, db)


@router.post("/{practice_id}/unblock", response_model=UsageOut)
async def unblock(practice_id: str, payload: PhoneIn, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    practice.blocked_numbers = [n for n in practice.blocked_numbers or [] if n != payload.phone]
    await db.commit()
    return await usage(practice_id, db)


# --- offboarding (OP7) ---


@router.post("/{practice_id}/offboard")
async def offboard(practice_id: str, db: AsyncSession = Depends(get_db)):
    """The agent stops answering at once; links stop working and queued reminder calls are
    cancelled. The owner gets the code to turn forwarding off. Recordings and transcripts are
    deleted by the scheduler 30 days later. Undo with /reactivate before then."""
    practice = await _get(db, practice_id)
    now = datetime.utcnow()
    if practice.offboarded_at is None:
        practice.offboarded_at = now
        for link in (await db.execute(select(AdminLink).where(AdminLink.practice_id == practice.id))).scalars():
            link.revoked = True
        for call in (await db.execute(select(Call).where(
            Call.practice_id == practice.id, Call.status == CallStatus.queued))).scalars():
            call.status, call.ended_at = CallStatus.cancelled, now
        greek = practice.language == "el"
        subject = (f"{practice.name}: ο ψηφιακός βοηθός απενεργοποιήθηκε" if greek
                   else f"{practice.name}: the digital assistant is off")
        body = (f"Για να σταματήσει η προώθηση, πληκτρολογήστε {FORWARDING_OFF} και πατήστε κλήση από το κινητό "
                "της επιχείρησης. Για σταθερό, ρωτήστε τον πάροχό σας.\n"
                "Οι ηχογραφήσεις και οι απομαγνητοφωνήσεις διαγράφονται σε 30 ημέρες." if greek else
                f"To stop forwarding, dial {FORWARDING_OFF} and press call on the business mobile. For a landline, "
                "ask your provider.\nRecordings and transcripts are deleted within 30 days.")
        await notifications.queue_business(db, practice, kind="offboarded", subject=subject, body=body,
                                           dedupe_key=f"offboard:{practice.id}:{now.date()}")
    await db.commit()
    notifications.kick()
    return {"offboarded_at": practice.offboarded_at, "forwarding_off_code": FORWARDING_OFF,
            "export_csv": f"/practices/{practice.id}/export.csv"}


@router.post("/{practice_id}/reactivate")
async def reactivate(practice_id: str, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    practice.offboarded_at = None
    await db.commit()
    return {"offboarded_at": None}


@router.get("/{practice_id}/export.csv")
async def export_csv(practice_id: str, db: AsyncSession = Depends(get_db)):
    """Calls and appointments for the owner (OP7), one CSV."""
    practice = await _get(db, practice_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["type", "when", "caller_or_customer", "phone", "outcome_or_service", "status", "duration_s", "summary"])
    for c in (await db.execute(select(Call).where(Call.practice_id == practice.id).order_by(Call.created_at))).scalars():
        w.writerow(["call", c.created_at.isoformat(), "", c.caller_number or "", c.outcome or "",
                    c.status.value, c.duration_seconds or "", c.summary or ""])
    for a in (await db.execute(select(Appointment).where(Appointment.practice_id == practice.id)
                               .order_by(Appointment.starts_at))).scalars():
        w.writerow(["appointment", a.starts_at.isoformat(), a.customer_name, a.customer_phone or "", a.service_name,
                    a.status.value if hasattr(a.status, "value") else a.status, "", ""])
    name = "".join(ch for ch in practice.name if ch.isalnum()) or "practice"
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}.csv"', "Cache-Control": "no-store"})


# --- operator alerts (OP9) ---


@alerts_router.get("", response_model=list[AlertOut])
async def list_alerts(open_only: bool = True, db: AsyncSession = Depends(get_db)):
    q = select(Alert)
    if open_only:
        q = q.where(Alert.acked_at.is_(None))
    return (await db.execute(q.order_by(Alert.created_at.desc()).limit(200))).scalars().all()


@alerts_router.post("/{alert_id}/ack", response_model=AlertOut)
async def ack_alert(alert_id: str, db: AsyncSession = Depends(get_db)):
    alert = await db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.acked_at = alert.acked_at or datetime.utcnow()
    await db.commit()
    events.publish("alerts")
    return alert
