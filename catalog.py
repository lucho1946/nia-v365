"""
catalog.py — Consultas al catálogo real de ViaIndustrial en MongoDB Atlas.

Fuente oficial:
- Base de datos: MONGO_DB=nia
- Colección: products_catalog

Campos reales detectados:
- CODIGO
- REFERENCIA
- REF_ALTERNATIVA
- MARCA_LET
- DESCRIPCION_CORTA_PRE
- DESCRIPCION_LARGA_PRE
- NIVEL_0..NIVEL_4
- PRECIO_VENTA
- PV_FECHA
- STOCK_BOG
- STOCK_CALI
- STOCK_TOTAL
- VISIBLE_EN_LINEA
- EXISTENCIA
- texto_busqueda
- score_nia

Regla principal:
NIA no debe inventar productos. Si no hay coincidencia confiable,
debe pedir más información o indicar que no encontró una coincidencia suficiente.
"""

import logging
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Optional, Tuple

from memory import get_db


logger = logging.getLogger("nia.catalog")


# ============================================================
# CONFIGURACIÓN
# ============================================================

PRODUCTS_COLLECTION = "products_catalog"

DEFAULT_LIMIT = 30

# Umbrales internos.
# No son porcentajes de similitud textual pura, sino score compuesto.
UMBRAL_BASE = 0.55
UMBRAL_CON_CONTEXTO_TECNICO = 0.45


# ============================================================
# UTILIDADES DE TEXTO
# ============================================================

def _clean_text(value: Any) -> str:
    """
    Convierte un valor a texto limpio.
    """
    if value is None:
        return ""
    return str(value).strip()


