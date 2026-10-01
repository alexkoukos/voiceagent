"""Running practices after go-live: patient data requests (OP8), blocked numbers and cost
caps (OP10), offboarding (OP7), and operator alerts (OP9)."""

import csv
import io
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import admin_changes, data_requests, events, notifications, onboarding, receptionist
from app.database import get_db
from app.models import AdminLink, Alert, Appointment, Call, CallStatus, DataRequest, Practice
from app.routers.practices import _get
from app.schemas import (
    AdminPinIn, AlertOut, AppointmentOut, CallRouting, ConfigVersionOut, CostCapIn, DataRequestOut, GoogleImportIn, PhoneIn, PriceListIn,
    UsageOut,
)

router = APIRouter(prefix="/practices", tags=["ops"])
alerts_router = APIRouter(prefix="/alerts", tags=["ops"])

# Cancels every call forwarding on Greek mobile networks (Cosmote, Vodafone, Nova).
FORWARDING_OFF = "##002#"


def _mobile_forwarding_verified(practice: Practice) -> bool:
    setup = practice.call_routing or {}
    return (setup.get("phone_system") == "mobile" and setup.get("carrier_configuration_confirmed")
            and setup.get("provider", "").lower() == "nova gr")


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
        stop = (f"Πληκτρολογήστε {FORWARDING_OFF} από το κινητό της επιχείρησης."
                if greek else f"Dial {FORWARDING_OFF} from the business mobile.") if _mobile_forwarding_verified(practice) else (
                "Ζητήστε από τον πάροχο ή τον διαχειριστή τηλεφωνικού κέντρου να απενεργοποιήσει την προώθηση."
                if greek else "Ask the phone provider or PBX administrator to disable forwarding.")
        body = (f"{stop}\nΟι ηχογραφήσεις και οι απομαγνητοφωνήσεις διαγράφονται σε 30 ημέρες." if greek else
                f"{stop}\nRecordings and transcripts are deleted within 30 days.")
        await notifications.queue_business(db, practice, kind="offboarded", subject=subject, body=body,
                                           dedupe_key=f"offboard:{practice.id}:{now.date()}")
    await db.commit()
    notifications.kick()
    return {"offboarded_at": practice.offboarded_at,
            "forwarding_off_code": FORWARDING_OFF if _mobile_forwarding_verified(practice) else None,
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


# --- onboarding imports (O1, O2, O4) and forwarding (O5) ---


def _import_error(e: onboarding.ImportError_) -> HTTPException:
    missing = e.code.endswith("_missing")
    return HTTPException(status_code=503 if missing else 422, detail=e.code)


@router.post("/{practice_id}/imports/google", response_model=ConfigVersionOut | None)
async def import_google(practice_id: str, payload: GoogleImportIn, db: AsyncSession = Depends(get_db)):
    """Hours, address, phone and website from the Google profile, into the approval queue.
    None when nothing differs from today's config."""
    practice = await _get(db, practice_id)
    try:
        version = await onboarding.import_google(db, practice, payload.query)
    except onboarding.ImportError_ as e:
        raise _import_error(e)
    await db.commit()
    events.publish(f"practice:{practice.id}")
    return version


@router.post("/{practice_id}/imports/price-list", response_model=ConfigVersionOut | None)
async def import_price_list(practice_id: str, payload: PriceListIn, db: AsyncSession = Depends(get_db)):
    """Services, prices and durations read from a photo, PDF or web page, into the approval queue."""
    practice = await _get(db, practice_id)
    if not payload.url and not payload.data_base64:
        raise HTTPException(status_code=422, detail="file_or_url_required")
    try:
        version = await onboarding.import_price_list(db, practice, data_b64=payload.data_base64,
                                                     mime_type=payload.mime_type, url=payload.url)
    except onboarding.ImportError_ as e:
        raise _import_error(e)
    await db.commit()
    events.publish(f"practice:{practice.id}")
    return version


@router.get("/{practice_id}/forwarding")
async def forwarding(practice_id: str, mode: Literal["backup", "full"] = "backup", db: AsyncSession = Depends(get_db)):
    """Carrier-specific codes only for an explicitly confirmed compatible mobile setup."""
    practice = await _get(db, practice_id)
    setup = practice.call_routing or {}
    if not _mobile_forwarding_verified(practice):
        raise HTTPException(status_code=409, detail="Use the clinic-specific routing instructions; mobile codes are unverified")
    target = setup.get("ai_destination_number")
    if not target:
        raise HTTPException(status_code=409, detail="no_agent_number")
    if mode != "backup" or setup.get("mode") != "human_first":
        raise HTTPException(status_code=409, detail="No verified mobile dial codes for this routing mode")
    if setup.get("no_answer_seconds") not in (5, 10, 15, 20, 25, 30):
        raise HTTPException(status_code=409, detail="No verified Nova no-answer timeout")
    allowed = {"no_answer"}
    if setup.get("busy_behavior") == "forward_to_ai":
        allowed.add("busy")
    codes = [code for code in onboarding.forwarding_codes(target, mode, setup["no_answer_seconds"])
             if code["what"] in allowed]
    return {"target": target, "mode": mode,
            "codes": codes,
            "off": FORWARDING_OFF}


@router.get("/{practice_id}/routing/setup")
async def routing_setup(practice_id: str, db: AsyncSession = Depends(get_db)):
    return onboarding.routing_instructions(await _get(db, practice_id))


@router.put("/{practice_id}/routing/setup")
async def update_routing_setup(practice_id: str, payload: CallRouting, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    if payload.ai_destination_number and payload.ai_destination_number not in (practice.phone_numbers or []):
        raise HTTPException(status_code=422, detail="AI destination must be a provisioned inbound number")
    practice.call_routing = payload.model_dump(mode="json")
    await db.commit()
    events.publish(f"practice:{practice.id}")
    return onboarding.routing_instructions(practice)


# --- changes by phone and SMS (OP2) ---


@router.put("/{practice_id}/admin-pin", status_code=204)
async def set_admin_pin(practice_id: str, payload: AdminPinIn, db: AsyncSession = Depends(get_db)):
    """The PIN staff say on the phone before a change; null turns changes by phone off."""
    practice = await _get(db, practice_id)
    practice.admin_pin_hash = admin_changes.hash_pin(practice.id, payload.pin) if payload.pin else None
    await db.commit()


# --- operator alerts (OP9) ---


@alerts_router.get("", response_model=list[AlertOut])
async def list_alerts(open_only: bool = True, db: AsyncSession = Depends(get_db)):
    q = select(Alert)
    if db.info.get("tenant_id"):
        q = q.where(Alert.practice_id == db.info["tenant_id"])
    if open_only:
        q = q.where(Alert.acked_at.is_(None))
    return (await db.execute(q.order_by(Alert.created_at.desc()).limit(200))).scalars().all()


@alerts_router.post("/{alert_id}/ack", response_model=AlertOut)
async def ack_alert(alert_id: str, db: AsyncSession = Depends(get_db)):
    alert = await db.get(Alert, alert_id)
    if alert is None or (db.info.get("tenant_id") and alert.practice_id != db.info["tenant_id"]):
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.acked_at = alert.acked_at or datetime.utcnow()
    await db.commit()
    events.publish("alerts")
    return alert


from datetime import date
from decimal import Decimal
from pydantic import BaseModel, Field
from app.auth import require_master_token
from app.models import BillingAccount, BillingDraft
from app import billing


class BillingIn(BaseModel):
    pilot_started_on: date
    monthly_fee: Decimal = Field(ge=0, max_digits=10, decimal_places=2)


@router.put("/{practice_id}/billing", dependencies=[Depends(require_master_token)])
async def configure_billing(practice_id: str, payload: BillingIn, db: AsyncSession = Depends(get_db)):
    await _get(db, practice_id)
    account = await db.get(BillingAccount, practice_id)
    if account:
        if account.pilot_started_on != payload.pilot_started_on:
            raise HTTPException(status_code=409, detail="Pilot start is immutable once configured")
        account.monthly_fee = payload.monthly_fee
    else:
        db.add(BillingAccount(practice_id=practice_id, **payload.model_dump()))
    await db.commit()
    return await billing_status(practice_id, db)


@router.get("/{practice_id}/billing")
async def billing_status(practice_id: str, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    account = await db.get(BillingAccount, practice_id)
    drafts = (await db.execute(select(BillingDraft).where(BillingDraft.practice_id == practice_id)
                               .order_by(BillingDraft.period_start.desc()))).scalars()
    return {"pilot_started_on": account.pilot_started_on if account else None,
            "paid_period_starts_on": account.pilot_started_on + timedelta(days=billing.PILOT_DAYS) if account else None,
            "monthly_fee": str(account.monthly_fee) if account else None,
            "guarantee_threshold": practice.guarantee_threshold,
            "provider_configured": False,
            "drafts": [{"id": d.id, "period_start": d.period_start, "period_end": d.period_end,
                        "bookings": d.bookings, "threshold": d.threshold, "amount": str(d.amount),
                        "status": d.status, "call_ids": d.call_ids} for d in drafts]}


@router.post("/{practice_id}/billing/prepare", dependencies=[Depends(require_master_token)])
async def prepare_billing(practice_id: str, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    await billing.prepare_due(db, practice, datetime.now(timezone.utc))
    await db.commit()
    return await billing_status(practice_id, db)


from typing import Literal
from app.models import HealthCheck
from app import alerts


class HealthReportIn(BaseModel):
    kind: Literal["web", "phone"]
    number: str | None = None
    success: bool
    detail: str = Field(default="", max_length=300)


@router.post("/{practice_id}/health-checks")
async def report_health(practice_id: str, payload: HealthReportIn, db: AsyncSession = Depends(get_db)):
    practice = await _get(db, practice_id)
    if (payload.kind == "phone" and payload.number not in (practice.phone_numbers or [])) or (payload.kind == "web" and payload.number):
        raise HTTPException(status_code=422, detail="Check target does not belong to this practice")
    check = HealthCheck(practice_id=practice_id, **payload.model_dump())
    db.add(check)
    if not payload.success:
        await alerts.raise_alert(db, practice, "synthetic_failed", f"{practice.name}: {payload.kind} call check failed",
                                 payload.detail, dedupe_key=f"synthetic:{practice_id}:{payload.kind}:{datetime.utcnow():%Y-%m-%dT%H}")
    await db.commit()
    notifications.kick()
    return {"id": check.id}


@router.get("/{practice_id}/health-checks")
async def recent_health(practice_id: str, db: AsyncSession = Depends(get_db)):
    await _get(db, practice_id)
    rows = (await db.execute(select(HealthCheck).where(HealthCheck.practice_id == practice_id)
                             .order_by(HealthCheck.created_at.desc()).limit(100))).scalars()
    return [{"kind": x.kind, "number": x.number, "success": x.success, "detail": x.detail,
             "created_at": x.created_at} for x in rows]
