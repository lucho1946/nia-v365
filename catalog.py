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
from collections import Counter
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

# ============================================================
# NORMALIZACIÓN DE CONSULTAS CONVERSACIONALES
# ============================================================

PALABRAS_ENVOLTURA_BUSQUEDA = {
    # Verbos conversacionales o comerciales.
    "necesito",
    "necesitamos",
    "requiero",
    "requerimos",
    "quiero",
    "queremos",
    "busco",
    "buscamos",
    "solicito",
    "solicitamos",
    "deseo",
    "deseamos",
    "gustaria",
    "quisiera",
    "cotizar",
    "cotizacion",
    "comprar",
    "compra",

    # Sustantivos genéricos que no identifican la familia.
    "producto",
    "productos",
    "equipo",
    "equipos",

    # Artículos y conectores conversacionales.
    "un",
    "una",
    "unos",
    "unas",
    "el",
    "la",
    "los",
    "las",
    "del",
    "por",
    "para",
    "favor",
}


def _normalizar_query_busqueda_catalogo(valor: str) -> str:
    """
    Elimina únicamente la envoltura conversacional de una consulta.

    Ejemplos:
        "necesito un termometro"
        -> "termometro"

        "quiero cotizar un transmisor de temperatura"
        -> "transmisor temperatura"

        "termometro sin certificado"
        -> "termometro sin certificado"

    Importante:
    - No elimina 'con' ni 'sin', porque pueden expresar requisitos técnicos.
    - No elimina marcas, materiales, unidades, códigos o especificaciones.
    - No contiene nombres específicos de productos.
    """
    texto_normalizado = _normalize_text(valor)

    if not texto_normalizado:
        return ""

    tokens_utiles = []

    for token in texto_normalizado.split():
        if len(token) < 3:
            continue

        if token in PALABRAS_ENVOLTURA_BUSQUEDA:
            continue

        tokens_utiles.append(token)

    return " ".join(tokens_utiles).strip()


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
    Busca productos por texto directamente en MongoDB.

    Antes de construir la consulta:
    - elimina la envoltura conversacional;
    - conserva términos técnicos importantes;
    - registra la consulta original y la consulta efectiva.

    Ejemplos:
        "necesito un termometro"
        -> "termometro"

        "quiero cotizar un transmisor de temperatura"
        -> "transmisor temperatura"

        "termometro sin certificado"
        -> "termometro sin certificado"
    """
    query_original = _clean_text(query)

    if not query_original:
        return None

    # Convertimos la frase conversacional en una consulta útil
    # para el catálogo, sin perder especificaciones técnicas.
    query_busqueda = _normalizar_query_busqueda_catalogo(
        query_original
    )

    if not query_busqueda:
        logger.info(
            "Consulta sin términos útiles para catálogo: original='%s'",
            query_original,
        )
        return None

    # La consulta Mongo debe construirse con la versión limpia,
    # no con la frase conversacional completa.
    mongo_query = _build_mongo_text_query(
        query_busqueda
    )

    if not mongo_query:
        return None

    db = get_db()
    collection = db[PRODUCTS_COLLECTION]

    cursor = collection.find(
        mongo_query,
        {"_id": 0},
    ).limit(DEFAULT_LIMIT)

    docs = await cursor.to_list(
        length=DEFAULT_LIMIT
    )

    if not docs:
        logger.info(
            "Sin resultados de catálogo: original='%s' limpia='%s'",
            query_original,
            query_busqueda,
        )
        return None

    normalizados = [
        normalizar_producto(doc)
        for doc in docs
    ]

    logger.info(
        "Búsqueda catálogo original='%s' limpia='%s' resultados=%s",
        query_original,
        query_busqueda,
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
    Extrae pares campo-valor desde la descripción técnica del catálogo.

    Formatos reales soportados:
    - ■ Campo: valor      U+25A0
    - • Campo: valor      U+2022
    - ▪ Campo: valor      U+25AA
    - ¦ Campo: valor      U+00A6, compatibilidad histórica
    - Campos separados por saltos de línea
    - Campos separados mediante etiquetas HTML <br>

    Ejemplo:
        ■ Entrada: 4-20 mA
        ■ Salida(s) de control: 1 (Relé)
        ■ Alarma(s): 1 (Relé)

    Resultado:
        {
            "entrada": "4-20 mA",
            "salida s de control": "1 (Relé)",
            "alarma s": "1 (Relé)"
        }

    Reglas:
    - No inventa campos.
    - Ignora líneas sin separador ":".
    - Ignora valores vacíos.
    - Normaliza el nombre del campo para permitir comparaciones.
    - Conserva el valor técnico original.
    - Si un campo se repite con valores diferentes, conserva ambos.
    """
    if not desc_larga:
        return {}

    texto = str(desc_larga).strip()

    if not texto:
        return {}

    # ------------------------------------------------------------
    # 1. Normalizar saltos de línea y etiquetas HTML
    # ------------------------------------------------------------
    texto = texto.replace("\r\n", "\n")
    texto = texto.replace("\r", "\n")

    texto = re.sub(
        r"(?i)<br\s*/?>",
        "\n",
        texto,
    )

    # ------------------------------------------------------------
    # 2. Convertir todos los separadores conocidos en saltos de línea
    # ------------------------------------------------------------
    separadores = (
        chr(0x25A0),  # ■ BLACK SQUARE
        chr(0x2022),  # • BULLET
        chr(0x25AA),  # ▪ SMALL SQUARE
        chr(0x00A6),  # ¦ BROKEN BAR, compatibilidad histórica
    )

    for separador in separadores:
        texto = texto.replace(separador, "\n")

    # ------------------------------------------------------------
    # 3. Extraer pares campo: valor
    # ------------------------------------------------------------
    campos: dict[str, str] = {}

    for linea in texto.splitlines():
        linea = linea.strip()

        if not linea or ":" not in linea:
            continue

        campo_original, valor_original = linea.split(":", 1)

        # Limpieza defensiva de cualquier marcador residual.
        campo_original = re.sub(
            r"^[\s\-\*\u25A0\u2022\u25AA\u00A6]+",
            "",
            campo_original,
        ).strip()

        valor = re.sub(
            r"\s+",
            " ",
            valor_original,
        ).strip()

        if not campo_original or not valor:
            continue

        # Mantener el mismo contrato interno que ya usa catalog.py:
        # minúsculas, sin tildes, signos normalizados y espacios compactos.
        campo = _normalize_text(campo_original)

        if len(campo) < 2 or len(campo) > 100:
            continue

        if campo not in campos:
            campos[campo] = valor
            continue

        # Si el mismo campo aparece repetido con un valor diferente,
        # conservamos ambos sin duplicarlos.
        valores_existentes = [
            item.strip()
            for item in campos[campo].split(" | ")
            if item.strip()
        ]

        if valor not in valores_existentes:
            campos[campo] = f"{campos[campo]} | {valor}"

    return campos

