"""
Runtime-реестр UDP endpoint'ов GoIP.

Зачем нужен:
- По спецификации GoIP SMS Interface сервер получает keepalive/RECEIVE от GoIP.
- На практике GoIP часто отвечает только на тот UDP endpoint, с которого
  общается в текущей сессии, а не на "жёсткий" порт 9991.
- Поэтому фиксируем последний (ip, port) по goip id и используем его для
  команд send_sms/get_imei/get_status.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple


@dataclass
class GoIPEndpoint:
    goip_id: str
    ip: str
    port: int
    password: str
    last_seen: datetime


_by_id: Dict[str, GoIPEndpoint] = {}
_by_host: Dict[str, GoIPEndpoint] = {}


def update_endpoint(goip_id: str, password: str, addr: Tuple[str, int]) -> None:
    ip, port = addr
    ep = GoIPEndpoint(
        goip_id=goip_id,
        ip=ip,
        port=port,
        password=password,
        last_seen=datetime.now(timezone.utc),
    )
    _by_id[goip_id] = ep
    _by_host[ip] = ep


def get_endpoint_by_id(goip_id: str) -> Optional[GoIPEndpoint]:
    return _by_id.get(goip_id)


def get_endpoint_by_host(host: str) -> Optional[GoIPEndpoint]:
    return _by_host.get(host)


def get_any_endpoint_for_host(host: str) -> Optional[GoIPEndpoint]:
    """
    Любой известный endpoint для host (для многоканального GoIP — один из 1001..1008).
    Нужно для первого ping до прихода keepalive: не слать на 9991, а подождать или взять канал.
    """
    ep = _by_host.get(host)
    if ep:
        return ep
    for i in range(1, 9):
        ep = _by_id.get(str(1000 + i))
        if ep and ep.ip == host:
            return ep
    return None
