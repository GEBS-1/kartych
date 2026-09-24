from __future__ import annotations

from typing import Any


def inline_keyboard(rows: list[list[dict[str, Any]]]) -> dict[str, Any]:
    return {"type": "inline_keyboard", "payload": {"buttons": rows}}


def callback_button(text: str, payload: str) -> dict[str, Any]:
    return {"type": "callback", "text": text, "payload": payload}


def link_button(text: str, url: str) -> dict[str, Any]:
    return {"type": "link", "text": text, "url": url}


def open_app_button(
    text: str,
    *,
    web_app: str | None = None,
    contact_id: int | None = None,
    payload: str | None = None,
) -> dict[str, Any]:
    button: dict[str, Any] = {"type": "open_app", "text": text}
    if web_app:
        button["web_app"] = web_app
    if contact_id is not None:
        button["contact_id"] = contact_id
    if payload:
        button["payload"] = payload
    return button


def home_keyboard(
    *,
    bot_username: str = "",
    bot_user_id: int | None = None,
    miniapp_url: str = "",
) -> list[dict[str, Any]]:
    rows = [
        [callback_button("Считать QR", "scan")],
        [callback_button("Открыть приложение", "openapp")],
    ]
    return [inline_keyboard(rows)]


def launch_app_keyboard(
    *,
    bot_username: str = "",
    bot_user_id: int | None = None,
    payload: str = "home",
    label: str = "Открыть",
) -> list[dict[str, Any]]:
    if not bot_username and bot_user_id is None:
        return home_keyboard()
    return [
        inline_keyboard(
            [
                [
                    open_app_button(
                        label,
                        web_app=bot_username or None,
                        contact_id=bot_user_id,
                        payload=payload,
                    )
                ]
            ]
        )
    ]


def client_keyboard(client: Any | None = None, bot: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    username = ""
    bot_user_id = None
    if client is not None:
        username = client.settings.max_bot_username
    if bot:
        username = username or str(bot.get("username") or "")
        bot_user_id = bot.get("user_id")
    return home_keyboard(bot_username=username, bot_user_id=bot_user_id)


def business_keyboard(client: Any | None = None, bot: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return client_keyboard(client, bot)


def shop_pick_keyboard(shops: list[tuple[str, str]]) -> list[dict[str, Any]]:
    rows = [[callback_button(name[:40], f"checkin:{shop_id}")] for shop_id, name in shops[:20]]
    rows.append([callback_button("Открыть Картыч", "home")])
    return [inline_keyboard(rows)]


def welcome_attachments(
    *,
    bot_username: str = "",
    bot_user_id: int | None = None,
    miniapp_url: str = "",
) -> list[dict[str, Any]]:
    return home_keyboard(
        bot_username=bot_username,
        bot_user_id=bot_user_id,
        miniapp_url=miniapp_url,
    )
