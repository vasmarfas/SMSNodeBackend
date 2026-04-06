import abc
from typing import Dict, Any, Optional


class GatewayResponse:
    """Унифицированный ответ любого GSM-шлюза."""

    def __init__(self, success: bool, message: str, data: Optional[Dict[str, Any]] = None):
        self.success = success
        self.message = message
        self.data = data or {}

    def __repr__(self):
        return f"<GatewayResponse success={self.success} message='{self.message}'>"


class BaseGateway(abc.ABC):
    """
    Абстрактный базовый класс для всех GSM-шлюзов.
    Каждый конкретный шлюз (GoIP, Skyline, Dinstar) наследуется отсюда
    и реализует абстрактные методы.
    """

    def __init__(self, gateway_id: int, name: str, host: str, port: int,
                 username: str, password: str):
        self.gateway_id = gateway_id
        self.name = name
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.is_online = False

    @abc.abstractmethod
    async def get_status(self) -> GatewayResponse:
        """Получить общий статус шлюза (онлайн, загрузка CPU, память)."""
        pass

    @abc.abstractmethod
    async def get_port_status(self, port_num: int) -> GatewayResponse:
        """Получить статус конкретного порта (уровень сигнала, SIM)."""
        pass

    @abc.abstractmethod
    async def read_sms(self, port_num: int) -> GatewayResponse:
        """
        Прочитать входящие SMS с указанного порта.

        В большинстве поддерживаемых шлюзов входящие сообщения приходят
        через push-механизмы (UDP/HTTP webhook), поэтому реализация по
        умолчанию может возвращать сообщение о неподдерживаемой операции.
        """
        pass

    @abc.abstractmethod
    async def send_sms(self, port_num: int, phone: str, text: str) -> GatewayResponse:
        """Отправить SMS через указанный порт."""
        pass

    @abc.abstractmethod
    async def send_ussd(self, port_num: int, ussd_code: str) -> GatewayResponse:
        """Отправить USSD-запрос (проверка баланса и т.д.)."""
        pass

    @abc.abstractmethod
    async def reboot(self) -> GatewayResponse:
        """Перезагрузить шлюз."""
        pass
