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

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📱 Отправка SMS", callback_data="help_sms"),
            InlineKeyboardButton(text="👤 Мои номера", callback_data="help_number")
        ],
        [
            InlineKeyboardButton(text="📝 Шаблоны", callback_data="help_templates"),
            InlineKeyboardButton(text="👥 Контакты", callback_data="help_contacts")
        ],
        [
            InlineKeyboardButton(text="📋 История", callback_data="help_history"),
            InlineKeyboardButton(text="🌐 Mini App", callback_data="help_miniapp")
        ]
    ])

    if is_admin:
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(text="🔐 Админ-панель", callback_data="help_admin")
        ])

    await message.reply(
        "🤖 <b>SMS Node Help</b>\n\n"
        "📱 <b>Отправка SMS:</b>\n"
        "/my_numbers — посмотреть ваши доступные номера\n"
        "/send_sms &lt;номер&gt; &lt;текст&gt; — отправить SMS\n"
        "/history — история отправленных и полученных SMS\n\n"
        "👥 <b>Контакты и Группы:</b>\n"
        "/contacts — список контактов\n"
        "/groups — ваши группы контактов\n"
        "/mass_send &lt;группа&gt; | &lt;текст&gt; — массовая рассылка на группу\n\n"
        "🔐 <b>Аккаунт и Приложение:</b>\n"
        "/link — привязать этот Telegram к существующей учетной записи\n"
        "/set_password — установить/изменить пароль\n\n"
        "Выберите раздел для получения подробной информации:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@router.callback_query(F.data == "help_sms")
async def help_sms(callback: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="help_back")]
    ])
    
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
        "• Сообщение отправляется через соответствующий канал шлюза GOIP",
        reply_markup=keyboard
    )

@router.callback_query(F.data == "help_number")
async def help_number(callback: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="help_back")]
    ])
    
    await callback.message.edit_text(
        "👤 Управление номерами\n\n"
        "Команды:\n"
        "/my_numbers - Показать ваши текущие номера и шлюзы\n\n"
        "⚠️ Примечания:\n"
        "• У вас может быть несколько номеров для разных целей\n"
        "• Номера назначаются администратором\n"
        "• Каждый номер привязан к определенному каналу шлюза GOIP\n"
        "• Вы можете выбирать, с какого номера отправить SMS",
        reply_markup=keyboard
    )

@router.callback_query(F.data == "help_admin")
async def help_admin(callback: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="help_back")]
    ])
    
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
        f"🔧 Конфигурация настроена на {config.MAX_CHANNELS} каналов",
        reply_markup=keyboard
    )

@router.callback_query(F.data == "help_templates")
async def help_templates(callback: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="help_back")]
    ])

    await callback.message.edit_text(
        "📝 Шаблоны SMS\n\n"
        "Команды:\n"
        "/templates - Показать все доступные шаблоны\n"
        "/add_template - Создать новый шаблон\n"
        "/use_template <название> - Использовать шаблон\n\n"
        "Формат создания шаблона:\n"
        "/add_template название | текст | категория\n\n"
        "Категории:\n"
        "• general - Общие\n"
        "• business - Бизнес\n"
        "• personal - Личные\n"
        "• marketing - Маркетинг\n\n"
        "Примеры:\n"
        "/add_template Приветствие | Добрый день! | general\n"
        "/add_template Заказ | Ваш заказ готов | business\n\n"
        "⚠️ Примечания:\n"
        "• Шаблоны сохраняются для повторного использования\n"
        "• Администратор может создавать глобальные шаблоны\n"
        "• Максимальная длина текста - 160 символов",
        reply_markup=keyboard
    )