# ============================================================
# ANÁLISIS DINÁMICO DE CAMPOS TÉCNICOS
# ============================================================

VALORES_TECNICOS_NO_UTILES = {
    "",
    "-",
    "--",
    "n/a",
    "na",
    "no aplica",
    "sin dato",
    "sin datos",
    "no disponible",
    "pendiente",
    "ninguno",
    "ninguna",
}


CAMPOS_NO_PREGUNTABLES = {
    # Identificación y contenido comercial.
    "codigo",
    "referencia",
    "marca",
    "modelo",
    "nombre",
    "descripcion",
    "descripcion corta",
    "descripcion larga",

    # Campos informativos que normalmente no discriminan un SKU técnico.
    "articulo",
    "articula",
    "origen",
    "incluye",
    "contenido",
    "observacion",
    "observaciones",
    "nota",
    "notas",
    "para usar con",
}


# Prioridades generales de ingeniería.
# No corresponden a productos específicos.
PATRONES_PRIORIDAD_CAMPO = (
    ("rango", 1.00),
    ("presion", 1.00),
    ("temperatura", 1.00),
    ("capacidad", 1.00),
    ("caudal", 1.00),
    ("flujo", 1.00),

    ("entrada", 0.95),
    ("salida", 0.95),
    ("senal", 0.95),
    ("conexion", 0.95),
    ("alimentacion", 0.90),
    ("voltaje", 0.90),
    ("corriente", 0.90),
    ("protocolo", 0.90),
    ("comunicacion", 0.90),

    ("material", 0.85),
    ("proteccion", 0.85),
    ("montaje", 0.85),

    ("precision", 0.80),
    ("exactitud", 0.80),
    ("resolucion", 0.80),
    ("repetibilidad", 0.80),
    ("sensibilidad", 0.80),

    ("dimension del sensor", 0.90),
    ("dimension", 0.85),
    ("dimensiones", 0.85),
    ("diametro", 0.82),
    ("longitud", 0.82),
    ("tamano", 0.80),
    ("largo", 0.78),
    ("altura", 0.70),
    ("ancho", 0.70),

    ("tipo", 0.60),
    ("aplicacion", 0.55),
    ("estilo", 0.50),
)


def _valor_tecnico_es_util(valor: str) -> bool:
    """
    Decide si un valor extraído puede utilizarse para discriminar productos.

    No interpreta el significado técnico.
    Solo elimina valores vacíos o marcadores administrativos.
    """
    valor_norm = _normalize_text(valor)

    if not valor_norm:
        return False

    if valor_norm in VALORES_TECNICOS_NO_UTILES:
        return False

    return len(valor_norm) >= 2


def _campo_tecnico_es_preguntable(campo: str) -> bool:
    """
    Decide si el campo puede convertirse en una pregunta técnica.

    Se excluyen campos comerciales, descriptivos o administrativos.
    """
    campo_norm = _normalize_text(campo)

    if not campo_norm:
        return False

    if campo_norm in CAMPOS_NO_PREGUNTABLES:
        return False

    if len(campo_norm) < 2 or len(campo_norm) > 100:
        return False

    return True


def _prioridad_semantica_campo(campo: str) -> float:
    """
    Asigna una prioridad general de ingeniería entre 0.0 y 1.0.

    Esta prioridad no decide sola.
    Se combina con cobertura y diversidad obtenidas desde el catálogo real.
    """
    campo_norm = _normalize_text(campo)

    for patron, prioridad in PATRONES_PRIORIDAD_CAMPO:
        if patron in campo_norm:
            return prioridad

    return 0.40


def _familia_semantica_campo(campo: str) -> str:
    """
    Agrupa campos técnicos para evitar dos preguntas redundantes.

    Ejemplos:
    - rango y rango de temperatura -> rango_condicion
    - entrada y salida -> interfaz
    - precisión y resolución -> desempeno
    """
    campo_norm = _normalize_text(campo)

    familias = {
        "condicion_operacion": {
            "rango",
            "presion",
            "temperatura",
            "capacidad",
            "caudal",
            "flujo",
            "peso",
        },
        "interfaz_instalacion": {
            "entrada",
            "salida",
            "senal",
            "conexion",
            "montaje",
            "alimentacion",
            "voltaje",
            "corriente",
            "protocolo",
            "comunicacion",
            "proteccion",
            "frecuencia",
        },
        "desempeno": {
            "precision",
            "exactitud",
            "resolucion",
            "repetibilidad",
            "sensibilidad",
            "estabilidad",
            "linealidad",
        },
        "construccion": {
            "material",
            "cuerpo",
            "carcasa",
            "acabado",
            "pantalla",
        },
        "dimensiones_fisicas": {
            "dimension",
            "dimensiones",
            "diametro",
            "longitud",
            "tamano",
            "largo",
            "altura",
            "ancho",
            "talla",
        },
        "tipo_aplicacion": {
            "tipo",
            "aplicacion",
            "estilo",
            "uso",
        },
    }

    for familia, patrones in familias.items():
        if any(patron in campo_norm for patron in patrones):
            return familia

    return "otro"


def _campos_son_redundantes(campo_a: str, campo_b: str) -> bool:
    """
    Detecta nombres de campo equivalentes o demasiado parecidos.

    Ejemplos:
    - rango
    - rango de temperatura
    - temperatura rango
    """
    a = _normalize_text(campo_a)
    b = _normalize_text(campo_b)

    if not a or not b:
        return False

    if a == b:
        return True

    if a in b or b in a:
        return True

    funcionales = {
        "de",
        "del",
        "la",
        "el",
        "los",
        "las",
        "para",
        "con",
        "y",
        "o",
    }

    tokens_a = {
        token
        for token in a.split()
        if token not in funcionales
    }

    tokens_b = {
        token
        for token in b.split()
        if token not in funcionales
    }

    if not tokens_a or not tokens_b:
        return False

    interseccion = len(tokens_a & tokens_b)
    base = min(len(tokens_a), len(tokens_b))

    return (interseccion / base) >= 0.75

# ============================================================
# COHERENCIA DE CANDIDATOS PARA CAMPOS TÉCNICOS
# ============================================================

PALABRAS_FUNCIONALES_CONSULTA = {
    "de",
    "del",
    "la",
    "el",
    "los",
    "las",
    "un",
    "una",
    "unos",
    "unas",
    "para",
    "con",
    "sin",
    "por",
    "en",
    "y",
    "o",
    "que",
    "necesito",
    "necesita",
    "busco",
    "buscar",
    "quiero",
    "cotizar",
    "comprar",
    "producto",
    "equipo",
}


def _tokens_identidad_consulta(texto: str) -> list[str]:
    """
    Extrae los términos que representan la identidad del producto solicitado.

    No contiene nombres de productos específicos.
    Solo elimina conectores y expresiones comerciales.
    """
    tokens = []

    for token in _tokens(texto or ""):
        token_norm = _normalize_text(token)

        if not token_norm:
            continue

        if token_norm in PALABRAS_FUNCIONALES_CONSULTA:
            continue

        if len(token_norm) < 3:
            continue

        if token_norm not in tokens:
            tokens.append(token_norm)

    return tokens