def _normalize_text(value: Any) -> str:
    """
    Normaliza texto para comparación:
    - minúsculas;
    - sin tildes;
    - espacios compactos.
    """
    text = _clean_text(value).lower()

    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")

    text = re.sub(r"[^a-z0-9ñ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    return text


def _tokens(text: str) -> list[str]:
    """
    Extrae tokens útiles para búsqueda sin aplicar una lista rígida de stopwords.

    Regla profesional:
    - No eliminamos palabras por criterio semántico fijo.
    - Solo normalizamos texto.
    - Solo descartamos tokens demasiado cortos porque normalmente no aportan
      valor de búsqueda y generan mucho ruido técnico.
    
    Esto evita que NIA pierda contexto importante en búsquedas industriales.
    """
    normalized = _normalize_text(text)
    raw_tokens = re.findall(r"[a-z0-9ñ]+", normalized)

    return [
        token
        for token in raw_tokens
        if len(token) >= 3
    ]


def _first_value(prod: dict, keys: list[str], default: Any = "") -> Any:
    """
    Retorna el primer valor no vacío de una lista de campos posibles.
    """
    for key in keys:
        value = prod.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return default


def _sim(a: str, b: str) -> float:
    """
    Similitud textual básica.
    """
    a_norm = _normalize_text(a)
    b_norm = _normalize_text(b)

    if not a_norm or not b_norm:
        return 0.0

    return SequenceMatcher(None, a_norm, b_norm).ratio()


def _token_coverage(query_tokens: list[str], target_text: str) -> float:
    """
    Mide cuántos tokens importantes de la consulta aparecen en el texto destino.

    Ejemplo:
    query_tokens = ["bomba", "agua"]
    target_text = "interruptor de presión de bomba de agua"
    coverage = 1.0
    """
    if not query_tokens:
        return 0.0

    normalized_target = _normalize_text(target_text)

    if not normalized_target:
        return 0.0

    hits = sum(1 for token in query_tokens if token in normalized_target)

    return hits / len(query_tokens)


def _build_search_text(prod: dict) -> str:
    """
    Construye un texto compuesto del producto normalizado para scoring.
    """
    fields = [
        prod.get("codigo"),
        prod.get("referencia"),
        prod.get("ref_alternativa"),
        prod.get("marca"),
        prod.get("nombre"),
        prod.get("descripcion_corta"),
        prod.get("descripcion_larga"),
        prod.get("categoria"),
        prod.get("nivel_0"),
        prod.get("nivel_1"),
        prod.get("nivel_2"),
        prod.get("nivel_3"),
        prod.get("nivel_4"),
        prod.get("texto_busqueda"),
    ]

    return " ".join(_clean_text(f) for f in fields if _clean_text(f))


# ============================================================
# NORMALIZACIÓN
# ============================================================

def normalizar_producto(prod: dict) -> dict:
    """
    Normaliza un documento real de products_catalog al contrato interno de NIA.

    Contrato interno:
    - codigo
    - referencia
    - ref_alternativa
    - nombre
    - marca
    - descripcion_corta
    - descripcion_larga
    - categoria
    - precio
    - moneda
    - stock_total
    - existencia
    - visible_en_linea
    - texto_busqueda
    - score_nia
    - raw
    """
    codigo = _clean_text(_first_value(prod, ["CODIGO", "codigo"]))
    referencia = _clean_text(_first_value(prod, ["REFERENCIA", "referencia"]))
    ref_alternativa = _clean_text(_first_value(prod, ["REF_ALTERNATIVA", "ref_alternativa"]))

    marca = _clean_text(_first_value(prod, ["MARCA_LET", "marca", "MARCA"]))

    descripcion_corta = _clean_text(
        _first_value(
            prod,
            [
                "DESCRIPCION_CORTA_PRE",
                "descripcion_corta_pre",
                "DESCRIPCION_CORTA",
                "descripcion_corta",
            ],
        )
    )

    descripcion_larga = _clean_text(
        _first_value(
            prod,
            [
                "DESCRIPCION_LARGA_PRE",
                "descripcion_larga_pre",
                "DESCRIPCION_LARGA",
                "descripcion_larga",
            ],
        )
    )

    nivel_0 = _clean_text(_first_value(prod, ["NIVEL_0", "nivel_0"]))
    nivel_1 = _clean_text(_first_value(prod, ["NIVEL_1", "nivel_1"]))
    nivel_2 = _clean_text(_first_value(prod, ["NIVEL_2", "nivel_2"]))
    nivel_3 = _clean_text(_first_value(prod, ["NIVEL_3", "nivel_3"]))
    nivel_4 = _clean_text(_first_value(prod, ["NIVEL_4", "nivel_4"]))

    # En este catálogo no siempre hay NOMBRE_PRODUCTO.
    # Para NIA usamos como nombre comercial preferido:
    # NIVEL_4 > DESCRIPCION_CORTA_PRE > NIVEL_3 > REFERENCIA.
    nombre = _clean_text(
        _first_value(
            prod,
            [
                "NOMBRE_PRODUCTO",
                "nombre_producto",
                "NOMBRE",
                "nombre",
            ],
        )
    )

    if not nombre:
        nombre = nivel_4 or descripcion_corta or nivel_3 or referencia or codigo

    categoria = nivel_4 or nivel_3 or nivel_2 or nivel_1 or nivel_0

    precio = _first_value(prod, ["PRECIO_VENTA", "precio_venta", "PRECIO", "precio"], None)
    stock_total = _first_value(prod, ["STOCK_TOTAL", "stock_total", "STOCK", "stock"], None)
    stock_bog = _first_value(prod, ["STOCK_BOG", "stock_bog"], None)
    stock_cali = _first_value(prod, ["STOCK_CALI", "stock_cali"], None)

    visible = prod.get("VISIBLE_EN_LINEA")
    if visible is None:
        visible = prod.get("visible_en_linea", True)

    texto_busqueda = _clean_text(_first_value(prod, ["texto_busqueda", "TEXTO_BUSQUEDA"]))
    existencia = _clean_text(_first_value(prod, ["EXISTENCIA", "existencia"]))
    pv_fecha = _clean_text(_first_value(prod, ["PV_FECHA", "pv_fecha"]))
    score_nia = _first_value(prod, ["score_nia"], None)

    return {
        "codigo": codigo,
        "referencia": referencia,
        "ref_alternativa": ref_alternativa,
        "nombre": nombre,
        "marca": marca,
        "descripcion_corta": descripcion_corta,
        "descripcion_larga": descripcion_larga,
        "descripcion": descripcion_corta or descripcion_larga,
        "categoria": categoria,
        "nivel_0": nivel_0,
        "nivel_1": nivel_1,
        "nivel_2": nivel_2,
        "nivel_3": nivel_3,
        "nivel_4": nivel_4,
        "precio": precio,
        "moneda": "COP",
        "stock_total": stock_total,
        "stock_bog": stock_bog,
        "stock_cali": stock_cali,
        "visible_en_linea": bool(visible),
        "existencia": existencia,
        "pv_fecha": pv_fecha,
        "texto_busqueda": texto_busqueda,
        "score_nia": score_nia,
        "_raw": prod,
    }


# ============================================================
# BÚSQUEDA POR CÓDIGO / REFERENCIA
# ============================================================

async def buscar_por_codigo(codigo: str) -> Optional[dict]:
    """
    Busca por CODIGO, REFERENCIA o REF_ALTERNATIVA exactos.
    """
    valor = _clean_text(codigo)

    if not valor:
        return None

    db = get_db()
    collection = db[PRODUCTS_COLLECTION]

    valor_upper = valor.upper()

    query = {
        "$or": [
            {"CODIGO": valor},
            {"CODIGO": valor_upper},
            {"REFERENCIA": valor},
            {"REFERENCIA": valor_upper},
            {"REF_ALTERNATIVA": valor},
            {"REF_ALTERNATIVA": valor_upper},
        ]
    }

    prod = await collection.find_one(query, {"_id": 0})

    if not prod:
        logger.info("Producto no encontrado por código/referencia: %s", valor)
        return None

    normalizado = normalizar_producto(prod)
    normalizado["_match_type"] = "exacto_codigo_referencia"
    normalizado["_score"] = 1.0

    return normalizado


# ============================================================
# BÚSQUEDA POR TEXTO
# ============================================================

def _build_mongo_text_query(query: str) -> dict:
    """
    Construye una consulta MongoDB robusta usando texto_busqueda.

    Regla:
    - Con 1 token útil: busca ese token.
    - Con 2 o más tokens útiles: exige que al menos los tokens importantes
      aparezcan en texto_busqueda.

    Esto evita que "bomba de agua" traiga cualquier producto que solo diga "agua".
    """
    tokens = _tokens(query)

    if not tokens:
        return {}

    # Limitamos tokens para evitar consultas demasiado pesadas.
    tokens = tokens[:6]

    and_filters = []

    for token in tokens:
        safe = re.escape(token)
        and_filters.append(
            {
                "$or": [
                    {"texto_busqueda": {"$regex": safe, "$options": "i"}},
                    {"DESCRIPCION_CORTA_PRE": {"$regex": safe, "$options": "i"}},
                    {"DESCRIPCION_LARGA_PRE": {"$regex": safe, "$options": "i"}},
                    {"NIVEL_4": {"$regex": safe, "$options": "i"}},
                    {"NIVEL_3": {"$regex": safe, "$options": "i"}},
                    {"REFERENCIA": {"$regex": safe, "$options": "i"}},
                    {"MARCA_LET": {"$regex": safe, "$options": "i"}},
                ]
            }
        )

    base_filter = {
        "$and": and_filters
    }

    # Preferimos productos visibles, pero no bloqueamos al 100% por ahora
    # porque algunos productos internos pueden no estar visibles en línea.
    return base_filter


async def buscar_por_texto(query: str) -> Optional[list]:
    """
    Busca productos por texto en MongoDB usando products_catalog.

    Retorna lista de productos normalizados.
    """
    query = _clean_text(query)

    if not query:
        return None

    mongo_query = _build_mongo_text_query(query)

    if not mongo_query:
        return None

    db = get_db()
    collection = db[PRODUCTS_COLLECTION]

    cursor = collection.find(mongo_query, {"_id": 0}).limit(DEFAULT_LIMIT)
    docs = await cursor.to_list(length=DEFAULT_LIMIT)

    if not docs:
        logger.info("Sin resultados de catálogo para query: %s", query)
        return None

    normalizados = [normalizar_producto(doc) for doc in docs]

    logger.info(
        "Búsqueda catálogo query='%s' resultados=%s",
        query,
        len(normalizados),
    )

    return normalizados


# ============================================================
# SCORING
# ============================================================
def _contiene_indicador_accesorio(texto: str) -> bool:
    """
    Detecta si un producto parece ser accesorio, control, repuesto,
    kit, switch, interruptor o componente relacionado con otro equipo.

    Regla general:
    No se usa para bloquear siempre. Se usa para penalizar cuando el cliente
    pidió el equipo principal y el resultado parece ser solo un accesorio.
    """
    texto_norm = _normalize_text(texto)

    indicadores = [
        "control de",
        "control para",
        "switch",
        "interruptor",
        "kit para",
        "kit de",
        "repuesto",
        "accesorio",
        "modulo para",
        "tarjeta para",
        "soporte para",
        "base para",
        "cable para",
        "sensor para",
        "protector para",
        "arrancador para",
        "contactores para",
        "valvula para",
    ]

    return any(indicador in texto_norm for indicador in indicadores)


def _consulta_pide_accesorio(query: str) -> bool:
    """
    Determina si el usuario explícitamente pidió un accesorio/componente.

    Si el usuario pide "control de presión para bomba", sí podemos devolver
    controles. Si pide solo "bomba de agua", no deberíamos devolver controles
    como primera opción.
    """
    query_norm = _normalize_text(query)

    indicadores = [
        "control",
        "switch",
        "interruptor",
        "kit",
        "repuesto",
        "accesorio",
        "modulo",
        "tarjeta",
        "soporte",
        "base",
        "cable",
        "sensor",
        "protector",
        "arrancador",
        "contactor",
        "valvula",
    ]

    return any(indicador in query_norm for indicador in indicadores)


def _penalizacion_por_incompatibilidad(prod: dict, query: str) -> float:
    """
    Calcula penalización si el producto parece accesorio pero la consulta
    no pidió explícitamente un accesorio.

    Retorna:
    - 1.0: sin penalización.
    - 0.65: penalización moderada por posible accesorio.
    """
    if _consulta_pide_accesorio(query):
        return 1.0

    texto_producto = " ".join(
        [
            _clean_text(prod.get("nombre")),
            _clean_text(prod.get("descripcion_corta")),
            _clean_text(prod.get("descripcion_larga")),
            _clean_text(prod.get("categoria")),
        ]
    )

    if _contiene_indicador_accesorio(texto_producto):
        return 0.65

    return 1.0
def _score_producto(prod: dict, query: str) -> float:
    """
    Score compuesto para evaluar relevancia real.

    Componentes:
    - Cobertura de tokens en texto total: 45%
    - Cobertura de tokens en nombre/categoría: 25%
    - Similitud contra nombre: 15%
    - Similitud contra descripción corta: 10%
    - Score NIA previo: 5%

    Este enfoque es más robusto que SequenceMatcher puro porque el catálogo
    tiene nombres y descripciones largas.
    """
    query_tokens = _tokens(query)

    if not query_tokens:
        return 0.0

    texto_total = _build_search_text(prod)
    nombre_categoria = " ".join(
        [
            _clean_text(prod.get("nombre")),
            _clean_text(prod.get("categoria")),
            _clean_text(prod.get("nivel_4")),
            _clean_text(prod.get("nivel_3")),
        ]
    )

    coverage_total = _token_coverage(query_tokens, texto_total)
    coverage_nombre = _token_coverage(query_tokens, nombre_categoria)

    sim_nombre = _sim(query, prod.get("nombre", ""))
    sim_desc = _sim(query, prod.get("descripcion_corta", ""))

    raw_score_nia = prod.get("score_nia")
    try:
        score_nia_norm = min(float(raw_score_nia or 0) / 100, 1.0)
    except (TypeError, ValueError):
        score_nia_norm = 0.0

    score = (
        coverage_total * 0.45
        + coverage_nombre * 0.25
        + sim_nombre * 0.15
        + sim_desc * 0.10
        + score_nia_norm * 0.05
    )

    # Penalización leve si el producto no está visible en línea.
    # No lo descartamos automáticamente porque puede ser producto interno cotizable.
    if prod.get("visible_en_linea") is False:
        score *= 0.90

    return round(score, 4)


def evaluar_coincidencia(
    resultados: Optional[list],
    query: str,
    campos: int = 1,
    marca_presente: bool = False,
) -> Tuple[bool, Optional[dict]]:
    """
    Evalúa resultados y retorna UNO solo: el mejor producto confiable.

    Reglas:
    - Si no hay resultados, retorna False.
    - Si hay más contexto técnico, se permite un umbral menor.
    - No acepta productos sin nombre/categoría/descripción.
    """
    if not resultados:
        return False, None

    if campos >= 3:
        umbral = UMBRAL_CON_CONTEXTO_TECNICO
    elif campos == 2 or marca_presente:
        umbral = 0.50
    else:
        umbral = UMBRAL_BASE

    candidatos = []

    for producto in resultados:
        # Evita candidatos imposibles de explicar al cliente.
        if not producto.get("nombre") and not producto.get("descripcion_corta"):
            continue

        score = _score_producto(producto, query)
        candidatos.append((score, producto))

    if not candidatos:
        return False, None

    candidatos.sort(key=lambda x: x[0], reverse=True)

    mejor_score, mejor_prod = candidatos[0]

    logger.debug(
        "Mejor score catálogo: %.3f | umbral: %.2f | codigo: %s | nombre: %s",
        mejor_score,
        umbral,
        mejor_prod.get("codigo"),
        mejor_prod.get("nombre"),
    )

    if mejor_score >= umbral:
        mejor_prod["_score"] = mejor_score
        return True, mejor_prod

    return False, None


# ============================================================
# FORMATO DE RESPUESTA
# ============================================================

def formatear_producto(p: dict) -> str:
    """
    Formatea un producto para mostrarlo al cliente.

    Nunca usa placeholders tipo [código] o [marca].
    Si falta un dato real, muestra 'No disponible'.
    """
    codigo = p.get("codigo") or "No disponible"
    referencia = p.get("referencia") or "No disponible"
    nombre = p.get("nombre") or "No disponible"
    marca = p.get("marca") or "No disponible"
    desc = p.get("descripcion_corta") or p.get("descripcion") or "No disponible"
    existencia = p.get("existencia") or "No disponible"
    stock_total = p.get("stock_total")

    stock_texto = "No disponible"
    if stock_total is not None:
        stock_texto = str(stock_total)

    return (
        f"Código: {codigo}\n"
        f"Referencia: {referencia}\n"
        f"Nombre: {nombre}\n"
        f"Marca: {marca}\n"
        f"Descripción: {desc}\n"
        f"Existencia: {existencia}\n"
        f"Stock total: {stock_texto}"
    )