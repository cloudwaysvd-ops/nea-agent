"""Fork: el catálogo de fichas vive en una hoja, no en el prompt.

Antes, las ~90 fichas de Bogotana (≈60.000 caracteres) viajaban completas en
CADA llamada al modelo, pegadas a mano en 12 bloques del CRM. Ahora viven en
una hoja de cálculo (Google Sheets publicada como CSV, o un CSV local) y Diana
consulta solo la del carro con la herramienta `buscar_ficha`.

La hoja tiene una fila por batería. Columnas (la primera fila son los nombres;
el orden no importa y las mayúsculas/tildes tampoco):

    ficha               el carro como lo busca la gente: «Kia Picanto»
    marca               «Kia»
    se_escribe_tambien  otras formas de escribirlo, separadas por coma
    nota                pregunta obligatoria o aviso para Diana
    modelo              el carro tal cual va en la línea (vacío = la ficha)
    medida              «NS40», «36», «LN3 o LN4»…
    referencia          «Alphaline NS40-540» (vacía = fila solo de nota)
    garantia_meses      15 (o «sin dato»)
    precio              320000 (con o sin $ y puntos)
    precio_con_usada    280000
    no_se_cotiza        «sí» si esa batería no se ofrece (Diana escala)

`se_escribe_tambien` y `nota` basta con ponerlos en una fila de la ficha; si
van en varias, se juntan sin repetir.

Cada ficha se le muestra al modelo con el MISMO texto que tenía en el
conocimiento («- Batería para X: **Ref** (medida M) — garantía N meses — $p ·
$u entregando la usada»), así que el formato de cotización y la red de
precios (`app/redes.py`, `cotizacion_ajena`) no cambian.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("nea.catalogo")

CATALOGO_TTL_SECONDS = 300.0
MAX_FICHAS_POR_CONSULTA = 4

COLUMNAS = (
    "ficha",
    "marca",
    "se_escribe_tambien",
    "nota",
    "modelo",
    "medida",
    "referencia",
    "garantia_meses",
    "precio",
    "precio_con_usada",
    "no_se_cotiza",
)

TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "buscar_ficha",
        "description": (
            "Trae del catálogo la ficha del carro del cliente: medida, referencias, "
            "garantía, los dos precios y la pregunta obligatoria si la tiene. "
            "Llámala SIEMPRE antes de dar medida, referencia o precio, con la marca "
            "y el modelo que dijo el cliente (p. ej. «Kia Picanto»). Para un carro "
            "start-stop usa «carro start-stop»; para camión liviano, «camión "
            "liviano». Copia de la ficha las líneas tal cual. Si no hay ficha, no "
            "cotices: ruta segura."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "modelo": {
                    "type": "string",
                    "description": "Marca y modelo del carro, como lo dijo el cliente",
                }
            },
            "required": ["modelo"],
        },
    },
}

INSTRUCCION_PROMPT = (
    "CATÁLOGO DE FICHAS: las fichas de cada modelo (medida, referencias, garantía y "
    "precios) NO están en el conocimiento: se consultan con la herramienta "
    "buscar_ficha. Antes de dar medida, referencia o precio, llama buscar_ficha con "
    "el carro del cliente y copia sus líneas tal cual. El Índice de modelos con "
    "ficha sigue diciendo qué carros se cotizan; si buscar_ficha no trae ficha, no "
    "cotices: ruta segura."
)

_SI = {"si", "sí", "x", "true", "1", "verdadero", "yes"}


def _norm(texto: str) -> str:
    """minúsculas, sin tildes; guiones fuera («cx-30» = «cx30»)."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", (texto or "").lower())
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"[-–]", "", sin_tildes)


def _tokens(texto: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9ñ]+", _norm(texto)))


def _pesos(valor: str) -> str | None:
    """«$320.000», «320000», «320,000» → «320.000». None si no hay número."""
    digitos = re.sub(r"\D", "", valor or "")
    if not digitos:
        return None
    return f"{int(digitos):,}".replace(",", ".")


def _clave(nombre: str) -> str:
    return re.sub(r"[^a-z]", "", _norm(nombre))


@dataclass(frozen=True)
class Opcion:
    modelo: str
    medida: str
    referencia: str
    garantia: str  # «15» o «sin dato»
    precio: str
    usada: str
    no_se_cotiza: bool = False

    def linea(self) -> str:
        if self.garantia.isdigit():
            garantia = f"garantía {self.garantia} meses"
        else:
            garantia = f"garantía {self.garantia or 'sin dato'}"
        aviso = " — no cotizar, escalar" if self.no_se_cotiza else ""
        return (
            f"- Batería para {self.modelo}: **{self.referencia}** (medida {self.medida})"
            f" — {garantia}{aviso} — ${self.precio} · ${self.usada} entregando la usada"
        )


