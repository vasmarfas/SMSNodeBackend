"""Административные команды бота (назначение SIM пользователям)."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.db.models import User, SimCard, Gateway, RoleEnum
from config_reader import config

router = Router()


async def _is_admin(telegram_id: int) -> bool:
    return telegram_id == config.ADMIN_ID


# /assign_number user_telegram_id phone_number port_number
# Назначить SIM-карту пользователю. Если порт не существует — создаётся.

@router.message(Command("assign_number"))
async def assign_number(message: Message, session: AsyncSession):
    if not await _is_admin(message.from_user.id):
        await message.reply("⛔ У вас нет прав администратора.")
        return

    try:
        command_parts = message.text.split()
        if len(command_parts) != 4:
            await message.reply(
                "Формат: /assign_number <telegram_id> <номер_телефона> <номер_порта>\n"
                "Пример: /assign_number 123456789 +79001234567 1"
            )
            return

        _, user_tid, phone_number, port_str = command_parts
        user_tid = int(user_tid)
        port_number = int(port_str)

        if port_number < 1 or port_number > config.MAX_CHANNELS:
            await message.reply(f"Номер порта должен быть от 1 до {config.MAX_CHANNELS}.")
            return

        # Находим пользователя
        r = await session.execute(select(User).where(User.telegram_id == user_tid))
        user = r.scalar_one_or_none()
        if not user:
            await message.reply(
                f"Пользователь с Telegram ID {user_tid} не найден.\n"
                "Он должен сначала написать боту /start."
            )
            return

        # Ищем существующую SimCard на этом порту
        r2 = await session.execute(
            select(SimCard).where(SimCard.port_number == port_number)
        )
        sim = r2.scalar_one_or_none()

        if sim:
            old_user_id = sim.assigned_user_id
            sim.phone_number = phone_number
            sim.assigned_user_id = user.id
            if old_user_id and old_user_id != user.id:
                await message.reply(
                    f"⚠️ Порт {port_number} был ранее назначен другому пользователю — переназначен."
                )
        else:
            # Создаём новую SIM-карту, привязывая к первому активному шлюзу
            r3 = await session.execute(
                select(Gateway).where(Gateway.is_active == True).order_by(Gateway.id).limit(1)
            )
            gw = r3.scalar_one_or_none()
            if not gw:
                await message.reply(
                    "❌ Нет активных шлюзов.\n"
                    "Сначала добавьте шлюз через /gateways или /admin."
                )
                return

            sim = SimCard(
                gateway_id=gw.id,
                port_number=port_number,
                phone_number=phone_number,
                assigned_user_id=user.id,
            )
            session.add(sim)

        await session.commit()
        await message.reply(
            f"✅ Номер {phone_number} (порт {port_number}) назначен пользователю "
            f"@{user.username or user_tid}."
        )

    except ValueError:
        await message.reply("Формат: /assign_number <telegram_id> <номер_телефона> <порт>")


# /revoke_number user_telegram_id
# Отозвать SIM-карту у пользователя (assigned_user_id → None)

@router.message(Command("revoke_number"))
async def revoke_number(message: Message, session: AsyncSession):
    if not await _is_admin(message.from_user.id):
        await message.reply("⛔ У вас нет прав администратора.")
        return

    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("Формат: /revoke_number <telegram_id>")
            return

        _, user_tid = parts
        user_tid = int(user_tid)

        r = await session.execute(select(User).where(User.telegram_id == user_tid))
        user = r.scalar_one_or_none()
        if not user:
            await message.reply(f"Пользователь {user_tid} не найден.")
            return

        r2 = await session.execute(
            select(SimCard)
            .options(selectinload(SimCard.gateway))
            .where(SimCard.assigned_user_id == user.id)
        )
        sims = r2.scalars().all()

        if not sims:
            await message.reply("У пользователя нет назначенных SIM-карт.")
            return

        for sim in sims:
            sim.assigned_user_id = None

        await session.commit()

        phones = ", ".join(s.phone_number or f"порт {s.port_number}" for s in sims)
        await message.reply(f"✅ SIM-карты отозваны у пользователя @{user.username}: {phones}")

    except ValueError:
        await message.reply("Формат: /revoke_number <telegram_id>")
