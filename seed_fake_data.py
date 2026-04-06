import asyncio
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, insert
from core.db.database import AsyncSessionLocal
from core.db.models import (
    User, Gateway, SimCard, Contact, ContactGroup,
    SMSTemplate, Message, GatewayTypeEnum,
    MessageDirectionEnum, MessageStatusEnum, RoleEnum, contact_group_members
)
from core.api.auth import get_password_hash

async def seed_demo_data():
    async with AsyncSessionLocal() as session:
        # 1. Admin
        res_admin = await session.execute(select(User).where(User.username == "admin"))
        admin_user = res_admin.scalars().first()
        if not admin_user:
            print("Создаём пользователя admin (admin:admin)…")
            admin_user = User(
                username="admin",
                role=RoleEnum.ADMIN,
                hashed_password=get_password_hash("admin"),
                is_active=True
            )
            session.add(admin_user)
            await session.commit()
            await session.refresh(admin_user)

        # 2. Demo User
        res_demo = await session.execute(select(User).where(User.username == "demo"))
        demo_user = res_demo.scalars().first()
        if not demo_user:
            print("Создаём пользователя demo (demo:demo)…")
            demo_user = User(
                username="demo",
                role=RoleEnum.USER,
                hashed_password=get_password_hash("demo"),
                is_active=True
            )
            session.add(demo_user)
            await session.commit()
            await session.refresh(demo_user)

        users = [admin_user, demo_user]

        # 3. Gateways
        gateways = []
        for i in range(1, 4):
            gw_name = f"Mock-Gateway-{i}"
            res = await session.execute(select(Gateway).where(Gateway.name == gw_name))
            gw = res.scalars().first()
            if not gw:
                gw = Gateway(
                    name=gw_name,
                    type=GatewayTypeEnum.GOIP_UDP if i < 3 else GatewayTypeEnum.SKYLINE,
                    host=f"192.168.10.{100+i}",
                    port=9991 if i < 3 else 80,
                    username="admin",
                    password="password",
                    is_active=True,
                    last_seen=datetime.now(timezone.utc),
                    last_status="ONLINE"
                )
                session.add(gw)
                await session.flush()
            gateways.append(gw)
        await session.commit()

        # 4. SIM Cards
        for gw in gateways:
            for port in range(1, 5):
                phone = f"+7999{random.randint(100, 999)}{random.randint(1000, 9999)}"
                res = await session.execute(select(SimCard).where(SimCard.gateway_id == gw.id, SimCard.port_number == port))
                sim = res.scalars().first()
                if not sim:
                    sim = SimCard(
                        gateway_id=gw.id,
                        port_number=port,
                        phone_number=phone,
                        status="IDLE",
                        label=f"Work {port}" if port % 2 == 0 else f"Reserve {port}",
                        # Assign SIMs randomly to demo or admin
                        assigned_user_id=random.choice(users).id,
                        balance=random.uniform(50.0, 500.0)
                    )
                    session.add(sim)
                    await session.flush()
        await session.commit()

        # Generate fake data for both admin and demo
        fake_names_ru = ["Иван Иванов", "Петр Петров", "Елена Смирнова", "Анна Козлова"]
        fake_names_en = ["John Doe", "Jane Smith", "Michael Johnson", "Emily Davis"]
        
        all_contacts = []
        
        for u in users:
            # 5. Contacts (mixed RU/EN)
            user_contacts = []
            names = fake_names_ru + fake_names_en
            for name in names:
                phone = f"+7900{random.randint(100, 999)}{random.randint(1000, 9999)}"
                res = await session.execute(select(Contact).where(Contact.user_id == u.id, Contact.name == name))
                c = res.scalars().first()
                if not c:
                    c = Contact(
                        user_id=u.id,
                        name=name,
                        phone_number=phone
                    )
                    session.add(c)
                    await session.flush()
                user_contacts.append(c)
                all_contacts.append(c)

            # 6. Groups
            group_names = ["VIP Клиенты", "VIP Clients", "Colleagues", "Сотрудники"]
            for g_name in group_names:
                res = await session.execute(select(ContactGroup).where(ContactGroup.user_id == u.id, ContactGroup.name == g_name))
                g = res.scalars().first()
                if not g:
                    g = ContactGroup(user_id=u.id, name=g_name)
                    session.add(g)
                    await session.flush()
                    g_contacts = random.sample(user_contacts, k=random.randint(2, 4))
                    for gc in g_contacts:
                        await session.execute(insert(contact_group_members).values(group_id=g.id, contact_id=gc.id))

            # 7. Templates
            templates_data = [
                ("Приветствие", "Привет! Это демо-сообщение.", "marketing"),
                ("Greeting", "Hello! This is a demo message.", "marketing"),
                ("Оплата", "Ваш счет оплачен.", "billing"),
                ("Payment", "Your invoice has been paid.", "billing"),
            ]
            for t_name, t_content, category in templates_data:
                res = await session.execute(select(SMSTemplate).where(SMSTemplate.user_id == u.id, SMSTemplate.name == t_name))
                if not res.scalars().first():
                    t = SMSTemplate(user_id=u.id, name=t_name, content=t_content, category=category)
                    session.add(t)

        await session.commit()

        # 8. Messages
        # Check if messages already exist to avoid spamming on every start
        res_msgs = await session.execute(select(Message).limit(1))
        if not res_msgs.scalars().first():
            print("Генерация сообщений…")
            message_texts = [
                "Привет! Как дела?",
                "Hello! How are you?",
                "Ваш заказ готов к выдаче.",
                "Your order is ready.",
                "Скидка 20%!",
                "20% discount!",
                "Код подтверждения: 459812",
                "Verification code: 123456"
            ]

            # Re-fetch sims to ensure we have them
            res_sims = await session.execute(select(SimCard))
            sims = res_sims.scalars().all()

            if sims and all_contacts:
                now = datetime.now(timezone.utc)
                for i in range(150):
                    contact = random.choice(all_contacts)
                    sim = random.choice(sims)
                    direction = random.choice([MessageDirectionEnum.INCOMING, MessageDirectionEnum.OUTGOING])

                    status = MessageStatusEnum.SENT_OK if direction == MessageDirectionEnum.OUTGOING else MessageStatusEnum.RECEIVED
                    if direction == MessageDirectionEnum.OUTGOING and random.random() < 0.1:
                        status = MessageStatusEnum.FAILED
                    elif direction == MessageDirectionEnum.OUTGOING and random.random() < 0.2:
                        status = MessageStatusEnum.DELIVERED

                    created_at = now - timedelta(days=random.randint(0, 30), hours=random.randint(0, 24), minutes=random.randint(0, 60))

                    m = Message(
                        sim_card_id=sim.id,
                        external_phone=contact.phone_number,
                        direction=direction,
                        text=random.choice(message_texts),
                        status=status,
                        created_at=created_at,
                        gateway_task_id=f"mock-job-{i}" if direction == MessageDirectionEnum.OUTGOING else None
                    )
                    session.add(m)

                await session.commit()
        print("Данные успешно сгенерированы.")

if __name__ == "__main__":
    asyncio.run(seed_demo_data())
