"""
knowledge.py — Base de conocimiento técnico de NIA
Carga los JSONL de Creus y Kuphaldt al arrancar y los mantiene en memoria.
Expone búsqueda por dominio para alimentar al agente de preguntas.
"""

import json
import os
from pathlib import Path
from difflib import SequenceMatcher
from typing import List, Dict, Optional

# Ruta a los archivos JSONL (junto al código)
BASE_DIR   = Path(__file__).parent / "conocimiento"
RAG_FILE   = BASE_DIR / "book_rag_ready_all.jsonl"
CONC_FILE  = BASE_DIR / "book_concepts_all.jsonl"

# ─── Mapeo de palabras clave → dominio ───────────────────────────────────────
DOMINIO_KEYWORDS = {
    "transmisores":               ["transmisor", "transmitter", "4-20ma", "hart", "fieldbus"],
    "presion":                    ["presión", "pressure", "manómetro", "manometer", "psi", "bar", "pascal"],
    "temperatura":                ["temperatura", "temperature", "termopar", "thermocouple", "rtd", "pt100"],
    "nivel":                      ["nivel", "level", "tanque", "tank", "ultrasonido", "radar", "tdr", "flotador"],
    "caudal":                     ["caudal", "flujo", "flow", "medidor", "caudalímetro", "flowmeter"],
    "valvulas_control":           ["válvula", "valve", "actuador", "actuator", "control valve"],
    "control_pid":                ["pid", "controlador", "controller", "lazo", "loop", "setpoint"],
    "analitica_proceso":          ["ph", "conductividad", "oxígeno", "disuelto", "turbidez", "analyzer"],
    "plc_automatizacion":         ["plc", "scada", "hmi", "automatización", "automation", "siemens", "allen"],
    "calibracion":                ["calibración", "calibration", "patrón", "trazabilidad", "span", "zero"],
    "comunicaciones_industriales":["modbus", "profibus", "devicenet", "ethernet", "protocolo", "protocol"],
    "seguridad_funcional":        ["sil", "safety", "seguridad", "iec 61511", "iec 61508", "funcional"],
    "vibracion_mantenimiento":    ["vibración", "vibration", "mantenimiento", "predictivo", "bearing"],
}

# ─── Carga en memoria al importar ────────────────────────────────────────────
_chunks:   List[Dict] = []
_concepts: List[Dict] = []

def _cargar():
    global _chunks, _concepts
    if _chunks:
        return  # ya cargado

    if RAG_FILE.exists():
        with open(RAG_FILE, encoding="utf-8") as f:
            _chunks = [json.loads(l) for l in f if l.strip()]

    if CONC_FILE.exists():
        with open(CONC_FILE, encoding="utf-8") as f:
            _concepts = [json.loads(l) for l in f if l.strip()]

_cargar()

# ─── Detección de dominio ─────────────────────────────────────────────────────
def detectar_dominio(texto: str) -> Optional[str]:
    """
    Detecta el dominio técnico más probable dado el texto del cliente.
    Retorna el nombre del dominio o None si no hay coincidencia clara.
    """
    texto_lower = texto.lower()
    scores = {}
    for dominio, keywords in DOMINIO_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in texto_lower)
        if score > 0:
            scores[dominio] = score
    return max(scores, key=scores.get) if scores else None

# ─── Búsqueda de chunks relevantes ───────────────────────────────────────────
def buscar_contexto(texto: str, top_k: int = 5) -> List[Dict]:
    """
    Busca los chunks más relevantes para el texto dado.
    Primero filtra por dominio, luego rankea por similitud textual.
    """
    dominio = detectar_dominio(texto)
    pool    = [c for c in _chunks if c.get("domain") == dominio] if dominio else _chunks

    if not pool:
        pool = _chunks

    # Rankea por similitud del texto de búsqueda
    scored = []
    for chunk in pool:
        sim = SequenceMatcher(
            None,
            texto.lower(),
            (chunk.get("text") or chunk.get("search_text") or "").lower()[:500]
        ).ratio()
        scored.append((sim, chunk))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:top_k]]

