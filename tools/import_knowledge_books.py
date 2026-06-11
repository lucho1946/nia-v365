"""
tools/import_knowledge_books.py

Importa los libros técnicos de NIA desde JSONL hacia MongoDB.

Objetivo:
- Cargar book_rag_ready_all.jsonl en la colección nia_knowledge_chunks.
- No subir los archivos pesados al repositorio.
- Permitir re-ejecución segura usando upsert por chunk_id.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict

# ============================================================
# Permite ejecutar este script desde la carpeta raíz source_v2.
#
# El archivo está dentro de tools/, pero memory.py está en source_v2/.
# Por eso agregamos el directorio padre al path de Python antes
# de importar get_db.
# ============================================================
ROOT_DIR = Path(__file__).resolve().parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from memory import get_db


COLLECTION_NAME = "nia_knowledge_chunks"


def _normalizar_chunk(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normaliza un registro del JSONL a un documento estable para MongoDB.

    No inventamos campos.
    Solo hacemos mapeo controlado:
    - id del JSONL pasa a chunk_id
    - se conserva metadata original
    - se agregan campos útiles para búsqueda y auditoría
    """
    chunk_id = raw.get("id")

    if not chunk_id:
        raise ValueError("Registro sin campo 'id'")

    return {
        "chunk_id": chunk_id,
        "source_type": raw.get("source_type"),
        "source_id": raw.get("source_id"),
        "title": raw.get("title"),
        "author": raw.get("author"),
        "edition": raw.get("edition"),
        "language": raw.get("language"),
        "page": raw.get("page"),
        "chapter": raw.get("chapter"),
        "section": raw.get("section"),
        "domain": raw.get("domain") or "general",
        "content_type": raw.get("content_type"),
        "text": raw.get("text") or "",
        "search_text": raw.get("search_text") or raw.get("text") or "",
        "metadata": raw.get("metadata") or {},
    }


async def ensure_indexes() -> None:
    """
    Crea índices necesarios para consultar los libros.
    """
    db = get_db()
    collection = db[COLLECTION_NAME]

    await collection.create_index("chunk_id", unique=True)
    await collection.create_index("source_id")
    await collection.create_index("domain")
    await collection.create_index("content_type")
    await collection.create_index([("search_text", "text"), ("text", "text")])


async def importar_jsonl(path: Path, batch_size: int = 500) -> None:
    """
    Importa un archivo JSONL usando bulk_write por lotes.
    """
    from pymongo import UpdateOne

    if not path.exists():
        raise FileNotFoundError(f"No existe el archivo: {path}")

    db = get_db()
    collection = db[COLLECTION_NAME]

    total = 0
    batch = []

    with path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                raw = json.loads(line)
                doc = _normalizar_chunk(raw)

                batch.append(
                    UpdateOne(
                        {"chunk_id": doc["chunk_id"]},
                        {"$set": doc},
                        upsert=True,
                    )
                )

            except Exception as e:
                print(f"[WARN] Línea {line_number} omitida: {e}")
                continue

            if len(batch) >= batch_size:
                result = await collection.bulk_write(batch, ordered=False)
                total += len(batch)
                print(
                    f"Importados/procesados: {total} "
                    f"| upserted={len(result.upserted_ids)} "
                    f"| modified={result.modified_count}"
                )
                batch = []

    if batch:
        result = await collection.bulk_write(batch, ordered=False)
        total += len(batch)
        print(
            f"Importados/procesados: {total} "
            f"| upserted={len(result.upserted_ids)} "
            f"| modified={result.modified_count}"
        )

    count = await collection.count_documents({})
    print("===================================")
    print("Importación finalizada")
    print("Colección:", COLLECTION_NAME)
    print("Documentos en colección:", count)
    print("===================================")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--file",
        required=True,
        help="Ruta al archivo book_rag_ready_all.jsonl",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Tamaño de lote para bulk_write",
    )

    args = parser.parse_args()

    path = Path(args.file)

    print("Archivo:", path)
    print("Existe:", path.exists())

    await ensure_indexes()
    await importar_jsonl(path, batch_size=args.batch_size)


if __name__ == "__main__":
    asyncio.run(main())