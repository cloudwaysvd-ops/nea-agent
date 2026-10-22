"""Fork: los 3 toques de seguimiento después de una cotización (app/toques.py).

Lo que importa: salen solo si Diana cotizó, con el texto fijo, a los tiempos
contados desde el último mensaje del cliente, uno por toque y nunca un cuarto;
y se cortan si el cliente escribe, si la tarjeta ya está en Cliente/Perdido o
si la ventana de Meta se cerró.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx

from app import toques
from app.followup import FollowupWorker
from app.llm import LlmReply
from app.main import create_app
from app.state import utcnow
from tests.conftest import (
    CRM_CONV_ID,
    CRM_URL,
    IDENTITY,
    crm_context,
    make_ctx,
    make_settings,
    mock_crm_basics,
    wa_body,
)

COTIZACION = (
    "Para tu Renault Logan te sirven estas 🔋\n\n"
    "*MAC 42I*\n⭐ Garantía 12 meses\n💰 $385.000 · $345.000 si nos entregas la vieja\n\n"
    "¿Te la instalamos hoy? Dime en qué zona estás y coordinamos 🙌"
)


def _ctx(**overrides):
    return make_ctx(make_settings(toques_activos=True, **overrides))


async def _post(ctx, body: bytes) -> None:
    app = create_app(ctx=ctx)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as c:
        resp = await c.post("/webhook", content=body)
        assert resp.status_code == 200
        await asyncio.sleep(0.3)


def _conv(ctx):
    return next(iter(ctx.store.conversations.values()))


def _enviados(route) -> list[str]:
    return [json.loads(c.request.content)["text"] for c in route.calls]


async def _cotizada(ctx, *, hace_minutos: float, urgente: bool = False):
    """Una conversación en la que Diana ya cotizó y el cliente se calló."""
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    desde = utcnow() - timedelta(minutes=hace_minutos)
    await ctx.store.add_message(conv.id, "user", "tengo un Logan")
    await ctx.store.add_message(conv.id, "assistant", COTIZACION)
    await ctx.store.update_conversation(
        conv.id,
        crm_conversation_id=CRM_CONV_ID,
        greeted=True,
        last_inbound_at=desde,
        cotizada=True,
        urgente=urgente,
        toque=0,
        toques_desde=desde,
        followup_due_at=toques.vence(ctx.settings, desde, 0, urgente),
    )
    return conv, desde


# ------------------------------------------------------- final del turno ---


async def test_cotizar_programa_el_toque_1_a_los_20_minutos(respx_mock):
    ctx = _ctx()
    mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content=COTIZACION)]

    antes = utcnow()
    await _post(ctx, wa_body(text="tengo un Logan 2015"))
    await ctx.crm.aclose()

    conv = _conv(ctx)
    assert conv.cotizada and not conv.urgente and conv.toque == 0
    assert conv.toques_desde >= antes
    assert conv.followup_due_at == conv.toques_desde + timedelta(minutes=20)


async def test_carro_varado_adelanta_el_toque_1_a_10_minutos(respx_mock):
    ctx = _ctx()
    mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content=COTIZACION)]

    await _post(ctx, wa_body(text="mi Logan no arranca, estoy varado en Suba"))
    await ctx.crm.aclose()

    conv = _conv(ctx)
    assert conv.urgente
    assert conv.followup_due_at == conv.toques_desde + timedelta(minutes=10)


async def test_sin_cotizacion_queda_el_empujon_unico_de_4_horas(respx_mock):
    ctx = _ctx()
    mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content="¡Hola! ¿Qué carro tienes?")]

    antes = utcnow()
    await _post(ctx, wa_body(text="hola"))
    await ctx.crm.aclose()

    conv = _conv(ctx)
    assert not conv.cotizada and conv.toques_desde is None
    assert conv.followup_due_at >= antes + timedelta(hours=4)


async def test_apagados_por_defecto_cotizar_deja_el_empujon_de_siempre(respx_mock):
    ctx = make_ctx(make_settings())
    mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content=COTIZACION)]

    antes = utcnow()
    await _post(ctx, wa_body(text="tengo un Logan"))
    await ctx.crm.aclose()

    conv = _conv(ctx)
    assert not conv.cotizada
    assert conv.followup_due_at >= antes + timedelta(hours=4)


def test_ya_cotizada_vuelve_a_contar_desde_el_nuevo_mensaje():
    settings = make_settings(toques_activos=True)

    class Conv:
        cotizada = True
        urgente = False

    ahora = utcnow()
    out = toques.al_cerrar_turno(
        settings,
        Conv(),
        texto_enviado="Claro, ¿en qué zona estás?",
        texto_cliente="y cuánto se demora?",
        cliente_escribio_en=ahora,
    )
    assert out["toque"] == 0 and out["toques_desde"] == ahora
    assert out["followup_due_at"] == ahora + timedelta(minutes=20)
    assert out["followup_sent"] is False


def test_es_urgencia():
    for t in [
        "el carro no arranca",
        "No me prende el Spark",
        "estoy varada en la 80",
        "me dejó tirado",
        "se me descargó la batería",
        "Quedé botado en la autopista",
    ]:
        assert toques.es_urgencia(t), t
    for t in ["hola", "cuánto vale la del Logan", "la batería se descarga rápido?", None]:
        assert not toques.es_urgencia(t), t


# ------------------------------------------------------------ el barrido ---


async def test_los_tres_toques_salen_en_orden_y_nunca_un_cuarto(respx_mock):
    ctx = _ctx()
    conv, desde = await _cotizada(ctx, hace_minutos=21)
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=crm_context())
    )
    messages = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m1"})
    )
    worker = FollowupWorker(ctx)

    await worker.tick()
    assert _enviados(messages) == [ctx.settings.toque_1_texto]
    assert conv.toque == 1
    assert conv.followup_due_at == desde + timedelta(hours=4)

    await worker.tick()  # todavía no son las 4 h
    assert messages.call_count == 1

    await worker.tick(now=desde + timedelta(hours=4, minutes=1))
    await worker.tick(now=desde + timedelta(hours=23, minutes=1))
    assert _enviados(messages) == [
        ctx.settings.toque_1_texto,
        ctx.settings.toque_2_texto,
        ctx.settings.toque_3_texto,
    ]
    assert conv.toque == 3 and conv.followup_due_at is None

    await worker.tick(now=desde + timedelta(days=2))
    assert messages.call_count == 3
    # Quedan en el historial: si el cliente responde «sí», Diana sabe dónde iba.
    historial = await ctx.store.recent_messages(conv.id, 10)
    assert historial[-1].content == ctx.settings.toque_3_texto
    await ctx.crm.aclose()


async def test_ningun_toque_si_el_cliente_escribio_mientras_tanto(respx_mock):
    ctx = _ctx()
    conv, _ = await _cotizada(ctx, hace_minutos=25)
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=crm_context())
    )
    messages = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m1"})
    )
    original = ctx.store.claim_toque

    async def claim_y_el_cliente_escribe(*args):
        ganado = await original(*args)
        conv.last_inbound_at = utcnow()  # llegó su mensaje justo antes del candado
        return ganado

    ctx.store.claim_toque = claim_y_el_cliente_escribe
    await FollowupWorker(ctx).tick()
    assert messages.call_count == 0
    await ctx.crm.aclose()


async def test_tarjeta_en_cliente_corta_los_toques(respx_mock):
    ctx = _ctx()
    conv, _ = await _cotizada(ctx, hace_minutos=21)
    contexto = crm_context()
    contexto["lead"]["stageName"] = "Cliente"
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=contexto)
    )
    messages = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m1"})
    )
    await FollowupWorker(ctx).tick()
    assert messages.call_count == 0
    assert conv.followup_due_at is None, "no se programa el siguiente"
    await ctx.crm.aclose()


async def test_ventana_cerrada_corta_los_toques(respx_mock):
    ctx = _ctx()
    conv, _ = await _cotizada(ctx, hace_minutos=21)
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=crm_context(window_open=False))
    )
    messages = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m1"})
    )
    await FollowupWorker(ctx).tick()
    assert messages.call_count == 0
    assert conv.followup_due_at is None
    await ctx.crm.aclose()


async def test_conversacion_escalada_no_recibe_toques(respx_mock):
    ctx = _ctx()
    conv, _ = await _cotizada(ctx, hace_minutos=21)
    await ctx.store.update_conversation(conv.id, phase="cerrada")
    messages = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m1"})
    )
    await FollowupWorker(ctx).tick()
    assert messages.call_count == 0
    await ctx.crm.aclose()


async def test_reset_borra_el_estado_de_los_toques():
    ctx = _ctx()
    conv, _ = await _cotizada(ctx, hace_minutos=5, urgente=True)
    await ctx.store.reset_conversation(conv.id)
    assert (conv.cotizada, conv.urgente, conv.toque, conv.toques_desde) == (
        False,
        False,
        0,
        None,
    )
