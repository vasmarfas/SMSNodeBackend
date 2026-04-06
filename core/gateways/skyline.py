"""
Skyline / Dinstar Gateway — адаптер через HTTP JSON API.
"""

import httpx
import time
from core.gateways.base import BaseGateway, GatewayResponse


class SkylineGateway(BaseGateway):
    """Skyline / Dinstar GSM-шлюз через HTTP POST JSON API."""

    async def get_status(self) -> GatewayResponse:
        url = f"http://{self.host}:{self.port}/goip_get_status.html"
        params = {"username": self.username, "password": self.password,
                  "url": f"http://{self.host}", "period": 60}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, params=params)
                if r.status_code == 200:
                    self.is_online = True
                    ct = r.headers.get("Content-Type", "")
                    data = r.json() if "json" in ct else {"raw": r.text}
                    return GatewayResponse(True, "Online", data=data)
                self.is_online = False
                return GatewayResponse(False, f"HTTP {r.status_code}")
        except Exception as e:
            self.is_online = False
            return GatewayResponse(False, str(e))

    async def get_port_status(self, port_num: int) -> GatewayResponse:
        url = f"http://{self.host}:{self.port}/api/get_port_info"
        params = {
            "username": self.username, "password": self.password,
            "port": port_num,
            "info_type": "type,imei,imsi,iccid,number,reg,slot,callstate,signal,gprs",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, params=params)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("error_code") == 200:
                        info = data.get("info", [{}])[0]
                        return GatewayResponse(True, "Port info fetched", data=info)
                    return GatewayResponse(False, f"API error {data.get('error_code')}")
                return GatewayResponse(False, f"HTTP {r.status_code}")
        except Exception as e:
            return GatewayResponse(False, str(e))

    async def read_sms(self, port_num: int) -> GatewayResponse:
        """
        Чтение входящих SMS для Skyline/Dinstar.

        Для данного типа оборудования в проекте используется модель push-событий
        (вебхуки/UDP) вместо опроса API на предмет новых SMS, поэтому polling
        не реализован.
        """
        return GatewayResponse(
            False,
            "Polling SMS для Skyline/Dinstar не реализован: входящие ожидаются через push-события",
        )

    async def send_sms(self, port_num: int, phone: str, text: str) -> GatewayResponse:
        url = f"http://{self.host}:{self.port}/goip_post_sms.html"
        params = {"username": self.username, "password": self.password}
        tid = int(time.time()) % 100000
        payload = {
            "type": "send-sms",
            "task_num": 1,
            "tasks": [{"tid": tid, "from": str(port_num), "to": phone, "sms": text, "chs": "utf8"}],
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(url, params=params, json=payload)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("code") == 200:
                        return GatewayResponse(True, "SMS queued", data=data)
                    return GatewayResponse(False, f"API error: {data.get('reason')}")
                return GatewayResponse(False, f"HTTP {r.status_code}")
        except Exception as e:
            return GatewayResponse(False, str(e))

    async def send_ussd(self, port_num: int, ussd_code: str) -> GatewayResponse:
        url = f"http://{self.host}:{self.port}/api/send_ussd"
        payload = {"port": [port_num], "command": "send", "text": ussd_code}
        try:
            async with httpx.AsyncClient(
                timeout=10.0, auth=(self.username, self.password)
            ) as client:
                r = await client.post(url, json=payload)
                if r.status_code in (200, 202):
                    return GatewayResponse(True, "USSD sent", data=r.json())
                return GatewayResponse(False, f"HTTP {r.status_code}")
        except Exception as e:
            return GatewayResponse(False, str(e))

    async def reboot(self) -> GatewayResponse:
        url = f"http://{self.host}:{self.port}/goip_send_cmd.html"
        params = {"username": self.username, "password": self.password}
        payload = {"type": "command", "op": "reboot", "ports": "all"}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(url, params=params, json=payload)
                if r.status_code == 200:
                    return GatewayResponse(True, "Rebooting")
                return GatewayResponse(False, f"HTTP {r.status_code}")
        except Exception as e:
            return GatewayResponse(False, str(e))
