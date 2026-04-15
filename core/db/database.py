"""
Конфигурация подключения к PostgreSQL через SQLAlchemy (async).

Настройки читаются из .env файла. При запуске на новом стенде достаточно
задать переменные POSTGRES_* в .env — всё остальное подтянется автоматически.

При старте приложения вызывается ensure_database_exists() — если базы с именем
POSTGRES_DB ещё нет, она создаётся (подключение к служебной БД postgres).
Затем create_tables() создаёт таблицы внутри этой базы.
"""

import logging
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "sms_gateway"

    # JWT
    API_SECRET_KEY: str = "change-me-in-production-use-long-random-string"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # API server
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"  # Игнорируем лишние переменные (BOT_TOKEN и т.д.)

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


settings = Settings()

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=10,
    max_overflow=20,
    # Гарантируем, что сессия БД всегда работает в UTC,
    # чтобы все now()/CURRENT_TIMESTAMP были в UTC, а не в локальном времени сервера.
    connect_args={"server_settings": {"timezone": "UTC"}},
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def ensure_database_exists() -> None:
    """
    Создать базу данных POSTGRES_DB, если её ещё нет.
    Подключаемся к служебной БД 'postgres', проверяем pg_database, при отсутствии — CREATE DATABASE.
    Требует прав у пользователя POSTGRES_USER на создание БД (CREATEDB или суперпользователь).
    """
    try:
        import asyncpg
    except ImportError:
        logger.warning("asyncpg не установлен — пропуск автосоздания базы (убедитесь, что БД %s существует)", settings.POSTGRES_DB)
        return

    conn = None
    try:
        conn = await asyncpg.connect(
            host=settings.POSTGRES_HOST,
            port=settings.POSTGRES_PORT,
            user=settings.POSTGRES_USER,
            password=settings.POSTGRES_PASSWORD,
            database="postgres",
            timeout=5
        )
        row = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1",
            settings.POSTGRES_DB,
        )
        if row is None:
            await conn.execute(f'CREATE DATABASE "{settings.POSTGRES_DB}"')
            logger.info("База данных %s создана.", settings.POSTGRES_DB)
        else:
            logger.debug("База данных %s уже существует.", settings.POSTGRES_DB)
    except Exception as e:
        logger.warning(
            "Не удалось подключиться к базе 'postgres' для проверки/создания %s. "
            "Возможно, база уже существует (ошибка: %s)", settings.POSTGRES_DB, e
        )
    finally:
        if conn:
            await conn.close()


from config_reader import config

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: выдаёт сессию на время запроса."""
    async with AsyncSessionLocal() as session:
        if config.IS_DEMO:
            # Подменяем commit на flush, чтобы генерировать ID, но не сохранять в БД
            async def mocked_commit():
                await session.flush()
            session.commit = mocked_commit
            
        try:
            yield session
        finally:
            if config.IS_DEMO:
                # Откатываем транзакцию в конце запроса (ничего не попадёт в базу)
                await session.rollback()
            await session.close()


async def create_tables():
    """
    Создать все таблицы в базе POSTGRES_DB (альтернатива миграциям для быстрого старта).
    Перед первым запуском вызывается ensure_database_exists(), чтобы создать базу, если её нет.
    """
    from core.db.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
