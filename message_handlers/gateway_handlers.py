"""
Telegram-хендлеры для управления GSM-шлюзами.

Доступно только администраторам (проверка по ADMIN_ID из конфига).

Команды и взаимодействия:
  /gateways   — список шлюзов с кнопками
  + ADD        — FSM-мастер добавления нового шлюза (6 шагов)
  + TEST       — пинг выбранного шлюза (показывает latency)
  + DELETE     — удаление шлюза (с подтверждением)
  + REFRESH    — обновить статусы всех шлюзов
"""

import logging
from datetime import datetime, timedelta, timezone

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
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from config_reader import config
from core.db.models import Gateway, GatewayTypeEnum, SimCard, GoIPEvent, GoIPEventTypeEnum
from gateway_service import gateway_service

logger = logging.getLogger(__name__)
router = Router()


class AddGatewayForm(StatesGroup):
    name     = State()   # Шаг 1: название
    gw_type  = State()   # Шаг 2: тип (выбор кнопкой)
    host     = State()   # Шаг 3: IP-адрес
    port     = State()   # Шаг 4: порт
    username = State()   # Шаг 5: логин
    password = State()   # Шаг 6: пароль


class AddSimsForm(StatesGroup):
    """Мастер добавления каналов (SIM) к уже созданному шлюзу."""
    ports = State()   # номера портов через запятую, например 1,2,3,4,5,6,7,8


# Вспомогательные функции

def _is_admin(user_id: int) -> bool:
    return user_id == config.ADMIN_ID


def _build_gateways_keyboard(gateways: list[Gateway]) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура со списком шлюзов и кнопками управления."""
    buttons = []
    for gw in gateways:
        status = "🟢" if gateway_service.get(gw.id) and gateway_service.get(gw.id).is_online else "⚪"
        disabled = "" if gw.is_active else " [OFF]"
        buttons.append([
            InlineKeyboardButton(
                text=f"{status} {gw.name} ({gw.type}) {disabled}",
                callback_data=f"gw_info:{gw.id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(text="➕ Добавить шлюз", callback_data="gw_add"),
        InlineKeyboardButton(text="🔄 Обновить статусы", callback_data="gw_refresh_all"),
    ])
    buttons.append([
        InlineKeyboardButton(text="◀ В админку", callback_data="admin_back"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _build_gateway_actions_keyboard(gw_id: int) -> InlineKeyboardMarkup:
    """Кнопки действий для конкретного шлюза."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔍 Тест (пинг)", callback_data=f"gw_test:{gw_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"gw_delete_confirm:{gw_id}"),
        ],
        [
            InlineKeyboardButton(text="🔁 Вкл/Выкл", callback_data=f"gw_toggle:{gw_id}"),
            InlineKeyboardButton(text="◀ К списку шлюзов", callback_data="gw_list"),
        ],
        [
            InlineKeyboardButton(text="📋 Добавить SIM (каналы)", callback_data=f"gw_add_sims:{gw_id}"),
            InlineKeyboardButton(text="🔍 Обнаруженные каналы", callback_data=f"gw_discovered:{gw_id}"),
        ],
        [
            InlineKeyboardButton(text="🏠 В админку", callback_data="admin_back"),
        ]
    ])


def _gw_type_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора типа шлюза."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="GoIP UDP", callback_data="gwtype:goip_udp"),
            InlineKeyboardButton(text="GoIP HTTP", callback_data="gwtype:goip_http"),
        ],
        [
            InlineKeyboardButton(text="Skyline / Dinstar", callback_data="gwtype:skyline"),
        ],
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data="gw_cancel"),
        ]
    ])


def _format_gw_info(gw: Gateway) -> str:
    """Форматировать карточку шлюза для отображения."""
    active_gw = gateway_service.get(gw.id)
    is_online = active_gw.is_online if active_gw else False
    status_icon = "🟢 Онлайн" if is_online else "⚪ Неизвестно"

    last_seen = gw.last_seen.strftime("%d.%m %H:%M") if gw.last_seen else "никогда"

    return (
        f"<b>{gw.name}</b>\n"
        f"Тип: <code>{gw.type}</code>\n"
        f"Адрес: <code>{gw.host}:{gw.port}</code>\n"
        f"Логин: <code>{gw.username}</code>\n"
        f"Статус: {status_icon}\n"
        f"Последний раз онлайн: {last_seen}\n"
        f"Активен: {'Да' if gw.is_active else 'Нет'}"
    )


