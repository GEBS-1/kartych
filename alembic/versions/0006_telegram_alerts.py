"""Telegram alert bot settings and subscribers."""

import sqlalchemy as sa

from alembic import op

revision = "0006_telegram_alerts"
down_revision = "0005_verify_staff_archive"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "telegram_settings" not in tables:
        op.create_table(
            "telegram_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("bot_token", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("bot_username", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("webhook_secret", sa.String(length=64), nullable=False, server_default=""),
        )
    if "telegram_subscribers" not in tables:
        op.create_table(
            "telegram_subscribers",
            sa.Column("chat_id", sa.Integer(), primary_key=True),
            sa.Column("display_name", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("username", sa.String(length=160), nullable=True),
            sa.Column("added_by", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "telegram_subscribers" in tables:
        op.drop_table("telegram_subscribers")
    if "telegram_settings" in tables:
        op.drop_table("telegram_settings")
