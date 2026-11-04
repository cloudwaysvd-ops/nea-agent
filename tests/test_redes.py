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


# --- Cotización contaminada (banco, 4 nov 2026: Kia Picanto con líneas del Soluto) ---
from app.redes import cotizacion_ajena  # noqa: E402

KB = """## Kia Picanto
- Batería para Kia Picanto: **Alphaline NS40-540** (medida NS40) — garantía 15 meses — $320.000 · $280.000 entregando la usada
- Batería para Kia Picanto: **Mac Silver Plus NS40-700** (medida NS40) — garantía 15 meses — $360.000 · $320.000 entregando la usada
- Batería para Kia Picanto: **Bosch NS40-700** (medida NS40) — garantía 12 meses — $430.000 · $390.000 entregando la usada

## Kia Soluto
- Batería para Kia Soluto: **Alphaline 36-750** (medida 36) — garantía 15 meses — $340.000 · $300.000 entregando la usada
- Batería para Kia Soluto: **Moura 36-950** (medida 36) — garantía 24 meses — $620.000 · $580.000 entregando la usada

## Baterías para carros start-stop
- Batería para carro start-stop: **Bosch EFB 70-80 Ah** (medida LN3 o LN4) — garantía 18 meses — $990.000 · $950.000 entregando la usada
"""


def _cotizacion(carro: str, *opciones: tuple[str, int, str, str]) -> str:
    cuerpo = "\n\n".join(
        f"*{r}*\n⭐ Garantía {g} meses\n💰 ${p} · ${u} si nos entregas la vieja" for r, g, p, u in opciones
    )
    return f"Para tu {carro} te sirven estas 🔋\n\n{cuerpo}\n\n✅ Incluye instalación a domicilio."


def test_cotizacion_copiada_de_su_ficha_pasa():
    texto = _cotizacion(
        "Kia Picanto",
        ("Alphaline NS40-540", 15, "320.000", "280.000"),
        ("Bosch NS40-700", 12, "430.000", "390.000"),
    )
    assert cotizacion_ajena(texto, KB) is None


def test_referencia_inventada_no_pasa():
    texto = _cotizacion(
        "Kia Picanto",
        ("Alphaline NS40-540", 15, "320.000", "280.000"),
        ("Moura NS40-950", 24, "620.000", "580.000"),
    )
    assert "Moura NS40-950".lower() in cotizacion_ajena(texto, KB).lower()


def test_precio_de_la_ficha_vecina_no_pasa():
    texto = _cotizacion("Kia Picanto", ("Alphaline NS40-540", 15, "340.000", "300.000"))
    assert cotizacion_ajena(texto, KB) is not None


def test_ficha_de_otro_carro_no_pasa():
    texto = _cotizacion("Kia Picanto", ("Moura 36-950", 24, "620.000", "580.000"))
    assert "Kia Soluto" in cotizacion_ajena(texto, KB)


def test_ficha_generica_start_stop_pasa_para_cualquier_carro():
    texto = _cotizacion("Ford Bronco", ("Bosch EFB 70-80 Ah", 18, "990.000", "950.000"))
    assert cotizacion_ajena(texto, KB) is None


def test_mensaje_sin_cotizacion_o_sin_catalogo_no_se_revisa():
    assert cotizacion_ajena("¿Tu Spark es GT o normal?", KB) is None
    texto = _cotizacion("Kia Picanto", ("Moura NS40-950", 24, "620.000", "580.000"))
    assert cotizacion_ajena(texto, None) is None


class _PerfilFijo:
    def __init__(self, kb: str) -> None:
        from app.profile import BusinessProfile

        self._perfil = BusinessProfile(agent_name="Diana", kb_text=kb)

    async def get(self):
        return self._perfil


CONTAMINADA = _cotizacion(
    "Kia Picanto",
    ("Alphaline NS40-540", 15, "340.000", "300.000"),
    ("Moura NS40-950", 24, "620.000", "580.000"),
)
LIMPIA = _cotizacion(
    "Kia Picanto",
    ("Alphaline NS40-540", 15, "320.000", "280.000"),
    ("Bosch NS40-700", 12, "430.000", "390.000"),
)


async def test_turno_rehace_la_cotizacion_contaminada(ctx, client, respx_mock):
    routes = mock_crm_basics(respx_mock)
    ctx.profile = _PerfilFijo(KB)
    ctx.llm.replies = [LlmReply(content=CONTAMINADA), LlmReply(content=LIMPIA)]
    await client.post("/webhook", content=wa_body(text="batería para kia picanto"))
    await asyncio.sleep(0.3)
    assert routes["messages"].call_count == 1
    enviado = json.loads(routes["messages"].calls[0].request.content)["text"]
    assert "$320.000" in enviado and "Moura" not in enviado
    assert routes["handoff"].call_count == 0


async def test_turno_con_dos_cotizaciones_contaminadas_escala_sin_enviar(ctx, client, respx_mock):
    routes = mock_crm_basics(respx_mock)
    ctx.profile = _PerfilFijo(KB)
    ctx.llm.replies = [LlmReply(content=CONTAMINADA), LlmReply(content=CONTAMINADA)]
    await client.post("/webhook", content=wa_body(text="batería para kia picanto"))
    await asyncio.sleep(0.3)
    assert routes["messages"].call_count == 0
    assert routes["handoff"].call_count == 1


def test_ficha_de_camion_liviano_no_cubre_un_carro_sin_ficha():
    kb = KB + (
        "\n- Batería para camión liviano: **Willard Extrema Titanio 27-1000** (medida 27) — "
        "garantía 12 meses — $540.000 · $500.000 entregando la usada\n"
    )
    carro = _cotizacion("SsangYong Rexton", ("Willard Extrema Titanio 27-1000", 12, "540.000", "500.000"))
    camion = _cotizacion("camión liviano", ("Willard Extrema Titanio 27-1000", 12, "540.000", "500.000"))
    assert cotizacion_ajena(carro, kb) is not None
    assert cotizacion_ajena(camion, kb) is None
