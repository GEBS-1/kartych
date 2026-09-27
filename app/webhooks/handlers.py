from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sqlalchemy import select

from app.api.max_client import MaxClient
from app.db.models import AppUser, UserRole
from app.web.max_login import tickets
from app.web.loyalty import (
    accept_staff_invite,
    apply_scan,
    business_people,
    client_cards,
    get_or_create_user,
    join_promo_token,
    live_qr_payload,
    parse_promo_start,
    parse_staff_start,
    shop_directory,
    shop_for_member,
    shop_for_owner,
)
from app.webhooks.keyboards import (
    business_keyboard,
    callback_button,
    client_keyboard,
    inline_keyboard,
    launch_app_keyboard,
    link_button,
    shop_pick_keyboard,
    welcome_attachments,
)

log = logging.getLogger(__name__)
_pending: dict[int, str] = {}

WELCOME_TEXT = (
    "Привет{name_part}! Картыч — карты лояльности.\n\n"
    "Кнопки под сообщением: «Показать QR», «Открыть кабинет» и «Поддержка»."
)

HELP_TEXT = (
    "Частые вопросы\n\n"
    "Как начислить покупку?\n"
    "Гость нажимает «Показать QR», кассир — «Сканировать QR». "
    "Или кассир показывает QR покупки, а гость сканирует сам.\n\n"
    "Как списать баллы?\n"
    "Гость открывает карту точки и создаёт QR списания. Кассир сканирует его.\n\n"
    "Не открывается камера?\n"
    "Разреши доступ в браузере или вставь текст QR вручную в приложении.\n\n"
    "Пропала акция, а штампы были?\n"
    "Накопленный прогресс не сгорает, даже если точку сняли акцию.\n\n"
    "Как открыть точку?\n"
    "Все входят как гости. В профиле отправь заявку с сайтом и меткой на карте — мы подтвердим кабинет."
)


