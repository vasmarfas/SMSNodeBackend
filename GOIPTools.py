# SMTP Handler for receiving messages from GOIP
import base64
import email
import logging
import re
import aiohttp
import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from database import User, create_db_session_pool, log_sms, SMSType
from core.db.models import SimCard, MessageStatusEnum
from bot import bot
from config_reader import config
from notification_manager import notification_manager


class GOIPSMTPHandler:
    def __init__(self):
        # Основной формат: SN:xxx Channel:x time,sender,text
        self.regexp = re.compile(
            r"SN:(?P<sn>\S+)\s+Channel:(?P<channel>\d+)\s+(?P<time>\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(?P<sender>[^,]+),(?P<text>.+)"
        )
        # Альтернативный формат: SN:xxx Channel:x Sender:time,sender,text
        self.regexp_alt = re.compile(
            r"SN:(?P<sn>\S+)\s+Channel:(?P<channel>\d+)\s+Sender:(?P<time>\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(?P<sender>[^,]+),(?P<text>.+)"
        )
        self.db_session = None
        self.loop = None
        
    async def setup_db_session(self, session):
        """
        Настройка сессии базы данных
        """
        self.db_session = session
    
    def _run_in_background(self, coro):
        """
        Run a coroutine in a background task
        """
        if not self.loop:
            # Fallback in case the loop wasn't set
            self.loop = asyncio.get_event_loop()
            
        try:
            # Run the coroutine in the loop
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
            return future
        except RuntimeError as e:
            logging.error(f"Error running coroutine in background: {str(e)}")
            # If we can't run in the loop, try to create a task directly
            try:
                task = asyncio.create_task(coro)
                return task
            except Exception as e2:
                logging.error(f"Failed to create task: {str(e2)}")
                return None
    
    async def _process_message(self, result, channel_number):
        """
        Process the message and send notifications in a proper task
        """
        try:
            # Создаем сессию для работы с БД
            _, db_session = await create_db_session_pool()
            
            # Ищем SIM-карту, привязанную к этому порту (channel → port_number)
            async with db_session() as session:
                phone_result = await session.execute(
                    select(SimCard).options(selectinload(SimCard.assigned_user)).where(
                        SimCard.port_number == channel_number
                    )
                )
                phone_obj = phone_result.scalar_one_or_none()

                # Формируем сообщение
                message = (f"{result['text']}\n\n"
                           f"Отправитель: {result['sender']} \n"
                           f"На канал: {result['channel']}")

                if phone_obj:
                    message += f" (номер: {phone_obj.phone_number})"

                message += f"\nВремя: {result['time']}"
                
                # Логируем входящее сообщение
                logging.info(message)

                # Логируем SMS в базу данных
                try:
                    await log_sms(
                        session=session,
                        sms_type=SMSType.INCOMING,
                        message_text=result['text'],
                        channel=channel_number,
                        sender=result['sender'],
                        status="received"
                    )
                except Exception as e:
                    logging.error(f"Error logging SMS to database: {e}")
                
                # Определяем, нужно ли отключать уведомления
                is_silent = result['sender'] == "RSCHS"

                if config.RSCHS_CHAT_ID != 0 and is_silent:
                    try:
                        await bot.send_message(config.RSCHS_CHAT_ID, message, disable_notification=is_silent)
                    except Exception as e:
                        message += f"\n Error while sending to RSCHS{e}"
                        await bot.send_message(config.ADMIN_ID, message, disable_notification=is_silent)
                else:
                    await bot.send_message(config.ADMIN_ID, message, disable_notification=is_silent)

                # Если SIM-карта привязана к пользователю, отправляем и ему
                if phone_obj and phone_obj.assigned_user and phone_obj.assigned_user.telegram_id != config.ADMIN_ID:
                    await bot.send_message(
                        phone_obj.assigned_user.telegram_id,
                        message,
                        disable_notification=is_silent
                    )
                    logging.info(f"Message forwarded to user {phone_obj.assigned_user.telegram_id} for channel {channel_number}")
                    
        except Exception as e:
            error_msg = f"Error processing incoming message: {str(e)}"
            logging.error(error_msg)
            try:
                await bot.send_message(config.ADMIN_ID, error_msg)
            except Exception as e2:
                logging.error(f"Failed to send error message to admin: {str(e2)}")
                
    async def handle_DATA(self, server, session, envelope):
        logging.info("Got a message from goip")
        msg = envelope.content.decode()
        text_b64 = email.message_from_string(msg).get_payload()
        text = base64.b64decode(text_b64).decode()
        
        # Пробуем основной формат
        match = self.regexp.match(text)
        
        # Если не подошел, пробуем альтернативный формат с "Sender:"
        if match is None:
            match = self.regexp_alt.match(text)
        
        # Если оба формата не подошли, отправляем ошибку
        if match is None:
            logging.error(f"Regex did not match text: {text}")  # Логируем ошибку
            # Запускаем в отдельной задаче
            self._run_in_background(bot.send_message(
                config.ADMIN_ID, 
                f"Error processing incoming message\n\n{text}"
            ))
            logging.error(f"Error processing incoming message\n\n{text}")
            return '250 OK'

        result = match.groupdict()  # Словарь с данными
        
        # Получаем номер канала из входящего сообщения
        channel_number = int(result['channel'])
        
        # Запускаем обработку сообщения в отдельной задаче
        self._run_in_background(self._process_message(result, channel_number))

        return '250 OK'


