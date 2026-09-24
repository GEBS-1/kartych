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
from app.db.models import Business, LoyaltyProgram, Product


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
            "ALTER TABLE loyalty_programs ADD COLUMN qty_required INTEGER DEFAULT 0",
            "ALTER TABLE loyalty_programs ADD COLUMN amount_required INTEGER DEFAULT 0",
            "ALTER TABLE loyalty_programs ADD COLUMN product_id VARCHAR(36)",
            "ALTER TABLE loyalty_programs ADD COLUMN group_name VARCHAR(80) DEFAULT ''",
            "ALTER TABLE loyalty_programs ADD COLUMN reward_bonus INTEGER DEFAULT 0",
            "ALTER TABLE customers ADD COLUMN bonus INTEGER DEFAULT 0",
        ):
            # Inspect first: a duplicate-column error aborts a PostgreSQL transaction.
            table, column = sql.split()[2], sql.split()[5]
            columns = await conn.run_sync(
                lambda sync, name=table: {c["name"] for c in inspect(sync).get_columns(name)}
            )
            if column not in columns:
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
        ("Точка на Ленина", "Москва", "Ленина, 10"),
        ("Маркет 12", "Казань", "Баумана, 12"),
        ("Студия", "Санкт-Петербург", "Невский, 8"),
    ]
    for name, city, address in wanted:
        shop = await session.scalar(select(Business).where(Business.name == name))
        if shop is None:
            shop = Business(
                name=name, city=city, address=address, category="shop", owner_max_user_id=None
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
