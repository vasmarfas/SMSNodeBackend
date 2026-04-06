"""
Режимы регистрации и заявки (semi_open).

- open: кто угодно может зарегистрироваться (бот /start или API /auth/register)
- closed: только создание учётки админом или по приглашению
- semi_open: можно подать заявку; учётку создаёт админ после одобрения
Режим задаётся в .env (REGISTRATION_MODE) или переопределяется в админке (system_settings).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config_reader import config
from core.db.models import SystemSetting

KEY_REGISTRATION_MODE = "registration_mode"
VALID_MODES = ("open", "closed", "semi_open")


async def get_registration_mode(session: AsyncSession) -> str:
    """
    Текущий режим регистрации: из БД (system_settings), иначе из конфига/env.
    """
    r = await session.execute(
        select(SystemSetting.value).where(SystemSetting.key == KEY_REGISTRATION_MODE).limit(1)
    )
    row = r.scalars().first()
    if row and row.strip().lower() in VALID_MODES:
        return row.strip().lower()
    return config.get_registration_mode()


async def set_registration_mode(session: AsyncSession, mode: str) -> None:
    """Установить режим регистрации из админки (open/closed/semi_open)."""
    mode = (mode or "").strip().lower()
    if mode not in VALID_MODES:
        mode = "open"
    stmt = select(SystemSetting).where(SystemSetting.key == KEY_REGISTRATION_MODE)
    r = await session.execute(stmt)
    row = r.scalars().first()
    if row:
        row.value = mode
    else:
        session.add(SystemSetting(key=KEY_REGISTRATION_MODE, value=mode))
    await session.commit()
