"""Административная панель Telegram-бота (инлайн-меню)."""

from datetime import datetime, timedelta, timezone
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types.input_file import BufferedInputFile
import csv
import io
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.db.models import (
    User, SimCard, Gateway,
    Message as SmsMessage,
    RoleEnum,
    MessageDirectionEnum,
)
from database import get_sms_stats
from goip_monitor import goip_monitor
from config_reader import config

router = Router()


async def _is_admin(telegram_id: int) -> bool:
    return telegram_id == config.ADMIN_ID


def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
            InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users"),
        ],
        [
            InlineKeyboardButton(text="📱 SIM-карты", callback_data="admin_numbers"),
            InlineKeyboardButton(text="📨 SMS логи", callback_data="admin_sms_logs"),
        ],
        [
            InlineKeyboardButton(text="📥 Выгрузить отчет (CSV)", callback_data="admin_export_stats"),
        ],
        [
            InlineKeyboardButton(text="📡 Шлюзы", callback_data="gw_list"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin_settings"),
        ],
    ])


@router.message(Command("admin"))
async def admin_panel(message: types.Message, session: AsyncSession):
    if not await _is_admin(message.from_user.id):
        await message.reply("⛔ У вас нет прав администратора.")
        return

    users_count = await session.scalar(select(func.count(User.id))) or 0
    sims_count = await session.scalar(select(func.count(SimCard.id))) or 0
    assigned_count = await session.scalar(
        select(func.count(SimCard.id)).where(SimCard.assigned_user_id.isnot(None))
    ) or 0

    gateways_result = await session.execute(select(Gateway).order_by(Gateway.id))
    gateways = gateways_result.scalars().all()
    
    channels_text = ""
    for gw in gateways:
        r = await session.execute(select(SimCard).where(SimCard.gateway_id == gw.id).order_by(SimCard.port_number))
        sims = r.scalars().all()
        channels_text += f"\n<b>Шлюз: {gw.name}</b>\n"
        if not sims:
            channels_text += "  Нет SIM-карт\n"
        for sim in sims:
            num = sim.phone_number if sim.phone_number else "Отсутствует"
            channels_text += f"  Порт {sim.port_number}. Номер: {num}\n"

    await message.reply(
        f"<b>Панель администратора</b>\n\n"
        f"<b>Статистика:</b>\n"
        f"Пользователей: {users_count}\n"
        f"SIM-карт всего: {sims_count}\n"
        f"SIM-карт назначено: {assigned_count}\n\n"
        f"<b>Сводка по шлюзам:</b>{channels_text}",
        reply_markup=_admin_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_stats")
async def show_stats(callback: types.CallbackQuery, session: AsyncSession):
    users_count = await session.scalar(select(func.count(User.id))) or 0
    sims_count = await session.scalar(select(func.count(SimCard.id))) or 0
    assigned_count = await session.scalar(
        select(func.count(SimCard.id)).where(SimCard.assigned_user_id.isnot(None))
    ) or 0

    goip_status = goip_monitor.get_status_summary()

    channels_stats = {}
    for port in range(1, config.MAX_CHANNELS + 1):
        r = await session.execute(
            select(SimCard.id).where(SimCard.port_number == port)
        )
        sim_ids = [row[0] for row in r.fetchall()]
        if sim_ids:
            count = await session.scalar(
                select(func.count(SmsMessage.id)).where(SmsMessage.sim_card_id.in_(sim_ids))
            ) or 0
        else:
            count = 0
        channels_stats[port] = count

    free_ports = config.MAX_CHANNELS - assigned_count
    channels_text = "\n".join(
        [f"  Порт {p}: {c} SMS" for p, c in channels_stats.items()]
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="admin_back")]
    ])

    await callback.message.edit_text(
        f"<b>Статистика системы</b>\n\n"
        f"Пользователей: {users_count}\n"
        f"SIM-карт: {sims_count} (назначено {assigned_count}, свободно {free_ports})\n\n"
        f"<b>SMS по портам:</b>\n{channels_text}\n\n"
        f"{goip_status}",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_export_stats")
async def admin_export_stats(callback: types.CallbackQuery, session: AsyncSession):
    """Выгрузка CSV-отчета по SMS (последние 7 дней)."""
    until = datetime.now(timezone.utc)
    since = until - timedelta(days=7)

    r = await session.execute(
        select(SmsMessage)
        .order_by(SmsMessage.created_at)
        .where(SmsMessage.created_at >= since)
        .limit(200000)
    )
    messages = r.scalars().all()

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["created_at", "sim_card_id", "external_phone", "direction", "status", "text_len", "error_text"])
    for m in messages:
        w.writerow(
            [
                (m.created_at.isoformat() if m.created_at else ""),
                m.sim_card_id or "",
                m.external_phone,
                (m.direction.value if m.direction else ""),
                (m.status.value if m.status else ""),
                len(m.text or ""),
                (m.error_text or ""),
            ]
        )

    data = out.getvalue().encode("utf-8-sig")
    await callback.message.answer_document(
        BufferedInputFile(data, filename="sms_stats.csv"),
        caption="📥 Отчет по SMS (CSV, последние 7 дней)",
    )
    await callback.answer("Готово")


