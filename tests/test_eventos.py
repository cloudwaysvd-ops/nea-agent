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
    crm_context,
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


# ---------------------------------------------------------------- codigo ---

TEXTO_CON_CODIGO = "Hola, solicito asesoría en baterías (Ref. C7K2QX)"


def _textos_al_llm(ctx) -> list[str]:
    out = []
    for call in ctx.llm.calls:
        for m in call["messages"]:
            if m.get("role") == "user":
                c = m.get("content")
                out.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
    return out


async def test_codigo_va_al_webhook_y_no_al_llm(respx_mock):
    ctx = _ctx_con_eventos()
    mock_crm_basics(respx_mock)
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(200))
    ctx.llm.replies = [LlmReply(content="¡Hola! ¿Qué carro tienes?")]

    await _post(ctx, wa_body(text=TEXTO_CON_CODIGO, wamid="wamid.ref1"))

    eventos_codigo = [
        json.loads(c.request.content)
        for c in hook.calls
        if json.loads(c.request.content)["evento"] == "codigo"
    ]
    assert len(eventos_codigo) == 1
    ev = eventos_codigo[0]
    assert ev["codigo"] == "C7K2QX"
    assert ev["texto"] == TEXTO_CON_CODIGO, "n8n recibe el texto tal cual"
    assert ev["identidad"] == IDENTITY
    assert ev["nombre"] == "Lead de Prueba"
    assert ev["wamid"] == "wamid.ref1"
    assert hook.calls[0].request.headers["x-nea-token"] == "tok-eventos"

    textos = _textos_al_llm(ctx)
    assert textos, "el turno sí llegó al LLM"
    assert not any("C7K2QX" in t for t in textos), "el agente nunca ve el código"
    assert any("solicito asesoría en baterías" in t for t in textos)
    # La bandeja sí lo ve: el relay al CRM va crudo.
    assert any("C7K2QX" in r.body.decode() for r in ctx.store.relays.values())


async def test_codigo_se_captura_aunque_nea_este_en_modo_prueba(respx_mock):
    ctx = _ctx_con_eventos(allowed_wa_ids="573999999999")  # el lead NO está en la lista
    routes = mock_crm_basics(respx_mock)
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(200))

    await _post(ctx, wa_body(text=TEXTO_CON_CODIGO))

    assert routes["messages"].call_count == 0, "en modo prueba no le contesta"
    assert hook.call_count == 1
    assert json.loads(hook.calls[0].request.content)["codigo"] == "C7K2QX"


async def test_mensaje_sin_codigo_no_manda_evento(respx_mock):
    ctx = _ctx_con_eventos()
    mock_crm_basics(respx_mock)
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(200))
    ctx.llm.replies = [LlmReply(content="¡Hola!")]

    await _post(ctx, wa_body(text="hola, necesito una batería"))

    assert hook.call_count == 0


def test_detectar_y_quitar_codigo():
    assert eventos.detectar_codigo(TEXTO_CON_CODIGO) == "C7K2QX"
    assert eventos.detectar_codigo("hola ref c7k2qx") == "C7K2QX"
    assert eventos.detectar_codigo("Ref: AB12CD") is None  # 1 no está en el alfabeto
    assert eventos.detectar_codigo("Ref. C7K2QXZ") is None  # 7 caracteres
    assert eventos.detectar_codigo(None) is None
    assert eventos.quitar_codigo(TEXTO_CON_CODIGO) == "Hola, solicito asesoría en baterías"
    assert eventos.quitar_codigo("(Ref. C7K2QX)") == "(Ref. C7K2QX)"  # no deja el texto vacío


# ------------------------------------------------------------ cotización ---

COTIZACION = (
    "Para tu Renault Logan te sirven estas 🔋\n\n"
    "*MAC 42I*\n⭐ Garantía 12 meses\n💰 $385.000 · $345.000 si nos entregas la vieja\n\n"
    "✅ Instalación a domicilio incluida, no pagas nada aparte.\n"
    "¿Te la instalamos hoy? Dime en qué zona estás y coordinamos 🙌"
)
CONFIRMACION = (
    "Te confirmo entonces:\n🔋 MAC 42I para tu Renault Logan\n📍 Cl 1 # 2-3, Suba\n"
    "💰 $345.000 — Nequi, instalación incluida\nEntregas la batería usada\n\n"
    "¿Todo bien así? Confirmo y sale el domiciliario."
)


def test_es_cotizacion_reconoce_solo_el_formato_fijo():
    assert eventos.es_cotizacion(COTIZACION)
    assert not eventos.es_cotizacion(CONFIRMACION), "la confirmación no es cotización"
    assert not eventos.es_cotizacion("La visita técnica cuesta $70.000, ¿cuál prefieres?")
    assert not eventos.es_cotizacion("La garantía es de 12 meses.")
    assert not eventos.es_cotizacion(None)


def _cotizados(hook) -> list[dict]:
    out = []
    for c in hook.calls:
        b = json.loads(c.request.content)
        if b.get("evento") == "cotizado":
            out.append(b)
    return out


