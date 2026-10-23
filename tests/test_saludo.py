"""Fork: el saludo del dueño sale tal cual en el primer contacto (app/saludo.py)."""
from __future__ import annotations

import asyncio
import json

import httpx

from app import saludo
from app.llm import LlmReply
from app.main import create_app
from app.profile import BusinessProfile
from tests.conftest import FakeLLM, make_ctx, mock_crm_basics, wa_body

SALUDO = (
    "¡Hola! 👋 Te habla Diana de Bogotana de Baterías, la empresa de baterías a "
    "domicilio mejor calificada de Bogotá ⭐ 4.9 con más de 700 reseñas en Google. "
    "¿Para qué carro necesitas la batería?"
)
SIN_PREGUNTA = SALUDO[: SALUDO.rfind(" ¿")]


class PerfilFijo:
    def __init__(self, perfil: BusinessProfile) -> None:
        self.perfil = perfil

    async def get(self) -> BusinessProfile:
        return self.perfil


def test_sin_pregunta_final():
    assert saludo.sin_pregunta_final(SALUDO) == SIN_PREGUNTA
    assert saludo.sin_pregunta_final("¡Hola! Soy Diana.") == "¡Hola! Soy Diana."


def test_solo_saludo_manda_el_saludo_completo():
    assert saludo.mensajes(SALUDO, "SOLO_SALUDO") == [SALUDO]
    assert saludo.mensajes(SALUDO, "  ") == [SALUDO]
    assert saludo.mensajes(SALUDO, None) == [SALUDO]


def test_con_respuesta_va_el_saludo_sin_pregunta_y_luego_la_respuesta():
    assert saludo.mensajes(SALUDO, "¿Tu Spark es normal o GT?") == [
        SIN_PREGUNTA,
        "¿Tu Spark es normal o GT?",
    ]


def test_si_el_modelo_saludo_igual_no_se_repite():
    respuesta = f"{SIN_PREGUNTA}\n\n¿Tu Spark es normal o GT?"
    assert saludo.mensajes(SALUDO, respuesta) == [SIN_PREGUNTA, "¿Tu Spark es normal o GT?"]
    # Banco de pruebas: «¡Hola! Sí, puedes pagar con datáfono…» tras el saludo.
    assert saludo.mensajes(SALUDO, "¡Hola! 👋 Por ahora solo manejamos carros 🙏") == [
        SIN_PREGUNTA,
        "Por ahora solo manejamos carros 🙏",
    ]


def test_es_solo_saludo():
    for texto in (
        "Hola",
        "hola!! 👋",
        "Buenas tardes",
        "Hola, solicito asesoría en baterías",
        "necesito una batería",
        "[Nota de voz del lead, transcrita]: hola buenos días",
    ):
        assert saludo.es_solo_saludo(texto), texto
    for texto in (
        "necesito una bateria para mi renault twingo",
        "Hola, ¿reciben tarjeta?",
        "Hola, solicito asesoría en baterías para moto",
        "tengo un spark",
        "chevrolet onix, no arranca",
        "",
    ):
        assert not saludo.es_solo_saludo(texto), texto


async def _enviados(
    respx_mock, texto: str, respuesta: str, wamid: str, llamadas: int = 1
) -> list[str]:
    llm = FakeLLM([LlmReply(content=respuesta)])
    ctx = make_ctx(llm=llm)
    ctx.profile = PerfilFijo(BusinessProfile(agent_name="Diana", greeting=SALUDO))
    routes = mock_crm_basics(respx_mock)
    app = create_app(ctx=ctx)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as c:
        await c.post("/webhook", content=wa_body(text=texto, wamid=wamid))
        await asyncio.sleep(0.3)
    await ctx.crm.aclose()
    assert len(llm.calls) == llamadas
    return [json.loads(call.request.content)["text"] for call in routes["messages"].calls]


async def test_hola_recibe_el_saludo_exacto_sin_llamar_al_modelo(respx_mock):
    enviados = await _enviados(respx_mock, "hola", "no se usa", "wamid.sal1", llamadas=0)
    assert enviados == [SALUDO]


async def test_el_modelo_tambien_puede_pedir_solo_el_saludo(respx_mock):
    enviados = await _enviados(respx_mock, "hola, qué más pues", "SOLO_SALUDO", "wamid.sal3")
    assert enviados == [SALUDO]


async def test_con_el_carro_saludo_y_respuesta_en_dos_mensajes(respx_mock):
    enviados = await _enviados(
        respx_mock, "tengo un spark", "¿Tu Spark es normal o Spark GT?", "wamid.sal2"
    )
    assert enviados == [SIN_PREGUNTA, "¿Tu Spark es normal o Spark GT?"]