def _texto_identidad_producto(producto: dict) -> str:
    """
    Construye el texto fuerte de identidad del producto.

    Importante:
    - Usa nombre y taxonomía.
    - No usa descripción larga para decidir la familia.
    - Así evita que accesorios o productos relacionados entren solo porque
      mencionan la palabra en una especificación.
    """
    campos = [
        producto.get("nombre"),
        producto.get("categoria"),
        producto.get("nivel_0"),
        producto.get("nivel_1"),
        producto.get("nivel_2"),
        producto.get("nivel_3"),
        producto.get("nivel_4"),
    ]

    return " ".join(
        _clean_text(valor)
        for valor in campos
        if _clean_text(valor)
    )


def _raiz_identidad_token(token: str) -> str:
    """
    Obtiene una raíz morfológica básica para comparar identidad de producto.

    Objetivo:
    - controlador / controladores / controles -> control
    - transmisor / transmisores -> transmis
    - termometro / termometros -> termometro
    - digital / digitales -> digital
    - portatil / portatiles -> portatil

    No intenta hacer análisis lingüístico completo.
    Solo evita plurales y derivaciones comerciales evidentes.
    """
    texto = _normalize_text(token)

    if not texto:
        return ""

    sufijos = (
        "adores",
        "adoras",
        "idores",
        "idoras",
        "ador",
        "adora",
        "idor",
        "idora",
        "ores",
        "oras",
        "or",
        "ora",
        "es",
        "s",
    )

    for sufijo in sufijos:
        if not texto.endswith(sufijo):
            continue

        raiz = texto[:-len(sufijo)]

        # No generamos raíces demasiado cortas porque producirían
        # equivalencias débiles o falsas.
        if len(raiz) >= 5:
            return raiz

    return texto


def _tokens_son_equivalentes(token_a: str, token_b: str) -> bool:
    """
    Compara tokens de identidad tolerando plural y derivaciones simples.

    Evita utilizar solo un prefijo fijo, porque eso puede generar falsos
    positivos como:
    - termometro
    - termohigrometro
    """
    a = _normalize_text(token_a)
    b = _normalize_text(token_b)

    if not a or not b:
        return False

    if a == b:
        return True

    raiz_a = _raiz_identidad_token(a)
    raiz_b = _raiz_identidad_token(b)

    if raiz_a and raiz_a == raiz_b:
        return True

    # Tolerancia para errores menores de escritura.
    # La diferencia de longitud se limita para no aceptar palabras compuestas
    # que solamente comparten una parte inicial.
    if abs(len(a) - len(b)) <= 2 and _sim(a, b) >= 0.90:
        return True

    return False


