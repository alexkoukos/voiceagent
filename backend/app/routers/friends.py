from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Friend
from app.schemas import FriendCreate, FriendOut

router = APIRouter(prefix="/friends", tags=["friends"])


@router.post("", response_model=FriendOut)
async def create_friend(payload: FriendCreate, db: AsyncSession = Depends(get_db)):
    friend = Friend(name=payload.name, phone_number=payload.phone_number)
    db.add(friend)
    await db.commit()
    await db.refresh(friend)
    return friend


@router.get("", response_model=list[FriendOut])
async def list_friends(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Friend).order_by(Friend.name))
    return result.scalars().all()