# ─── Conceptos del dominio ────────────────────────────────────────────────────
def conceptos_del_dominio(dominio: str, top_k: int = 10) -> List[str]:
    """Retorna los términos técnicos más relevantes de un dominio."""
    return [
        c["term"] for c in _concepts
        if c.get("domain") == dominio
    ][:top_k]

# ─── Resumen de contexto para el agente ──────────────────────────────────────
def contexto_para_agente(texto: str) -> dict:
    """
    Prepara el contexto completo que necesita questions_agent.py:
    - dominio detectado
    - chunks relevantes
    - términos técnicos del dominio
    """
    dominio = detectar_dominio(texto)
    chunks  = buscar_contexto(texto, top_k=4)
    terminos = conceptos_del_dominio(dominio, top_k=8) if dominio else []

    extractos = []
    for c in chunks:
        txt = (c.get("text") or "")[:400]
        if txt.strip():
            extractos.append(txt)

    return {
        "dominio":   dominio or "general",
        "extractos": extractos,
        "terminos":  terminos,
    }

def _es_chunk_tecnico_util(doc: dict) -> bool:
    """
    Determina si un chunk sirve como contexto técnico.

    Regla general:
    - elimina ruido documental;
    - elimina portadas, copyright, índices, tablas de contenido;
    - conserva contenido con señales técnicas industriales.
    """
    texto = _normalizar_texto_retrieval(
        (doc.get("text") or "") + " " + (doc.get("search_text") or "")
    )

    if not texto:
        return False

    if len(texto) < 120:
        return False

    bloqueados = {
        "isbn",
        "derechos reservados",
        "prohibida su reproduccion",
        "prohibida la reproduccion",
        "copyright",
        "editorial",
        "alfomega",
        "impreso en",
        "printed in",
        "todos los derechos",
        "ninguna parte de esta publicacion",
        "miembro de la camara nacional",
        "datos de catalogacion",
        "biblioteca",
        "contents",
        "table of contents",
        "review of fundamental principles",
        "preface",
        "acknowledgements",
        "acknowledgments",
        "indice",
        "índice",
        "index",
        "contents",
        "table of contents",
        "review of fundamental principles",
    }

    if any(palabra in texto for palabra in bloqueados):
        return False

    # Señales técnicas generales: derivadas de los dominios disponibles.
    señales_tecnicas = set()

    for keywords in DOMINIOS_TECNICOS_KEYWORDS.values():
        señales_tecnicas.update(_normalizar_texto_retrieval(k) for k in keywords)

    if any(senal in texto for senal in señales_tecnicas):
        return True

    # También aceptamos si el documento ya tiene un dominio técnico conocido.
    domain = doc.get("domain")

    if domain in DOMINIOS_TECNICOS_KEYWORDS:
        return True

    return False

# ============================================================
# Helpers generales para retrieval técnico industrial
# ============================================================

