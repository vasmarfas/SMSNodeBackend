from enum import Enum
from pydantic import SecretStr
from pydantic_settings import BaseSettings


class RegistrationMode(str, Enum):
    """Режим регистрации: открытая, закрытая (только админ/приглашение), по заявке (админ одобряет)."""
    OPEN = "open"
    CLOSED = "closed"
    SEMI_OPEN = "semi_open"


class Settings(BaseSettings):
    bot_token: SecretStr
    ADMIN_ID: int

    # Режим регистрации (можно переопределить в админке)
    # open — кто угодно может зарегистрироваться (бот /start или API /auth/register)
    # closed — только создание учётки админом или по приглашению
    # semi_open — можно подать заявку, учётку создаёт админ после одобрения
    REGISTRATION_MODE: str = "open"

    # Необязательные поля — теперь шлюзы управляются через /gateways в БД,
    # GOIP_IP/LOGIN/PASSWORD оставлены для обратной совместимости, но НЕ обязательны.
    PC_IP: str = "0.0.0.0"
    GOIP_IP: str = ""
    GOIP_LOGIN: str = ""
    GOIP_PASSWORD: str = ""
    RSCHS_CHAT_ID: int = 0
    IS_NEED_TO_SEND_RSCHS_MESSAGES_TO_USER: bool = False
    MAX_CHANNELS: int = 8

    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8007
    API_SECRET_KEY: str = "your-secret-key-change-this-in-production"

    # SMTP-сервер для приёма входящих SMS от GoIP (не 25 — требует прав админа на Windows)
    SMTP_HOST: str = "0.0.0.0"
    SMTP_PORT: int = 2525

    # UDP-порт для приёма входящих SMS по GoIP UDP-протоколу (основной канал)
    GOIP_LISTEN_HOST: str = "0.0.0.0"
    GOIP_LISTEN_PORT: int = 9999

    MINI_APP_URL: str = "https://your-mini-app-url.com"

    # Глобальный интервал между SMS для одного канала (секунды),
    # чтобы избежать блокировки SIM. Можно переопределить для конкретного канала.
    MIN_INTERVAL_PER_CHANNEL_SEC: float = 5.0

    # Минимальная пауза между обработкой задач воркером очереди (секунды).
    # По умолчанию 0.5с как дополнительная "страховка" от перегрузки шлюза/SIM.
    # Для стресс-тестов можно выставлять 0.
    SMS_WORKER_MIN_SLEEP_SEC: float = 0.5

    # Демо-режим: запрет на изменения (read-only), автоматическое создание демо-данных
    IS_DEMO: bool = False

    # HTTP/SOCKS прокси для Telegram
    # Например: http://user:pass@12.34.56.78:8000
    TG_PROXY: str = ""


    def get_registration_mode(self) -> str:
        """Текущий режим (open/closed/semi_open). По умолчанию из env."""
        return (self.REGISTRATION_MODE or "open").strip().lower() or "open"

    class Config:
        env_file = '.env'
        env_file_encoding = 'utf-8'
        extra = 'ignore'  # Игнорируем POSTGRES_*, ACCESS_TOKEN_EXPIRE_MINUTES и другие новые переменные


config = Settings()


