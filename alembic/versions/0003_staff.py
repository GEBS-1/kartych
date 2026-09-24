"""Shop cashiers, invites, and limited admin permissions."""

import sqlalchemy as sa

from alembic import op

revision = "0003_staff"
down_revision = "0002_cabinets"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "shop_staff" not in existing:
        op.create_table(
            "shop_staff",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "business_id",
                sa.String(36),
                sa.ForeignKey("businesses.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("max_user_id", sa.Integer(), nullable=False),
            sa.Column("kind", sa.String(20), nullable=False, server_default="cashier"),
            sa.Column("can_stats", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("can_earn", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("can_scan", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("can_edit", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("max_user_id", name="uq_shop_staff_user"),
        )
        op.create_index("ix_shop_staff_business_id", "shop_staff", ["business_id"])
        op.create_index("ix_shop_staff_max_user_id", "shop_staff", ["max_user_id"])
    if "shop_invites" not in existing:
        op.create_table(
            "shop_invites",
            sa.Column("id", sa.String(16), primary_key=True),
            sa.Column(
                "business_id",
                sa.String(36),
                sa.ForeignKey("businesses.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("created_by", sa.Integer(), nullable=False),
            sa.Column("can_stats", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("can_earn", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("can_scan", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("can_edit", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("used_by", sa.Integer(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index("ix_shop_invites_business_id", "shop_invites", ["business_id"])


def downgrade():
    op.drop_table("shop_invites")
    op.drop_table("shop_staff")