def _normalizar_texto_retrieval(texto: str) -> str:
    """
    Normaliza texto para búsquedas técnicas:
    - minúsculas
    - sin acentos
    - espacios limpios

    No cambia el texto original que se mostrará al usuario.
    Solo se usa para comparar y puntuar.
    """
    import re
    import unicodedata

    texto = (texto or "").lower().strip()

    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")

    texto = re.sub(r"[^a-z0-9_ñ\s/-]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()

    return texto


def _tokens_retrieval(texto: str) -> list[str]:
    """
    Extrae tokens útiles para retrieval técnico.

    Evita basarse en frases exactas.
    La idea es que funcione para muchas consultas distintas.
    """
    texto_norm = _normalizar_texto_retrieval(texto)

    stopwords = {
        "para",
        "con",
        "sin",
        "por",
        "una",
        "uno",
        "unos",
        "unas",
        "del",
        "las",
        "los",
        "que",
        "como",
        "cual",
        "cuales",
        "necesito",
        "quiero",
        "busco",
        "cotizar",
        "cotizacion",
        "producto",
        "equipo",
        "industrial",
        "tipo",
        "modelo",
        "marca",
        "sistema",
        "aplicacion",
        "aplicación",
        "uso",
        "usar",
        "tengo",
        "requiere",
        "requiero",
    }

    tokens = []

    # Tokens técnicos cortos que sí son importantes.
    # Ejemplos:
    # - pH: analítica de proceso
    # - ORP: potencial redox
    # - DO: oxígeno disuelto
    # - CV: coeficiente de válvula
    tokens_cortos_tecnicos = {
        "ph",
        "orp",
        "do",
        "cv",
        "ma",
        "mv",
    }

    for token in texto_norm.split():
        token = token.strip(".,;:()[]{}¿?¡!+-=*")

        if not token:
            continue

        if len(token) < 3 and token not in tokens_cortos_tecnicos:
            continue

        if token in stopwords:
            continue

        tokens.append(token)

    return tokens


DOMINIOS_TECNICOS_KEYWORDS = {
    "transmisores": {
        "transmisor",
        "transmisores",
        "sensor",
        "sensores",
        "senal",
        "4-20ma",
        "hart",
        "salida",
        "entrada",
    },
    "presion": {
        "presion",
        "diferencial",
        "manometro",
        "vacio",
        "bar",
        "psi",
        "pascal",
        "mbar",
    },
    "caudal": {
        "caudal",
        "flujo",
        "flujometro",
        "rotametro",
        "rotametros",
        "magnetico",
        "ultrasonico",
        "vortex",
        "turbina",
        "coriolis",
    },
    "temperatura": {
        "temperatura",
        "rtd",
        "pt100",
        "termocupla",
        "termopar",
        "termistor",
        "termopozo",
        "calor",
    },
    "nivel": {
        "nivel",
        "tanque",
        "radar",
        "flotador",
        "ultrasonico",
        "capacitativo",
        "hidrostatico",
    },
    "valvulas_control": {
        "valvula",
        "valvulas",
        "actuador",
        "posicionador",
        "obturador",
        "asiento",
        "cv",
        "vapor",
    },
    "control_pid": {
        "pid",
        "controlador",
        "control",
        "lazo",
        "setpoint",
        "proporcional",
        "integral",
        "derivativo",
    },
    "plc_automatizacion": {
        "plc",
        "automatizacion",
        "automatizar",
        "scada",
        "modbus",
        "entrada",
        "salida",
        "digital",
        "analogica",
    },
    "calibracion": {
        "calibracion",
        "calibrador",
        "patron",
        "ajuste",
        "verificacion",
        "certificado",
        "exactitud",
        "precision",
    },
    "seguridad_funcional": {
        "seguridad",
        "sil",
        "riesgo",
        "alarma",
        "interlock",
        "proteccion",
        "falla",
        "redundancia",
    },
    "comunicaciones_industriales": {
        "comunicacion",
        "comunicaciones",
        "protocolo",
        "modbus",
        "hart",
        "profibus",
        "ethernet",
        "rs485",
        "bus",
    },
    "vibracion_mantenimiento": {
        "vibracion",
        "vibraciones",
        "mantenimiento",
        "predictivo",
        "rodamiento",
        "rodamientos",
        "desbalance",
        "alineacion",
        "maquina",
        "maquinas",
        "motor",
        "motores",
        "bomba",
        "bombas",
        "rotativo",
        "rotativos",
    },
    "analitica_proceso": {
        "ph",
        "orp",
        "do",
        "redox",
        "conductividad",
        "conductivo",
        "conductiva",
        "analizador",
        "analizadores",
        "analitica",
        "analitico",
        "oxigeno",
        "turbidez",
        "cloro",
        "agua",
        "liquido",
        "liquidos",
    },
}


def inferir_dominio_tecnico(consulta: str) -> str | None:
    """
    Infiere el dominio técnico más probable desde una consulta.

    Esta función NO decide productos.
    Solo ayuda a enfocar la búsqueda en libros técnicos.

    Es una solución general porque:
    - usa tokens normalizados;
    - compara contra dominios técnicos amplios;
    - no depende de una frase exacta de prueba.
    """
    tokens = _tokens_retrieval(consulta)

    if not tokens:
        return None

    mejor_dominio = None
    mejor_score = 0

    for dominio, keywords in DOMINIOS_TECNICOS_KEYWORDS.items():
        score = 0

        dominio_tokens = set(_tokens_retrieval(dominio.replace("_", " ")))
        keywords_norm = {_normalizar_texto_retrieval(k) for k in keywords}

        for token in tokens:
            if token in keywords_norm:
                score += 3

            if token in dominio_tokens:
                score += 2

            # Coincidencia parcial controlada para variantes:
            # ejemplo: "transmisores" vs "transmisor"
            for kw in keywords_norm:
                if len(token) >= 5 and (token in kw or kw in token):
                    score += 1

        if score > mejor_score:
            mejor_score = score
            mejor_dominio = dominio

    # Umbral mínimo para no forzar dominio cuando la consulta es ambigua.
    if mejor_score < 2:
        return None

    return mejor_dominio


def _score_relevancia_local(doc: dict, consulta: str) -> int:
    """
    Reordena candidatos de Mongo usando coincidencia general de tokens.

    No usa frases específicas de casos.
    Premia:
    - tokens de la consulta presentes en el chunk;
    - coincidencia con dominio;
    - coincidencia con capítulo/sección/tipo de contenido.
    """
    tokens = _tokens_retrieval(consulta)

    if not tokens:
        return 0

    texto_doc = _normalizar_texto_retrieval(
        " ".join(
            [
                str(doc.get("text") or ""),
                str(doc.get("search_text") or ""),
                str(doc.get("domain") or ""),
                str(doc.get("content_type") or ""),
                str(doc.get("chapter") or ""),
                str(doc.get("section") or ""),
                str(doc.get("title") or ""),
            ]
        )
    )

    score = 0

    for token in tokens:
        ocurrencias = texto_doc.count(token)

        if ocurrencias:
            score += min(ocurrencias, 5) * 2

    dominio_doc = doc.get("domain") or ""
    dominio_inferido = inferir_dominio_tecnico(consulta)

    if dominio_inferido and dominio_doc == dominio_inferido:
        score += 8

    # Premiar chunks con contenido explicativo, no solo listas.
    content_type = (doc.get("content_type") or "").lower()

    if content_type in {"concepto", "explicacion", "teoria", "procedimiento"}:
        score += 3

    return score

# ============================================================
# Conocimiento técnico industrial desde MongoDB
# ============================================================

async def buscar_contexto_tecnico_libros(
    consulta: str,
    domain: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """
    Busca contexto técnico en la colección MongoDB `nia_knowledge_chunks`.

    Esta función NO recomienda productos.
    Solo devuelve fragmentos técnicos útiles para que NIA entienda mejor
    la necesidad del cliente antes de buscar en catálogo real.

    Parámetros:
    - consulta: texto técnico del cliente.
    - domain: dominio opcional, por ejemplo: "presion", "caudal", "temperatura".
    - limit: cantidad máxima de fragmentos a devolver.

    Retorna:
    - Lista de fragmentos técnicos normalizados.
    """
    import re
    from memory import get_db

    consulta = (consulta or "").strip()

    if not consulta:
        return []

    limit = max(1, min(int(limit or 5), 10))

    if not domain:
        domain = inferir_dominio_tecnico(consulta)

    consulta_busqueda = " ".join(_tokens_retrieval(consulta)[:12]) or consulta
    candidate_limit = max(limit * 8, 30)

    db = get_db()
    collection = db["nia_knowledge_chunks"]

    filtro: dict = {
        "$text": {
            "$search": consulta_busqueda
        }
    }

    if domain:
        filtro["domain"] = domain

    projection_base = {
        "_id": 0,
        "chunk_id": 1,
        "source_id": 1,
        "title": 1,
        "author": 1,
        "edition": 1,
        "language": 1,
        "page": 1,
        "chapter": 1,
        "section": 1,
        "domain": 1,
        "content_type": 1,
        "text": 1,
        "search_text": 1,
        "metadata": 1,
    }

    # Esta proyección solo se puede usar cuando la consulta tiene $text.
    projection_text = {
        **projection_base,
        "score": {"$meta": "textScore"},
    }

    try:
        cursor = (
            collection
            .find(filtro, projection_text)
            .sort([("score", {"$meta": "textScore"})])
            .limit(candidate_limit)
        )

        resultados = await cursor.to_list(length=candidate_limit)
        
    except Exception:
        # Fallback seguro:
        # Si el índice de texto falla por cualquier razón, hacemos una búsqueda regex simple.
        patron = re.escape(consulta_busqueda[:120])

        filtro_fallback: dict = {
            "search_text": {
                "$regex": patron,
                "$options": "i",
            }
        }

        if domain:
            filtro_fallback["domain"] = domain

        cursor = collection.find(filtro_fallback, projection_base).limit(candidate_limit)
        resultados = await cursor.to_list(length=candidate_limit)
        
    # Fallback adicional:
    # Si Mongo Text Search no devuelve resultados pero sí tenemos dominio,
    # traemos candidatos por dominio y los reordenamos localmente.
    #
    # Esto ayuda en consultas técnicas cortas como 
    # donde el índice de texto puede no recuperar bien.
    if not resultados and domain:
        cursor = (
            collection
            .find({"domain": domain}, projection_base)
            .limit(candidate_limit)
        )
        resultados = await cursor.to_list(length=candidate_limit)
        
    resultados = sorted(
        resultados,
        key=lambda doc: _score_relevancia_local(doc, consulta),
        reverse=True,
    )
    
    salida: list[dict] = []

    for doc in resultados:
        if not _es_chunk_tecnico_util(doc):
            continue

        texto = (doc.get("text") or "").strip()

        if not texto:
            continue

        salida.append(
            {
                "chunk_id": doc.get("chunk_id"),
                "source_id": doc.get("source_id"),
                "title": doc.get("title"),
                "author": doc.get("author"),
                "edition": doc.get("edition"),
                "language": doc.get("language"),
                "page": doc.get("page"),
                "chapter": doc.get("chapter"),
                "section": doc.get("section"),
                "domain": doc.get("domain"),
                "content_type": doc.get("content_type"),
                "text": texto,
                "score": doc.get("score"),
                "metadata": doc.get("metadata") or {},
            }
        )

    return salida[:limit]

# ============================================================
# Construcción de contexto técnico para NIA
# ============================================================

async def construir_contexto_tecnico_para_nia(
    consulta: str,
    limit: int = 3,
    max_chars_por_fragmento: int = 700,
) -> dict:
    """
    Construye un paquete de contexto técnico para que NIA pueda usarlo
    en una respuesta o en generación de preguntas técnicas.

    Importante:
    - No recomienda productos.
    - No reemplaza catálogo.
    - No inventa disponibilidad, precio ni compatibilidad.
    - Solo resume fragmentos técnicos recuperados desde los libros.
    """
    consulta = (consulta or "").strip()

    if not consulta:
        return {
            "ok": False,
            "domain": None,
            "consulta": consulta,
            "fragmentos": [],
            "contexto": "",
        }

    domain = inferir_dominio_tecnico(consulta)

    fragmentos = await buscar_contexto_tecnico_libros(
        consulta=consulta,
        domain=domain,
        limit=limit,
    )

    partes_contexto: list[str] = []

    for idx, frag in enumerate(fragmentos, start=1):
        texto = (frag.get("text") or "").strip()

        if not texto:
            continue

        if len(texto) > max_chars_por_fragmento:
            texto = texto[:max_chars_por_fragmento].rstrip() + "..."

        titulo = frag.get("title") or "Fuente técnica"
        pagina = frag.get("page")
        dominio = frag.get("domain") or "general"

        encabezado = f"[{idx}] {titulo}"
        if pagina:
            encabezado += f" · pág. {pagina}"
        encabezado += f" · dominio: {dominio}"

        partes_contexto.append(f"{encabezado}\n{texto}")

    contexto = "\n\n".join(partes_contexto).strip()

    return {
        "ok": bool(contexto),
        "domain": domain,
        "consulta": consulta,
        "fragmentos": fragmentos,
        "contexto": contexto,
    }