@router.callback_query(F.data == "help_contacts")
async def help_contacts(callback: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="help_back")]
    ])

    await callback.message.edit_text(
        "👥 Управление контактами\n\n"
        "Команды:\n"
        "/contacts - Показать все контакты\n"
        "/add_contact - Добавить новый контакт\n"
        "/send_to_contact <имя> <текст> - Отправить SMS контакту\n\n"
        "Формат добавления контакта:\n"
        "/add_contact имя_контакта | номер_телефона\n\n"
        "Примеры:\n"
        "/add_contact Иван Петров | +79001234567\n"
        "/send_to_contact Иван Петров Сообщение доставлено\n\n"
        "⚠️ Примечания:\n"
        "• Контакты доступны только вам\n"
        "• Номера должны быть в международном формате\n"
        "• Имена контактов должны быть уникальными",
        reply_markup=keyboard
    )

@router.callback_query(F.data == "help_history")
async def help_history(callback: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="help_back")]
    ])

    await callback.message.edit_text(
        "📋 История SMS\n\n"
        "Команды:\n"
        "/history - Показать последние 10 SMS\n\n"
        "⚠️ Примечания:\n"
        "• Показываются только ваши SMS\n"
        "• История включает входящие и исходящие сообщения\n"
        "• Сообщения сортируются по времени (новые сверху)\n"
        "• Для администратора доступна расширенная статистика в админ-панели",
        reply_markup=keyboard
    )

@router.callback_query(F.data == "help_miniapp")
async def help_miniapp(callback: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="help_back")]
    ])

    await callback.message.edit_text(
        "🌐 Веб-интерфейс (Telegram Mini App)\n\n"
        "Mini App предоставляет удобный веб-интерфейс для:\n\n"
        "📱 Основные функции:\n"
        "• Быстрая отправка SMS\n"
        "• Управление шаблонами\n"
        "• Работа с контактами\n"
        "• Просмотр истории\n"
        "• Админ-панель (для администратора)\n\n"
        "🚀 Преимущества:\n"
        "• Современный интерфейс\n"
        "• Быстрый поиск и фильтры\n"
        "• Удобное управление данными\n"
        "• Работает в Telegram Web и мобильном приложении\n\n"
        "📍 Как открыть:\n"
        "Mini App доступен через веб-интерфейс бота",
        reply_markup=keyboard
    )

@router.callback_query(F.data == "help_back")
async def help_back(callback: types.CallbackQuery, session: AsyncSession):
    """Возврат к главному меню помощи"""
    is_admin = callback.from_user.id == config.ADMIN_ID

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📱 Отправка SMS", callback_data="help_sms"),
            InlineKeyboardButton(text="👤 Мои номера", callback_data="help_number")
        ],
        [
            InlineKeyboardButton(text="📝 Шаблоны", callback_data="help_templates"),
            InlineKeyboardButton(text="👥 Контакты", callback_data="help_contacts")
        ],
        [
            InlineKeyboardButton(text="📋 История", callback_data="help_history"),
            InlineKeyboardButton(text="🌐 Mini App", callback_data="help_miniapp")
        ]
    ])

    if is_admin:
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(text="🔐 Админ-панель", callback_data="help_admin")
        ])

    await callback.message.edit_text(
        "🤖 <b>SMS Node Help</b>\n\n"
        "📱 <b>Отправка SMS:</b>\n"
        "/my_numbers — посмотреть ваши доступные номера\n"
        "/send_sms &lt;номер&gt; &lt;текст&gt; — отправить SMS\n"
        "/history — история отправленных и полученных SMS\n\n"
        "👥 <b>Контакты и Группы:</b>\n"
        "/contacts — список контактов\n"
        "/groups — ваши группы контактов\n"
        "/mass_send &lt;группа&gt; | &lt;текст&gt; — массовая рассылка на группу\n\n"
        "🔐 <b>Аккаунт и Приложение:</b>\n"
        "/link — привязать этот Telegram к существующей учетной записи\n"
        "/set_password — установить/изменить пароль\n\n"
        "Выберите раздел для получения подробной информации:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@router.message()
async def unknown_command(message: types.Message):
    await message.reply(
        "❌ Неизвестная команда\n\n"
        "Используйте /help для получения списка доступных команд."
    ) 