# /gateways — главный список

@router.message(Command("gateways"))
async def cmd_gateways(message: Message, session: AsyncSession):
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ Эта команда только для администраторов.")
        return

    result = await session.execute(select(Gateway).order_by(Gateway.id))
    gateways = result.scalars().all()

    if not gateways:
        text = (
            "📡 <b>Шлюзы не добавлены</b>\n\n"
            "Нажмите <b>Добавить шлюз</b>, чтобы подключить первый GSM-шлюз.\n"
            "Поддерживаются: GoIP (UDP / HTTP), Skyline, Dinstar."
        )
    else:
        text = f"📡 <b>GSM-шлюзы ({len(gateways)} шт.)</b>\n\nВыберите шлюз для управления:"

    await message.answer(
        text,
        reply_markup=_build_gateways_keyboard(gateways),
        parse_mode="HTML"
    )


# Инлайн-кнопки: просмотр/список

@router.callback_query(F.data == "gw_list")
async def cb_gw_list(callback: CallbackQuery, session: AsyncSession):
    result = await session.execute(select(Gateway).order_by(Gateway.id))
    gateways = result.scalars().all()

    text = (
        f"📡 <b>GSM-шлюзы ({len(gateways)} шт.)</b>\n\nВыберите шлюз для управления:"
        if gateways else
        "📡 <b>Шлюзы не добавлены</b>\n\nНажмите Добавить шлюз."
    )

    await callback.message.edit_text(
        text,
        reply_markup=_build_gateways_keyboard(gateways),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("gw_info:"))
async def cb_gw_info(callback: CallbackQuery, session: AsyncSession):
    gw_id = int(callback.data.split(":")[1])
    gw = await session.get(Gateway, gw_id)
    if not gw:
        await callback.answer("Шлюз не найден.", show_alert=True)
        return

    await callback.message.edit_text(
        _format_gw_info(gw),
        reply_markup=_build_gateway_actions_keyboard(gw_id),
        parse_mode="HTML"
    )
    await callback.answer()


# Тест шлюза (пинг)

@router.callback_query(F.data.startswith("gw_test:"))
async def cb_gw_test(callback: CallbackQuery, session: AsyncSession):
    gw_id = int(callback.data.split(":")[1])
    gw = await session.get(Gateway, gw_id)
    if not gw:
        await callback.answer("Шлюз не найден.", show_alert=True)
        return

    await callback.answer("Проверяю связь...", show_alert=False)
    await callback.message.edit_text(f"⏳ Проверяю связь с <b>{gw.name}</b>...", parse_mode="HTML")

    active = gateway_service.get(gw_id)
    if not active:
        # Нет в памяти — добавим временно для теста
        gateway_service.reload_gateway(gw)
        active = gateway_service.get(gw_id)

    result = await active.ping()

    # Обновляем статус в БД
    status_str = "online" if result["online"] else f"offline: {result['detail']}"
    gw.last_status = (status_str[:50]) if status_str else None
    if result["online"]:
        gw.last_seen = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(gw)

    if result["online"]:
        status_text = (
            f"✅ <b>{gw.name}</b> — ОНЛАЙН\n"
            f"Задержка: <b>{result['latency_ms']} мс</b>\n"
            f"Детали: {result['detail']}"
        )
    else:
        status_text = (
            f"❌ <b>{gw.name}</b> — ОФЛАЙН\n"
            f"Причина: {result['detail']}"
        )

    await callback.message.edit_text(
        status_text,
        reply_markup=_build_gateway_actions_keyboard(gw_id),
        parse_mode="HTML"
    )


# Вкл/Выкл шлюза

