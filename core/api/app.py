"""
HTTP API на FastAPI. Документация: /docs, /redoc.

Запуск (пример): uvicorn core.api.app:app --host 0.0.0.0 --port 8000
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from core.api.routers import auth, users, gateways
from core.api.routers import messages, contacts, templates, rules
from core.db.database import create_tables, settings, AsyncSessionLocal
from core.gateways.manager import gateway_manager
from config_reader import config

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Жизненный цикл FastAPI:
      - startup: создать таблицы, загрузить шлюзы из БД
      - shutdown: (в будущем) закрыть соединения
    """
    logger.info("FastAPI startup: создание таблиц...")
    await create_tables()
    logger.info("Таблицы созданы.")

    logger.info("Загрузка шлюзов из БД...")
    async with AsyncSessionLocal() as session:
        count = await gateway_manager.load_from_db(session)
    logger.info(f"Загружено шлюзов: {count}")

    yield

    logger.info("FastAPI shutdown.")


app = FastAPI(
    title="SMS Node Backend API",
    description=(
        "REST API для кросс-платформенной системы управления SMS-коммуникациями.\n\n"
        "**Авторизация:** OAuth2 Password Flow (JWT Bearer Token)\n\n"
        "Для получения токена: `POST /auth/token` с form-data `username` + `password`.\n"
        "Первый зарегистрированный пользователь автоматически получает роль **admin**."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # На продакшне заменить конкретными доменами
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(gateways.router)
app.include_router(messages.router)
app.include_router(contacts.router)
app.include_router(templates.router)
app.include_router(rules.router)


@app.get("/", tags=["Health"])
async def health_check():
    """Проверка работоспособности API."""
    return {
        "status": "ok",
        "version": "1.0.0",
        "gateways_loaded": gateway_manager.count(),
    }


@app.get("/health", tags=["Health"])
async def health_check_alias():
    """
    Совместимость с Docker healthcheck и внешними мониторингами.
    Возвращает тот же ответ, что и корневой эндпоинт.
    """
    return await health_check()
