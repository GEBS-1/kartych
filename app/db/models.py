from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ProgramKind(StrEnum):
    STAMP_CARD = "stamp_card"
    VISITS = "visits"
    QUANTITY = "quantity"
    AMOUNT = "amount"
    REFERRAL = "referral"
    PRODUCT_OF_WEEK = "product_of_week"


class UserRole(StrEnum):
    NONE = "none"
    CLIENT = "client"
    BUSINESS = "business"


class TicketKind(StrEnum):
    EARN = "earn"
    REDEEM = "redeem"


def _id() -> str:
    return str(uuid4())


class AppUser(Base):
    __tablename__ = "app_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    max_user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(160), default="")
    username: Mapped[str | None] = mapped_column(String(160), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default=UserRole.NONE.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(160))
    city: Mapped[str] = mapped_column(String(160), default="")
    address: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(40), default="shop")
    owner_max_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    programs: Mapped[list[LoyaltyProgram]] = relationship(back_populates="business")
    products: Mapped[list[Product]] = relationship(back_populates="business")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    business_id: Mapped[str] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    group_name: Mapped[str] = mapped_column(String(80), default="Основное")
    price_rub: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    business: Mapped[Business] = relationship(back_populates="products")


class LoyaltyProgram(Base):
    __tablename__ = "loyalty_programs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(40), default=ProgramKind.VISITS.value)
    title: Mapped[str] = mapped_column(String(160))
    stamps_required: Mapped[int] = mapped_column(Integer, default=7)
    qty_required: Mapped[int] = mapped_column(Integer, default=0)
    amount_required: Mapped[int] = mapped_column(Integer, default=0)
    product_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    group_name: Mapped[str] = mapped_column(String(80), default="")
    reward_title: Mapped[str] = mapped_column(String(160), default="Подарок")
    reward_bonus: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    business: Mapped[Business] = relationship(back_populates="programs")


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("business_id", "max_user_id", name="uq_customer_business_max"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    max_user_id: Mapped[int] = mapped_column(Integer, index=True)
    display_name: Mapped[str] = mapped_column(String(160), default="")
    username: Mapped[str | None] = mapped_column(String(160), nullable=True)
    bonus: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Visit(Base):
    __tablename__ = "visits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    program_id: Mapped[str] = mapped_column(ForeignKey("loyalty_programs.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(40), default="qr")
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    business_id: Mapped[str] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[str | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    program_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), default=TicketKind.EARN.value)
    amount_rub: Mapped[int] = mapped_column(Integer, default=0)
    qty: Mapped[int] = mapped_column(Integer, default=1)
    items_json: Mapped[str] = mapped_column(Text, default="[]")
    place: Mapped[str] = mapped_column(String(200), default="")
    note: Mapped[str] = mapped_column(String(240), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    kind: Mapped[str] = mapped_column(String(20), default=TicketKind.EARN.value)
    business_id: Mapped[str] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    program_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    max_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_rub: Mapped[int] = mapped_column(Integer, default=0)
    qty: Mapped[int] = mapped_column(Integer, default=1)
    bonus: Mapped[int] = mapped_column(Integer, default=0)
    items_json: Mapped[str] = mapped_column(Text, default="[]")
    place: Mapped[str] = mapped_column(String(200), default="")
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    update_type: Mapped[str] = mapped_column(String(64))
    payload_preview: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BusinessLocation(Base):
    __tablename__ = "business_locations"
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"), primary_key=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)


class Challenge(Base):
    __tablename__ = "challenges"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"), index=True)
    title: Mapped[str] = mapped_column(String(160))
    goal: Mapped[int] = mapped_column(Integer)
    reward_bonus: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChallengeClaim(Base):
    __tablename__ = "challenge_claims"
    __table_args__ = (
        UniqueConstraint("challenge_id", "customer_id", name="uq_challenge_customer"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    challenge_id: Mapped[str] = mapped_column(ForeignKey("challenges.id"))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ShopStaff(Base):
    __tablename__ = "shop_staff"
    __table_args__ = (UniqueConstraint("max_user_id", name="uq_shop_staff_user"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    business_id: Mapped[str] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    max_user_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="cashier")
    can_stats: Mapped[bool] = mapped_column(Boolean, default=True)
    can_earn: Mapped[bool] = mapped_column(Boolean, default=True)
    can_scan: Mapped[bool] = mapped_column(Boolean, default=True)
    can_edit: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ShopInvite(Base):
    __tablename__ = "shop_invites"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    business_id: Mapped[str] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    created_by: Mapped[int] = mapped_column(Integer)
    can_stats: Mapped[bool] = mapped_column(Boolean, default=True)
    can_earn: Mapped[bool] = mapped_column(Boolean, default=True)
    can_scan: Mapped[bool] = mapped_column(Boolean, default=True)
    can_edit: Mapped[bool] = mapped_column(Boolean, default=False)
    used_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PromoLink(Base):
    __tablename__ = "promo_links"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    program_id: Mapped[str] = mapped_column(
        ForeignKey("loyalty_programs.id", ondelete="CASCADE"), index=True
    )
    business_id: Mapped[str] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
