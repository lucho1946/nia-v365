import asyncio
from memory import get_db

async def main():
    db = get_db()
    result = await db.command("ping")
    print(result)

asyncio.run(main())