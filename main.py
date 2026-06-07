"""
main.py — Endpoint principal NIA v365

Responsabilidades:
- Exponer endpoints FastAPI para chat y archivos.
- Mantener sesión conversacional en MongoDB.
- Capturar datos básicos del cliente.
- Evaluar necesidad técnica.
- Transformar mensaje natural en query limpia de catálogo.
- Buscar catálogo real.
- Validar compatibilidad producto/necesidad.
- Construir respuestas seguras cuando hay datos de catálogo.
- Evitar que el LLM invente códigos, marcas, nombres o descripciones.

Regla de arquitectura:
El LLM conversa, pero el backend decide y construye respuestas críticas
cuando hay productos reales del catálogo.
"""

import hashlib
import hmac
import logging
import logging.handlers
import os
import re
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from pydantic import BaseModel

from memory import get_session, save_session, ensure_index
from openai_client import call_nia, call_llm_json
from nia_prompt import PROMPT_MAESTRO
from catalog import (
    buscar_por_codigo,
    buscar_por_texto,
    evaluar_coincidencia,
)
from product_matcher import validar_compatibilidad_producto
from file_processor import procesar_archivo
from knowledge import contexto_para_agente
from questions_agent import generar_preguntas
from response_engine import (
    respuesta_producto_encontrado,
    respuesta_producto_relacionado,
    respuesta_sin_resultado,
    contiene_placeholder,
)


# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────

def setup_logging():
    """
    Configura logging para consola y archivo rotativo.

    Evita duplicar handlers cuando Uvicorn recarga con --reload.
    """
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

    root = logging.getLogger("nia")
    root.setLevel(level)

    if root.handlers:
        return

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)

    fh = logging.handlers.RotatingFileHandler(
        "nia.log",
        maxBytes=10_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)

    root.addHandler(ch)
    root.addHandler(fh)


setup_logging()
logger = logging.getLogger("nia.main")


# ─────────────────────────────────────────────────────────────
# FastAPI / Rate limiting
# ─────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="NIA — Asistente Comercial ViaIndustrial")
app.state.limiter = limiter

app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await ensure_index()
    logger.info("NIA arrancó correctamente")


# ─────────────────────────────────────────────────────────────
# Modelos API
# ─────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: str
    mensaje: str
    phone_id: Optional[str] = None


class ChatResponse(BaseModel):
    respuesta: str
    etapa: Optional[str] = None
    items_resultado: Optional[list] = None
    cliente: Optional[dict] = None


# ─────────────────────────────────────────────────────────────
# WhatsApp webhook auth
# ─────────────────────────────────────────────────────────────

WA_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WA_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")


