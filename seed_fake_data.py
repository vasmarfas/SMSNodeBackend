import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, insert, text
from core.db.database import AsyncSessionLocal
from core.db.models import (
    User, Gateway, SimCard, Contact, ContactGroup,
    SMSTemplate, Message, GatewayTypeEnum,
    MessageDirectionEnum, MessageStatusEnum, RoleEnum, contact_group_members
)
from core.api.auth import get_password_hash

DATA_PATH = Path(__file__).resolve().parent / "core" / "demo" / "demo_seed_data.json"

# Таблицы с serial-id, чьи sequence нужно поправить после вставки с явными id.
_SEQ_TABLES = ["users", "gateways", "sim_cards", "contacts", "contact_groups", "sms_templates", "messages"]


def _parse_dt(value: str) -> datetime:
    """Разбирает таймстемп из дампа ('2026-03-30 20:00:00+00'), нормализуя смещение."""
    s = value.strip().replace(" ", "T")
    if re.search(r"[+-]\d{2}$", s):
        s += ":00"
    return datetime.fromisoformat(s)


async def seed_demo_data():
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))

    # Сдвигаем все даты так, чтобы самое свежее сообщение оказалось "сейчас",
    # сохраняя относительные интервалы внутри диалогов.
    offset = datetime.now(timezone.utc) - _parse_dt(data["anchor"])

    async with AsyncSessionLocal() as session:
        if (await session.execute(select(User).limit(1))).scalars().first():
            print("Демо-данные уже присутствуют, пропускаю.")
            return

        print("Разворачиваем демо-данные…")

        for u in data["users"]:
            session.add(User(
                id=u["id"],
                username=u["username"],
                hashed_password=get_password_hash(u["password"]),
                role=RoleEnum[u["role"]],
                is_active=u["is_active"],
            ))

        for g in data["gateways"]:
            session.add(Gateway(
                id=g["id"],
                name=g["name"],
                type=GatewayTypeEnum[g["type"]],
                host=g["host"],
                port=g["port"],
                username=g["username"],
                password=g["password"],
                is_active=g["is_active"],
                last_seen=datetime.now(timezone.utc),
                last_status=g.get("last_status"),
            ))

        for s in data["sim_cards"]:
            session.add(SimCard(
                id=s["id"],
                gateway_id=s["gateway_id"],
                port_number=s["port_number"],
                phone_number=s.get("phone_number"),
                imei=s.get("imei"),
                iccid=s.get("iccid"),
                operator=s.get("operator"),
                balance=s.get("balance"),
                status=s["status"],
                label=s.get("label"),
                assigned_user_id=s.get("assigned_user_id"),
            ))

        for c in data["contacts"]:
            session.add(Contact(
                id=c["id"],
                user_id=c["user_id"],
                phone_number=c["phone_number"],
                name=c["name"],
            ))

        for grp in data["contact_groups"]:
            session.add(ContactGroup(id=grp["id"], user_id=grp["user_id"], name=grp["name"]))

        for t in data["templates"]:
            session.add(SMSTemplate(
                id=t["id"],
                name=t["name"],
                content=t["content"],
                category=t["category"],
                is_global=t["is_global"],
                user_id=t.get("user_id"),
            ))

        # Группы и контакты должны существовать до записей в таблице связей.
        await session.flush()

        for m in data["contact_group_members"]:
            await session.execute(
                insert(contact_group_members).values(group_id=m["group_id"], contact_id=m["contact_id"])
            )

        for msg in data["messages"]:
            created = _parse_dt(msg["created_at"]) + offset
            updated = _parse_dt(msg["updated_at"]) + offset if msg.get("updated_at") else created
            session.add(Message(
                id=msg["id"],
                sim_card_id=msg.get("sim_card_id"),
                external_phone=msg["external_phone"],
                direction=MessageDirectionEnum[msg["direction"]],
                text=msg["text"],
                status=MessageStatusEnum[msg["status"]],
                error_text=msg.get("error_text"),
                created_at=created,
                updated_at=updated,
                gateway_task_id=msg.get("gateway_task_id"),
            ))

        await session.commit()

        # После вставки с явными id sequence остаётся на нуле — выставляем на максимум,
        # иначе регистрация новых пользователей/сообщений упрётся в конфликт ключей.
        for table in _SEQ_TABLES:
            await session.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"(SELECT COALESCE(MAX(id), 1) FROM {table}))"
            ))
        await session.commit()

        print("Демо-данные успешно развёрнуты.")


if __name__ == "__main__":
    asyncio.run(seed_demo_data())
