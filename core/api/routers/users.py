"""
Роутер управления пользователями — /api/v1/admin/users
Доступно только администраторам.
"""

from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.api.auth import get_password_hash
from core.api.dependencies import get_current_user, require_admin
from core.db.database import get_db
from core.db.models import User, RoleEnum, SimCard, PendingRegistration, PendingRegistrationSource
from core.registration import get_registration_mode, set_registration_mode, VALID_MODES

router = APIRouter(prefix="/api/v1", tags=["Users"])


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    telegram_id: Optional[int]
    is_active: bool

    class Config:
        from_attributes = True


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6)
    role: RoleEnum = RoleEnum.USER
    telegram_id: Optional[int] = None


class UserUpdateRequest(BaseModel):
    password: Optional[str] = Field(None, min_length=6)
    role: Optional[RoleEnum] = None
    telegram_id: Optional[int] = None
    is_active: Optional[bool] = None


class SimCardResponse(BaseModel):
    id: int
    port_number: int
    phone_number: Optional[str]
    label: Optional[str]
    status: str
    gateway_id: int

    class Config:
        from_attributes = True


class PendingRegistrationResponse(BaseModel):
    id: int
    telegram_id: Optional[int]
    username: str
    source: str
    created_at: datetime

    class Config:
        from_attributes = True


class RegistrationModeResponse(BaseModel):
    mode: str  # open | closed | semi_open


class RegistrationModeSetRequest(BaseModel):
    mode: str  # open | closed | semi_open


class SimCardUpdateRequest(BaseModel):
    label: Optional[str] = None


@router.get(
    "/admin/settings/registration-mode",
    response_model=RegistrationModeResponse,
    summary="[Admin] Текущий режим регистрации",
)
async def get_registration_mode_setting(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    mode = await get_registration_mode(db)
    return RegistrationModeResponse(mode=mode)


@router.patch(
    "/admin/settings/registration-mode",
    response_model=RegistrationModeResponse,
    summary="[Admin] Установить режим регистрации",
)
async def patch_registration_mode_setting(
    body: RegistrationModeSetRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    mode = (body.mode or "").strip().lower()
    if mode not in VALID_MODES:
        mode = "open"
    await set_registration_mode(db, mode)
    return RegistrationModeResponse(mode=mode)


@router.get(
    "/admin/pending-registrations",
    response_model=List[PendingRegistrationResponse],
    summary="[Admin] Список заявок на регистрацию",
)
async def list_pending_registrations(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(
        select(PendingRegistration).order_by(PendingRegistration.created_at.desc())
    )
    return result.scalars().all()


@router.post(
    "/admin/pending-registrations/{pending_id}/approve",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="[Admin] Одобрить заявку — создать пользователя",
)
async def approve_pending_registration(
    pending_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    pending = await db.get(PendingRegistration, pending_id)
    if not pending:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Заявка не найдена")

    existing = await db.execute(select(User).where(User.username == pending.username))
    if existing.scalar_one_or_none():
        await db.delete(pending)
        await db.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Пользователь с таким именем уже существует (заявка удалена)",
        )

    user = User(
        username=pending.username,
        hashed_password=pending.hashed_password,
        role=RoleEnum.USER,
        telegram_id=pending.telegram_id,
    )
    db.add(user)
    await db.delete(pending)
    await db.commit()
    await db.refresh(user)
    return user


@router.delete(
    "/admin/pending-registrations/{pending_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="[Admin] Отклонить заявку",
)
async def reject_pending_registration(
    pending_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    pending = await db.get(PendingRegistration, pending_id)
    if not pending:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Заявка не найдена")
    await db.delete(pending)
    await db.commit()


@router.get(
    "/admin/users",
    response_model=List[UserResponse],
    summary="[Admin] Список всех пользователей",
)
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(select(User).order_by(User.id))
    return result.scalars().all()


@router.post(
    "/admin/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="[Admin] Создать пользователя",
)
async def create_user(
    body: UserCreateRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    existing = await db.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "Пользователь уже существует")

    user = User(
        username=body.username,
        hashed_password=get_password_hash(body.password),
        role=body.role,
        telegram_id=body.telegram_id,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.get(
    "/admin/users/{user_id}",
    response_model=UserResponse,
    summary="[Admin] Получить пользователя по ID",
)
async def get_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    return user


@router.patch(
    "/admin/users/{user_id}",
    response_model=UserResponse,
    summary="[Admin] Обновить пользователя",
)
async def update_user(
    user_id: int,
    body: UserUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")

    if body.password is not None:
        user.hashed_password = get_password_hash(body.password)
    if body.role is not None:
        user.role = body.role
    if body.telegram_id is not None:
        if body.telegram_id == 0:
            user.telegram_id = None
        else:
            existing = await db.execute(select(User).where(User.telegram_id == body.telegram_id, User.id != user_id))
            if existing.scalar_one_or_none():
                raise HTTPException(status.HTTP_409_CONFLICT, "Этот Telegram ID уже привязан к другому пользователю")
            user.telegram_id = body.telegram_id
    if body.is_active is not None:
        user.is_active = body.is_active

    await db.commit()
    await db.refresh(user)
    return user


@router.delete(
    "/admin/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="[Admin] Деактивировать пользователя",
)
async def deactivate_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    if user_id == current_admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нельзя деактивировать себя")
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    user.is_active = False
    await db.commit()


@router.get(
    "/admin/users/{user_id}/sims",
    response_model=List[SimCardResponse],
    summary="[Admin] SIM-карты пользователя",
)
async def get_user_sims(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(
        select(SimCard).where(SimCard.assigned_user_id == user_id)
    )
    return result.scalars().all()


@router.post(
    "/admin/users/{user_id}/sims/{sim_id}",
    summary="[Admin] Назначить SIM-карту пользователю",
)
async def assign_sim(
    user_id: int,
    sim_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    sim = await db.get(SimCard, sim_id)
    if not sim:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SIM-карта не найдена")

    sim.assigned_user_id = user_id
    await db.commit()
    return {"detail": f"SIM {sim_id} назначена пользователю {user.username}"}


@router.delete(
    "/admin/users/{user_id}/sims/{sim_id}",
    summary="[Admin] Отозвать SIM-карту у пользователя",
)
async def revoke_sim(
    user_id: int,
    sim_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    sim = await db.get(SimCard, sim_id)
    if not sim or sim.assigned_user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SIM не найдена или не принадлежит этому юзеру")
    sim.assigned_user_id = None
    await db.commit()
    return {"detail": f"SIM {sim_id} отозвана"}


@router.get(
    "/user/me/sims",
    response_model=List[SimCardResponse],
    summary="[User] Мои SIM-карты",
)
async def get_my_sims(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(SimCard).where(SimCard.assigned_user_id == current_user.id)
    )
    return result.scalars().all()


@router.patch(
    "/user/me/sims/{sim_id}",
    response_model=SimCardResponse,
    summary="[User] Обновить подпись (label) для SIM-карты",
)
async def update_my_sim(
    sim_id: int,
    body: SimCardUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sim = await db.get(SimCard, sim_id)
    if not sim or sim.assigned_user_id != current_user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SIM-карта не найдена или нет доступа")
    
    sim.label = body.label
    await db.commit()
    await db.refresh(sim)
    return sim
