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

async def buscar_con_campos(texto: str) -> Tuple[Optional[list], dict]:
    """
    Búsqueda técnica sobre MongoDB.

    Objetivo:
    - Extraer campos técnicos del mensaje del cliente.
    - Generar consultas limpias para el catálogo.
    - Mantener MongoDB como fuente oficial.
    - No depender de API externa.
    - No usar listas cerradas de productos o marcas.

    Importante:
    Esta función NO decide compatibilidad final.
    Solo recupera candidatos y entrega campos_query para scoring.
    """
    texto_original = _clean_text(texto)

    if not texto_original:
        return None, {}

    campos_query = _extraer_campos_query(texto_original)

    def _normalizar_query_catalogo(valor: str) -> str:
        """
        Limpia una frase conversacional y deja términos útiles de búsqueda.

        Esto no es una lista de productos. Son palabras funcionales genéricas
        del lenguaje que no aportan al catálogo.
        """
        valor_norm = _normalize_text(valor)

        # Quitamos valores técnicos ya detectados para que no dominen
        # la búsqueda textual.
        for campo_valor in campos_query.values():
            campo_valor_norm = _normalize_text(str(campo_valor))
            if campo_valor_norm:
                valor_norm = valor_norm.replace(campo_valor_norm, " ")

        tokens = [
            token
            for token in valor_norm.split()
            if len(token) > 2
            and token not in {
                "necesito",
                "necesitamos",
                "quiero",
                "requiere",
                "requiero",
                "buscar",
                "busco",
                "para",
                "con",
                "una",
                "uno",
                "del",
                "los",
                "las",
                "que",
                "por",
                "favor",
                "equipo",
                "producto",
                "sistema",
                "proceso",
            }
        ]

        return " ".join(tokens).strip()

    queries = []

    # 1. Consulta limpia principal.
    query_limpia = _normalizar_query_catalogo(texto_original)

    if query_limpia:
        queries.append(query_limpia)

    # 2. Consulta original como fallback.
    if texto_original and texto_original not in queries:
        queries.append(texto_original)

    # 3. Consulta corta con los primeros términos útiles.
    if query_limpia:
        tokens_base = query_limpia.split()[:4]
        query_base = " ".join(tokens_base).strip()

        if query_base and query_base not in queries:
            queries.append(query_base)

    # 4. Consulta técnica como último fallback.
    if campos_query and query_limpia:
        valores_tecnicos = [
            str(v).strip()
            for v in campos_query.values()
            if isinstance(v, str) and str(v).strip()
        ]

        if valores_tecnicos:
            query_tecnica = f"{query_limpia} {' '.join(valores_tecnicos[:2])}".strip()

            if query_tecnica and query_tecnica not in queries:
                queries.append(query_tecnica)

    logger.info(
        "buscar_con_campos texto='%s' campos=%s queries=%s",
        texto_original,
        campos_query,
        queries,
    )

    for query in queries:
        resultados = await buscar_por_texto(query)

        if resultados:
            logger.info(
                "buscar_con_campos encontró %s resultados con query='%s'",
                len(resultados),
                query,
            )
            return resultados, campos_query

    logger.info(
        "buscar_con_campos sin resultados texto='%s' queries=%s",
        texto_original,
        queries,
    )

    return None, campos_query

# ============================================================
# SCORING
# ============================================================
def _parsear_campos(desc_larga: str) -> dict:
    """
    Extrae campos estructurados desde descripcion_larga.

    Formato esperado en catálogo:
    ¦ campo: valor ¦ campo: valor

    Ejemplo:
    ¦ rango: 0 a 10 bar ¦ salida: 4-20 mA ¦ conexion: 1/2 NPT
    """
    campos = {}

    if not desc_larga or "¦" not in desc_larga:
        return campos

    for parte in str(desc_larga).split("¦"):
        parte = parte.strip()

        if ":" not in parte:
            continue

        campo, valor = parte.split(":", 1)
        campo = _normalize_text(campo)
        valor = str(valor).strip()

        if 2 < len(campo) < 50 and valor:
            campos[campo] = valor

    return campos


