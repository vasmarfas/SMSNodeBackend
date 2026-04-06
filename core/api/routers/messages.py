"""
Роутер диалогов и сообщений — /api/v1/user/dialogs, /api/v1/user/messages.
Доступ к диалогам и отправка SMS для текущего пользователя (JWT).
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.api.dependencies import get_current_user, require_admin
from core.db.database import get_db
from core.db.models import ContactGroup, Message, SimCard, User

router = APIRouter(prefix="/api/v1", tags=["Messages"])


class MessageResponse(BaseModel):
    id: int
    sim_card_id: Optional[int]
    external_phone: str
    direction: str
    text: str
    status: str
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


class AdminMessageResponse(BaseModel):
    id: int
    sim_card_id: Optional[int]
    sim_card_label: Optional[str] = None
    username: Optional[str] = None
    external_phone: str
    direction: str
    text: str
    status: str
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


class DialogSummary(BaseModel):
    external_phone: str
    last_text: str
    last_at: Optional[datetime]
    last_direction: str


class SendMessageRequest(BaseModel):
    phone: str = Field(..., min_length=10, max_length=20)
    text: str = Field(..., min_length=1, max_length=160)
    sim_card_id: Optional[int] = None


class SendMessageResponse(BaseModel):
    job_id: str
    status: str = "queued"

class SendGroupRequest(BaseModel):
    group_id: int
    text: str = Field(..., min_length=1, max_length=160)
    sim_card_id: Optional[int] = None


class SendGroupResponse(BaseModel):
    total: int
    job_ids: List[str]


class RecentMessagesResponse(BaseModel):
    messages: List[MessageResponse]


async def _get_user_sim_ids(session: AsyncSession, user_id: int) -> List[int]:
    r = await session.execute(
        select(SimCard.id).where(SimCard.assigned_user_id == user_id)
    )
    return [row[0] for row in r.fetchall()]


@router.get(
    "/user/dialogs",
    response_model=List[DialogSummary],
    summary="[User] Список диалогов (по внешнему номеру)",
)
async def list_dialogs(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sim_ids = await _get_user_sim_ids(db, current_user.id)
    if not sim_ids:
        return []

    r = await db.execute(
        select(Message)
        .where(Message.sim_card_id.in_(sim_ids))
        .order_by(desc(Message.created_at))
        .limit(1000)
    )
    messages = r.scalars().all()

    seen: dict[str, Message] = {}
    for msg in messages:
        if msg.external_phone not in seen:
            seen[msg.external_phone] = msg

    return [
        DialogSummary(
            external_phone=m.external_phone,
            last_text=(m.text[:80] + "…") if len(m.text) > 80 else m.text,
            last_at=m.created_at,
            last_direction=m.direction.value,
        )
        for m in seen.values()
    ]


@router.get(
    "/user/dialogs/{phone}/messages",
    response_model=List[MessageResponse],
    summary="[User] Сообщения диалога с номером",
)
async def get_dialog_messages(
    phone: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    sim_ids = await _get_user_sim_ids(db, current_user.id)
    if not sim_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Нет назначенных SIM-карт",
        )

    r = await db.execute(
        select(Message)
        .where(
            and_(
                Message.sim_card_id.in_(sim_ids),
                Message.external_phone == phone,
            )
        )
        .order_by(desc(Message.created_at))
        .limit(limit)
        .offset(offset)
    )
    messages = r.scalars().all()
    return [
        MessageResponse(
            id=m.id,
            sim_card_id=m.sim_card_id,
            external_phone=m.external_phone,
            direction=m.direction.value,
            text=m.text,
            status=m.status.value,
            created_at=m.created_at,
        )
        for m in messages
    ]


@router.get(
    "/user/messages/recent",
    response_model=RecentMessagesResponse,
    summary="[User] Новые сообщения с момента (polling)",
)
async def get_recent_messages(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    since: Optional[datetime] = Query(None, description="ISO datetime или оставить пустым для последнего часа"),
):
    sim_ids = await _get_user_sim_ids(db, current_user.id)
    if not sim_ids:
        return RecentMessagesResponse(messages=[])

    if since is None:
        since = datetime.now(timezone.utc) - timedelta(hours=1)

    r = await db.execute(
        select(Message)
        .where(
            and_(
                Message.sim_card_id.in_(sim_ids),
                Message.created_at >= since,
            )
        )
        .order_by(Message.created_at)
        .limit(200)
    )
    messages = r.scalars().all()
    return RecentMessagesResponse(
        messages=[
            MessageResponse(
                id=m.id,
                sim_card_id=m.sim_card_id,
                external_phone=m.external_phone,
                direction=m.direction.value,
                text=m.text,
                status=m.status.value,
                created_at=m.created_at,
            )
            for m in messages
        ]
    )


@router.post(
    "/user/messages/send",
    response_model=SendMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="[User] Отправить SMS (постановка в очередь)",
)
async def send_message(
    body: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    r = await db.execute(
        select(SimCard)
        .where(SimCard.assigned_user_id == current_user.id)
        .order_by(SimCard.id)
    )
    sims = r.scalars().all()
    if not sims:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Нет назначенных SIM-карт для отправки",
        )

    if body.sim_card_id is not None:
        sim = next((s for s in sims if s.id == body.sim_card_id), None)
        if not sim:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Указанная SIM-карта не назначена вам",
            )
    else:
        import random
        sim = random.choice(sims)

    from sms_queue import enqueue_sms
    job_id = await enqueue_sms(
        gateway_id=sim.gateway_id,
        port_num=sim.port_number,
        phone=body.phone,
        text=body.text,
        sim_card_id=sim.id,
    )
    return SendMessageResponse(job_id=job_id, status="queued")


@router.post(
    "/user/messages/send_group",
    response_model=SendGroupResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="[User] Массовая отправка по группе контактов",
)
async def send_group_message(
    body: SendGroupRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    r = await db.execute(
        select(SimCard)
        .where(SimCard.assigned_user_id == current_user.id)
        .order_by(SimCard.id)
    )
    sims = r.scalars().all()
    if not sims:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Нет назначенных SIM-карт для отправки",
        )

    if body.sim_card_id is not None:
        sim = next((s for s in sims if s.id == body.sim_card_id), None)
        if not sim:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Указанная SIM-карта не назначена вам",
            )
    else:
        import random
        sim = random.choice(sims)

    group = await db.get(
        ContactGroup,
        body.group_id,
        options=[selectinload(ContactGroup.contacts)],
    )
    if not group or group.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Группа не найдена")

    phones = []
    for c in group.contacts or []:
        if c.phone_number:
            phones.append(c.phone_number)

    if not phones:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="В группе нет контактов для рассылки")

    from sms_queue import enqueue_sms

    job_ids: List[str] = []
    for phone in phones:
        job_id = await enqueue_sms(
            gateway_id=sim.gateway_id,
            port_num=sim.port_number,
            phone=phone,
            text=body.text,
            sim_card_id=sim.id,
        )
        job_ids.append(job_id)

    return SendGroupResponse(total=len(job_ids), job_ids=job_ids)


@router.get(
    "/admin/messages",
    response_model=List[AdminMessageResponse],
    summary="[Admin] Список сообщений с фильтрами",
)
async def admin_list_messages(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    direction: Optional[str] = Query(None),
    external_phone: Optional[str] = Query(None),
):
    q = (
        select(Message)
        .options(selectinload(Message.sim_card).selectinload(SimCard.assigned_user))
        .order_by(desc(Message.created_at))
        .limit(limit)
        .offset(offset)
    )
    if direction is not None:
        q = q.where(Message.direction == direction)
    if external_phone is not None:
        q = q.where(Message.external_phone.ilike(f"%{external_phone}%"))
    r = await db.execute(q)
    messages = r.scalars().all()
    
    result = []
    for m in messages:
        username = None
        sim_label = None
        if m.sim_card:
            sim_label = m.sim_card.label or m.sim_card.phone_number or f"Порт {m.sim_card.port_number}"
            if m.sim_card.assigned_user:
                username = m.sim_card.assigned_user.username
                
        result.append(
            AdminMessageResponse(
                id=m.id,
                sim_card_id=m.sim_card_id,
                sim_card_label=sim_label,
                username=username,
                external_phone=m.external_phone,
                direction=m.direction.value,
                text=m.text,
                status=m.status.value,
                created_at=m.created_at,
            )
        )
    return result


@router.get(
    "/admin/stats/export",
    summary="[Admin] Экспорт статистики SMS (CSV)",
)
async def admin_export_stats_csv(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
    since: Optional[datetime] = Query(None, description="ISO datetime, по умолчанию 7 дней назад"),
    until: Optional[datetime] = Query(None, description="ISO datetime, по умолчанию сейчас"),
):
    if until is None:
        until = datetime.now(timezone.utc)
    if since is None:
        since = until - timedelta(days=7)

    r = await db.execute(
        select(Message)
        .where(and_(Message.created_at >= since, Message.created_at <= until))
        .order_by(Message.created_at)
        .limit(200000)
    )
    messages = r.scalars().all()

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["created_at", "sim_card_id", "external_phone", "direction", "status", "text_len", "error_text"])
    for m in messages:
        w.writerow(
            [
                (m.created_at.isoformat() if m.created_at else ""),
                m.sim_card_id or "",
                m.external_phone,
                (m.direction.value if m.direction else ""),
                (m.status.value if m.status else ""),
                len(m.text or ""),
                (m.error_text or ""),
            ]
        )

    data = out.getvalue().encode("utf-8-sig")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=sms_stats.csv"},
    )


@router.get(
    "/admin/stats/export/excel",
    summary="[Admin] Экспорт статистики SMS (Excel XLSX)",
)
async def admin_export_stats_excel(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
    since: Optional[datetime] = Query(None, description="ISO datetime, по умолчанию 7 дней назад"),
    until: Optional[datetime] = Query(None, description="ISO datetime, по умолчанию сейчас"),
):
    import openpyxl
    import io
    if until is None:
        until = datetime.now(timezone.utc)
    if since is None:
        since = until - timedelta(days=7)

    r = await db.execute(
        select(Message)
        .where(and_(Message.created_at >= since, Message.created_at <= until))
        .order_by(Message.created_at)
        .limit(200000)
    )
    messages = r.scalars().all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "SMS Stats"
    ws.append(["created_at", "sim_card_id", "external_phone", "direction", "status", "text_len", "error_text"])
    
    for m in messages:
        ws.append([
            (m.created_at.isoformat() if m.created_at else ""),
            m.sim_card_id or "",
            m.external_phone,
            (m.direction.value if m.direction else ""),
            (m.status.value if m.status else ""),
            len(m.text or ""),
            (m.error_text or ""),
        ])

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=stats_{since.date()}_{until.date()}.xlsx"},
    )
