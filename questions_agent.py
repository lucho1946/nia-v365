"""
questions_agent.py — Agente de 3 preguntas estratégicas
Solo se activa cuando búsqueda 1→5 falla completamente.
Usa los libros de Creus y Kuphaldt. No hace nada más.
"""
import os
import httpx
from knowledge import contexto_para_agente

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

SYSTEM_AGENTE = """Eres un agente técnico especializado en instrumentación industrial.
Tu ÚNICA función es generar exactamente 3 preguntas estratégicas para identificar
el producto correcto que necesita el cliente.

REGLAS ESTRICTAS:
- Genera EXACTAMENTE 3 preguntas. Ni más, ni menos.
- Las preguntas deben ser técnicas, concretas y cortas.
- Cada pregunta debe ayudar a determinar una variable diferente del producto.
- NO saludes, NO expliques, NO cotices, NO recomiendes productos.
- NO hagas preguntas genéricas como ¿para qué lo usas?
- Basa las preguntas en las variables técnicas del dominio detectado.
- Responde SOLO con las 3 preguntas numeradas. Nada más."""

async def generar_preguntas(texto_cliente: str) -> list:
    ctx = contexto_para_agente(texto_cliente)
    contexto_libros = f"Dominio: {ctx['dominio']}\n"
    if ctx.get("terminos"):
        contexto_libros += f"Términos técnicos: {', '.join(ctx['terminos'])}\n"
    if ctx.get("extractos"):
        for e in ctx["extractos"][:2]:
            contexto_libros += f"- {e[:300]}\n"

    prompt = (
        f"El cliente necesita: \"{texto_cliente}\"\n\n"
        f"{contexto_libros}\n"
        f"Genera exactamente 3 preguntas técnicas para identificar el producto."
    )

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                json={"model": "gpt-4o-mini", "max_tokens": 300, "temperature": 0.2,
                      "messages": [{"role": "system", "content": SYSTEM_AGENTE},
                                   {"role": "user",   "content": prompt}]},
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}
            )
            texto = r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return [
            "¿Cuál es el rango de operación que necesitas medir?",
            "¿Qué tipo de señal de salida requieres (4-20 mA, Modbus, HART)?",
            "¿Cuál es el fluido o proceso donde se instalará?"
        ]

    preguntas = []
    for linea in texto.splitlines():
        linea = linea.strip()
        for prefijo in ["1.","2.","3.","1)","2)","3)","•","-","*"]:
            if linea.startswith(prefijo):
                linea = linea[len(prefijo):].strip()
                break
        if linea and len(linea) > 5:
            preguntas.append(linea)

    if len(preguntas) >= 3:
        return preguntas[:3]
    while len(preguntas) < 3:
        preguntas.append("¿Tienes alguna especificación técnica adicional?")
    return preguntas
