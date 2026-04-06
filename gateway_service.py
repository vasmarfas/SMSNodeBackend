"""
GatewayService — менеджер GSM-шлюзов для Telegram-бота.

Вместо хардкода одного шлюза в .env-файле, теперь все шлюзы хранятся
в базе данных. Этот модуль:
  - Загружает список активных шлюзов из SQLite при старте бота.
  - Хранит активные подключения (UDP-клиенты для GoIP) в памяти.
  - Предоставляет единый интерфейс send_sms / test_gateway для всего бота.
  - Синглтон: импортируй `gateway_service` из этого модуля везде.
"""

import asyncio
import logging
import socket
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Any

import aiohttp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import Gateway
from goip_udp_client import GoIPUDPClient, SMSResult
from goip_runtime_registry import get_endpoint_by_id, get_endpoint_by_host, get_any_endpoint_for_host

logger = logging.getLogger(__name__)


class ActiveGateway:
    """
    Обёртка над конкретным шлюзом в памяти.
    Хранит данные из БД + живой объект клиента для отправки команд.
    """

    def __init__(self, db_record: Gateway):
        self.id: int = db_record.id
        self.name: str = db_record.name
        # Нормализуем тип: GatewayTypeEnum.GOIP_UDP.value == "goip_udp"
        raw = db_record.type
        self.type: str = raw.value if hasattr(raw, "value") else str(raw).lower()
        self.host: str = db_record.host
        self.port: int = db_record.port
        self.username: str = db_record.username
        self.password: str = db_record.password
        self.is_online: bool = False

        # UDP-клиент, создаётся только для GoIP-UDP шлюзов
        self._udp_client: Optional[GoIPUDPClient] = None

    def _get_udp_client(self) -> GoIPUDPClient:
        """Лениво создать / переиспользовать UDP-клиент."""
        if self._udp_client is None:
            self._udp_client = GoIPUDPClient(goip_ip=self.host, port=self.port)
        return self._udp_client

    def _udp_runtime(self, port_num: Optional[int] = None) -> tuple[int, str]:
        """
        Определить runtime endpoint/password для GoIP UDP.
        Если port_num 1–8 — канал goip_id 1001..1008 (порт 10991..10998), иначе id/host/конфиг.
        """
        if port_num is not None and 1 <= port_num <= 8:
            ch_ep = get_endpoint_by_id(str(1000 + port_num))
            if ch_ep and ch_ep.ip == self.host:
                return (ch_ep.port, ch_ep.password or self.password)
        runtime_ep = get_endpoint_by_id(self.username) or get_endpoint_by_host(self.host)
        target_port = runtime_ep.port if runtime_ep else self.port
        target_password = runtime_ep.password if runtime_ep and runtime_ep.password else self.password
        return target_port, target_password

    # Отправка SMS

    async def send_sms(self, phone: str, text: str, port_num: int = 1) -> SMSResult:
        """
        Отправить SMS через шлюз.
        - goip_udp  → UDP-протокол (GoIPUDPClient)
        - goip_http → HTTP GET /default/en_US/send.html
        - skyline   → HTTP POST /goip_post_sms.html (JSON)
        """
        if self.type == "goip_udp":
            return await asyncio.get_event_loop().run_in_executor(
                None,
                self._send_udp,
                phone, text, port_num
            )
        elif self.type == "goip_http":
            return await self._send_goip_http(port_num, phone, text)
        elif self.type in ("skyline", "dinstar"):
            return await self._send_skyline(port_num, phone, text)
        else:
            return SMSResult(False, f"Unsupported gateway type: {self.type}")

    def _send_udp(self, phone: str, text: str, port_num: int = 1) -> SMSResult:
        """Синхронная отправка SMS по UDP (GoIP). Канал port_num → goip_id 1001..1008."""
        try:
            client = self._get_udp_client()
            target_port, target_password = self._udp_runtime(port_num=port_num)
            logger.info(
                f"UDP send via {self.host}:{target_port} "
                f"(channel {port_num}, goip_id={1000 + port_num})"
            )
            return client.send_sms(
                password=target_password,
                phone=phone,
                message=text,
                target_port=target_port,
                telid=max(1, int(port_num)),
            )
        except Exception as e:
            logger.error(f"UDP SMS error [{self.name}]: {e}")
            return SMSResult(False, str(e))

    async def _send_goip_http(self, channel: int, phone: str, text: str) -> SMSResult:
        """HTTP GET API (GoIP basic HTTP)."""
        url = f"http://{self.host}:{self.port}/default/en_US/send.html"
        params = {"u": self.username, "p": self.password, "l": channel, "n": phone, "m": text}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
                async with http.get(url, params=params) as resp:
                    body = await resp.text()
                    if resp.status == 200 and body.startswith("Sending"):
                        msg_id = body.split("ID:")[-1].strip() if "ID:" in body else ""
                        return SMSResult(True, "Queued", msg_id)
                    return SMSResult(False, f"HTTP {resp.status}: {body[:200]}")
        except Exception as e:
            return SMSResult(False, str(e))

    async def _send_skyline(self, port_num: int, phone: str, text: str) -> SMSResult:
        """HTTP POST JSON API (Skyline / Dinstar)."""
        import urllib.parse
        url = f"http://{self.host}:{self.port}/goip_post_sms.html"
        params = {"username": self.username, "password": self.password}
        # Генерируем псевдо-уникальный tid из номера телефона
        tid = int(time.time()) % 100000
        payload = {
            "type": "send-sms",
            "task_num": 1,
            "tasks": [{"tid": tid, "from": str(port_num), "to": phone, "sms": text, "chs": "utf8"}]
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
                async with http.post(url, params=params, json=payload) as resp:
                    data = await resp.json()
                    code = data.get("code", -1)
                    if code == 200:
                        return SMSResult(True, "Queued", str(tid))
                    return SMSResult(False, f"API error {code}: {data.get('reason')}")
        except Exception as e:
            return SMSResult(False, str(e))

    # Проверка статуса / пинг

    async def ping(self) -> dict:
        """
        Проверить связь со шлюзом.
        Возвращает: {"online": bool, "detail": str, "latency_ms": float|None}
        """
        if self.type == "goip_udp":
            return await asyncio.get_event_loop().run_in_executor(None, self._ping_udp)
        else:
            return await self._ping_http()

    def _ping_udp(self) -> dict:
        """UDP ping: получаем IMEI по каналу 1 (1001). Первый запрос откладываем, если endpoint ещё нет."""
        start = time.monotonic()
        try:
            client = self._get_udp_client()
            # Сначала пробуем канал 1; если нет endpoint — ждём keepalive и повторяем один раз
            target_port, target_password = self._udp_runtime(port_num=1)
            if target_port == self.port and get_any_endpoint_for_host(self.host) is None:
                time.sleep(3)
                target_port, target_password = self._udp_runtime(port_num=1)
            if target_port == self.port and get_any_endpoint_for_host(self.host) is None:
                return {
                    "online": False,
                    "detail": "Ожидание keepalive от GoIP (подождите 5–10 с)",
                    "latency_ms": None,
                }
            logger.debug(
                f"UDP ping via {self.host}:{target_port} (channel 1, goip_id=1001)"
            )
            imei = client.get_imei(target_password, target_port=target_port)
            ms = round((time.monotonic() - start) * 1000, 1)
            if imei:
                self.is_online = True
                return {"online": True, "detail": f"IMEI: {imei}", "latency_ms": ms}
            self.is_online = False
            return {"online": False, "detail": "UDP timeout (нет ответа)", "latency_ms": None}
        except Exception as e:
            self.is_online = False
            return {"online": False, "detail": str(e), "latency_ms": None}

    async def _ping_http(self) -> dict:
        """HTTP ping: проверяем доступность веб-интерфейса шлюза."""
        url = f"http://{self.host}:{self.port}/"
        start = time.monotonic()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
                async with http.get(url) as resp:
                    ms = round((time.monotonic() - start) * 1000, 1)
                    self.is_online = resp.status in (200, 401, 403)
                    return {
                        "online": self.is_online,
                        "detail": f"HTTP {resp.status}",
                        "latency_ms": ms
                    }
        except Exception as e:
            self.is_online = False
            return {"online": False, "detail": str(e), "latency_ms": None}

    def get_ussd(self, ussd_code: str) -> Optional[str]:
        """Синхронный USSD-запрос (только GoIP UDP)."""
        if self.type != "goip_udp":
            return None
        try:
            client = self._get_udp_client()
            target_port, target_password = self._udp_runtime()
            return client.send_ussd(target_password, ussd_code, target_port=target_port)
        except Exception as e:
            logger.error(f"USSD error [{self.name}]: {e}")
            return None

    # Полный UDP API (внутренний доступ)
    def udp_get_imei(self) -> Optional[str]:
        if self.type != "goip_udp":
            return None
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.get_imei(target_password, target_port=target_port)

    def udp_get_gsm_num(self) -> Optional[str]:
        if self.type != "goip_udp":
            return None
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.get_gsm_number(target_password, target_port=target_port)

    def udp_get_gsm_state(self) -> Optional[str]:
        if self.type != "goip_udp":
            return None
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.get_gsm_state(target_password, target_port=target_port)

    def udp_get_remain_time(self) -> Optional[int]:
        if self.type != "goip_udp":
            return None
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.get_remain_time(target_password, target_port=target_port)

    def udp_get_exp_time(self) -> Optional[int]:
        if self.type != "goip_udp":
            return None
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.get_exp_time(target_password, target_port=target_port)

    def udp_set_exp_time(self, exp_minutes: int) -> bool:
        if self.type != "goip_udp":
            return False
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.set_exp_time(target_password, exp_minutes, target_port=target_port)

    def udp_reset_remain_time(self) -> bool:
        if self.type != "goip_udp":
            return False
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.reset_remain_time(target_password, target_port=target_port)

    def udp_set_imei(self, imei: str) -> bool:
        if self.type != "goip_udp":
            return False
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.set_imei(target_password, imei, target_port=target_port)

    def udp_set_gsm_num(self, gsm_num: str) -> bool:
        if self.type != "goip_udp":
            return False
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.set_gsm_number(target_password, gsm_num, target_port=target_port)

    def udp_drop_call(self) -> bool:
        if self.type != "goip_udp":
            return False
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.drop_call(target_password, target_port=target_port)

    def udp_reboot_module(self) -> bool:
        if self.type != "goip_udp":
            return False
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.reboot_module(target_password, target_port=target_port)

    def udp_reboot_device(self) -> bool:
        if self.type != "goip_udp":
            return False
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.reboot_device(target_password, target_port=target_port)

    def udp_set_call_forward(self, reason: int, mode: int, number: str, timeout: int = 0) -> bool:
        if self.type != "goip_udp":
            return False
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.set_call_forward(
            target_password, reason=reason, mode=mode, number=number, timeout=timeout, target_port=target_port
        )

    def udp_get_out_call_interval(self) -> Optional[int]:
        if self.type != "goip_udp":
            return None
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.get_out_call_interval(target_password, target_port=target_port)

    def udp_set_out_call_interval(self, interval_sec: int) -> bool:
        if self.type != "goip_udp":
            return False
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.set_out_call_interval(target_password, interval_sec, target_port=target_port)

    def udp_module_ctl_i(self, value: int) -> bool:
        if self.type != "goip_udp":
            return False
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.module_ctl_i(target_password, value, target_port=target_port)

    def udp_module_ctl(self, value: str) -> bool:
        if self.type != "goip_udp":
            return False
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.module_ctl(target_password, value, target_port=target_port)

    def udp_set_base_cell(self, cell_id: int) -> bool:
        if self.type != "goip_udp":
            return False
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.set_base_cell(target_password, cell_id, target_port=target_port)

    def udp_get_cells_list(self) -> bool:
        if self.type != "goip_udp":
            return False
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.get_cells_list(target_password, target_port=target_port)

    def udp_get_current_cell(self) -> Optional[int]:
        if self.type != "goip_udp":
            return None
        client = self._get_udp_client()
        target_port, target_password = self._udp_runtime()
        return client.get_current_cell(target_password, target_port=target_port)


class GatewayService:
    """
    Синглтон-менеджер, держит все активные шлюзы в памяти.
    Загружается из БД при старте бота через `await gateway_service.load_from_db(session)`.
    """

    def __init__(self):
        self._gateways: Dict[int, ActiveGateway] = {}
        self._lock = asyncio.Lock()

    async def load_from_db(self, session: AsyncSession) -> int:
        """
        Загрузить все активные шлюзы из базы данных.
        Вызывается один раз при старте бота.
        Возвращает количество загруженных шлюзов.
        """
        result = await session.execute(
            select(Gateway).where(Gateway.is_active == True)
        )
        gateways = result.scalars().all()

        async with self._lock:
            self._gateways.clear()
            for gw in gateways:
                self._gateways[gw.id] = ActiveGateway(gw)

        logger.info(f"GatewayService: loaded {len(gateways)} gateways from DB.")
        return len(gateways)

    def reload_gateway(self, gw_record: Gateway):
        """Обновить/добавить один шлюз из его DB-записи (после сохранения)."""
        self._gateways[gw_record.id] = ActiveGateway(gw_record)

    def remove_gateway(self, gateway_id: int):
        """Удалить шлюз из памяти (не трогает БД)."""
        self._gateways.pop(gateway_id, None)

    # Обращение к шлюзам

    def get(self, gateway_id: int) -> Optional[ActiveGateway]:
        return self._gateways.get(gateway_id)

    def all(self) -> list[ActiveGateway]:
        return list(self._gateways.values())

    def count(self) -> int:
        return len(self._gateways)

    async def send_sms(
        self,
        gateway_id: int,
        phone: str,
        text: str,
        port_num: int = 1
    ) -> SMSResult:
        """Отправить SMS через конкретный шлюз по его ID."""
        gw = self.get(gateway_id)
        if not gw:
            return SMSResult(False, f"Шлюз ID={gateway_id} не найден в менеджере")
        return await gw.send_sms(phone=phone, text=text, port_num=port_num)

    def run_udp_command(self, gateway_id: int, command: str, **kwargs):
        """
        Внутренний API для полного GoIP UDP набора команд.
        command: имя метода ActiveGateway без префикса 'udp_'.
        Пример: run_udp_command(1, 'set_imei', imei='123...')
        """
        gw = self.get(gateway_id)
        if not gw or gw.type != "goip_udp":
            return None
        method = getattr(gw, f"udp_{command}", None)
        if not method:
            return None
        return method(**kwargs)

    async def send_sms_auto(self, phone: str, text: str) -> SMSResult:
        """
        Отправить SMS через первый доступный онлайн-шлюз.
        Используется для обратной совместимости с GOIPTools.
        """
        for gw in self._gateways.values():
            if gw.is_online:
                return await gw.send_sms(phone=phone, text=text)
        # Если нет онлайн-шлюза — пробуем первый попавшийся
        if self._gateways:
            first = next(iter(self._gateways.values()))
            return await first.send_sms(phone=phone, text=text)
        return SMSResult(False, "Нет доступных шлюзов. Добавьте шлюз через /gateways.")

    async def ping_all(self, session: AsyncSession) -> dict:
        """
        Пинговать все шлюзы, обновить статус last_seen / last_status в БД.
        Возвращает словарь {gateway_id: ping_result}.
        """
        results = {}
        for gw_id, gw in self._gateways.items():
            result = await gw.ping()
            results[gw_id] = result

            # Обновляем запись в БД
            try:
                db_gw = await session.get(Gateway, gw_id)
                if db_gw:
                    db_gw.last_seen = (
                        datetime.now(timezone.utc) if result["online"] else db_gw.last_seen
                    )
                    status_str = "online" if result["online"] else f"offline: {result['detail']}"
                    db_gw.last_status = (status_str[:50] if status_str else None)
                    await session.commit()
                else:
                    # Шлюз удален из БД, удаляем из кэша
                    self.remove_gateway(gw_id)
            except Exception as e:
                logger.warning(f"Failed to update gateway status in DB: {e}")

        return results


# Глобальный синглтон — импортируй его везде
gateway_service = GatewayService()