def verificar_firma_whatsapp(request: Request, body: bytes) -> bool:
    """
    Valida la firma HMAC-SHA256 que Meta envía en X-Hub-Signature-256.

    En desarrollo, si no hay WA_APP_SECRET configurado, permite pasar.
    """
    if not WA_APP_SECRET:
        return True

    sig_header = request.headers.get("X-Hub-Signature-256", "")
    if not sig_header.startswith("sha256="):
        return False

    firma_esperada = hmac.new(
        WA_APP_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(sig_header[7:], firma_esperada)


@app.get("/webhook/whatsapp")
async def whatsapp_verify(
    hub_mode: str = None,
    hub_challenge: str = None,
    hub_verify_token: str = None,
):
    """
    Verificación del webhook de WhatsApp Business.
    """
    if hub_mode == "subscribe" and hub_verify_token == WA_VERIFY_TOKEN:
        logger.info("Webhook WhatsApp verificado correctamente")
        return int(hub_challenge)

    raise HTTPException(status_code=403, detail="Token inválido")


# ─────────────────────────────────────────────────────────────
# Regex / intención básica
# ─────────────────────────────────────────────────────────────

CODIGO_RE = re.compile(r"\b(\d{6})\b")
REF_RE = re.compile(r"\b(P\d{3,}|[A-Z]{1,4}\d{3,}[A-Z0-9]*)\b", re.IGNORECASE)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
NIT_RE = re.compile(r"\b(\d{8,10}-?\d?)\b")

PALABRAS_SALUDO = {"hola", "buenas", "buenos", "buen", "hi", "hello", "hey", "saludos"}
PALABRAS_MAS = {"también", "otro", "otra", "más", "adicional", "y además", "necesito más", "y también"}
PALABRAS_FIN = {"solo eso", "con eso", "es todo", "nada más", "eso es todo", "listo", "ok cotiza", "cotiza"}


def detectar_identificador(texto: str):
    """
    Detecta código exacto de 6 dígitos o referencia tipo Pxxxxx / alfanumérica.
    """
    m = CODIGO_RE.search(texto)
    if m:
        return "codigo", m.group(1)

    m = REF_RE.search(texto)
    if m:
        return "referencia", m.group(1).upper()

    return None, None


def es_solo_saludo(texto: str) -> bool:
    """
    Determina si el mensaje solo es saludo.
    """
    t = texto.lower().strip().rstrip(".,!")
    return t in PALABRAS_SALUDO or (
        len(t.split()) <= 2 and any(saludo in t for saludo in PALABRAS_SALUDO)
    )


# ─────────────────────────────────────────────────────────────
# Captura silenciosa de cliente
# ─────────────────────────────────────────────────────────────

def extraer_datos_cliente(mensaje: str, cliente_actual: dict) -> dict:
    """
    Extrae datos básicos del cliente sin interrumpir el flujo.

    Reglas:
    - Solo completa campos vacíos.
    - No sobreescribe datos ya capturados.
    - Captura email, NIT, nombre y empresa.
    - Limpia empresa para no arrastrar correo, NIT o frases posteriores.
    """
    cliente = dict(cliente_actual) if cliente_actual else {}

    if not cliente.get("email"):
        m = EMAIL_RE.search(mensaje)
        if m:
            cliente["email"] = m.group(0).strip()
            logger.debug("Email capturado: %s", cliente["email"])

    if not cliente.get("nit"):
        m = NIT_RE.search(mensaje)
        if m:
            cliente["nit"] = m.group(1).strip()
            logger.debug("NIT capturado: %s", cliente["nit"])

    if not cliente.get("nombre"):
        patrones_nombre = [
            r"(?:soy|me llamo|mi nombre es)\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){0,3})",
            r"(?:habla|llama|escribe)\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){0,2})",
        ]

        for patron in patrones_nombre:
            m = re.search(patron, mensaje, re.IGNORECASE)
            if m:
                cliente["nombre"] = m.group(1).strip()
                logger.debug("Nombre capturado: %s", cliente["nombre"])
                break

    if not cliente.get("empresa"):
        patrones_empresa = [
            r"(?:de la empresa|trabajo en|empresa|compañía)\s+(.+)",
            r"(?:somos|representamos a)\s+(.+)",
        ]

        cortes = [
            r"\s+y\s+mi\s+correo\s+es\s+",
            r"\s+y\s+mi\s+email\s+es\s+",
            r"\s+y\s+el\s+correo\s+es\s+",
            r"\s+mi\s+correo\s+es\s+",
            r"\s+mi\s+email\s+es\s+",
            r"\s+correo\s+",
            r"\s+email\s+",
            r"\s+y\s+mi\s+nit\s+es\s+",
            r"\s+mi\s+nit\s+es\s+",
            r"\s+nit\s+",
            r"\s+soy\s+",
            r"\s+me\s+llamo\s+",
            r"\s+mi\s+nombre\s+es\s+",
        ]

        for patron in patrones_empresa:
            m = re.search(patron, mensaje, re.IGNORECASE)
            if not m:
                continue

            empresa = m.group(1).strip()

            for corte in cortes:
                empresa = re.split(corte, empresa, flags=re.IGNORECASE)[0].strip()

            empresa = empresa.strip(" ,.;:-")

            if len(empresa) > 2:
                cliente["empresa"] = empresa
                logger.debug("Empresa capturada: %s", cliente["empresa"])
                break

    return cliente


# ─────────────────────────────────────────────────────────────
# Datos faltantes / saludo
# ─────────────────────────────────────────────────────────────

def datos_faltantes(cliente: dict, etapa: str) -> list:
    faltantes = []

    if etapa == "cotizacion":
        if not cliente.get("nombre"):
            faltantes.append("¿A nombre de quién va la cotización?")
        if not cliente.get("email"):
            faltantes.append("¿A qué correo te envío la cotización?")

    elif etapa == "proforma":
        if not cliente.get("empresa"):
            faltantes.append("¿Cuál es la razón social de tu empresa?")
        if not cliente.get("nit"):
            faltantes.append("¿Cuál es el NIT?")

    return faltantes


def saludo_personalizado(cliente: dict) -> str:
    if cliente.get("nombre"):
        return f"[CLIENTE CONOCIDO: {cliente['nombre']}]"
    return "[CLIENTE NUEVO — capturar nombre si lo menciona]"


# ─────────────────────────────────────────────────────────────
# Evaluación de necesidad
# ─────────────────────────────────────────────────────────────

def _cuenta_parametros_tecnicos_generales(texto: str) -> int:
    """
    Cuenta señales técnicas generales sin depender de una familia específica.

    No es una lista de productos ni de exclusiones.
    Son patrones de magnitudes industriales comunes.
    """
    t = texto.lower()

    patrones = [
        r"\b\d+(\.\d+)?\s*(bar|psi|pa|kpa|mpa)\b",
        r"\b\d+(\.\d+)?\s*(l/min|lpm|gpm|m3/h|m³/h)\b",
        r"\b\d+(\.\d+)?\s*(°c|c|grados)\b",
        r"\b\d+(\.\d+)?\s*(v|vac|vdc|ma|a|hz|kw|hp)\b",
        r"\b\d+(\.\d+)?\s*(mm|cm|m|pulg|pulgadas|in)\b",
        r"\b(agua|aire|vapor|aceite|gas|fluido|quimico|químico)\b",
        r"\b(limpia|residual|corrosivo|industrial|sanitario|explosivo)\b",
    ]

    return sum(1 for patron in patrones if re.search(patron, t, re.IGNORECASE))


def _parece_solicitud_de_producto(texto: str) -> bool:
    """
    Detecta si el usuario está solicitando un producto o familia de producto.

    Es una detección general de intención comercial, no una regla por producto.
    """
    t = texto.lower().strip()

    patrones = [
        r"\bnecesito\b",
        r"\brequiero\b",
        r"\bbusco\b",
        r"\bcotizar\b",
        r"\bquiero\b",
        r"\bme sirve\b",
        r"\bproducto\b",
        r"\bequipo\b",
        r"\breferencia\b",
        r"\bcódigo\b",
        r"\bcodigo\b",
    ]

    return any(re.search(patron, t) for patron in patrones) and len(t.split()) >= 3


async def evaluar_necesidad(texto: str) -> dict:
    """
    Evalúa si hay suficiente información para iniciar búsqueda en catálogo.

    Principio NIA v365:
    - Si el cliente menciona una necesidad comercial clara, se permite búsqueda preliminar.
    - Buscar NO significa recomendar.
    - La recomendación final la controla product_matcher.py.
    - Si no hay match confiable, NIA pregunta más datos.
    """
    ctx = contexto_para_agente(texto)
    dominio = ctx.get("dominio", "general")

    parametros_generales = _cuenta_parametros_tecnicos_generales(texto)
    parece_solicitud = _parece_solicitud_de_producto(texto)

    if parece_solicitud:
        return {
            "clara": True,
            "preguntas": [],
            "dominio": dominio,
            "razon": "Solicitud comercial suficiente para búsqueda preliminar en catálogo.",
        }

    if parametros_generales >= 2:
        return {
            "clara": True,
            "preguntas": [],
            "dominio": dominio,
            "razon": "Solicitud con suficientes parámetros técnicos para búsqueda preliminar.",
        }

    prompt_evaluacion = (
        f"El cliente de una empresa de instrumentación industrial dice: \"{texto}\"\n\n"
        f"Dominio detectado: {dominio}\n\n"
        "¿Hay suficiente información para iniciar una búsqueda preliminar en catálogo?\n"
        "No significa recomendar todavía; solo buscar candidatos reales.\n"
        "Responde SOLO JSON sin markdown:\n"
        "{\"clara\": true/false, \"razon\": \"una frase corta\"}"
    )

    try:
        resultado = await call_llm_json(prompt_evaluacion)
        clara = bool(resultado.get("clara", False))

        logger.debug(
            "LLM evalúa necesidad: clara=%s razón=%s",
            clara,
            resultado.get("razon", ""),
        )

        if clara:
            return {
                "clara": True,
                "preguntas": [],
                "dominio": dominio,
                "razon": resultado.get("razon", ""),
            }

    except Exception as exc:
        logger.warning("LLM evaluación fallida, usando preguntas: %s", exc)

    preguntas = await generar_preguntas(texto)

    return {
        "clara": False,
        "preguntas": preguntas,
        "dominio": dominio,
    }


# ─────────────────────────────────────────────────────────────
# Búsqueda catálogo / compatibilidad
# ─────────────────────────────────────────────────────────────

async def generar_queries_catalogo(texto: str) -> list[str]:
    """
    Convierte el mensaje natural del cliente en una o varias consultas útiles
    para el catálogo.

    Responsabilidad:
    - Separar conversación humana de búsqueda técnica.
    - No modificar catalog.py para entender frases conversacionales.
    - No usar listas rígidas de palabras prohibidas.
    - Pedir al LLM una frase compacta de búsqueda, pero con fallback seguro.
    """
    texto = (texto or "").strip()

    if not texto:
        return []

    queries = []

    prompt = f"""
Eres un normalizador de búsqueda para un catálogo industrial.

Convierte el mensaje del cliente en una consulta corta para buscar productos reales en catálogo.

REGLAS:
1. No inventes productos.
2. No agregues marcas, referencias ni datos que el cliente no dijo.
3. Conserva el tipo de producto solicitado.
4. Conserva atributos técnicos relevantes: presión, caudal, señal, voltaje, material, fluido, referencia, marca si existen.
5. Quita intención conversacional como "necesito", "busco", "quiero cotizar", pero sin perder el producto.
6. Devuelve SOLO JSON válido.

Mensaje del cliente:
{texto}

Formato:
{{
  "queries": ["consulta principal", "consulta alternativa opcional"]
}}
"""

    try:
        data = await call_llm_json(prompt)
        raw_queries = data.get("queries", [])

        if isinstance(raw_queries, list):
            for query in raw_queries:
                q = str(query).strip()
                if q and q not in queries:
                    queries.append(q)

    except Exception as exc:
        logger.warning("No se pudo generar query limpia con LLM: %s", exc)

    if texto not in queries:
        queries.append(texto)

    return queries[:3]


async def buscar_en_catalogo(texto: str) -> dict:
    """
    Busca candidatos reales y valida compatibilidad producto/necesidad.

    Flujo correcto:
    1. Recibe mensaje natural del cliente.
    2. Genera queries limpias para catálogo.
    3. Busca candidatos reales en MongoDB.
    4. Valida compatibilidad contra la necesidad original.
    5. Retorna encontrado / relacionado / sin_resultado.
    """
    logger.info("Búsqueda catálogo solicitada: '%s'", texto[:100])

    queries_catalogo = await generar_queries_catalogo(texto)

    if not queries_catalogo:
        return {
            "estado": "sin_resultado",
            "razon": "No se pudo construir una consulta válida para catálogo.",
            "pregunta_sugerida": "¿Puedes indicar el tipo de producto o referencia que necesitas?",
            "candidatos_encontrados": False,
        }

    resultados = None
    query_usada = None

    for query in queries_catalogo:
        logger.info("Intentando búsqueda catálogo con query limpia: '%s'", query)

        resultados = await buscar_por_texto(query)

        if resultados:
            query_usada = query
            logger.info(
                "Catálogo devolvió %s candidatos usando query='%s'",
                len(resultados),
                query,
            )
            break

    if not resultados:
        return {
            "estado": "sin_resultado",
            "razon": "No se encontraron candidatos reales en catálogo.",
            "pregunta_sugerida": "¿Puedes darme una referencia, marca, aplicación exacta o especificación adicional?",
            "candidatos_encontrados": False,
        }

    ok_textual, prod_textual = evaluar_coincidencia(
        resultados=resultados,
        query=query_usada or texto,
        campos=2,
    )

    if ok_textual and prod_textual:
        logger.info(
            "Mejor candidato textual: %s score=%s query='%s'",
            prod_textual.get("codigo"),
            prod_textual.get("_score"),
            query_usada,
        )

    decision = await validar_compatibilidad_producto(
        necesidad_cliente=texto,
        candidatos=resultados,
        contexto_tecnico={
            "query_catalogo": query_usada,
            "queries_intentadas": queries_catalogo,
        },
    )

    estado_match = decision.get("estado")
    producto = decision.get("producto")

    if estado_match == "exact_match" and producto:
        producto["_compatibilidad"] = {
            "estado": "exact_match",
            "confianza": decision.get("confianza"),
            "razon": decision.get("razon"),
            "query_catalogo": query_usada,
        }

        return {
            "estado": "encontrado",
            "producto": producto,
            "tipo": "compatibilidad_exacta",
            "exacto": True,
            "razon": decision.get("razon"),
            "query_catalogo": query_usada,
            "candidatos_encontrados": True,
        }

    if estado_match == "related_match" and producto:
        producto["_compatibilidad"] = {
            "estado": "related_match",
            "confianza": decision.get("confianza"),
            "razon": decision.get("razon"),
            "query_catalogo": query_usada,
        }

        return {
            "estado": "relacionado",
            "producto": producto,
            "tipo": "producto_relacionado",
            "exacto": False,
            "razon": decision.get("razon"),
            "pregunta_sugerida": decision.get("pregunta_sugerida"),
            "query_catalogo": query_usada,
            "candidatos_encontrados": True,
        }

    return {
        "estado": "sin_resultado",
        "razon": decision.get("razon"),
        "pregunta_sugerida": decision.get("pregunta_sugerida"),
        "query_catalogo": query_usada,
        "candidatos_encontrados": True,
    }


async def rama_codigo(valor: str, tipo: str) -> dict:
    """
    Rama de búsqueda exacta por código o referencia.
    """
    logger.info("Búsqueda exacta: %s=%s", tipo, valor)

    prod = await buscar_por_codigo(valor)

    if prod:
        return {
            "estado": "encontrado",
            "producto": prod,
            "tipo": tipo,
            "exacto": True,
            "candidatos_encontrados": True,
        }

    logger.info("Fallback catálogo para identificador: %s", valor)

    res = await buscar_en_catalogo(valor)

    if res["estado"] in {"encontrado", "relacionado"}:
        res["tipo"] = "fallback_identificador"
        return res

    logger.info("Sin resultado para identificador: %s", valor)

    return {
        "estado": "sin_resultado",
        "tipo": tipo,
        "pregunta_sugerida": "¿Puedes verificar el código o compartir marca/referencia adicional?",
        "candidatos_encontrados": res.get("candidatos_encontrados", False),
    }


def debe_intentar_enriquecimiento(res: dict) -> bool:
    """
    Decide si vale la pena intentar Libros Rol 2.

    Regla:
    - Si no hubo candidatos en catálogo, sí tiene sentido enriquecer la búsqueda.
    - Si sí hubo candidatos, pero product_matcher dijo que ninguno es compatible,
      NO se debe enriquecer a ciegas. Se debe responder seguro y pedir precisión.
    """
    if res.get("estado") != "sin_resultado":
        return False

    return res.get("candidatos_encontrados") is False


async def enriquecer_y_buscar(texto: str) -> dict:
    """
    Usa contexto de conocimiento para enriquecer la búsqueda,
    pero mantiene la validación de compatibilidad.
    """
    ctx = contexto_para_agente(texto)
    terminos = ctx.get("terminos", [])
    dominio = ctx.get("dominio", "")
    query = f"{texto} {' '.join(terminos[:4])}".strip()

    logger.info("Libros Rol 2 — query enriquecida: '%s'", query[:80])

    res = await buscar_en_catalogo(query)

    if res["estado"] in {"encontrado", "relacionado"}:
        res["tipo"] = "libros_rol2"
        res["dominio"] = dominio
        res["query_enriquecida"] = query
        return res

    if res.get("candidatos_encontrados") is True:
        res["dominio"] = dominio
        res["query_enriquecida"] = query
        return res

    preguntas = await generar_preguntas(texto)

    return {
        "estado": "pendiente",
        "preguntas": preguntas,
        "dominio": dominio,
    }


# ─────────────────────────────────────────────────────────────
# Response helpers
# ─────────────────────────────────────────────────────────────

def _marcar_respuesta_segura(texto: str) -> str:
    """
    Marca una respuesta para que no sea reescrita por el LLM.
    """
    return "[RESPUESTA_SEGURA]\n" + texto


def construir_respuesta_desde_resultado(
    res: dict,
    cliente: dict,
    productos_acumulados: list,
    desde: str,
    necesidad_ctx_base: Optional[dict] = None,
) -> tuple[str, str, dict]:
    """
    Convierte un resultado de catálogo en:
    - contexto_extra
    - nueva_etapa
    - necesidad_ctx actualizado

    Reglas:
    - encontrado: se agrega al carrito.
    - relacionado: no se agrega al carrito; se pide confirmación.
    - sin_resultado/pendiente: se mantiene descubrimiento.
    """
    necesidad_ctx_base = necesidad_ctx_base or {}

    estado = res.get("estado")

    if estado == "encontrado" and res.get("producto"):
        producto = res["producto"]

        productos_acumulados.append({
            "producto": producto,
            "cantidad": None,
            "desde": desde,
            "ts": datetime.utcnow().isoformat(),
        })

        return (
            _marcar_respuesta_segura(respuesta_producto_encontrado(producto, cliente)),
            "producto_encontrado",
            {},
        )

    if estado == "relacionado" and res.get("producto"):
        producto = res["producto"]

        necesidad_ctx = {
            **necesidad_ctx_base,
            "producto_relacionado": producto,
            "pregunta_sugerida": res.get("pregunta_sugerida"),
            "razon": res.get("razon"),
        }

        return (
            _marcar_respuesta_segura(
                respuesta_producto_relacionado(
                    producto=producto,
                    razon=res.get("razon"),
                    pregunta_sugerida=res.get("pregunta_sugerida"),
                    cliente=cliente,
                )
            ),
            "validando_relacionado",
            necesidad_ctx,
        )

    if estado == "pendiente":
        preguntas = res.get("preguntas", [])
        texto_preguntas = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(preguntas))

        return (
            f"[PENDIENTE — NECESITA MÁS INFORMACIÓN]\n{texto_preguntas}",
            "descubrimiento",
            necesidad_ctx_base,
        )

    return (
        _marcar_respuesta_segura(
            respuesta_sin_resultado(
                pregunta_sugerida=res.get("pregunta_sugerida"),
                cliente=cliente,
            )
        ),
        "descubrimiento",
        necesidad_ctx_base,
    )


