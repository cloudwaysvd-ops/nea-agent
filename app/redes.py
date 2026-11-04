"""Fork: tres redes de seguridad sobre el texto que Nea está por mandar.

Las tres atacan fallos que el lead SÍ sufre y que el prompt no evita siempre
(banco de pruebas de Bogotana, 21 oct 2026):

1. Herramienta filtrada. GLM a veces escribe la llamada a una herramienta
   como texto al final del mensaje («¿es híbrido o normal?update_ficha
   <arg_key>…</tool_call>») y ese texto le llegaba tal cual al cliente.
   `limpiar_herramienta_filtrada` corta desde el primer rastro de marcado.

2. Promesa sin escalar. Diana decía «Ya le avisé a un asesor…» sin llamar
   `handoff`: el cliente esperaba y ningún asesor se enteraba.
   `promete_aviso` detecta la frase para que el turno escale solo.

3. Cotización contaminada. Con el catálogo entero en el prompt, GLM a veces
   mezcla la ficha del carro con la vecina: al Kia Picanto (NS40) le ponía la
   «Moura NS40-950» y el precio de la Alphaline del Kia Soluto (36), que va
   justo después (banco, 4 nov 2026; también con el perfil en producción).
   `cotizacion_ajena` exige que todas las opciones sean líneas de UNA ficha
   de ese carro, sin cambiar referencia, garantía ni precios.
"""
from __future__ import annotations

import re
import unicodedata

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


# Línea de ficha: «- Batería para Kia Picanto: **Alphaline NS40-540** (medida
# NS40) — garantía 15 meses — $320.000 · $280.000 entregando la usada».
_LINEA_FICHA = re.compile(
    r"Batería para ([^:\n]+): \*\*([^*\n]+)\*\* \(medida [^)]*\) — garantía (\d+) meses"
    r" — \$([\d.]+) · \$([\d.]+)"
)

# Opción del formato fijo de cotización: «*Ref*» (o «**Ref**»), luego
# «⭐ Garantía N meses» y «💰 $precio · $precio con usada…».
_OPCION = re.compile(
    r"\*{1,2}([^*\n]+?)\*{1,2}[^\n]*\n\s*⭐\s*Garant[ií]a\s+(\d+)\s+meses[^\n]*\n"
    r"\s*💰\s*\$([\d.]+)\s*·\s*\$([\d.]+)"
)

# Ficha que no es de un modelo: vale para cualquier carro start-stop. La de
# camión liviano no entra aquí: con ella pasaría cualquier caja 27 o 31H para
# un carro sin ficha; exige que el mensaje diga «liviano».
_GENERICAS = ("start-stop",)

# Palabras del nombre de la ficha que no identifican al carro.
_VACIAS = {"no", "a", "y", "de", "o", "con", "sin", "aspirado", "turbo", "hibrido",
           "diesel", "gasolina", "crdi", "sedan", "normal", "version"}


def _norm(texto: str) -> str:
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"[-–]", "", sin_tildes)


def _palabras(texto: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", _norm(texto)))


def _fichas(kb_text: str) -> dict[str, set[tuple[str, str, str, str]]]:
    fichas: dict[str, set[tuple[str, str, str, str]]] = {}
    for m in _LINEA_FICHA.finditer(kb_text):
        modelo, ref, gar, precio, usada = (g.strip() for g in m.groups())
        fichas.setdefault(modelo, set()).add((_norm(ref), gar, precio, usada))
    return fichas


def _nombra(texto: set[str], modelo: str) -> bool:
    if any(g in modelo.lower() for g in _GENERICAS):
        return True
    marca, *resto = modelo.split(maxsplit=1)
    propias = _palabras(resto[0] if resto else marca) - _VACIAS - _palabras(marca)
    return not propias or bool(propias & texto)


def cotizacion_ajena(texto: str | None, kb_text: str | None) -> str | None:
    """Por qué esta cotización no sale tal cual del catálogo, o None si sale.

    Revisa solo mensajes con el formato fijo de cotización. Cada opción
    (referencia, garantía, precio y precio con usada) tiene que ser una línea
    de una MISMA ficha, y esa ficha tiene que ser del carro que nombra el
    mensaje (o una genérica, como la de start-stop).
    """
    if not texto or not kb_text:
        return None
    crudas = [(r.strip(), g, p, u) for r, g, p, u in _OPCION.findall(texto)]
    opciones = [(_norm(r), g, p, u) for r, g, p, u in crudas]
    if not opciones:
        return None
    fichas = _fichas(kb_text)
    if not fichas:
        return None
    candidatas = [m for m, lineas in fichas.items() if all(o in lineas for o in opciones)]
    if not candidatas:
        malas = [c for c, o in zip(crudas, opciones) if not any(o in lineas for lineas in fichas.values())]
        if malas:
            r, g, p, u = malas[0]
            detalle = f"la opción «{r}» con garantía {g} meses, ${p} · ${u} no existe en el catálogo"
        else:
            detalle = "mezclaba opciones de fichas de carros distintos"
        return (
            f"cotizaba algo que no está en la ficha del carro: {detalle}. Busca la ficha "
            "de ESE carro y copia sus líneas tal cual (referencia, garantía y los dos "
            "precios); no completes con líneas de otra ficha"
        )
    nombradas = _palabras(texto)
    if not any(_nombra(nombradas, m) for m in candidatas):
        return (
            f"copió la ficha de otro carro ({candidatas[0]}). Busca la ficha del carro "
            "del cliente y copia sus líneas tal cual; si no tiene ficha, ruta segura"
        )
    return None
