"""
test_catalog.py

Prueba comparativa del buscador de catálogo.

Objetivo:
- Evaluar varias consultas reales contra products_catalog.
- Ver cuántos resultados llegan.
- Ver qué producto selecciona el scoring.
- Detectar si el catálogo devuelve producto principal o producto relacionado.

Este archivo es temporal de diagnóstico.
No hace parte del backend productivo.
"""

import asyncio

from catalog import buscar_por_texto, evaluar_coincidencia, formatear_producto


QUERIES = [
    {
        "query": "bomba",
        "campos": 1,
        "descripcion": "Consulta amplia"
    },
    {
        "query": "bomba de agua",
        "campos": 2,
        "descripcion": "Producto principal solicitado por cliente"
    },
    {
        "query": "bomba centrifuga",
        "campos": 2,
        "descripcion": "Tipo específico de bomba"
    },
    {
        "query": "bomba sumergible",
        "campos": 2,
        "descripcion": "Tipo específico de bomba"
    },
    {
        "query": "control presion bomba",
        "campos": 3,
        "descripcion": "Componente/control relacionado con bomba"
    },
    {
        "query": "interruptor presion bomba agua",
        "campos": 3,
        "descripcion": "Componente específico relacionado con bomba"
    },
    {
        "query": "medidor presion diferencial dwyer",
        "campos": 3,
        "descripcion": "Consulta que sabemos que existe en catálogo"
    },
    {
        "query": "control proceso pid 4-20 ma",
        "campos": 3,
        "descripcion": "Consulta técnica que sabemos que existe en catálogo"
    },
]


async def probar_query(query: str, campos: int, descripcion: str):
    print("\n" + "=" * 100)
    print("QUERY:", query)
    print("TIPO:", descripcion)
    print("=" * 100)

    resultados = await buscar_por_texto(query)

    print("Resultados encontrados:", len(resultados or []))

    if not resultados:
        print("No llegaron resultados desde MongoDB.")
        return

    print("\nTOP 5 CANDIDATOS:")
    for idx, producto in enumerate(resultados[:5], start=1):
        print("-" * 100)
        print(f"CANDIDATO #{idx}")
        print("Código:", producto.get("codigo"))
        print("Referencia:", producto.get("referencia"))
        print("Nombre:", producto.get("nombre"))
        print("Marca:", producto.get("marca"))
        print("Descripción corta:", producto.get("descripcion_corta"))
        print("Categoría:", producto.get("categoria"))
        print("Existencia:", producto.get("existencia"))
        print("Stock total:", producto.get("stock_total"))
        print("Precio:", producto.get("precio"))

    print("\nEVALUACIÓN DE COINCIDENCIA:")
    ok, producto = evaluar_coincidencia(
        resultados=resultados,
        query=query,
        campos=campos,
        marca_presente=False,
    )

    print("Coincidencia aceptada:", ok)

    if producto:
        print("\nPRODUCTO SELECCIONADO:")
        print(formatear_producto(producto))
        print("Score:", producto.get("_score"))
    else:
        print("Ningún producto superó el umbral actual.")


async def main():
    for item in QUERIES:
        await probar_query(
            query=item["query"],
            campos=item["campos"],
            descripcion=item["descripcion"],
        )


if __name__ == "__main__":
    asyncio.run(main())