"""Fork: redes de seguridad sobre el texto saliente (app/redes.py + app/turn.py)."""
from __future__ import annotations

import asyncio
import json

from app.llm import LlmReply, ToolCall
from app.redes import limpiar_herramienta_filtrada, promete_aviso
from tests.conftest import mock_crm_basics, wa_body

# Tal cual salió en el banco de pruebas (Corolla Cross, 21 oct 2026).
FILTRADA = (
    "Tu Corolla Cross es híbrido o normal?update_ficha<arg_key>dolor_principal</arg_key>"
    "<arg_value>Necesita batería</arg_value></tool_call>"
)


def test_corta_la_herramienta_filtrada():
    texto, habia = limpiar_herramienta_filtrada(FILTRADA)
    assert habia is True
    assert texto == "Tu Corolla Cross es híbrido o normal?"


def test_marcado_solo_sin_nombre_de_herramienta():
    texto, habia = limpiar_herramienta_filtrada("Listo 🙌\n<tool_call>handoff</tool_call>")
    assert habia is True
    assert texto == "Listo 🙌"


def test_texto_normal_no_se_toca():
    normal = "Para tu Logan te sirven estas 🔋\n\n*Willard 42*\n⭐ Garantía 12 meses"
    assert limpiar_herramienta_filtrada(normal) == (normal, False)
    assert limpiar_herramienta_filtrada(None) == (None, False)


def test_detecta_la_promesa_de_aviso():
    assert promete_aviso("Ya le avisé a un asesor y en un momento se comunica contigo por aquí mismo 🙌")
    assert promete_aviso("eso te lo confirma un técnico: ya le avisé a un asesor")
    assert promete_aviso("Ya avisé al equipo, no tienes que hacer nada más")
    assert promete_aviso("le avisé a una persona del equipo")


def test_no_confunde_otras_frases():
    assert not promete_aviso("Cuando confirmes el pedido coordinamos la hora exacta de entrega.")
    assert not promete_aviso("Eso te lo confirma el técnico cuando llegue.")
    assert not promete_aviso("Te aviso apenas salga el domiciliario")
    assert not promete_aviso(None)


async def test_turno_con_herramienta_filtrada_manda_solo_el_texto(ctx, client, respx_mock):
    routes = mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content=FILTRADA)]
    await client.post("/webhook", content=wa_body(text="bateria para corolla cross"))
    await asyncio.sleep(0.3)
    assert routes["messages"].call_count == 1
    enviado = json.loads(routes["messages"].calls[0].request.content)["text"]
    assert "arg_key" not in enviado and "update_ficha" not in enviado
    assert enviado.startswith("Tu Corolla Cross es híbrido o normal?")


async def test_promesa_sin_handoff_escala_igual(ctx, client, respx_mock):
    routes = mock_crm_basics(respx_mock)
    ctx.llm.replies = [
        LlmReply(content="Eso mejor te lo confirma un técnico. Ya le avisé a un asesor y en un momento se comunica contigo por aquí mismo 🙌")
    ]
    await client.post("/webhook", content=wa_body(text="¿esa batería es sellada?"))
    await asyncio.sleep(0.3)
    assert routes["messages"].call_count == 1
    assert routes["handoff"].call_count == 1  # la red de seguridad escaló
    # Primero la despedida, luego el handoff (si no, 409 ai_paused).
    rutas = [str(c.request.url.path) for c in respx_mock.calls if "/api/bot/" in str(c.request.url)]
    assert rutas.index("/api/bot/messages") < rutas.index("/api/bot/handoff")


async def test_promesa_con_handoff_no_escala_dos_veces(ctx, client, respx_mock):
    routes = mock_crm_basics(respx_mock)
    ctx.llm.replies = [
        LlmReply(content=None, tool_calls=[ToolCall(id="t1", name="handoff", arguments={"reason": "FUERA DE CATALOGO: Ertiga"})]),
        LlmReply(content="Ese modelo no lo tengo en mi lista. Ya le avisé a un asesor 🙌"),
    ]
    await client.post("/webhook", content=wa_body(text="bateria suzuki ertiga"))
    await asyncio.sleep(0.3)
    assert routes["handoff"].call_count == 1


async def test_sin_promesa_no_escala(ctx, client, respx_mock):
    routes = mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content="¿Para qué carro necesitas la batería?")]
    await client.post("/webhook", content=wa_body(text="hola"))
    await asyncio.sleep(0.3)
    assert routes["handoff"].call_count == 0
