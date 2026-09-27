"""Keep inbound numbers and external calendar credentials owned by one practice.

The transaction lock serializes assignments across tenants, including OAuth callbacks.
"""
from fastapi import HTTPException
from sqlalchemy import select, text

from app.models import CalendarConnection, Practice, Staff


async def validate_assignments(db, practice_id, *, numbers=(), calendar_id=None):
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('tenant-resource-assignments'))"))
    if numbers:
        rows = (await db.execute(select(Practice).where(Practice.id != practice_id))).scalars()
        if any(set(numbers).intersection(p.phone_numbers or []) for p in rows):
            raise HTTPException(status_code=409, detail="Number already assigned")
    if calendar_id:
        for model in (Practice, Staff, CalendarConnection):
            owner = model.id if model is Practice else model.practice_id
            other = (await db.execute(select(model).where(owner != practice_id,
                                                          model.calendar_id == calendar_id).limit(1))).first()
            if other:
                raise HTTPException(status_code=409, detail="Calendar already assigned")
