from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.db import Base
from app.db import models as _models  # noqa: F401
from datetime import UTC, datetime
from app.db.models import Business, BusinessLocation, LoyaltyProgram, Product


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(settings.database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def ping_db(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True


async def create_all(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for sql in (
            "ALTER TABLE businesses ADD COLUMN city VARCHAR(160) DEFAULT ''",
            "ALTER TABLE businesses ADD COLUMN address VARCHAR(200) DEFAULT ''",
            "ALTER TABLE businesses ADD COLUMN category VARCHAR(40) DEFAULT 'shop'",
            "ALTER TABLE businesses ADD COLUMN inn VARCHAR(12) DEFAULT ''",
            "ALTER TABLE businesses ADD COLUMN director_name VARCHAR(160) DEFAULT ''",
            "ALTER TABLE businesses ADD COLUMN verified_at DATETIME",
            "ALTER TABLE businesses ADD COLUMN website VARCHAR(240) DEFAULT ''",
            "ALTER TABLE businesses ADD COLUMN status VARCHAR(20) DEFAULT 'verified'",
            "ALTER TABLE loyalty_programs ADD COLUMN qty_required INTEGER DEFAULT 0",
            "ALTER TABLE loyalty_programs ADD COLUMN amount_required INTEGER DEFAULT 0",
            "ALTER TABLE loyalty_programs ADD COLUMN product_id VARCHAR(36)",
            "ALTER TABLE loyalty_programs ADD COLUMN group_name VARCHAR(80) DEFAULT ''",
            "ALTER TABLE loyalty_programs ADD COLUMN reward_bonus INTEGER DEFAULT 0",
            "ALTER TABLE loyalty_programs ADD COLUMN archived_at DATETIME",
            "ALTER TABLE customers ADD COLUMN bonus INTEGER DEFAULT 0",
            "ALTER TABLE shop_staff ADD COLUMN schedule_days VARCHAR(80) DEFAULT 'mon,tue,wed,thu,fri'",
            "ALTER TABLE shop_staff ADD COLUMN shift_from VARCHAR(5) DEFAULT '10:00'",
            "ALTER TABLE shop_staff ADD COLUMN shift_to VARCHAR(5) DEFAULT '22:00'",
            "ALTER TABLE shop_invites ADD COLUMN schedule_days VARCHAR(80) DEFAULT 'mon,tue,wed,thu,fri'",
            "ALTER TABLE shop_invites ADD COLUMN shift_from VARCHAR(5) DEFAULT '10:00'",
            "ALTER TABLE shop_invites ADD COLUMN shift_to VARCHAR(5) DEFAULT '22:00'",
        ):
            # Inspect first: a duplicate-column error aborts a PostgreSQL transaction.
            table, column = sql.split()[2], sql.split()[5]

            def _columns(sync, name=table):
                inspector = inspect(sync)
                if name not in inspector.get_table_names():
                    return set()
                return {c["name"] for c in inspector.get_columns(name)}

            columns = await conn.run_sync(_columns)
            if columns and column not in columns:
                await conn.execute(text(sql))


async def seed_demo_shops(session: AsyncSession) -> None:
    from sqlalchemy import func, select

    for old_name, new_name, city in (
        ("Зерно", "Точка на Ленина", "Москва"),
        ("Молоко и мёд", "Маркет 12", "Казань"),
        ("Седьмая чашка", "Студия", "Санкт-Петербург"),
        ("Кофейня на углу", "Моя точка", "Москва"),
    ):
        row = await session.scalar(select(Business).where(Business.name == old_name))
        if row is not None:
            row.name = new_name
            row.city = city
            row.category = "shop"
    wanted = [
        ("Точка на Ленина", "Москва", "Ленина, 10", 55.7558, 37.6173),
        ("Маркет 12", "Казань", "Баумана, 12", 55.7963, 49.1088),
        ("Студия", "Санкт-Петербург", "Невский, 8", 59.9343, 30.3351),
    ]
    for name, city, address, lat, lng in wanted:
        shop = await session.scalar(select(Business).where(Business.name == name))
        if shop is None:
            shop = Business(
                name=name,
                city=city,
                address=address,
                category="shop",
                owner_max_user_id=None,
                status="verified",
                verified_at=datetime.now(UTC),
            )
            session.add(shop)
            await session.flush()
            session.add(
                LoyaltyProgram(
                    business_id=shop.id,
                    kind="visits",
                    title="5 визитов — подарок",
                    stamps_required=5,
                    reward_title="Подарок",
                    reward_bonus=50,
                )
            )
            if name == "Маркет 12":
                session.add(
                    LoyaltyProgram(
                        business_id=shop.id,
                        kind="amount",
                        title="Набери 2000 ₽",
                        stamps_required=1,
                        amount_required=2000,
                        reward_title="Скидка 300 ₽",
                        reward_bonus=300,
                    )
                )
                session.add(
                    LoyaltyProgram(
                        business_id=shop.id,
                        kind="quantity",
                        title="3 кофе",
                        stamps_required=1,
                        qty_required=3,
                        group_name="Напитки",
                        reward_title="Кофе в подарок",
                        reward_bonus=100,
                    )
                )
        elif not shop.address:
            shop.address = address
            shop.city = city
        shop.status = "verified"
        if shop.verified_at is None:
            shop.verified_at = datetime.now(UTC)
        loc = await session.get(BusinessLocation, shop.id)
        if loc is None:
            session.add(BusinessLocation(business_id=shop.id, latitude=lat, longitude=lng))
    shops = (await session.scalars(select(Business))).all()
    for shop in shops:
        products = await session.scalar(
            select(func.count()).select_from(Product).where(Product.business_id == shop.id)
        )
        if int(products or 0) == 0:
            session.add(
                Product(business_id=shop.id, name="Кофе", group_name="Напитки", price_rub=180)
            )
            session.add(
                Product(business_id=shop.id, name="Сэндвич", group_name="Еда", price_rub=250)
            )
        if not shop.address:
            shop.address = shop.city
    await session.commit()


async def session_iter(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session
