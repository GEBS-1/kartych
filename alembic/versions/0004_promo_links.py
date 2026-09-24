"""One-off share links for shop promotions."""

import sqlalchemy as sa

from alembic import op

revision = "0004_promo_links"
down_revision = "0003_staff"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "promo_links" not in existing:
        op.create_table(
            "promo_links",
            sa.Column("id", sa.String(16), primary_key=True),
            sa.Column(
                "program_id",
                sa.String(36),
                sa.ForeignKey("loyalty_programs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "business_id",
                sa.String(36),
                sa.ForeignKey("businesses.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("created_by", sa.Integer(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index("ix_promo_links_program_id", "promo_links", ["program_id"])
        op.create_index("ix_promo_links_business_id", "promo_links", ["business_id"])


def downgrade():
    op.drop_table("promo_links")
