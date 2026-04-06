"""
Совместимый слой для бота: инициализация БД, middleware сессий SQLAlchemy,
логирование SMS и статистика. Модели — в core/db/models.py.
"""

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Callable, Awaitable, Dict, Any, Optional
from datetime import date, datetime, timedelta, timezone

from core.db.database import AsyncSessionLocal, create_tables, ensure_database_exists
from core.db.models import (
    User,
    Gateway,
    SimCard,
    Contact,
    SMSTemplate,
    Message,
    GoIPEvent,
    RoleEnum,
    GatewayTypeEnum,
    MessageDirectionEnum,
    MessageStatusEnum,
    GoIPEventTypeEnum,
)

# Алиасы для совместимости со старыми хендлерами
# PhoneNumber → SimCard (поля: .number→.phone_number, .channel→.port_number)
# SMSLog → Message
# SMSType → MessageDirectionEnum
PhoneNumber = SimCard
SMSLog = Message
SMSType = MessageDirectionEnum


# Middleware: инжектирует PostgreSQL сессию в data["session"] каждого хендлера

from config_reader import config

class DbSessionMiddleware(BaseMiddleware):
    """Aiogram middleware для PostgreSQL сессий."""

    def __init__(self, session_pool=None):
        super().__init__()
        # session_pool принимается для обратной совместимости, но не используется

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        async with AsyncSessionLocal() as session:
            if config.IS_DEMO:
                # Подменяем commit на flush в демо-режиме, чтобы имитировать успешные
                # операции без реального сохранения в БД.
                async def mocked_commit():
                    await session.flush()
                session.commit = mocked_commit

            data["session"] = session
            try:
                return await handler(event, data)
            finally:
                if config.IS_DEMO:
                    # Принудительно откатываем после завершения хендлера
                    await session.rollback()


# Функции инициализации (совместимый интерфейс)

async def init_db(engine=None) -> None:
    """Создать базу данных (если нет) и все таблицы PostgreSQL. Аргумент engine игнорируется."""
    await ensure_database_exists()
    await create_tables()


async def init_db_functions(engine=None) -> None:
    """No-op: оставлен для обратной совместимости."""
    pass


async def create_db_session_pool():
    """
    Compat shim для legacy-вызовов вида:
        engine, db_session = await create_db_session_pool()
    Возвращает (None, AsyncSessionLocal).
    """
    return None, AsyncSessionLocal


async def log_sms(
    session: AsyncSession,
    sms_type,
    message_text: str,
    channel: Optional[int] = None,
    sender: Optional[str] = None,
    recipient: Optional[str] = None,
    user_id: Optional[int] = None,
    status: str = "sent",
    sim_card_id: Optional[int] = None,
) -> Message:
    """
    Записать SMS-событие в таблицу messages.

    Args:
        sms_type: MessageDirectionEnum.INCOMING / OUTGOING
        message_text: текст SMS
        channel: номер порта шлюза (port_number в SimCard)
        sender: номер отправителя (для входящих)
        recipient: номер получателя (для исходящих)
        user_id: ID пользователя в системе (для исходящих)
        status: "received", "sent", "sending", "failed"
        sim_card_id: ID SimCard (если известен)
    """
    # Находим SimCard по port_number, если не передан явно
    if sim_card_id is None and channel:
        r = await session.execute(
            select(SimCard).where(SimCard.port_number == channel)
        )
        sim = r.scalar_one_or_none()
        if sim:
            sim_card_id = sim.id

    status_map = {
        "received": MessageStatusEnum.RECEIVED,
        "sent": MessageStatusEnum.SENT_OK,
        "sending": MessageStatusEnum.SENDING,
        "failed": MessageStatusEnum.FAILED,
    }
    msg_status = status_map.get(status, MessageStatusEnum.PENDING)

    direction = (
        MessageDirectionEnum.INCOMING
        if sms_type in (MessageDirectionEnum.INCOMING, "incoming", "in")
        else MessageDirectionEnum.OUTGOING
    )

    external_phone = (sender if direction == MessageDirectionEnum.INCOMING else recipient) or "unknown"

    msg = Message(
        sim_card_id=sim_card_id,
        external_phone=external_phone,
        direction=direction,
        text=message_text,
        status=msg_status,
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)
    return msg


# Статистика SMS для панели администратора

async def get_sms_stats(session: AsyncSession) -> dict:
    """Сводная статистика SMS для /admin."""
    total = await session.scalar(select(func.count(Message.id))) or 0

    incoming = await session.scalar(
        select(func.count(Message.id)).where(
            Message.direction == MessageDirectionEnum.INCOMING
        )
    ) or 0

    outgoing = await session.scalar(
        select(func.count(Message.id)).where(
            Message.direction == MessageDirectionEnum.OUTGOING
        )
    ) or 0

    today_utc = datetime.now(timezone.utc).date()
    today_count = await session.scalar(
        select(func.count(Message.id)).where(
            func.date(Message.created_at) == today_utc
        )
    ) or 0

    # Статистика по портам шлюза (аналог каналов) — за всё время
    port_stats = {}
    # За последний час (UTC) — для проверки порогов нагрузки
    hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    port_stats_last_hour = {}
    for port in range(1, 9):
        r = await session.execute(
            select(SimCard.id).where(SimCard.port_number == port)
        )
        sim_ids = [row[0] for row in r.fetchall()]
        if sim_ids:
            count = await session.scalar(
                select(func.count(Message.id)).where(Message.sim_card_id.in_(sim_ids))
            ) or 0
            count_hour = await session.scalar(
                select(func.count(Message.id)).where(
                    Message.sim_card_id.in_(sim_ids),
                    Message.created_at >= hour_ago,
                )
            ) or 0
        else:
            count = 0
            count_hour = 0
        port_stats[port] = count
        port_stats_last_hour[port] = count_hour

    return {
        "total": total,
        "incoming": incoming,
        "outgoing": outgoing,
        "today": today_count,
        "channels": port_stats,
        "channels_last_hour": port_stats_last_hour,
    }