@router.callback_query(F.data == "admin_users")
async def show_users(callback: types.CallbackQuery, session: AsyncSession):
    r = await session.execute(
        select(User).options(selectinload(User.sim_cards)).order_by(User.id)
    )
    users = r.scalars().all()

    text = "<b>Пользователи:</b>\n\n"
    for user in users:
        role_icon = "👑" if user.role == RoleEnum.ADMIN else "👤"
        status = "активен" if user.is_active else "заблокирован"
        tg = f"@{user.username}" if user.username else f"ID:{user.telegram_id}"

        if user.sim_cards:
            sims_info = ", ".join(
                f"{s.phone_number or '?'}(порт {s.port_number})" for s in user.sim_cards
            )
        else:
            sims_info = "нет SIM-карт"

        text += f"{role_icon} {tg} [{status}]\n   {sims_info}\n\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="admin_back")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_numbers")
async def show_numbers(callback: types.CallbackQuery, session: AsyncSession):
    r = await session.execute(
        select(SimCard)
        .options(selectinload(SimCard.assigned_user), selectinload(SimCard.gateway))
        .order_by(SimCard.port_number)
    )
    sims = r.scalars().all()

    text = "<b>SIM-карты (порты шлюзов):</b>\n\n"
    if not sims:
        text += "Нет зарегистрированных SIM-карт.\n"
        text += "Назначьте номер через /assign_number"
    else:
        for sim in sims:
            gw = sim.gateway.name if sim.gateway else "?"
            phone = sim.phone_number or "номер не задан"
            owner = f"@{sim.assigned_user.username}" if sim.assigned_user else "свободна"
            text += f"Порт {sim.port_number} | {phone}\n   Шлюз: {gw} | Пользователь: {owner}\n\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="admin_back")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_sms_logs")
async def show_sms_logs(callback: types.CallbackQuery, session: AsyncSession):
    stats = await get_sms_stats(session)

    r = await session.execute(
        select(SmsMessage)
        .options(selectinload(SmsMessage.sim_card))
        .order_by(SmsMessage.created_at.desc())
        .limit(10)
    )
    recent = r.scalars().all()

    text = "<b>SMS логи</b>\n\n"
    text += (
        f"Всего: {stats['total']} | "
        f"Входящих: {stats['incoming']} | "
        f"Исходящих: {stats['outgoing']} | "
        f"Сегодня: {stats['today']}\n\n"
    )

    if recent:
        text += "<b>Последние 10:</b>\n\n"
        for sms in recent:
            icon = "📥" if sms.direction == MessageDirectionEnum.INCOMING else "📤"
            time_str = sms.created_at.strftime("%d.%m %H:%M") if sms.created_at else ""
            port_info = f"[порт {sms.sim_card.port_number}]" if sms.sim_card else ""
            short_text = sms.text[:50] + ("..." if len(sms.text) > 50 else "")
            text += f"{icon} {time_str} {sms.external_phone} {port_info}\n   {short_text}\n\n"
    else:
        text += "История SMS пуста.\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Статистика по портам", callback_data="admin_sms_stats")],
        [InlineKeyboardButton(text="Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_sms_stats")
async def sms_detailed_stats(callback: types.CallbackQuery, session: AsyncSession):
    stats = await get_sms_stats(session)

    text = "<b>Статистика SMS по портам:</b>\n\n"
    for port in range(1, 9):
        count = stats["channels"].get(port, 0)
        icon = "📶" if count > 0 else "⬜"
        text += f"{icon} Порт {port}: {count} SMS\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад к логам", callback_data="admin_sms_logs")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_settings")
async def show_settings(callback: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Проверить шлюзы", callback_data="admin_check_goip")],
        [InlineKeyboardButton(text="Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(
        f"<b>Настройки</b>\n\n"
        f"Максимум каналов: {config.MAX_CHANNELS}\n"
        f"GOIP IP (default): {config.GOIP_IP}\n\n"
        f"<b>Команды:</b>\n"
        f"/assign_number — назначить SIM пользователю\n"
        f"/revoke_number — отозвать SIM\n"
        f"/gateways — управление шлюзами",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_check_goip")
async def check_goip_status(callback: types.CallbackQuery):
    await goip_monitor.check_goip_status()
    status_text = goip_monitor.get_status_summary()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обновить", callback_data="admin_check_goip")],
        [InlineKeyboardButton(text="Назад", callback_data="admin_settings")],
    ])
    await callback.message.edit_text(
        f"<b>Статус шлюзов</b>\n\n{status_text}",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery, session: AsyncSession):
    users_count = await session.scalar(select(func.count(User.id))) or 0
    sims_count = await session.scalar(select(func.count(SimCard.id))) or 0
    assigned_count = await session.scalar(
        select(func.count(SimCard.id)).where(SimCard.assigned_user_id.isnot(None))
    ) or 0

    channels_stats = {}
    for port in range(1, config.MAX_CHANNELS + 1):
        r = await session.execute(select(SimCard).where(SimCard.port_number == port))
        sim = r.scalar_one_or_none()
        channels_stats[port] = sim.phone_number if sim and sim.phone_number else "Отсутствует"

    channels_text = "\n".join([f"  Порт {p}. Номер: {n}" for p, n in channels_stats.items()])

    await callback.message.edit_text(
        f"<b>Панель администратора</b>\n\n"
        f"Пользователей: {users_count}\n"
        f"SIM-карт: {sims_count} (назначено {assigned_count})\n\n"
        f"<b>Порты шлюза:</b>\n{channels_text}",
        reply_markup=_admin_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()
