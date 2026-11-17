"""Fork: precio de una batería pedida por su nombre, antes de saber el carro.

Caso real (13-nov): «Batería sellada Bosch caja 42 MP 970CA 62Ah». El dueño
quiere que Diana dé primero el precio de esa batería y las otras marcas de la
caja, y después pregunte el carro. La herramienta buscar_bateria lee la misma
hoja del catálogo, así que el precio nunca se desactualiza.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.catalogo import TOOL_BATERIA, desde_csv
from app.llm import LlmReply, ToolCall
from app.redes import cotizacion_ajena
from app.tools import TOOL_SCHEMAS, ToolRuntime
from app.turn import _herramientas, _kb_para_precios
from tests.conftest import CRM_CONV_ID, IDENTITY, make_ctx, mock_crm_basics, wa_body

HOJA = """ficha,marca,se_escribe_tambien,nota,modelo,medida,referencia,garantia_meses,precio,precio_con_usada,no_se_cotiza
Renault Logan,Renault,"logan, renault logan",,,42,Willard Extrema Titanio 42-850,12,390.000,350.000,
Renault Logan,,,,,42,Mac Silver Plus 42-970,15,460.000,420.000,
Renault Logan,,,,,42,Bosch S4 42-970,12,540.000,500.000,
Renault Logan,,,,,42,Varta Dynamic 42-1250,15,570.000,530.000,
Renault Sandero,Renault,"sandero, renault sandero",,,42,Bosch S4 42-970,12,540.000,500.000,
Kia Picanto,Kia,"kia picanto, picanto",,,NS40,Alphaline NS40-540,15,320.000,280.000,
Kia Picanto,,,,,NS40,Willard Extrema Titanio NS40-560,12,340.000,300.000,
Chevrolet Aveo,Chevrolet,"aveo, chevrolet aveo",,,34,Willard Extrema Titanio 34-1000,12,490.000,450.000,
Chevrolet Aveo,,,,,34,Mac Silver Plus 34-1100,15,570.000,530.000,
Ford Escape,Ford,"escape, ford escape",,,65,Varta 65,sin dato,660.000,620.000,sí
"""


@pytest.fixture
def cat():
    return desde_csv(HOJA)


def _refs(opciones):
    return [o.referencia for o in opciones]


def test_el_caso_real_trae_la_bosch_y_las_otras_marcas_de_la_caja(cat):
    pedidas, misma = cat.buscar_bateria("Bateria Sellada Bosch Caja 42 Mp 970Ca 62Ah")
    assert _refs(pedidas) == ["Bosch S4 42-970"]
    assert _refs(misma) == [
        "Willard Extrema Titanio 42-850", "Mac Silver Plus 42-970", "Varta Dynamic 42-1250",
    ]


@pytest.mark.parametrize(
    "consulta, pedidas",
    [
        ("Mac 34", ["Mac Silver Plus 34-1100"]),
        ("bosch 42-970", ["Bosch S4 42-970"]),
        ("NS40", ["Alphaline NS40-540", "Willard Extrema Titanio NS40-560"]),
        ("una batería buena", []),
        ("Varta 65", []),  # la 65 no tiene garantía: no se ofrece
    ],
)
def test_buscar_bateria(cat, consulta, pedidas):
    assert _refs(cat.buscar_bateria(consulta)[0]) == pedidas


def test_una_referencia_que_esta_en_varios_carros_sale_una_vez(cat):
    pedidas, _ = cat.buscar_bateria("bosch caja 42")
    assert _refs(pedidas) == ["Bosch S4 42-970"]


def test_marca_que_no_tenemos_en_esa_caja_ofrece_la_caja(cat):
    r = cat.consultar_bateria("Moura 42")
    assert "no la tenemos en esa caja" in r["resultado"]
    assert "Willard Extrema Titanio 42-850 (caja 42): $390.000 · $350.000" in r["resultado"]


def test_marca_sola_en_varias_cajas_da_el_rango(cat):
    r = cat.consultar_bateria("bateria willard")
    assert "desde $340.000 hasta $490.000" in r["resultado"]
    assert "pregunta para qué carro" in r["resultado"]


def test_sin_reconocer_no_da_precio_de_memoria(cat):
    r = cat.consultar_bateria("la mejor batería")
    assert r["referencias"] == [] and "No des precio de memoria" in r["resultado"]


def test_la_respuesta_pide_el_orden_y_el_carro_al_final(cat):
    r = cat.consultar_bateria("bosch 42 970")
    texto = r["resultado"]
    assert texto.index("Bosch S4 42-970") < texto.index("Otras marcas")
    assert "$540.000 · $500.000 entregando la usada" in texto
    assert "pregunta para qué carro es" in texto


class _CatalogoFijo:
    def __init__(self, cat) -> None:
        self.cat = cat

    async def get(self):
        return self.cat


def test_la_herramienta_solo_existe_con_catalogo(cat):
    ctx = make_ctx()
    ctx.agenda_enabled = False
    assert "buscar_bateria" not in [t["function"]["name"] for t in _herramientas(ctx)]
    ctx.catalogo = _CatalogoFijo(cat)
    assert "buscar_bateria" in [t["function"]["name"] for t in _herramientas(ctx)]
    assert TOOL_BATERIA not in TOOL_SCHEMAS


async def test_runtime_ejecuta_buscar_bateria(cat):
    ctx = make_ctx()
    ctx.catalogo = _CatalogoFijo(cat)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    r = await ToolRuntime(ctx, conv, CRM_CONV_ID).execute("buscar_bateria", {"bateria": "Bosch caja 42"})
    assert r["ok"] and r["referencias"] == ["Bosch S4 42-970"]
    ctx.catalogo = _CatalogoFijo(None)
    r = await ToolRuntime(ctx, conv, CRM_CONV_ID).execute("buscar_bateria", {"bateria": "Bosch caja 42"})
    assert r["ok"] is False and r["error"] == "catalogo_no_disponible"
    await ctx.crm.aclose()


async def test_red_de_precios_deja_pasar_el_precio_sin_carro(cat):
    """Si Diana usa el formato de cotización sin nombrar carro, no es «la ficha de otro carro»."""
    ctx = make_ctx()
    ctx.catalogo = _CatalogoFijo(cat)
    kb = await _kb_para_precios(ctx, "Zonas…")
    sin_carro = "*Bosch S4 42-970*\n⭐ Garantía 12 meses\n💰 $540.000 · $500.000 entregando la usada"
    assert cotizacion_ajena(sin_carro, kb) is None
    # El precio igual tiene que ser el de la hoja.
    assert cotizacion_ajena(sin_carro.replace("$540.000", "$520.000"), kb) is not None
    # Y si nombra un carro, tiene que ser la ficha de ESE carro.
    otro = "Para tu Kia Picanto te sirven estas 🔋\n\n" + sin_carro
    assert cotizacion_ajena(otro, kb) is not None
    await ctx.crm.aclose()


async def test_turno_da_el_precio_y_pregunta_el_carro(ctx, client, respx_mock, cat):
    routes = mock_crm_basics(respx_mock)
    ctx.catalogo = _CatalogoFijo(cat)
    respuesta = (
        "La Bosch S4 42-970 vale $540.000, o $500.000 si nos entregas la vieja. En caja 42 "
        "también tenemos Willard ($390.000), Mac ($460.000) y Varta ($570.000). "
        "¿Para qué carro la necesitas? 🔋"
    )
    ctx.llm.replies = [
        LlmReply(content=None, tool_calls=[ToolCall(id="t1", name="buscar_bateria", arguments={"bateria": "Bosch caja 42 970"})]),
        LlmReply(content=respuesta),
    ]
    await client.post("/webhook", content=wa_body(text="Batería sellada Bosch caja 42 970CA"))
    await asyncio.sleep(0.3)
    assert routes["messages"].call_count == 1
    assert "$540.000" in json.loads(routes["messages"].calls[0].request.content)["text"]
    assert routes["handoff"].call_count == 0
    tool_msg = [m for m in ctx.llm.calls[1]["messages"] if m.get("role") == "tool"][0]
    assert "Bosch S4 42-970 (caja 42)" in json.loads(tool_msg["content"])["resultado"]
