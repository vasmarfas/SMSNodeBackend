import asyncio
from core.db.database import get_db, AsyncSessionLocal
from core.db.models import User, RoleEnum, SystemSetting
from core.api.auth import get_password_hash
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as session:
        # Устанавливаем режим open
        r = await session.execute(select(SystemSetting).where(SystemSetting.key == 'registration_mode'))
        setting = r.scalar_one_or_none()
        if setting:
            setting.value = 'open'
        else:
            session.add(SystemSetting(key='registration_mode', value='open'))
            
        # Создаем админа
        r = await session.execute(select(User).where(User.username == 'admin_test'))
        user = r.scalar_one_or_none()
        if not user:
            user = User(
                username='admin_test',
                hashed_password=get_password_hash('admin_password'),
                role=RoleEnum.ADMIN
            )
            session.add(user)
        
        await session.commit()
        print("User and settings updated.")

if __name__ == "__main__":
    asyncio.run(main())
