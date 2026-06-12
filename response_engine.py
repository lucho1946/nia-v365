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


def _numero_seguro(valor) -> float:
    """
    Convierte valores numéricos del catálogo a float de forma segura.

    Ejemplos aceptados:
    - 0
    - 0.0
    - "0"
    - "0.0"
    - "12"
    - "12.5"

    Si el valor no se puede convertir, retorna 0.0.
    """
    if valor is None:
        return 0.0

    try:
        texto = str(valor).strip().replace(",", ".")
        if not texto:
            return 0.0
        return float(texto)
    except (TypeError, ValueError):
        return 0.0


def construir_disponibilidad_producto(producto: dict) -> str:
    """
    Construye una línea comercial segura de disponibilidad.

    Regla de negocio:
    - stock_total > 0:
        Mostrar disponibilidad con stock.
    - stock_total == 0 y existencia tiene dato:
        Mostrar existencia como tiempo de entrega estimado.
        En este proyecto, el campo 'existencia' representa tiempo de entrega,
        no existencia física.
    - Sin datos confiables:
        Mostrar disponibilidad a confirmar.

    Importante:
    - No mostrar 'Stock total: 0.0' al cliente.
    - No llamar 'Existencia' a un campo que realmente indica tiempo de entrega.
    """
    stock_total_raw = producto.get("stock_total")
    stock_total = _numero_seguro(stock_total_raw)

    tiempo_entrega = valor_visible(producto.get("existencia"), fallback="").strip()

    if stock_total > 0:
        return f"Disponibilidad: stock disponible ({stock_total:g} unidades)"

    if tiempo_entrega:
        return f"Tiempo de entrega estimado: {tiempo_entrega}"

    return "Disponibilidad: a confirmar con asesor"


def construir_bloque_producto(producto: dict) -> str:
    """
    Construye el bloque estándar de producto exigido por el flujo NIA.

    Campos visibles:
    - Código
    - Referencia
    - Nombre
    - Marca
    - Descripción
    - Disponibilidad comercial segura

    Nota:
    El campo 'existencia' del catálogo se interpreta como tiempo de entrega,
    no como stock físico.
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
    disponibilidad = construir_disponibilidad_producto(producto)

    return (
        f"Código: {codigo}\n"
        f"Referencia: {referencia}\n"
        f"Nombre: {nombre}\n"
        f"Marca: {marca}\n"
        f"Descripción: {descripcion}\n"
        f"{disponibilidad}"
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