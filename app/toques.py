"""Fork: los 3 toques de seguimiento después de una cotización.

Si Diana ya cotizó y el cliente se calla, salen hasta tres mensajes, contados
desde el último mensaje del cliente. Cada uno lo redacta la IA con el texto
del dueño como guía, ajustado a la conversación (el carro, la batería y los
precios que ya se cotizaron); si la IA falla, sale el texto del dueño tal cual
(`TOQUES_CON_IA=false` = siempre fijo):

- toque 1 a los `TOQUE_1_MINUTOS` (o `TOQUE_1_URGENTE_MINUTOS` si el carro
  está varado),
- toque 2 a las `TOQUE_2_HORAS`,
- toque 3 a las `TOQUE_3_HORAS` — antes de las 24 h, porque después Meta ya
  no deja escribirle sin plantilla.

También arrancan con el carro varado cuando Diana ofrece la visita técnica o
cotizar de una (ahí todavía no hay precios de batería, pero es el cliente más
caliente). Si el cliente escribe, la cuenta vuelve a empezar desde su mensaje.
Sin nada de eso todavía, el seguimiento es el empujón único de siempre
(app/followup.py). Lo programa el final del turno (`al_cerrar_turno`) y lo
envía el mismo barrido del seguimiento (`enviar`).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any

from app.crm import CrmConflict, CrmError
from app.eventos import es_cotizacion
from app.formato import a_whatsapp
from app.llm import LlmExhausted
from app.redes import limpiar_herramienta_filtrada, promete_aviso

logger = logging.getLogger("nea.toques")

TOTAL = 3

# Etapas del Pipeline en las que ya no se insiste: compró o se perdió.
ETAPAS_SIN_TOQUES = frozenset({"cliente", "perdido"})

# «Es urgencia» = el carro está varado. Lo decide el código, no el modelo.
_URGENCIA_RE = re.compile(
    r"no (me )?(arranca|prende|enciende|da arranque|quiere arrancar|quiere prender)"
    r"|varad[oa]"
    r"|(me )?(dejo|quede|quedo|esta) (tirad|botad)[oa]"
    r"|se (me )?descargo"
    r"|sin arranque",
)


def _norm(texto: str | None) -> str:
    t = unicodedata.normalize("NFD", texto or "")
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower()


def es_urgencia(texto: str | None) -> bool:
    return bool(_URGENCIA_RE.search(_norm(texto)))


# La respuesta al carro varado: visita técnica con su precio (y cotizar de una).
_VISITA_RE = re.compile(r"visita tecnica")
_PRECIO_RE = re.compile(r"\$\s*\d")


def es_oferta_de_visita(texto: str | None) -> bool:
    t = _norm(texto)
    return bool(_VISITA_RE.search(t) and _PRECIO_RE.search(t))


def vence(settings: Any, desde: datetime, paso: int, urgente: bool) -> datetime | None:
    """Cuándo sale el toque número `paso` (0 = el primero). None = ya no hay más."""
    if paso == 0:
        minutos = settings.toque_1_urgente_minutos if urgente else settings.toque_1_minutos
        return desde + timedelta(minutes=minutos)
    if paso == 1:
        return desde + timedelta(hours=settings.toque_2_horas)
    if paso == 2:
        return desde + timedelta(hours=settings.toque_3_horas)
    return None


def texto(settings: Any, paso: int) -> str:
    return (settings.toque_1_texto, settings.toque_2_texto, settings.toque_3_texto)[paso]


_ESPERA = ("unos minutos", "unas horas", "casi un día")

INSTRUCCION = (
    "El cliente lleva {espera} sin responder después de tu último mensaje. Escribe "
    "el SEGUIMIENTO {n} de 3: UN solo mensaje corto de WhatsApp.\n"
    "Guía del dueño:\n---\n{guia}\n---\n"
    "Reglas:\n"
    "- Parte de la guía y cambia lo MENOS posible: conserva sus frases, su tono "
    "y su largo. Habla como Bogotana (atendemos, te llevamos).\n"
    "- Antes de escribir, mira lo que el cliente YA respondió (marca, zona, "
    "dirección, forma de pago…). No se lo vuelvas a preguntar: cambia esa parte "
    "de la guía por lo siguiente que falta para cerrar el pedido.\n"
    "- Puedes nombrar el carro y la batería que cotizaste.\n"
    "- Precios, marcas y garantías: solo los que ya enviaste, copiados exactos. "
    "Nada nuevo: ni precios, ni descuentos, ni promesas. Si todavía no "
    "cotizaste baterías (por ejemplo, ofreciste la visita técnica), no nombres "
    "marcas ni precios de batería: ofrécele cotizarle de una o mandarle la visita.\n"
    "- No reenvíes la cotización completa ni hagas listas.\n"
    "- No digas que avisaste a un asesor.\n"
    "- Responde solo con el mensaje, sin comillas ni explicaciones."
)


async def redactar(ctx: Any, conv: Any, context: dict[str, Any], paso: int) -> str:
    """El texto del toque: el de la IA si sale bien; si no, el del dueño."""
    guia = texto(ctx.settings, paso).strip()
    if not getattr(ctx.settings, "toques_con_ia", True):
        return guia
    # Tarde, a propósito: app.turn importa este módulo.
    from app.profile import resolve_profile
    from app.prompt import build_system_prompt
    from app.turn import _agent_tz

    llm = ctx.llm
    if ctx.settings.multi_org:
        # Igual que el empujón: se piensa con la credencial del negocio, nunca
        # con la del entorno. Sin ella, sale el texto del dueño.
        config_ia = getattr(ctx.crm, "credenciales_llm", lambda: None)()
        if not config_ia:
            return guia
        llm = ctx.registro.llm(
            conv.organization_id, conv.organization_slug, config_ia, ctx.settings
        )
    try:
        system = build_system_prompt(
            profile=await resolve_profile(ctx),
            context=context,
            conv=conv,
            offered=[],
            tz=_agent_tz(ctx.settings),
            recordatorios=bool(getattr(ctx.crm, "supports_agenda_v2", False)),
        )
        history = await ctx.store.recent_messages(conv.id, ctx.settings.history_window)
        instruccion = INSTRUCCION.format(espera=_ESPERA[paso], n=paso + 1, guia=guia)
        messages: list[dict[str, Any]] = (
            [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in history]
            + [{"role": "system", "content": instruccion}]
        )
        reply = await llm.complete(messages, tools=None)
    except LlmExhausted as exc:
        logger.warning("toque %d de %s: IA agotada (%s) — sale la guía", paso + 1, conv.wa_identity, exc)
        return guia
    except Exception:
        logger.exception("toque %d de %s: no pude redactarlo — sale la guía", paso + 1, conv.wa_identity)
        return guia
    propio, filtrado = limpiar_herramienta_filtrada((reply.content or "").strip())
    propio = (propio or "").strip().strip('"«»“”').strip()
    if filtrado or not propio:
        logger.warning("toque %d de %s: la IA no dejó texto limpio — sale la guía", paso + 1, conv.wa_identity)
        return guia
    if promete_aviso(propio) or es_cotizacion(propio):
        logger.warning(
            "toque %d de %s: la IA prometió un asesor o reenvió la cotización — sale la guía",
            paso + 1,
            conv.wa_identity,
        )
        return guia
    return propio


def al_cerrar_turno(
    settings: Any,
    conv: Any,
    *,
    texto_enviado: str | None,
    texto_cliente: str | None,
    cliente_escribio_en: datetime,
) -> dict[str, Any] | None:
    """Lo que el final del turno guarda para los toques, o None si no aplican
    (entonces sigue el empujón único). Solo se llama si el turno SÍ envió."""
    if not settings.toques_activos:
        return None
    urgente = bool(conv.urgente) or es_urgencia(texto_cliente)
    arrancan = (
        bool(conv.cotizada)
        or es_cotizacion(texto_enviado)
        or (urgente and es_oferta_de_visita(texto_enviado))
    )
    if not arrancan:
        return None
    return {
        "cotizada": True,
        "urgente": urgente,
        "toque": 0,
        "toques_desde": cliente_escribio_en,
        "followup_due_at": vence(settings, cliente_escribio_en, 0, urgente),
        # El empujón único ya no aplica a esta conversación; sin esto, uno
        # enviado ANTES de cotizar la dejaría fuera del barrido.
        "followup_sent": False,
    }


def aplica(settings: Any, conv: Any) -> bool:
    """¿Esta conversación vencida es de toques (y no del empujón único)?"""
    return bool(
        settings.toques_activos
        and conv.cotizada
        and conv.toques_desde is not None
        and conv.toque < TOTAL
    )


async def enviar(ctx: Any, conv: Any, ultimo_inbound: datetime | None) -> None:
    """Envía el toque que toca. Ya fue reclamado (`claim_toque`): pase lo que
    pase aquí, ese toque no se repite. Corre bajo el candado de la conversación."""
    paso = conv.toque  # el valor ANTES del claim
    identity = conv.wa_identity
    fresca = await ctx.store.get_or_create_conversation(
        identity, conv.organization_id, conv.organization_slug
    )
    if fresca.last_inbound_at != ultimo_inbound:
        logger.info("toque %d de %s: el cliente escribió — omitido", paso + 1, identity)
        return
    registrar = getattr(ctx.crm, "registrar", None)
    if callable(registrar) and conv.crm_conversation_id:
        registrar(identity, conv.crm_conversation_id)
    try:
        context = await ctx.crm.get_context(identity)
    except CrmError as exc:
        logger.warning("toque %d de %s: CRM inaccesible (%s) — omitido", paso + 1, identity, exc)
        return
    if context is None:
        logger.warning("toque %d de %s: sin contexto — omitido", paso + 1, identity)
        return
    info = context.get("conversation") or {}
    crm_conv_id = info.get("id") or conv.crm_conversation_id
    etapa = _norm(((context.get("lead") or {}).get("stageName")) or "").strip()
    motivo = None
    if not crm_conv_id:
        motivo = "sin conversationId"
    elif not info.get("aiEnabled", False):
        motivo = "IA pausada"
    elif not info.get("windowOpen", False):
        motivo = "ventana de 24 h cerrada"
    elif etapa in ETAPAS_SIN_TOQUES:
        motivo = f"la tarjeta está en «{etapa}»"
    if motivo:
        # No tiene caso esperar al siguiente: se corta la serie.
        await ctx.store.update_conversation(conv.id, followup_due_at=None)
        logger.info("toque %d de %s: %s — se cortan los toques", paso + 1, identity, motivo)
        return
    mensaje = a_whatsapp(await redactar(ctx, conv, context, paso))
    try:
        await ctx.crm.send_message(str(crm_conv_id), mensaje)
    except CrmConflict as exc:
        logger.info("toque %d de %s: bloqueado por el CRM (%s)", paso + 1, identity, exc.code)
        return
    except CrmError as exc:
        logger.warning("toque %d de %s: envío falló (%s)", paso + 1, identity, exc)
        return
    await ctx.store.add_message(conv.id, "assistant", mensaje)
    logger.info("toque %d de %s enviado", paso + 1, identity)
