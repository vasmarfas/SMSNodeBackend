"""
FastAPI dependencies — переиспользуемые зависимости для роутеров.

Использование:
    @router.get("/me")
    async def get_me(current_user: User = Depends(get_current_user)):
        ...

    @router.post("/admin/users")
    async def create_user(admin: User = Depends(require_admin)):
        ...
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.api.auth import decode_token
from core.db.database import get_db
from core.db.models import User, RoleEnum
from core.gateways.manager import gateway_manager, GatewayManager

# Схема: токен передаётся в заголовке Authorization: Bearer <token>
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Зависимость: достать текущего пользователя из JWT.
    Выбрасывает 401 если токен невалиден или пользователь не найден / неактивен.
    """
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Неверный или просроченный токен",
        headers={"WWW-Authenticate": "Bearer"},
    )

    token_data = decode_token(token)
    if not token_data:
        raise credentials_error

    result = await db.execute(
        select(User).where(User.username == token_data.username)
    )
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise credentials_error

    return user


async def require_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Зависимость: проверить что пользователь — администратор.
    Выбрасывает 403 для обычных пользователей.
    """
    if current_user.role != RoleEnum.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступ только для администраторов",
        )
    return current_user


def get_gateway_manager() -> GatewayManager:
    """Зависимость: получить синглтон GatewayManager."""
    return gateway_manager