async def test_cotizacion_enviada_emite_evento_cotizado(respx_mock):
    ctx = _ctx_con_eventos()
    routes = mock_crm_basics(respx_mock)
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(200))
    ctx.llm.replies = [LlmReply(content=COTIZACION)]

    await _post(ctx, wa_body(text="tengo un Logan"))

    assert routes["messages"].call_count == 1
    evs = _cotizados(hook)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["identidad"] == IDENTITY
    assert ev["crm_conversation_id"] == CRM_CONV_ID
    assert ev["crm_contact_id"] == "ct_1"
    assert ev["etapa_al_cotizar"] == "Nuevo"
    urls = [str(c.request.url) for c in respx_mock.calls]
    assert urls.index("http://crm.test/api/bot/messages") < urls.index(HOOK_URL), (
        "primero sale la cotización, después el evento"
    )
    assert not any(u.endswith("/api/bot/stage") for u in urls), "Nea no mueve la tarjeta"


async def test_respuesta_normal_no_emite_cotizado(respx_mock):
    ctx = _ctx_con_eventos()
    mock_crm_basics(respx_mock)
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(200))
    ctx.llm.replies = [LlmReply(content="¡Hola! ¿Qué carro tienes?")]

    await _post(ctx, wa_body(text="hola"))

    assert _cotizados(hook) == []


async def test_sin_url_no_emite_cotizado(respx_mock):
    ctx = make_ctx()  # sin EVENTOS_WEBHOOK_URL: como la imagen oficial
    routes = mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content=COTIZACION)]

    await _post(ctx, wa_body(text="tengo un Logan"))

    assert routes["messages"].call_count == 1
    assert not any("n8n" in str(c.request.url) for c in respx_mock.calls)


async def test_webhook_caido_no_afecta_la_cotizacion(respx_mock, monkeypatch):
    monkeypatch.setattr(eventos, "ESPERA_BASE", 0.0)
    ctx = _ctx_con_eventos()
    routes = mock_crm_basics(respx_mock)
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(502))
    ctx.llm.replies = [LlmReply(content=COTIZACION)]

    await _post(ctx, wa_body(text="tengo un Logan"))

    assert routes["messages"].call_count == 1, "la cotización salió igual"
    assert hook.call_count == eventos.INTENTOS


# ------------------------------------------------------------- respondio ---


def _eventos(hook, nombre: str) -> list[dict]:
    return [
        json.loads(c.request.content)
        for c in hook.calls
        if json.loads(c.request.content).get("evento") == nombre
    ]


async def _diana_ya_hablo(ctx) -> None:
    """Simula que Diana ya saludó en esta conversación (historial de Nea)."""
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.add_message(conv.id, "user", "hola")
    await ctx.store.add_message(conv.id, "assistant", "¡Hola! Soy Diana. ¿Qué carro tienes?")


async def test_hola_suelto_no_es_respuesta(respx_mock):
    ctx = _ctx_con_eventos()
    mock_crm_basics(respx_mock)
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(200))
    ctx.llm.replies = [LlmReply(content="¡Hola! Soy Diana. ¿Qué carro tienes?")]

    await _post(ctx, wa_body(text="hola"))

    assert _eventos(hook, "respondio") == [], "el primer mensaje no es respuesta"


async def test_responderle_a_diana_en_nuevo_emite_respondio(respx_mock):
    ctx = _ctx_con_eventos()
    mock_crm_basics(respx_mock)  # lead en «Nuevo»
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(200))
    await _diana_ya_hablo(ctx)
    ctx.llm.replies = [LlmReply(content="¿Es Logan normal o Logan con aire?")]

    await _post(ctx, wa_body(text="tengo un Logan", wamid="wamid.r2"))

    evs = _eventos(hook, "respondio")
    assert len(evs) == 1
    assert evs[0]["identidad"] == IDENTITY
    assert evs[0]["crm_conversation_id"] == CRM_CONV_ID
    assert evs[0]["etapa_al_responder"] == "Nuevo"
    assert _eventos(hook, "cotizado") == []


async def test_fuera_de_nuevo_no_emite_respondio(respx_mock):
    ctx = _ctx_con_eventos()
    mock_crm_basics(respx_mock)
    respx_mock.get("http://crm.test/api/bot/context").mock(
        return_value=httpx.Response(
            200,
            json={
                **crm_context(),
                "lead": {"id": "ld_1", "stageName": "En conversación"},
            },
        )
    )
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(200))
    await _diana_ya_hablo(ctx)
    ctx.llm.replies = [LlmReply(content="¿Es Logan normal o Logan con aire?")]

    await _post(ctx, wa_body(text="tengo un Logan", wamid="wamid.r3"))

    assert _eventos(hook, "respondio") == []


