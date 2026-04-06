"""
Обработчики Telegram-бота: пользовательские команды и FSM.

/start, /link, /set_password, /my_numbers, /send_sms, /history, /contacts,
ответ на входящие SMS по inline-кнопке.
"""

import logging
import csv
import io
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.types.input_file import BufferedInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.db.models import (
    User, SimCard, Contact, ContactGroup, SMSTemplate,
    Message as SmsMessage,
    RoleEnum,
    PendingRegistration,
    PendingRegistrationSource,
    MessageDirectionEnum,
    MessageStatusEnum,
)
from core.registration import get_registration_mode
from core.api.auth import verify_password, get_password_hash
from config_reader import config
from gateway_service import gateway_service
from sms_queue import enqueue_sms

logger = logging.getLogger(__name__)
router = Router()


class ReplyForm(StatesGroup):
    """Состояния FSM для ответа на входящее SMS."""
    text = State()


class LinkForm(StatesGroup):
    """Привязка Telegram к существующему аккаунту API (логин + пароль)."""
    username = State()
    password = State()

class ImportContactsForm(StatesGroup):
    """Ожидание CSV-файла с контактами."""
    file = State()


async def _get_or_create_user(session: AsyncSession, telegram_id: int, username: str | None) -> User | None:
    """
    Найти или создать пользователя по telegram_id.
    Учитывает режим регистрации (open/closed/semi_open).
    Возвращает User или None, если регистрация закрыта или заявка подана (semi_open).
    """
    r = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = r.scalar_one_or_none()
    if user:
        return user

    mode = await get_registration_mode(session)

    if mode == "closed":
        return None

    if mode == "semi_open":
        # Проверяем, нет ли уже заявки от этого telegram_id
        pr = await session.execute(select(PendingRegistration).where(PendingRegistration.telegram_id == telegram_id))
        if pr.scalar_one_or_none():
            return None  # уже подана, ждём одобрения
        uname = username or f"tg_{telegram_id}"
        existing_user = await session.execute(select(User).where(User.username == uname))
        if existing_user.scalar_one_or_none():
            uname = f"tg_{telegram_id}"
        existing_pending = await session.execute(select(PendingRegistration).where(PendingRegistration.username == uname))
        if existing_pending.scalar_one_or_none():
            uname = f"tg_{telegram_id}"
        pending = PendingRegistration(
            telegram_id=telegram_id,
            username=uname,
            hashed_password="",
            source=PendingRegistrationSource.TELEGRAM,
        )
        session.add(pending)
        await session.commit()
        return None

    uname = username or f"tg_{telegram_id}"
    existing = await session.execute(select(User).where(User.username == uname))
    if existing.scalar_one_or_none():
        uname = f"tg_{telegram_id}"
    user = User(
        telegram_id=telegram_id,
        username=uname,
        hashed_password="",
        role=RoleEnum.ADMIN if telegram_id == config.ADMIN_ID else RoleEnum.USER,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@router.message(Command("start"))
async def cmd_start(message: Message, session: AsyncSession):
    r = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
    user = r.scalar_one_or_none()

    if not user:
        user = await _get_or_create_user(session, message.from_user.id, message.from_user.username)
        if user is None:
            mode = await get_registration_mode(session)
            if mode == "closed":
                await message.reply(
                    "Регистрация закрыта. Доступ только по приглашению администратора.\n"
                    "Обратитесь к администратору для создания учётной записи."
                )
                return
            if mode == "semi_open":
                await message.reply(
                    "Заявка на регистрацию подана. Ожидайте одобрения администратора — "
                    "после одобрения вы получите доступ к боту."
                )
                return
        role_text = "👑 Администратор" if user.role == RoleEnum.ADMIN else "👤 Пользователь"
        await message.reply(
            f"Добро пожаловать в SMS Node!\n\n"
            f"Ваша роль: {role_text}\n\n"
            f"Команды:\n"
            f"/my_numbers — мои SIM-карты\n"
            f"/send_sms — отправить SMS\n"
            f"/history — история SMS\n"
            f"/contacts — контакты\n"
            f"/link — привязать Telegram к существующей учётной записи KMP/API\n"
            f"/set_password — установить пароль для входа в приложение (если вы регистрировались через Telegram)\n"
            f"/help — справка"
        )
    else:
        await message.reply("С возвращением! Используйте /help для списка команд.")


@router.message(Command("link"))
async def cmd_link(message: Message, state: FSMContext):
    """Начать привязку: ввести логин и пароль от учётки API."""
    await state.set_state(LinkForm.username)
    await message.reply(
        "Привязка Telegram к учётной записи API.\n\n"
        "Введите логин (username) от вашей учётки в приложении/API:"
    )


@router.message(Command("cancel"), LinkForm.username)
@router.message(Command("cancel"), LinkForm.password)
async def link_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.reply("Привязка отменена.")


@router.message(LinkForm.username, F.text)
async def link_username(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text or message.text.startswith("/"):
        await message.reply("Введите логин текстом (без слэша). Для отмены: /cancel")
        return
    await state.update_data(link_username=message.text.strip())
    await state.set_state(LinkForm.password)
    await message.reply("Теперь введите пароль от этой учётки:")


@router.message(LinkForm.password, F.text)
async def link_password(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text:
        await message.reply("Введите пароль.")
        return
    data = await state.get_data()
    await state.clear()
    username = data.get("link_username", "").strip()
    password = message.text

    r = await session.execute(select(User).where(User.username == username))
    user = r.scalar_one_or_none()
    if not user:
        await message.reply("❌ Пользователь с таким логином не найден.")
        return
    if not user.hashed_password or not verify_password(password, user.hashed_password):
        await message.reply("❌ Неверный пароль.")
        return
    user_with_tg = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
    user_with_tg_obj = user_with_tg.scalar_one_or_none()
    
    if user_with_tg_obj:
        if user_with_tg_obj.id == user.id:
            await message.reply("Этот Telegram уже привязан к данной учетной записи.")
        else:
            await message.reply("❌ Этот Telegram уже привязан к другой учетной записи.")
        return

    if user.telegram_id and user.telegram_id != message.from_user.id:
        await message.reply("❌ Этот аккаунт уже привязан к другому Telegram.")
        return

    user.telegram_id = message.from_user.id
    await session.commit()
    await message.reply(
        "✅ Telegram привязан к учётной записи. Теперь вы можете пользоваться ботом и API под одной учёткой."
    )


class SetPasswordForm(StatesGroup):
    password = State()


@router.message(Command("set_password"))
async def cmd_set_password(message: Message, state: FSMContext, session: AsyncSession):
    user = await _get_or_create_user(session, message.from_user.id, message.from_user.username)
    if user is None:
        await message.reply("Доступ возможен после регистрации. Напишите /start.")
        return
    await state.set_state(SetPasswordForm.password)
    await message.reply(
        "Установка пароля для входа в приложение/API.\n\n"
        "Введите новый пароль (не менее 6 символов). Для отмены: /cancel"
    )


@router.message(Command("cancel"), SetPasswordForm.password)
async def set_password_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.reply("Отменено.")


@router.message(SetPasswordForm.password, F.text)
async def set_password_apply(message: Message, state: FSMContext, session: AsyncSession):
    pwd = (message.text or "").strip()
    await state.clear()
    if len(pwd) < 6:
        await message.reply("Пароль должен быть не менее 6 символов.")
        return
    r = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
    user = r.scalar_one_or_none()
    if not user:
        await message.reply("Сначала зарегистрируйтесь через /start.")
        return
    user.hashed_password = get_password_hash(pwd)
    await session.commit()
    await message.reply("✅ Пароль установлен. Теперь вы можете входить в приложение/API с логином и этим паролем.")


@router.message(Command("my_numbers"))
async def cmd_my_numbers(message: Message, session: AsyncSession):
    user = await _get_or_create_user(session, message.from_user.id, message.from_user.username)
    if user is None:
        await message.reply("Доступ возможен после регистрации. Напишите /start.")
        return

    r = await session.execute(
        select(SimCard)
        .options(selectinload(SimCard.gateway))
        .where(SimCard.assigned_user_id == user.id)
    )
    sims = r.scalars().all()

    if not sims:
        await message.reply(
            "У вас нет назначенных номеров.\n"
            "Обратитесь к администратору для назначения SIM-карты."
        )
        return

    text = "📱 <b>Ваши SIM-карты:</b>\n\n"
    for sim in sims:
        gw_name = sim.gateway.name if sim.gateway else "N/A"
        phone = sim.phone_number or "номер не указан"
        text += (
            f"📡 Шлюз: {gw_name}\n"
            f"   Порт: {sim.port_number}\n"
            f"   Номер: {phone}\n\n"
        )

    await message.reply(text, parse_mode="HTML")


@router.message(Command("send_sms"))
async def cmd_send_sms(message: Message, session: AsyncSession):
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.reply("Формат: /send_sms <номер_телефона> <текст сообщения>")
            return

        _, phone_number, text = parts
        text = text.strip()

        if not text:
            await message.reply("Текст сообщения не может быть пустым.")
            return

        user = await _get_or_create_user(session, message.from_user.id, message.from_user.username)
        if user is None:
            await message.reply("Доступ возможен после регистрации. Напишите /start.")
            return

        r = await session.execute(
        select(SimCard)
        .options(selectinload(SimCard.gateway))
        .where(SimCard.assigned_user_id == user.id)
        )
        sims = r.scalars().all()

        if not sims:
            await message.reply(
                "У вас нет назначенных номеров для отправки SMS.\n"
                "Обратитесь к администратору."
            )
            return

        sim = sims[0]
        if not sim.gateway_id:
            await message.reply("SIM-карта не привязана к шлюзу.")
            return

        wait_msg = await message.reply("⏳ Отправляем сообщение...")

        job_id = await enqueue_sms(
            gateway_id=sim.gateway_id,
            port_num=sim.port_number,
            phone=phone_number,
            text=text,
            sim_card_id=sim.id,
        )

        await wait_msg.delete()
        await message.reply(
            f"✅ SMS поставлено в очередь\n"
            f"📱 Кому: {phone_number}\n"
            f"📡 Через: {sim.gateway.name if sim.gateway else sim.gateway_id}, порт {sim.port_number}\n"
            f"🔖 ID задачи: {job_id}"
        )

    except ValueError:
        await message.reply("Формат: /send_sms <номер_телефона> <текст сообщения>")
    except Exception as e:
        logger.error(f"Ошибка отправки SMS: {e}")
        await message.reply(f"❌ Ошибка: {e}")


@router.message(Command("history"))
async def cmd_history(message: Message, session: AsyncSession):
    user = await _get_or_create_user(session, message.from_user.id, message.from_user.username)
    if user is None:
        await message.reply("Доступ возможен после регистрации. Напишите /start.")
        return

    r_sims = await session.execute(
        select(SimCard.id).where(SimCard.assigned_user_id == user.id)
    )
    sim_ids = [row[0] for row in r_sims.fetchall()]

    if not sim_ids:
        await message.reply("У вас нет истории SMS (нет назначенных SIM-карт).")
        return

    r = await session.execute(
        select(SmsMessage)
        .where(SmsMessage.sim_card_id.in_(sim_ids))
        .order_by(SmsMessage.created_at.desc())
        .limit(15)
    )
    messages = r.scalars().all()

    if not messages:
        await message.reply("История SMS пока пуста.")
        return

    text = "📋 <b>Последние 15 SMS:</b>\n\n"
    for sms in messages:
        icon = "📥" if sms.direction == MessageDirectionEnum.INCOMING else "📤"
        status_icon = {
            MessageStatusEnum.SENT_OK: "✅",
            MessageStatusEnum.FAILED: "❌",
            MessageStatusEnum.RECEIVED: "📩",
            MessageStatusEnum.PENDING: "⏳",
            MessageStatusEnum.SENDING: "🔄",
        }.get(sms.status, "❓")
        time_str = sms.created_at.strftime("%d.%m %H:%M") if sms.created_at else ""
        short_text = sms.text[:60] + ("..." if len(sms.text) > 60 else "")

        text += f"{icon} {time_str} {sms.external_phone} {status_icon}\n"
        text += f"   {short_text}\n\n"

    await message.reply(text, parse_mode="HTML")


@router.message(Command("contacts"))
async def cmd_contacts(message: Message, session: AsyncSession):
    user = await _get_or_create_user(session, message.from_user.id, message.from_user.username)
    if user is None:
        await message.reply("Доступ возможен после регистрации. Напишите /start.")
        return

    r = await session.execute(
        select(Contact).where(Contact.user_id == user.id).order_by(Contact.name)
    )
    contacts = r.scalars().all()

    if not contacts:
        await message.reply(
            "У вас нет контактов.\n\n"
            "Добавьте контакт: /add_contact <имя> | <номер_телефона>"
        )
        return

    text = "👥 <b>Ваши контакты:</b>\n\n"
    for c in contacts:
        text += f"• {c.name}: <code>{c.phone_number}</code>\n"

    text += "\nОтправить SMS: /send_sms <номер> <текст>"
    await message.reply(text, parse_mode="HTML")


@router.message(Command("export_contacts"))
async def cmd_export_contacts(message: Message, session: AsyncSession):
    """Экспорт контактов в CSV файлом."""
    user = await _get_or_create_user(session, message.from_user.id, message.from_user.username)
    if user is None:
        await message.reply("Доступ возможен после регистрации. Напишите /start.")
        return

    r = await session.execute(
        select(Contact).where(Contact.user_id == user.id).order_by(Contact.name)
    )
    contacts = r.scalars().all()

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["name", "phone_number"])
    for c in contacts:
        w.writerow([c.name, c.phone_number])

    data = out.getvalue().encode("utf-8-sig")
    await message.answer_document(
        BufferedInputFile(data, filename="contacts.csv"),
        caption="📤 Экспорт контактов (CSV)",
    )


@router.message(Command("import_contacts"))
async def cmd_import_contacts(message: Message, state: FSMContext, session: AsyncSession):
    """Импорт контактов из CSV: попросить пользователя прислать файл."""
    user = await _get_or_create_user(session, message.from_user.id, message.from_user.username)
    if user is None:
        await message.reply("Доступ возможен после регистрации. Напишите /start.")
        return
    await state.set_state(ImportContactsForm.file)
    await message.reply(
        "📥 Импорт контактов.\n\n"
        "Пришлите CSV-файл документом (колонки: name, phone_number).\n"
        "Для отмены: /cancel"
    )


@router.message(Command("cancel"), ImportContactsForm.file)
async def import_contacts_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.reply("Импорт отменён.")


@router.message(ImportContactsForm.file, F.document)
async def import_contacts_file(message: Message, state: FSMContext, session: AsyncSession):
    await state.clear()
    user = await _get_or_create_user(session, message.from_user.id, message.from_user.username)
    if user is None:
        await message.reply("Доступ возможен после регистрации. Напишите /start.")
        return
    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".csv"):
        await message.reply("Ожидается CSV-файл (расширение .csv).")
        return

    file = await message.bot.get_file(doc.file_id)
    raw = await message.bot.download_file(file.file_path)
    content = raw.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1251")

    reader = csv.DictReader(io.StringIO(text))
    created = updated = skipped = 0
    for row in reader:
        name = (row.get("name") or "").strip()
        phone = (row.get("phone_number") or row.get("phone") or row.get("number") or "").strip()
        if not name or not phone:
            skipped += 1
            continue

        existing = await session.execute(
            select(Contact).where(Contact.user_id == user.id, Contact.phone_number == phone)
        )
        c = existing.scalar_one_or_none()
        if c:
            if c.name != name:
                c.name = name
                updated += 1
            else:
                skipped += 1
        else:
            session.add(Contact(user_id=user.id, name=name, phone_number=phone))
            created += 1

    await session.commit()
    await message.reply(
        "✅ Импорт завершён.\n"
        f"Создано: {created}\n"
        f"Обновлено: {updated}\n"
        f"Пропущено: {skipped}"
    )


@router.message(Command("groups"))
async def cmd_groups(message: Message, session: AsyncSession):
    """Список групп контактов."""
    user = await _get_or_create_user(session, message.from_user.id, message.from_user.username)
    if user is None:
        await message.reply("Доступ возможен после регистрации. Напишите /start.")
        return
    r = await session.execute(
        select(ContactGroup)
        .where(ContactGroup.user_id == user.id)
        .options(selectinload(ContactGroup.contacts))
        .order_by(ContactGroup.name)
    )
    groups = r.scalars().all()
    if not groups:
        await message.reply("Групп пока нет. Создайте в приложении или через API.")
        return
    text = "🗂 <b>Ваши группы:</b>\n\n"
    for g in groups:
        text += f"• {g.name} (контактов: {len(g.contacts or [])})\n"
    text += "\nМассовая рассылка: /mass_send <имя_группы> | <текст>"
    await message.reply(text, parse_mode="HTML")


@router.message(Command("mass_send"))
async def cmd_mass_send(message: Message, session: AsyncSession):
    """
    Массовая рассылка по группе.
    Формат: /mass_send <имя_группы> | <текст>
    """
    if "|" not in (message.text or ""):
        await message.reply(
            "Формат: /mass_send <имя_группы> | <текст>\n"
            "Пример: /mass_send Клиенты | Напоминаем о платеже"
        )
        return

    user = await _get_or_create_user(session, message.from_user.id, message.from_user.username)
    if user is None:
        await message.reply("Доступ возможен после регистрации. Напишите /start.")
        return

    left, text = message.text.split("|", 1)
    group_name = left.replace("/mass_send", "").strip()
    text = text.strip()
    if not group_name or not text:
        await message.reply("Укажите имя группы и текст.")
        return

    r = await session.execute(
        select(ContactGroup)
        .where(ContactGroup.user_id == user.id, ContactGroup.name == group_name)
        .options(selectinload(ContactGroup.contacts))
    )
    group = r.scalar_one_or_none()
    if not group:
        await message.reply("❌ Группа не найдена. Посмотрите список: /groups")
        return

    r_sims = await session.execute(
        select(SimCard).where(SimCard.assigned_user_id == user.id).order_by(SimCard.id)
    )
    sims = r_sims.scalars().all()
    if not sims:
        await message.reply("❌ У вас нет назначенных SIM-карт для отправки.")
        return
    sim = sims[0]

    phones = [c.phone_number for c in (group.contacts or []) if c.phone_number]
    if not phones:
        await message.reply("❌ В группе нет контактов.")
        return

    wait_msg = await message.reply(f"⏳ Ставлю в очередь {len(phones)} SMS...")
    job_ids = []
    for phone in phones:
        job_ids.append(
            await enqueue_sms(
                gateway_id=sim.gateway_id,
                port_num=sim.port_number,
                phone=phone,
                text=text,
                sim_card_id=sim.id,
            )
        )
    await wait_msg.delete()
    await message.reply(
        f"✅ Рассылка поставлена в очередь.\n"
        f"Группа: {group.name}\n"
        f"Сообщений: {len(job_ids)}\n"
        f"Пример ID: {job_ids[0] if job_ids else '-'}"
    )


@router.message(Command("add_contact"))
async def cmd_add_contact(message: Message, session: AsyncSession):
    """Добавить контакт: /add_contact имя | номер_телефона"""
    if "|" not in (message.text or ""):
        await message.reply(
            "Формат: /add_contact <имя> | <номер_телефона>\n"
            "Пример: /add_contact Иван Петров | +79001234567"
        )
        return

    try:
        parts = message.text.split("|", 1)
        name = parts[0].replace("/add_contact", "").strip()
        phone = parts[1].strip()

        if not name or not phone:
            await message.reply("Имя и номер не могут быть пустыми.")
            return

        user = await _get_or_create_user(session, message.from_user.id, message.from_user.username)
        if user is None:
            await message.reply("Доступ возможен после регистрации. Напишите /start.")
            return

        contact = Contact(name=name, phone_number=phone, user_id=user.id)
        session.add(contact)
        await session.commit()
        await message.reply(f"✅ Контакт <b>{name}</b> ({phone}) добавлен!", parse_mode="HTML")

    except Exception as e:
        logger.error(f"add_contact error: {e}")
        await message.reply("❌ Ошибка при добавлении контакта.")


@router.callback_query(F.data.startswith("reply:"))
async def cb_reply_start(callback: CallbackQuery, state: FSMContext):
    """Начало FSM: пользователь нажал «Ответить» на уведомление."""
    _, sim_id_str, external_phone = callback.data.split(":", 2)
    await state.set_data({"sim_card_id": int(sim_id_str), "external_phone": external_phone})
    await state.set_state(ReplyForm.text)

    await callback.message.reply(
        f"✍️ Введите текст ответа для <code>{external_phone}</code>:\n"
        "(или отправьте /cancel для отмены)",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(Command("cancel"), ReplyForm.text)
async def cmd_reply_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.reply("❌ Ответ отменён.")


@router.message(ReplyForm.text)
async def form_reply_text(message: Message, state: FSMContext, session: AsyncSession):
    """Получаем текст ответа, отправляем SMS."""
    data = await state.get_data()
    await state.clear()

    sim_card_id: int = data["sim_card_id"]
    external_phone: str = data["external_phone"]
    reply_text: str = message.text.strip()

    if not reply_text:
        await message.reply("Пустой текст. Ответ не отправлен.")
        return

    sim = await session.get(SimCard, sim_card_id)
    if not sim:
        await message.reply("❌ SIM-карта не найдена.")
        return

    wait_msg = await message.reply(f"⏳ Отправляю ответ на {external_phone}...")

    job_id = await enqueue_sms(
        gateway_id=sim.gateway_id,
        port_num=sim.port_number,
        phone=external_phone,
        text=reply_text,
        sim_card_id=sim_card_id,
    )

    await wait_msg.delete()
    await message.reply(
        f"✅ Ответ отправлен!\n"
        f"📱 Кому: {external_phone}\n"
        f"🔖 ID задачи: {job_id}"
    )