@dataclass
class Ficha:
    nombre: str
    marca: str = ""
    alias: list[str] = field(default_factory=list)
    notas: list[str] = field(default_factory=list)
    medidas_sueltas: list[str] = field(default_factory=list)  # filas sin referencia
    opciones: list[Opcion] = field(default_factory=list)

    def medidas(self) -> str:
        vistas: list[str] = []
        for m in [o.medida for o in self.opciones] + self.medidas_sueltas:
            if m and m not in vistas:
                vistas.append(m)
        return " · ".join(vistas)

    def texto(self) -> str:
        cabecera = f"MODELO: {self.nombre}"
        if self.marca:
            cabecera += f" · MARCA: {self.marca}"
        if self.medidas():
            cabecera += f" · MEDIDA: {self.medidas()}"
        partes = [f"## {self.nombre}", cabecera]
        if self.alias:
            partes[-1] += "\nSE ESCRIBE TAMBIÉN: " + ", ".join(self.alias)
        partes += [f"⚠️ {self.nombre}: {n}" for n in self.notas]
        if self.opciones:
            partes.append("\n".join(o.linea() for o in self.opciones))
        return "\n\n".join(partes)

    def nombres(self) -> list[tuple[str, ...]]:
        """Formas de nombrar la ficha, como secuencias de palabras."""
        crudas = [self.nombre, *self.alias, *{o.modelo for o in self.opciones}]
        vistas: list[tuple[str, ...]] = []
        for c in crudas:
            # «Chevrolet Onix (aspirado, no turbo)» se busca como «chevrolet onix».
            t = _tokens(re.sub(r"\(.*?\)", "", c))
            # Un alias de puro número («3» del Mazda 3) no sirve para buscar:
            # pescaría «BMW serie 3».
            if t and not all(x.isdigit() for x in t) and t not in vistas:
                vistas.append(t)
        return vistas