def _extraer_respuesta_segura(contexto_extra: str) -> Optional[str]:
    """
    Extrae respuesta segura marcada.
    """
    if contexto_extra.startswith("[RESPUESTA_SEGURA]"):
        return contexto_extra.replace("[RESPUESTA_SEGURA]\n", "", 1).strip()

    return None

# ─────────────────────────────────────────────────────────────
# Estado comercial prioritario
# ─────────────────────────────────────────────────────────────

def _normalizar_intencion(texto: str) -> str:
    """
    Normaliza texto corto para interpretar confirmaciones,
    cierres y respuestas comerciales simples.

    No se usa para catálogo. Solo para control de flujo.
    """
    t = (texto or "").lower().strip()
    reemplazos = {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ñ": "n",
    }

    for origen, destino in reemplazos.items():
        t = t.replace(origen, destino)

    t = re.sub(r"\s+", " ", t)
    return t.strip(" .,!¡¿?")


def _es_confirmacion_afirmativa(texto: str) -> bool:
    """
    Detecta respuestas afirmativas del cliente.

    Aplica para confirmar producto sugerido, no para buscar catálogo.
    """
    t = _normalizar_intencion(texto)

    afirmaciones_exactas = {
        "si",
        "correcto",
        "ese",
        "esa",
        "ese me sirve",
        "esa me sirve",
        "me sirve",
        "sirve",
        "ok",
        "dale",
        "perfecto",
        "confirmo",
        "confirmado",
    }

    return t in afirmaciones_exactas