def _score_campos_estructurados(
    campos_producto: dict,
    campos_query: dict,
) -> float:
    """
    Compara campos técnicos detectados en la consulta del cliente contra
    campos estructurados presentes en la descripción larga del producto.

    Retorna un promedio entre 0.0 y 1.0.
    """
    if not campos_producto or not campos_query:
        return 0.0

    scores = []

    for campo_q, valor_q in campos_query.items():
        campo_q_norm = _normalize_text(campo_q)
        valor_q_norm = _normalize_text(valor_q)

        if not campo_q_norm or not valor_q_norm:
            continue

        for campo_p, valor_p in campos_producto.items():
            campo_p_norm = _normalize_text(campo_p)
            valor_p_norm = _normalize_text(valor_p)

            # Coincidencia flexible de nombre de campo.
            # Ejemplo: "salida" puede coincidir con "señal de salida".
            if campo_q_norm in campo_p_norm or campo_p_norm in campo_q_norm:
                scores.append(_sim(valor_q_norm, valor_p_norm))
                break

    if not scores:
        return 0.0

    return sum(scores) / len(scores)

def _extraer_campos_query(texto: str) -> dict:
    """
    Extrae valores técnicos desde el mensaje del cliente usando patrones generales.

    Regla de diseño:
    - No depende de listas cerradas de materiales, marcas o protocolos.
    - Detecta estructuras comunes: rangos, unidades, conexión, salida/señal,
      material declarado, protección IP y voltaje.
    - No reemplaza al product_matcher; solo aporta contexto técnico para mejorar scoring.
    """
    campos = {}
    t_original = _clean_text(texto)
    t = t_original.lower()

    # ------------------------------------------------------------
    # 1. Rangos numéricos con unidades técnicas
    # Ejemplos:
    # - 0 a 10 bar
    # - 0-100 psi
    # - 10 a 80 °C
    # - 50 l/min
    # ------------------------------------------------------------
    rango = re.search(
        r"(\d+[\.,]?\d*)\s*(?:a|-|hasta)\s*(\d+[\.,]?\d*)\s*"
        r"([a-zA-Z°/%\.]+(?:/[a-zA-Z]+)?)",
        t,
        re.IGNORECASE,
    )

    if rango:
        inicio, fin, unidad = rango.groups()
        campos["rango"] = f"{inicio} a {fin} {unidad}"

    # Valor único con unidad cuando no hay rango.
    # Ejemplo: 80 C, 24 VDC, 50 l/min, 10 bar.
    valor_unico = re.search(
        r"\b(\d+[\.,]?\d*)\s*"
        r"(bar|psi|mbar|kpa|mpa|pa|°c|°f|c|f|v|vac|vdc|ma|a|gpm|lpm|l/min|m3/h|hz)\b",
        t,
        re.IGNORECASE,
    )

    if valor_unico and "rango" not in campos:
        valor, unidad = valor_unico.groups()
        unidad_norm = unidad.upper() if unidad in ["c", "f"] else unidad
        campos["valor_tecnico"] = f"{valor} {unidad_norm}"

    # ------------------------------------------------------------
    # 2. Señal / salida / comunicación / protocolo
    # Extrae lo que venga después de palabras técnicas.
    # Ejemplos:
    # - salida 4-20 mA
    # - señal 0-10 V
    # - protocolo Modbus RTU
    # - comunicación IO-Link
    # ------------------------------------------------------------
    salida = re.search(
        r"\b(?:salida|señal|senal|protocolo|comunicacion|comunicación)\s+"
        r"([a-zA-Z0-9\-\s/\.]+)",
        t_original,
        re.IGNORECASE,
    )

    if salida:
        valor = salida.group(1).strip()
        valor = re.split(r"[,.;\n]| con | para | y ", valor, maxsplit=1)[0].strip()

        if valor:
            campos["salida"] = valor

    # Si el cliente no escribió la palabra salida/señal, pero sí un patrón eléctrico claro.
    salida_patron = re.search(
        r"\b(\d+[\.,]?\d*)\s*-\s*(\d+[\.,]?\d*)\s*(ma|v)\b",
        t,
        re.IGNORECASE,
    )

    if salida_patron and "salida" not in campos:
        inicio, fin, unidad = salida_patron.groups()
        campos["salida"] = f"{inicio}-{fin} {unidad}"

    # ------------------------------------------------------------
    # 3. Conexión mecánica
    # Ejemplos:
    # - 1/2 NPT
    # - 3/4 BSP
    # - conexión roscada
    # - conexión bridada
    # ------------------------------------------------------------
    conexion_medida = re.search(
        r"\b(\d+/\d+|\d+\.\d+|\d+)\s*(?:\"|''|pulg|pulgada|pulgadas)?\s*"
        r"([a-zA-Z]{2,10})\b",
        t,
        re.IGNORECASE,
    )

    if conexion_medida:
        medida, tipo = conexion_medida.groups()

        # Evitamos capturar cualquier unidad eléctrica como conexión.
        if tipo.lower() not in {"v", "vac", "vdc", "ma", "a", "hz", "bar", "psi"}:
            campos["conexion"] = f"{medida} {tipo}"

    conexion_texto = re.search(
        r"\bconexion\s+([a-zA-Z0-9\-\s/\.]+)",
        t_original,
        re.IGNORECASE,
    )

    if conexion_texto and "conexion" not in campos:
        valor = conexion_texto.group(1).strip()
        valor = re.split(r"[,.;\n]| con | para | y ", valor, maxsplit=1)[0].strip()

        if valor:
            campos["conexion"] = valor

    # ------------------------------------------------------------
    # 4. Material declarado explícitamente
    # No usamos lista cerrada. Solo extraemos si el usuario lo declara.
    # Ejemplos:
    # - material acero inoxidable
    # - cuerpo en bronce
    # - fabricado en PVC
    # ------------------------------------------------------------
    material = re.search(
        r"\b(?:material|cuerpo en|fabricado en|construido en|en material)\s+"
        r"([a-zA-Z0-9áéíóúñÁÉÍÓÚÑ\-\s/\.]+)",
        t_original,
        re.IGNORECASE,
    )

    if material:
        valor = material.group(1).strip()
        valor = re.split(r"[,.;\n]| con | para | y ", valor, maxsplit=1)[0].strip()

        if valor:
            campos["material"] = valor

    # ------------------------------------------------------------
    # 5. Protección IP
    # Ejemplos:
    # - IP65
    # - ip 67
    # ------------------------------------------------------------
    ip = re.search(r"\bip\s*(\d{2})\b", t, re.IGNORECASE)

    if ip:
        campos["proteccion"] = f"IP{ip.group(1)}"

    # ------------------------------------------------------------
    # 6. Voltaje / alimentación
    # Ejemplos:
    # - 24 VDC
    # - 110 VAC
    # - alimentación 220V
    # ------------------------------------------------------------
    voltaje = re.search(
        r"\b(\d+[\.,]?\d*)\s*(v|vac|vdc)\b",
        t,
        re.IGNORECASE,
    )

    if voltaje:
        valor, unidad = voltaje.groups()
        campos["voltaje"] = f"{valor} {unidad.upper()}"

    alimentacion = re.search(
        r"\b(?:alimentacion|alimentación)\s+([a-zA-Z0-9\-\s/\.]+)",
        t_original,
        re.IGNORECASE,
    )

    if alimentacion and "voltaje" not in campos:
        valor = alimentacion.group(1).strip()
        valor = re.split(r"[,.;\n]| con | para | y ", valor, maxsplit=1)[0].strip()

        if valor:
            campos["voltaje"] = valor

    return campos

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