@dataclass
class Catalogo:
    fichas: list[Ficha]
    problemas: list[str] = field(default_factory=list)

    @property
    def opciones(self) -> int:
        return sum(len(f.opciones) for f in self.fichas)

    def texto_lineas(self) -> str:
        """Todas las líneas de todas las fichas: lo que lee la red de precios."""
        return "\n".join(o.linea() for f in self.fichas for o in f.opciones)

    def texto(self) -> str:
        return "\n\n---\n\n".join(f.texto() for f in self.fichas)

    def buscar(self, consulta: str) -> list[Ficha]:
        """Las fichas que nombra la consulta (las más específicas primero).

        1. Un nombre de la ficha (o un «se escribe también») aparece completo en
           la consulta: «kia picanto 2015» → Kia Picanto. Si dos se pisan, gana
           el más largo: «spark gt» → Spark GT, no Spark.
        2. Si no, la consulta aparece dentro de un nombre: «corolla» → Corolla
           (sedán) y Corolla Cross, para que el modelo pregunte cuál.
        El año no cuenta: «2019» se ignora. Si la consulta nombra una marca,
        solo cuentan las fichas de esa marca (o sin marca, como start-stop).
        """
        q = self._consulta(consulta)
        if not q:
            return []
        marcas = self._marcas()
        de_marca = {t for t in q if t in marcas}

        def marca_ok(f: Ficha) -> bool:
            return not de_marca or not f.marca or bool(set(_tokens(f.marca)) & de_marca)

        candidatas = [f for f in self.fichas if marca_ok(f)]

        def contiene(grande: tuple[str, ...], chico: tuple[str, ...]) -> tuple[int, int] | None:
            # Palabras seguidas de `grande` que, juntas, son `chico` juntas:
            # «cx 30» = «cx30», «mazda 2» = «mazda2».
            meta = "".join(chico)
            for i in range(len(grande)):
                acumulado = ""
                for j in range(i, len(grande)):
                    acumulado += grande[j]
                    if acumulado == meta:
                        return (i, j + 1)
                    if len(acumulado) >= len(meta):
                        break
            return None

        directas: list[tuple[Ficha, tuple[int, int]]] = []
        for f in candidatas:
            mejor: tuple[int, int] | None = None
            for nombre in f.nombres():
                span = contiene(q, nombre)
                if span is not None and (mejor is None or span[1] - span[0] > mejor[1] - mejor[0]):
                    mejor = span
            if mejor is not None:
                directas.append((f, mejor))
        if directas:
            # Fuera las que quedan dentro de otra más larga («spark» ⊂ «spark gt»).
            def dentro(a: tuple[int, int], b: tuple[int, int]) -> bool:
                return b[0] <= a[0] and a[1] <= b[1] and (b[1] - b[0]) > (a[1] - a[0])

            quedan = [
                f for f, s in directas if not any(dentro(s, s2) for _, s2 in directas)
            ]
            return quedan[:MAX_FICHAS_POR_CONSULTA]

        # «hyundai tucson» → «hyundai new tucson»: sin la marca, el resto
        # tiene que estar dentro de un nombre.
        resto = tuple(t for t in q if t not in marcas)
        if not resto:
            return []
        parciales = [
            f for f in candidatas if any(contiene(nombre, resto) for nombre in f.nombres())
        ]
        return parciales[:MAX_FICHAS_POR_CONSULTA]

    @staticmethod
    def _consulta(consulta: str) -> tuple[str, ...]:
        return tuple(t for t in _tokens(consulta) if not re.fullmatch(r"(19|20)\d\d", t))

    def _marcas(self) -> set[str]:
        return {t for f in self.fichas for t in _tokens(f.marca)}

    def consultar(self, consulta: str) -> dict[str, Any]:
        """Respuesta de la herramienta buscar_ficha."""
        fichas = self.buscar(consulta)
        q = self._consulta(consulta)
        if not fichas and q and set(q) <= self._marcas():
            de_la_marca = [f.nombre for f in self.fichas if set(_tokens(f.marca)) & set(q)]
            return {
                "ok": True,
                "fichas": [],
                "resultado": (
                    "Falta el modelo: pregúntale al cliente cuál es su carro antes de "
                    "cotizar. Modelos con ficha de esa marca: " + ", ".join(de_la_marca)
                ),
            }
        if not fichas:
            return {
                "ok": True,
                "fichas": [],
                "resultado": (
                    f"No hay ficha para «{consulta}» en el catálogo. No cotices de "
                    "memoria ni con la ficha de otro carro. Si es start-stop, busca "
                    "«carro start-stop»; si no, ruta segura (escala con FUERA DE "
                    "CATALOGO)."
                ),
            }
        varias = len(fichas) > 1
        return {
            "ok": True,
            "fichas": [f.nombre for f in fichas],
            "resultado": (
                ("Hay varias fichas posibles: si no sabes cuál es la del cliente, "
                 "pregúntale antes de cotizar. " if varias else "")
                + "Copia referencia, garantía y los dos precios tal cual; no mezcles "
                "fichas.\n\n" + "\n\n---\n\n".join(f.texto() for f in fichas)
            ),
        }


def _encabezado(nombre: str) -> str | None:
    clave = _clave(nombre)
    for col in COLUMNAS:
        if _clave(col) == clave:
            return col
    alias = {
        "garantia": "garantia_meses",
        "preciousada": "precio_con_usada",
        "precioconlausada": "precio_con_usada",
        "alias": "se_escribe_tambien",
        "seescribetambien": "se_escribe_tambien",
        "nosecotiza": "no_se_cotiza",
    }
    return alias.get(clave)