def _es_confirmacion_negativa(texto: str) -> bool:
    """
    Detecta rechazo del producto sugerido.
    """
    t = _normalizar_intencion(texto)

    negativas_exactas = {
        "no",
        "no me sirve",
        "no es",
        "no corresponde",
        "otro",
        "otra",
        "diferente",
    }

    return t in negativas_exactas


def _extraer_cantidad_solicitada(texto: str) -> Optional[int]:
    """
    Extrae una cantidad comercial cuando NIA está esperando cantidad.

    Regla:
    - Acepta cantidades razonables de 1 a 5 dígitos.
    - No interpreta números largos como cantidad para evitar confundir NIT,
      teléfonos o códigos.
    """
    if not texto:
        return None

    t = texto.lower().strip()

    m = re.search(
        r"\b(\d{1,5})\b\s*(und|unds|unidad|unidades|pieza|piezas|u)?\b",
        t,
        re.IGNORECASE,
    )

    if not m:
        return None

    try:
        cantidad = int(m.group(1))
    except ValueError:
        return None

    if cantidad <= 0:
        return None

    return cantidad


def _asignar_cantidad_ultimo_producto(productos_acumulados: list, cantidad: int) -> None:
    """
    Asigna la cantidad al último producto acumulado que aún no tenga cantidad.
    Si todos tienen cantidad, actualiza el último producto como decisión comercial.
    """
    if not productos_acumulados:
        return

    for item in reversed(productos_acumulados):
        if not item.get("cantidad"):
            item["cantidad"] = cantidad
            return

    productos_acumulados[-1]["cantidad"] = cantidad


