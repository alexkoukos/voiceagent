from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import PromptTemplate
from app.schemas import PromptTemplateCreate, PromptTemplateOut

router = APIRouter(prefix="/templates", tags=["templates"])


@router.post("", response_model=PromptTemplateOut)
async def create_template(
    payload: PromptTemplateCreate, db: AsyncSession = Depends(get_db)
):
    template = PromptTemplate(**payload.model_dump())
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return template


@router.get("", response_model=list[PromptTemplateOut])
async def list_templates(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PromptTemplate).order_by(PromptTemplate.title))
    return result.scalars().all()


@router.delete("/{template_id}", status_code=204)
async def delete_template(template_id: str, db: AsyncSession = Depends(get_db)):
    template = await db.get(PromptTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")
    await db.delete(template)
    await db.commit()