@router.callback_query(F.data.startswith("gw_toggle:"))
async def cb_gw_toggle(callback: CallbackQuery, session: AsyncSession):
    gw_id = int(callback.data.split(":")[1])
    gw = await session.get(Gateway, gw_id)
    if not gw:
        await callback.answer("Шлюз не найден.", show_alert=True)
        return

    gw.is_active = not gw.is_active
    await session.commit()
    await session.refresh(gw)

    if gw.is_active:
        gateway_service.reload_gateway(gw)
        status = "включен"
    else:
        gateway_service.remove_gateway(gw_id)
        status = "выключен"

    await callback.answer(f"Шлюз {status}.", show_alert=True)
    await callback.message.edit_text(
        _format_gw_info(gw),
        reply_markup=_build_gateway_actions_keyboard(gw_id),
        parse_mode="HTML"
    )


# Удаление шлюза

@router.callback_query(F.data.startswith("gw_delete_confirm:"))
async def cb_gw_delete_confirm(callback: CallbackQuery, session: AsyncSession):
    gw_id = int(callback.data.split(":")[1])
    gw = await session.get(Gateway, gw_id)
    if not gw:
        await callback.answer("Шлюз не найден.", show_alert=True)
        return

    await callback.message.edit_text(
        f"⚠️ Удалить шлюз <b>{gw.name}</b> ({gw.host})?\n\n"
        "Это действие необратимо.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"gw_delete:{gw_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"gw_info:{gw_id}"),
            ]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("gw_delete:"))
async def cb_gw_delete(callback: CallbackQuery, session: AsyncSession):
    gw_id = int(callback.data.split(":")[1])
    gw = await session.get(Gateway, gw_id)
    if not gw:
        await callback.answer("Шлюз не найден.", show_alert=True)
        return

    name = gw.name
    await session.delete(gw)
    await session.commit()
    gateway_service.remove_gateway(gw_id)

    logger.info(f"Gateway '{name}' (ID={gw_id}) deleted by admin {callback.from_user.id}")

    result = await session.execute(select(Gateway).order_by(Gateway.id))
    gateways = result.scalars().all()

    await callback.message.edit_text(
        f"🗑 Шлюз <b>{name}</b> удалён.\n\n"
        f"Активных шлюзов: {len(gateways)}",
        reply_markup=_build_gateways_keyboard(gateways),
        parse_mode="HTML"
    )
    await callback.answer("Удалено.")


# Обновить статусы всех шлюзов

@router.callback_query(F.data == "gw_refresh_all")
async def cb_gw_refresh_all(callback: CallbackQuery, session: AsyncSession):
    await callback.answer("Опрашиваю шлюзы...", show_alert=False)
    await callback.message.edit_text("⏳ Проверяю все шлюзы...")

    results = await gateway_service.ping_all(session)

    online = sum(1 for r in results.values() if r["online"])
    total = len(results)

    result_db = await session.execute(select(Gateway).order_by(Gateway.id))
    gateways = result_db.scalars().all()

    await callback.message.edit_text(
        f"📡 <b>Обновление завершено</b>\n"
        f"Онлайн: {online}/{total}\n\n"
        "Выберите шлюз для управления:",
        reply_markup=_build_gateways_keyboard(gateways),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "gw_add")
async def cb_gw_add_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddGatewayForm.name)
    await callback.message.edit_text(
        "➕ <b>Добавление нового шлюза</b>\n\n"
        "<b>Шаг 1 / 6</b> — Введите название шлюза:\n"
        "<i>Например: Офис - GoIP8, Склад GoIP-1</i>",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "gw_cancel")
async def cb_gw_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Добавление шлюза отменено.")
    await callback.answer()


# Добавление каналов (SIM) к шлюзу

@router.callback_query(F.data.startswith("gw_add_sims:"))
async def cb_gw_add_sims_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    gw_id = int(callback.data.split(":")[1])
    gw = await session.get(Gateway, gw_id)
    if not gw:
        await callback.answer("Шлюз не найден.", show_alert=True)
        return
    await state.update_data(gw_add_sims_gateway_id=gw_id)
    await state.set_state(AddSimsForm.ports)
    await callback.message.edit_text(
        f"📋 <b>Добавление каналов (SIM) к шлюзу «{gw.name}»</b>\n\n"
        "Введите номера портов через запятую (1–8).\n"
        "Пример: <code>1,2,3,4,5,6,7,8</code> — добавить все 8 каналов.\n"
        "Или один порт: <code>1</code>\n\n"
        "Отправь <b>Отмена</b> для выхода.",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AddSimsForm.ports, F.text)