def _cantidad_minima_tokens_coincidentes(total_tokens: int) -> int:
    """
    Define cuántos términos de identidad debe cubrir un candidato.

    Reglas:
    - 1 término: debe coincidir 1.
    - 2 términos: deben coincidir los 2.
    - 3 términos: deben coincidir los 3.
    - 4 o más: debe coincidir al menos el 75 % aproximadamente.

    Esto evita:
    - controlador de nivel para "controlador temperatura"
    - calibrador de temperatura para "controlador temperatura"
    """
    if total_tokens <= 1:
        return total_tokens

    if total_tokens == 2:
        return 2

    if total_tokens == 3:
        return 3

    return max(3, (total_tokens * 3 + 3) // 4)

MARCADORES_ACCESORIO_IDENTIDAD = {
    "parte y accesorio",
    "partes y accesorios",
    "accesorio",
    "accesorios",
    "repuesto",
    "repuestos",
    "kit para",
    "soporte para",
    "adaptador para",
    "cable para",
    "modulo para",
    "tarjeta para",
}


def _consulta_solicita_accesorio(texto_cliente: str) -> bool:
    """
    Determina si el cliente pidió explícitamente un accesorio o repuesto.
    """
    texto_norm = _normalize_text(texto_cliente)

    indicadores = {
        "accesorio",
        "accesorios",
        "repuesto",
        "repuestos",
        "kit",
        "soporte",
        "adaptador",
        "cable",
        "modulo",
        "tarjeta",
    }

    tokens = set(texto_norm.split())

    return bool(tokens & indicadores)


def _producto_es_accesorio_por_identidad(producto: dict) -> bool:
    """
    Detecta accesorios desde el nombre y la taxonomía principal.

    No usa descripción larga porque allí pueden mencionarse accesorios
    incluidos sin que el producto principal sea un accesorio.
    """
    texto_identidad = _normalize_text(
        _texto_identidad_producto(producto)
    )

    return any(
        marcador in texto_identidad
        for marcador in MARCADORES_ACCESORIO_IDENTIDAD
    )

def _token_consulta_coincide(
    token_consulta: str,
    tokens_producto: list[str],
) -> bool:
    """
    Indica si un término de identidad de la consulta aparece realmente
    en el nombre o taxonomía del producto.
    """
    return any(
        _tokens_son_equivalentes(
            token_consulta,
            token_producto,
        )
        for token_producto in tokens_producto
    )


def _seleccionar_token_ancla_identidad(
    tokens_consulta: list[str],
    productos: list[dict],
) -> Optional[str]:
    """
    Selecciona el término más discriminante de la consulta.

    Ejemplo:
        consulta:
            termometro digital portatil bolsillo

        En los candidatos:
            digital, portatil y bolsillo aparecen casi siempre;
            termometro es el término que define la familia principal.

        Resultado:
            token_ancla = "termometro"

    La selección se calcula desde los candidatos reales.
    No contiene nombres de productos quemados.
    """
    if not tokens_consulta or not productos:
        return None

    frecuencias = Counter()

    for producto in productos:
        texto_producto = _texto_identidad_producto(producto)

        if not texto_producto:
            continue

        tokens_producto = _tokens(texto_producto)

        for token_consulta in tokens_consulta:
            if _token_consulta_coincide(
                token_consulta,
                tokens_producto,
            ):
                frecuencias[token_consulta] += 1

    # Solo consideramos términos que sí aparecen en algún candidato.
    candidatos_ancla = [
        token
        for token in tokens_consulta
        if frecuencias.get(token, 0) > 0
    ]

    if not candidatos_ancla:
        return None

    # El término que aparece en menos candidatos es el más discriminante.
    # En empate se conserva el orden de la consulta.
    return min(
        candidatos_ancla,
        key=lambda token: (
            frecuencias[token],
            tokens_consulta.index(token),
        ),
    )

def filtrar_candidatos_coherentes(
    texto_cliente: str,
    productos: list[dict],
) -> list[dict]:
    """
    Filtra candidatos antes de analizar campos técnicos.

    Fuente de verdad:
    - nombre
    - categoría
    - niveles taxonómicos

    No usa descripción larga para decidir si el producto pertenece
    a la necesidad solicitada.

    La descripción larga se usa únicamente después, para extraer campos.
"""
    productos_validos = [
        producto
        for producto in productos or []
        if isinstance(producto, dict)
    ]

    if not productos_validos:
        return []

    tokens_consulta = _tokens_identidad_consulta(texto_cliente)

    if not tokens_consulta:
        return productos_validos

    minimo_coincidencias = _cantidad_minima_tokens_coincidentes(
        len(tokens_consulta)
    )

    token_ancla = _seleccionar_token_ancla_identidad(
        tokens_consulta,
        productos_validos,
    )

    coherentes = []

    consulta_pide_accesorio = _consulta_solicita_accesorio(
        texto_cliente
    )

    for producto in productos_validos:
        # Si el cliente pidió un transmisor, sensor, válvula, etc.,
        # no usamos partes o accesorios para calcular campos técnicos.
        #
        # Sí se conservan cuando el cliente pide explícitamente accesorio,
        # repuesto, kit, cable, soporte, adaptador, etc.
        if (
            not consulta_pide_accesorio
            and _producto_es_accesorio_por_identidad(producto)
        ):
            continue

        texto_producto = _texto_identidad_producto(producto)

        if not texto_producto:
            continue

        tokens_producto = _tokens(texto_producto)

        # El candidato debe contener el término que define mejor
        # la identidad de la consulta.
        #
        # Esto evita, por ejemplo, aceptar un producto que coincida
        # únicamente por palabras secundarias como digital, portátil
        # o bolsillo.
        if (
            token_ancla
            and not _token_consulta_coincide(
                token_ancla,
                tokens_producto,
            )
        ):
            continue

        coincidencias = 0

        for token_consulta in tokens_consulta:
            coincide = any(
                _tokens_son_equivalentes(
                    token_consulta,
                    token_producto,
                )
                for token_producto in tokens_producto
            )

            if coincide:
                coincidencias += 1

        cobertura = coincidencias / len(tokens_consulta)

        if coincidencias < minimo_coincidencias:
            continue

        coherentes.append(
            {
                "producto": producto,
                "coincidencias": coincidencias,
                "cobertura": cobertura,
                "similitud_identidad": _sim(
                    texto_cliente,
                    texto_producto,
                ),
            }
        )

    coherentes.sort(
        key=lambda item: (
            -item["cobertura"],
            -item["similitud_identidad"],
        )
    )

    productos_filtrados = [
        item["producto"]
        for item in coherentes
    ]

    logger.info(
        "Filtro coherencia campos: query='%s' candidatos=%s coherentes=%s "
        "tokens=%s token_ancla=%s minimo_coincidencias=%s",
        texto_cliente,
        len(productos_validos),
        len(productos_filtrados),
        tokens_consulta,
        token_ancla,
        minimo_coincidencias,
    )

    return productos_filtrados

# ============================================================
# CANONIZACIÓN DE CAMPOS TÉCNICOS
# ============================================================

def _canonizar_nombre_campo_tecnico(campo: str) -> str:
    """
    Agrupa nombres diferentes que representan la misma decisión técnica.
    No modifica los datos originales del catálogo.
    Solo crea una representación común para análisis y preguntas.
    """
    campo_norm = _normalize_text(campo)

    if not campo_norm:
        return ""

    # Entrada o tipo de sensor.
    if (
        campo_norm in {
            "rtd",
            "termocupla",
            "termocuplas",
            "termopar",
            "termopares",
        }
        or campo_norm.startswith("entrada ")
        or campo_norm.startswith("tipo de entrada")
        or campo_norm.startswith("sensor de entrada")
    ):
        return "tipo de entrada"

    # Salidas de control, señal o relevadores.
    if campo_norm.startswith("salida"):
        return "salida"

    # Rangos con diferentes nombres.
    if "rango" in campo_norm:
        if "temperatura" in campo_norm:
            return "rango de temperatura"

        if "presion" in campo_norm:
            return "rango de presion"

        return "rango"

    # Dimensiones expresadas de diferentes formas.
    if campo_norm.startswith("dimension"):
        return "dimensiones"

    # Resolución y exactitud a veces vienen en un único campo.
    if campo_norm in {
        "resolucion exactitud",
        "exactitud resolucion",
    }:
        return "resolucion y exactitud"

    # Variantes frecuentes del voltaje de alimentación.
    if campo_norm in {
        "voltaje alimentacion",
        "voltaje de alimentacion",
        "tension de alimentacion",
    }:
        return "alimentacion"

    # Campos físicos equivalentes.
    #
    # Todos representan una decisión dimensional y deben compararse
    # contra el campo canónico "dimensiones".
    if any(
        patron in campo_norm
        for patron in (
            "dimension",
            "tamano",
            "longitud",
            "largo",
            "diametro",
            "ancho",
            "alto",
            "altura",
            "espesor",
            "profundidad",
            "longitud del bulbo",
            "largo del punzon",
        )
    ):
        return "dimensiones"

    return campo_norm


def _contextualizar_valor_canonico(
    campo_original: str,
    campo_canonico: str,
    valor: str,
) -> str:
    """
    Conserva el origen técnico cuando varios campos se agrupan.
    """
    campo_original_norm = _normalize_text(campo_original)
    valor_limpio = str(valor or "").strip()

    if not valor_limpio:
        return ""

    if campo_canonico == "tipo de entrada":
        if "rtd" in campo_original_norm:
            return f"RTD: {valor_limpio}"

        if (
            "termocupla" in campo_original_norm
            or "termopar" in campo_original_norm
        ):
            return f"Termocupla: {valor_limpio}"

    return valor_limpio

def campos_disponibles_de(
    productos: list[dict],
    min_cobertura: float = 0.10,
    min_valores_distintos: int = 2,
    max_campos: int = 20,
) -> list[dict]:
    """
    Analiza los campos técnicos reales disponibles en una lista de productos.

    Retorna los campos ordenados por utilidad técnica.

    Métricas:
    - cobertura:
      porcentaje de candidatos que contiene el campo.
    - valores_distintos:
      cantidad de valores distintos detectados.
    - dominancia:
      porcentaje ocupado por el valor más frecuente.
      Si todos tienen el mismo valor, el campo discrimina poco.
    - prioridad_semantica:
      importancia general como variable de selección industrial.
    - score:
      combinación de los criterios anteriores.

    No inventa campos ni utiliza una categoría fija.
    """
    productos = [
        producto
        for producto in productos or []
        if isinstance(producto, dict)
    ]

    total_productos = len(productos)

    if total_productos == 0:
        return []

    estadisticas: dict[str, dict] = {}

    for producto in productos:
        descripcion_larga = _clean_text(
            producto.get("descripcion_larga")
            or producto.get("DESCRIPCION_LARGA_PRE")
            or producto.get("descripcion_larga_pre")
            or producto.get("DESCRIPCION_LARGA")
        )

        campos_producto = _parsear_campos(descripcion_larga)

        # --------------------------------------------------------
        # Canonizar campos dentro de cada producto
        # --------------------------------------------------------
        # Un producto puede tener:
        # - entrada RTD
        # - entrada termocupla
        #
        # Ambos deben convertirse en una sola variable técnica:
        # - tipo de entrada
        campos_canonicos_producto: dict[str, list[str]] = {}

        for campo_original, valor_original in campos_producto.items():
            campo_canonico = _canonizar_nombre_campo_tecnico(
                campo_original
            )

            if not _campo_tecnico_es_preguntable(campo_canonico):
                continue

            if not _valor_tecnico_es_util(valor_original):
                continue

            valor_canonico = _contextualizar_valor_canonico(
                campo_original=campo_original,
                campo_canonico=campo_canonico,
                valor=valor_original,
            )

            if not valor_canonico:
                continue

            valores = campos_canonicos_producto.setdefault(
                campo_canonico,
                [],
            )

            if valor_canonico not in valores:
                valores.append(valor_canonico)

        # --------------------------------------------------------
        # Registrar una sola vez cada campo por producto
        # --------------------------------------------------------
        for campo_canonico, valores in campos_canonicos_producto.items():
            valor_compuesto = " | ".join(valores).strip()

            if not valor_compuesto:
                continue

            valor_norm = _normalize_text(valor_compuesto)

            if campo_canonico not in estadisticas:
                estadisticas[campo_canonico] = {
                    "campo": campo_canonico,
                    "productos_con_campo": 0,
                    "valores": Counter(),
                    "valor_original": {},
                }

            info = estadisticas[campo_canonico]

            info["productos_con_campo"] += 1
            info["valores"][valor_norm] += 1
            info["valor_original"].setdefault(
                valor_norm,
                valor_compuesto,
            )

    resultados = []

    for campo, info in estadisticas.items():
        cantidad = info["productos_con_campo"]
        cobertura = cantidad / total_productos

        valores_counter: Counter = info["valores"]
        valores_distintos = len(valores_counter)

        if cobertura < min_cobertura:
            continue

        if valores_distintos < min_valores_distintos:
            continue

        valor_mas_comun_cantidad = valores_counter.most_common(1)[0][1]
        dominancia = valor_mas_comun_cantidad / cantidad
        dispersion = 1.0 - dominancia

        # Hasta ocho valores distintos aportan al máximo de la métrica.
        score_diversidad = min(valores_distintos / 8.0, 1.0)
        prioridad_semantica = _prioridad_semantica_campo(campo)

        score = (
            cobertura * 0.45
            + score_diversidad * 0.20
            + dispersion * 0.20
            + prioridad_semantica * 0.15
        )

        ejemplos = []

        for valor_norm, frecuencia in valores_counter.most_common(5):
            ejemplos.append(
                {
                    "valor": info["valor_original"][valor_norm],
                    "frecuencia": frecuencia,
                }
            )

        resultados.append(
            {
                "campo": campo,
                "familia_semantica": _familia_semantica_campo(campo),
                "productos_con_campo": cantidad,
                "total_productos": total_productos,
                "cobertura": round(cobertura, 4),
                "valores_distintos": valores_distintos,
                "dominancia": round(dominancia, 4),
                "prioridad_semantica": round(prioridad_semantica, 4),
                "score": round(score, 4),
                "ejemplos": ejemplos,
            }
        )

    resultados.sort(
        key=lambda item: (
            -item["score"],
            -item["cobertura"],
            -item["valores_distintos"],
            item["campo"],
        )
    )

    return resultados[:max_campos]


def ordenar_campos_por_prioridad(
    campos_disponibles: list[dict],
    max_campos: int = 2,
) -> list[dict]:
    """
    Selecciona los campos más útiles para preguntar al cliente.

    Reglas:
    - máximo dos por defecto;
    - no repite campos equivalentes;
    - los grupos técnicos son una penalización suave, no un bloqueo;
    - permite entrada + salida cuando ambas discriminan correctamente;
    - prioriza el score calculado desde los datos reales del catálogo.
    """
    candidatos = [
        campo
        for campo in campos_disponibles or []
        if isinstance(campo, dict) and campo.get("campo")
    ]

    if not candidatos or max_campos <= 0:
        return []

    seleccionados = []

    while len(seleccionados) < max_campos:
        mejor_candidato = None
        mejor_score_ajustado = -1.0

        for candidato in candidatos:
            if candidato in seleccionados:
                continue

            nombre = candidato["campo"]

            # Nunca preguntamos dos nombres equivalentes.
            if any(
                _campos_son_redundantes(
                    nombre,
                    seleccionado["campo"],
                )
                for seleccionado in seleccionados
            ):
                continue

            score_ajustado = float(candidato.get("score") or 0.0)

            grupo_candidato = (
                candidato.get("familia_semantica")
                or "otro"
            )

            # Compartir grupo no bloquea el campo.
            # Solo recibe una penalización moderada.
            if any(
                grupo_candidato
                == (
                    seleccionado.get("familia_semantica")
                    or "otro"
                )
                and grupo_candidato != "otro"
                for seleccionado in seleccionados
            ):
                score_ajustado *= 0.92

            # Desempate por cobertura.
            score_ajustado += (
                float(candidato.get("cobertura") or 0.0)
                * 0.001
            )

            if score_ajustado > mejor_score_ajustado:
                mejor_score_ajustado = score_ajustado
                mejor_candidato = candidato

        if mejor_candidato is None:
            break

        seleccionado = dict(mejor_candidato)
        seleccionado["score_seleccion"] = round(
            mejor_score_ajustado,
            4,
        )

        seleccionados.append(seleccionado)

    return seleccionados

# ============================================================
# COMPARACIÓN NUMÉRICA DE CAMPOS TÉCNICOS
# ============================================================

def _extraer_medidas_longitud_mm(texto: str) -> list[float]:
    """
    Extrae medidas físicas y las convierte a milímetros.
    """
    texto = str(texto or "")

    patron = re.compile(
        r"([-+]?\d+(?:[\.,]\d+)?)\s*"
        r"("
        r"mm|cm|"
        r"m(?![a-zA-Z])|"
        r"pulg(?:ada|adas)?|"
        r"inches?|inch|"
        r"in(?![a-zA-Z])|"
        r"\"|''"
        r")",
        re.IGNORECASE,
    )

    factores_mm = {
        "mm": 1.0,
        "cm": 10.0,
        "m": 1000.0,
        "pulg": 25.4,
        "pulgada": 25.4,
        "pulgadas": 25.4,
        "in": 25.4,
        "inch": 25.4,
        "inches": 25.4,
        '"': 25.4,
        "''": 25.4,
    }

    medidas = []

    for numero_texto, unidad_texto in patron.findall(texto):
        try:
            numero = float(
                numero_texto.replace(",", ".")
            )
        except ValueError:
            continue

        unidad = unidad_texto.lower()
        factor = factores_mm.get(unidad)

        if factor is None:
            continue

        medidas.append(numero * factor)

    return medidas


def _score_error_relativo(error_relativo: float) -> float:
    """
    Convierte un error relativo entre medidas en score de compatibilidad.
    """
    if error_relativo <= 0.03:
        return 1.0

    if error_relativo <= 0.08:
        return 0.90

    if error_relativo <= 0.15:
        return 0.70

    if error_relativo <= 0.30:
        return 0.40

    return 0.0


def _score_dimensiones_numericas(
    valor_query: str,
    valor_producto: str,
) -> float:
    """
    Compara dimensiones usando milímetros.
    Cada medida solicitada debe encontrar una medida del producto.
    """
    medidas_query = _extraer_medidas_longitud_mm(
        valor_query
    )

    medidas_producto = _extraer_medidas_longitud_mm(
        valor_producto
    )

    # Sin medidas numéricas suficientes se conserva el fallback textual.
    if not medidas_query or not medidas_producto:
        return _sim(
            _normalize_text(valor_query),
            _normalize_text(valor_producto),
        )

    # Comenzamos por las medidas mayores para asociar primero
    # longitud con longitud y después diámetros pequeños.
    pendientes_query = sorted(
        medidas_query,
        reverse=True,
    )

    disponibles_producto = list(
        medidas_producto
    )

    scores = []

    for medida_query in pendientes_query:
        if not disponibles_producto:
            # Una medida solicitada no existe en el producto.
            scores.append(0.0)
            continue

        mejor_indice = None
        mejor_error = None

        for indice, medida_producto in enumerate(
            disponibles_producto
        ):
            denominador = max(
                abs(medida_query),
                0.000001,
            )

            error = abs(
                medida_producto - medida_query
            ) / denominador

            if mejor_error is None or error < mejor_error:
                mejor_error = error
                mejor_indice = indice

        medida_elegida = disponibles_producto.pop(
            mejor_indice
        )

        error_relativo = abs(
            medida_elegida - medida_query
        ) / max(
            abs(medida_query),
            0.000001,
        )

        scores.append(
            _score_error_relativo(error_relativo)
        )

    if not scores:
        return 0.0

    return sum(scores) / len(scores)


def _extraer_intervalo_numerico(
    texto: str,
) -> tuple[float, float] | None:
    """
    Extrae el primer intervalo numérico de un texto.
    """
    coincidencia = re.search(
        r"([-+]?\d+(?:[\.,]\d+)?)\s*"
        r"(?:a|hasta|[-–—]|\.{2,})\s*"
        r"([-+]?\d+(?:[\.,]\d+)?)",
        str(texto or ""),
        re.IGNORECASE,
    )

    if not coincidencia:
        return None

    try:
        inicio = float(
            coincidencia.group(1).replace(",", ".")
        )

        fin = float(
            coincidencia.group(2).replace(",", ".")
        )
    except ValueError:
        return None

    return (
        min(inicio, fin),
        max(inicio, fin),
    )


def _score_rango_numerico(
    valor_query: str,
    valor_producto: str,
) -> float:
    """
    Evalúa si el rango del producto cubre el rango requerido.
    """
    rango_query = _extraer_intervalo_numerico(
        valor_query
    )

    rango_producto = _extraer_intervalo_numerico(
        valor_producto
    )

    if not rango_query or not rango_producto:
        return _sim(
            _normalize_text(valor_query),
            _normalize_text(valor_producto),
        )

    minimo_query, maximo_query = rango_query
    minimo_producto, maximo_producto = rango_producto

    # Cobertura completa.
    if (
        minimo_producto <= minimo_query
        and maximo_producto >= maximo_query
    ):
        return 1.0

    # Cobertura parcial.
    inicio_solapamiento = max(
        minimo_query,
        minimo_producto,
    )

    fin_solapamiento = min(
        maximo_query,
        maximo_producto,
    )

    solapamiento = max(
        0.0,
        fin_solapamiento - inicio_solapamiento,
    )

    amplitud_query = max(
        maximo_query - minimo_query,
        0.000001,
    )

    proporcion = solapamiento / amplitud_query

    # La cobertura parcial nunca equivale a una compatibilidad completa.
    return min(
        proporcion * 0.75,
        0.75,
    )


def _score_valor_tecnico_por_campo(
    campo_canonico: str,
    valor_query: str,
    valor_producto: str,
) -> float:
    """
    Selecciona el comparador apropiado según el campo técnico.
    """
    campo = _normalize_text(
        campo_canonico
    )

    if campo == "dimensiones":
        return _score_dimensiones_numericas(
            valor_query,
            valor_producto,
        )

    if campo == "rango" or campo.startswith("rango "):
        return _score_rango_numerico(
            valor_query,
            valor_producto,
        )

    return _sim(
        _normalize_text(valor_query),
        _normalize_text(valor_producto),
    )

def _score_campos_estructurados(
    campos_producto: dict,
    campos_query: dict,
) -> float:
    """
    Compara los campos solicitados contra los campos reales del producto.

    Reglas:
    - Cada campo solicitado aporta un score.
    - Un campo solicitado que no existe en el producto aporta 0.
    - Los rangos se comparan por cobertura numérica.
    - Las dimensiones se convierten a milímetros.
    - Los demás campos conservan comparación textual flexible.
    """
    if not campos_query:
        return 0.0

    campos_producto = campos_producto or {}

    scores = []

    for campo_query, valor_query in campos_query.items():
        campo_query_canonico = (
            _canonizar_nombre_campo_tecnico(
                campo_query
            )
        )

        mejor_score_campo = 0.0

        for campo_producto, valor_producto in campos_producto.items():
            campo_producto_canonico = (
                _canonizar_nombre_campo_tecnico(
                    campo_producto
                )
            )

            nombres_compatibles = (
                campo_query_canonico
                == campo_producto_canonico
                or campo_query_canonico
                in campo_producto_canonico
                or campo_producto_canonico
                in campo_query_canonico
            )

            if not nombres_compatibles:
                continue

            score_actual = _score_valor_tecnico_por_campo(
                campo_canonico=campo_query_canonico,
                valor_query=valor_query,
                valor_producto=valor_producto,
            )

            mejor_score_campo = max(
                mejor_score_campo,
                score_actual,
            )

        # Se agrega incluso cuando es cero.
        # Así, un producto que no tiene la dimensión solicitada
        # no puede ganar solo por coincidir en el rango.
        scores.append(mejor_score_campo)

    if not scores:
        return 0.0

    return sum(scores) / len(scores)

def score_campos_producto(
    producto: dict,
    campos_query: dict,
) -> float:
    """
    Calcula públicamente la compatibilidad entre los campos solicitados
    y los campos técnicos reales de un producto.

    Se utiliza como guardrail determinístico antes de aceptar o reemplazar
    un candidato mediante el product_matcher basado en LLM.

    Retorna un valor entre 0.0 y 1.0.
    """
    if not isinstance(producto, dict):
        return 0.0

    if not isinstance(campos_query, dict) or not campos_query:
        return 0.0

    raw = producto.get("_raw") or {}

    descripcion_larga = (
        producto.get("descripcion_larga")
        or producto.get("DESCRIPCION_LARGA_PRE")
        or raw.get("DESCRIPCION_LARGA_PRE")
        or ""
    )

    campos_producto = _parsear_campos(
        descripcion_larga
    )

    if not campos_producto:
        return 0.0

    score = _score_campos_estructurados(
        campos_producto=campos_producto,
        campos_query=campos_query,
    )

    try:
        return round(float(score), 4)
    except (TypeError, ValueError):
        return 0.0

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
    # 0. Entrada técnica explícita
    # -----------------------------------------------------------
    # La captura termina cuando comienza otro campo técnico.
    entrada = re.search(
        r"\b(?:"
        r"entrada|"
        r"señal\s+de\s+entrada|"
        r"senal\s+de\s+entrada"
        r")\s*:?\s*(.+?)"
        r"(?=\s+(?:"
        r"salida|"
        r"señal\s+de\s+salida|"
        r"senal\s+de\s+salida|"
        r"rango|"
        r"dimensiones?|"
        r"tama(?:ñ|n)o|"
        r"conexion|conexión|"
        r"alimentacion|alimentación|"
        r"voltaje|"
        r"material|"
        r"presion|presión"
        r")\s*:?\s*|$)",
        t_original,
        re.IGNORECASE,
    )

    if entrada:
        valor = re.sub(
            r"\s+",
            " ",
            entrada.group(1),
        ).strip(" ,.;")

        if valor:
            campos["entrada"] = valor

    # ------------------------------------------------------------
    # 1. Rango técnico
    # ------------------------------------------------------------
    # Primero aprovechamos la etiqueta explícita producida por el flujo
    # dinámico:
    rango_explicito = re.search(
        r"\brango\s*:\s*(.+?)"
        r"(?=\s+(?:dimensiones?|tama(?:ñ|n)o|conexion|conexión|salida|"
        r"tipo\s+de\s+entrada|alimentacion|alimentación|voltaje|material|"
        r"presion|presión)\s*:|$)",
        t_original,
        re.IGNORECASE,
    )

    if rango_explicito:
        valor = re.sub(
            r"\s+",
            " ",
            rango_explicito.group(1),
        ).strip(" ,.;")

        if valor:
            campos["rango"] = valor

    # Si el mensaje no contiene una etiqueta explícita, buscamos un
    # intervalo general con signo opcional.
    if "rango" not in campos:
        rango = re.search(
            r"(?<!\d)"
            r"([-+]?\d+(?:[\.,]\d+)?)\s*"
            r"(?:a|hasta|[-–—])\s*"
            r"([-+]?\d+(?:[\.,]\d+)?)\s*"
            r"(°\s*[cf]|grados?\s*[cf]|"
            r"bar|psi|mbar|kpa|mpa|pa|"
            r"gpm|lpm|l/min|m3/h|m³/h)",
            t,
            re.IGNORECASE,
        )

        if rango:
            inicio, fin, unidad = rango.groups()

            campos["rango"] = (
                f"{inicio} a {fin} {unidad}"
            ).strip()

    # Valor único con unidad cuando no hay rango.
    valor_unico = re.search(
        r"(?<!\d)"
        r"([-+]?\d+(?:[\.,]\d+)?)\s*"
        r"(bar|psi|mbar|kpa|mpa|pa|"
        r"°\s*c|°\s*f|grados?\s*c|grados?\s*f|"
        r"v|vac|vdc|ma|a|gpm|lpm|l/min|m3/h|m³/h|hz)\b",
        t,
        re.IGNORECASE,
    )

    if (
        valor_unico
        and "rango" not in campos
        and "entrada" not in campos
    ):
        valor, unidad = valor_unico.groups()

        campos["valor_tecnico"] = (
            f"{valor} {unidad}"
        ).strip()

    # ------------------------------------------------------------
    # 2. Señal / salida / comunicación / protocolo
    # Extrae lo que venga después de palabras técnicas.
    # ------------------------------------------------------------
    salida = re.search(
        r"\b(?:salida|señal|senal|protocolo|comunicacion|comunicación)\s+"
        r"([a-zA-Z0-9\-\s/\.]+)",
        t_original,
        re.IGNORECASE,
    )

    salida = re.search(
        r"\b(?:"
        r"salida|"
        r"señal\s+de\s+salida|"
        r"senal\s+de\s+salida|"
        r"protocolo|"
        r"comunicacion|comunicación"
        r")\s*:?\s*(.+?)"
        r"(?=\s+(?:"
        r"entrada|"
        r"señal\s+de\s+entrada|"
        r"senal\s+de\s+entrada|"
        r"rango|"
        r"dimensiones?|"
        r"tama(?:ñ|n)o|"
        r"conexion|conexión|"
        r"alimentacion|alimentación|"
        r"voltaje|"
        r"material|"
        r"presion|presión"
        r")\s*:?\s*|$)",
        t_original,
        re.IGNORECASE,
    )

    if salida:
        valor = re.sub(
            r"\s+",
            " ",
            salida.group(1),
        ).strip(" ,.;")

        if valor:
            campos["salida"] = valor

    # Si el cliente no escribió la palabra salida/señal, pero sí un patrón eléctrico claro.
    salida_patron = re.search(
        r"\b(\d+[\.,]?\d*)\s*-\s*(\d+[\.,]?\d*)\s*(ma|v)\b",
        t,
        re.IGNORECASE,
    )

    if (
        salida_patron
        and "salida" not in campos
        and "entrada" not in campos
    ):
        inicio, fin, unidad = salida_patron.groups()
        campos["salida"] = f"{inicio}-{fin} {unidad}"

    # ------------------------------------------------------------
    # 3. Conexión mecánica
    # ------------------------------------------------------------
    # Solo se crea el campo "conexion" cuando existe:
    # - una etiqueta explícita;
    # - o un estándar real de conexión mecánica.
    conexion_texto = re.search(
        r"\b(?:conexion|conexión)\s*:\s*(.+?)"
        r"(?=\s+(?:rango|dimensiones?|tama(?:ñ|n)o|salida|"
        r"tipo\s+de\s+entrada|alimentacion|alimentación|voltaje|material|"
        r"presion|presión)\s*:|$)",
        t_original,
        re.IGNORECASE,
    )

    if not conexion_texto:
        conexion_texto = re.search(
            r"\b(?:conexion|conexión)\s+"
            r"([a-zA-Z0-9áéíóúñÁÉÍÓÚÑ\-\s/\.\"']+)",
            t_original,
            re.IGNORECASE,
        )

    if conexion_texto:
        valor = conexion_texto.group(1).strip()

        valor = re.split(
            r"[,.;\n]|\s+con\s+|\s+para\s+|\s+y\s+",
            valor,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()

        if valor:
            campos["conexion"] = valor

    # Conexiones declaradas mediante estándar mecánico:
    if "conexion" not in campos:
        conexion_estandar = re.search(
            r"\b("
            r"(?:\d+\s*/\s*\d+|\d+(?:[\.,]\d+)?)"
            r"\s*(?:\"|''|pulg(?:ada|adas)?)?\s*"
            r"(?:npt|bsp|bspt|bspp|sae|unf|unc|métric[ao]|metric[ao])"
            r")\b",
            t_original,
            re.IGNORECASE,
        )

        if conexion_estandar:
            campos["conexion"] = re.sub(
                r"\s+",
                " ",
                conexion_estandar.group(1),
            ).strip()

    # Variantes compactas: G1/2, R1/4.
    if "conexion" not in campos:
        conexion_compacta = re.search(
            r"\b(?:G|R)\s*\d+\s*/\s*\d+\b",
            t_original,
            re.IGNORECASE,
        )

        if conexion_compacta:
            campos["conexion"] = re.sub(
                r"\s+",
                "",
                conexion_compacta.group(0),
            )

    # ------------------------------------------------------------
    # 4. Material declarado explícitamente
    # No usamos lista cerrada. Solo extraemos si el usuario lo declara.
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
    # 4. Dimensiones físicas
    # ------------------------------------------------------------
    dimensiones_etiquetadas = re.search(
        r"\b(?:dimensiones?|tama(?:ñ|n)o)\s*:?\s*(.+?)"
        r"(?=\s+(?:rango|conexion|conexión|salida|"
        r"tipo\s+de\s+entrada|alimentacion|alimentación|"
        r"voltaje|material|presion|presión)\s*:?\s*|$)",
        t_original,
        re.IGNORECASE,
    )

    if dimensiones_etiquetadas:
        valor = re.sub(
            r"\s+",
            " ",
            dimensiones_etiquetadas.group(1),
        ).strip(" ,.;")

        # Solo aceptamos el campo cuando realmente contiene
        # al menos una medida física.
        contiene_medida_fisica = re.search(
            r"[-+]?\d+(?:[\.,]\d+)?\s*"
            r"(?:mm|cm|pulg(?:ada|adas)?|inches|inch|in|m)\b",
            valor,
            re.IGNORECASE,
        )

        if valor and contiene_medida_fisica:
            campos["dimensiones"] = valor

    # ------------------------------------------------------------
    # Fallback para mensajes naturales sin etiqueta explícita
    # ------------------------------------------------------------
    if "dimensiones" not in campos:
        inicio_dimension = re.search(
            r"\b(?:"
            r"sonda|largo|longitud|diametro|diámetro|"
            r"ancho|alto|altura|dimension|dimensiones|"
            r"tama(?:ñ|n)o"
            r")\b",
            t_original,
            re.IGNORECASE,
        )

        if inicio_dimension:
            fragmento = t_original[
                inicio_dimension.start():
            ]

            # Si después comienza otro campo técnico, cortamos allí.
            fragmento = re.split(
                r"\s+(?:rango|conexion|conexión|salida|"
                r"tipo\s+de\s+entrada|alimentacion|alimentación|"
                r"voltaje|material|presion|presión)\s*:?\s*",
                fragmento,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]

            medidas = re.findall(
                r"[-+]?\d+(?:[\.,]\d+)?\s*"
                r"(?:mm|cm|pulg(?:ada|adas)?|inches|inch|in|m)\b",
                fragmento,
                re.IGNORECASE,
            )

            if medidas:
                valor = re.sub(
                    r"\s+",
                    " ",
                    fragmento,
                ).strip(" ,.;")

                if valor:
                    campos["dimensiones"] = valor

    # ------------------------------------------------------------
    # 5. Protección IP
    # ------------------------------------------------------------
    ip = re.search(r"\bip\s*(\d{2})\b", t, re.IGNORECASE)

    if ip:
        campos["proteccion"] = f"IP{ip.group(1)}"

    # ------------------------------------------------------------
    # 6. Voltaje / alimentación
    # ------------------------------------------------------------
    # Primero buscamos una declaración explícita:
    #
    # La captura termina cuando comienza otro campo técnico.
    alimentacion_explicita = re.search(
        r"\b(?:"
        r"alimentacion|alimentación|"
        r"voltaje|"
        r"tension|tensión"
        r")\s*:?\s*(.+?)"
        r"(?=\s+(?:"
        r"entrada|"
        r"señal\s+de\s+entrada|senal\s+de\s+entrada|"
        r"salida|"
        r"señal\s+de\s+salida|senal\s+de\s+salida|"
        r"rango|"
        r"dimensiones?|"
        r"tama(?:ñ|n)o|"
        r"conexion|conexión|"
        r"material|"
        r"presion|presión"
        r")\s*:?\s*|$)",
        t_original,
        re.IGNORECASE,
    )

    if alimentacion_explicita:
        valor = re.sub(
            r"\s+",
            " ",
            alimentacion_explicita.group(1),
        ).strip(" ,.;")

        if valor:
            campos["voltaje"] = valor

    # ------------------------------------------------------------
    # Voltaje suelto
    # ------------------------------------------------------------
    if "voltaje" not in campos:
        voltaje_suelto = re.search(
            r"(?<![\d\-])"
            r"([-+]?\d+(?:[\.,]\d+)?)\s*"
            r"(VAC|VDC|V)\b",
            t_original,
            re.IGNORECASE,
        )

        if voltaje_suelto:
            posicion_voltaje = voltaje_suelto.start(1)

            dentro_de_entrada = (
                entrada is not None
                and entrada.start(1)
                <= posicion_voltaje
                < entrada.end(1)
            )

            dentro_de_salida = (
                salida is not None
                and salida.start(1)
                <= posicion_voltaje
                < salida.end(1)
            )

            if not dentro_de_entrada and not dentro_de_salida:
                valor, unidad = voltaje_suelto.groups()

                campos["voltaje"] = (
                    f"{valor} {unidad.upper()}"
                )
    return campos

def extraer_campos_query(texto: str) -> dict:
    """
    Interfaz pública para extraer campos técnicos declarados por el cliente.

    Permite que la capa de orquestación consulte la estructura técnica
    sin depender directamente de una función privada del catálogo.
    """
    return _extraer_campos_query(texto)

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

    # Expresiones conversacionales y comerciales.
    # No representan la familia técnica solicitada.
    "necesito",
    "necesitamos",
    "requiero",
    "requerimos",
    "quiero",
    "queremos",
    "busco",
    "buscamos",
    "solicito",
    "solicitamos",
    "deseo",
    "deseamos",
    "gustaria",
    "cotizar",
    "cotizacion",
    "comprar",
    "compra",
    "producto",
    "productos",
    "equipo",
    "equipos",
    "favor",
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