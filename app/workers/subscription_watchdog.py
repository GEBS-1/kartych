from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI

from app.api.max_client import MaxAPIError, iter_subscriptions

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def check_subscriptions(app: FastAPI) -> dict[str, Any]:
    client = app.state.max_client
    settings = app.state.settings
    status: dict[str, Any] = {
        "ok": False,
        "checked_at": _now(),
        "url": settings.webhook_url,
        "action": "none",
    }
    try:
        payload = await client.get_subscriptions()
        status["subscriptions"] = iter_subscriptions(payload)
        if client.subscription_matches(payload):
            status["ok"] = True
            status["action"] = "ok"
        else:
            log.warning("webhook subscription missing or stale, resubscribing to %s", settings.webhook_url)
            await client.subscribe_webhook()
            status["ok"] = True
            status["action"] = "resubscribed"
    except MaxAPIError as exc:
        log.error("GET /subscriptions failed: %s body=%s", exc, exc.body)
        status["error"] = str(exc)
        status["body"] = exc.body
    except Exception as exc:
        log.exception("subscription watchdog failed")
        status["error"] = str(exc)
    app.state.webhook_status = status
    return status


async def run_subscription_watchdog(app: FastAPI) -> None:
    settings = app.state.settings
    interval = settings.watchdog_interval_seconds
    log.info("subscription watchdog started, interval=%ss", interval)
    while True:
        if settings.max_bot_token and settings.public_base_url.startswith("https://"):
            await check_subscriptions(app)
        else:
            app.state.webhook_status = {
                "ok": False,
                "action": "skipped",
                "reason": "no token or webhook URL is not HTTPS",
            }
        await asyncio.sleep(interval)
