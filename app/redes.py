"""Fork: dos redes de seguridad sobre el texto que Nea está por mandar.

Las dos atacan fallos que el lead SÍ sufre y que el prompt no evita siempre
(banco de pruebas de Bogotana, 21 oct 2026):

1. Herramienta filtrada. GLM a veces escribe la llamada a una herramienta
   como texto al final del mensaje («¿es híbrido o normal?update_ficha
   <arg_key>…</tool_call>») y ese texto le llegaba tal cual al cliente.
   `limpiar_herramienta_filtrada` corta desde el primer rastro de marcado.

2. Promesa sin escalar. Diana decía «Ya le avisé a un asesor…» sin llamar
   `handoff`: el cliente esperaba y ningún asesor se enteraba.
   `promete_aviso` detecta la frase para que el turno escale solo.
"""
from __future__ import annotations

import re

# Nombres de herramientas que el modelo puede pegar delante del marcado.
_HERRAMIENTAS = (
    "update_ficha|propose_slots|book_session|reschedule_session|route_out|handoff"
    "|list_bookings|cancel_session|coordination_consent"
)

# Primer rastro de una llamada a herramienta escrita como texto: el nombre de
# una herramienta pegado a una etiqueta, o la etiqueta sola.
_MARCADO = re.compile(
    r"(?:\b(?:" + _HERRAMIENTAS + r")\s*)?"
    r"<\s*/?\s*(?:tool_call|tool_calls|arg_key|arg_value|function_call|function_calls|invoke|parameter)\b",
    re.IGNORECASE,
)

# «Ya le avisé a un asesor», «ya le avisé a una persona del equipo»,
# «ya avisé al equipo», «le avisé a un técnico»…
_PROMESA = re.compile(
    r"\b(?:ya\s+)?(?:le\s+)?avis[eé]\s+(?:a\s+(?:un|una|el|la|nuestro|nuestra)?\s*|al\s+)"
    r"(?:asesor|asesora|persona|t[eé]cnico|equipo|humano)",
    re.IGNORECASE,
)


def limpiar_herramienta_filtrada(texto: str | None) -> tuple[str | None, bool]:
    """(texto sin el marcado filtrado, ¿había marcado?).

    Corta desde el primer rastro hasta el final: lo que viene detrás de una
    llamada filtrada es parte de la llamada, nunca mensaje para el cliente.
    """
    if not texto:
        return texto, False
    m = _MARCADO.search(texto)
    if m is None:
        return texto, False
    return texto[: m.start()].rstrip(), True


def promete_aviso(texto: str | None) -> bool:
    """¿El mensaje le dice al cliente que ya se avisó a una persona?"""
    return bool(texto) and _PROMESA.search(texto) is not None
