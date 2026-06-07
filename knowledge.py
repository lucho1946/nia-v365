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