def _parece_nombre_simple(texto: str) -> Optional[str]:
    """
    Captura nombres escritos de forma directa, por ejemplo:
    - Luis
    - Luis Díaz
    - Juan Carlos Pérez

    No captura correos, números, NIT, frases de cierre ni solicitudes de producto.
    """
    if not texto:
        return None

    limpio = texto.strip(" ,.;:-")

    if not limpio or EMAIL_RE.search(limpio) or NIT_RE.search(limpio):
        return None

    t = _normalizar_intencion(limpio)

    bloqueados = PALABRAS_SALUDO | PALABRAS_FIN | {
        "si",
        "no",
        "ok",
        "dale",
        "correcto",
        "solo",
        "eso",
        "solo eso",
        "cotiza",
    }

    if t in bloqueados:
        return None

    if _parece_solicitud_de_producto(limpio):
        return None

    partes = limpio.split()

    if not (1 <= len(partes) <= 4):
        return None

    patron_nombre = re.compile(r"^[A-Za-zÁÉÍÓÚáéíóúÑñüÜ'-]{2,}$")

    if not all(patron_nombre.match(p) for p in partes):
        return None

    return " ".join(p.capitalize() for p in partes)


def _parece_empresa_simple(texto: str) -> Optional[str]:
    """
    Captura razón social escrita de forma directa cuando NIA está en etapa proforma.

    Ejemplos:
    - ViaIndustrial SAS
    - Equipos Industriales Fenix S.A.S
    - Industrias ABC
    """
    if not texto:
        return None

    limpio = texto.strip(" ,.;:-")

    if not limpio or EMAIL_RE.search(limpio) or NIT_RE.search(limpio):
        return None

    if _es_confirmacion_afirmativa(limpio) or _es_confirmacion_negativa(limpio):
        return None

    if _parece_solicitud_de_producto(limpio):
        return None

    if len(limpio) < 3 or len(limpio) > 80:
        return None

    if not re.search(r"[A-Za-zÁÉÍÓÚáéíóúÑñ]", limpio):
        return None

    return limpio


def _capturar_dato_comercial_por_etapa(mensaje: str, cliente: dict, etapa: str) -> dict:
    """
    Completa datos comerciales según el estado actual de la conversación.

    Esta función evita que datos como nombre, empresa o NIT sean tratados
    como búsqueda de producto cuando NIA está cerrando una cotización.
    """
    cliente = dict(cliente or {})

    # extraer_datos_cliente ya capturó email/NIT si venían explícitos.
    # Aquí completamos casos escritos de forma directa.
    if etapa in {"cotizacion", "calificacion", "confirmando_cierre"}:
        if not cliente.get("nombre"):
            nombre = _parece_nombre_simple(mensaje)
            if nombre:
                cliente["nombre"] = nombre
                logger.debug("Nombre simple capturado por etapa: %s", nombre)

    if etapa == "proforma":
        if not cliente.get("empresa"):
            empresa = _parece_empresa_simple(mensaje)
            if empresa:
                cliente["empresa"] = empresa
                logger.debug("Empresa simple capturada por etapa: %s", empresa)

    return cliente


def _respuesta_siguiente_dato_comercial(cliente: dict) -> tuple[str, str]:
    """
    Decide cuál es el siguiente dato comercial faltante.

    Orden profesional:
    1. Nombre
    2. Email
    3. Razón social
    4. NIT
    5. Cierre listo para revisión de asesor
    """
    cliente = cliente or {}
    nombre = cliente.get("nombre")

    if not cliente.get("nombre"):
        return "¿A nombre de quién va la cotización?", "cotizacion"

    if not cliente.get("email"):
        return f"Gracias, {nombre}. ¿Cuál es el correo electrónico para enviar la cotización?", "cotizacion"

    if not cliente.get("empresa"):
        return (
            f"Perfecto, {nombre}. Para preparar la proforma, ¿cuál es la razón social de tu empresa?",
            "proforma",
        )

    if not cliente.get("nit"):
        return "Gracias. ¿Cuál es el NIT de la empresa?", "proforma"

    return (
    "Perfecto, ya tengo el producto, la cantidad y los datos básicos.\n\n"
    "Voy a dejar la solicitud lista para que un asesor revise disponibilidad, precio y condiciones antes de continuar con la cotización.",
    "cotizacion_lista",
    )


