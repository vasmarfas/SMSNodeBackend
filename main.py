"""
Точка входа SMSNodeBackend.

Инициализация БД, загрузка шлюзов, очередь отправки SMS, приём входящих (UDP/SMTP),
мониторинг, Telegram-бот и HTTP API (FastAPI).
"""

import asyncio
import logging
import os
import sys
import uvicorn
import aiosmtpd.controller
from datetime import datetime, timezone

from message_handlers import admin_messages, help_messages, user_messages, admin_panel_messages
from message_handlers import gateway_handlers
from GOIPTools import GOIPSMTPHandler
from goip_monitor import start_goip_monitoring
from goip_sms_receiver import start_goip_udp_receiver
from notification_manager import start_notification_scheduler
from bot import dp, bot
from config_reader import config
from database import init_db, create_db_session_pool, DbSessionMiddleware, init_db_functions
from core.db.models import (
    Gateway, SimCard, Message as SmsMessage,
    MessageDirectionEnum, MessageStatusEnum,
    GoIPEvent, GoIPEventTypeEnum,
)
from core.db.database import AsyncSessionLocal
from gateway_service import gateway_service
from sms_queue import start_sms_worker, stop_sms_worker
from sqlalchemy import select


def setup_logger():
    os.makedirs("logs", exist_ok=True)
    log_filename = f"logs/log_{datetime.now(timezone.utc).strftime('%d-%m-%Y_%H-%M-%S')}.log"
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    file_handler = logging.FileHandler(log_filename, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    # Явно UTF-8 на Windows — без этого эмодзи вызывают UnicodeEncodeError (cp1251)
    console_handler = logging.StreamHandler(
        stream=open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1, closefd=False)
    )
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    logging.info("Логгер настроен. Логи: папка logs + консоль (UTF-8).")


setup_logger()