def desde_csv(texto: str) -> Catalogo:
    """Arma el catálogo desde el CSV de la hoja. Filas malas: fuera, y a `problemas`."""
    lector = csv.reader(io.StringIO(texto.lstrip("﻿")))
    filas = list(lector)
    if not filas:
        return Catalogo(fichas=[], problemas=["la hoja está vacía"])
    columnas = [_encabezado(c) for c in filas[0]]
    faltan = [c for c in ("ficha", "referencia", "precio") if c not in columnas]
    if faltan:
        return Catalogo(fichas=[], problemas=[f"faltan columnas: {', '.join(faltan)}"])

    fichas: dict[str, Ficha] = {}
    problemas: list[str] = []
    for n, fila in enumerate(filas[1:], start=2):
        d = {c: (fila[i].strip() if i < len(fila) else "") for i, c in enumerate(columnas) if c}
        nombre = d.get("ficha", "")
        if not any(d.values()):
            continue
        if not nombre:
            problemas.append(f"fila {n}: sin «ficha»")
            continue
        ficha = fichas.get(_norm(nombre))
        if ficha is None:
            ficha = fichas[_norm(nombre)] = Ficha(nombre=nombre)
        if d.get("marca") and not ficha.marca:
            ficha.marca = d["marca"]
        # Coma separa; la coma dentro de paréntesis no: «onix (aspirado, no turbo)».
        for a in re.split(r",(?![^()]*\))", d.get("se_escribe_tambien") or ""):
            a = a.strip()
            if a and a not in ficha.alias:
                ficha.alias.append(a)
        for nota in (d.get("nota") or "").splitlines():  # un renglón, una nota
            nota = nota.strip()
            if nota and nota not in ficha.notas:
                ficha.notas.append(nota)
        referencia = d.get("referencia") or ""
        if not referencia:
            if d.get("medida"):
                ficha.medidas_sueltas.append(d["medida"])
            continue  # fila solo de nota
        no_cotiza = _norm(d.get("no_se_cotiza") or "").split(" ")[0] in _SI
        precio, usada = _pesos(d.get("precio", "")), _pesos(d.get("precio_con_usada", ""))
        garantia_txt = (d.get("garantia_meses") or "").strip()
        garantia = re.sub(r"\D", "", garantia_txt) or ("sin dato" if no_cotiza else "")
        malos = []
        if not d.get("medida"):
            malos.append("medida")
        if precio is None:
            malos.append("precio")
        if usada is None:
            malos.append("precio_con_usada")
        if not garantia:
            malos.append("garantia_meses")
        if malos:
            problemas.append(
                f"fila {n} ({nombre}, {referencia}): falta o no se entiende {', '.join(malos)} — no se ofrece"
            )
            continue
        ficha.opciones.append(
            Opcion(
                modelo=d.get("modelo") or nombre,
                medida=d["medida"],
                referencia=referencia,
                garantia=garantia,
                precio=precio,
                usada=usada,
                no_se_cotiza=no_cotiza,
            )
        )
    return Catalogo(fichas=list(fichas.values()), problemas=problemas)


class CatalogoProvider:
    """La hoja con caché TTL. Si falla, el último catálogo bueno; nunca tumba un turno.

    `url`: el CSV publicado de Google Sheets (Archivo → Compartir → Publicar en
    la web → CSV). `path`: un CSV local, de respaldo si la URL nunca respondió
    (o el único, en pruebas). Una hoja que llega con menos de la mitad de las
    baterías que la anterior se toma por un error de edición y no se usa.
    """

    def __init__(self, url: str = "", path: str = "", ttl: float = CATALOGO_TTL_SECONDS) -> None:
        self._url = url.strip()
        self._path = Path(path) if path else None
        self._ttl = ttl
        self._cached: Catalogo | None = None
        self._fetched_at = 0.0

    async def _bajar(self) -> str | None:
        if not self._url:
            return None
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as c:
                r = await c.get(self._url)
                r.raise_for_status()
                return r.content.decode("utf-8")
        except Exception as exc:
            logger.warning("catálogo: no pude bajar la hoja (%s) — uso el último bueno", exc)
            return None

    def _leer_local(self) -> str | None:
        if self._path is None:
            return None
        try:
            return self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("catálogo: no pude leer %s: %s", self._path, exc)
            return None

    def _aceptable(self, nuevo: Catalogo) -> bool:
        if not nuevo.fichas or nuevo.opciones == 0:
            logger.error("catálogo: la hoja llegó sin baterías (%s) — no la uso", "; ".join(nuevo.problemas) or "vacía")
            return False
        if self._cached is not None and nuevo.opciones < self._cached.opciones / 2:
            logger.error(
                "catálogo: la hoja trae %d baterías y antes %d — parece un error de edición, sigo con la anterior",
                nuevo.opciones,
                self._cached.opciones,
            )
            return False
        return True

    async def get(self) -> Catalogo | None:
        ahora = time.monotonic()
        if self._cached is not None and (ahora - self._fetched_at) < self._ttl:
            return self._cached
        texto = await self._bajar()
        if texto is None and self._cached is None:
            texto = self._leer_local()
        self._fetched_at = ahora
        if texto is not None:
            nuevo = desde_csv(texto)
            for p in nuevo.problemas:
                logger.warning("catálogo: %s", p)
            if self._aceptable(nuevo):
                if self._cached is None or nuevo.texto() != self._cached.texto():
                    logger.info("catálogo: %d fichas, %d baterías", len(nuevo.fichas), nuevo.opciones)
                self._cached = nuevo
        return self._cached