def fingerprint(update: dict[str, Any]) -> str:
    raw = json.dumps(update, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def update_type_of(update: dict[str, Any]) -> str:
    return str(update.get("update_type") or update.get("updateType") or "")


def user_from(update: dict[str, Any]) -> dict[str, Any]:
    if isinstance(update.get("user"), dict):
        return update["user"]
    callback = update.get("callback")
    if isinstance(callback, dict) and isinstance(callback.get("user"), dict):
        return callback["user"]
    message = update.get("message")
    if isinstance(message, dict) and isinstance(message.get("sender"), dict):
        return message["sender"]
    return {}


def user_id_of(update: dict[str, Any]) -> int | None:
    user = user_from(update)
    value = user.get("user_id") or user.get("userId")
    if value is None:
        recipient = (update.get("message") or {}).get("recipient") or {}
        value = recipient.get("user_id") or recipient.get("userId")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def display_name_of(update: dict[str, Any]) -> str:
    user = user_from(update)
    return str(user.get("first_name") or user.get("name") or user.get("username") or "")


def username_of(update: dict[str, Any]) -> str | None:
    user = user_from(update)
    value = user.get("username")
    return str(value) if value else None


def chat_id_of(update: dict[str, Any]) -> int | None:
    value = update.get("chat_id") or update.get("chatId")
    if value is None:
        recipient = (update.get("message") or {}).get("recipient") or {}
        value = recipient.get("chat_id") or recipient.get("chatId")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def message_text_of(update: dict[str, Any]) -> str:
    message = update.get("message") or {}
    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    return str(body.get("text") or message.get("text") or "").strip()


def start_payload_of(update: dict[str, Any]) -> str:
    for key in ("payload", "start_payload", "startPayload"):
        value = update.get(key)
        if value:
            return str(value).strip()
    text = message_text_of(update)
    if text.lower().startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            return parts[1].strip()
    return ""


def callback_payload_of(update: dict[str, Any]) -> str:
    callback = update.get("callback") or {}
    return str(callback.get("payload") or "")


def start_keyboard(
    client: MaxClient, bot: dict[str, Any] | None = None, *, business: bool = False
) -> list[dict[str, Any]]:
    return welcome_attachments(
        bot_username=client.settings.max_bot_username or str((bot or {}).get("username") or ""),
        bot_user_id=(bot or {}).get("user_id"),
        miniapp_url=client.settings.miniapp_url,
        business=business,
    )


def menu_button() -> list[dict[str, Any]]:
    return [inline_keyboard([[callback_button("Меню", "home")]])]


async def is_business_user(
    session_factory: async_sessionmaker[AsyncSession] | None, user_id: int | None
) -> bool:
    if session_factory is None or user_id is None:
        return False
    async with session_factory() as session:
        user = await session.scalar(select(AppUser).where(AppUser.max_user_id == user_id))
        shop = await shop_for_member(session, user_id)
        return bool(shop and user and user.role == UserRole.BUSINESS.value)


def app_launch_keyboard(
    client: MaxClient,
    bot: dict[str, Any] | None = None,
    *,
    payload: str = "cabinet",
    label: str = "Открыть",
) -> list[dict[str, Any]]:
    return launch_app_keyboard(
        bot_username=client.settings.max_bot_username or str((bot or {}).get("username") or ""),
        bot_user_id=(bot or {}).get("user_id"),
        payload=payload,
        label=label,
    )


def callback_id_of(update: dict[str, Any]) -> str:
    callback = update.get("callback") or {}
    return str(callback.get("callback_id") or callback.get("callbackId") or "")


async def dispatch_update(
    client: MaxClient,
    update: dict[str, Any],
    bot: dict[str, Any] | None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    secret: str = "",
) -> None:
    kind = update_type_of(update)
    log.info("update %s user=%s", kind, user_id_of(update))
    if kind == "bot_started":
        await handle_bot_started(client, update, bot, session_factory=session_factory)
    elif kind == "message_created":
        await handle_message_created(client, update, bot, session_factory=session_factory, secret=secret)
    elif kind == "message_callback":
        await handle_message_callback(
            client, update, bot, session_factory=session_factory, secret=secret
        )
    else:
        log.info("skip update_type=%s", kind)


async def handle_bot_started(
    client: MaxClient,
    update: dict[str, Any],
    bot: dict[str, Any] | None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    user_id = user_id_of(update)
    if user_id is None:
        log.warning("bot_started without user_id: %s", update)
        return
    _pending.pop(user_id, None)
    name = display_name_of(update)
    username = username_of(update)
    token = tickets.parse(start_payload_of(update))
    if token and session_factory is not None:
        item = tickets.peek(token)
        async with session_factory() as session:
            user = await get_or_create_user(session, user_id, name or "Гость MAX", username)
            if user.role == UserRole.NONE.value:
                user.role = UserRole.CLIENT.value
            await session.commit()
        if tickets.complete(token, user_id):
            base = client.settings.public_base_url.rstrip("/")
            complete = f"{base}/login/complete/{token}"
            await client.send_message(
                user_id=user_id,
                text="Вход на сайт подтверждён. Вернись во вкладку — или жми кнопку ниже.",
                attachments=[inline_keyboard([[link_button("Открыть Картыч", complete)]])],
            )
            return
    invite_token = parse_staff_start(start_payload_of(update))
    if invite_token and session_factory is not None:
        async with session_factory() as session:
            user = await get_or_create_user(session, user_id, name or "Гость MAX", username)
            ok, message = await accept_staff_invite(session, user, invite_token)
            await session.commit()
        base = client.settings.public_base_url.rstrip("/")
        await client.send_message(
            user_id=user_id,
            text=message,
            attachments=[
                inline_keyboard(
                    [[link_button("Открыть кабинет", f"{base}/biz/scan" if ok else f"{base}/login")]]
                )
            ],
        )
        return
    promo_id = parse_promo_start(start_payload_of(update))
    if promo_id and session_factory is not None:
        async with session_factory() as session:
            user = await get_or_create_user(session, user_id, name or "Гость MAX", username)
            ok, message, _shop = await join_promo_token(session, user, promo_id)
            await session.commit()
        base = client.settings.public_base_url.rstrip("/")
        await client.send_message(
            user_id=user_id,
            text=message + (" На кассе нажми QR." if ok else ""),
            attachments=[
                inline_keyboard(
                    [[link_button("Мой QR", f"{base}/me/qr" if ok else f"{base}/promo/{promo_id}")]]
                )
            ],
        )
        return
    name_part = f", {name}" if name else ""
    business = await is_business_user(session_factory, user_id)
    await client.send_message(
        user_id=user_id,
        text=WELCOME_TEXT.format(name_part=name_part),
        attachments=start_keyboard(client, bot, business=business),
    )


async def handle_message_created(
    client: MaxClient,
    update: dict[str, Any],
    bot: dict[str, Any] | None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    secret: str = "",
) -> None:
    text = message_text_of(update)
    lowered = text.lower()
    user_id = user_id_of(update)
    if user_id is None:
        return
    if lowered in {"/scan", "scan", "qr", "считать qr", "камера"}:
        _pending[user_id] = "scan"
        await client.send_message(
            user_id=user_id,
            text="Открой камеру в Картыче. Можно и прислать QR сюда.",
            attachments=app_launch_keyboard(client, bot, payload="scan", label="Камера"),
        )
        return
    if lowered in {"ping", "/ping", "эхо", "echo"}:
        await client.send_message(user_id=user_id, text="pong")
        return
    wait = _pending.get(user_id)
    if wait == "shop_name" and session_factory is not None:
        _pending.pop(user_id, None)
        base = client.settings.public_base_url.rstrip("/")
        await client.send_message(
            user_id=user_id,
            text="Точку подтверждаем на сайте: название, сайт организации и метка на карте.",
            attachments=[inline_keyboard([[link_button("Открыть заявку", f"{base}/biz/apply")]])],
        )
        return
    if wait == "scan" or text.startswith("cupcard:") or text.startswith("cc1:") or text.startswith(
        "cupuser:"
    ):
        _pending.pop(user_id, None)
        if session_factory is None:
            return
        async with session_factory() as session:
            user = await get_or_create_user(session, user_id, display_name_of(update), username_of(update))
            result = await apply_scan(session, user=user, code=text, secret=secret or client.settings.webhook_secret)
            await session.commit()
        note = result.message
        if result.next_url:
            note = f"{note}\n{client.settings.public_base_url.rstrip('/')}{result.next_url}"
        await client.send_message(user_id=user_id, text=note, attachments=client_keyboard(client))
        return
    if lowered in {"/balance", "баланс", "карты"}:
        await _send_cards(client, user_id, display_name_of(update), username_of(update), session_factory)
        return
    if lowered in {"/help", "help", "помощь", "поддержка", "faq", "вопросы"}:
        await client.send_message(user_id=user_id, text=HELP_TEXT, attachments=menu_button())
        return
    if lowered in {"/menu", "меню", "menu"}:
        await handle_bot_started(client, update, bot, session_factory=session_factory)
        return
    await client.send_message(
        user_id=user_id,
        text="Нажми «Меню» — там «Показать QR», кабинет и поддержка.",
        attachments=menu_button(),
    )


async def handle_message_callback(
    client: MaxClient,
    update: dict[str, Any],
    bot: dict[str, Any] | None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    secret: str = "",
) -> None:
    callback_id = callback_id_of(update)
    payload = callback_payload_of(update)
    user_id = user_id_of(update)
    if user_id is None:
        return

    async def ack(note: str) -> None:
        if callback_id:
            await client.answer_callback(callback_id, notification=note)

    if payload == "home":
        await ack("Меню")
        await handle_bot_started(client, update, None, session_factory=session_factory)
        return
    if payload == "scan":
        await ack("Камера")
        _pending[user_id] = "scan"
        await client.send_message(
            user_id=user_id,
            text="Открой камеру в Картыче. Можно и прислать QR сюда.",
            attachments=app_launch_keyboard(client, bot, payload="scan", label="Камера"),
        )
        return
    if payload == "showqr":
        await ack("QR")
        business = await is_business_user(session_factory, user_id)
        await client.send_message(
            user_id=user_id,
            text="Покажи QR на кассе." if not business else "Покажи гостю QR покупки.",
            attachments=app_launch_keyboard(
                client, bot, payload="qr", label="Показать QR"
            ),
        )
        return
    if payload in {"openapp", "cabinet"}:
        await ack("Кабинет")
        await client.send_message(
            user_id=user_id,
            text="Кабинет Картыча.",
            attachments=app_launch_keyboard(client, bot, payload="cabinet", label="Кабинет"),
        )
        return
    if payload == "admin":
        await ack("Заявки")
        await client.send_message(
            user_id=user_id,
            text="Открой заявки и подтверди точку.",
            attachments=app_launch_keyboard(client, bot, payload="admin", label="Открыть заявку"),
        )
        return
    if payload == "help":
        await ack("Инструкция")
        await client.send_message(user_id=user_id, text=HELP_TEXT, attachments=menu_button())
        return
    if payload == "role:client":
        await ack("Кабинет гостя")
        if session_factory is not None:
            async with session_factory() as session:
                user = await get_or_create_user(session, user_id, display_name_of(update), username_of(update))
                if user.role == UserRole.NONE.value:
                    user.role = UserRole.CLIENT.value
                await session.commit()
        await client.send_message(
            user_id=user_id,
            text="Кабинет гостя. Сканируй QR — карта точки появится сама. Камера в приложении.",
            attachments=client_keyboard(client),
        )
        return
    if payload == "role:biz":
        await ack("Кабинет точки")
        base = client.settings.public_base_url.rstrip("/")
        if session_factory is not None:
            async with session_factory() as session:
                user = await get_or_create_user(session, user_id, display_name_of(update), username_of(update))
                shop = await shop_for_owner(session, user_id)
                from app.web.loyalty import shop_is_live

                if shop is not None and shop_is_live(shop):
                    user.role = UserRole.BUSINESS.value
                    await session.commit()
                    await client.send_message(
                        user_id=user_id,
                        text=f"Точка «{shop.name}». QR — гостю, гости — кто уже был.",
                        attachments=business_keyboard(client),
                    )
                    return
                await session.commit()
        await client.send_message(
            user_id=user_id,
            text="Кабинет точки откроется после подтверждения заявки на сайте.",
            attachments=[inline_keyboard([[link_button("Открыть заявку", f"{base}/biz/apply")]])],
        )
        return
    if payload == "dir":
        await ack("Точки")
        await _send_directory(client, user_id, session_factory)
        return
    if payload == "cards" or payload == "balance":
        await ack("Карты")
        await _send_cards(client, user_id, display_name_of(update), username_of(update), session_factory)
        return
    if payload == "visit":
        await ack("Визит")
        await _send_visit_picker(client, user_id, session_factory)
        return
    if payload.startswith("checkin:"):
        shop_id = payload.split(":", 1)[1]
        await ack("Визит")
        if session_factory is None:
            return
        async with session_factory() as session:
            user = await get_or_create_user(session, user_id, display_name_of(update), username_of(update))
            result = await apply_scan(
                session,
                user=user,
                code=f"cupcard:{shop_id}",
                secret=secret or client.settings.webhook_secret,
            )
            await session.commit()
        await client.send_message(user_id=user_id, text=result.message, attachments=client_keyboard(client))
        return
    if payload == "newshop":
        await ack("Название")
        _pending[user_id] = "shop_name"
        await client.send_message(user_id=user_id, text="Напиши название точки одним сообщением.")
        return
    if payload == "myshop":
        await ack("Точка")
        await _send_my_shop(client, user_id, session_factory)
        return
    if payload == "qr":
        await ack("QR")
        await _send_qr(client, user_id, session_factory, secret or client.settings.webhook_secret)
        return
    if payload == "people":
        await ack("Гости")
        await _send_people(client, user_id, session_factory)
        return
    await ack("Ок")


async def _send_directory(
    client: MaxClient,
    user_id: int,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> None:
    if session_factory is None:
        await client.send_message(user_id=user_id, text="Каталог пока недоступен.", attachments=client_keyboard(client))
        return
    async with session_factory() as session:
        shops = await shop_directory(session)
    if not shops:
        await client.send_message(user_id=user_id, text="Пока нет точек.", attachments=client_keyboard(client))
        return
    lines = [f"• {shop.name}" + (f" — {shop.city}" if shop.city else "") for shop in shops]
    await client.send_message(
        user_id=user_id,
        text="Точки в Картыч:\n" + "\n".join(lines),
        attachments=client_keyboard(client),
    )


async def _send_cards(
    client: MaxClient,
    user_id: int,
    name: str,
    username: str | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> None:
    if session_factory is None:
        await client.send_message(user_id=user_id, text="Карты пока недоступны.", attachments=client_keyboard(client))
        return
    async with session_factory() as session:
        await get_or_create_user(session, user_id, name, username)
        cards = await client_cards(session, user_id)
        await session.commit()
    if not cards:
        await client.send_message(
            user_id=user_id,
            text="Карт пока нет. Отметь визит — карта появится.",
            attachments=client_keyboard(client),
        )
        return
    lines = [
        f"«{card['name']}»: {card['visits']} из {card['goal']}"
        + (" — подарок!" if card["ready"] else "")
        for card in cards
    ]
    await client.send_message(user_id=user_id, text="Твои карты:\n" + "\n".join(lines), attachments=client_keyboard(client))


async def _send_visit_picker(
    client: MaxClient,
    user_id: int,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> None:
    if session_factory is None:
        return
    async with session_factory() as session:
        shops = await shop_directory(session)
    if not shops:
        await client.send_message(user_id=user_id, text="Пока нет точек для визита.", attachments=client_keyboard(client))
        return
    _pending[user_id] = "scan"
    await client.send_message(
        user_id=user_id,
        text="Выбери точку или пришли код с QR.",
        attachments=shop_pick_keyboard([(shop.id, shop.name) for shop in shops]),
    )


async def _send_my_shop(
    client: MaxClient,
    user_id: int,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> None:
    if session_factory is None:
        return
    async with session_factory() as session:
        shop = await shop_for_owner(session, user_id)
    if shop is None:
        await client.send_message(
            user_id=user_id,
            text="Точки ещё нет. Нажми «Создать / изменить» и напиши название.",
            attachments=business_keyboard(client),
        )
        return
    city = f"\nГород: {shop.city}" if shop.city else ""
    await client.send_message(
        user_id=user_id,
        text=f"«{shop.name}»{city}",
        attachments=business_keyboard(client),
    )


async def _send_qr(
    client: MaxClient,
    user_id: int,
    session_factory: async_sessionmaker[AsyncSession] | None,
    secret: str,
) -> None:
    if session_factory is None:
        return
    async with session_factory() as session:
        shop = await shop_for_owner(session, user_id)
    if shop is None:
        await client.send_message(
            user_id=user_id,
            text="Сначала создай точку.",
            attachments=business_keyboard(client),
        )
        return
    code = live_qr_payload(secret, shop.id)
    image_url = f"{client.settings.public_base_url.rstrip('/')}/qr/{shop.id}.png"
    await client.send_message(
        user_id=user_id,
        text=f"Покажи это гостю. Код: {code}",
        attachments=[
            {"type": "image", "payload": {"url": image_url}},
            *business_keyboard(client),
        ],
    )


async def _send_people(
    client: MaxClient,
    user_id: int,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> None:
    if session_factory is None:
        return
    async with session_factory() as session:
        shop = await shop_for_owner(session, user_id)
        if shop is None:
            people: list[dict[str, Any]] = []
        else:
            people = await business_people(session, shop)
    if not people:
        await client.send_message(user_id=user_id, text="Гостей пока нет.", attachments=business_keyboard(client))
        return
    lines = [f"• {row['name']}: {row['visits']} из {row['goal']}" for row in people[:30]]
    await client.send_message(user_id=user_id, text="Гости:\n" + "\n".join(lines), attachments=business_keyboard(client))
