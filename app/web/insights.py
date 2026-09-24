from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import AppUser, Business, Challenge, ChallengeClaim, Customer, Receipt, Visit


def utc(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def activity(session, business_id=None, user_id=None):
    """One event per confirmed receipt or legacy visit, never per issued QR."""
    visits = select(
        Visit.created_at,
        Customer.id,
        Customer.max_user_id,
        Customer.display_name,
        Customer.business_id,
    ).join(Customer, Visit.customer_id == Customer.id)
    receipts = (
        select(
            Receipt.created_at,
            Customer.id,
            Customer.max_user_id,
            Customer.display_name,
            Customer.business_id,
        )
        .join(Customer, Receipt.customer_id == Customer.id)
        .where(Receipt.kind == "earn")
    )
    for name, value in (("business_id", business_id), ("max_user_id", user_id)):
        if value is not None:
            visits = visits.where(getattr(Customer, name) == value)
            receipts = receipts.where(getattr(Customer, name) == value)
    return [
        {"at": utc(row[0]), "customer": row[1], "uid": row[2], "name": row[3], "shop": row[4]}
        for row in (await session.execute(visits.union_all(receipts))).all()
    ]


async def analytics(session, shop, period):
    days = {"week": 7, "month": 30}.get(period, 7)
    now = datetime.now(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    events = await activity(session, business_id=shop.id) if shop else []
    current = [e for e in events if start <= e["at"] <= now]
    previous = [e for e in events if start - timedelta(days=days) <= e["at"] < start]
    customers = (
        list((await session.scalars(select(Customer).where(Customer.business_id == shop.id))).all())
        if shop
        else []
    )
    counts = Counter(e["customer"] for e in current)
    old = {e["customer"] for e in events if e["at"] < start}
    returning = sum(1 for cid, count in counts.items() if cid in old or count > 1)
    # Redemption receipts carry the operation time; ticket creation time can differ.
    redemptions = (
        list(
            (
                await session.scalars(
                    select(Receipt).where(
                        Receipt.business_id == shop.id,
                        Receipt.kind == "redeem",
                        Receipt.created_at >= start,
                    )
                )
            ).all()
        )
        if shop
        else []
    )
    redeemed = sum(
        int(r.note.split()[1])
        for r in redemptions
        if r.note.startswith("Списание ") and r.note.split()[1].isdigit()
    )
    per_day = Counter(e["at"].date() for e in current)
    peak = max(per_day.values(), default=1)
    chart = [
        {
            "label": (start + timedelta(days=i)).strftime("%d.%m"),
            "count": per_day[(start + timedelta(days=i)).date()],
            "height": round(per_day[(start + timedelta(days=i)).date()] / peak * 100),
        }
        for i in range(days)
    ]
    people = []
    for customer in customers:
        mine = [e for e in events if e["customer"] == customer.id]
        last = max((e["at"] for e in mine), default=None)
        people.append(
            {
                "name": customer.display_name or "Гость",
                "visits": len(mine),
                "bonus": customer.bonus,
                "last": last.strftime("%d.%m.%Y") if last else "Без покупок",
            }
        )
    people.sort(key=lambda p: (-p["visits"], p["name"]))
    return {
        "visits": len(current),
        "change": len(current) - len(previous),
        "new": sum(1 for c in customers if utc(c.created_at) >= start),
        "returning": round(returning / len(counts) * 100) if counts else 0,
        "redeemed": redeemed,
        "chart": chart,
        "people": people,
        "guests": len(customers),
        "period": period,
        "days": days,
    }


async def league(session, uid, period="week"):
    now = datetime.now(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start -= timedelta(days=start.weekday())
    elif period == "season":
        start = start.replace(month=((start.month - 1) // 3) * 3 + 1, day=1)
    events = [e for e in await activity(session) if start <= e["at"] <= now]
    counts = Counter(e["uid"] for e in events)
    users = {u.max_user_id: u for u in (await session.scalars(select(AppUser))).all()}
    rows = []
    for rank, (user_id, count) in enumerate(
        sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])), 1
    ):
        user = users.get(user_id)
        name = user.display_name.split()[0] if user and user.display_name else "Гость"
        rows.append({"rank": rank, "name": name, "xp": count * 10, "mine": user_id == uid})
    mine = next((r for r in rows if r["mine"]), {"rank": None, "xp": 0})
    visible = rows[:50]
    if mine.get("rank") and mine["rank"] > 50:
        visible.append(mine)
    return {"rows": visible, "mine": mine, "period": period}


async def games(session, uid, business_id=None):
    query = select(Challenge, Business.name).join(Business, Challenge.business_id == Business.id)
    if business_id:
        query = query.where(Challenge.business_id == business_id)
    else:
        query = query.where(Challenge.is_active.is_(True))
    events = await activity(session, user_id=uid)
    claims = set(
        (
            await session.scalars(
                select(ChallengeClaim.challenge_id)
                .join(Customer, ChallengeClaim.customer_id == Customer.id)
                .where(Customer.max_user_id == uid)
            )
        ).all()
    )
    return [
        {
            "game": game,
            "shop": name,
            "progress": sum(
                1
                for e in events
                if e["shop"] == game.business_id and e["at"] >= utc(game.created_at)
            ),
            "claimed": game.id in claims,
        }
        for game, name in (await session.execute(query.order_by(Challenge.created_at.desc()))).all()
    ]
