"""Fork: el saludo del dueño sale tal cual en el primer contacto.

Con «úsalo como base» el modelo lo reescribía a su gusto («la mejor calificada
de Bogotá ⭐ 4.9. Con gusto te la llevamos…»), y en el banco de pruebas solo
4 de 26 primeros mensajes lo traían completo. Pedirle que lo copiara subió a
15 de 26: cuando escalaba de una o el carro estaba varado, otras reglas
ganaban. Por eso el saludo no lo escribe el modelo: lo manda el código, en un
mensaje aparte, y el modelo contesta después sin volver a saludar.

Si el lead todavía no dijo nada concreto («hola»), sale el saludo completo,
con su pregunta, sin llamar al modelo (`es_solo_saludo`; si se le escapa
alguno, el modelo puede responder `SOLO_SALUDO` y pasa lo mismo). Si ya lo dijo
(«tengo un Spark»), el saludo sale sin la pregunta final —el lead ya la
contestó— y detrás va la respuesta del modelo.
"""
from __future__ import annotations

import re
import unicodedata

MARCA = "SOLO_SALUDO"

# Palabras que no dicen nada concreto en un primer mensaje: «hola», «buenas
# tardes», «solicito asesoría en baterías» (el texto del anuncio, 41 de 110
# chats reales), «necesito una batería». Si el mensaje es solo esto, sale el
# saludo completo sin preguntarle al modelo: con la marca SOLO_SALUDO el
# modelo igual se inventaba su propia pregunta en el banco de pruebas.
_VACIAS = set(
    "hola holi ola buenas buenos buen buena dia dias tarde tardes noche noches "
    "saludos que tal como estas esta estan muy amigo amiga senor senora "
    "info informacion solicito solicitar quiero quisiera queria necesito "
    "necesitaria busco me gustaria pedir asesoria cotizacion cotizar precio "
    "precios en de del sobre una un unas unos la el las los bateria baterias "
    "por favor porfa porfavor gracias y o a con para hay tienen manejan".split()
)
_MARCADOR_MEDIA = re.compile(r"\[[^\]]*\]:?")
_RESALUDO = re.compile(r"^\s*(?:¡\s*)?(?:hola|buenas)\b[!¡.,\s]*(?:👋\s*)?", re.IGNORECASE)


def _plano(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto)
    sin_tildes = "".join(c for c in sin_tildes if not unicodedata.combining(c))
    return sin_tildes.lower()


def es_solo_saludo(texto: str | None) -> bool:
    """¿El primer mensaje del lead no trae nada concreto todavía?"""
    plano = _plano(_MARCADOR_MEDIA.sub(" ", texto or ""))
    palabras = re.findall(r"[a-z0-9]+", plano)
    return bool(palabras) and all(p in _VACIAS for p in palabras)


def quitar_resaludo(texto: str) -> str:
    """Le quita a la respuesta un «¡Hola! 👋» de entrada: el saludo ya salió."""
    sin = _RESALUDO.sub("", texto, count=1).lstrip()
    if not sin:
        return texto
    return sin[0].upper() + sin[1:]


def instruccion(saludo: str) -> str:
    """La línea de CONTEXTO ACTUAL del primer contacto."""
    return (
        "- Es el PRIMER contacto. El sistema le envía al lead, ANTES de tu "
        f"respuesta y en un mensaje aparte, el saludo del negocio: «{saludo}». "
        "Tú NO saludas, NO te presentas y NO repites la pregunta del saludo: "
        "contesta directo a lo que escribió el lead. Si el lead solo saludó o "
        "todavía no dijo nada concreto, tu respuesta es únicamente "
        f"{MARCA}, sin nada más."
    )


def sin_pregunta_final(saludo: str) -> str:
    """El saludo sin su última pregunta («¿Para qué carro…?»)."""
    texto = saludo.rstrip()
    if not texto.endswith("?"):
        return texto
    corte = texto.rfind("¿")
    if corte <= 0:
        return texto
    return texto[:corte].rstrip()


def mensajes(saludo: str, respuesta: str | None) -> list[str]:
    """Lo que sale en el primer contacto, en orden.

    Si el modelo saludó de todos modos con el saludo exacto, se le quita para
    no mandarlo dos veces.
    """
    texto = (respuesta or "").replace(MARCA, "").strip()
    for repetido in (saludo.strip(), sin_pregunta_final(saludo)):
        if repetido and texto.startswith(repetido):
            texto = texto[len(repetido):].strip()
    if texto:
        texto = quitar_resaludo(texto)
    if not texto:
        return [saludo.strip()]
    return [sin_pregunta_final(saludo), texto]