def _score_producto(
    prod: dict,
    query: str,
    campos_query: Optional[dict] = None,
) -> float:
    """
    Score compuesto para evaluar relevancia real.

    Mantiene el scoring estable actual basado en cobertura textual y agrega,
    de forma opcional, scoring por campos técnicos estructurados.

    Si no hay campos_query, el comportamiento sigue siendo el mismo.
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

    score_textual = (
        coverage_total * 0.45
        + coverage_nombre * 0.25
        + sim_nombre * 0.15
        + sim_desc * 0.10
        + score_nia_norm * 0.05
    )

    score_campos = 0.0

    if campos_query:
        descripcion_larga = _clean_text(prod.get("descripcion_larga"))
        campos_producto = _parsear_campos(descripcion_larga)
        score_campos = _score_campos_estructurados(campos_producto, campos_query)

    if campos_query and score_campos > 0:
        # El texto sigue pesando más porque no todos los productos tienen
        # descripción larga estructurada con separador ¦.
        score = (score_textual * 0.70) + (score_campos * 0.30)
    else:
        score = score_textual

    if prod.get("visible_en_linea") is False:
        score *= 0.90

    return round(score, 4)


def evaluar_coincidencia(
    resultados: Optional[list],
    query: str,
    campos: int = 1,
    marca_presente: bool = False,
    campos_query: Optional[dict] = None,
) -> Tuple[bool, Optional[dict]]:
    """
    Evalúa resultados y retorna UNO solo: el mejor producto confiable.

    Reglas:
    - Si no hay resultados, retorna False.
    - Si hay más contexto técnico, permite un umbral menor.
    - Si hay campos técnicos detectados, se usa scoring estructurado adicional.
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

    if campos_query:
        cantidad_campos_query = len(campos_query)

        if cantidad_campos_query >= 3:
            umbral = min(umbral, 0.45)
        elif cantidad_campos_query == 2:
            umbral = min(umbral, 0.50)

    candidatos = []

    for producto in resultados:
        if not producto.get("nombre") and not producto.get("descripcion_corta"):
            continue

        score = _score_producto(
            producto,
            query,
            campos_query=campos_query,
        )

        candidatos.append((score, producto))

    if not candidatos:
        return False, None

    candidatos.sort(key=lambda x: x[0], reverse=True)

    mejor_score, mejor_prod = candidatos[0]

    logger.debug(
        "Mejor score catálogo: %.3f | umbral: %.2f | codigo: %s | nombre: %s | campos_query: %s",
        mejor_score,
        umbral,
        mejor_prod.get("codigo"),
        mejor_prod.get("nombre"),
        list(campos_query.keys()) if campos_query else [],
    )

    if mejor_score >= umbral:
        mejor_prod["_score"] = mejor_score
        mejor_prod["_campos_match"] = list(campos_query.keys()) if campos_query else []
        return True, mejor_prod

    return False, None