def _es_nueva_solicitud_durante_cierre(mensaje: str) -> bool:
    """
    Permite salir del flujo de cierre si el cliente realmente pide otro producto.

    Ejemplo:
    - también necesito una válvula
    - agrega otro sensor
    - necesito otro equipo
    """
    t = _normalizar_intencion(mensaje)

    if any(p in t for p in {"tambien necesito", "tambien quiero", "agrega", "agregar", "otro producto", "otra referencia"}):
        return True

    return _parece_solicitud_de_producto(mensaje)


# ─────────────────────────────────────────────────────────────
# Núcleo conversacional
# ─────────────────────────────────────────────────────────────

async def procesar_turno(
    session_id: str,
    mensaje: str,
    phone_id: Optional[str] = None,
    archivo_bytes: Optional[bytes] = None,
    archivo_nombre: Optional[str] = None,
) -> dict:
    logger.info(
        "Turno: session=%s etapa_msg='%s'",
        session_id,
        mensaje[:50] if mensaje else "[archivo]",
    )

    session = await get_session(session_id) or {}

    historial = session.get("turnos", [])
    etapa = session.get("etapa", "inicio")
    archivo_activo = session.get("archivo_activo")
    necesidad_ctx = session.get("necesidad_ctx", {})
    cliente = session.get("cliente", {})
    productos_acumulados = session.get("productos_acumulados", [])

    contexto_extra = ""
    nueva_etapa = etapa
    items_resultado = []

    if mensaje.strip():
        cliente = extraer_datos_cliente(mensaje, cliente)

    # ══════════════════════════════════════════════════════
    # MODO ARCHIVO
    # ══════════════════════════════════════════════════════

    if archivo_bytes and archivo_nombre:
        logger.info("Procesando archivo: %s", archivo_nombre)

        items = await procesar_archivo(archivo_bytes, archivo_nombre)

        for item in items:
            tipo, valor = detectar_identificador(item["texto"])

            if tipo:
                res = await rama_codigo(valor, tipo)
            else:
                nec = await evaluar_necesidad(item["texto"])

                if nec["clara"]:
                    res = await buscar_en_catalogo(item["texto"])

                    if debe_intentar_enriquecimiento(res):
                        res = await enriquecer_y_buscar(item["texto"])

                else:
                    res = {
                        "estado": "pendiente",
                        "preguntas": nec["preguntas"],
                    }

            res["texto_original"] = item["texto"]
            res["fila"] = item.get("fila")
            res["cantidad"] = item.get("cantidad")
            items_resultado.append(res)

            if res["estado"] == "encontrado" and res.get("producto"):
                productos_acumulados.append({
                    "producto": res["producto"],
                    "cantidad": item.get("cantidad"),
                    "desde": "archivo",
                    "ts": datetime.utcnow().isoformat(),
                })

        encontrados = [i for i in items_resultado if i["estado"] == "encontrado"]
        pendientes = [
            i for i in items_resultado
            if i["estado"] in {"pendiente", "sin_resultado", "relacionado"}
        ]

        archivo_activo = {
            "nombre": archivo_nombre,
            "total_items": len(items),
            "items": items_resultado,
            "ts": datetime.utcnow().isoformat(),
        }

        nueva_etapa = "procesando_archivo"

        resumen = (
            f"[ARCHIVO: {archivo_nombre}]\n"
            f"Total: {len(items)} · Encontrados: {len(encontrados)} · "
            f"Pendientes/por validar: {len(pendientes)}\n"
        )

        for item_resultado in items_resultado:
            resumen += f"- {item_resultado['texto_original']}: {item_resultado['estado'].upper()}"

            if item_resultado.get("producto"):
                p = item_resultado["producto"]
                resumen += f" → {p.get('codigo')} | {p.get('nombre')}"

                if item_resultado["estado"] == "relacionado":
                    resumen += " [RELACIONADO — REQUIERE CONFIRMACIÓN]"
                elif not item_resultado.get("exacto", True):
                    resumen += " [COINCIDENCIA CERCANA]"

            resumen += "\n"

        contexto_extra = resumen

    # ══════════════════════════════════════════════════════
    # MODO TEXTO
    # ══════════════════════════════════════════════════════

    elif mensaje.strip():
        msg_lower = mensaje.lower().strip()
        estado_comercial_resuelto = False

        # ══════════════════════════════════════════════════════
        # PRIORIDAD: ESTADO COMERCIAL
        # ══════════════════════════════════════════════════════
        # Antes de buscar catálogo, NIA debe respetar lo que estaba esperando:
        # confirmación de producto, cantidad o datos para cotización/proforma.

        if etapa == "producto_encontrado":
            if _es_confirmacion_afirmativa(mensaje):
                contexto_extra = _marcar_respuesta_segura(
                    "Perfecto. ¿Cuál es la cantidad que necesitas?"
                )
                nueva_etapa = "esperando_cantidad"
                necesidad_ctx = {"esperando": "cantidad"}
                estado_comercial_resuelto = True

            elif _es_confirmacion_negativa(mensaje):
                contexto_extra = _marcar_respuesta_segura(
                    "Entendido. Para buscar una mejor opción, ¿puedes indicarme tipo de producto, aplicación, marca, referencia o especificación técnica requerida?"
                )
                nueva_etapa = "descubrimiento"
                necesidad_ctx = {}
                estado_comercial_resuelto = True

        elif etapa == "esperando_cantidad":
            cantidad = _extraer_cantidad_solicitada(mensaje)

            if cantidad:
                _asignar_cantidad_ultimo_producto(productos_acumulados, cantidad)

                contexto_extra = _marcar_respuesta_segura(
                    f"Listo, dejé la cantidad en {cantidad}. ¿Necesitas algo más o cotizamos con esto?"
                )
                nueva_etapa = "confirmando_cierre"
                necesidad_ctx = {}
                estado_comercial_resuelto = True

            else:
                contexto_extra = _marcar_respuesta_segura(
                    "Para avanzar con la cotización necesito la cantidad en unidades. ¿Cuántas unidades necesitas?"
                )
                nueva_etapa = "esperando_cantidad"
                necesidad_ctx = {"esperando": "cantidad"}
                estado_comercial_resuelto = True

        elif (
            etapa in {"confirmando_cierre", "cotizacion", "calificacion", "proforma"}
            and not _es_nueva_solicitud_durante_cierre(mensaje)
        ):
            cliente = _capturar_dato_comercial_por_etapa(
                mensaje=mensaje,
                cliente=cliente,
                etapa=etapa,
            )

            respuesta_dato, etapa_dato = _respuesta_siguiente_dato_comercial(cliente)

            contexto_extra = _marcar_respuesta_segura(respuesta_dato)
            nueva_etapa = etapa_dato
            necesidad_ctx = {}
            estado_comercial_resuelto = True

        # Caso 1: respuesta a ítem pendiente de archivo
        if not estado_comercial_resuelto and archivo_activo:
            pendientes = [
                item for item in archivo_activo.get("items", [])
                if item["estado"] in {"pendiente", "sin_resultado", "relacionado"}
            ]

            if pendientes:
                item_pend = pendientes[0]
                query_e = f"{item_pend['texto_original']} {mensaje}".strip()

                res = await buscar_en_catalogo(query_e)

                if debe_intentar_enriquecimiento(res):
                    res = await enriquecer_y_buscar(query_e)

                res["texto_original"] = item_pend["texto_original"]

                for idx, item in enumerate(archivo_activo["items"]):
                    if item["texto_original"] == item_pend["texto_original"]:
                        archivo_activo["items"][idx] = res
                        break

                contexto_extra, nueva_etapa, necesidad_ctx = construir_respuesta_desde_resultado(
                    res=res,
                    cliente=cliente,
                    productos_acumulados=productos_acumulados,
                    desde="archivo_pendiente",
                    necesidad_ctx_base={
                        "texto_original": item_pend["texto_original"],
                        "query_evaluada": query_e,
                    },
                )

        # Caso 2: validación de producto relacionado
        elif etapa == "validando_relacionado" and necesidad_ctx.get("producto_relacionado"):
            producto_relacionado = necesidad_ctx["producto_relacionado"]

            if any(palabra in msg_lower for palabra in {"sí", "si", "correcto", "ese", "me sirve", "sirve"}):
                productos_acumulados.append({
                    "producto": producto_relacionado,
                    "cantidad": None,
                    "desde": "confirmacion_relacionado",
                    "ts": datetime.utcnow().isoformat(),
                })

                contexto_extra = _marcar_respuesta_segura(
                    respuesta_producto_encontrado(producto_relacionado, cliente)
                )
                nueva_etapa = "producto_encontrado"
                necesidad_ctx = {}

            else:
                texto_original = (
                    necesidad_ctx.get("texto_original")
                    or necesidad_ctx.get("query_evaluada")
                    or mensaje
                )
                query_e = f"{texto_original} {mensaje}".strip()

                res = await buscar_en_catalogo(query_e)

                if debe_intentar_enriquecimiento(res):
                    res = await enriquecer_y_buscar(query_e)

                contexto_extra, nueva_etapa, necesidad_ctx = construir_respuesta_desde_resultado(
                    res=res,
                    cliente=cliente,
                    productos_acumulados=productos_acumulados,
                    desde="validacion_relacionado",
                    necesidad_ctx_base={
                        "texto_original": texto_original,
                        "query_evaluada": query_e,
                    },
                )

        # Caso 3: respuesta a preguntas de descubrimiento
        elif etapa == "descubrimiento" and necesidad_ctx.get("texto_original"):
            query_e = f"{necesidad_ctx['texto_original']} {mensaje}".strip()

            nec = await evaluar_necesidad(query_e)

            if nec["clara"]:
                res = await buscar_en_catalogo(query_e)

                if debe_intentar_enriquecimiento(res):
                    res = await enriquecer_y_buscar(query_e)

                contexto_extra, nueva_etapa, necesidad_ctx = construir_respuesta_desde_resultado(
                    res=res,
                    cliente=cliente,
                    productos_acumulados=productos_acumulados,
                    desde="descubrimiento",
                    necesidad_ctx_base={
                        "texto_original": necesidad_ctx.get("texto_original"),
                        "query_evaluada": query_e,
                    },
                )

            else:
                preguntas = nec["preguntas"]
                contexto_extra = "[NECESIDAD AÚN NO CLARA]\n" + "\n".join(
                    f"{i + 1}. {p}" for i, p in enumerate(preguntas)
                )
                nueva_etapa = "descubrimiento"
                necesidad_ctx = {"texto_original": necesidad_ctx.get("texto_original")}

        # Caso 4: solo saludo
        elif es_solo_saludo(mensaje):
            contexto_extra = (
                f"{saludo_personalizado(cliente)}\n"
                "[SALUDA Y PREGUNTA QUÉ NECESITA O SI TIENE CÓDIGO]"
            )
            nueva_etapa = "saludo"

        # Caso 5: código, referencia o descripción nueva
        else:
            tipo, valor = detectar_identificador(mensaje)

            if tipo:
                res = await rama_codigo(valor, tipo)

                contexto_extra, nueva_etapa, necesidad_ctx = construir_respuesta_desde_resultado(
                    res=res,
                    cliente=cliente,
                    productos_acumulados=productos_acumulados,
                    desde="codigo",
                    necesidad_ctx_base={
                        "texto_original": mensaje,
                        "query_evaluada": valor,
                    },
                )

            else:
                nec = await evaluar_necesidad(mensaje)

                if nec["clara"]:
                    res = await buscar_en_catalogo(mensaje)

                    if debe_intentar_enriquecimiento(res):
                        res = await enriquecer_y_buscar(mensaje)

                    contexto_extra, nueva_etapa, necesidad_ctx = construir_respuesta_desde_resultado(
                        res=res,
                        cliente=cliente,
                        productos_acumulados=productos_acumulados,
                        desde="busqueda",
                        necesidad_ctx_base={
                            "texto_original": mensaje,
                            "query_evaluada": mensaje,
                        },
                    )

                else:
                    preguntas = nec["preguntas"]
                    contexto_extra = (
                        f"[NECESIDAD NO CLARA — dominio: {nec['dominio']}]\n"
                        + "\n".join(f"{i + 1}. {p}" for i, p in enumerate(preguntas))
                    )
                    nueva_etapa = "descubrimiento"
                    necesidad_ctx = {"texto_original": mensaje}

       
        # Intenciones comerciales transversales
        # Solo se aplican si el estado comercial prioritario no resolvió el turno.
        # Esto evita que datos como cantidad, nombre, empresa o NIT sean tratados
        # como nuevas búsquedas o cambien de etapa por accidente.
        if not estado_comercial_resuelto:
            if any(w in msg_lower for w in PALABRAS_MAS):
                nueva_etapa = "acumulando"

            elif any(w in msg_lower for w in PALABRAS_FIN):
                nueva_etapa = "cotizacion"

            elif "presupuesto" in msg_lower or "fecha" in msg_lower:
                nueva_etapa = "calificacion"

            elif "rut" in msg_lower or "proforma" in msg_lower:
                nueva_etapa = "proforma"

            elif "pago" in msg_lower or "pse" in msg_lower or "transferencia" in msg_lower:
                nueva_etapa = "pago"
    # ─────────────────────────────────────────────────────────
    # Construcción de contexto para LLM o respuesta segura
    # ─────────────────────────────────────────────────────────

    ctx_cliente = ""
    if cliente:
        partes = []
        if cliente.get("nombre"):
            partes.append(f"Nombre: {cliente['nombre']}")
        if cliente.get("empresa"):
            partes.append(f"Empresa: {cliente['empresa']}")
        if cliente.get("nit"):
            partes.append(f"NIT: {cliente['nit']}")
        if cliente.get("email"):
            partes.append(f"Email: {cliente['email']}")

        if partes:
            ctx_cliente = "[DATOS DEL CLIENTE]\n" + "\n".join(partes) + "\n"

    ctx_carrito = ""
    if productos_acumulados:
        ctx_carrito = f"[CARRITO: {len(productos_acumulados)} producto(s)]\n"

        for i, item in enumerate(productos_acumulados[-5:], start=1):
            prod = item.get("producto", {})
            ctx_carrito += f"{i}. {prod.get('codigo', '—')} | {prod.get('nombre', '—')}"

            if item.get("cantidad"):
                ctx_carrito += f" | cant: {item['cantidad']}"

            ctx_carrito += "\n"

    ctx_faltantes = ""
    if nueva_etapa in {"cotizacion", "calificacion"}:
        faltantes = datos_faltantes(cliente, "cotizacion")
        if faltantes:
            ctx_faltantes = f"[DATO FALTANTE — pregunta solo este: {faltantes[0]}]\n"

    elif nueva_etapa == "proforma":
        faltantes = datos_faltantes(cliente, "proforma")
        if faltantes:
            ctx_faltantes = f"[DATO FALTANTE — pregunta solo este: {faltantes[0]}]\n"

    system = PROMPT_MAESTRO
    partes_ctx = [
        c for c in [ctx_cliente, ctx_carrito, ctx_faltantes, contexto_extra]
        if c
    ]

    if partes_ctx:
        system += "\n\n---\nCONTEXTO ACTUAL:\n" + "\n".join(partes_ctx)

    msg_llm = mensaje if mensaje.strip() else f"[Cliente envió archivo: {archivo_nombre}]"

    respuesta_segura = _extraer_respuesta_segura(contexto_extra)
    
    if nueva_etapa == "cotizacion_lista" and not respuesta_segura:
        respuesta_segura = (
            "Perfecto, ya tengo el producto, la cantidad y los datos básicos.\n\n"
            "Voy a dejar la solicitud lista para que un asesor revise disponibilidad, precio y condiciones antes de continuar con la cotización."
        )

    if respuesta_segura:
        respuesta = respuesta_segura

    else:
        respuesta = await call_nia(
            system=system,
            historial=historial[-20:],
            mensaje_usuario=msg_llm,
        )

        if contiene_placeholder(respuesta):
            logger.warning(
                "Respuesta del LLM contenía placeholders. Se reemplaza por respuesta segura sin resultado."
            )
            respuesta = respuesta_sin_resultado(cliente=cliente)
            nueva_etapa = "descubrimiento"

    logger.info("Respuesta generada: etapa=%s session=%s", nueva_etapa, session_id)

    turno_user = {
        "role": "user",
        "content": msg_llm,
        "ts": datetime.utcnow().isoformat(),
    }

    turno_nia = {
        "role": "assistant",
        "content": respuesta,
        "ts": datetime.utcnow().isoformat(),
    }

    await save_session(
        session_id=session_id,
        phone_id=phone_id,
        turnos=historial + [turno_user, turno_nia],
        etapa=nueva_etapa,
        archivo_activo=archivo_activo,
        necesidad_ctx=necesidad_ctx,
        cliente=cliente,
        productos_acumulados=productos_acumulados,
    )

    return {
        "respuesta": respuesta,
        "etapa": nueva_etapa,
        "items_resultado": items_resultado or None,
        "cliente": cliente or None,
    }


# ─────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────

@app.post("/nia/chat", response_model=ChatResponse)
@limiter.limit("30/minute")
async def nia_chat_texto(request: Request, req: ChatRequest):
    return ChatResponse(**await procesar_turno(
        session_id=req.session_id,
        mensaje=req.mensaje,
        phone_id=req.phone_id,
    ))


@app.post("/nia/chat/archivo", response_model=ChatResponse)
@limiter.limit("10/minute")
async def nia_chat_archivo(
    request: Request,
    session_id: str = Form(...),
    mensaje: str = Form(default=""),
    phone_id: str = Form(default=None),
    archivo: UploadFile = File(...),
):
    contenido = await archivo.read()

    return ChatResponse(**await procesar_turno(
        session_id=session_id,
        mensaje=mensaje,
        phone_id=phone_id,
        archivo_bytes=contenido,
        archivo_nombre=archivo.filename,
    ))


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "servicio": "NIA ViaIndustrial",
    }