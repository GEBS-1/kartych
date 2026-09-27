from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.api.telegram_client import TelegramAPIError, TelegramClient
from app.web.loyalty import (
    add_telegram_subscriber,
    get_telegram_config,
    remove_telegram_subscriber,
    set_shop_review,
)
from app.db.models import Business

log = logging.getLogger(__name__)
router = APIRouter()


def _expected_secret(request: Request, stored: str, token: str) -> str:
    if stored:
        return stored
    settings = request.app.state.settings
    if token and settings.webhook_secret:
        import hashlib

        return hashlib.sha256(f"{token}:{settings.webhook_secret}".encode()).hexdigest()[:32]
    return ""


def _secret_ok(request: Request, expected: str) -> bool:
    if not expected:
        return not request.app.state.settings.is_production
    incoming = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if len(incoming) != len(expected):
        return False
    return hmac.compare_digest(incoming, expected)


def _display_name(user: dict[str, Any] | None, chat: dict[str, Any] | None) -> str:
    parts = []
    if user:
        parts.extend(part for part in [user.get("first_name"), user.get("last_name")] if part)
    if not parts and chat:
        parts.append(str(chat.get("title") or chat.get("first_name") or ""))
    return " ".join(str(part) for part in parts if part).strip()


async def _client_for(request: Request, token: str) -> TelegramClient:
    current = getattr(request.app.state, "telegram", None)
    if current is not None and current.token == token:
        return current
    return TelegramClient(token)


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> JSONResponse:
    factory = getattr(request.app.state, "session_factory", None)
    settings = request.app.state.settings
    if factory is None:
        raise HTTPException(status_code=503, detail="no database")
    async with factory() as session:
        cfg = await get_telegram_config(session)
        token = (cfg.bot_token if cfg else "") or settings.telegram_bot_token
        stored_secret = cfg.webhook_secret if cfg else ""
    if not token:
        raise HTTPException(status_code=503, detail="telegram bot is off")
    expected = _expected_secret(request, stored_secret, token)
    if not _secret_ok(request, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bad telegram secret")
    try:
        payload = await request.json()
    except Exception as extra:
        raise HTTPException(status_code=400, detail="invalid json") from extra
    try:
        await dispatch_telegram(request, payload, token)
    except Exception:
        log.exception("telegram update failed")
    return JSONResponse({"ok": True})


async def dispatch_telegram(request: Request, payload: dict[str, Any], token: str) -> None:
    factory = request.app.state.session_factory
    client = await _client_for(request, token)
    callback = payload.get("callback_query")
    if isinstance(callback, dict):
        await _handle_callback(request, client, callback)
        return
    message = payload.get("message")
    if not isinstance(message, dict):
        return
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    user = message.get("from") if isinstance(message.get("from"), dict) else {}
    try:
        chat_id = int(chat.get("id"))
    except (TypeError, ValueError):
        return
    text = str(message.get("text") or "").strip()
    name = _display_name(user, chat)
    username = str(user.get("username") or "") or None
    lowered = text.lower()
    if lowered.startswith("/start"):
        async with factory() as session:
            await add_telegram_subscriber(
                session, chat_id, display_name=name, username=username, added_by=0
            )
            await session.commit()
        await client.send_message(
            chat_id,
            "Готово. Сюда буду писать, когда точка отправит заявку. "
            "Можно сразу подтвердить или отклонить кнопками.",
        )
        return
    if lowered.startswith("/stop"):
        async with factory() as session:
            await remove_telegram_subscriber(
                session, chat_id, pinned=request.app.state.settings.telegram_ids()
            )
            await session.commit()
        await client.send_message(chat_id, "Больше не пишу. Если снова нужно — нажми /start.")
        return
    await client.send_message(
        chat_id,
        "Это бот заявок Картыча. /start — получать заявки, /stop — отключить.",
    )


async def _handle_callback(request: Request, client: TelegramClient, callback: dict[str, Any]) -> None:
    data = str(callback.get("data") or "")
    callback_id = str(callback.get("id") or "")
    from_user = callback.get("from") if isinstance(callback.get("from"), dict) else {}
    message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    try:
        chat_id = int(chat.get("id") or from_user.get("id"))
    except (TypeError, ValueError):
        return
    approved = data.startswith("ok:")
    rejected = data.startswith("no:")
    if not approved and not rejected:
        if callback_id:
            await client.answer_callback(callback_id)
        return
    shop_id = data[3:]
    factory = request.app.state.session_factory
    async with factory() as session:
        shop = await session.get(Business, shop_id)
        if shop is None:
            if callback_id:
                await client.answer_callback(callback_id, "Заявку уже убрали.")
            return
        if shop.status == "verified" and approved:
            if callback_id:
                await client.answer_callback(callback_id, "Уже подтверждена.")
            return
        owner = await set_shop_review(session, shop, approved=approved)
        await session.commit()
        name = shop.name
        owner_id = owner.max_user_id if owner is not None else None
    note = (
        f"Точку «{name}» подтвердили. Кабинет бизнеса открыт."
        if approved
        else f"Заявку «{name}» отклонили."
    )
    if callback_id:
        await client.answer_callback(callback_id, "Готово")
    try:
        await client.send_message(chat_id, note)
    except TelegramAPIError:
        log.warning("telegram confirm notice failed chat=%s", chat_id)
    max_client = getattr(request.app.state, "max_client", None)
    if approved and max_client is not None and owner_id is not None:
        try:
            await max_client.send_message(text=note, user_id=owner_id)
        except Exception:
            pass
