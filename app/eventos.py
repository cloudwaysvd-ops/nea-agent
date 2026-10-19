"""Eventos salientes hacia un orquestador propio (n8n, Make…). Fork, no upstream.

Hoy hay uno: `handoff`. Cuando Nea le pasa una conversación a un humano, el
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
                        "EVENTO %s entregado (conv=%s)",
                        evento,
                        payload.get("crm_conversation_id"),
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
        tarea = asyncio.create_task(enviar(settings, payload))
        _tareas.add(tarea)
        tarea.add_done_callback(_tareas.discard)
        return tarea
    except Exception:
        logger.exception("EVENTO handoff: no pude programar el aviso — sigo")
        return None
