from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AppUser,
    Business,
    BusinessLocation,
    Challenge,
    Customer,
    LoyaltyProgram,
    PlatformAdmin,
    Product,
    Receipt,
    PromoLink,
    ShopInvite,
    ShopStaff,
    TelegramSetting,
    TelegramSubscriber,
    Ticket,
    TicketKind,
    UserRole,
    Visit,
)

COOLDOWN = timedelta(hours=4)
QR_SLOT = 30
INVITE_TTL = timedelta(days=7)
STAFF_START = "s_"
PROMO_START = "p_"
WEEKDAYS = (
    ("mon", "Пн"),
    ("tue", "Вт"),
    ("wed", "Ср"),
    ("thu", "Чт"),
    ("fri", "Пт"),
    ("sat", "Сб"),
    ("sun", "Вс"),
)
DEFAULT_DAYS = "mon,tue,wed,thu,fri"


def inn_digits(value: str) -> str:
    return "".join(char for char in (value or "") if char.isdigit())


def valid_inn(value: str) -> bool:
    digits = inn_digits(value)
    if len(digits) == 10:
        coef = (2, 4, 10, 3, 5, 9, 4, 6, 8)
        check = sum(int(digits[i]) * coef[i] for i in range(9)) % 11 % 10
        return check == int(digits[9])
    if len(digits) == 12:
        first = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        second = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        n11 = sum(int(digits[i]) * first[i] for i in range(10)) % 11 % 10
        n12 = sum(int(digits[i]) * second[i] for i in range(11)) % 11 % 10
        return n11 == int(digits[10]) and n12 == int(digits[11])
    return False


def valid_fio(value: str) -> bool:
    parts = [part for part in (value or "").replace("ё", "е").split() if part]
    if len(parts) < 2 or len(parts) > 4:
        return False
    allowed = set("абвгдежзийклмнопрстуфхцчшщъыьэюя-")
    return all(set(part.lower()) <= allowed and len(part) >= 2 for part in parts)


def normalize_schedule(days: list[str] | str | None, start: str, end: str) -> tuple[str, str, str]:
    wanted = {item for item, _label in WEEKDAYS}
    if isinstance(days, str):
        picked = [item.strip() for item in days.split(",") if item.strip() in wanted]
    else:
        picked = [item for item in (days or []) if item in wanted]
    if not picked:
        picked = DEFAULT_DAYS.split(",")
    start = start.strip()[:5] or "10:00"
    end = end.strip()[:5] or "22:00"
    return ",".join(picked), start, end


def schedule_label(days: str, start: str, end: str) -> str:
    names = dict(WEEKDAYS)
    labels = [names[item] for item in (days or "").split(",") if item in names]
    hours = f"{start or '10:00'}–{end or '22:00'}"
    return f"{', '.join(labels) or 'Пн–Пт'} · {hours}"


def shop_is_live(shop: Business) -> bool:
    return shop.status == "verified"


def program_open(program: LoyaltyProgram) -> bool:
    return bool(program.is_active) and program.archived_at is None


async def program_usable(
    session: AsyncSession, program: LoyaltyProgram, customer_id: str | None
) -> bool:
    if program_open(program):
        return True
    if program.archived_at is not None and customer_id:
        return await progress_for(session, customer_id, program) > 0
    return False


@dataclass
class ScanResult:
    ok: bool
    message: str
    visits: int = 0
    goal: int = 7
    reward: bool = False
    shop: str = ""
    next_url: str = ""


def _sign(secret: str, raw: str) -> str:
    return hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()[:10]


def _sign_slot(secret: str, business_id: str, slot: int) -> str:
    return _sign(secret, f"{business_id}:{slot}")


def live_qr_payload(secret: str, business_id: str) -> str:
    slot = int(time.time()) // QR_SLOT
    return f"cupcard:{business_id}:{slot}:{_sign_slot(secret, business_id, slot)}"


def ticket_payload(secret: str, ticket_id: str) -> str:
    return f"cc1:{ticket_id}:{_sign(secret, ticket_id)}"


def guest_payload(secret: str, max_user_id: int) -> str:
    return f"cupuser:{max_user_id}:{_sign(secret, str(max_user_id))}"


def parse_guest_code(secret: str, code: str) -> int | None:
    text = (code or "").strip()
    if "cupuser:" in text:
        text = text[text.index("cupuser:") :]
    parts = text.split(":")
    if len(parts) < 3 or parts[0] != "cupuser":
        return None
    try:
        uid = int(parts[1])
    except ValueError:
        return None
    if hmac.compare_digest(parts[2], _sign(secret, str(uid))):
        return uid
    return 0


def staff_start_payload(token: str) -> str:
    return f"{STAFF_START}{token}"


def parse_staff_start(raw: str) -> str | None:
    text = (raw or "").strip()
    if text.startswith(STAFF_START) and len(text) == len(STAFF_START) + 16:
        return text[len(STAFF_START) :]
    return None


def promo_start_payload(token: str) -> str:
    return f"{PROMO_START}{token}"


def parse_promo_start(raw: str) -> str | None:
    text = (raw or "").strip()
    if text.startswith(PROMO_START) and len(text) == len(PROMO_START) + 16:
        return text[len(PROMO_START) :]
    return None


def parse_qr_payload(secret: str, code: str) -> str | None:
    text = (code or "").strip()
    if "cupcard:" in text:
        text = text[text.index("cupcard:") :]
    parts = text.split(":")
    if len(parts) < 2 or parts[0] != "cupcard":
        return None
    business_id = parts[1]
    if len(parts) == 2:
        return business_id
    if len(parts) >= 4:
        sig = parts[3]
        now_slot = int(time.time()) // QR_SLOT
        for candidate in (now_slot, now_slot - 1, now_slot + 1):
            if hmac.compare_digest(sig, _sign_slot(secret, business_id, candidate)):
                return business_id
        return None
    return None


def parse_ticket_code(secret: str, code: str) -> str | None:
    text = (code or "").strip()
    if "cc1:" in text:
        text = text[text.index("cc1:") :]
    parts = text.split(":")
    if len(parts) < 3 or parts[0] != "cc1":
        return None
    ticket_id, sig = parts[1], parts[2]
    if hmac.compare_digest(sig, _sign(secret, ticket_id)):
        return ticket_id
    return None


def promo_goal(program: LoyaltyProgram) -> int:
    kind = program.kind
    if kind in {ProgramKind_AMOUNT, "amount"}:
        return max(program.amount_required, 1)
    if kind in {ProgramKind_QTY, "quantity"}:
        return max(program.qty_required, 1)
    return max(program.stamps_required, 1)


ProgramKind_AMOUNT = "amount"
ProgramKind_QTY = "quantity"
ProgramKind_VISITS = "visits"


def promo_unit(program: LoyaltyProgram) -> str:
    if program.kind == "amount":
        return "₽"
    if program.kind == "quantity":
        return "шт"
    return "визитов"


