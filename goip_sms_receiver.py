"""
GoIP UDP SMS Receiver — сервер приёма входящих SMS от GoIP по UDP-протоколу.

Согласно официальной спецификации GoIP SMS Interface (goip_sms_Interface_en.txt),
GoIP отправляет входящее SMS на SMS-сервер в формате UDP:

    RECEIVE:$recvid;id:$id;pass:$password;srcnum:$srcnum;msg:$msg

Сервер должен ответить подтверждением (ACK) в течение 3 секунд:
    RECEIVE $recvid OK

Если ACK не получен, GoIP повторит отправку до 3 раз.

Также по той же спецификации GoIP периодически шлёт keepalive-пакеты:
    req:$count;id:$id;pass:$password;num:$gsm_num;signal:$signal;...
Ответ сервера:
    reg:$count;status:0;

Настройка GoIP:
  - В веб-интерфейсе шлюза включить "SMS Sender"
  - Указать адрес этого сервера и GOIP_LISTEN_PORT (по умолчанию 9999)
"""

import asyncio
import logging
import re
from typing import Callable, Awaitable, Optional, Dict, Any
from goip_runtime_registry import update_endpoint

logger = logging.getLogger(__name__)

# Входящее SMS (раздел 4 спецификации)
_RECEIVE_RE = re.compile(
    r"RECEIVE:(?P<recvid>\d+);id:(?P<gw_id>[^;]+);pass(?:word)?:(?P<password>[^;]+)"
    r";srcnum:(?P<srcnum>[^;]+);msg:(?P<msg>.+)",
    re.DOTALL,
)

# Keepalive (раздел 2 спецификации)
_KEEPALIVE_RE = re.compile(
    r"req:(?P<count>\d+);id:(?P<gw_id>[^;]+);pass(?:word)?:(?P<password>[^;]+)"
    r";num:(?P<num>[^;]*);signal:(?P<signal>[^;]*)"
    r"(?:;gsm_status:(?P<gsm_status>[^;]*))?(?:;imei:(?P<imei>[^;]*))?",
)

# Статус канала (раздел 5.2.1)
_STATE_RE = re.compile(
    r"STATE:(?P<recvid>\d+);id:(?P<gw_id>[^;]+);pass(?:word)?:(?P<password>[^;]+)"
    r";gsm_remain_state:(?P<state>[^;]+)",
)

# Статус звонка (раздел 5.2.2)
_RECORD_RE = re.compile(
    r"RECORD:(?P<recvid>\d+);id:(?P<gw_id>[^;]+);pass(?:word)?:(?P<password>[^;]+)"
    r";dir:(?P<dir>\d+);num:(?P<num>[^;]+)",
)

# Остаток времени (раздел 5.2.3)
_REMAIN_RE = re.compile(
    r"REMAIN:(?P<recvid>\d+);id:(?P<gw_id>[^;]+);pass(?:word)?:(?P<password>[^;]+)"
    r";gsm_remain_time:(?P<remain_time>[^;]+)",
)

# Список ячеек (раздел 10.1)
_CELLS_RE = re.compile(
    r"CELLS:(?P<recvid>\d+);id:(?P<gw_id>[^;]+);pass(?:word)?:(?P<password>[^;]+)"
    r";lists:(?P<cells>.+)",
)

# Отчёт о доставке (практика GoIP)
_DELIVER_RE = re.compile(
    r"DELIVER:(?P<recvid>\d+);id:(?P<gw_id>[^;]+);pass(?:word)?:(?P<password>[^;]+)"
    r";sms_no:(?P<sms_no>[^;]+);state:(?P<state>[^;]+);num:(?P<num>[^;]+)",
)


SMSCallback = Callable[[str, str, str, str, tuple], Awaitable[None]]
EventCallback = Callable[[str, str, Dict[str, Any], tuple], Awaitable[None]]