async def test_respuesta_con_cotizacion_manda_solo_cotizado(respx_mock):
    ctx = _ctx_con_eventos()
    mock_crm_basics(respx_mock)
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(200))
    await _diana_ya_hablo(ctx)
    ctx.llm.replies = [LlmReply(content=COTIZACION)]

    await _post(ctx, wa_body(text="tengo un Logan", wamid="wamid.r4"))

    assert len(_eventos(hook, "cotizado")) == 1
    assert _eventos(hook, "respondio") == [], "un solo evento por turno"


async def test_reset_no_cuenta_como_respuesta(respx_mock):
    ctx = _ctx_con_eventos(tester_wa_ids=IDENTITY)
    mock_crm_basics(respx_mock)
    respx_mock.post("http://crm.test/api/bot/reset").mock(return_value=httpx.Response(200, json={"ok": True}))
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(200))
    await _diana_ya_hablo(ctx)

    await _post(ctx, wa_body(text="/reset", wamid="wamid.r5"))

    assert _eventos(hook, "respondio") == []
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    historial = await ctx.store.recent_messages(conv.id, 10)
    assert not any(m.role == "assistant" for m in historial), (
        "el reset borra el historial: el conteo empieza de cero"
    )


def test_respuesta_en_etapa_inicial_sin_tildes_ni_mayusculas():
    class M:
        def __init__(self, role):
            self.role = role

    ctx_nuevo = {"lead": {"stageName": " nuevo "}}
    assert eventos.es_respuesta_en_etapa_inicial([M("user"), M("assistant")], ctx_nuevo)
    assert not eventos.es_respuesta_en_etapa_inicial([M("user")], ctx_nuevo)
    assert not eventos.es_respuesta_en_etapa_inicial([M("assistant")], {"lead": {"stageName": "Interesado"}})
    assert not eventos.es_respuesta_en_etapa_inicial([M("assistant")], None)


# --- en_espera: el cliente escribe mientras espera al asesor ----------------

HANDOFF_AT = "2026-10-23T16:02:00.000Z"


def _contexto_escalado(handoff_at=HANDOFF_AT, window_open=True):
    contexto = crm_context(ai_enabled=False, window_open=window_open)
    contexto["conversation"]["handoffAt"] = handoff_at
    return contexto


def _mock_escalado(respx_mock, **kwargs):
    routes = mock_crm_basics(respx_mock)
    routes["context"].mock(return_value=httpx.Response(200, json=_contexto_escalado(**kwargs)))
    return routes


async def test_cliente_en_espera_emite_un_evento_y_no_contesta(respx_mock):
    eventos._espera_avisada.clear()
    ctx = _ctx_con_eventos()
    routes = _mock_escalado(respx_mock)
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(200))

    await _post(ctx, wa_body(text="no me han escrito", wamid="wamid.e1"))

    assert routes["messages"].call_count == 0, "el bot no escribe con la IA en pausa"
    [evento] = _eventos(hook, "en_espera")
    assert evento["crm_conversation_id"] == CRM_CONV_ID
    assert evento["handoff_at"] == HANDOFF_AT
    assert evento["identidad"] == IDENTITY
    assert evento["texto"] == "no me han escrito"


async def test_en_espera_sale_una_sola_vez_por_handoff(respx_mock):
    eventos._espera_avisada.clear()
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(200))
    _mock_escalado(respx_mock)

    await _post(_ctx_con_eventos(), wa_body(text="hola?", wamid="wamid.e2"))
    await _post(_ctx_con_eventos(), wa_body(text="tu puedes validar?", wamid="wamid.e3"))
    assert len(_eventos(hook, "en_espera")) == 1

    # Un handoff nuevo en la misma conversación vuelve a habilitar el aviso.
    _mock_escalado(respx_mock, handoff_at="2026-10-24T09:00:00.000Z")
    await _post(_ctx_con_eventos(), wa_body(text="otra vez yo", wamid="wamid.e4"))
    assert len(_eventos(hook, "en_espera")) == 2


async def test_pausa_manual_sin_handoff_no_emite_en_espera(respx_mock):
    eventos._espera_avisada.clear()
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(200))
    _mock_escalado(respx_mock, handoff_at=None)

    await _post(_ctx_con_eventos(), wa_body(text="hola", wamid="wamid.e5"))
    assert _eventos(hook, "en_espera") == []


async def test_ventana_cerrada_no_emite_en_espera(respx_mock):
    eventos._espera_avisada.clear()
    hook = respx_mock.post(HOOK_URL).mock(return_value=httpx.Response(200))
    _mock_escalado(respx_mock, window_open=False)

    await _post(_ctx_con_eventos(), wa_body(text="hola", wamid="wamid.e6"))
    assert _eventos(hook, "en_espera") == []


async def test_sin_url_no_emite_en_espera(respx_mock):
    eventos._espera_avisada.clear()
    ctx = make_ctx(make_settings())
    routes = _mock_escalado(respx_mock)

    await _post(ctx, wa_body(text="hola", wamid="wamid.e7"))
    assert routes["messages"].call_count == 0
    assert not any("n8n.test" in str(c.request.url) for c in respx_mock.calls)