def promo_rule(program: LoyaltyProgram) -> str:
    if program.kind == "amount":
        return f"набери {program.amount_required} ₽ — {program.reward_title}"
    if program.kind == "quantity":
        target = program.group_name or "товар"
        return f"купи {program.qty_required} шт. ({target}) — {program.reward_title}"
    return f"{program.stamps_required} визитов — {program.reward_title}"


async def get_or_create_user(
    session: AsyncSession, max_user_id: int, name: str, username: str | None
) -> AppUser:
    user = await session.scalar(select(AppUser).where(AppUser.max_user_id == max_user_id))
    if user is None:
        user = AppUser(
            max_user_id=max_user_id, display_name=name, username=username, role=UserRole.NONE.value
        )
        session.add(user)
        await session.flush()
        return user
    if name and user.display_name != name:
        user.display_name = name
    if username:
        user.username = username
    return user


async def owner_shops(session: AsyncSession, max_user_id: int) -> list[Business]:
    rows = (
        await session.scalars(
            select(Business)
            .where(Business.owner_max_user_id == max_user_id)
            .order_by(Business.created_at)
        )
    ).all()
    return list(rows)


async def consolidate_owner_network(session: AsyncSession, max_user_id: int) -> None:
    rows = await owner_shops(session, max_user_id)
    if not rows:
        return
    hq = next((row for row in rows if not row.parent_id), rows[0])
    hq.parent_id = None
    if not (hq.org_name or "").strip():
        hq.org_name = hq.name
    for row in rows:
        if row.id == hq.id:
            continue
        row.parent_id = hq.id
        if not (row.org_name or "").strip():
            row.org_name = hq.org_name
        if not row.inn:
            row.inn = hq.inn
        if not row.website:
            row.website = hq.website
        if not row.director_name:
            row.director_name = hq.director_name


async def shop_for_owner(session: AsyncSession, max_user_id: int) -> Business | None:
    await consolidate_owner_network(session, max_user_id)
    return await session.scalar(
        select(Business)
        .where(Business.owner_max_user_id == max_user_id, Business.parent_id.is_(None))
        .order_by(Business.created_at)
        .limit(1)
    )


async def points_for_owner(session: AsyncSession, max_user_id: int) -> list[Business]:
    hq = await shop_for_owner(session, max_user_id)
    if hq is None:
        return []
    children = list(
        (
            await session.scalars(
                select(Business).where(Business.parent_id == hq.id).order_by(Business.created_at)
            )
        ).all()
    )
    return [hq, *children]


async def staff_for(session: AsyncSession, max_user_id: int) -> ShopStaff | None:
    return await session.scalar(select(ShopStaff).where(ShopStaff.max_user_id == max_user_id))


async def ensure_owner_staff(session: AsyncSession, shop: Business) -> None:
    if shop.owner_max_user_id is None:
        return
    hq_id = shop.parent_id or shop.id
    row = await session.scalar(
        select(ShopStaff).where(ShopStaff.max_user_id == shop.owner_max_user_id)
    )
    if row is None:
        session.add(
            ShopStaff(
                business_id=hq_id,
                max_user_id=shop.owner_max_user_id,
                kind="owner",
                can_stats=True,
                can_earn=True,
                can_scan=True,
                can_edit=True,
            )
        )
        await session.flush()
        return
    row.business_id = hq_id
    row.kind = "owner"
    row.can_stats = True
    row.can_earn = True
    row.can_scan = True
    row.can_edit = True


async def shop_for_member(
    session: AsyncSession, max_user_id: int, preferred_id: str | None = None
) -> Business | None:
    staff = await staff_for(session, max_user_id)
    if staff is not None and staff.kind != "owner":
        shop = await session.get(Business, staff.business_id)
        if shop is not None:
            return shop
    points = await points_for_owner(session, max_user_id)
    if not points:
        return None
    hq = points[0]
    await ensure_owner_staff(session, hq)
    if preferred_id:
        for point in points:
            if point.id == preferred_id:
                return point
    return hq


async def is_shop_member(session: AsyncSession, user: AppUser, shop: Business) -> bool:
    if shop.owner_max_user_id == user.max_user_id:
        return True
    staff = await staff_for(session, user.max_user_id)
    return staff is not None and staff.business_id == shop.id


async def can_scan_for_shop(session: AsyncSession, user: AppUser, shop: Business) -> bool:
    if shop.owner_max_user_id == user.max_user_id:
        return True
    staff = await staff_for(session, user.max_user_id)
    return bool(staff and staff.business_id == shop.id and staff.can_scan)


async def active_program(session: AsyncSession, business_id: str) -> LoyaltyProgram | None:
    return await session.scalar(
        select(LoyaltyProgram).where(
            LoyaltyProgram.business_id == business_id,
            LoyaltyProgram.is_active.is_(True),
            LoyaltyProgram.archived_at.is_(None),
        )
    )


async def shop_programs(
    session: AsyncSession, business_id: str, *, include_archived: bool = False
) -> list[LoyaltyProgram]:
    query = select(LoyaltyProgram).where(LoyaltyProgram.business_id == business_id)
    if not include_archived:
        query = query.where(
            LoyaltyProgram.is_active.is_(True), LoyaltyProgram.archived_at.is_(None)
        )
    rows = (await session.scalars(query.order_by(LoyaltyProgram.created_at.desc()))).all()
    return list(rows)


async def shop_products(
    session: AsyncSession, business_id: str, *, active_only: bool = True
) -> list[Product]:
    query = select(Product).where(Product.business_id == business_id)
    if active_only:
        query = query.where(Product.is_active.is_(True))
    rows = (await session.scalars(query.order_by(Product.group_name, Product.name))).all()
    return list(rows)


async def create_shop(
    session: AsyncSession,
    owner: AppUser,
    *,
    name: str,
    city: str,
    stamps: int = 7,
    reward: str = "Подарок",
    verified: bool = False,
) -> Business:
    existing = await shop_for_owner(session, owner.max_user_id)
    if existing is not None:
        existing.name = name
        existing.city = city
        program = await active_program(session, existing.id)
        if program is not None:
            program.stamps_required = max(2, min(stamps, 12))
            program.reward_title = reward
            program.title = f"{program.stamps_required} визитов — подарок"
        if verified:
            existing.status = "verified"
            existing.verified_at = existing.verified_at or datetime.now(UTC)
            owner.role = UserRole.BUSINESS.value
        await ensure_owner_staff(session, existing)
        if not (existing.org_name or "").strip():
            existing.org_name = existing.name
        return existing
    shop = Business(
        name=name.strip() or "Моя точка",
        city=city.strip(),
        category="shop",
        owner_max_user_id=owner.max_user_id,
        org_name=name.strip() or "Моя точка",
        status="verified" if verified else "pending",
        verified_at=datetime.now(UTC) if verified else None,
    )
    session.add(shop)
    await session.flush()
    session.add(
        LoyaltyProgram(
            business_id=shop.id,
            kind="visits",
            title=f"{max(2, min(stamps, 12))} визитов — подарок",
            stamps_required=max(2, min(stamps, 12)),
            reward_title=reward.strip() or "Подарок",
        )
    )
    if verified:
        owner.role = UserRole.BUSINESS.value
    else:
        if owner.role == UserRole.NONE.value:
            owner.role = UserRole.CLIENT.value
    await ensure_owner_staff(session, shop)
    return shop


