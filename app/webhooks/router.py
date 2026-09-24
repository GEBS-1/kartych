from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.api.max_client import MaxClient
from app.db.redis import claim_idempotency
from app.webhooks.handlers import dispatch_update, fingerprint, update_type_of

log = logging.getLogger(__name__)
router = APIRouter()


def _secret_ok(request: Request) -> bool:
    expected = request.app.state.settings.webhook_secret
    if not expected:
        return not request.app.state.settings.is_production
    incoming = request.headers.get("X-Max-Bot-Api-Secret", "")
    if len(incoming) != len(expected):
        return False
    return hmac.compare_digest(incoming, expected)


def _as_updates(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        nested = payload.get("updates")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        if payload.get("update_type") or payload.get("updateType"):
            return [payload]
    return []


async def _process(
    client: MaxClient,
    bot: dict[str, Any] | None,
    redis: Any | None,
    update: dict[str, Any],
    session_factory: Any | None,
    secret: str,
) -> None:
    key = fingerprint(update)
    if not await claim_idempotency(redis, key):
        log.info("duplicate update skipped type=%s", update_type_of(update))
        return
    try:
        await dispatch_update(
            client,
            update,
            bot,
            session_factory=session_factory,
            secret=secret,
        )
    except Exception:
        log.exception("failed to process update type=%s", update_type_of(update))


@router.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks) -> JSONResponse:
    if not _secret_ok(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bad webhook secret")
    try:
        payload = await request.json()
    except Exception as extra:
        raise HTTPException(status_code=400, detail="invalid json") from extra

    updates = _as_updates(payload)
    client: MaxClient = request.app.state.max_client
    bot = getattr(request.app.state, "bot", None)
    redis = getattr(request.app.state, "redis", None)
    factory = getattr(request.app.state, "session_factory", None)
    secret = request.app.state.settings.webhook_secret
    for update in updates:
        background.add_task(_process, client, bot, redis, update, factory, secret)
    return JSONResponse({"ok": True, "accepted": len(updates)}, status_code=200)
