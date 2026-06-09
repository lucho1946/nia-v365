import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# ============================================================
# Permite importar módulos desde la raíz del proyecto source_v2
# y cargar variables de entorno locales.
# ============================================================
BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

load_dotenv(ENV_PATH)

print("BASE_DIR:", BASE_DIR)
print("ENV_PATH:", ENV_PATH)
print("MONGO_URI cargado:", bool(os.getenv("MONGO_URI")))
print("MONGO_DB:", os.getenv("MONGO_DB"))

from catalog import buscar_con_campos, evaluar_coincidencia


async def probar(texto: str):
    print("=" * 100)
    print("CONSULTA:", texto)

    resultados, campos_query = await buscar_con_campos(texto)

    print("CAMPOS QUERY:", campos_query)
    print("CANTIDAD RESULTADOS:", len(resultados) if resultados else 0)

    ok, producto = evaluar_coincidencia(
        resultados,
        texto,
        campos=len(campos_query) if campos_query else 1,
        campos_query=campos_query,
    )

    print("OK:", ok)

    if producto:
        print("CODIGO:", producto.get("codigo"))
        print("NOMBRE:", producto.get("nombre"))
        print("MARCA:", producto.get("marca"))
        print("SCORE:", producto.get("_score"))
        print("CAMPOS MATCH:", producto.get("_campos_match"))
    else:
        print("SIN PRODUCTO CONFIABLE")


async def main():
    await probar("Necesito un transmisor de presión de 0 a 10 bar con salida 4-20 mA")
    await probar("Necesito un sensor con cuerpo en acero inoxidable y conexión 1/2 NPT")
    await probar("Necesito un medidor con protección IP67 y alimentación 24 VDC")
    await probar("Necesito medir temperatura en agua caliente a 80 C")
    await probar("Necesito una bomba centrifuga")
    await probar("Necesito una bomba de agua")


if __name__ == "__main__":
    asyncio.run(main())