async def network_ids_for(session: AsyncSession, shop: Business) -> list[str]:
    hq_id = shop.parent_id or shop.id
    children = list(
        (await session.scalars(select(Business.id).where(Business.parent_id == hq_id))).all()
    )
    return [hq_id, *children]


async def add_network_point(
    session: AsyncSession,
    hq: Business,
    *,
    name: str,
    city: str,
    address: str,
    latitude: float | None = None,
    longitude: float | None = None,
) -> Business:
    program = await active_program(session, hq.id)
    point = Business(
        name=name.strip() or "Новая точка",
        city=city.strip() or hq.city,
        address=address.strip(),
        category=hq.category or "shop",
        owner_max_user_id=hq.owner_max_user_id,
        inn=hq.inn,
        director_name=hq.director_name,
        website=hq.website,
        org_name=hq.org_name or hq.name,
        parent_id=hq.id,
        status=hq.status,
        verified_at=hq.verified_at,
    )
    session.add(point)
    await session.flush()
    stamps = program.stamps_required if program is not None else 5
    reward = program.reward_title if program is not None else "Подарок"
    session.add(
        LoyaltyProgram(
            business_id=point.id,
            kind="visits",
            title=f"{stamps} визитов — подарок",
            stamps_required=stamps,
            reward_title=reward,
            reward_bonus=program.reward_bonus if program is not None else 0,
        )
    )
    if latitude is not None and longitude is not None:
        await save_shop_location(session, point, latitude, longitude)
    return point


GAME_CATALOG = (
    {
        "slug": "welcome",
        "title": "Приветственный бонус",
        "blurb": "Баллы за первый визит — гость сразу чувствует заботу.",
        "rule": "1 визит",
        "icon": "gift",
        "kind": "visits",
        "goal": 1,
        "bonus": 50,
    },
    {
        "slug": "stamps",
        "title": "Карта штампов",
        "blurb": "Пять походов — и награда сама падает на карту.",
        "rule": "5 визитов",
        "icon": "cards",
        "kind": "visits",
        "goal": 5,
        "bonus": 80,
    },
    {
        "slug": "weekend",
        "title": "Любимый гость",
        "blurb": "Длинная серия для тех, кто ходит к вам постоянно.",
        "rule": "10 визитов",
        "icon": "trophy",
        "kind": "visits",
        "goal": 10,
        "bonus": 150,
    },
    {
        "slug": "wheel",
        "title": "Колесо удачи",
        "blurb": "После визита гость крутит колесо и ловит случайные баллы.",
        "rule": "случайный приз",
        "icon": "game",
        "kind": "wheel",
        "goal": 1,
        "bonus": 100,
    },
    {
        "slug": "invite",
        "title": "Приведи друга",
        "blurb": "Гость зовёт своего — баллы получают оба.",
        "rule": "1 друг",
        "icon": "users",
        "kind": "referral",
        "goal": 1,
        "bonus": 60,
    },
)


async def install_catalog_game(session: AsyncSession, shop: Business, slug: str) -> Challenge | None:
    spec = next((item for item in GAME_CATALOG if item["slug"] == slug), None)
    if spec is None:
        return None
    existing = await session.scalar(
        select(Challenge).where(Challenge.business_id == shop.id, Challenge.slug == slug)
    )
    if existing is not None:
        existing.is_active = True
        existing.title = spec["title"]
        existing.kind = spec["kind"]
        existing.goal = spec["goal"]
        existing.reward_bonus = spec["bonus"]
        return existing
    game = Challenge(
        business_id=shop.id,
        title=spec["title"],
        slug=spec["slug"],
        kind=spec["kind"],
        goal=spec["goal"],
        reward_bonus=spec["bonus"],
    )
    session.add(game)
    await session.flush()
    return game


async def visit_count(session: AsyncSession, customer_id: str, program_id: str) -> int:
    value = await session.scalar(
        select(func.count())
        .select_from(Visit)
        .where(
            Visit.customer_id == customer_id,
            Visit.program_id == program_id,
        )
    )
    return int(value or 0)


async def progress_for(session: AsyncSession, customer_id: str, program: LoyaltyProgram) -> int:
    receipts = (
        await session.scalars(
            select(Receipt).where(
                Receipt.customer_id == customer_id,
                Receipt.program_id == program.id,
                Receipt.kind == TicketKind.EARN.value,
            )
        )
    ).all()
    if program.kind == "amount":
        return sum(row.amount_rub for row in receipts)
    if program.kind == "quantity":
        return sum(row.qty for row in receipts)
    visits = await visit_count(session, customer_id, program.id)
    return visits + len(receipts)


async def ensure_customer(
    session: AsyncSession,
    shop: Business,
    user: AppUser,
    *,
    referral_code: str = "",
) -> Customer:
    customer = await session.scalar(
        select(Customer).where(
            Customer.business_id == shop.id, Customer.max_user_id == user.max_user_id
        )
    )
    created = customer is None
    if customer is None:
        customer = Customer(
            business_id=shop.id,
            max_user_id=user.max_user_id,
            display_name=user.display_name,
            username=user.username,
            bonus=0,
        )
        session.add(customer)
        await session.flush()
    if not customer.referral_code:
        for _ in range(8):
            code = secrets.token_hex(3)
            taken = await session.scalar(select(Customer.id).where(Customer.referral_code == code))
            if taken is None:
                customer.referral_code = code
                break
    code = (referral_code or "").strip().lower()
    if created and code and customer.referred_by is None:
        referrer = await session.scalar(
            select(Customer).where(
                Customer.business_id == shop.id, Customer.referral_code == code
            )
        )
        if referrer is not None and referrer.max_user_id != user.max_user_id:
            customer.referred_by = referrer.max_user_id
    return customer


