"""
questions_agent.py — Agente de 3 preguntas estratégicas v2
Basado en los campos técnicos reales del catálogo ViaIndustrial
(289.017 productos · separador ¦ en desc_larga) + OpenAI GPT-4o-mini

LÓGICA DE 3 ESCENARIOS:
  Escenario 1: Cliente tiene código/referencia/marca/características → 0-2 preguntas
  Escenario 2: Cliente tiene el nombre del producto pero nada más → 3 preguntas
  Escenario 3: Cliente solo tiene la necesidad → 3 preguntas (identifica familia primero)

Las preguntas apuntan EXACTAMENTE a los campos de desc_larga del catálogo:
  Q1: variable/aplicación/proceso → identifica nivel_1 del catálogo
  Q2: rango/condición → campos_q2 de esa categoría
  Q3: señal/interfaz/material → campos_q3 de esa categoría
"""

import os
import re
import logging
import httpx
from knowledge import construir_contexto_tecnico_para_nia
from product_fields import get_campos, detectar_categoria, CAMPOS_POR_CATEGORIA

logger = logging.getLogger("nia.questions_agent")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL          = "gpt-4o-mini"

# ─── Detectar escenario ───────────────────────────────────────────────────────
CODIGO_RE = re.compile(r'\b(\d{6})\b')
REF_RE    = re.compile(r'\b(P\d{3,}|[A-Z]{1,4}\d{3,}[A-Z0-9]*)\b', re.IGNORECASE)

# Términos que indican que el cliente ya tiene características técnicas
TERMINOS_TECNICOS = [
    "bar", "psi", "mbar", "kpa", "mpa",             # presión
    "°c", "celsius", "fahrenheit",                    # temperatura
    "gpm", "lpm", "m3/h", "l/min",                   # caudal
    "4-20", "0-10v", "hart", "modbus", "profibus",   # señal/protocolo
    "npt", "bsp", "brida", "rosca",                  # conexión
    "ip65", "ip67", "atex", "ex",                    # protección
    "rele", "relé", "ssr", "transistor",              # salida
    "termopar", "rtd", "pt100", "tc tipo",           # sensor temperatura
    "inox", "bronce", "acero",                        # material
]

def detectar_escenario(texto: str) -> str:
    """
    Detecta cuál de los 3 escenarios aplica.
    Escenario 1: tiene código/referencia/marca/características técnicas
    Escenario 2: tiene nombre del producto pero no más
    Escenario 3: solo tiene la necesidad
    """
    t = texto.lower()

    # Escenario 1A: tiene código exacto o referencia
    if CODIGO_RE.search(texto) or REF_RE.search(texto):
        return "escenario_1_codigo"

    # Escenario 1B: tiene características técnicas concretas
    terminos_presentes = sum(1 for term in TERMINOS_TECNICOS if term in t)
    if terminos_presentes >= 2:
        return "escenario_1_caracteristicas"

    # Escenario 2: tiene nombre del producto (detectamos categoría)
    categoria = detectar_categoria(texto)
    if categoria != "default":
        return "escenario_2_nombre"

    # Escenario 3: solo necesidad
    return "escenario_3_necesidad"

# ─── Prompt por escenario ─────────────────────────────────────────────────────
SYSTEM_BASE = """Eres un experto técnico en instrumentación industrial de ViaIndustrial.
Tu ÚNICA función es generar exactamente 3 preguntas para identificar el producto correcto.

REGLAS ABSOLUTAS:
- Genera EXACTAMENTE 3 preguntas. Ni más, ni menos.
- Cada pregunta apunta a UN campo técnico diferente.
- Las preguntas deben ser cortas, concretas y directas.
- NO saludes, NO expliques, NO cotices, NO recomiendes marcas.
- NO repitas información que el cliente ya dio.
- Adapta el lenguaje al nivel técnico del cliente.
- Responde SOLO con las 3 preguntas numeradas. Nada más."""

def _prompt_escenario_1(texto: str, campos: dict) -> str:
    return f"""{SYSTEM_BASE}

ESCENARIO: El cliente tiene información parcial (marca, modelo o características).
Lo que dijo: "{texto}"

Campos que FALTAN del catálogo para encontrar el SKU exacto:
- Campos de rango/condición: {campos['campos_q2']}
- Campos de interfaz/material: {campos['campos_q3']}

Genera 3 preguntas para completar SOLO los campos que faltan.
Si ya dio el rango, NO lo preguntes. Pregunta lo que falta para llegar al código exacto."""

def _prompt_escenario_2(texto: str, categoria: str, campos: dict) -> str:
    return f"""{SYSTEM_BASE}

ESCENARIO: El cliente sabe el nombre del producto pero nada más.
Lo que dijo: "{texto}"
Categoría detectada en el catálogo: {categoria}

Campos técnicos que discriminan SKUs en esta categoría:
- Q2 (rango/condición): {campos['campos_q2']}
- Q3 (interfaz/material): {campos['campos_q3']}

Pregunta sugerida Q2: {campos['q2_pregunta']}
Pregunta sugerida Q3: {campos['q3_pregunta']}

Genera 3 preguntas en este orden:
1. Aplicación + proceso (para confirmar la subcategoría exacta)
2. {campos['q2_pregunta']}
3. {campos['q3_pregunta']}

Adapta las preguntas al contexto del cliente."""

