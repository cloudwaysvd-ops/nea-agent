"""Eventos salientes hacia un orquestador propio (n8n, Make…). Fork, no upstream.

Dos eventos:

- `codigo`: el primer mensaje trae el código de la landing, «(Ref. C7K2QX)», que
  une el clic de Google Ads (gclid) con este teléfono. Nea lo manda al
  orquestador y lo QUITA del texto antes del LLM: el agente nunca lo ve, así
  que no depende del prompt. La bandeja sí lo ve (el relay va crudo).
- Cotización (no es un evento al orquestador: va directo al CRM). Cuando Nea
  ENVÍA una cotización con el formato fijo del perfil («⭐ Garantía N meses» y
  «💰 $precio»), la tarjeta del lead pasa a `COTIZACION_STAGE` («Cotizado»),
  solo hacia adelante. Lo decide el código, no el LLM.
- `handoff`: cuando Nea le pasa una conversación a un humano, el
CRM solo pausa la IA y lo pinta en la bandeja; nadie se entera si no la tiene
abierta, y el motivo completo del agente («FUERA DE CATALOGO: Suzuki Ertiga…»)
se reduce a una de cinco categorías. Este evento lleva ese motivo, tal cual, a
un webhook que avisa a los asesores.

Best-effort absoluto: corre en segundo plano DESPUÉS de registrar el handoff en
el CRM, reintenta poco y jamás lanza. Si el webhook se cae, el handoff ya
quedó hecho y la despedida ya salió; solo se pierde el aviso, que queda en el
log (`EVENTO handoff …`) para reenviarlo a mano.

Sin `EVENTOS_WEBHOOK_URL` no hace nada: el fork se comporta como la imagen
oficial.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("nea.eventos")

INTENTOS = 3
TIMEOUT_SECONDS = 5.0
# Espera entre intentos: 1 s y luego 2 s.
ESPERA_BASE = 1.0
# El motivo viaja completo, pero no sin tope: lo escribe un LLM.
MAX_MOTIVO = 1000

_tareas: set[asyncio.Task[Any]] = set()

# El código de la landing: 6 caracteres sin I, O, 0 ni 1. Misma regla que
# `core.vincular_codigo` en la Postgres de n8n, que es quien decide de verdad.
_CODIGO = r"ref\.?\s*:?\s*([A-HJ-NP-Z2-9]{6})\b"
CODIGO_RE = re.compile(_CODIGO, re.IGNORECASE)
# Lo que se quita del texto: el código con sus paréntesis y el espacio previo.
_QUITAR_RE = re.compile(r"\s*\(?\s*" + _CODIGO + r"\s*\)?", re.IGNORECASE)


# Una cotización trae, por formato fijo del perfil, la garantía en meses y el
# precio con el emoji de dinero. La confirmación final del pedido trae precio
# pero no garantía: no cuenta (y de todos modos el CRM solo avanza).
_GARANTIA_RE = re.compile(r"garant[ií]a\s*:?\s*\d+\s*mes", re.IGNORECASE)
_PRECIO_RE = re.compile(r"💰\s*\$\s*\d")


def es_cotizacion(texto: str | None) -> bool:
    t = texto or ""
    return bool(_GARANTIA_RE.search(t) and _PRECIO_RE.search(t))


async def _avanzar(ctx: Any, crm_conversation_id: str, etapa: str) -> None:
    try:
        r = await ctx.crm.post_stage(crm_conversation_id, etapa)
        logger.info(
            "ETAPA %s: %s (conv=%s, desde=%s)",
            etapa,
            r.get("status"),
            crm_conversation_id,
            (r.get("from") or {}).get("name"),
        )
    except Exception as exc:  # 404, red, CRM sin el endpoint: nada escapa
        logger.warning(
            "ETAPA %s: no pude mover la tarjeta (conv=%s): %s",
            etapa,
            crm_conversation_id,
            exc,
        )


def avanzar_si_cotizo(
    ctx: Any, *, crm_conversation_id: str, texto: str | None
) -> asyncio.Task[Any] | None:
    """Si lo enviado es una cotización, pide al CRM avanzar la tarjeta. En
    segundo plano: jamás retrasa ni bloquea la respuesta."""
    try:
        etapa = (getattr(ctx.settings, "cotizacion_stage", "") or "").strip()
        if not etapa or not crm_conversation_id or not es_cotizacion(texto):
            return None
        tarea = asyncio.create_task(_avanzar(ctx, crm_conversation_id, etapa))
        _tareas.add(tarea)
        tarea.add_done_callback(_tareas.discard)
        return tarea
    except Exception:
        logger.exception("ETAPA: no pude programar el avance — sigo")
        return None


def detectar_codigo(texto: str | None) -> str | None:
    """El código en mayúsculas, o None si el texto no trae uno."""
    m = CODIGO_RE.search(texto or "")
    return m.group(1).upper() if m else None


def quitar_codigo(texto: str) -> str:
    """El texto sin «(Ref. XXXXXX)». Si no queda nada, el original."""
    limpio = _QUITAR_RE.sub("", texto).strip()
    return limpio or texto


def _ahora_iso(zona: str) -> str:
    try:
        tz = ZoneInfo(zona)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).isoformat(timespec="seconds")


def payload_handoff(
    *,
    identity: str,
    crm_conversation_id: str,
    motivo: str,
    context: dict[str, Any] | None,
    zona: str,
) -> dict[str, Any]:
    """Lo que viaja al webhook. Sale del contexto que el turno ya leyó del CRM:
    ni una llamada extra. Sin contexto (redes de seguridad), lo mínimo."""
    contacto = (context or {}).get("contact") or {}
    ficha = contacto.get("ficha")
    return {
        "evento": "handoff",
        "identidad": identity,
        "nombre": contacto.get("name") or "",
        "telefono": contacto.get("phone") or identity,
        "motivo": (motivo or "").strip()[:MAX_MOTIVO],
        "ficha": ficha if isinstance(ficha, dict) else {},
        "crm_conversation_id": crm_conversation_id,
        "crm_contact_id": contacto.get("id") or "",
        "fecha": _ahora_iso(zona),
    }


async def enviar(settings: Any, payload: dict[str, Any]) -> bool:
    """POST al webhook con el token en `X-Nea-Token`. True si respondió 2xx."""
    url = (getattr(settings, "eventos_webhook_url", "") or "").strip()
    if not url:
        return False
    headers = {}
    token = (getattr(settings, "eventos_webhook_token", "") or "").strip()
    if token:
        headers["X-Nea-Token"] = token
    evento = payload.get("evento", "?")
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        for intento in range(1, INTENTOS + 1):
            try:
                resp = await client.post(url, json=payload, headers=headers)
                if 200 <= resp.status_code < 300:
                    logger.info(
                        "EVENTO %s entregado (conv=%s codigo=%s)",
                        evento,
                        payload.get("crm_conversation_id", "-"),
                        payload.get("codigo", "-"),
                    )
                    return True
                logger.warning(
                    "EVENTO %s: el webhook respondió %s (intento %d)",
                    evento,
                    resp.status_code,
                    intento,
                )
            except Exception as exc:  # red, DNS, timeout: nada debe escapar
                logger.warning(
                    "EVENTO %s: el webhook falló (intento %d): %s",
                    evento,
                    intento,
                    type(exc).__name__,
                )
            if intento < INTENTOS:
                await asyncio.sleep(ESPERA_BASE * 2.0 ** (intento - 1))
    logger.error(
        "EVENTO %s perdido tras %d intentos: conv=%s identidad=%s motivo=%r",
        evento,
        INTENTOS,
        payload.get("crm_conversation_id"),
        payload.get("identidad"),
        payload.get("motivo"),
    )
    return False


def _programar(settings: Any, payload: dict[str, Any]) -> asyncio.Task[Any] | None:
    tarea = asyncio.create_task(enviar(settings, payload))
    _tareas.add(tarea)
    tarea.add_done_callback(_tareas.discard)
    return tarea


def avisar_codigo(
    settings: Any,
    *,
    identity: str,
    texto: str,
    codigo: str,
    nombre: str | None = None,
    wamid: str | None = None,
) -> asyncio.Task[Any] | None:
    """Evento `codigo` en segundo plano. Nunca bloquea ni lanza."""
    try:
        if not (getattr(settings, "eventos_webhook_url", "") or "").strip():
            return None
        return _programar(
            settings,
            {
                "evento": "codigo",
                "codigo": codigo,
                # El texto completo viaja: n8n lo pasa tal cual a
                # core.vincular_codigo, que tiene la última palabra.
                "texto": (texto or "")[:MAX_MOTIVO],
                "identidad": identity,
                "nombre": nombre or "",
                "wamid": wamid or "",
                "fecha": _ahora_iso(getattr(settings, "agent_timezone", "UTC")),
            },
        )
    except Exception:
        logger.exception("EVENTO codigo: no pude programar el envío — sigo")
        return None


def avisar_handoff(
    ctx: Any,
    *,
    identity: str,
    crm_conversation_id: str,
    motivo: str,
    context: dict[str, Any] | None = None,
) -> asyncio.Task[Any] | None:
    """Dispara el evento en segundo plano. Nunca bloquea ni lanza."""
    try:
        settings = ctx.settings
        if not (getattr(settings, "eventos_webhook_url", "") or "").strip():
            return None
        payload = payload_handoff(
            identity=identity,
            crm_conversation_id=crm_conversation_id,
            motivo=motivo,
            context=context,
            zona=getattr(settings, "agent_timezone", "UTC"),
        )
        return _programar(settings, payload)
    except Exception:
        logger.exception("EVENTO handoff: no pude programar el aviso — sigo")
        return None
