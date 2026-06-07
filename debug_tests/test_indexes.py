import asyncio
from memory import ensure_index, SESSIONS_COLLECTION

async def main():
    await ensure_index()
    print("Índices OK en", SESSIONS_COLLECTION)

asyncio.run(main())