"""Google sign-in for calendars (O3). The callback is public: Google's browser redirect has no
API key, so the signed, 10-minute `state` carries which practice and staff member it is for."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events, gcal, google_oauth
from app.database import get_db
from app.models import CalendarConnection, Practice, Staff
from app.routers.practices import _get

public = APIRouter(prefix="/oauth", tags=["oauth"])
router = APIRouter(prefix="/practices", tags=["oauth"])


class ConnectIn(BaseModel):
    staff_id: str | None = None


class ConnectionOut(BaseModel):
    id: str
    staff_id: str | None
    google_email: str
    calendar_id: str


@router.post("/{practice_id}/calendar/connect")
async def connect(practice_id: str, payload: ConnectIn, db: AsyncSession = Depends(get_db)):
    """The Google sign-in page to open in a private browser session."""
    await _get(db, practice_id)
    if payload.staff_id:
        person = await db.get(Staff, payload.staff_id)
        if person is None or person.practice_id != practice_id:
            raise HTTPException(status_code=404, detail="Staff not found")
    try:
        return {"url": google_oauth.authorize_url(practice_id, payload.staff_id),
                "callback_scheme": google_oauth.APP_RETURN.split(":")[0]}
    except google_oauth.OAuthError as e:
        raise HTTPException(status_code=503, detail=e.code)


@router.get("/{practice_id}/calendar/connections", response_model=list[ConnectionOut])
async def connections(practice_id: str, db: AsyncSession = Depends(get_db)):
    await _get(db, practice_id)
    return (await db.execute(select(CalendarConnection).where(CalendarConnection.practice_id == practice_id)
                             .order_by(CalendarConnection.created_at))).scalars().all()


@router.delete("/{practice_id}/calendar/connections/{connection_id}", status_code=204)
async def disconnect(practice_id: str, connection_id: str, db: AsyncSession = Depends(get_db)):
    conn = await db.get(CalendarConnection, connection_id)
    if conn is None or conn.practice_id != practice_id:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        await google_oauth.revoke(conn.refresh_token)
    except Exception:
        pass
    await _unassign(db, conn)
    await db.delete(conn)
    await db.commit()
    gcal._oauth_tokens.pop(conn.calendar_id, None)
    events.publish(f"practice:{practice_id}")


async def _unassign(db: AsyncSession, conn: CalendarConnection) -> None:
    if conn.staff_id:
        person = await db.get(Staff, conn.staff_id)
        if person and person.calendar_id == conn.calendar_id:
            person.calendar_id = None
    else:
        practice = await db.get(Practice, conn.practice_id)
        if practice and practice.calendar_id == conn.calendar_id:
            practice.calendar_id = None


@public.get("/google/callback")
async def callback(state: str = "", code: str = "", error: str = "", db: AsyncSession = Depends(get_db)):
    def back(result: str) -> RedirectResponse:
        return RedirectResponse(f"{google_oauth.APP_RETURN}?result={result}", status_code=302)

    if error or not code:
        return back("cancelled")
    try:
        practice_id, staff_id = google_oauth.read_state(state)
        refresh_token, email = await google_oauth.exchange(code)
    except google_oauth.OAuthError as e:
        return back(e.code)
    # The primary calendar's id is the account's email.
    for old in (await db.execute(select(CalendarConnection).where(
        CalendarConnection.practice_id == practice_id, CalendarConnection.staff_id == staff_id,
    ))).scalars():
        await db.delete(old)
    db.add(CalendarConnection(practice_id=practice_id, staff_id=staff_id, google_email=email,
                              calendar_id=email, refresh_token=refresh_token))
    if staff_id:
        person = await db.get(Staff, staff_id)
        if person:
            person.calendar_id = email
    else:
        practice = await db.get(Practice, practice_id)
        if practice:
            practice.calendar_id = email
    await db.commit()
    gcal._oauth_tokens.pop(email, None)
    events.publish(f"practice:{practice_id}")
    return back("ok")
