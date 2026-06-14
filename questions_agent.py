"""
questions_agent.py — Agente de 3 preguntas estratégicas v2
Basado en los campos técnicos reales del catálogo ViaIndustrial
(catálogo real · separadores ■, •, ▪ y compatibilidad histórica ¦)

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

# ─── Preguntas dinámicas desde campos reales del catálogo ────────────────────

VALORES_EJEMPLO_NO_UTILES = {
    "",
    "-",
    "--",
    "no",
    "n/a",
    "na",
    "no aplica",
    "sin dato",
    "sin datos",
    "no disponible",
    "ninguno",
    "ninguna",
}


def _humanizar_nombre_campo(campo: str) -> str:
    """
    Convierte el nombre normalizado del catálogo en una etiqueta legible.

    No cambia el significado del campo.
    Solo recupera tildes y mejora la presentación.
    """
    campo_norm = _normalizar_texto_simple(campo)

    reemplazos = {
        "conexion": "conexión",
        "presion": "presión",
        "alimentacion": "alimentación",
        "resolucion": "resolución",
        "precision": "precisión",
        "dimension": "dimensión",
        "dimensiones": "dimensiones",
        "tamano": "tamaño",
        "senal": "señal",
        "proteccion": "protección",
        "comunicacion": "comunicación",
    }

    palabras = [
        reemplazos.get(palabra, palabra)
        for palabra in campo_norm.split()
    ]

    return " ".join(palabras).strip()


def _agregar_opcion_unica(
    opciones: list[str],
    vistos: set[str],
    valor: str,
) -> None:
    """
    Agrega una opción sin duplicados y limita valores excesivamente largos.
    """
    valor = re.sub(r"\s+", " ", str(valor or "")).strip()

    if not valor:
        return

    valor_norm = _normalizar_texto_simple(valor)

    if not valor_norm:
        return

    if valor_norm in VALORES_EJEMPLO_NO_UTILES:
        return

    if valor_norm in vistos:
        return

    if len(valor) > 85:
        valor = valor[:82].rstrip() + "..."

    vistos.add(valor_norm)
    opciones.append(valor)


def _extraer_opciones_catalogo(
    campo_info: dict,
    max_opciones: int = 3,
) -> list[str]:
    """
    Extrae opciones cortas desde los ejemplos reales del catálogo.

    Casos especiales:
    - Para 'tipo de entrada', convierte valores compuestos como:
        RTD: Pt100 | Termocupla: J, K
      en opciones:
        RTD, Termocupla

    No inventa opciones.
    """
    if not isinstance(campo_info, dict):
        return []

    campo = _normalizar_texto_simple(
        campo_info.get("campo") or ""
    )

    ejemplos = campo_info.get("ejemplos") or []

    opciones: list[str] = []
    vistos: set[str] = set()

    for ejemplo in ejemplos:
        if isinstance(ejemplo, dict):
            valor = ejemplo.get("valor")
        else:
            valor = ejemplo

        valor = re.sub(r"\s+", " ", str(valor or "")).strip()

        if not valor:
            continue

        # --------------------------------------------------------
        # Tipo de entrada
        # --------------------------------------------------------
        # En este campo nos interesan las clases de entrada:
        # RTD, Termocupla, etc., no toda la lista interna de sensores.
        if campo == "tipo de entrada":
            fragmentos = re.split(r"\s*\|\s*", valor)

            for fragmento in fragmentos:
                fragmento = fragmento.strip()

                if ":" not in fragmento:
                    continue

                etiqueta, contenido = fragmento.split(":", 1)

                etiqueta = etiqueta.strip()
                contenido_norm = _normalizar_texto_simple(contenido)

                if (
                    not etiqueta
                    or not contenido_norm
                    or contenido_norm in VALORES_EJEMPLO_NO_UTILES
                ):
                    continue

                _agregar_opcion_unica(
                    opciones,
                    vistos,
                    etiqueta,
                )

                if len(opciones) >= max_opciones:
                    return opciones

            continue

        # --------------------------------------------------------
        # Campos generales
        # --------------------------------------------------------
        _agregar_opcion_unica(
            opciones,
            vistos,
            valor,
        )

        if len(opciones) >= max_opciones:
            return opciones

    return opciones


def _formatear_lista_opciones(opciones: list[str]) -> str:
    """
    Convierte una lista en texto natural en español.
    """
    opciones = [
        str(opcion).strip()
        for opcion in opciones or []
        if str(opcion).strip()
    ]

    if not opciones:
        return ""

    if len(opciones) == 1:
        return opciones[0]

    if len(opciones) == 2:
        return f"{opciones[0]} o {opciones[1]}"

    return ", ".join(opciones[:-1]) + f" o {opciones[-1]}"


def _pregunta_base_para_campo(campo: str) -> str:
    """
    Genera una pregunta determinística según una dimensión técnica general.

    Importante:
    - No contiene familias de producto específicas.
    - Trabaja con atributos industriales transversales.
    - Nunca inventa valores.
    """
    campo_norm = _normalizar_texto_simple(campo)
    campo_humano = _humanizar_nombre_campo(campo_norm)

    if campo_norm == "tipo de entrada":
        return "¿Qué tipo de entrada necesita el equipo?"

    if campo_norm == "salida":
        return "¿Qué tipo de salida necesita?"

    if "conexion" in campo_norm and "orificio" in campo_norm:
        return "¿Qué conexión y tamaño de orificio necesita?"

    if "conexion" in campo_norm:
        return "¿Qué tipo y tamaño de conexión necesita?"

    if campo_norm == "rango":
        return "¿Qué rango de operación necesita?"

    if "rango" in campo_norm:
        return f"¿Qué {campo_humano} necesita?"

    if campo_norm == "presion":
        return "¿Qué rango de presión debe manejar?"

    if "temperatura" in campo_norm:
        return "¿Qué rango de temperatura debe manejar?"

    if campo_norm in {"dimensiones", "dimension", "tamano"}:
        return "¿Qué dimensiones o tamaño necesita?"

    if "alimentacion" in campo_norm or "voltaje" in campo_norm:
        return "¿Qué alimentación eléctrica tiene disponible?"

    if (
        "resolucion" in campo_norm
        or "exactitud" in campo_norm
        or "precision" in campo_norm
    ):
        return f"¿Qué {campo_humano} necesita?"

    if "material" in campo_norm or campo_norm == "cuerpo":
        return "¿Qué material de construcción necesita?"

    if "proteccion" in campo_norm:
        return "¿Qué grado de protección necesita?"

    if "protocolo" in campo_norm or "comunicacion" in campo_norm:
        return "¿Qué protocolo o tipo de comunicación necesita?"

    return f"¿Qué valor necesita para {campo_humano}?"


CAMPOS_CON_OPCIONES_BREVES = {
    "tipo de entrada",
    "salida",
    "protocolo",
    "comunicacion",
    "material",
    "cuerpo",
    "tipo",
}


def _campo_admite_opciones_breves(campo: str) -> bool:
    """
    Decide si conviene mostrar opciones al cliente.

    Los campos categóricos pueden mostrar ejemplos reales.

    Los campos continuos, como rango, presión, dimensiones,
    temperatura o alimentación, se preguntan de forma abierta.
    """
    campo_norm = _normalizar_texto_simple(campo)

    if campo_norm in CAMPOS_CON_OPCIONES_BREVES:
        return True

    if "protocolo" in campo_norm:
        return True

    if "comunicacion" in campo_norm:
        return True

    if "material" in campo_norm:
        return True

    return False


def _compactar_opcion_para_pregunta(
    campo: str,
    opcion: str,
) -> str:
    """
    Reduce una opción técnica extensa sin cambiar su significado.

    Ejemplo:
        4-20 mA (2 hilos) + HART
        permanece completa.

        Se eliminan espacios repetidos y textos excesivamente largos.
    """
    campo_norm = _normalizar_texto_simple(campo)

    opcion = re.sub(
        r"\s+",
        " ",
        str(opcion or ""),
    ).strip()

    if not opcion:
        return ""

    # En salidas compuestas evitamos repetir detalles secundarios
    # cuando ya existe un protocolo diferenciador.
    if campo_norm == "salida" and "+" in opcion:
        opcion = re.sub(
            r"\s*\(2\s*hilos?\)\s*(?=\+)",
            " ",
            opcion,
            flags=re.IGNORECASE,
        )

        opcion = re.sub(r"\s+", " ", opcion).strip()

    # Las opciones demasiado largas no son adecuadas para una pregunta.
    if len(opcion) > 60:
        return ""

    return opcion


def _formatear_opciones_breves(
    campo: str,
    opciones: list[str],
) -> str:
    """
    Construye una lista breve y permite que el cliente indique otra opción.

    Ejemplos:
        Termocupla, RTD u otra

        4-20 mA, 4-20 mA + HART o Profibus
    """
    opciones_limpias = []
    vistos = set()

    for opcion in opciones or []:
        opcion_limpia = _compactar_opcion_para_pregunta(
            campo,
            opcion,
        )

        if not opcion_limpia:
            continue

        opcion_norm = _normalizar_texto_simple(
            opcion_limpia
        )

        if not opcion_norm or opcion_norm in vistos:
            continue

        vistos.add(opcion_norm)
        opciones_limpias.append(opcion_limpia)

        if len(opciones_limpias) >= 3:
            break

    if not opciones_limpias:
        return ""

    return ", ".join(opciones_limpias) + " u otra"


def generar_pregunta_campo_dinamico(
    texto_producto: str,
    campo_info: dict,
) -> str:
    """
    Genera UNA pregunta breve basada en un campo real del catálogo.

    Reglas:
    - El backend ya decidió qué campo preguntar.
    - No usa OpenAI para decidir campos ni opciones.
    - Solo muestra opciones en campos categóricos.
    - Los campos continuos se preguntan de forma abierta.
    - No inventa valores.
    """
    if not isinstance(campo_info, dict):
        return "¿Qué especificación técnica adicional necesita?"

    campo = str(
        campo_info.get("campo") or ""
    ).strip()

    if not campo:
        return "¿Qué especificación técnica adicional necesita?"

    pregunta_base = _pregunta_base_para_campo(campo)

    # Rangos, dimensiones, presión, temperatura, alimentación y otros
    # valores continuos deben responderse libremente.
    if not _campo_admite_opciones_breves(campo):
        return pregunta_base.strip()

    opciones_catalogo = _extraer_opciones_catalogo(
        campo_info,
        max_opciones=3,
    )

    opciones_texto = _formatear_opciones_breves(
        campo,
        opciones_catalogo,
    )

    if not opciones_texto:
        return pregunta_base.strip()

    # Eliminamos únicamente el signo final para integrar las opciones
    # dentro de la misma pregunta.
    pregunta_sin_cierre = pregunta_base.rstrip().rstrip("?")

    return (
        f"{pregunta_sin_cierre}: "
        f"{opciones_texto}?"
    )


def generar_preguntas_campos_dinamicos(
    texto_producto: str,
    campos_seleccionados: list[dict],
    max_preguntas: int = 2,
) -> list[str]:
    """
    Convierte los campos seleccionados por catalog.py en preguntas.

    Reglas:
    - máximo dos preguntas técnicas después de confirmar el tipo;
    - una pregunta por campo;
    - no repite preguntas;
    - no inventa campos ni opciones;
    - conserva el agente tradicional como fallback independiente.
    """
    if max_preguntas <= 0:
        return []

    preguntas = []
    preguntas_vistas = set()

    for campo_info in campos_seleccionados or []:
        pregunta = generar_pregunta_campo_dinamico(
            texto_producto=texto_producto,
            campo_info=campo_info,
        )

        pregunta_norm = _normalizar_texto_simple(pregunta)

        if not pregunta_norm:
            continue

        if pregunta_norm in preguntas_vistas:
            continue

        preguntas_vistas.add(pregunta_norm)
        preguntas.append(pregunta)

        if len(preguntas) >= max_preguntas:
            break

    logger.info(
        "Preguntas dinámicas generadas: producto='%s' campos=%s preguntas=%s",
        str(texto_producto or "")[:100],
        [
            campo.get("campo")
            for campo in campos_seleccionados or []
            if isinstance(campo, dict)
        ],
        preguntas,
    )

    return preguntas

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
