from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from pydantic import BaseModel

from core.db.database import get_db
from core.db.models import User, IncomingRule, IncomingRuleActionEnum
from core.api.routers.auth import get_current_user

router = APIRouter(prefix="/user/rules", tags=["Rules"])

class IncomingRuleCreate(BaseModel):
    name: str
    keyword: Optional[str] = None
    action_type: IncomingRuleActionEnum
    target_data: str
    is_active: bool = True

class IncomingRuleResponse(BaseModel):
    id: int
    name: str
    keyword: Optional[str]
    action_type: IncomingRuleActionEnum
    target_data: str
    is_active: bool

    class Config:
        from_attributes = True

@router.get("", response_model=List[IncomingRuleResponse])
async def get_rules(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(IncomingRule).where(IncomingRule.user_id == current_user.id)
    )
    return result.scalars().all()

@router.post("", response_model=IncomingRuleResponse)
async def create_rule(
    data: IncomingRuleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    new_rule = IncomingRule(
        user_id=current_user.id,
        name=data.name,
        keyword=data.keyword,
        action_type=data.action_type,
        target_data=data.target_data,
        is_active=data.is_active
    )
    db.add(new_rule)
    await db.commit()
    await db.refresh(new_rule)
    return new_rule

@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    rule = await db.get(IncomingRule, rule_id)
    if not rule or rule.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Rule not found")
    
    await db.delete(rule)
    await db.commit()
