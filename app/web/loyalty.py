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
    Customer,
    LoyaltyProgram,
    Product,
    Receipt,
    PromoLink,
    ShopInvite,
    ShopStaff,
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


async def shop_for_owner(session: AsyncSession, max_user_id: int) -> Business | None:
    return await session.scalar(select(Business).where(Business.owner_max_user_id == max_user_id))


async def staff_for(session: AsyncSession, max_user_id: int) -> ShopStaff | None:
    return await session.scalar(select(ShopStaff).where(ShopStaff.max_user_id == max_user_id))


async def ensure_owner_staff(session: AsyncSession, shop: Business) -> None:
    if shop.owner_max_user_id is None:
        return
    row = await session.scalar(
        select(ShopStaff).where(
            ShopStaff.business_id == shop.id,
            ShopStaff.max_user_id == shop.owner_max_user_id,
        )
    )
    if row is None:
        session.add(
            ShopStaff(
                business_id=shop.id,
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
    row.kind = "owner"
    row.can_stats = True
    row.can_earn = True
    row.can_scan = True
    row.can_edit = True


async def shop_for_member(session: AsyncSession, max_user_id: int) -> Business | None:
    staff = await staff_for(session, max_user_id)
    if staff is not None:
        shop = await session.get(Business, staff.business_id)
        if shop is not None:
            await ensure_owner_staff(session, shop)
            return shop
    shop = await shop_for_owner(session, max_user_id)
    if shop is not None:
        await ensure_owner_staff(session, shop)
    return shop


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
        )
    )


async def shop_programs(session: AsyncSession, business_id: str) -> list[LoyaltyProgram]:
    rows = (
        await session.scalars(
            select(LoyaltyProgram).where(
                LoyaltyProgram.business_id == business_id,
                LoyaltyProgram.is_active.is_(True),
            )
        )
    ).all()
    return list(rows)


async def shop_products(session: AsyncSession, business_id: str) -> list[Product]:
    rows = (
        await session.scalars(
            select(Product)
            .where(Product.business_id == business_id, Product.is_active.is_(True))
            .order_by(Product.group_name, Product.name)
        )
    ).all()
    return list(rows)


async def create_shop(
    session: AsyncSession,
    owner: AppUser,
    *,
    name: str,
    city: str,
    stamps: int = 7,
    reward: str = "Подарок",
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
        owner.role = UserRole.BUSINESS.value
        await ensure_owner_staff(session, existing)
        return existing
    shop = Business(
        name=name.strip() or "Моя точка",
        city=city.strip(),
        category="shop",
        owner_max_user_id=owner.max_user_id,
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
    owner.role = UserRole.BUSINESS.value
    await ensure_owner_staff(session, shop)
    return shop


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


async def ensure_customer(session: AsyncSession, shop: Business, user: AppUser) -> Customer:
    customer = await session.scalar(
        select(Customer).where(
            Customer.business_id == shop.id, Customer.max_user_id == user.max_user_id
        )
    )
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
    return customer


async def client_cards(session: AsyncSession, max_user_id: int) -> list[dict[str, Any]]:
    customers = (
        await session.scalars(select(Customer).where(Customer.max_user_id == max_user_id))
    ).all()
    cards: list[dict[str, Any]] = []
    for customer in customers:
        shop = await session.get(Business, customer.business_id)
        if shop is None:
            continue
        programs = await shop_programs(session, shop.id)
        promo_rows = []
        for program in programs:
            current = await progress_for(session, customer.id, program)
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
    rows = (await session.scalars(select(Business).order_by(Business.name))).all()
    return list(rows)


async def shop_public(session: AsyncSession, business_id: str) -> dict[str, Any] | None:
    shop = await session.get(Business, business_id)
    if shop is None:
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
    if program and (program.business_id != shop.id or not program.is_active):
        return ScanResult(ok=False, message="Акция больше не действует.")
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
    if program and (program.business_id != shop.id or not program.is_active):
        return ScanResult(ok=False, message="Акция больше не действует.")
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
        return ScanResult(
            ok=True,
            message=f"Гость {name}. Запиши покупку.",
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
    can_stats: bool,
    can_earn: bool,
    can_scan: bool,
    can_edit: bool,
) -> ShopInvite:
    invite = ShopInvite(
        id=secrets.token_hex(8),
        business_id=shop.id,
        created_by=created_by,
        can_stats=can_stats,
        can_earn=can_earn,
        can_scan=can_scan,
        can_edit=can_edit,
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
            )
        )
    else:
        existing.can_stats = invite.can_stats
        existing.can_earn = invite.can_earn
        existing.can_scan = invite.can_scan
        existing.can_edit = invite.can_edit
    invite.used_by = user.max_user_id
    await session.flush()
    return True, f"Ты кассир «{shop.name}». Можно показывать QR и считать код гостя."


async def join_promo(
    session: AsyncSession, user: AppUser, program_id: str
) -> tuple[bool, str, Business | None]:
    program = await session.get(LoyaltyProgram, program_id)
    if program is None or not program.is_active:
        return False, "Акция недоступна или на паузе.", None
    shop = await session.get(Business, program.business_id)
    if shop is None:
        return False, "Точка не найдена.", None
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