def _normalizar_texto_simple(texto: str) -> str:
    """
    Normalización liviana para validar si el contexto técnico recuperado
    realmente tiene relación con la consulta del cliente.

    No se usa para responder al cliente.
    Solo sirve como guardrail interno.
    """
    import re
    import unicodedata

    texto = (texto or "").lower().strip()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    texto = re.sub(r"[^a-z0-9ñ\s/-]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()

    return texto


def _tokens_tecnicos_consulta(texto: str) -> set[str]:
    """
    Extrae tokens útiles de la consulta del cliente para validar relevancia.

    Evita usar palabras demasiado generales como:
    necesito, producto, equipo, proceso, industrial, etc.
    """
    texto_norm = _normalizar_texto_simple(texto)

    stopwords = {
        "necesito",
        "quiero",
        "busco",
        "tengo",
        "para",
        "con",
        "sin",
        "una",
        "uno",
        "unos",
        "unas",
        "del",
        "los",
        "las",
        "que",
        "como",
        "cual",
        "cuales",
        "producto",
        "equipo",
        "instrumento",
        "instrumentos",
        "industrial",
        "proceso",
        "linea",
        "línea",
        "usar",
        "saber",
        "automatizar",  # demasiado amplio para usarlo como prueba de relevancia
    }

    tokens = set()

    for token in texto_norm.split():
        token = token.strip()

        if len(token) < 4:
            continue

        if token in stopwords:
            continue

        tokens.add(token)

    return tokens


def _contexto_tecnico_es_util(
    texto_cliente: str,
    contexto_tecnico: str,
    dominio: str | None = None,
) -> bool:
    """
    Decide si el contexto recuperado desde libros debe entrar al prompt.

    Regla:
    - Si la consulta es demasiado genérica, no usar contexto.
    - Si el contexto no comparte tokens técnicos con la consulta, no usarlo.
    - Si el dominio fue inferido pero el texto del contexto no lo soporta, no usarlo.

    Esto evita contaminar preguntas con fragmentos irrelevantes.
    """
    contexto_norm = _normalizar_texto_simple(contexto_tecnico)
    tokens = _tokens_tecnicos_consulta(texto_cliente)

    if not contexto_norm:
        return False

    # Si no hay suficientes tokens técnicos en la consulta, es mejor NO usar libros.
    # Ejemplo: "necesito automatizar una línea de proceso y no sé qué instrumento usar"
    # es muy general; debe generar preguntas abiertas, no traer fragmentos forzados.
    if len(tokens) < 2:
        return False

    coincidencias = [token for token in tokens if token in contexto_norm]

    if len(coincidencias) >= 2:
        return True

    # Casos técnicos cortos pero válidos.
    # Ejemplo: "pH", "RTD", "PT100", "PLC", "Modbus".
    tokens_especiales = {"ph", "rtd", "pt100", "plc", "modbus", "hart", "orp"}

    if tokens_especiales.intersection(tokens) and tokens_especiales.intersection(set(contexto_norm.split())):
        return True

    return False

def _prompt_escenario_3(texto: str,dominio: str,contexto_tecnico: str,campos: dict,) -> str:
    """
    Prompt para cuando el cliente solo expresa una necesidad.

    Usa contexto técnico recuperado desde MongoDB/libros industriales
    únicamente como apoyo para formular mejores preguntas.
    No recomienda productos y no reemplaza el catálogo.
    """
    bloque_contexto = ""

    if contexto_tecnico:
        # No metemos contexto infinito al prompt.
        # Solo damos una muestra controlada para orientar preguntas.
        contexto_recortado = contexto_tecnico[:1600].strip()

        bloque_contexto = f"""
Contexto técnico recuperado desde libros industriales:
{contexto_recortado}

Usa este contexto SOLO para formular mejores preguntas técnicas.
No cites los libros.
No recomiendes productos.
No inventes compatibilidad.
"""

    return f"""{SYSTEM_BASE}

ESCENARIO: El cliente solo tiene la necesidad. No sabe el nombre del producto.
Lo que dijo: "{texto}"
Dominio técnico detectado: {dominio}
{bloque_contexto}

Campos del catálogo que llevan al producto:
- Q2 (rango/condición): {campos['campos_q2']}
- Q3 (interfaz/material): {campos['campos_q3']}

Genera 3 preguntas en este orden:
1. Proceso completo: qué mide/controla, dónde va instalado, qué fluido/material.
2. Rango + condiciones del proceso: temperatura, presión, tamaño, capacidad o escala.
3. Señal de salida + entorno de instalación: protocolo, área clasificada, material o conexión.

El objetivo es llegar al SKU exacto en máximo 3 preguntas."""

# ─── Llamada a OpenAI ─────────────────────────────────────────────────────────
async def _llamar_openai(prompt: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                json={
                    "model": MODEL,
                    "max_tokens": 400,
                    "temperature": 0.2,
                    "messages": [{"role": "user", "content": prompt}]
                },
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}
            )
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Error OpenAI questions_agent: {e}")
        return ""

