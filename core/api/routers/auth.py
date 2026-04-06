"""
Роутер авторизации: /auth/token, /auth/register, /auth/me, /auth/telegram
Режим регистрации: open / closed / semi_open (конфиг или админка).
"""

import hashlib
import hmac
import json
from urllib.parse import parse_qs, unquote

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.api.auth import (
    create_access_token, get_password_hash, verify_password, TokenResponse
)
from core.api.dependencies import get_current_user
from core.db.database import get_db
from core.db.models import User, RoleEnum, PendingRegistration, PendingRegistrationSource
from core.registration import get_registration_mode

router = APIRouter(prefix="/auth", tags=["Auth"])


def _validate_telegram_init_data(init_data: str, bot_token: str) -> dict | None:
    """
    Валидация init_data от Telegram WebApp (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
    Возвращает распарсенный dict параметров при успехе, иначе None.
    """
    try:
        parsed = parse_qs(init_data, keep_blank_values=True)
        vals = {k: (v[0] if v else "") for k, v in parsed.items()}
        if "hash" not in vals:
            return None
        received_hash = vals.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(vals.items()))
        secret_key = hmac.new(
            b"WebAppData",
            bot_token.encode(),
            hashlib.sha256,
        ).digest()
        computed_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(computed_hash, received_hash):
            return None
        return vals
    except Exception:
        return None


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6)


class TelegramLoginRequest(BaseModel):
    init_data: str = Field(..., min_length=1, description="Строка initData из Telegram.WebApp.initData")


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    telegram_id: int | None
    is_active: bool

    class Config:
        from_attributes = True


@router.post(
    "/telegram",
    response_model=TokenResponse,
    summary="Вход по Telegram Mini App (initData). При неудаче использовать /token с логином/паролем.",
)
async def login_telegram(
    body: TelegramLoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Принимает init_data из window.Telegram.WebApp.initData.
    Валидирует подпись через BOT_TOKEN, извлекает telegram_id из user, ищет пользователя в БД.
    Если пользователь найден и активен — возвращает JWT. Иначе 401 (фолбэк на логин/пароль).
    """
    try:
        from config_reader import config
        bot_token = config.bot_token.get_secret_value()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Вход через Telegram недоступен (бот не настроен)",
        )
    validated = _validate_telegram_init_data(body.init_data.strip(), bot_token)
    if not validated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверные данные Telegram. Войдите по логину и паролю.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_json_str = validated.get("user")
    if not user_json_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="В данных Telegram нет пользователя.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        user_data = json.loads(unquote(user_json_str))
        telegram_id = int(user_data.get("id"))
    except (json.JSONDecodeError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный формат данных пользователя Telegram.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Пользователь Telegram не привязан к учётной записи. Войдите по логину и паролю или зарегистрируйтесь в боте.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Аккаунт заблокирован",
        )
    token = create_access_token(username=user.username, role=user.role.value)
    return TokenResponse(access_token=token)


@router.post("/token", response_model=TokenResponse, summary="Получить JWT-токен")
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """
    Стандартный OAuth2 Password Flow.
    Принимает form-data: username + password.
    Возвращает access_token.
    """
    result = await db.execute(select(User).where(User.username == form.username))
    user = result.scalar_one_or_none()

    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Аккаунт заблокирован",
        )

    token = create_access_token(username=user.username, role=user.role.value)
    return TokenResponse(access_token=token)


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Регистрация (по режиму: open — сразу, closed — 403, semi_open — заявка)",
)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Регистрация нового пользователя.
    Режим задаётся в .env (REGISTRATION_MODE) или в админке.
    - open: создаётся пользователь (первый — admin).
    - closed: 403, учётку создаёт только админ.
    - semi_open: создаётся заявка (202), после одобрения админом — учётка.
    """
    mode = await get_registration_mode(db)

    if mode == "closed":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Регистрация закрыта. Учётная запись создаётся только администратором.",
        )

    existing_user = await db.execute(select(User).where(User.username == body.username))
    if existing_user.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Пользователь с таким именем уже существует",
        )

    if mode == "semi_open":
        existing_pending = await db.execute(
            select(PendingRegistration).where(PendingRegistration.username == body.username)
        )
        if existing_pending.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Заявка с таким именем уже подана. Ожидайте одобрения.",
            )
        pending = PendingRegistration(
            username=body.username,
            hashed_password=get_password_hash(body.password),
            source=PendingRegistrationSource.API,
        )
        db.add(pending)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_202_ACCEPTED,
            detail="Заявка на регистрацию подана. Ожидайте одобрения администратора.",
        )

    count_result = await db.execute(select(User))
    is_first = len(count_result.scalars().all()) == 0
    role = RoleEnum.ADMIN if is_first else RoleEnum.USER

    user = User(
        username=body.username,
        hashed_password=get_password_hash(body.password),
        role=role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.get("/me", response_model=UserResponse, summary="Данные текущего пользователя")
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user
