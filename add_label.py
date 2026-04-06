import asyncio
from core.db.database import get_db, AsyncSessionLocal
from sqlalchemy import text

async def main():
    print("Modifying DB...")
    async with AsyncSessionLocal() as session:
        try:
            await session.execute(text("ALTER TABLE sim_cards ADD COLUMN label VARCHAR(100)"))
            await session.commit()
            print("Added label to sim_cards")
        except Exception as e:
            print("Already exists or error:", e)

if __name__ == "__main__":
    asyncio.run(main())
