from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Friend
from app.schemas import FriendCreate, FriendOut

router = APIRouter(prefix="/friends", tags=["friends"])


async def _get_friend(db: AsyncSession, friend_id: str) -> Friend:
    friend = await db.get(Friend, friend_id)
    if friend is None or friend.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Friend not found")
    return friend


@router.post("", response_model=FriendOut)
async def create_friend(payload: FriendCreate, db: AsyncSession = Depends(get_db)):
    friend = Friend(name=payload.name, phone_number=payload.phone_number)
    db.add(friend)
    await db.commit()
    await db.refresh(friend)
    return friend


@router.get("", response_model=list[FriendOut])
async def list_friends(include_deleted: bool = False, db: AsyncSession = Depends(get_db)):
    """Deleted friends are left out unless asked for (history needs their names)."""
    query = select(Friend).order_by(Friend.name)
    if not include_deleted:
        query = query.where(Friend.deleted_at.is_(None))
    result = await db.execute(query)
    return result.scalars().all()


@router.put("/{friend_id}", response_model=FriendOut)
async def update_friend(
    friend_id: str, payload: FriendCreate, db: AsyncSession = Depends(get_db)
):
    friend = await _get_friend(db, friend_id)
    friend.name = payload.name
    friend.phone_number = payload.phone_number
    await db.commit()
    await db.refresh(friend)
    return friend


@router.delete("/{friend_id}", status_code=204)
async def delete_friend(friend_id: str, db: AsyncSession = Depends(get_db)):
    friend = await _get_friend(db, friend_id)
    friend.deleted_at = datetime.utcnow()
    await db.commit()
