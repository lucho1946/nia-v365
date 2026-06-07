"""
memory.py — Persistencia de sesión en MongoDB Atlas para NIA v365.

Responsabilidades:
- Cargar variables de entorno desde .env.
- Conectar con MongoDB Atlas.
- Crear índices de sesión y TTL.
- Leer, guardar y eliminar sesiones conversacionales.
- Aislar las sesiones de NIA v365 en una colección propia.

Base de datos:
- MONGO_DB=nia

Colección usada:
- Por defecto: nia_v365_sessions

TTL:
- 8 días desde updated_at.
"""

import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient


# ============================================================
# CARGA SEGURA DEL .env
# ============================================================
# Cargamos explícitamente el .env ubicado en la misma carpeta
# que este archivo. Esto evita que memory.py lea variables vacías
# o valores por defecto antes de que main.py cargue el entorno.
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

load_dotenv(dotenv_path=ENV_PATH)


# ============================================================
# CONFIGURACIÓN MONGODB
# ============================================================
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "nia")

# Colección propia para NIA v365.
# No usamos "sessions" para no chocar con datos históricos del proyecto anterior.
SESSIONS_COLLECTION = os.getenv("MONGO_SESSIONS_COLLECTION", "nia_v365_sessions")

TTL_SEGUNDOS = 60 * 60 * 24 * 8  # 8 días
MAX_TURNOS = 40
MAX_PRODUCTOS_ACUMULADOS = 20

_client: Optional[AsyncIOMotorClient] = None


def _validar_configuracion_mongo() -> None:
    """
    Valida que exista la configuración mínima de MongoDB.

    Sin esta validación, Motor/PyMongo puede intentar conectarse por defecto a
    localhost:27017, lo cual genera errores confusos.
    """
    if not MONGO_URI:
        raise RuntimeError(
            "Falta MONGO_URI en el archivo .env. "
            "Configura la cadena real de MongoDB Atlas."
        )

    if not MONGO_DB:
        raise RuntimeError(
            "Falta MONGO_DB en el archivo .env. "
            "Para este proyecto debe ser, por ahora: MONGO_DB=nia."
        )

    if not SESSIONS_COLLECTION:
        raise RuntimeError(
            "Falta MONGO_SESSIONS_COLLECTION o está vacío. "
            "Usa por defecto: MONGO_SESSIONS_COLLECTION=nia_v365_sessions."
        )


def get_db():
    """
    Retorna la base de datos MongoDB configurada.

    La conexión se crea una sola vez y se reutiliza durante toda la ejecución
    de la aplicación.
    """
    global _client

    _validar_configuracion_mongo()

    if _client is None:
        _client = AsyncIOMotorClient(MONGO_URI)

    return _client[MONGO_DB]


def get_sessions_collection():
    """
    Retorna la colección oficial de sesiones para NIA v365.

    Usar esta función evita escribir db.sessions en varias partes del archivo
    y nos permite cambiar el nombre de la colección desde .env.
    """
    db = get_db()
    return db[SESSIONS_COLLECTION]


async def ensure_index():
    """
    Crea los índices necesarios para sesiones de NIA v365.

    Índices:
    - idx_ttl_updated_at:
      elimina automáticamente sesiones después de 8 días sin actualización.

    - idx_unique_session_id:
      evita duplicados por session_id.

    Importante:
    El índice único usa partialFilterExpression para aplicar unicidad solo
    cuando session_id sea string. Esto evita errores con documentos antiguos
    o corruptos que tengan session_id null.
    """
    collection = get_sessions_collection()

    await collection.create_index(
        [("updated_at", 1)],
        expireAfterSeconds=TTL_SEGUNDOS,
        background=True,
        name="idx_ttl_updated_at",
    )

    await collection.create_index(
        [("session_id", 1)],
        unique=True,
        background=True,
        name="idx_unique_session_id",
        partialFilterExpression={"session_id": {"$type": "string"}},
    )


async def get_session(session_id: str) -> Optional[dict]:
    """
    Obtiene una sesión por session_id.

    Retorna:
    - dict si existe la sesión.
    - None si no existe.
    """
    collection = get_sessions_collection()

    return await collection.find_one(
        {"session_id": session_id},
        {"_id": 0},
    )


async def save_session(
    session_id: str,
    turnos: list,
    etapa: str,
    phone_id: Optional[str] = None,
    archivo_activo: Optional[dict] = None,
    necesidad_ctx: Optional[dict] = None,
    cliente: Optional[dict] = None,
    productos_acumulados: Optional[list] = None,
):
    """
    Guarda o actualiza una sesión conversacional.

    Reglas:
    - Conserva máximo los últimos 40 turnos.
    - Conserva máximo los últimos 20 productos acumulados.
    - Reinicia el TTL actualizando updated_at.
    """
    if not session_id:
        raise ValueError("session_id es obligatorio para guardar una sesión.")

    collection = get_sessions_collection()

    payload = {
        "session_id": session_id,
        "phone_id": phone_id,
        "turnos": (turnos or [])[-MAX_TURNOS:],
        "etapa": etapa,
        "updated_at": datetime.now(timezone.utc),
        "necesidad_ctx": necesidad_ctx or {},
        "cliente": cliente or {},
        "productos_acumulados": (productos_acumulados or [])[-MAX_PRODUCTOS_ACUMULADOS:],
    }

    if archivo_activo is not None:
        payload["archivo_activo"] = archivo_activo

    await collection.update_one(
        {"session_id": session_id},
        {"$set": payload},
        upsert=True,
    )


async def delete_session(session_id: str):
    """
    Elimina una sesión manualmente si el cliente pide reiniciar.
    """
    if not session_id:
        raise ValueError("session_id es obligatorio para eliminar una sesión.")

    collection = get_sessions_collection()

    await collection.delete_one({"session_id": session_id})