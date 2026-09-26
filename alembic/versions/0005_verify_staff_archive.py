"""Business INN, staff schedule, archived promotions."""

import sqlalchemy as sa

from alembic import op

revision = "0005_verify_staff_archive"
down_revision = "0004_promo_links"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    if table not in sa.inspect(bind).get_table_names():
        return set()
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _add(table: str, column: str, spec) -> None:
    if table in sa.inspect(op.get_bind()).get_table_names() and column not in _columns(table):
        op.add_column(table, spec)


def upgrade():
    _add("businesses", "inn", sa.Column("inn", sa.String(12), nullable=False, server_default=""))
    _add(
        "businesses",
        "director_name",
        sa.Column("director_name", sa.String(160), nullable=False, server_default=""),
    )
    _add("businesses", "verified_at", sa.Column("verified_at", sa.DateTime(timezone=True)))
    _add("loyalty_programs", "archived_at", sa.Column("archived_at", sa.DateTime(timezone=True)))
    for table in ("shop_staff", "shop_invites"):
        _add(
            table,
            "schedule_days",
            sa.Column(
                "schedule_days",
                sa.String(80),
                nullable=False,
                server_default="mon,tue,wed,thu,fri",
            ),
        )
        _add(
            table,
            "shift_from",
            sa.Column("shift_from", sa.String(5), nullable=False, server_default="10:00"),
        )
        _add(
            table,
            "shift_to",
            sa.Column("shift_to", sa.String(5), nullable=False, server_default="22:00"),
        )


def downgrade():
    for table in ("shop_staff", "shop_invites"):
        for column in ("shift_to", "shift_from", "schedule_days"):
            if column in _columns(table):
                op.drop_column(table, column)
    if "archived_at" in _columns("loyalty_programs"):
        op.drop_column("loyalty_programs", "archived_at")
    for column in ("verified_at", "director_name", "inn"):
        if column in _columns("businesses"):
            op.drop_column("businesses", column)