async def guest_shop_profile(
    session: AsyncSession, shop: Business, guest: AppUser
) -> dict[str, Any]:
    customer = await ensure_customer(session, shop, guest)
    if guest.display_name:
        customer.display_name = guest.display_name
    if guest.username:
        customer.username = guest.username
    programs = await shop_programs(session, shop.id)
    receipts = (
        await session.scalars(
            select(Receipt)
            .where(Receipt.customer_id == customer.id, Receipt.kind == "earn")
            .order_by(Receipt.created_at.desc())
        )
    ).all()
    visits = (
        await session.scalars(
            select(Visit)
            .where(Visit.customer_id == customer.id)
            .order_by(Visit.created_at.desc())
        )
    ).all()
    last_at = receipts[0].created_at if receipts else (visits[0].created_at if visits else None)
    if last_at is not None and last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=UTC)
    promo_rows = []
    for program in programs:
        current = await progress_for(session, customer.id, program)
        promo_rows.append(
            {
                "title": program.title,
                "current": current,
                "goal": promo_goal(program),
                "rule": promo_rule(program),
            }
        )
    return {
        "customer_id": customer.id,
        "name": customer.display_name or guest.display_name or "Гость",
        "username": customer.username or guest.username or "",
        "bonus": customer.bonus,
        "purchases": len(receipts),
        "visits": len(receipts) or len(visits),
        "last": last_at.strftime("%d.%m.%Y %H:%M") if last_at else "Ещё без покупок",
        "promos": promo_rows,
    }


async def client_cards(session: AsyncSession, max_user_id: int) -> list[dict[str, Any]]:
    customers = (
        await session.scalars(select(Customer).where(Customer.max_user_id == max_user_id))
    ).all()
    cards: list[dict[str, Any]] = []
    for customer in customers:
        shop = await session.get(Business, customer.business_id)
        if shop is None:
            continue
        programs = await shop_programs(session, shop.id, include_archived=True)
        promo_rows = []
        for program in programs:
            current = await progress_for(session, customer.id, program)
            if program.archived_at is not None and current <= 0:
                continue
            if not program.is_active and program.archived_at is None and current <= 0:
                continue
            goal = promo_goal(program)
            redeemed = await reward_redemptions(session, customer.id, program.id)
            available = max(0, current // goal - redeemed)
            ready = available > 0
            promo_rows.append(
                {
                    "id": program.id,
                    "title": program.title,
                    "rule": promo_rule(program),
                    "current": current,
                    "goal": goal,
                    "unit": promo_unit(program),
                    "reward": program.reward_title,
                    "ready": ready,
                    "available": available,
                    "bonus_reward": program.reward_bonus,
                    "archived": program.archived_at is not None,
                }
            )
        cards.append(
            {
                "shop_id": shop.id,
                "name": shop.name,
                "city": shop.city,
                "address": shop.address,
                "bonus": customer.bonus,
                "customer_id": customer.id,
                "promos": promo_rows,
                "visits": promo_rows[0]["current"] if promo_rows else 0,
                "goal": promo_rows[0]["goal"] if promo_rows else 7,
                "reward": promo_rows[0]["reward"] if promo_rows else "Подарок",
                "ready": any(row["ready"] for row in promo_rows),
            }
        )
    cards.sort(key=lambda item: item["name"])
    return cards


async def reward_redemptions(session: AsyncSession, customer_id: str, program_id: str) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Receipt)
            .where(
                Receipt.customer_id == customer_id,
                Receipt.program_id == program_id,
                Receipt.kind == "reward",
            )
        )
        or 0
    )


async def shop_directory(session: AsyncSession) -> list[Business]:
    rows = (
        await session.scalars(
            select(Business)
            .where(Business.status == "verified")
            .order_by(Business.name)
        )
    ).all()
    return list(rows)


async def shop_public(session: AsyncSession, business_id: str) -> dict[str, Any] | None:
    shop = await session.get(Business, business_id)
    if shop is None or not shop_is_live(shop):
        return None
    products = await shop_products(session, shop.id)
    programs = await shop_programs(session, shop.id)
    groups: dict[str, list[Product]] = {}
    for product in products:
        groups.setdefault(product.group_name or "Основное", []).append(product)
    return {
        "shop": shop,
        "groups": groups,
        "programs": programs,
        "rules": [promo_rule(item) for item in programs],
    }


async def business_people(session: AsyncSession, shop: Business) -> list[dict[str, Any]]:
    people: list[dict[str, Any]] = []
    customers = (
        await session.scalars(select(Customer).where(Customer.business_id == shop.id))
    ).all()
    programs = await shop_programs(session, shop.id)
    for customer in customers:
        promo_bits = []
        for program in programs:
            current = await progress_for(session, customer.id, program)
            promo_bits.append(f"{program.title}: {current}/{promo_goal(program)}")
        people.append(
            {
                "id": customer.id,
                "name": customer.display_name or f"гость {customer.max_user_id}",
                "visits": promo_bits[0].split(": ")[-1] if promo_bits else "0",
                "goal": programs[0].stamps_required if programs else 7,
                "bonus": customer.bonus,
                "promos": promo_bits,
            }
        )
    people.sort(key=lambda item: item["name"])
    return people


async def shop_history(session: AsyncSession, shop: Business, limit: int = 40) -> list[Receipt]:
    rows = (
        await session.scalars(
            select(Receipt)
            .where(Receipt.business_id == shop.id)
            .order_by(Receipt.created_at.desc())
        )
    ).all()
    return list(rows)[:limit]


async def create_earn_ticket(
    session: AsyncSession,
    shop: Business,
    *,
    program_id: str | None,
    amount_rub: int,
    qty: int,
    items: str,
    place: str,
) -> Ticket:
    ticket = Ticket(
        kind=TicketKind.EARN.value,
        business_id=shop.id,
        program_id=program_id,
        amount_rub=max(0, amount_rub),
        qty=max(1, qty),
        items_json=json.dumps(
            [{"name": items.strip() or "Покупка", "qty": max(1, qty), "price": max(0, amount_rub)}],
            ensure_ascii=False,
        ),
        place=place.strip() or shop.address or shop.city or shop.name,
    )
    session.add(ticket)
    await session.flush()
    return ticket


async def create_redeem_ticket(
    session: AsyncSession, user: AppUser, shop: Business, bonus: int
) -> Ticket | None:
    customer = await ensure_customer(session, shop, user)
    bonus = min(bonus, customer.bonus or 0)
    if bonus <= 0:
        return None
    ticket = Ticket(
        kind=TicketKind.REDEEM.value,
        business_id=shop.id,
        customer_id=customer.id,
        max_user_id=user.max_user_id,
        bonus=bonus,
        place=shop.address or shop.name,
    )
    session.add(ticket)
    await session.flush()
    return ticket


