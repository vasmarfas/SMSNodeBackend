import asyncio
import aiohttp
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
import re

from config_reader import config
from bot import bot
from core.db.database import AsyncSessionLocal


class GOIPMonitor:
    """
    Мониторинг GSM-шлюзов через GatewayService.

    Вместо хардкода config.GOIP_IP теперь опрашиваются все шлюзы,
    добавленные в систему через /gateways.
    """

    def __init__(self):
        self.last_check = None
        self.channel_status = {}  # channel -> status_info
        self.goip_online = False
        self.monitoring_active = False
        self.check_interval = 60  # секунд

    async def start_monitoring(self):
        """Запустить мониторинг"""
        self.monitoring_active = True
        logging.info("Запуск мониторинга GSM-шлюзов")

        while self.monitoring_active:
            try:
                await self.check_all_gateways()
                await asyncio.sleep(self.check_interval)
            except Exception as e:
                logging.error(f"Ошибка в мониторинге шлюзов: {e}")
                await asyncio.sleep(self.check_interval)

    def stop_monitoring(self):
        """Остановить мониторинг"""
        self.monitoring_active = False
        logging.info("Остановлен мониторинг GSM-шлюзов")

    async def check_all_gateways(self):
        """Проверить статус всех шлюзов через GatewayService."""
        from gateway_service import gateway_service

        if gateway_service.count() == 0:
            return

        async with AsyncSessionLocal() as session:
            results = await gateway_service.ping_all(session)

        online_count = sum(1 for r in results.values() if r["online"])
        was_online_before = self.goip_online
        self.goip_online = online_count > 0
        self.last_check = datetime.now(timezone.utc)

        # Уведомляем при смене статуса
        if not was_online_before and self.goip_online:
            await self.notify_gateways_online(online_count, len(results))
        elif was_online_before and not self.goip_online:
            await self.notify_gateways_offline(len(results))

    # Обратная совместимость: оставляем check_goip_status как обёртку
    async def check_goip_status(self):
        await self.check_all_gateways()

    def was_online(self) -> bool:
        """Был ли шлюз онлайн при предыдущей проверке"""
        return hasattr(self, '_previous_online') and self._previous_online

    async def parse_status_html(self, html: str):
        """Парсинг HTML страницы статуса GOIP"""
        # Ищем информацию о каналах в HTML
        # Это примерный парсинг, может потребоваться адаптация под реальный HTML GOIP

        previous_channels = self.channel_status.copy()
        self.channel_status = {}

        # Пример регулярных выражений для поиска статуса каналов
        # Нужно адаптировать под реальный HTML GOIP
        channel_patterns = [
            r'Channel\s*(\d+).*?Status:\s*([^<\n]+)',
            r'Port\s*(\d+).*?State:\s*([^<\n]+)',
            r'Line\s*(\d+).*?Status:\s*([^<\n]+)'
        ]

        for pattern in channel_patterns:
            matches = re.findall(pattern, html, re.IGNORECASE | re.MULTILINE)
            for channel_str, status in matches:
                try:
                    channel = int(channel_str)
                    if 1 <= channel <= config.MAX_CHANNELS:
                        self.channel_status[channel] = {
                            'status': status.strip(),
                            'last_update': datetime.now(timezone.utc)
                        }
                except ValueError:
                    continue

        # Если не нашли информацию о каналах через regex, создадим базовую
        if not self.channel_status:
            for channel in range(1, config.MAX_CHANNELS + 1):
                self.channel_status[channel] = {
                    'status': 'unknown',
                    'last_update': datetime.now(timezone.utc)
                }

        # Проверяем изменения статуса каналов
        await self.check_channel_changes(previous_channels)

        self._previous_online = True

    async def check_channel_changes(self, previous_channels: Dict):
        """Проверить изменения статуса каналов"""
        for channel in range(1, config.MAX_CHANNELS + 1):
            current = self.channel_status.get(channel, {}).get('status', 'unknown')
            previous = previous_channels.get(channel, {}).get('status', 'unknown')

            if current != previous and previous != 'unknown':
                await self.notify_channel_status_change(channel, previous, current)

    async def notify_gateways_online(self, online: int, total: int):
        """Уведомление о восстановлении связи со шлюзами."""
        message = (
            f"[OK] Шлюзы снова доступны: {online}/{total}\n"
            f"Время: {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M:%S')}"
        )
        try:
            await bot.send_message(config.ADMIN_ID, message)
        except Exception as e:
            logging.error(f"Ошибка отправки уведомления шлюз онлайн: {e}")

    async def notify_gateways_offline(self, total: int):
        """Уведомление о потере связи со всеми шлюзами."""
        message = (
            f"[!!] Все {total} шлюза(ов) недоступны!\n"
            f"Время: {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M:%S')}\n\n"
            "Проверьте подключение устройств к сети.\n"
            "Управление: /gateways"
        )
        try:
            await bot.send_message(config.ADMIN_ID, message)
        except Exception as e:
            logging.error(f"Ошибка отправки уведомления шлюзы оффлайн: {e}")

    # Устаревшие методы (оставлены для совместимости)
    async def notify_goip_online(self):
        await self.notify_gateways_online(1, 1)

    async def notify_goip_offline(self):
        await self.notify_gateways_offline(1)

    async def notify_channel_status_change(self, channel: int, old_status: str, new_status: str):
        """Уведомление об изменении статуса канала"""
        message = ("📡 Изменение статуса канала\n\n"
                  f"Канал: {channel}\n"
                  f"Статус: {old_status} → {new_status}\n"
                  f"Время: {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M:%S')}")

        try:
            await bot.send_message(config.ADMIN_ID, message)
            logging.info(f"Отправлено уведомление: канал {channel} {old_status} → {new_status}")
        except Exception as e:
            logging.error(f"Ошибка отправки уведомления изменения канала: {e}")

    def get_status_summary(self) -> str:
        """Получить сводку статуса GOIP"""
        if not self.last_check:
            return "📊 Статус GOIP: не проверялся"

        status_icon = "🟢" if self.goip_online else "🔴"
        status_text = "онлайн" if self.goip_online else "оффлайн"

        text = f"{status_icon} GOIP: {status_text}\n"
        text += f"📡 Последняя проверка: {self.last_check.strftime('%H:%M:%S')}\n\n"

        if self.goip_online and self.channel_status:
            text += "📶 Каналы:\n"
            for channel in range(1, config.MAX_CHANNELS + 1):
                ch_status = self.channel_status.get(channel, {})
                status = ch_status.get('status', 'неизвестно')
                last_update = ch_status.get('last_update')

                if last_update:
                    time_diff = datetime.now(timezone.utc) - last_update
                    if time_diff < timedelta(minutes=5):
                        icon = "🟢"
                    elif time_diff < timedelta(minutes=15):
                        icon = "🟡"
                    else:
                        icon = "🔴"
                else:
                    icon = "⚪"

                text += f"{icon} Канал {channel}: {status}\n"

        return text

    def get_channel_status(self, channel: int) -> Optional[Dict]:
        """Получить статус конкретного канала"""
        return self.channel_status.get(channel)

    def is_channel_available(self, channel: int) -> bool:
        """Проверить доступность канала"""
        ch_status = self.get_channel_status(channel)
        if not ch_status:
            return False

        status = ch_status.get('status', '').lower()
        # Считаем канал доступным если статус содержит слова типа "ready", "free", "idle"
        available_statuses = ['ready', 'free', 'idle', 'available', 'ok']
        unavailable_statuses = ['busy', 'calling', 'error', 'offline']

        for bad_status in unavailable_statuses:
            if bad_status in status:
                return False

        for good_status in available_statuses:
            if good_status in status:
                return True

        # Если статус неизвестен, считаем канал доступным
        return True


# Глобальный экземпляр монитора
goip_monitor = GOIPMonitor()


async def start_goip_monitoring():
    """Запуск мониторинга GOIP в фоне"""
    asyncio.create_task(goip_monitor.start_monitoring())


async def stop_goip_monitoring():
    """Остановка мониторинга GOIP"""
    goip_monitor.stop_monitoring()