async def on_goip_sms_received(recvid: str, gw_id: str, src_phone: str, text: str, addr: tuple):
    """
    Обработчик входящего SMS от GoIP UDP: запись в БД, уведомления в Telegram.

    gw_id — идентификатор из пакета RECEIVE (многоканальный GoIP: 1001, 1002, …).
    Привязка к SimCard: шлюз по host, канал по port_number = int(gw_id) - 1000.
    """
    logging.info(f"Входящее SMS: от={src_phone}, шлюз/канал={gw_id}, текст={text!r}")

    sim_card_id = None
    assigned_user = None
    
    gw_name_str = gw_id
    channel_str = gw_id
    sim_phone_str = ""

    try:
        async with AsyncSessionLocal() as session:
            r_gw = await session.execute(select(Gateway).where(Gateway.username == gw_id))
            gw = r_gw.scalar_one_or_none()
            if not gw:
                r_host = await session.execute(select(Gateway).where(Gateway.host == addr[0]))
                gw = r_host.scalars().first()

            if gw:
                gw_name_str = gw.name or gw.username or gw_id
                port_number = None
                try:
                    gid = int(gw_id)
                    if 1001 <= gid <= 1008:
                        port_number = gid - 1000
                except ValueError:
                    pass

                if port_number is not None:
                    channel_str = str(port_number)
                else:
                    channel_str = gw_id

                if port_number is not None:
                    r_sim = await session.execute(
                        select(SimCard).where(
                            SimCard.gateway_id == gw.id,
                            SimCard.port_number == port_number,
                        )
                    )
                    sim = r_sim.scalar_one_or_none()
                else:
                    sim = None
                if not sim:
                    r_sim = await session.execute(
                        select(SimCard).where(SimCard.gateway_id == gw.id).order_by(SimCard.port_number)
                    )
                    sim = r_sim.scalars().first()
                if sim:
                    sim_card_id = sim.id
                    if sim.phone_number:
                        sim_phone_str = f"\nНомер: {sim.phone_number}"
                    if sim.assigned_user_id:
                        from core.db.models import User
                        assigned_user = await session.get(User, sim.assigned_user_id)

            msg = SmsMessage(
                sim_card_id=sim_card_id,
                external_phone=src_phone,
                direction=MessageDirectionEnum.INCOMING,
                text=text,
                status=MessageStatusEnum.RECEIVED,
            )
            session.add(msg)
            await session.commit()
            await session.refresh(msg)

            if assigned_user:
                from core.db.models import IncomingRule, IncomingRuleActionEnum
                rules = await session.execute(
                    select(IncomingRule).where(
                        IncomingRule.user_id == assigned_user.id,
                        IncomingRule.is_active == True
                    )
                )
                for rule in rules.scalars().all():
                    if rule.keyword and rule.keyword.lower() not in text.lower():
                        continue
                    
                    if rule.action_type == IncomingRuleActionEnum.WEBHOOK:
                        import httpx
                        import asyncio
                        async def send_webhook(url: str, payload: dict):
                            try:
                                async with httpx.AsyncClient(timeout=5.0) as client:
                                    await client.post(url, json=payload)
                            except Exception as e:
                                logging.error(f"Webhook error: {e}")
                        
                        payload = {"from": src_phone, "text": text, "sim_card_id": sim_card_id}
                        asyncio.create_task(send_webhook(rule.target_data, payload))

                    elif rule.action_type == IncomingRuleActionEnum.AUTOREPLY:
                        reply_msg = SmsMessage(
                            sim_card_id=sim_card_id,
                            external_phone=src_phone,
                            direction=MessageDirectionEnum.OUTGOING,
                            text=rule.target_data,
                            status=MessageStatusEnum.PENDING,
                        )
                        session.add(reply_msg)
                        await session.commit()
                        await session.refresh(reply_msg)

                        from sms_queue import get_queue
                        q = get_queue()
                        if q:
                            await q.put(reply_msg.id)

    except Exception as e:
        logging.error(f"Ошибка записи входящего SMS в PostgreSQL: {e}")

    notify_text = (
        f"<b>Входящее SMS</b>\n"
        f"От: <code>{src_phone}</code>\n"
        f"Шлюз: {gw_name_str}\n"
        f"Канал: {channel_str}{sim_phone_str}\n\n"
        f"{text}"
    )

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    reply_kb = None
    if sim_card_id:
        reply_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Ответить",
                callback_data=f"reply:{sim_card_id}:{src_phone}",
            )
        ]])

    if assigned_user and assigned_user.telegram_id:
        try:
            should_send = (
                    src_phone.upper() != "RSCHS"
                    or config.IS_NEED_TO_SEND_RSCHS_MESSAGES_TO_USER
            )

            if should_send:
                await bot.send_message(
                    assigned_user.telegram_id,
                    notify_text,
                    parse_mode="HTML",
                    reply_markup=reply_kb,
                )

        except Exception as e:
            logging.error(f"Не удалось уведомить пользователя {assigned_user.telegram_id}: {e}")

    try:
        admin_text = notify_text
        if assigned_user and assigned_user.telegram_id != config.ADMIN_ID:
            admin_text += f"\n\n(Уведомлён: @{assigned_user.username or assigned_user.telegram_id})"
        await bot.send_message(config.ADMIN_ID, admin_text, parse_mode="HTML", reply_markup=reply_kb)
    except Exception as e:
        logging.error(f"Не удалось уведомить администратора: {e}")

    if config.RSCHS_CHAT_ID != 0 and src_phone.upper() == "RSCHS":
        try:
            await bot.send_message(config.RSCHS_CHAT_ID, notify_text, parse_mode="HTML", disable_notification=True)
        except Exception as e:
            logging.error(f"Не удалось уведомить RSCHS: {e}")


