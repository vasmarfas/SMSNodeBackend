"""
GoIP UDP Gateway — адаптер для GoIP-шлюзов через UDP-протокол.

Использует GoIPUDPClient из того же пакета SMSNodeBackend.
UDP-клиент синхронный, поэтому синхронные вызовы оборачиваем в asyncio.to_thread.
"""

import asyncio
import logging
from typing import Optional

from core.gateways.base import BaseGateway, GatewayResponse
from goip_runtime_registry import get_endpoint_by_id, get_endpoint_by_host

logger = logging.getLogger(__name__)


class GoIPGateway(BaseGateway):
    """
    Интеграция с GoIP-шлюзами через UDP-протокол.
    Порт (self.port) = UDP-порт шлюза (обычно 9991).
    """

    def __init__(self, gateway_id: int, name: str, host: str, port: int,
                 username: str, password: str):
        super().__init__(gateway_id, name, host, port, username, password)
        self._udp_client = None

    def _get_client(self):
        """Лениво создать UDP-клиент."""
        if self._udp_client is None:
            # Импорт здесь, чтобы не зависеть от пути при загрузке модуля
            from goip_udp_client import GoIPUDPClient
            self._udp_client = GoIPUDPClient(goip_ip=self.host, port=self.port, timeout=10)
        return self._udp_client

    def _runtime(self, port_num: Optional[int] = None) -> tuple[int, str]:
        """
        Порт и пароль для UDP-команды.
        Если задан port_num (1–8), используем endpoint канала goip_id=1001..1008 —
        каждый канал GoIP шлёт keepalive со своего порта (10991..10998), по нему и отвечает get_imei.
        """
        if port_num is not None and 1 <= port_num <= 8:
            channel_ep = get_endpoint_by_id(str(1000 + port_num))
            if channel_ep and channel_ep.ip == self.host:
                return (
                    channel_ep.port,
                    channel_ep.password or self.password,
                )
        runtime_ep = get_endpoint_by_id(self.username) or get_endpoint_by_host(self.host)
        target_port = runtime_ep.port if runtime_ep else self.port
        target_password = runtime_ep.password if runtime_ep and runtime_ep.password else self.password
        return target_port, target_password

    async def get_status(self, port_num: Optional[int] = 1) -> GatewayResponse:
        """Получить статус через UDP (IMEI, SIM, состояние линии). port_num=1 по умолчанию (канал 1001)."""
        try:
            client = self._get_client()
            target_port, target_password = self._runtime(port_num=port_num)
            imei = await asyncio.to_thread(client.get_imei, target_password, target_port)
            sim = await asyncio.to_thread(client.get_gsm_number, target_password, target_port)
            state = await asyncio.to_thread(client.get_gsm_state, target_password, target_port)

            if imei:
                self.is_online = True
                return GatewayResponse(True, "Online via UDP",
                                       data={"imei": imei, "sim": sim, "state": state})
            self.is_online = False
            return GatewayResponse(False, "UDP timeout — шлюз не ответил")
        except Exception as e:
            self.is_online = False
            return GatewayResponse(False, str(e))

    async def get_port_status(self, port_num: int) -> GatewayResponse:
        """Статус порта: для многоканального GoIP — порт канала 1001..1008 (UDP 10991..10998)."""
        try:
            client = self._get_client()
            target_port, target_password = self._runtime(port_num=port_num)
            state = await asyncio.to_thread(client.get_gsm_state, target_password, target_port)
            remain = await asyncio.to_thread(client.get_remain_time, target_password, target_port)
            sim = await asyncio.to_thread(client.get_gsm_number, target_password, target_port)
            if state:
                return GatewayResponse(True, "Port status fetched",
                                       data={"state": state, "sim": sim, "remain_time": remain})
            return GatewayResponse(False, "Could not fetch port status")
        except Exception as e:
            return GatewayResponse(False, str(e))

    async def read_sms(self, port_num: int) -> GatewayResponse:
        """
        Чтение входящих SMS с порта GoIP.

        В текущей архитектуре входящие сообщения приходят по UDP/SMTP и
        сразу сохраняются в PostgreSQL (`goip_sms_receiver.py`, `GOIPTools.py`).
        Polling с устройства не используется, чтобы избежать дубликатов и
        лишней нагрузки на шлюз.
        """
        return GatewayResponse(
            False,
            "Polling SMS с GoIP не поддерживается: входящие обрабатываются через UDP/SMTP push",
        )

    async def send_sms(self, port_num: int, phone: str, text: str) -> GatewayResponse:
        """Отправка SMS через UDP на канал port_num (1–8 → goip_id 1001..1008)."""
        try:
            client = self._get_client()
            target_port, target_password = self._runtime(port_num=port_num)
            result = await asyncio.to_thread(
                client.send_sms,
                target_password,
                phone,
                text,
                target_port,
                max(1, int(port_num)),
            )
            return GatewayResponse(result.success, result.message,
                                   data={"raw_response": result.data})
        except Exception as e:
            logger.error(f"GoIPGateway.send_sms failed: {e}")
            return GatewayResponse(False, str(e))

    async def send_ussd(self, port_num: int, ussd_code: str) -> GatewayResponse:
        """USSD-запрос через UDP на канал port_num."""
        try:
            client = self._get_client()
            target_port, target_password = self._runtime(port_num=port_num)
            response = await asyncio.to_thread(
                client.send_ussd, target_password, ussd_code, target_port
            )
            if response:
                return GatewayResponse(True, "USSD OK", data={"response": response})
            return GatewayResponse(False, "USSD timeout")
        except Exception as e:
            return GatewayResponse(False, str(e))

    async def reboot(self, port_num: Optional[int] = None) -> GatewayResponse:
        """Перезагрузка через UDP (если port_num задан — на канал 1000+port_num)."""
        try:
            client = self._get_client()
            target_port, target_password = self._runtime(port_num=port_num)
            success = await asyncio.to_thread(client.reboot_device, target_password, target_port)
            return GatewayResponse(success, "Reboot sent" if success else "Reboot failed")
        except Exception as e:
            return GatewayResponse(False, str(e))

    # Полный UDP API (внутренний доступ, без публичных роутов)
    async def udp_command(self, command: str, port_num: Optional[int] = None, **kwargs) -> GatewayResponse:
        client = self._get_client()
        target_port, target_password = self._runtime(port_num=port_num)
        method = getattr(client, command, None)
        if not method:
            return GatewayResponse(False, f"Unknown UDP command: {command}")
        try:
            call_kwargs = dict(kwargs)
            call_kwargs["target_port"] = target_port
            result = await asyncio.to_thread(method, target_password, **call_kwargs)
            return GatewayResponse(True, "UDP command executed", data={"result": result, "command": command})
        except Exception as e:
            return GatewayResponse(False, str(e))
