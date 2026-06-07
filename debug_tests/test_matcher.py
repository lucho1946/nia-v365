import asyncio

from catalog import buscar_por_texto
from product_matcher import validar_compatibilidad_producto


async def main():
    query = "bomba centrifuga"

    candidatos = await buscar_por_texto(query)

    print("Candidatos:", len(candidatos or []))

    decision = await validar_compatibilidad_producto(
        necesidad_cliente=query,
        candidatos=candidatos,
        contexto_tecnico={},
    )

    print("DECISIÓN:")
    print(decision.get("estado"))
    print("Confianza:", decision.get("confianza"))
    print("Razón:", decision.get("razon"))
    print("Pregunta sugerida:", decision.get("pregunta_sugerida"))

    producto = decision.get("producto")
    if producto:
        print("Producto:", producto.get("codigo"), producto.get("nombre"))


asyncio.run(main())