# ============================================================
# FORMATO DE RESPUESTA
# ============================================================

def _numero_seguro_catalogo(valor) -> float:
    """
    Convierte un valor de stock a float de forma segura.
    Si no se puede convertir, retorna 0.0.
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


def _disponibilidad_catalogo(p: dict) -> str:
    """
    Construye disponibilidad visible para respuestas originadas desde catalog.py.

    En la data actual, 'existencia' representa tiempo de entrega.
    No debe mostrarse como existencia física.
    """
    stock_total = _numero_seguro_catalogo(p.get("stock_total"))
    tiempo_entrega = str(p.get("existencia") or "").strip()

    if stock_total > 0:
        return f"Disponibilidad: stock disponible ({stock_total:g} unidades)"

    if tiempo_entrega:
        return f"Tiempo de entrega estimado: {tiempo_entrega}"

    return "Disponibilidad: a confirmar con asesor"

# ============================================================
# DETECCIÓN GENERAL DE TIPOS POR TAXONOMÍA DE CATÁLOGO
# ============================================================

CAMPOS_TAXONOMIA_PRODUCTO = [
    "nivel_0",
    "nivel_1",
    "nivel_2",
    "nivel_3",
    "nivel_4",
    "categoria",
]


CAMPOS_TAXONOMIA_PRIORIDAD = [
    # Primero intentamos niveles más generales y estables.
    "nivel_0",
    "nivel_1",
    "nivel_2",
    # Luego niveles más específicos.
    "nivel_3",
    "nivel_4",
    "categoria",
]


PALABRAS_FUNCIONALES_TAXONOMIA = {
    "de",
    "del",
    "la",
    "el",
    "los",
    "las",
    "un",
    "una",
    "uno",
    "unos",
    "unas",
    "para",
    "por",
    "con",
    "sin",
    "en",
    "y",
    "o",
    "a",
}


def _normalizar_taxonomia_texto(valor: str) -> str:
    """
    Normaliza texto de taxonomía para comparación.

    Importante:
    - No se usa para mostrar al cliente.
    - Solo sirve para comparar, filtrar y agrupar.
    """
    if not valor:
        return ""

    texto = str(valor).lower().strip()

    reemplazos = {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ü": "u",
        "ñ": "n",
    }

    for origen, destino in reemplazos.items():
        texto = texto.replace(origen, destino)

    texto = texto.replace("-", " ")
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()

    return texto


def _singular_taxonomia(token: str) -> str:
    """
    Singularización básica para comparar familias:
    - valvulas -> valvula
    - termometros -> termometro
    - transmisores -> transmisor

    No pretende ser lingüística perfecta.
    Solo reduce variaciones comunes del catálogo.
    """
    token = (token or "").strip().lower()

    if len(token) > 5 and token.endswith("es"):
        return token[:-2]

    if len(token) > 4 and token.endswith("s"):
        return token[:-1]

    return token


def _tokens_taxonomia(valor: str) -> list[str]:
    """
    Extrae tokens útiles desde texto de taxonomía.
    No toma tokens desde descripción comercial libre.
    """
    texto = _normalizar_taxonomia_texto(valor)

    tokens = []

    for token in texto.split():
        token = _singular_taxonomia(token)

        if len(token) < 4:
            continue

        if token.isdigit():
            continue

        if token in PALABRAS_FUNCIONALES_TAXONOMIA:
            continue

        tokens.append(token)

    return tokens


def _valor_campo_producto(producto: dict, campo: str):
    """
    Obtiene un campo de producto tolerando variaciones de mayúsculas/minúsculas.
    """
    if not isinstance(producto, dict):
        return None

    return (
        producto.get(campo)
        or producto.get(campo.upper())
        or producto.get(campo.lower())
    )


def _texto_taxonomico_producto(producto: dict) -> str:
    """
    Construye un texto solo con campos taxonómicos del producto.
    No usa nombre ni descripción para decidir tipos.
    """
    partes = []

    for campo in CAMPOS_TAXONOMIA_PRODUCTO:
        valor = _valor_campo_producto(producto, campo)
        if valor:
            partes.append(str(valor))

    return " ".join(partes)


def _producto_relacionado_con_familia(
    producto: dict,
    texto_cliente: str,
) -> bool:
    """
    Determina si un producto pertenece realmente a la familia consultada.

    Regla general:
    - No basta con que la palabra del cliente aparezca en cualquier parte.
    - La familia consultada debe aparecer al inicio de una rama taxonómica.
    - Esto evita aceptar productos como:
      "Transmisores controladores de válvulas" cuando el cliente pidió "válvula".
      "Switches de presión para bombas" cuando el cliente pidió "bomba".

    Esta lógica no es por producto específico; depende de la estructura
    jerárquica del catálogo.
    """
    tokens_cliente = [
        _singular_taxonomia(token)
        for token in _tokens_taxonomia(texto_cliente)
    ]

    if not tokens_cliente:
        return False

    # Para consultas cortas como "valvula", "termometro", "bomba",
    # usamos el primer token como familia principal.
    familia_principal = tokens_cliente[0]

    campos_prioritarios = [
        "nivel_0",
        "nivel_1",
        "nivel_2",
        "nivel_3",
        "nivel_4",
        "categoria",
    ]

    for campo in campos_prioritarios:
        valor = _valor_campo_producto(producto, campo)

        if not valor:
            continue

        texto_norm = _normalizar_taxonomia_texto(str(valor))
        tokens_valor = [
            _singular_taxonomia(token)
            for token in texto_norm.split()
            if token.strip()
        ]

        if not tokens_valor:
            continue

        primer_token = tokens_valor[0]

        if primer_token == familia_principal:
            return True

        if texto_norm.startswith(familia_principal + " "):
            return True

    return False


def _filtrar_productos_por_familia(
    texto_cliente: str,
    productos: list[dict],
) -> list[dict]:
    """
    Filtra candidatos para quedarse con productos cuya taxonomía sí pertenece
    a la familia escrita por el cliente.
    """
    if not productos:
        return []

    filtrados = [
        producto
        for producto in productos
        if _producto_relacionado_con_familia(producto, texto_cliente)
    ]

    return filtrados


def _limpiar_valor_taxonomico(valor: str) -> str:
    """
    Limpia un valor de taxonomía para mostrarlo como opción al cliente.
    """
    if not valor:
        return ""

    texto = str(valor).strip()
    texto = re.sub(r"\s+", " ", texto).strip()

    return texto


def _valor_taxonomico_producto(producto: dict, campo: str) -> str:
    """
    Devuelve un valor taxonómico limpio de un producto.
    """
    valor = _valor_campo_producto(producto, campo)

    if not valor:
        return ""

    return _limpiar_valor_taxonomico(str(valor))


def _resumir_opcion_taxonomica(
    texto_cliente: str,
    valor_taxonomico: str,
) -> str:
    """
    Convierte una ruta taxonómica en una opción más limpia para el cliente.

    Ejemplo:
    - texto_cliente: "termometro"
    - valor_taxonomico: "Termometros digitales portatiles de bolsillo"
    - salida: "digitales portátiles de bolsillo"

    No inventa. Solo remueve la familia principal ya escrita por el cliente.
    """
    valor_original = _limpiar_valor_taxonomico(valor_taxonomico)

    if not valor_original:
        return ""

    texto_norm = _normalizar_taxonomia_texto(valor_original)

    tokens_cliente = {
        _singular_taxonomia(token)
        for token in _tokens_taxonomia(texto_cliente)
    }

    tokens_salida = []

    for token in texto_norm.split():
        token_base = _singular_taxonomia(token)

        if token_base in PALABRAS_FUNCIONALES_TAXONOMIA:
            continue

        # Removemos la familia principal escrita por el cliente.
        # Ejemplo: "termometros" cuando el cliente escribió "termometro".
        if token_base in tokens_cliente:
            continue

        tokens_salida.append(token)

    if not tokens_salida:
        return valor_original

    opcion = " ".join(tokens_salida)
    opcion = re.sub(r"\s+", " ", opcion).strip()

    return opcion

def _opcion_taxonomica_especificacion_tecnica(etiqueta: str) -> bool:
    """
    Detecta si una opción taxonómica parece una especificación técnica
    y no un tipo/familia de producto.

    Regla general:
    - Si tiene números, unidades, voltajes, tamaños, capacidades o señales,
      no debe usarse como "tipo de producto".
    - Esos datos pueden preguntarse después como campos técnicos,
      pero no como primera pregunta de tipo.
    """
    texto = _normalizar_taxonomia_texto(etiqueta)

    if not texto:
        return True

    # Si contiene cualquier número, probablemente ya es una especificación:
    # voltaje, tamaño, capacidad, rango, dimensión, número de vías, etc.
    if any(char.isdigit() for char in texto):
        return True

    unidades_o_specs = {
        "vac",
        "vdc",
        "volt",
        "volts",
        "voltaje",
        "amp",
        "amps",
        "ma",
        "mv",
        "w",
        "kw",
        "hz",
        "cfm",
        "gpm",
        "lpm",
        "psi",
        "bar",
        "mbar",
        "pa",
        "kpa",
        "mpa",
        "mm",
        "cm",
        "pulgada",
        "pulgadas",
        "inch",
        "inches",
        "dial",
        "rango",
        "escala",
        "diametro",
        "diametro",
        "rosca",
        "npt",
        "bsp",
        "salida",
        "entrada",
        "senal",
        "señal",
        "rele",
        "relay",
        "contacto",
        "contactos",
    }

    tokens = set(texto.split())

    return bool(tokens & unidades_o_specs)

def _agrupar_por_campo_taxonomico(
    texto_cliente: str,
    productos: list[dict],
    campo: str,
) -> list[dict]:
    """
    Agrupa productos por un campo taxonómico.

    Retorna grupos con:
    - etiqueta: opción resumida para mostrar.
    - valor_original: valor real del catálogo.
    - campo: campo usado para agrupar.
    - cantidad: cantidad de productos en el grupo.

    Regla importante:
    - No usa como tipo opciones que ya parecen especificaciones técnicas.
    """
    grupos: dict[str, dict] = {}

    for producto in productos:
        valor = _valor_taxonomico_producto(producto, campo)

        if not valor:
            continue

        etiqueta = _resumir_opcion_taxonomica(
            texto_cliente=texto_cliente,
            valor_taxonomico=valor,
        )

        if not etiqueta:
            continue

        # Evitamos que voltajes, dimensiones, capacidades o señales
        # se presenten como "tipo de producto".
        if _opcion_taxonomica_especificacion_tecnica(etiqueta):
            continue

        clave = _normalizar_taxonomia_texto(etiqueta)

        if not clave:
            continue

        if clave not in grupos:
            grupos[clave] = {
                "etiqueta": etiqueta,
                "valor_original": valor,
                "campo": campo,
                "cantidad": 0,
            }

        grupos[clave]["cantidad"] += 1

    return sorted(
        grupos.values(),
        key=lambda item: (-item["cantidad"], item["etiqueta"]),
    )


def _nivel_taxonomico_util(
    texto_cliente: str,
    productos: list[dict],
) -> tuple[Optional[str], list[dict]]:
    """
    Elige automáticamente el mejor nivel taxonómico para preguntar por tipo.

    Criterios:
    - Debe generar entre 2 y 5 opciones.
    - Las opciones deben cubrir una parte relevante de los productos filtrados.
    - Se prefieren niveles generales antes que niveles demasiado específicos.
    """
    if not productos:
        return None, []

    total = len(productos)

    mejor_campo = None
    mejores_grupos: list[dict] = []

    for campo in CAMPOS_TAXONOMIA_PRIORIDAD:
        grupos = _agrupar_por_campo_taxonomico(
            texto_cliente=texto_cliente,
            productos=productos,
            campo=campo,
        )

        if len(grupos) < 2:
            continue

        # Evitamos listas demasiado largas. Si hay más de 5 grupos,
        # tomamos los 5 más representativos, pero verificamos cobertura.
        grupos_top = grupos[:5]

        cobertura = sum(g["cantidad"] for g in grupos_top) / max(total, 1)

        if cobertura < 0.45:
            continue

        mejor_campo = campo
        mejores_grupos = grupos_top
        break

    return mejor_campo, mejores_grupos


def detectar_tipos_producto(
    texto_cliente: str,
    productos: list[dict],
    max_tipos: int = 5,
) -> list[str]:
    """
    Detecta tipos/variantes usando taxonomía real del catálogo.

    Garantías:
    - No usa nombre ni descripción libre.
    - No inventa tipos.
    - No usa reglas por producto específico.
    - Si la taxonomía no es clara, devuelve [].
    """
    productos_filtrados = _filtrar_productos_por_familia(
        texto_cliente=texto_cliente,
        productos=productos,
    )

    # Si no hay suficientes productos realmente relacionados,
    # no forzamos pregunta por tipo.
    if len(productos_filtrados) < 3:
        return []

    _, grupos = _nivel_taxonomico_util(
        texto_cliente=texto_cliente,
        productos=productos_filtrados,
    )

    if not grupos:
        return []

    tipos = [g["etiqueta"] for g in grupos[:max_tipos]]

    # Evitamos opciones duplicadas normalizadas.
    vistos = set()
    tipos_limpios = []

    for tipo in tipos:
        clave = _normalizar_taxonomia_texto(tipo)

        if not clave or clave in vistos:
            continue

        vistos.add(clave)
        tipos_limpios.append(tipo)

    return tipos_limpios


def debe_preguntar_tipo_producto(
    texto_cliente: str,
    productos: list[dict],
) -> tuple[bool, list[str]]:
    """
    Decide si conviene preguntar por tipo de producto antes de seguir.

    Solo activa el flujo si:
    - La consulta es corta/genérica.
    - Hay candidatos realmente relacionados con la familia.
    - La taxonomía genera entre 2 y 5 opciones claras.
    """
    if not productos or len(productos) < 6:
        return False, []

    tokens_cliente = _tokens_taxonomia(texto_cliente)

    # Si el cliente ya dio muchos detalles técnicos, no frenamos con tipo.
    if len(tokens_cliente) >= 4:
        return False, []

    tipos = detectar_tipos_producto(
        texto_cliente=texto_cliente,
        productos=productos,
    )

    if 2 <= len(tipos) <= 5:
        return True, tipos

    return False, tipos

def formatear_producto(p: dict) -> str:
    """
    Formatea un producto para mostrarlo al cliente.

    Nunca usa placeholders tipo [código] o [marca].
    Si falta un dato real, muestra 'No disponible'.

    Nota:
    El campo 'existencia' se muestra como tiempo de entrega estimado,
    porque no representa stock físico.
    """
    codigo = p.get("codigo") or "No disponible"
    referencia = p.get("referencia") or "No disponible"
    nombre = p.get("nombre") or "No disponible"
    marca = p.get("marca") or "No disponible"
    desc = p.get("descripcion_corta") or p.get("descripcion") or "No disponible"
    disponibilidad = _disponibilidad_catalogo(p)

    return (
        f"Código: {codigo}\n"
        f"Referencia: {referencia}\n"
        f"Nombre: {nombre}\n"
        f"Marca: {marca}\n"
        f"Descripción: {desc}\n"
        f"{disponibilidad}"
    )