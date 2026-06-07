"""
test_flow_debug.py

Diagnóstico del flujo completo para comparar:
- Necesito una bomba centrifuga
- Necesito una bomba de agua

Objetivo:
Ver en qué punto se desvía el flujo antes de tocar más código.
"""

import asyncio
import json

import main


async def probar(texto: str, session_id: str):
    print("\n" + "=" * 100)
    print("TEXTO:", texto)
    print("=" * 100)

    print("\n1) _parece_solicitud_de_producto:")
    print(main._parece_solicitud_de_producto(texto))

    print("\n2) evaluar_necesidad:")
    necesidad = await main.evaluar_necesidad(texto)
    print(json.dumps(necesidad, ensure_ascii=False, indent=2, default=str))

    print("\n3) generar_queries_catalogo:")
    queries = await main.generar_queries_catalogo(texto)
    print(json.dumps(queries, ensure_ascii=False, indent=2, default=str))

    print("\n4) buscar_en_catalogo:")
    resultado_catalogo = await main.buscar_en_catalogo(texto)
    print(json.dumps({
        "estado": resultado_catalogo.get("estado"),
        "tipo": resultado_catalogo.get("tipo"),
        "razon": resultado_catalogo.get("razon"),
        "pregunta_sugerida": resultado_catalogo.get("pregunta_sugerida"),
        "query_catalogo": resultado_catalogo.get("query_catalogo"),
        "producto": {
            "codigo": (resultado_catalogo.get("producto") or {}).get("codigo"),
            "nombre": (resultado_catalogo.get("producto") or {}).get("nombre"),
            "marca": (resultado_catalogo.get("producto") or {}).get("marca"),
            "descripcion": (resultado_catalogo.get("producto") or {}).get("descripcion_corta"),
        } if resultado_catalogo.get("producto") else None,
    }, ensure_ascii=False, indent=2, default=str))

    print("\n5) procesar_turno:")
    respuesta = await main.procesar_turno(
        session_id=session_id,
        phone_id="573001234567",
        mensaje=texto,
    )
    print(json.dumps(respuesta, ensure_ascii=False, indent=2, default=str))


async def main_debug():
    await probar("Necesito una bomba centrifuga", "debug_flow_001")
    await probar("Necesito una bomba de agua", "debug_flow_002")


if __name__ == "__main__":
    asyncio.run(main_debug())
    