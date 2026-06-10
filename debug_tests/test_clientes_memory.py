import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from memory import ensure_index, upsert_cliente, get_cliente, get_cliente_por_email


async def main():
    await ensure_index()

    phone_id = "test_cliente_perm_001"

    cliente = await upsert_cliente(
        phone_id,
        {
            "nombre": "Cliente Prueba",
            "email": "Cliente.Prueba@Example.com",
            "empresa": "",
            "nit": None,
        },
    )

    print("UPSERT:", cliente)

    por_phone = await get_cliente(phone_id)
    print("POR PHONE:", por_phone)

    por_email = await get_cliente_por_email("cliente.prueba@example.com")
    print("POR EMAIL:", por_email)


if __name__ == "__main__":
    asyncio.run(main())