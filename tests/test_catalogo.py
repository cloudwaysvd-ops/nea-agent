"""Fork: catálogo de fichas en una hoja + herramienta buscar_ficha (app/catalogo.py)."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.catalogo import TOOL_SCHEMA, CatalogoProvider, desde_csv
from app.llm import LlmReply, ToolCall
from app.profile import BusinessProfile
from app.prompt import build_system_prompt
from app.redes import _LINEA_FICHA, cotizacion_ajena
from app.state import Conversation
from app.tools import TOOL_SCHEMAS, ToolRuntime
from app.turn import _herramientas, _kb_para_precios
from tests.conftest import CRM_CONV_ID, IDENTITY, make_ctx, mock_crm_basics, wa_body

HOJA = """ficha,marca,se_escribe_tambien,nota,modelo,medida,referencia,garantia_meses,precio,precio_con_usada,no_se_cotiza
Kia Picanto,Kia,"kia picanto, picanto",,,NS40,Alphaline NS40-540,15,320.000,280.000,
Kia Picanto,,,,,NS40,Bosch NS40-700,12,$430.000,"$ 390,000",
Kia Soluto,Kia,"kia soluto, soluto",,,36,Alphaline 36-750,15,340000,300000,
Kia Soluto,,,,,36,Moura 36-950,24,620.000,580.000,
Chevrolet Spark,Chevrolet,"chevrolet spark, spark",Pregunta si es Spark normal o Spark GT.,,NS40,Alphaline NS40-540,15,320.000,280.000,
Chevrolet Spark GT,Chevrolet,"chevrolet spark gt, spark gt",,,36,Alphaline 36-750,15,340.000,300.000,
"Chevrolet Onix (aspirado, no turbo)",Chevrolet,"onix, chevrolet onix (aspirado, no turbo)","Pregunta si es turbo.
Willard no se ofrece para este carro.",,36,Mac Silver Plus 36-800,15,440.000,400.000,
Mazda 3,Mazda,"3, mazda 3",,,35,Alphaline 35-950,15,500.000,450.000,
Bmw Series,Bmw,"bmw series, bmw",es start-stop.,,LN3 o LN4,Bosch EFB 70-80 Ah,18,990.000,950.000,
Hyundai New Tucson,Hyundai,"hyundai new tucson, new tucson",,,42,Bosch S4 42-970,12,540.000,500.000,
Ford Escape,Ford,"escape, ford escape",Pregunta si es híbrido o 2018 en adelante.,Ford Escape 2018 en adelante o híbrido,LN3 o LN4,Bosch EFB 70-80 Ah,18,990.000,950.000,
Ford Escape,,,,,65,Varta 65,sin dato,660.000,620.000,sí
Ford Explorer,Ford,"ford explorer, explorer",2021 o anterior: escala.,,65 hasta 2021; start-stop desde 2022,,,,,
Baterías para carros start-stop,,"start stop, start-stop, efb, agm",Para cualquier carro start-stop.,carro start-stop,LN3 o LN4,Bosch EFB 70-80 Ah,18,990.000,950.000,
"""


@pytest.fixture
def cat():
    return desde_csv(HOJA)


# --- La hoja ----------------------------------------------------------------


def test_lee_la_hoja_y_normaliza_precios(cat):
    assert cat.problemas == []
    picanto = next(f for f in cat.fichas if f.nombre == "Kia Picanto")
    assert [(o.referencia, o.precio, o.usada) for o in picanto.opciones] == [
        ("Alphaline NS40-540", "320.000", "280.000"),
        ("Bosch NS40-700", "430.000", "390.000"),
    ]
    soluto = next(f for f in cat.fichas if f.nombre == "Kia Soluto")
    assert soluto.opciones[0].precio == "340.000"


def test_las_lineas_tienen_el_texto_de_siempre(cat):
    """La red de precios y el formato de cotización leen exactamente este texto."""
    linea = cat.fichas[0].opciones[0].linea()
    assert linea == (
        "- Batería para Kia Picanto: **Alphaline NS40-540** (medida NS40) — garantía 15 meses"
        " — $320.000 · $280.000 entregando la usada"
    )
    assert _LINEA_FICHA.search(linea)


def test_no_se_cotiza_y_garantia_sin_dato(cat):
    escape = next(f for f in cat.fichas if f.nombre == "Ford Escape")
    varta = escape.opciones[1]
    assert varta.no_se_cotiza and varta.garantia == "sin dato"
    assert "garantía sin dato — no cotizar, escalar — $660.000" in varta.linea()
    assert escape.opciones[0].modelo == "Ford Escape 2018 en adelante o híbrido"


def test_coma_entre_parentesis_y_notas_por_renglon(cat):
    onix = next(f for f in cat.fichas if f.nombre.startswith("Chevrolet Onix"))
    assert onix.alias == ["onix", "chevrolet onix (aspirado, no turbo)"]
    assert onix.notas == ["Pregunta si es turbo.", "Willard no se ofrece para este carro."]


def test_fila_solo_de_nota(cat):
    explorer = next(f for f in cat.fichas if f.nombre == "Ford Explorer")
    assert explorer.opciones == []
    assert "MEDIDA: 65 hasta 2021; start-stop desde 2022" in explorer.texto()
    assert "⚠️ Ford Explorer: 2021 o anterior: escala." in explorer.texto()


def test_fila_mala_se_salta_y_se_reporta():
    hoja = HOJA + "Kia Picanto,,,,,NS40,Mac NS40-700,15,,320.000,\n"
    cat = desde_csv(hoja)
    picanto = next(f for f in cat.fichas if f.nombre == "Kia Picanto")
    assert [o.referencia for o in picanto.opciones] == ["Alphaline NS40-540", "Bosch NS40-700"]
    assert len(cat.problemas) == 1 and "Mac NS40-700" in cat.problemas[0] and "precio" in cat.problemas[0]


def test_columnas_en_otro_orden_y_con_tildes():
    hoja = (
        "Referencia,Ficha,Precio,Precio con usada,Garantía,Medida\n"
        "Alphaline NS40-540,Kia Picanto,320000,280000,15,NS40\n"
    )
    cat = desde_csv(hoja)
    assert cat.problemas == []
    assert cat.fichas[0].opciones[0].precio == "320.000"


def test_hoja_sin_columnas_obligatorias():
    assert "faltan columnas" in desde_csv("ficha,medida\nKia Picanto,NS40\n").problemas[0]


# --- Buscar ------------------------------------------------------------------


@pytest.mark.parametrize(
    "consulta, esperadas",
    [
        ("Kia Picanto", ["Kia Picanto"]),
        ("picanto 2015", ["Kia Picanto"]),
        ("tengo un spark", ["Chevrolet Spark"]),
        ("Spark GT 2012", ["Chevrolet Spark GT"]),  # el más largo gana
        ("chevrolet onix turbo", ["Chevrolet Onix (aspirado, no turbo)"]),
        ("hyundai tucson", ["Hyundai New Tucson"]),  # sin marca, dentro del nombre
        ("BMW serie 3", ["Bmw Series"]),  # «3» solo no pesca el Mazda 3
        ("mazda 3", ["Mazda 3"]),
        ("Ford Escape híbrido 2020", ["Ford Escape"]),
        ("carro start-stop", ["Baterías para carros start-stop"]),
        ("Suzuki Ertiga", []),
        ("Kia K3", []),
    ],
)
def test_buscar(cat, consulta, esperadas):
    assert [f.nombre for f in cat.buscar(consulta)] == esperadas


def test_consultar_devuelve_solo_la_ficha_del_carro(cat):
    r = cat.consultar("Kia Picanto")
    assert r["fichas"] == ["Kia Picanto"]
    assert "Batería para Kia Picanto: **Alphaline NS40-540**" in r["resultado"]
    assert "Soluto" not in r["resultado"]


def test_consultar_sin_ficha_manda_a_ruta_segura(cat):
    r = cat.consultar("Suzuki Ertiga")
    assert r["fichas"] == [] and "ruta segura" in r["resultado"]


def test_consultar_solo_la_marca_pide_el_modelo(cat):
    r = cat.consultar("Kia")
    assert r["fichas"] == [] and "Falta el modelo" in r["resultado"]
    assert "Kia Picanto" in r["resultado"] and "Kia Soluto" in r["resultado"]


# --- Proveedor: URL, respaldo y protecciones ---------------------------------


async def test_proveedor_lee_el_csv_local(tmp_path):
    archivo = tmp_path / "fichas.csv"
    archivo.write_text(HOJA, encoding="utf-8")
    cat = await CatalogoProvider(path=str(archivo)).get()
    assert cat is not None and cat.opciones == 13


URL = "https://docs.google.com/spreadsheets/d/e/x/pub?output=csv"


async def test_proveedor_baja_la_hoja_y_guarda_la_ultima_buena(respx_mock):
    ruta = respx_mock.get(URL).mock(return_value=httpx.Response(200, text=HOJA))
    prov = CatalogoProvider(url=URL, ttl=0)
    assert (await prov.get()).opciones == 13
    ruta.mock(return_value=httpx.Response(500))
    assert (await prov.get()).opciones == 13  # la hoja falló: sigue la anterior


async def test_proveedor_no_usa_una_hoja_casi_vacia(respx_mock):
    ruta = respx_mock.get(URL).mock(return_value=httpx.Response(200, text=HOJA))
    prov = CatalogoProvider(url=URL, ttl=0)
    await prov.get()
    recortada = "\n".join(HOJA.splitlines()[:3]) + "\n"  # alguien borró casi todo
    ruta.mock(return_value=httpx.Response(200, text=recortada))
    assert (await prov.get()).opciones == 13


async def test_proveedor_respeta_el_ttl(respx_mock):
    ruta = respx_mock.get(URL).mock(return_value=httpx.Response(200, text=HOJA))
    prov = CatalogoProvider(url=URL, ttl=300)
    await prov.get()
    await prov.get()
    assert ruta.call_count == 1


async def test_proveedor_sin_nada_devuelve_none(respx_mock):
    respx_mock.get(URL).mock(return_value=httpx.Response(404))
    assert await CatalogoProvider(url=URL).get() is None


# --- Integración con Nea ------------------------------------------------------


class _CatalogoFijo:
    def __init__(self, cat) -> None:
        self.cat = cat

    async def get(self):
        return self.cat


def test_la_herramienta_solo_existe_con_catalogo(cat):
    ctx = make_ctx()
    ctx.agenda_enabled = False
    assert "buscar_ficha" not in [t["function"]["name"] for t in _herramientas(ctx)]
    ctx.catalogo = _CatalogoFijo(cat)
    assert "buscar_ficha" in [t["function"]["name"] for t in _herramientas(ctx)]
    assert TOOL_SCHEMA not in TOOL_SCHEMAS  # la lista global no se tocó


async def test_runtime_ejecuta_buscar_ficha(cat):
    ctx = make_ctx()
    ctx.catalogo = _CatalogoFijo(cat)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    r = await ToolRuntime(ctx, conv, CRM_CONV_ID).execute("buscar_ficha", {"modelo": "kia picanto"})
    assert r["ok"] and r["fichas"] == ["Kia Picanto"]
    ctx.catalogo = _CatalogoFijo(None)
    r = await ToolRuntime(ctx, conv, CRM_CONV_ID).execute("buscar_ficha", {"modelo": "kia picanto"})
    assert r["ok"] is False and r["error"] == "catalogo_no_disponible"
    await ctx.crm.aclose()


def test_el_prompt_avisa_del_catalogo_solo_si_hay_hoja():
    perfil = BusinessProfile(agent_name="Diana", kb_text="Zonas…")
    conv = Conversation(id=1, wa_identity=IDENTITY, greeted=True)
    con = build_system_prompt(profile=perfil, context=None, conv=conv, agenda=False, catalogo=True)
    sin = build_system_prompt(profile=perfil, context=None, conv=conv, agenda=False)
    assert "buscar_ficha" in con and "buscar_ficha" not in sin
    # Va antes del CONTEXTO ACTUAL: la parte fija del prompt sigue fija (caché).
    assert con.index("buscar_ficha") < con.index("\nCONTEXTO ACTUAL:")


async def test_red_de_precios_valida_contra_la_hoja(cat):
    ctx = make_ctx()
    cotizacion = (
        "Para tu Kia Picanto te sirven estas 🔋\n\n*Alphaline NS40-540*\n⭐ Garantía 15 meses\n"
        "💰 $320.000 · $280.000 si nos entregas la vieja"
    )
    vecina = cotizacion.replace("$320.000 · $280.000", "$340.000 · $300.000")
    assert await _kb_para_precios(ctx, "Zonas…") == "Zonas…"  # sin hoja: como siempre
    ctx.catalogo = _CatalogoFijo(cat)
    kb = await _kb_para_precios(ctx, "Zonas…")
    assert cotizacion_ajena(cotizacion, kb) is None
    assert cotizacion_ajena(vecina, kb) is not None
    await ctx.crm.aclose()


async def test_turno_cotiza_con_la_ficha_de_la_hoja(ctx, client, respx_mock, cat):
    routes = mock_crm_basics(respx_mock)
    ctx.catalogo = _CatalogoFijo(cat)
    cotizacion = (
        "Para tu Kia Picanto te sirven estas 🔋\n\n*Alphaline NS40-540*\n⭐ Garantía 15 meses\n"
        "💰 $320.000 · $280.000 si nos entregas la vieja\n\n*Bosch NS40-700*\n⭐ Garantía 12 meses\n"
        "💰 $430.000 · $390.000 si nos entregas la vieja"
    )
    ctx.llm.replies = [
        LlmReply(content=None, tool_calls=[ToolCall(id="t1", name="buscar_ficha", arguments={"modelo": "Kia Picanto"})]),
        LlmReply(content=cotizacion),
    ]
    await client.post("/webhook", content=wa_body(text="batería para kia picanto"))
    await asyncio.sleep(0.3)
    assert routes["messages"].call_count == 1
    assert "$320.000" in json.loads(routes["messages"].calls[0].request.content)["text"]
    assert routes["handoff"].call_count == 0
    # La segunda llamada al modelo ya trae la ficha como resultado de la herramienta.
    tool_msg = [m for m in ctx.llm.calls[1]["messages"] if m.get("role") == "tool"][0]
    assert "Batería para Kia Picanto" in json.loads(tool_msg["content"])["resultado"]


async def test_turno_rechaza_precio_que_no_esta_en_la_hoja(ctx, client, respx_mock, cat):
    """El CRM ya no trae fichas: la red de precios igual frena una mezcla."""
    routes = mock_crm_basics(respx_mock)
    ctx.catalogo = _CatalogoFijo(cat)
    mala = (
        "Para tu Kia Picanto te sirven estas 🔋\n\n*Alphaline NS40-540*\n⭐ Garantía 15 meses\n"
        "💰 $340.000 · $300.000 si nos entregas la vieja"
    )
    ctx.llm.replies = [LlmReply(content=mala), LlmReply(content=mala)]
    await client.post("/webhook", content=wa_body(text="batería para kia picanto"))
    await asyncio.sleep(0.3)
    assert routes["messages"].call_count == 0
    assert routes["handoff"].call_count == 1


def test_la_cabecera_dice_el_carro_de_las_lineas(cat):
    """Como en el conocimiento: «MODELO: carro start-stop», no el título de la ficha."""
    ss = next(f for f in cat.fichas if f.nombre == "Baterías para carros start-stop")
    assert ss.texto().startswith("## Baterías para carros start-stop\n\nMODELO: carro start-stop · MEDIDA: LN3 o LN4")
    escape = next(f for f in cat.fichas if f.nombre == "Ford Escape")
    assert "MODELO: Ford Escape · MARCA: Ford" in escape.texto()  # líneas con modelos distintos


def test_medida_ficha_opcional_manda_en_la_cabecera():
    hoja = HOJA.splitlines()[0] + ",medida_ficha\n" + (
        "Nissan Versa,Nissan,versa,,,42,Mac Silver Plus 42-970,15,460.000,420.000,,42 (a veces 36)\n"
        "Nissan Versa,,,,,36,Mac Silver Plus 36-800,15,440.000,400.000,,\n"
    )
    versa = desde_csv(hoja).fichas[0]
    assert "MEDIDA: 42 (a veces 36)" in versa.texto()
    sin = desde_csv(HOJA).fichas[0]
    assert "MEDIDA: NS40" in sin.texto()  # vacía: se arma con las filas