# Function to send SMS via GOIP
async def send_sms_via_goip(channel, phone_number, message_text, session=None, user_id=None,
                             gateway_id: int = None):
    """
    Отправить SMS через GSM-шлюз.

    Args:
        channel (int): Номер канала/порта шлюза.
        phone_number (str): Номер телефона получателя.
        message_text (str): Текст сообщения.
        session (AsyncSession, optional): Сессия БД для логирования.
        user_id (int, optional): ID пользователя (для лога).
        gateway_id (int, optional): ID шлюза из GatewayService.
                                    Если None — используется первый доступный.

    Returns:
        str: Статус отправки.
    """
    from gateway_service import gateway_service

    # Логируем отправку SMS в базу данных
    sms_log = None
    if session:
        try:
            sms_log = await log_sms(
                session=session,
                sms_type=SMSType.OUTGOING,
                message_text=message_text,
                channel=int(channel),
                recipient=phone_number,
                user_id=user_id,
                status="sending"
            )
        except Exception as e:
            logging.error(f"Error logging outgoing SMS to database: {e}")

    try:
        if gateway_id is not None:
            result = await gateway_service.send_sms(
                gateway_id=gateway_id, phone=phone_number, text=message_text, port_num=int(channel)
            )
        else:
            result = await gateway_service.send_sms_auto(phone=phone_number, text=message_text)

        if result.success:
            logging.info(f"SMS sent: channel={channel}, to={phone_number}, msg={message_text!r}")
            if sms_log and session:
                try:
                    sms_log.status = MessageStatusEnum.SENT_OK
                    await session.commit()
                except Exception as e:
                    logging.error(f"Error updating SMS log status: {e}")
            return f"OK: {result.message}"
        else:
            error_msg = f"Ошибка отправки: {result.message}"
            logging.error(error_msg)
            await notification_manager.notify_sms_failure(channel, phone_number, error_msg)
            if sms_log and session:
                try:
                    sms_log.status = MessageStatusEnum.FAILED
                    await session.commit()
                except Exception as e:
                    logging.error(f"Error updating SMS log status: {e}")
            return error_msg

    except Exception as e:
        error_msg = f"Error sending SMS: {str(e)}"
        logging.error(error_msg)
        await notification_manager.notify_sms_failure(channel, phone_number, error_msg)
        if sms_log and session:
            try:
                sms_log.status = MessageStatusEnum.FAILED
                await session.commit()
            except Exception as e2:
                logging.error(f"Error updating SMS log status: {e2}")
        return error_msg