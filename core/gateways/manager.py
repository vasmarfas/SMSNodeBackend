"""
GatewayManager — фабрика и диспетчер GSM-шлюзов.

Загружает шлюзы из PostgreSQL (core.db.models.Gateway) при старте FastAPI-приложения.
Используется как singleton через get_gateway_manager() зависимость FastAPI.
"""

import asyncio
import logging
from typing import Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.gateways.base import BaseGateway, GatewayResponse
from core.gateways.goip import GoIPGateway
from core.gateways.goip_http import GoIPHTTPGateway
from core.gateways.skyline import SkylineGateway

logger = logging.getLogger(__name__)


class GatewayManager:
    """Держит все активные шлюзы в памяти и управляет их жизненным циклом."""

    def __init__(self):
        self._gateways: Dict[int, BaseGateway] = {}
        self._lock = asyncio.Lock()

    async def load_from_db(self, session: AsyncSession) -> int:
        """
        Загрузить все активные шлюзы из БД.
        Вызывается один раз при старте FastAPI (lifespan).
        """
        from core.db.models import Gateway as GatewayModel

        result = await session.execute(
            select(GatewayModel).where(GatewayModel.is_active == True)
        )
        gateways = result.scalars().all()

        async with self._lock:
            self._gateways.clear()
            for gw in gateways:
                instance = self._create_instance(gw)
                if instance:
                    self._gateways[gw.id] = instance

        logger.info(f"GatewayManager: loaded {len(self._gateways)} gateways from DB")
        return len(self._gateways)

    def _create_instance(self, gw) -> Optional[BaseGateway]:
        """Создать экземпляр шлюза нужного типа по записи из БД."""
        kwargs = dict(
            gateway_id=gw.id, name=gw.name,
            host=gw.host, port=gw.port,
            username=gw.username, password=gw.password,
        )
        gw_type = str(gw.type).lower().replace("gatewaytyepeenum.", "")
        if "goip_udp" in gw_type or gw_type == "goip":
            return GoIPGateway(**kwargs)
        elif "goip_http" in gw_type:
            return GoIPHTTPGateway(**kwargs)
        elif gw_type in ("skyline", "dinstar"):
            return SkylineGateway(**kwargs)
        logger.warning(f"Unknown gateway type: {gw.type}")
        return None

    def add(self, gw_db_record) -> Optional[BaseGateway]:
        """Добавить/обновить шлюз в памяти после сохранения в БД."""
        instance = self._create_instance(gw_db_record)
        if instance:
            self._gateways[gw_db_record.id] = instance
        return instance

    def remove(self, gateway_id: int):
        """Удалить шлюз из памяти."""
        self._gateways.pop(gateway_id, None)

    def get(self, gateway_id: int) -> Optional[BaseGateway]:
        return self._gateways.get(gateway_id)

    def all(self) -> list[BaseGateway]:
        return list(self._gateways.values())

    def count(self) -> int:
        return len(self._gateways)

    async def send_sms(self, gateway_id: int, port_num: int,
                       phone: str, text: str) -> GatewayResponse:
        gw = self.get(gateway_id)
        if not gw:
            return GatewayResponse(False, f"Gateway ID={gateway_id} not found")
        return await gw.send_sms(port_num=port_num, phone=phone, text=text)

    async def check_all_status(self) -> Dict[int, GatewayResponse]:
        """Опросить все шлюзы, вернуть словарь {id: response}."""
        results = {}
        for gw_id, gw in self._gateways.items():
            results[gw_id] = await gw.get_status()
        return results


# Глобальный синглтон для FastAPI
gateway_manager = GatewayManager()
