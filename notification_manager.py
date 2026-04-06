import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
import asyncio

from bot import bot
from config_reader import config
from database import create_db_session_pool, get_sms_stats


class NotificationManager:
    """Менеджер уведомлений для администратора"""

    def __init__(self):
        self.last_error_time = {}
        self.error_cooldown = 300  # 5 минут между одинаковыми ошибками
        self.sms_threshold_warning_sent = False

    async def notify_error(self, error_type: str, message: str, details: Optional[str] = None):
        """Отправить уведомление об ошибке с cooldown"""
        current_time = datetime.now(timezone.utc)

        # Проверяем cooldown
        if error_type in self.last_error_time:
            time_diff = current_time - self.last_error_time[error_type]
            if time_diff < timedelta(seconds=self.error_cooldown):
                logging.debug(f"Пропускаем уведомление об ошибке {error_type} из-за cooldown")
                return

        self.last_error_time[error_type] = current_time

        emoji = self._get_error_emoji(error_type)
        full_message = (f"{emoji} Ошибка системы\n\n"
                       f"Тип: {error_type}\n"
                       f"Сообщение: {message}\n"
                       f"Время: {current_time.strftime('%d.%m.%Y %H:%M:%S')}")

        if details:
            full_message += f"\n\nДетали:\n{details}"

        await self._send_notification(full_message, disable_notification=False)
        logging.error(f"Отправлено уведомление об ошибке: {error_type}")

    async def notify_sms_failure(self, channel: int, recipient: str, error: str):
        """Уведомление о неудачной отправке SMS"""
        message = ("❌ Ошибка отправки SMS\n\n"
                  f"📱 Получатель: {recipient}\n"
                  f"📡 Канал: {channel}\n"
                  f"❌ Ошибка: {error}\n"
                  f"⏰ Время: {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M:%S')}")

        await self._send_notification(message)
        logging.warning(f"SMS отправка не удалась: канал {channel}, получатель {recipient}")

    async def notify_high_load(self, channel: int, sms_count: int, time_window: str):
        """Уведомление о высокой нагрузке на канал"""
        message = ("⚠️ Высокая нагрузка на канал\n\n"
                  f"📡 Канал: {channel}\n"
                  f"📨 SMS за {time_window}: {sms_count}\n"
                  f"⏰ Время: {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M:%S')}\n\n"
                  "Рекомендуется проверить:\n"
                  "• Нагрузку на канал\n"
                  "• Доступность SIM-карты\n"
                  "• Баланс оператора")

        await self._send_notification(message)

    async def notify_system_status(self, status_type: str, details: Dict[str, Any]):
        """Уведомление о системном статусе"""
        emoji = "🟢" if status_type == "recovery" else "🔴"
        status_text = "восстановление" if status_type == "recovery" else "проблема"

        message = f"{emoji} Системное уведомление: {status_text}\n\n"

        for key, value in details.items():
            message += f"{key}: {value}\n"

        message += f"\n⏰ Время: {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M:%S')}"

        await self._send_notification(message)

    async def check_sms_thresholds(self):
        """Проверить пороги SMS и отправить предупреждения (по количеству за последний час)."""
        try:
            _, db_session = await create_db_session_pool()
            async with db_session() as session:
                stats = await get_sms_stats(session)
                # Используем статистику именно за последний час, а не за всё время
                channels_hourly = stats.get("channels_last_hour") or stats["channels"]

                high_threshold = 100
                total_hourly = sum(channels_hourly.values())

                if total_hourly > high_threshold and not self.sms_threshold_warning_sent:
                    await self.notify_high_load(0, total_hourly, "последний час")
                    self.sms_threshold_warning_sent = True
                elif total_hourly < high_threshold * 0.8:  # Сброс предупреждения при снижении
                    self.sms_threshold_warning_sent = False

                # Проверка отдельных каналов (только по данным за последний час)
                for channel, count in channels_hourly.items():
                    if count > high_threshold // 2:  # 50 SMS за час на канал
                        await self.notify_high_load(channel, count, "последний час")

        except Exception as e:
            logging.error(f"Ошибка проверки порогов SMS: {e}")

    async def notify_daily_report(self):
        """Ежедневный отчет"""
        try:
            _, db_session = await create_db_session_pool()
            async with db_session() as session:
                stats = await get_sms_stats(session)

                message = ("📊 Ежедневный отчет\n\n"
                          "📈 Статистика за день:\n"
                          f"📨 Всего SMS: {stats['today']}\n"
                          f"📥 Входящих: {stats['incoming']}\n"
                          f"📤 Исходящих: {stats['outgoing']}\n\n")

                if stats['channels']:
                    message += "📡 По каналам:\n"
                    for channel in range(1, 9):
                        count = stats['channels'].get(channel, 0)
                        if count > 0:
                            message += f"Канал {channel}: {count} SMS\n"

                message += f"\n⏰ Отчет на {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')}"

                await self._send_notification(message)

        except Exception as e:
            logging.error(f"Ошибка формирования ежедневного отчета: {e}")

    async def _send_notification(self, message: str, disable_notification: bool = True):
        """Отправить уведомление админу"""
        try:
            await bot.send_message(
                config.ADMIN_ID,
                message,
                disable_notification=disable_notification
            )
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление админу: {e}")

    def _get_error_emoji(self, error_type: str) -> str:
        """Получить emoji для типа ошибки"""
        emoji_map = {
            "database": "🗄️",
            "network": "🌐",
            "sms_send": "📱",
            "goip": "📡",
            "system": "⚙️",
            "auth": "🔐",
        }
        return emoji_map.get(error_type.lower(), "❌")


# Глобальный экземпляр менеджера уведомлений
notification_manager = NotificationManager()


async def start_notification_scheduler():
    """Запуск планировщика уведомлений"""
    while True:
        try:
            current_time = datetime.now(timezone.utc)

            # Проверка порогов SMS каждый час
            if current_time.minute == 0:
                await notification_manager.check_sms_thresholds()

            # Ежедневный отчет в 9:00
            if current_time.hour == 9 and current_time.minute == 0:
                await notification_manager.notify_daily_report()

            await asyncio.sleep(60)  # Проверка каждую минуту

        except Exception as e:
            logging.error(f"Ошибка в планировщике уведомлений: {e}")
            await asyncio.sleep(300)  # При ошибке ждем 5 минут
