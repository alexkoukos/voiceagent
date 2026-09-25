"""Config versions and magic links for changes after go-live (OP2).

Revision ID: 0015
Revises: 0014
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "config_versions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=False, index=True),
        sa.Column("status", sa.String(), nullable=False, server_default="published"),
        sa.Column("source", sa.String(), nullable=False, server_default="app"),
        sa.Column("author", sa.String(), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("changes", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("snapshot", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "admin_links",
        sa.Column("token_hash", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=False, index=True),
        sa.Column("staff_id", sa.String(), sa.ForeignKey("staff.id"), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("admin_links")
    op.drop_table("config_versions")