async def credit_purchase(
    session: AsyncSession,
    *,
    shop: Business,
    customer: Customer,
    program: LoyaltyProgram | None,
    amount_rub: int,
    qty: int,
    items_json: str,
    place: str,
    note: str,
) -> ScanResult:
    previous = await progress_for(session, customer.id, program) if program else 0
    session.add(
        Receipt(
            business_id=shop.id,
            customer_id=customer.id,
            program_id=program.id if program else None,
            kind=TicketKind.EARN.value,
            amount_rub=max(0, amount_rub),
            qty=max(1, qty),
            items_json=items_json,
            place=place,
            note=note,
        )
    )
    await session.flush()
    if program:
        current = await progress_for(session, customer.id, program)
        goal = promo_goal(program)
        cycles = current // goal - previous // goal
        gift = cycles > 0
        if gift and program.reward_bonus:
            await session.execute(
                update(Customer)
                .where(Customer.id == customer.id)
                .values(bonus=Customer.bonus + program.reward_bonus * cycles)
            )
        text = f"«{shop.name}»: {promo_rule(program)}. Сейчас {current} из {goal}." + (
            f" {program.reward_title}!" if gift else ""
        )
        return ScanResult(
            ok=True, message=text, visits=current, goal=goal, reward=gift, shop=shop.name
        )
    return ScanResult(ok=True, message=f"Покупка в «{shop.name}» записана.", shop=shop.name)


async def record_purchase_for_guest(
    session: AsyncSession,
    *,
    shop: Business,
    guest: AppUser,
    program_id: str | None,
    amount_rub: int,
    qty: int,
    items: str,
    place: str,
) -> ScanResult:
    if guest.role == UserRole.NONE.value:
        guest.role = UserRole.CLIENT.value
    customer = await ensure_customer(session, shop, guest)
    await session.execute(select(Customer).where(Customer.id == customer.id).with_for_update())
    program = (
        await session.get(LoyaltyProgram, program_id)
        if program_id
        else await active_program(session, shop.id)
    )
    if program and program.business_id != shop.id:
        return ScanResult(ok=False, message="Акция больше не действует.")
    if program and not await program_usable(session, program, customer.id):
        return ScanResult(
            ok=False,
            message="Новых гостей акция уже не принимает, но накопленный прогресс сохранён.",
        )
    items_json = json.dumps(
        [{"name": items.strip() or "Покупка", "qty": max(1, qty), "price": max(0, amount_rub)}],
        ensure_ascii=False,
    )
    return await credit_purchase(
        session,
        shop=shop,
        customer=customer,
        program=program,
        amount_rub=amount_rub,
        qty=qty,
        items_json=items_json,
        place=place.strip() or shop.address or shop.city or shop.name,
        note="Покупка по QR гостя",
    )


async def apply_ticket(session: AsyncSession, *, user: AppUser, ticket: Ticket) -> ScanResult:
    shop = await session.get(Business, ticket.business_id)
    if shop is None:
        return ScanResult(ok=False, message="Точка не найдена.")
    if ticket.used:
        return ScanResult(ok=False, message="Этот QR уже использовали.")
    cashier = await can_scan_for_shop(session, user, shop)
    if ticket.kind == "reward":
        if not cashier:
            return ScanResult(ok=False, message="Выдачу подарка подтверждает кассир.")
        customer = await session.scalar(
            select(Customer).where(Customer.id == ticket.customer_id).with_for_update()
        )
        program = await session.get(LoyaltyProgram, ticket.program_id)
        if not customer or not program or program.business_id != shop.id:
            return ScanResult(ok=False, message="Подарок не найден.")
        earned = await progress_for(session, customer.id, program)
        used = await reward_redemptions(session, customer.id, program.id)
        if earned // promo_goal(program) <= used:
            return ScanResult(ok=False, message="Этот подарок уже получен.")
        claimed = await session.execute(
            update(Ticket).where(Ticket.id == ticket.id, Ticket.used.is_(False)).values(used=True)
        )
        if not claimed.rowcount:
            return ScanResult(ok=False, message="Этот QR уже использовали.")
        session.add(
            Receipt(
                business_id=shop.id,
                customer_id=customer.id,
                program_id=program.id,
                kind="reward",
                qty=0,
                note=f"Выдан подарок: {program.reward_title}",
            )
        )
        return ScanResult(
            ok=True,
            message=f"Выдай гостю {customer.display_name}: {program.reward_title}.",
            shop=shop.name,
        )
    if ticket.kind == TicketKind.REDEEM.value:
        if not cashier:
            return ScanResult(ok=False, message="Списание сканирует точка, не гость.")
        customer = await session.get(Customer, ticket.customer_id) if ticket.customer_id else None
        if customer is None or customer.bonus < ticket.bonus:
            return ScanResult(ok=False, message="Бонусов не хватает.")
        claimed = await session.execute(
            update(Ticket).where(Ticket.id == ticket.id, Ticket.used.is_(False)).values(used=True)
        )
        if not claimed.rowcount:
            return ScanResult(ok=False, message="Этот QR уже использовали.")
        debited = await session.execute(
            update(Customer)
            .where(Customer.id == customer.id, Customer.bonus >= ticket.bonus)
            .values(bonus=Customer.bonus - ticket.bonus)
        )
        if not debited.rowcount:
            await session.rollback()
            return ScanResult(ok=False, message="Бонусов не хватает.")
        session.add(
            Receipt(
                business_id=shop.id,
                customer_id=customer.id,
                program_id=ticket.program_id,
                kind=TicketKind.REDEEM.value,
                amount_rub=0,
                qty=0,
                items_json="[]",
                place=ticket.place,
                note=f"Списание {ticket.bonus} бонусов",
            )
        )
        return ScanResult(
            ok=True,
            message=f"Списано {ticket.bonus} бонусов у {customer.display_name}. Осталось {customer.bonus}.",
            shop=shop.name,
        )

    if await is_shop_member(session, user, shop):
        return ScanResult(ok=False, message="QR начисления сканирует гость, не точка.")
    if user.role == UserRole.NONE.value:
        user.role = UserRole.CLIENT.value
    customer = await ensure_customer(session, shop, user)
    await session.execute(select(Customer).where(Customer.id == customer.id).with_for_update())
    program = (
        await session.get(LoyaltyProgram, ticket.program_id)
        if ticket.program_id
        else await active_program(session, shop.id)
    )
    if program and program.business_id != shop.id:
        return ScanResult(ok=False, message="Акция больше не действует.")
    if program and not await program_usable(session, program, customer.id):
        return ScanResult(
            ok=False,
            message="Новых гостей акция уже не принимает, но накопленный прогресс сохранён.",
        )
    claimed = await session.execute(
        update(Ticket).where(Ticket.id == ticket.id, Ticket.used.is_(False)).values(used=True)
    )
    if not claimed.rowcount:
        return ScanResult(ok=False, message="Этот QR уже использовали.")
    ticket.customer_id = customer.id
    ticket.max_user_id = user.max_user_id
    return await credit_purchase(
        session,
        shop=shop,
        customer=customer,
        program=program,
        amount_rub=ticket.amount_rub,
        qty=ticket.qty,
        items_json=ticket.items_json,
        place=ticket.place,
        note="Начисление по QR",
    )


