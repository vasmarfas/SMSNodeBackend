"""
JWT-авторизация для REST API.

Логика:
  - Пароли хранятся в виде bcrypt-хешей (библиотека bcrypt напрямую;
    passlib не используется из-за несовместимости с bcrypt 4.1+).
  - Токен: JWT HS256, payload = {"sub": username, "role": "admin|user"}.
  - Срок действия: ACCESS_TOKEN_EXPIRE_MINUTES из settings.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from jose import JWTError, jwt
from pydantic import BaseModel

from core.db.database import settings

_BCRYPT_MAX_PASSWORD_BYTES = 72


class TokenData(BaseModel):
    username: str
    role: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


def get_password_hash(plain_password: str) -> str:
    """Хешировать пароль для хранения в БД (bcrypt). Пароль > 72 байт обрезается."""
    raw = plain_password.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_PASSWORD_BYTES:
        raw = raw[:_BCRYPT_MAX_PASSWORD_BYTES]
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Проверить пароль при логине."""
    raw = plain_password.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_PASSWORD_BYTES:
        raw = raw[:_BCRYPT_MAX_PASSWORD_BYTES]
    try:
        return bcrypt.checkpw(raw, hashed_password.encode("utf-8"))
    except Exception:
        return False


def create_access_token(username: str, role: str) -> str:
    """
    Создать JWT-токен.

    Payload:
      - sub: username (стандартное поле JWT)
      - role: "admin" или "user"
      - exp: timestamp истечения
    """
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {
        "sub": username,
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, settings.API_SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> Optional[TokenData]:
    """
    Декодировать и проверить JWT-токен.
    Возвращает TokenData или None при невалидном токене.
    """
    try:
        payload = jwt.decode(token, settings.API_SECRET_KEY, algorithms=["HS256"])
        username: str = payload.get("sub")
        role: str = payload.get("role", "user")
        if not username:
            return None
        return TokenData(username=username, role=role)
    except JWTError:
        return None
