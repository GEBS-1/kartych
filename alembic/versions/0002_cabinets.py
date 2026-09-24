"""Locations and visit challenges for the unified QR cabinets."""

import sqlalchemy as sa

from alembic import op

revision = "0002_cabinets"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "business_locations" not in existing:
        op.create_table(
            "business_locations",
            sa.Column(
                "business_id", sa.String(36), sa.ForeignKey("businesses.id"), primary_key=True
            ),
            sa.Column("latitude", sa.Float(), nullable=False),
            sa.Column("longitude", sa.Float(), nullable=False),
        )
    if "challenges" not in existing:
        op.create_table(
            "challenges",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("business_id", sa.String(36), sa.ForeignKey("businesses.id"), nullable=False),
            sa.Column("title", sa.String(160), nullable=False),
            sa.Column("goal", sa.Integer(), nullable=False),
            sa.Column("reward_bonus", sa.Integer(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index("ix_challenges_business_id", "challenges", ["business_id"])
    if "challenge_claims" not in existing:
        op.create_table(
            "challenge_claims",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "challenge_id", sa.String(36), sa.ForeignKey("challenges.id"), nullable=False
            ),
            sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("challenge_id", "customer_id", name="uq_challenge_customer"),
        )


def downgrade():
    op.drop_table("challenge_claims")
    op.drop_table("challenges")
    op.drop_table("business_locations")