async def apply_scan(
    session: AsyncSession,
    *,
    user: AppUser,
    code: str,
    secret: str,
) -> ScanResult:
    ticket_id = parse_ticket_code(secret, code)
    if ticket_id:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            return ScanResult(ok=False, message="QR не найден.")
        return await apply_ticket(session, user=user, ticket=ticket)

    guest_id = parse_guest_code(secret, code)
    if guest_id == 0:
        return ScanResult(ok=False, message="Неверный код гостя.")
    if guest_id:
        shop = await shop_for_member(session, user.max_user_id)
        if shop is None or not await can_scan_for_shop(session, user, shop):
            return ScanResult(ok=False, message="Этот QR показывает гость кассиру.")
        if guest_id == user.max_user_id:
            return ScanResult(ok=False, message="Это твой код гостя. Его сканирует кассир.")
        guest = await session.scalar(select(AppUser).where(AppUser.max_user_id == guest_id))
        if guest is None:
            return ScanResult(
                ok=False, message="Гость ещё не заходил в Картыч. Пусть откроет приложение."
            )
        name = guest.display_name or "Гость"
        await guest_shop_profile(session, shop, guest)
        return ScanResult(
            ok=True,
            message=f"Гость {name} найден в базе «{shop.name}».",
            shop=shop.name,
            next_url=f"/biz/charge/{guest_id}",
        )

    business_id = parse_qr_payload(secret, code)
    if not business_id:
        return ScanResult(ok=False, message="Это не QR Картыч. Попроси показать код точки.")
    shop = await session.get(Business, business_id)
    if shop is None:
        return ScanResult(ok=False, message="Точка не найдена.")
    if await is_shop_member(session, user, shop):
        return ScanResult(
            ok=False, message="Свой QR сканировать не нужно — его показывает кассир гостю."
        )
    program = await active_program(session, shop.id)
    if program is None:
        return ScanResult(ok=False, message="У точки нет активной программы.")
    customer = await ensure_customer(session, shop, user)
    last = await session.scalar(
        select(Visit)
        .where(Visit.customer_id == customer.id, Visit.program_id == program.id)
        .order_by(Visit.created_at.desc())
    )
    now = datetime.now(UTC)
    if last is not None and last.created_at is not None:
        last_at = last.created_at if last.created_at.tzinfo else last.created_at.replace(tzinfo=UTC)
        if now - last_at < COOLDOWN:
            stamps = await visit_count(session, customer.id, program.id)
            return ScanResult(
                ok=False,
                message=f"Визит в «{shop.name}» уже засчитан. Следующий можно через несколько часов.",
                visits=stamps,
                goal=program.stamps_required,
                shop=shop.name,
            )
    if user.role == UserRole.NONE.value:
        user.role = UserRole.CLIENT.value
    session.add(
        Visit(
            customer_id=customer.id,
            program_id=program.id,
            source="qr",
            idempotency_key=f"{customer.id}:{program.id}:{uuid4()}",
        )
    )
    await session.flush()
    stamps = await visit_count(session, customer.id, program.id)
    gift = stamps > 0 and stamps % max(program.stamps_required, 1) == 0
    if gift:
        if program.reward_bonus:
            customer.bonus += program.reward_bonus
        text = f"«{shop.name}»: {stamps} из {program.stamps_required}. Сегодня {program.reward_title.lower()}!"
    else:
        left = program.stamps_required - (stamps % program.stamps_required)
        text = f"«{shop.name}»: {stamps} из {program.stamps_required}. Ещё {left} до подарка."
    return ScanResult(
        ok=True,
        message=text,
        visits=stamps,
        goal=program.stamps_required,
        reward=gift,
        shop=shop.name,
    )


async def add_product(
    session: AsyncSession, shop: Business, *, name: str, group_name: str, price_rub: int
) -> Product:
    product = Product(
        business_id=shop.id,
        name=name.strip() or "Товар",
        group_name=group_name.strip() or "Основное",
        price_rub=max(0, price_rub),
    )
    session.add(product)
    await session.flush()
    return product


async def update_product(
    session: AsyncSession,
    shop: Business,
    product_id: str,
    *,
    name: str,
    group_name: str,
    price_rub: int,
    active: bool,
) -> Product | None:
    product = await session.get(Product, product_id)
    if product is None or product.business_id != shop.id:
        return None
    product.name = name.strip() or product.name
    product.group_name = group_name.strip() or "Основное"
    product.price_rub = max(0, price_rub)
    product.is_active = active
    await session.flush()
    return product


async def archive_promo(session: AsyncSession, program: LoyaltyProgram) -> None:
    program.is_active = False
    program.archived_at = datetime.now(UTC)
    await session.flush()


async def add_promo(
    session: AsyncSession,
    shop: Business,
    *,
    kind: str,
    title: str,
    stamps: int,
    qty: int,
    amount: int,
    group_name: str,
    reward: str,
    bonus: int,
) -> LoyaltyProgram:
    kind = kind if kind in {"visits", "quantity", "amount", "stamp_card"} else "visits"
    if kind == "stamp_card":
        kind = "visits"
    program = LoyaltyProgram(
        business_id=shop.id,
        kind=kind,
        title=title.strip() or "Акция",
        stamps_required=max(1, stamps),
        qty_required=max(0, qty),
        amount_required=max(0, amount),
        group_name=group_name.strip(),
        reward_title=reward.strip() or "Подарок",
        reward_bonus=max(0, bonus),
        is_active=True,
    )
    session.add(program)
    await session.flush()
    return program


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def create_staff_invite(
    session: AsyncSession,
    shop: Business,
    *,
    created_by: int,
    can_stats: bool = False,
    can_earn: bool = False,
    can_scan: bool = True,
    can_edit: bool = False,
    schedule_days: str = DEFAULT_DAYS,
    shift_from: str = "10:00",
    shift_to: str = "22:00",
) -> ShopInvite:
    days, start, end = normalize_schedule(schedule_days, shift_from, shift_to)
    invite = ShopInvite(
        id=secrets.token_hex(8),
        business_id=shop.id,
        created_by=created_by,
        can_stats=can_stats,
        can_earn=can_earn,
        can_scan=can_scan,
        can_edit=can_edit,
        schedule_days=days,
        shift_from=start,
        shift_to=end,
    )
    session.add(invite)
    await session.flush()
    return invite


async def pending_invites(session: AsyncSession, shop: Business) -> list[ShopInvite]:
    rows = (
        await session.scalars(
            select(ShopInvite)
            .where(ShopInvite.business_id == shop.id, ShopInvite.used_by.is_(None))
            .order_by(ShopInvite.created_at.desc())
        )
    ).all()
    now = datetime.now(UTC)
    return [row for row in rows if now - _aware(row.created_at) <= INVITE_TTL]


