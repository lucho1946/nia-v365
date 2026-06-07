"""
inspect_catalog_schema.py

Inspecciona documentos reales de products_catalog para identificar:
- nombres reales de campos;
- campos útiles para búsqueda;
- campos útiles para normalización;
- estructura real del catálogo.

Este archivo es temporal de diagnóstico.
No forma parte del backend productivo.
"""

import asyncio
import json
from memory import get_db


PRODUCTS_COLLECTION = "products_catalog"


async def main():
    db = get_db()
    collection = db[PRODUCTS_COLLECTION]

    print("=" * 100)
    print("INSPECCIÓN DE products_catalog")
    print("=" * 100)

    total = await collection.count_documents({})
    print("Total documentos:", total)

    print("\n" + "=" * 100)
    print("DOCUMENTO CUALQUIERA")
    print("=" * 100)

    sample = await collection.find_one({}, {"_id": 0})

    if not sample:
        print("No hay documentos en products_catalog.")
        return

    print("Campos encontrados:")
    for key in sample.keys():
        print("-", key)

    print("\nDocumento completo:")
    print(json.dumps(sample, ensure_ascii=False, indent=2, default=str))

    print("\n" + "=" * 100)
    print("DOCUMENTOS QUE COINCIDEN CON 'bomba' O 'agua'")
    print("=" * 100)

    query = {
        "$or": [
            {"CODIGO": {"$regex": "bomba|agua", "$options": "i"}},
            {"REFERENCIA": {"$regex": "bomba|agua", "$options": "i"}},
            {"MARCA_LET": {"$regex": "bomba|agua", "$options": "i"}},
            {"NOMBRE_PRODUCTO": {"$regex": "bomba|agua", "$options": "i"}},
            {"DESCRIPCION": {"$regex": "bomba|agua", "$options": "i"}},
            {"DESCRIPCION_CORTA": {"$regex": "bomba|agua", "$options": "i"}},
            {"DESCRIPCION_LARGA": {"$regex": "bomba|agua", "$options": "i"}},
            {"NIVEL_0": {"$regex": "bomba|agua", "$options": "i"}},
            {"NIVEL_1": {"$regex": "bomba|agua", "$options": "i"}},
            {"NIVEL_2": {"$regex": "bomba|agua", "$options": "i"}},
            {"NIVEL_3": {"$regex": "bomba|agua", "$options": "i"}},
            {"NIVEL_4": {"$regex": "bomba|agua", "$options": "i"}},
        ]
    }

    docs = await collection.find(query, {"_id": 0}).limit(5).to_list(length=5)

    print("Resultados encontrados:", len(docs))

    for idx, doc in enumerate(docs, start=1):
        print("\n" + "-" * 100)
        print(f"DOCUMENTO MATCH #{idx}")
        print("-" * 100)

        print("Campos:")
        for key in doc.keys():
            print("-", key)

        print("\nDocumento:")
        print(json.dumps(doc, ensure_ascii=False, indent=2, default=str))


asyncio.run(main())