class GoIPUDPProtocol(asyncio.DatagramProtocol):
    """
    asyncio-протокол UDP для GoIP.

    on_sms_received(recvid, gw_id, src_phone, text, addr) — вызывается при входящем SMS.
    """

    def __init__(self, on_sms_received: SMSCallback, on_event: Optional[EventCallback] = None):
        self._on_sms = on_sms_received
        self._on_event = on_event
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport
        addr = transport.get_extra_info("sockname")
        logger.info(f"GoIP UDP SMS-приёмник запущен на {addr[0]}:{addr[1]}")

    def datagram_received(self, data: bytes, addr: tuple):
        try:
            text = data.decode("utf-8", errors="replace").strip()
            logger.debug(f"UDP [{addr[0]}:{addr[1]}] -> {text[:200]}")
            asyncio.create_task(self._handle(text, addr))
        except Exception as e:
            logger.error(f"Ошибка декодирования UDP-пакета: {e}")

    async def _handle(self, text: str, addr: tuple):
        m = _RECEIVE_RE.match(text)
        if m:
            recvid = m.group("recvid")
            update_endpoint(
                goip_id=m.group("gw_id"),
                password=m.group("password"),
                addr=addr,
            )
            self._ack(f"RECEIVE {recvid} OK\n", addr)
            if self._on_event:
                await self._on_event(
                    "receive",
                    m.group("gw_id"),
                    {
                        "recvid": recvid,
                        "srcnum": m.group("srcnum"),
                        "msg": m.group("msg"),
                    },
                    addr,
                )
            await self._on_sms(
                recvid=recvid,
                gw_id=m.group("gw_id"),
                src_phone=m.group("srcnum"),
                text=m.group("msg"),
                addr=addr,
            )
            return

        m = _KEEPALIVE_RE.match(text)
        if m:
            count = m.group("count")
            update_endpoint(
                goip_id=m.group("gw_id"),
                password=m.group("password"),
                addr=addr,
            )
            self._ack(f"reg:{count};status:0;\n", addr)
            if self._on_event:
                await self._on_event(
                    "keepalive",
                    m.group("gw_id"),
                    {
                        "count": count,
                        "num": m.group("num"),
                        "signal": m.group("signal"),
                        "gsm_status": m.group("gsm_status"),
                        "imei": m.group("imei"),
                    },
                    addr,
                )
            logger.debug(
                f"Keepalive от {m.group('gw_id')} (сигнал {m.group('signal')}, "
                f"GSM {m.group('gsm_status') or 'N/A'})"
            )
            return

        m = _STATE_RE.match(text)
        if m:
            recvid = m.group("recvid")
            update_endpoint(
                goip_id=m.group("gw_id"),
                password=m.group("password"),
                addr=addr,
            )
            self._ack(f"STATE {recvid} OK\n", addr)
            if self._on_event:
                await self._on_event(
                    "state",
                    m.group("gw_id"),
                    {
                        "recvid": recvid,
                        "state": m.group("state"),
                    },
                    addr,
                )
            logger.debug(f"STATE от {m.group('gw_id')}: {m.group('state')}")
            return

        m = _RECORD_RE.match(text)
        if m:
            recvid = m.group("recvid")
            update_endpoint(
                goip_id=m.group("gw_id"),
                password=m.group("password"),
                addr=addr,
            )
            self._ack(f"RECORD {recvid} OK\n", addr)
            if self._on_event:
                await self._on_event(
                    "record",
                    m.group("gw_id"),
                    {
                        "recvid": recvid,
                        "dir": m.group("dir"),
                        "num": m.group("num"),
                    },
                    addr,
                )
            return

        m = _REMAIN_RE.match(text)
        if m:
            recvid = m.group("recvid")
            update_endpoint(
                goip_id=m.group("gw_id"),
                password=m.group("password"),
                addr=addr,
            )
            self._ack(f"REMAIN {recvid} OK\n", addr)
            if self._on_event:
                await self._on_event(
                    "remain",
                    m.group("gw_id"),
                    {
                        "recvid": recvid,
                        "remain_time": m.group("remain_time"),
                    },
                    addr,
                )
            return

        m = _CELLS_RE.match(text)
        if m:
            recvid = m.group("recvid")
            update_endpoint(
                goip_id=m.group("gw_id"),
                password=m.group("password"),
                addr=addr,
            )
            self._ack(f"CELLS {recvid} OK\n", addr)
            cells_raw = m.group("cells")
            cells_list = [c for c in cells_raw.split(",") if c]
            if self._on_event:
                await self._on_event(
                    "cells",
                    m.group("gw_id"),
                    {
                        "recvid": recvid,
                        "cells_raw": cells_raw,
                        "cells": cells_list,
                    },
                    addr,
                )
            return

        m = _DELIVER_RE.match(text)
        if m:
            recvid = m.group("recvid")
            update_endpoint(
                goip_id=m.group("gw_id"),
                password=m.group("password"),
                addr=addr,
            )
            self._ack(f"DELIVER {recvid} OK\n", addr)
            if self._on_event:
                await self._on_event(
                    "deliver",
                    m.group("gw_id"),
                    {
                        "recvid": recvid,
                        "sms_no": m.group("sms_no"),
                        "state": m.group("state"),
                        "num": m.group("num"),
                    },
                    addr,
                )
            return

        logger.warning(f"Неизвестный GoIP UDP-пакет от {addr}: {text[:100]}")

    def _ack(self, msg: str, addr: tuple):
        if self.transport:
            self.transport.sendto(msg.encode("utf-8"), addr)

    def error_received(self, exc: Exception):
        logger.error(f"GoIP UDP ошибка: {exc}")

    def connection_lost(self, exc: Optional[Exception]):
        if exc:
            logger.error(f"GoIP UDP соединение потеряно: {exc}")


async def start_goip_udp_receiver(
    host: str,
    port: int,
    on_sms_received: SMSCallback,
    on_event: Optional[EventCallback] = None,
) -> asyncio.DatagramTransport:
    """
    Запустить UDP-сервер приёма SMS.

    Args:
        host: Адрес для биндинга (обычно "0.0.0.0")
        port: UDP-порт (GOIP_LISTEN_PORT из .env, по умолчанию 9999)
        on_sms_received: async-коллбэк (recvid, gw_id, src_phone, text, addr)

    Returns:
        asyncio.DatagramTransport — можно закрыть при остановке.
    """
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: GoIPUDPProtocol(on_sms_received, on_event=on_event),
        local_addr=(host, port),
    )
    return transport