async def form_add_sims_ports(message: Message, state: FSMContext, session: AsyncSession):
    if message.text.strip().lower() in ("отмена", "cancel"):
        await state.clear()
        await message.answer("❌ Добавление каналов отменено.")
        return

    raw = message.text.strip().replace(" ", "")
    try:
        ports = [int(p) for p in raw.split(",") if p]
    except ValueError:
        await message.answer("Укажи числа через запятую, например: 1,2,3,4,5,6,7,8")
        return

    bad = [p for p in ports if not (1 <= p <= 8)]
    if bad:
        await message.answer(f"Порты должны быть от 1 до 8. Неверно: {bad}. Попробуй снова.")
        return

    ports = sorted(set(ports))
    data = await state.get_data()
    gw_id = data.get("gw_add_sims_gateway_id")
    await state.clear()

    if not gw_id:
        await message.answer("Сессия сброшена. Выбери шлюз снова и нажми «Добавить SIM (каналы)».")
        return

    gw = await session.get(Gateway, gw_id)
    if not gw:
        await message.answer("Шлюз не найден.")
        return

    # Уже существующие порты у этого шлюза
    existing = await session.execute(
        select(SimCard.port_number).where(
            SimCard.gateway_id == gw_id,
            SimCard.port_number.in_(ports),
        )
    )
    existing_ports = set(existing.scalars().all())

    added = []
    for p in ports:
        if p in existing_ports:
            continue
        sim = SimCard(gateway_id=gw_id, port_number=p)
        session.add(sim)
        added.append(p)

    await session.commit()

    if added:
        text = f"✅ Добавлено каналов: <b>{len(added)}</b> — порты {added}"
        if len(existing_ports) > 0:
            text += f"\n⚠️ Уже были: {sorted(existing_ports)}"
    else:
        text = f"Все указанные порты уже есть у этого шлюза: {sorted(existing_ports)}"

    await message.answer(text, parse_mode="HTML", reply_markup=_build_gateway_actions_keyboard(gw_id))


# Обнаруженные каналы по keepalive (автодетект)

async def _get_discovered_channels_for_gateway(
    session: AsyncSession, gateway_id: int, minutes: int = 10
) -> tuple[list[dict], int]:
    """
    Каналы из последних keepalive для данного шлюза, по которым ещё нет SimCard.
    Возвращает (список для добавления, количество каналов с keepalive за период).
    """
    gw = await session.get(Gateway, gateway_id)
    if not gw:
        return [], 0
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    r = await session.execute(
        select(GoIPEvent)
        .where(
            and_(
                GoIPEvent.event_type == GoIPEventTypeEnum.KEEPALIVE,
                GoIPEvent.received_at >= since,
                GoIPEvent.host == gw.host,
            )
        )
        .order_by(GoIPEvent.received_at.desc())
    )
    events = r.scalars().all()
    by_goip_id: dict[str, GoIPEvent] = {}
    for ev in events:
        try:
            port_num = int(ev.goip_id)
        except ValueError:
            continue
        if 1001 <= port_num <= 1008 and ev.goip_id not in by_goip_id:
            by_goip_id[ev.goip_id] = ev

    existing_r = await session.execute(
        select(SimCard.port_number).where(SimCard.gateway_id == gateway_id)
    )
    existing_ports = set(existing_r.scalars().all())

    out = []
    for goip_id, ev in by_goip_id.items():
        port_number = int(goip_id) - 1000
        if port_number in existing_ports:
            continue
        payload = ev.payload_json or {}
        out.append({
            "port_number": port_number,
            "goip_id": goip_id,
            "phone_number": payload.get("num"),
            "signal": payload.get("signal"),
            "gsm_status": payload.get("gsm_status"),
        })
    return sorted(out, key=lambda x: x["port_number"]), len(by_goip_id)


