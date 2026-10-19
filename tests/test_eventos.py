"""Fork: el evento `handoff` que avisa a los asesores (app/eventos.py).

Lo que importa: sale DESPUÉS de registrar el handoff en el CRM, lleva el motivo
completo del agente (el CRM lo reduce a cinco categorías), y si el webhook se
cae el turno sigue igual. Sin URL, el fork es la imagen oficial.
"""
from __future__ import annotations

import asyncio
import json

import httpx

from app import eventos
from app.llm import LlmExhausted, LlmReply, ToolCall
from app.main import create_app
from tests.conftest import (
    CRM_CONV_ID,
    IDENTITY,
    make_ctx,
    make_settings,
    mock_crm_basics,
    wa_body,
)

HOOK_URL = "http://n8n.test/webhook/nea-eventos"
MOTIVO = "FUERA DE CATALOGO: Suzuki Ertiga 2019"


def _ctx_con_eventos(**overrides):
    return make_ctx(
        make_settings(
            eventos_webhook_url=HOOK_URL,
            eventos_webhook_token="tok-eventos",
            agent_timezone="America/Bogota",
            **overrides,
        )
    )


async def _post(ctx, body: bytes) -> None:
    app = create_app(ctx=ctx)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as c:
        resp = await c.post("/webhook", content=body)
        assert resp.status_code == 200
        await asyncio.sleep(0.3)
    await ctx.crm.aclose()


def _escala_con(ctx, motivo: str) -> None:
    ctx.llm.replies = [
        LlmReply(
            content=None,
            tool_calls=[ToolCall(id="tc1", name="handoff", arguments={"reason": motivo})],
        ),
        LlmReply(content="Ese modelo no lo tengo en mi lista. Ya le avisé a un asesor 🙌"),
    ]


async def test_handoff_avisa_con_el_motivo_completo(respx_mock):
    ctx = _ctx_con_eventos()
    routes = mock_crm_basics(respx_mock)
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(200))
    _escala_con(ctx, MOTIVO)

    await _post(ctx, wa_body(text="tengo un Suzuki Ertiga"))

    assert routes["handoff"].call_count == 1
    assert hook.call_count == 1
    llamadas = [str(c.request.url) for c in respx_mock.calls]
    handoff_i = next(i for i, u in enumerate(llamadas) if u.endswith("/api/bot/handoff"))
    assert handoff_i < llamadas.index(HOOK_URL), "el aviso sale después del handoff"

    req = hook.calls[0].request
    assert req.headers["x-nea-token"] == "tok-eventos"
    body = json.loads(req.content)
    assert body["evento"] == "handoff"
    # El CRM recibe la categoría; el aviso, el texto del agente tal cual.
    assert body["motivo"] == MOTIVO
    assert body["identidad"] == IDENTITY
    assert body["nombre"] == "Lead de Prueba"
    assert body["telefono"] == IDENTITY
    assert body["crm_conversation_id"] == CRM_CONV_ID
    assert body["crm_contact_id"] == "ct_1"
    assert body["ficha"] == {}
    assert body["fecha"].endswith("-05:00")


async def test_llm_caido_tambien_avisa_como_error(respx_mock):
    ctx = _ctx_con_eventos()
    routes = mock_crm_basics(respx_mock)
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(200))
    ctx.llm.raise_exc = LlmExhausted("proveedor caído")

    await _post(ctx, wa_body(text="hola"))

    assert routes["handoff"].call_count == 1
    assert hook.call_count == 1
    assert json.loads(hook.calls[0].request.content)["motivo"] == "error"


async def test_sin_url_no_sale_nada(respx_mock):
    ctx = make_ctx()  # sin EVENTOS_WEBHOOK_URL: igual que la imagen oficial
    routes = mock_crm_basics(respx_mock)
    _escala_con(ctx, MOTIVO)

    await _post(ctx, wa_body(text="tengo un Suzuki Ertiga"))

    assert routes["handoff"].call_count == 1
    assert not any("n8n" in str(c.request.url) for c in respx_mock.calls)


async def test_webhook_caido_no_rompe_el_turno(respx_mock, monkeypatch):
    monkeypatch.setattr(eventos, "ESPERA_BASE", 0.0)
    ctx = _ctx_con_eventos()
    routes = mock_crm_basics(respx_mock)
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(502))
    _escala_con(ctx, MOTIVO)

    await _post(ctx, wa_body(text="tengo un Suzuki Ertiga"))

    assert routes["messages"].call_count == 1, "la despedida salió igual"
    assert routes["handoff"].call_count == 1, "el handoff quedó igual"
    assert hook.call_count == eventos.INTENTOS


async def test_handoff_que_falla_en_el_crm_no_avisa(respx_mock):
    ctx = _ctx_con_eventos()
    mock_crm_basics(respx_mock)
    respx_mock.post(f"{ctx.settings.crm_base_url}/api/bot/handoff").mock(
        return_value=httpx.Response(500, json={})
    )
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(200))
    _escala_con(ctx, MOTIVO)

    await _post(ctx, wa_body(text="tengo un Suzuki Ertiga"))

    # Si la IA no quedó en pausa, avisar «ya te lo pasé» sería mentirle al asesor.
    assert hook.call_count == 0


def test_payload_sin_contexto_lleva_lo_minimo():
    body = eventos.payload_handoff(
        identity="573000000000",
        crm_conversation_id="cv_9",
        motivo="  error  ",
        context=None,
        zona="Zona/Inexistente",
    )
    assert body["motivo"] == "error"
    assert body["telefono"] == "573000000000"
    assert body["nombre"] == "" and body["crm_contact_id"] == ""
    assert body["fecha"].endswith("+00:00")
