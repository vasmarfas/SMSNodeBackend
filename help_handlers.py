from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from database import User
from config_reader import config

router = Router()

@router.message(Command("help"))
async def help_command(message: types.Message, session: AsyncSession):
    is_admin = message.from_user.id == config.ADMIN_ID
    
    # Создаем клавиатуру
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📱 Отправка SMS", callback_data="help_sms"),
            InlineKeyboardButton(text="👤 Мой номер", callback_data="help_number")
        ]
    ])
    
    if is_admin:
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(text="🔐 Админ-панель", callback_data="help_admin")
        ])
    
    await message.reply(
        "🤖 Помощь по использованию бота\n\n"
        "Основные команды:\n"
        "/start - Начать работу с ботом\n"
        "/help - Показать это сообщение\n"
        "/my_number - Показать ваш номер\n"
        "/send_sms - Отправить SMS\n\n"
        "Выберите раздел для получения подробной информации:",
        reply_markup=keyboard
    )

@router.callback_query(F.data == "help_sms")
async def help_sms(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "📱 Отправка SMS\n\n"
        "Формат команды:\n"
        "/send_sms номер_телефона текст_сообщения\n\n"
        "Пример:\n"
        "/send_sms +79123456789 Привет, мир!\n\n"
        "⚠️ Примечания:\n"
        "• Вы можете отправлять SMS только с вашего номера\n"
        "• Номер должен быть в международном формате\n"
        "• Максимальная длина сообщения - 160 символов\n"
        "• Сообщение отправляется через соответствующий канал шлюза GOIP"
    )

@router.callback_query(F.data == "help_number")
async def help_number(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "👤 Управление номером\n\n"
        "Команды:\n"
        "/my_number - Показать ваш текущий номер и канал\n\n"
        "⚠️ Примечания:\n"
        "• У вас может быть только один номер\n"
        "• Номер назначается администратором\n"
        f"• Каждый номер привязан к определенному каналу шлюза GOIP (1-{config.MAX_CHANNELS})\n"
        "• На каждом канале может быть только один номер\n"
        "• Вы не можете передавать номер другим пользователям"
    )

@router.callback_query(F.data == "help_admin")
async def help_admin(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "🔐 Админ-панель\n\n"
        "Команды:\n"
        "/admin - Открыть панель администратора\n"
        "/assign_number - Назначить номер пользователю\n"
        "/revoke_number - Отозвать номер у пользователя\n\n"
        "Формат команд:\n"
        "/assign_number user_id phone_number channel - Назначить номер пользователю\n"
        "/revoke_number user_id - Отозвать номер у пользователя\n\n"
        "⚠️ Примечания:\n"
        f"• Channel - номер канала шлюза GOIP (1-{config.MAX_CHANNELS})\n"
        "• На каждом канале может быть только один номер\n"
        "• Только администратор может управлять номерами\n"
        "• При отзыве номера он автоматически назначается администратору\n"
        "• Администратор может отправлять SMS с любого номера\n\n"
        f"🔧 Конфигурация настроена на {config.MAX_CHANNELS} каналов"
    )

@router.message()
async def unknown_command(message: types.Message):
    await message.reply(
        "❌ Неизвестная команда\n\n"
        "Используйте /help для получения списка доступных команд."
    ) 