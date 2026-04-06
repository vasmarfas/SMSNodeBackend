from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from core.api.dependencies import get_current_user
from core.db.database import get_db
from core.db.models import SMSTemplate, User, RoleEnum

router = APIRouter(prefix="/api/v1/user/templates", tags=["Templates"])

class TemplateResponse(BaseModel):
    id: int
    name: str
    content: str
    category: str
    is_global: bool

    class Config:
        from_attributes = True

class TemplateCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    content: str = Field(..., min_length=1)
    category: str = Field(default="general", max_length=50)
    is_global: bool = False

class TemplateUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    content: Optional[str] = Field(None, min_length=1)
    is_global: Optional[bool] = None

@router.get("", response_model=List[TemplateResponse])
async def list_templates(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Возвращает шаблоны пользователя + глобальные."""
    stmt = select(SMSTemplate).where(
        or_(
            SMSTemplate.user_id == user.id,
            SMSTemplate.is_global == True
        )
    ).order_by(SMSTemplate.name)
    r = await db.execute(stmt)
    return r.scalars().all()

@router.post("", response_model=TemplateResponse, status_code=status.HTTP_201_CREATED)
async def create_template(
    body: TemplateCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Создать новый шаблон (личный или глобальный для админа)."""
    is_global = False
    if body.is_global and user.role == RoleEnum.ADMIN:
        is_global = True
        
    tmpl = SMSTemplate(
        name=body.name,
        content=body.content,
        category=body.category,
        is_global=is_global,
        user_id=None if is_global else user.id
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl

@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Удалить шаблон (только свой или админ может удалить любой)."""
    tmpl = await db.get(SMSTemplate, template_id)
    if not tmpl:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Шаблон не найден")
    if tmpl.user_id != user.id and user.role != RoleEnum.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет прав на удаление этого шаблона")
    
    await db.delete(tmpl)
    await db.commit()

@router.patch("/{template_id}", response_model=TemplateResponse)
async def update_template(
    template_id: int,
    body: TemplateUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Обновить шаблон (свой или любой, если админ)."""
    tmpl = await db.get(SMSTemplate, template_id)
    if not tmpl:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Шаблон не найден")
    
    if tmpl.user_id != user.id and user.role != RoleEnum.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет прав на изменение этого шаблона")
    
    if body.name is not None:
        tmpl.name = body.name
    if body.content is not None:
        tmpl.content = body.content
    if body.is_global is not None and user.role == RoleEnum.ADMIN:
        tmpl.is_global = body.is_global
        if body.is_global:
            tmpl.user_id = None
        else:
            tmpl.user_id = user.id
            
    await db.commit()
    await db.refresh(tmpl)
    return tmpl
