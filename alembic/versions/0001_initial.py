"""initial loyalty tables

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "businesses",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("owner_max_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_businesses_owner_max_user_id", "businesses", ["owner_max_user_id"])

    op.create_table(
        "loyalty_programs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("business_id", sa.String(length=36), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False, server_default="stamp_card"),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("stamps_required", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("reward_title", sa.String(length=160), nullable=False, server_default="Бесплатный кофе"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "customers",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("business_id", sa.String(length=36), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("max_user_id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("username", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("business_id", "max_user_id", name="uq_customer_business_max"),
    )
    op.create_index("ix_customers_max_user_id", "customers", ["max_user_id"])

    op.create_table(
        "visits",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("customer_id", sa.String(length=36), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("program_id", sa.String(length=36), sa.ForeignKey("loyalty_programs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False, server_default="qr"),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("idempotency_key"),
    )

    op.create_table(
        "processed_updates",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("update_type", sa.String(length=64), nullable=False),
        sa.Column("payload_preview", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("fingerprint"),
    )
    op.create_index("ix_processed_updates_fingerprint", "processed_updates", ["fingerprint"], unique=True)


def downgrade() -> None:
    op.drop_table("processed_updates")
    op.drop_table("visits")
    op.drop_table("customers")
    op.drop_table("loyalty_programs")
    op.drop_table("businesses")
