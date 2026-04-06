"""
GoIP HTTP Gateway — адаптер для GoIP через HTTP GET API.

Документация: httpapi.txt из gsm-gateway-spec.
URL: http://{host}:{port}/default/en_US/send.html?u=LOGIN&p=PASS&l=CH&n=PHONE&m=MSG
"""

import httpx
from core.gateways.base import BaseGateway, GatewayResponse


class GoIPHTTPGateway(BaseGateway):
    """GoIP через HTTP GET API (альтернатива UDP для старых прошивок)."""

    async def get_status(self) -> GatewayResponse:
        url = f"http://{self.host}:{self.port}/default/en_US/status.xml"
        params = {"u": self.username, "p": self.password}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, params=params)
                if r.status_code == 200:
                    self.is_online = True
                    return GatewayResponse(True, "Online", data={"raw_xml": r.text})
                self.is_online = False
                return GatewayResponse(False, f"HTTP {r.status_code}")
        except Exception as e:
            self.is_online = False
            return GatewayResponse(False, str(e))

    async def get_port_status(self, port_num: int) -> GatewayResponse:
        """GoIP HTTP не имеет отдельного запроса по порту — используем общий статус."""
        return await self.get_status()

    async def read_sms(self, port_num: int) -> GatewayResponse:
        """
        Чтение входящих SMS через HTTP API GoIP.

        В текущей системе приём входящих сообщений реализован через UDP/SMTP
        и push-события, поэтому HTTP polling для SMS намеренно не используется.
        """
        return GatewayResponse(
            False,
            "Polling входящих SMS через GoIP HTTP API не используется (используется UDP/SMTP push)",
        )

    async def send_sms(self, port_num: int, phone: str, text: str) -> GatewayResponse:
        url = f"http://{self.host}:{self.port}/default/en_US/send.html"
        params = {
            "u": self.username, "p": self.password,
            "l": port_num, "n": phone, "m": text,
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(url, params=params)
                if r.status_code == 200:
                    body = r.text.strip()
                    if body.startswith("Sending"):
                        msg_id = body.split("ID:")[-1].strip() if "ID:" in body else ""
                        return GatewayResponse(True, "Queued", data={"message_id": msg_id, "raw": body})
                    return GatewayResponse(False, f"Gateway error: {body}")
                return GatewayResponse(False, f"HTTP {r.status_code}")
        except Exception as e:
            return GatewayResponse(False, str(e))

    async def get_sms_delivery_status(self) -> GatewayResponse:
        """Проверить статусы отправки (delivery report)."""
        url = f"http://{self.host}:{self.port}/default/en_US/send_status.xml"
        params = {"u": self.username, "p": self.password}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, params=params)
                if r.status_code == 200:
                    return GatewayResponse(True, "Delivery status fetched", data={"raw_xml": r.text})
                return GatewayResponse(False, f"HTTP {r.status_code}")
        except Exception as e:
            return GatewayResponse(False, str(e))

    async def send_ussd(self, port_num: int, ussd_code: str) -> GatewayResponse:
        """В GoIP HTTP API USSD не поддерживается — только через UDP."""
        return GatewayResponse(False, "USSD not available in HTTP API (use GoIP UDP)")

    async def reboot(self) -> GatewayResponse:
        url = f"http://{self.host}:{self.port}/default/en_US/tools.html"
        params = {"type": "reboot", "u": self.username, "p": self.password}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.get(url, params=params)
                return GatewayResponse(True, "Reboot command sent")
        except Exception as e:
            return GatewayResponse(False, str(e))
