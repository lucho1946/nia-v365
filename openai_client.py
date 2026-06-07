"""
openai_client.py — Cliente async GPT-4o-mini
Incluye call_llm_json para respuestas estructuradas (evaluar_necesidad).
"""
import os, json, logging
import httpx

logger = logging.getLogger("nia.openai")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL          = "gpt-4o-mini"
MAX_TOKENS     = 800
TEMPERATURE    = 0.3

async def _post(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}
        )
        r.raise_for_status()
        return r.json()

async def call_nia(system: str, historial: list, mensaje_usuario: str) -> str:
    """Llamada principal al LLM con historial completo."""
    messages = [{"role": "system", "content": system}]
    for t in historial:
        if t.get("role") in ("user", "assistant") and t.get("content"):
            messages.append({"role": t["role"], "content": t["content"]})
    messages.append({"role": "user", "content": mensaje_usuario})

    try:
        data = await _post({
            "model": MODEL, "max_tokens": MAX_TOKENS,
            "temperature": TEMPERATURE, "messages": messages
        })
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Error call_nia: {e}")
        return ("En este momento tengo un inconveniente técnico. "
                "Por favor escríbenos directamente o intenta en unos minutos.")

async def call_llm_json(prompt: str) -> dict:
    """
    Llamada al LLM que retorna JSON estructurado.
    Usada por evaluar_necesidad() para decisiones binarias.
    """
    try:
        data = await _post({
            "model": MODEL, "max_tokens": 150, "temperature": 0.1,
            "messages": [{"role": "user", "content": prompt}]
        })
        texto = data["choices"][0]["message"]["content"].strip()
        # Limpiar markdown si viene con ```json
        texto = texto.replace("```json", "").replace("```", "").strip()
        return json.loads(texto)
    except json.JSONDecodeError as e:
        logger.warning(f"JSON inválido de LLM: {e}")
        return {"clara": False}
    except Exception as e:
        logger.error(f"Error call_llm_json: {e}")
        return {"clara": False}
