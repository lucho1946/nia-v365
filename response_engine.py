"""
response_engine.py — Motor de respuesta segura para NIA v365.

Responsabilidad:
- Construir respuestas comerciales críticas usando datos reales.
- Evitar placeholders como [código], [nombre], [marca].
- No inventar productos.
- Separar la respuesta estructurada del razonamiento conversacional del LLM.

Regla:
Cuando hay producto real, la respuesta se construye desde backend.
El LLM no debe inventar ni completar campos de catálogo.
"""

from typing import Optional


PLACEHOLDER_TOKENS = [
    "[código]",
    "[codigo]",
    "[nombre]",
    "[marca]",
    "[descripción]",
    "[descripcion]",
    "{código}",
    "{codigo}",
    "{nombre}",
    "{marca}",
    "{descripción}",
    "{descripcion}",
]


def contiene_placeholder(texto: str) -> bool:
    """
    Detecta placeholders que nunca deben salir al cliente.
    """
    if not texto:
        return False

    texto_lower = texto.lower()
    return any(token in texto_lower for token in PLACEHOLDER_TOKENS)


def valor_visible(valor, fallback: str = "No disponible") -> str:
    """
    Convierte un valor de producto en texto visible y seguro.
    """
    if valor is None:
        return fallback

    texto = str(valor).strip()

    if not texto:
        return fallback

    return texto


def construir_bloque_producto(producto: dict) -> str:
    """
    Construye el bloque estándar de producto exigido por el flujo NIA.

    Campos mínimos:
    - Código
    - Referencia
    - Nombre
    - Marca
    - Descripción
    - Existencia
    - Stock total
    """
    codigo = valor_visible(producto.get("codigo"))
    referencia = valor_visible(producto.get("referencia"))
    nombre = valor_visible(producto.get("nombre"))
    marca = valor_visible(producto.get("marca"))
    descripcion = valor_visible(
        producto.get("descripcion_corta")
        or producto.get("descripcion")
        or producto.get("descripcion_larga")
    )
    existencia = valor_visible(producto.get("existencia"))
    stock_total = valor_visible(producto.get("stock_total"))

    return (
        f"Código: {codigo}\n"
        f"Referencia: {referencia}\n"
        f"Nombre: {nombre}\n"
        f"Marca: {marca}\n"
        f"Descripción: {descripcion}\n"
        f"Existencia: {existencia}\n"
        f"Stock total: {stock_total}"
    )


def respuesta_producto_encontrado(producto: dict, cliente: Optional[dict] = None) -> str:
    """
    Respuesta segura cuando el producto es compatible exacto.
    """
    nombre_cliente = ""
    if cliente and cliente.get("nombre"):
        nombre_cliente = f"{cliente['nombre']}, "

    bloque = construir_bloque_producto(producto)

    return (
        f"{nombre_cliente}encontré una opción del catálogo que puede cubrir tu necesidad:\n\n"
        f"{bloque}\n\n"
        "¿Este producto cubre lo que necesitas?"
    )


def respuesta_producto_relacionado(
    producto: dict,
    razon: Optional[str] = None,
    pregunta_sugerida: Optional[str] = None,
    cliente: Optional[dict] = None,
) -> str:
    """
    Respuesta segura cuando el producto existe, pero es relacionado y requiere confirmación.

    Regla:
    - No se presenta como solución final.
    - No se agrega al carrito automáticamente.
    - Se pide confirmación.
    """
    nombre_cliente = ""
    if cliente and cliente.get("nombre"):
        nombre_cliente = f"{cliente['nombre']}, "

    bloque = construir_bloque_producto(producto)

    razon_txt = razon or (
        "El producto encontrado está relacionado con la solicitud, "
        "pero no puedo confirmarlo como solución exacta sin validación."
    )

    pregunta = pregunta_sugerida or (
        "¿Buscas exactamente este tipo de producto relacionado o necesitas el equipo principal?"
    )

    return (
        f"{nombre_cliente}encontré un producto relacionado en el catálogo, "
        "pero necesito confirmar antes de tomarlo como solución:\n\n"
        f"{bloque}\n\n"
        f"Motivo: {razon_txt}\n\n"
        f"{pregunta}"
    )


def respuesta_sin_resultado(
    pregunta_sugerida: Optional[str] = None,
    cliente: Optional[dict] = None,
) -> str:
    """
    Respuesta segura cuando no hay coincidencia confiable.
    """
    nombre_cliente = ""
    if cliente and cliente.get("nombre"):
        nombre_cliente = f"{cliente['nombre']}, "

    pregunta = pregunta_sugerida or (
        "¿Puedes compartirme una referencia, marca, aplicación exacta o alguna especificación adicional?"
    )

    return (
        f"{nombre_cliente}no encontré una coincidencia suficientemente confiable en el catálogo "
        "con la información actual.\n\n"
        f"{pregunta}"
    )