async def list_shop_staff(session: AsyncSession, shop: Business) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(select(ShopStaff).where(ShopStaff.business_id == shop.id))
    ).all()
    people: list[dict[str, Any]] = []
    for row in rows:
        user = await session.scalar(select(AppUser).where(AppUser.max_user_id == row.max_user_id))
        people.append(
            {
                "id": row.id,
                "kind": row.kind,
                "max_user_id": row.max_user_id,
                "name": (user.display_name if user else "") or f"гость {row.max_user_id}",
                "username": user.username if user else None,
                "can_stats": row.can_stats,
                "can_earn": row.can_earn,
                "can_scan": row.can_scan,
                "can_edit": row.can_edit,
                "schedule_days": row.schedule_days,
                "shift_from": row.shift_from,
                "shift_to": row.shift_to,
                "schedule": schedule_label(row.schedule_days, row.shift_from, row.shift_to),
            }
        )
    people.sort(key=lambda item: (item["kind"] != "owner", item["name"]))
    return people


async def accept_staff_invite(
    session: AsyncSession, user: AppUser, token: str
) -> tuple[bool, str]:
    invite = await session.get(ShopInvite, token)
    if invite is None:
        return False, "Ссылка приглашения недействительна."
    if datetime.now(UTC) - _aware(invite.created_at) > INVITE_TTL:
        return False, "Ссылка истекла. Попроси новую."
    if invite.used_by and invite.used_by != user.max_user_id:
        return False, "Эту ссылку уже использовали."
    shop = await session.get(Business, invite.business_id)
    if shop is None:
        return False, "Точка не найдена."
    if shop.owner_max_user_id == user.max_user_id:
        return False, "Ты уже владелец этой точки."
    existing = await staff_for(session, user.max_user_id)
    if existing is not None and existing.business_id != shop.id:
        return False, "Ты уже кассир другой точки."
    if existing is None:
        session.add(
            ShopStaff(
                business_id=shop.id,
                max_user_id=user.max_user_id,
                kind="cashier",
                can_stats=invite.can_stats,
                can_earn=invite.can_earn,
                can_scan=invite.can_scan,
                can_edit=invite.can_edit,
                schedule_days=invite.schedule_days,
                shift_from=invite.shift_from,
                shift_to=invite.shift_to,
            )
        )
    else:
        existing.can_stats = invite.can_stats
        existing.can_earn = invite.can_earn
        existing.can_scan = invite.can_scan
        existing.can_edit = invite.can_edit
        existing.schedule_days = invite.schedule_days
        existing.shift_from = invite.shift_from
        existing.shift_to = invite.shift_to
    invite.used_by = user.max_user_id
    user.role = UserRole.BUSINESS.value
    await session.flush()
    return True, f"Ты кассир «{shop.name}». Можно показывать QR и сканировать код гостя."


async def join_promo(
    session: AsyncSession, user: AppUser, program_id: str
) -> tuple[bool, str, Business | None]:
    program = await session.get(LoyaltyProgram, program_id)
    if program is None or not program_open(program):
        if program is not None and program.archived_at is not None:
            shop = await session.get(Business, program.business_id)
            if shop is not None and shop.owner_max_user_id == user.max_user_id:
                return True, f"Это твоя акция «{program.title}».", shop
            customer = await session.scalar(
                select(Customer).where(
                    Customer.business_id == program.business_id,
                    Customer.max_user_id == user.max_user_id,
                )
            )
            if (
                shop is not None
                and customer is not None
                and await progress_for(session, customer.id, program) > 0
            ):
                return True, f"Твой прогресс по «{program.title}» сохранён.", shop
            return False, "Акцию сняли, но накопленные визиты гостей не сгорают.", None
        return False, "Акция недоступна или на паузе.", None
    shop = await session.get(Business, program.business_id)
    if shop is None:
        return False, "Точка не найдена.", None
    if not shop_is_live(shop) and shop.owner_max_user_id != user.max_user_id:
        return False, "Эта точка ещё не подтверждена.", None
    if shop.owner_max_user_id == user.max_user_id:
        return True, f"Это твоя акция «{program.title}».", shop
    if user.role == UserRole.NONE.value:
        user.role = UserRole.CLIENT.value
    await ensure_customer(session, shop, user)
    return True, f"Ты в акции «{program.title}» точки «{shop.name}».", shop


async def create_promo_link(
    session: AsyncSession, shop: Business, program: LoyaltyProgram, *, created_by: int
) -> PromoLink:
    link = PromoLink(
        id=secrets.token_hex(8),
        program_id=program.id,
        business_id=shop.id,
        created_by=created_by,
    )
    session.add(link)
    await session.flush()
    return link


async def recent_promo_links(
    session: AsyncSession, shop: Business
) -> dict[str, list[PromoLink]]:
    rows = (
        await session.scalars(
            select(PromoLink)
            .where(PromoLink.business_id == shop.id)
            .order_by(PromoLink.created_at.desc())
        )
    ).all()
    now = datetime.now(UTC)
    grouped: dict[str, list[PromoLink]] = {}
    for row in rows:
        if now - _aware(row.created_at) > INVITE_TTL:
            continue
        grouped.setdefault(row.program_id, []).append(row)
    return grouped


async def resolve_promo_link(
    session: AsyncSession, token: str
) -> tuple[PromoLink | None, LoyaltyProgram | None, Business | None]:
    link = await session.get(PromoLink, token)
    if link is None:
        return None, None, None
    if datetime.now(UTC) - _aware(link.created_at) > INVITE_TTL:
        return link, None, None
    program = await session.get(LoyaltyProgram, link.program_id)
    shop = await session.get(Business, link.business_id)
    return link, program, shop


async def join_promo_token(
    session: AsyncSession, user: AppUser, token: str
) -> tuple[bool, str, Business | None]:
    _link, program, shop = await resolve_promo_link(session, token)
    if program is None or shop is None:
        return False, "Ссылка истекла или недействительна. Попроси новую.", None
    return await join_promo(session, user, program.id)


async def geocode_address(query: str) -> dict[str, Any] | None:
    text = (query or "").strip()
    if len(text) < 3:
        return None
    import httpx

    async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "Kartych/1.0"}) as client:
        response = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": text, "format": "json", "limit": 1, "addressdetails": 1},
        )
        response.raise_for_status()
        rows = response.json()
    if not rows:
        return None
    row = rows[0]
    return {
        "lat": float(row["lat"]),
        "lng": float(row["lon"]),
        "label": str(row.get("display_name") or text),
    }


async def save_shop_location(
    session: AsyncSession, shop: Business, latitude: float, longitude: float
) -> None:
    loc = await session.get(BusinessLocation, shop.id)
    if loc:
        loc.latitude, loc.longitude = latitude, longitude
    else:
        session.add(BusinessLocation(business_id=shop.id, latitude=latitude, longitude=longitude))