@router.callback_query(F.data.startswith("gw_discovered:"))
async def cb_gw_discovered(callback: CallbackQuery, session: AsyncSession):
    gw_id = int(callback.data.split(":")[1])
    gw = await session.get(Gateway, gw_id)
    if not gw:
        await callback.answer("Шлюз не найден.", show_alert=True)
        return

    discovered, keepalive_channels_count = await _get_discovered_channels_for_gateway(session, gw_id)
    if not discovered:
        if keepalive_channels_count == 0:
            msg = (
                "За последние 10 минут keepalive от этого шлюза не приходили.\n\n"
                "Убедись, что шлюз включён и в настройках указан этот сервер и порт (9991). "
                "Можно добавить каналы вручную через «Добавить SIM (каналы)»."
            )
        else:
            msg = (
                f"По всем каналам (1001–1008), от которых приходили keepalive ({keepalive_channels_count} шт.), "
                "уже созданы SIM. Новых каналов для добавления нет.\n\n"
                "Чтобы добавить порты вручную, используй «Добавить SIM (каналы)»."
            )
        await callback.message.edit_text(
            f"🔍 <b>Обнаруженные каналы</b> (шлюз «{gw.name}»)\n\n{msg}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀ К шлюзу", callback_data=f"gw_info:{gw_id}")],
            ])
        )
        await callback.answer()
        return

    lines = []
    for ch in discovered:
        num = ch.get("phone_number") or "—"
        lines.append(f"  Порт {ch['port_number']} (goip_id {ch['goip_id']}) — {num}")
    text = (
        f"🔍 <b>Обнаружены каналы</b> по keepalive (шлюз «{gw.name}»)\n\n"
        + "\n".join(lines)
        + "\n\nНажми <b>Добавить все</b>, чтобы создать SIM для этих портов."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Добавить все обнаруженные", callback_data=f"gw_discovered_add_all:{gw_id}")],
        [InlineKeyboardButton(text="◀ К шлюзу", callback_data=f"gw_info:{gw_id}")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("gw_discovered_add_all:"))
async def cb_gw_discovered_add_all(callback: CallbackQuery, session: AsyncSession):
    gw_id = int(callback.data.split(":")[1])
    gw = await session.get(Gateway, gw_id)
    if not gw:
        await callback.answer("Шлюз не найден.", show_alert=True)
        return

    discovered, _ = await _get_discovered_channels_for_gateway(session, gw_id)
    if not discovered:
        await callback.answer("Нечего добавлять — обнаруженных каналов нет.", show_alert=True)
        return

    added = []
    for ch in discovered:
        sim = SimCard(
            gateway_id=gw_id,
            port_number=ch["port_number"],
            phone_number=ch.get("phone_number"),
        )
        session.add(sim)
        added.append(ch["port_number"])
    await session.commit()

    await callback.message.edit_text(
        f"✅ Добавлено каналов по автодетекту: <b>{len(added)}</b> — порты {added}\n\n"
        f"Шлюз: {gw.name}",
        parse_mode="HTML",
        reply_markup=_build_gateway_actions_keyboard(gw_id),
    )
    await callback.answer()


@router.message(AddGatewayForm.name)
async def form_gw_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("Название слишком короткое. Попробуй ещё раз:")
        return
    await state.update_data(name=name)
    await state.set_state(AddGatewayForm.gw_type)
    await message.answer(
        f"✅ Название: <b>{name}</b>\n\n"
        "<b>Шаг 2 / 6</b> — Выберите тип шлюза:",
        reply_markup=_gw_type_keyboard(),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("gwtype:"), AddGatewayForm.gw_type)
async def form_gw_type(callback: CallbackQuery, state: FSMContext):
    gw_type = callback.data.split(":")[1]
    type_labels = {
        "goip_udp": "GoIP UDP (рекомендуется)",
        "goip_http": "GoIP HTTP",
        "skyline": "Skyline / Dinstar (HTTP JSON)",
    }
    await state.update_data(gw_type=gw_type)
    await state.set_state(AddGatewayForm.host)
    await callback.message.edit_text(
        f"✅ Тип: <b>{type_labels[gw_type]}</b>\n\n"
        "<b>Шаг 3 / 6</b> — Введите IP-адрес шлюза:\n"
        "<i>Например: 192.168.1.100</i>",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AddGatewayForm.host)
async def form_gw_host(message: Message, state: FSMContext):
    host = message.text.strip()
    data = await state.get_data()

    # Подставим дефолтный порт в зависимости от типа
    default_port = 9991 if data.get("gw_type") == "goip_udp" else 80

    await state.update_data(host=host)
    await state.set_state(AddGatewayForm.port)
    await message.answer(
        f"✅ Хост: <code>{host}</code>\n\n"
        f"<b>Шаг 4 / 6</b> — Введите порт (или отправь <code>{default_port}</code> для дефолтного):",
        parse_mode="HTML"
    )


@router.message(AddGatewayForm.port)
async def form_gw_port(message: Message, state: FSMContext):
    try:
        port = int(message.text.strip())
        if not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        await message.answer("Порт должен быть числом от 1 до 65535. Попробуй ещё раз:")
        return

    data = await state.get_data()
    gw_type = data.get("gw_type")
    is_udp = gw_type == "goip_udp"
    default_username = "goip01" if is_udp else "admin"
    username_hint = (
        "Для GoIP UDP укажи <b>SMS Server Auth ID</b> из настроек шлюза "
        "(поле id в пакетах keepalive/RECEIVE)."
        if is_udp else
        "Для GoIP HTTP / Skyline обычно используется веб-логин (admin)."
    )

    await state.update_data(port=port)
    await state.set_state(AddGatewayForm.username)
    await message.answer(
        f"✅ Порт: <b>{port}</b>\n\n"
        f"<b>Шаг 5 / 6</b> — Введите ID/логин (по умолчанию <code>{default_username}</code>):\n"
        f"{username_hint}",
        parse_mode="HTML"
    )


@router.message(AddGatewayForm.username)
async def form_gw_username(message: Message, state: FSMContext):
    data = await state.get_data()
    gw_type = data.get("gw_type")
    username = message.text.strip() or ("goip01" if gw_type == "goip_udp" else "admin")
    await state.update_data(username=username)
    await state.set_state(AddGatewayForm.password)
    await message.answer(
        f"✅ Логин: <code>{username}</code>\n\n"
        "<b>Шаг 6 / 6</b> — Введите пароль шлюза:",
        parse_mode="HTML"
    )


@router.message(AddGatewayForm.password)
async def form_gw_password(message: Message, state: FSMContext, session: AsyncSession):
    password = message.text.strip()
    if not password:
        await message.answer("Пароль не может быть пустым. Введи ещё раз:")
        return

    data = await state.get_data()
    await state.clear()

    # Маппинг строки типа → GatewayTypeEnum
    _type_map = {
        "goip_udp": GatewayTypeEnum.GOIP_UDP,
        "goip_http": GatewayTypeEnum.GOIP_HTTP,
        "skyline": GatewayTypeEnum.SKYLINE,
        "dinstar": GatewayTypeEnum.DINSTAR,
    }
    gw_type_enum = _type_map.get(data.get("gw_type", "goip_udp"), GatewayTypeEnum.GOIP_UDP)

    # Создаём запись в БД
    new_gw = Gateway(
        name=data["name"],
        type=gw_type_enum,
        host=data["host"],
        port=data["port"],
        username=data["username"],
        password=password,
        is_active=True,
    )
    session.add(new_gw)
    await session.commit()
    await session.refresh(new_gw)

    # Добавляем в менеджер
    gateway_service.reload_gateway(new_gw)

    # Сразу тестируем новый шлюз
    active = gateway_service.get(new_gw.id)
    ping_result = await active.ping()
    ping_icon = "🟢" if ping_result["online"] else "⚪"
    ping_detail = ping_result["detail"]

    logger.info(f"Gateway '{new_gw.name}' added (ID={new_gw.id}), ping={ping_result['online']}")

    await message.answer(
        f"✅ <b>Шлюз добавлен!</b>\n\n"
        f"Название: {new_gw.name}\n"
        f"Тип: {new_gw.type}\n"
        f"Адрес: {new_gw.host}:{new_gw.port}\n"
        f"Логин: {new_gw.username}\n\n"
        f"Тест связи: {ping_icon} {ping_detail}\n\n"
        "📌 <i>Один шлюз = одно устройство. Каналы (порты 1–8) добавляются отдельно — "
        "нажми «Добавить SIM (каналы)» ниже или в карточке шлюза.</i>\n\n"
        "Управляй шлюзом через /gateways",
        parse_mode="HTML",
        reply_markup=_build_gateway_actions_keyboard(new_gw.id)
    )
