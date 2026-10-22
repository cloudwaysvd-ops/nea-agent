"""Fork: los 3 toques de seguimiento después de una cotización.

Si Diana ya cotizó y el cliente se calla, salen hasta tres mensajes de texto
FIJO (sin IA), contados desde el último mensaje del cliente:

- toque 1 a los `TOQUE_1_MINUTOS` (o `TOQUE_1_URGENTE_MINUTOS` si el carro
  está varado),
- toque 2 a las `TOQUE_2_HORAS`,
- toque 3 a las `TOQUE_3_HORAS` — antes de las 24 h, porque después Meta ya
  no deja escribirle sin plantilla.

Si el cliente escribe, la cuenta vuelve a empezar desde su mensaje. Sin
cotización todavía, el seguimiento es el empujón único de siempre
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
    cotizada = bool(conv.cotizada) or es_cotizacion(texto_enviado)
    if not cotizada:
        return None
    urgente = bool(conv.urgente) or es_urgencia(texto_cliente)
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
    mensaje = a_whatsapp(texto(ctx.settings, paso).strip())
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