async def apply_for_business(
    session: AsyncSession,
    owner: AppUser,
    *,
    name: str,
    city: str,
    address: str,
    inn: str,
    director_name: str,
    website: str,
    latitude: float | None,
    longitude: float | None,
) -> tuple[Business, str]:
    shop = await shop_for_owner(session, owner.max_user_id)
    if shop is not None and shop.status == "verified":
        return shop, "already"
    if shop is None:
        shop = await create_shop(session, owner, name=name, city=city, verified=False)
    shop.name = name.strip() or shop.name
    shop.city = city.strip()
    shop.address = address.strip()
    shop.org_name = shop.org_name or shop.name
    shop.inn = inn_digits(inn)
    shop.director_name = director_name.strip()
    shop.website = website.strip()[:240]
    shop.status = "pending"
    shop.verified_at = None
    if owner.role == UserRole.NONE.value:
        owner.role = UserRole.CLIENT.value
    if latitude is not None and longitude is not None:
        await save_shop_location(session, shop, latitude, longitude)
    await session.flush()
    return shop, "pending"


async def pending_businesses(session: AsyncSession) -> list[Business]:
    rows = (
        await session.scalars(
            select(Business).where(Business.status == "pending").order_by(Business.created_at.desc())
        )
    ).all()
    return list(rows)


def _shop_search_blob(shop: Business) -> str:
    return " ".join(
        part
        for part in (
            shop.name,
            shop.org_name,
            shop.city,
            shop.address,
            shop.website,
            shop.inn,
            shop.director_name,
            shop.category,
        )
        if part
    ).casefold()


def _stem_match(shop: Business, needle: str) -> bool:
    stem = needle.rstrip("аеиоуыэюяйьъ")
    return len(stem) >= 4 and stem in _shop_search_blob(shop)


async def search_businesses(session: AsyncSession, query: str, *, limit: int = 80) -> list[Business]:
    text = (query or "").strip()
    if not text:
        return await pending_businesses(session)
    needle = text.casefold()
    rows = (
        await session.scalars(select(Business).order_by(Business.created_at.desc()))
    ).all()
    found = [shop for shop in rows if needle in _shop_search_blob(shop) or _stem_match(shop, needle)]
    return found[:limit]


async def set_shop_review(
    session: AsyncSession, shop: Business, *, approved: bool
) -> AppUser | None:
    owner = (
        await session.scalar(select(AppUser).where(AppUser.max_user_id == shop.owner_max_user_id))
        if shop.owner_max_user_id
        else None
    )
    if approved:
        shop.status = "verified"
        shop.verified_at = datetime.now(UTC)
        if owner is not None:
            owner.role = UserRole.BUSINESS.value
            await ensure_owner_staff(session, shop)
    else:
        shop.status = "rejected"
        shop.verified_at = None
    await session.flush()
    return owner


async def extra_admin_ids(session: AsyncSession) -> set[int]:
    return set(await session.scalars(select(PlatformAdmin.max_user_id)))


async def platform_admin_ids(session: AsyncSession, settings) -> set[int]:
    return set(settings.admin_ids()) | await extra_admin_ids(session)


async def claim_first_admin(session: AsyncSession, max_user_id: int, settings) -> tuple[bool, str]:
    if max_user_id <= 0:
        return False, "Сначала войди через MAX."
    if await platform_admin_ids(session, settings):
        return False, "Админ уже есть. Попроси его добавить тебя в списке."
    session.add(PlatformAdmin(max_user_id=max_user_id, added_by=max_user_id))
    await session.flush()
    return True, "Готово. Это твоя админ-панель. Здесь карточки заявок."


async def add_platform_admin(
    session: AsyncSession, max_user_id: int, *, added_by: int, pinned: set[int]
) -> tuple[bool, str]:
    if max_user_id <= 0:
        return False, "Нужен MAX id — положительное число из профиля MAX."
    if max_user_id in pinned or await session.get(PlatformAdmin, max_user_id) is not None:
        return False, "Этот человек уже админ."
    session.add(PlatformAdmin(max_user_id=max_user_id, added_by=added_by))
    await session.flush()
    return True, "Админ добавлен. Он увидит заявки после входа через MAX."


async def remove_platform_admin(
    session: AsyncSession, max_user_id: int, *, pinned: set[int]
) -> tuple[bool, str]:
    if max_user_id in pinned:
        return False, "Этого админа задали в настройках сервера. Его нельзя снять здесь."
    row = await session.get(PlatformAdmin, max_user_id)
    if row is None:
        return False, "Такого админа нет в списке."
    await session.delete(row)
    await session.flush()
    return True, "Админ снят."


async def get_telegram_config(session: AsyncSession) -> TelegramSetting | None:
    return await session.get(TelegramSetting, 1)


async def telegram_alert_chats(session: AsyncSession, settings) -> set[int]:
    ids = set(settings.telegram_ids())
    ids |= set(await session.scalars(select(TelegramSubscriber.chat_id)))
    return ids


async def list_telegram_people(session: AsyncSession, settings) -> list[dict[str, Any]]:
    pinned = settings.telegram_ids()
    rows = (await session.scalars(select(TelegramSubscriber).order_by(TelegramSubscriber.created_at.desc()))).all()
    seen: set[int] = set()
    people: list[dict[str, Any]] = []
    for row in rows:
        seen.add(row.chat_id)
        handle = f"@{row.username}" if row.username else ""
        people.append(
            {
                "id": row.chat_id,
                "name": row.display_name or handle or f"Telegram {row.chat_id}",
                "username": row.username or "",
                "pinned": row.chat_id in pinned,
            }
        )
    for chat_id in sorted(pinned - seen):
        people.append({"id": chat_id, "name": f"Telegram {chat_id}", "username": "", "pinned": True})
    return people


async def save_telegram_bot(
    session: AsyncSession, *, token: str, username: str, webhook_secret: str
) -> TelegramSetting:
    row = await session.get(TelegramSetting, 1)
    if row is None:
        row = TelegramSetting(id=1)
        session.add(row)
    row.bot_token = token
    row.bot_username = username
    row.webhook_secret = webhook_secret
    await session.flush()
    return row


async def add_telegram_subscriber(
    session: AsyncSession,
    chat_id: int,
    *,
    display_name: str = "",
    username: str | None = None,
    added_by: int = 0,
) -> TelegramSubscriber:
    row = await session.get(TelegramSubscriber, chat_id)
    if row is None:
        row = TelegramSubscriber(chat_id=chat_id, added_by=added_by)
        session.add(row)
    row.display_name = (display_name or row.display_name or "").strip()[:160]
    if username:
        row.username = username.lstrip("@")[:160]
    if added_by and not row.added_by:
        row.added_by = added_by
    await session.flush()
    return row


async def remove_telegram_subscriber(
    session: AsyncSession, chat_id: int, *, pinned: set[int]
) -> tuple[bool, str]:
    if chat_id in pinned:
        return False, "Этого человека задали в настройках сервера. Его нельзя снять здесь."
    row = await session.get(TelegramSubscriber, chat_id)
    if row is None:
        return False, "Такого получателя нет в списке."
    await session.delete(row)
    await session.flush()
    return True, "Больше не пишем этому человеку в Telegram."