async def on_goip_event(event_type: str, gw_id: str, payload: dict, addr: tuple):
    """
    Сохранить push-события GoIP в goip_events; при keepalive — обновить номер/статус в SimCard, если запись уже есть.
    """
    enum_map = {
        "keepalive": GoIPEventTypeEnum.KEEPALIVE,
        "state": GoIPEventTypeEnum.STATE,
        "record": GoIPEventTypeEnum.RECORD,
        "deliver": GoIPEventTypeEnum.RECORD,
        "remain": GoIPEventTypeEnum.REMAIN,
        "cells": GoIPEventTypeEnum.CELLS,
        "receive": GoIPEventTypeEnum.RECEIVE,
    }
    evt = enum_map.get(event_type)
    if not evt:
        return
    try:
        async with AsyncSessionLocal() as session:
            session.add(
                GoIPEvent(
                    goip_id=gw_id,
                    host=addr[0],
                    port=addr[1],
                    event_type=evt,
                    payload_json=payload,
                )
            )
            await session.commit()

            if event_type == "keepalive" and gw_id.isdigit():
                port_num = int(gw_id)
                if 1001 <= port_num <= 1008:
                    port_number = port_num - 1000
                    from sqlalchemy import select, update
                    from core.db.models import Gateway, SimCard
                    r = await session.execute(
                        select(Gateway.id).where(Gateway.host == addr[0]).limit(1)
                    )
                    gw_row = r.scalars().first()
                    if gw_row is not None:
                        gateway_id = gw_row
                        upd = (
                            update(SimCard)
                            .where(
                                SimCard.gateway_id == gateway_id,
                                SimCard.port_number == port_number,
                            )
                            .values(
                                phone_number=payload.get("num") or None,
                                status=(payload.get("gsm_status") or "UNKNOWN")[:50],
                            )
                        )
                        await session.execute(upd)
                        await session.commit()
    except Exception as e:
        logging.error(f"Ошибка сохранения goip_event ({event_type}): {e}")


async def main():
    logging.info("Starting service...")

    _, db_session_pool = await create_db_session_pool()
    dp.update.middleware(DbSessionMiddleware(session_pool=db_session_pool))
    await init_db()          # Создаёт таблицы PostgreSQL
    await init_db_functions()
    logging.info("PostgreSQL: таблицы готовы.")

    if config.IS_DEMO:
        logging.info("DEMO-режим включен. Генерация демо-данных...")
        try:
            from seed_fake_data import seed_demo_data
            await seed_demo_data()
        except Exception as e:
            logging.error(f"Ошибка при генерации демо-данных: {e}")

    async with AsyncSessionLocal() as session:
        gw_count = await gateway_service.load_from_db(session)

    if gw_count == 0:
        logging.warning("GatewayService: шлюзы не найдены. Добавь через /gateways в боте.")
    else:
        logging.info(f"GatewayService: загружено {gw_count} шлюз(ов).")

    start_sms_worker()
    logging.info("SMS-очередь запущена.")

    udp_transport = None
    try:
        udp_transport = await start_goip_udp_receiver(
            host=config.GOIP_LISTEN_HOST,
            port=config.GOIP_LISTEN_PORT,
            on_sms_received=on_goip_sms_received,
            on_event=on_goip_event,
        )
        logging.info(
            f"GoIP UDP SMS-приёмник: {config.GOIP_LISTEN_HOST}:{config.GOIP_LISTEN_PORT}. "
            f"Настрой GoIP: SMS Server = <IP этой машины>, Port = {config.GOIP_LISTEN_PORT}"
        )
    except Exception as e:
        logging.error(f"Не удалось запустить GoIP UDP-приёмник: {e}")

    goip_smtp_server = None
    try:
        goip_handler = GOIPSMTPHandler()
        goip_smtp_server = aiosmtpd.controller.Controller(
            goip_handler,
            hostname=config.SMTP_HOST,
            port=config.SMTP_PORT,
        )
        goip_smtp_server.start()
        logging.info(
            f"SMTP-сервер GoIP: {config.SMTP_HOST}:{config.SMTP_PORT}"
        )
    except Exception as e:
        logging.warning(
            f"SMTP-сервер GoIP не запустился ({e}). SMS через SMTP не принимаются."
        )

    await start_goip_monitoring()

    asyncio.create_task(start_notification_scheduler())

    from core.api.app import app as api_app

    dp.include_routers(
        gateway_handlers.router,
        admin_messages.router,
        admin_panel_messages.router,
        user_messages.router,
        help_messages.router,
    )

    async def run_bot():
        try:
            logging.info("Start polling")
            await dp.start_polling(bot)
        except Exception as e:
            logging.error(f"Bot polling failed: {e}")

    async def run_api():
        cfg = uvicorn.Config(api_app, host=config.API_HOST, port=config.API_PORT, log_level="info")
        server = uvicorn.Server(cfg)
        await server.serve()

    try:
        await asyncio.gather(run_bot(), run_api())
    finally:
        stop_sms_worker()
        if udp_transport:
            udp_transport.close()
        if goip_smtp_server:
            goip_smtp_server.stop()


if __name__ == "__main__":
    asyncio.run(main())