# ─── Parser de preguntas ──────────────────────────────────────────────────────
def _parsear_preguntas(texto: str) -> list:
    preguntas = []
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea:
            continue
        # Remover numeración 1. 1) • - *
        for prefijo in ["1.", "2.", "3.", "1)", "2)", "3)", "•", "-", "*"]:
            if linea.startswith(prefijo):
                linea = linea[len(prefijo):].strip()
                break
        if len(linea) > 8:
            preguntas.append(linea)
    if len(preguntas) >= 3:
        return preguntas[:3]
    # Fallback si el parser falla
    while len(preguntas) < 3:
        preguntas.append("¿Tienes alguna especificación técnica adicional?")
    return preguntas

# ─── Fallbacks por escenario ──────────────────────────────────────────────────
FALLBACKS = {
    "escenario_1_codigo": [
        "¿Cuál es el rango de operación que necesitas?",
        "¿Cuál es la conexión al proceso (tamaño y tipo)?",
        "¿Qué señal de salida o protocolo necesitas?",
    ],
    "escenario_1_caracteristicas": [
        "¿Cuál es el rango de operación exacto?",
        "¿Cuál es la conexión al proceso?",
        "¿Qué señal de salida necesitas (4-20 mA, Modbus, HART)?",
    ],
    "escenario_2_nombre": [
        "¿En qué proceso o aplicación va instalado?",
        "¿Cuál es el rango de operación y las condiciones del proceso?",
        "¿Qué señal de salida y tipo de conexión necesitas?",
    ],
    "escenario_3_necesidad": [
        "¿Qué variable necesitas medir o controlar, y en qué tipo de proceso?",
        "¿Cuál es el rango de operación y las condiciones físicas del proceso?",
        "¿Qué señal de salida necesitas y el área es clasificada?",
    ],
}

# ─── Función principal ────────────────────────────────────────────────────────
async def generar_preguntas(texto_cliente: str) -> list:
    """
    Genera exactamente 3 preguntas estratégicas para identificar el producto.

    Flujo:
    1. Detecta escenario (código/características/nombre/necesidad)
    2. Detecta categoría del catálogo y dominio técnico
    3. Obtiene campos técnicos reales de esa categoría
    4. Consulta libros Creus/Kuphaldt para contexto (escenario 3)
    5. GPT-4o-mini genera 3 preguntas alineadas con los campos del catálogo
    """
    escenario  = detectar_escenario(texto_cliente)
    categoria  = detectar_categoria(texto_cliente)
    campos     = get_campos(categoria)

    logger.info(f"questions_agent: escenario={escenario} categoría={categoria}")

    # ------------------------------------------------------------
    # Contexto técnico desde libros industriales
    # ------------------------------------------------------------
    # Solo lo usamos para escenario_3_necesidad.
    # No debe interferir cuando el cliente ya trae código, referencia,
    # características claras o nombre de producto.
    dominio = campos.get("dominio", "general")
    contexto_tecnico = ""

    if escenario == "escenario_3_necesidad":
        try:
            paquete_tecnico = await construir_contexto_tecnico_para_nia(
                texto_cliente,
                limit=3,
                max_chars_por_fragmento=500,
            )

            if paquete_tecnico.get("ok"):
                dominio_candidato = paquete_tecnico.get("domain") or dominio
                contexto_candidato = paquete_tecnico.get("contexto") or ""

                if _contexto_tecnico_es_util(
                    texto_cliente,
                    contexto_candidato,
                    dominio_candidato,
                ):
                    dominio = dominio_candidato
                    contexto_tecnico = contexto_candidato
                else:
                    logger.info(
                        "Contexto técnico descartado por baja relevancia: dominio=%s texto=%s",
                        dominio_candidato,
                        texto_cliente[:80],
                    )
                    contexto_tecnico = ""

        except Exception as e:
            logger.warning(
                "No se pudo construir contexto técnico para preguntas: %s",
                e,
            )
            contexto_tecnico = ""

    # Construir prompt según escenario
    if escenario == "escenario_1_codigo":
        # Tiene código → no debería llegar aquí, pero por si acaso
        return FALLBACKS["escenario_1_codigo"]

    elif escenario == "escenario_1_caracteristicas":
        prompt = _prompt_escenario_1(texto_cliente, campos)

    elif escenario == "escenario_2_nombre":
        prompt = _prompt_escenario_2(texto_cliente, categoria, campos)

    else:  # escenario_3_necesidad
        prompt = _prompt_escenario_3(texto_cliente, dominio, contexto_tecnico, campos,)

    # Llamar a OpenAI
    respuesta = await _llamar_openai(prompt)

    if not respuesta:
        logger.warning(f"OpenAI no respondió, usando fallback para {escenario}")
        return FALLBACKS.get(escenario, FALLBACKS["escenario_3_necesidad"])

    preguntas = _parsear_preguntas(respuesta)
    logger.info(f"Preguntas generadas: {preguntas}")
    return preguntas
